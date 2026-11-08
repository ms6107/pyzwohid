# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based USB HID transport for ZWO astronomy devices.

ZWO USB HID devices communicate via HID Feature Reports over USB Control
Endpoint 0 (`EP0`) using a shared `7e 5a` (`"~Z"`) framing protocol:

- **Host -> Device (`SET_REPORT`, Report ID `0x03`, 16 bytes):**
  `03 7e 5a <payload> 00 ...` (zero-padded to 16 bytes).
- **Device -> Host (`GET_REPORT`, Report ID `0x01`, 12–16 bytes):**
  `01 7e 5a <selector> <data ...>`, where `reply[3]` echoes `payload[-1]`
  (except EAF Pro `02 1e` battery queries, which return `reply[3] == 0x00`).

On physical ZWO hardware, the USB HID report descriptor declares Report ID `0x01`
as 15 payload bytes plus 1 Report ID byte (16 bytes total). Standard status,
firmware, error, and battery queries return 16 bytes, whereas 8-byte identity
queries (`02 0c` serial number and `02 0d` alias) return 12–16 bytes (`01 7e 5a`
+ selector + 8 payload bytes), and unsupported commands on older firmware return
a 1-byte (`b"\\x01"`) or unpopulated buffer.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Any, Generic, Self, TypeVar

_CMD_HEADER = b"\x03\x7e\x5a"
_CMD_LEN = 16

_REPLY_REPORT_ID = 0x01
_REPLY_HEADER = b"\x01\x7e\x5a"
_REPLY_LEN = 16
_ID_LEN = 8
_ID_REPLY_LEN = 12
_QUERY_ATTEMPTS = 3
_POLL_INTERVAL_S = 0.2
_CMD_GET_FIRMWARE = b"\x02\x04"
_CMD_GET_SERIAL_NUMBER = b"\x02\x0c"
_CMD_GET_ALIAS = b"\x02\x0d"
_CMD_SET_ALIAS = b"\x0d"
_CMD_CLEAR_ERROR = b"\x01\x0f"

# All hidapi calls execute on a single dedicated worker thread so that hid_init,
# hid_enumerate, hid_open_path, hid_close, and per-device feature-report I/O never
# race on hidapi's unsynchronized global state or on an open hid_device handle.
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_T = TypeVar("_T")
_StatusT = TypeVar("_StatusT", bound="ZwoHidStatus")


async def _run_in_executor(func: Callable[..., _T], *args: Any) -> _T:
    """Dispatch a blocking `hidapi` operation onto the single-worker `_EXECUTOR` thread."""
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, func, *args)


def _validate_timeout(timeout: float) -> None:
    """Validate that a polling `timeout` in seconds is a positive finite number."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")


class ZwoError(Exception):
    """Base exception for ZWO USB HID I/O failures, hardware faults, or wire-protocol errors."""


class ZwoBusyError(ZwoError):
    """Error raised when a motion or configuration command is rejected because the device is already moving."""


class ZwoDevicePathError(ZwoError):
    """Error raised by `open(path)` when `path` does not match an enumerated device for the class `(VID, PIDS)`."""


class ZwoQueryCollisionError(ZwoError):
    """Error raised when another OS process repeatedly overwrites the MCU's single `0x01` reply register."""


@dataclass(frozen=True, slots=True)
class ZwoHidStatus:
    """Base status snapshot for ZWO USB HID devices.

    Attributes:
        moving: True while the device is moving or calibrating.
    """

    moving: bool


def _get_hid() -> Any:
    """Import `hid` lazily so unit tests can run without `libhidapi` installed."""
    import hid  # type: ignore[import-untyped]

    return hid


class ZwoHidDevice(Generic[_StatusT]):
    """Path-based base class for ZWO USB HID devices (`03 7e 5a` framing).

    `await cls.open(path)` opens the OS HID device node and holds the handle open
    across `_send()` and `_query()` calls until `await close()` (or `async with` exit).
    Holding the handle open avoids the ~45-50 ms Linux `hidraw` first-open /
    last-close penalty (`hid_hw_open` / `usb_kill_urb` on the interrupt URB),
    keeping steady-state feature-report queries at ~6 ms (`3 ms` `SET_REPORT` +
    `3 ms` `GET_REPORT`).

    All `hidapi` C calls are serialized on `_EXECUTOR` (1 worker thread) so
    `hidapi` global state and open `hid_device` handles never race across threads,
    while fast (~3-6 ms) USB transactions across multiple devices interleave
    between `asyncio.sleep(0.2)` polling intervals. Methods assume one caller per
    physical device at a time. Concurrent operations across different devices are
    supported. Executor serialization protects individual HID transactions; it does
    not make whole controller operations atomic.
    """

    VID = 0x03C3
    PIDS: tuple[int, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Enforce at class definition time that every subclass defines a non-empty `PIDS` tuple."""
        super().__init_subclass__(**kwargs)
        if not cls.PIDS:
            raise TypeError(f"{cls.__name__} must define a non-empty PIDS tuple")

    def __init__(self, path: str, device: Any) -> None:
        """Initialize with `path` and an already opened `hid.Device` handle (prefer `await cls.open(path)`)."""
        self.path = path
        self._device = device

    @classmethod
    def _open_device(cls, path: str) -> Any:
        """Resolve symlinks, verify `path` against enumerated `(VID, PIDS)`, and open `hid.Device` synchronously.

        Uses `os.fsencode(path)` for lossless round-tripping of raw OS `bytes` paths
        and `os.path.realpath()` when `path` is a symlink (such as
        `/dev/hidraw/by-path/<ID_PATH>` or a custom `udev` symlink) so it matches the
        canonical `/dev/hidrawN` path returned by `hid.enumerate()`. Verifying membership
        in `valid_paths` before calling `backend.Device(path=raw_path)` ensures
        non-matching or wrong-class device nodes never open an OS file descriptor.
        """
        raw_path = os.fsencode(path)
        if os.path.islink(raw_path):
            raw_path = os.path.realpath(raw_path)
        backend = _get_hid()
        try:
            valid_paths = {info["path"] for pid in cls.PIDS for info in backend.enumerate(cls.VID, pid)}
            if raw_path in valid_paths:
                return backend.Device(path=raw_path)
        except Exception as exc:
            raise ZwoError(f"HID open failed on {path!r}: {exc}") from exc
        raise ZwoDevicePathError(f"Device at {path!r} does not match {cls.__name__}")

    @staticmethod
    def _close_unclaimed(raw_future: Future[Any]) -> None:
        """Close an opened `hid.Device` handle on `_EXECUTOR` if its awaiting task was cancelled during open."""
        if raw_future.exception() is None:
            raw_future.result().close()

    @classmethod
    async def _open_handle(cls, path: str) -> Any:
        """Open the `hid.Device` at `path` on `_EXECUTOR` with cancellation-safe handle cleanup.

        If the caller task is cancelled while `_open_device` is queued or executing in
        `_EXECUTOR`, `raw_future.cancel()` is attempted first; if the job is already
        running or completed without error, `_close_unclaimed` is scheduled on
        `_EXECUTOR` so the OS file descriptor is never leaked.
        """
        raw_future = _EXECUTOR.submit(cls._open_device, path)
        try:
            return await asyncio.shield(asyncio.wrap_future(raw_future))
        except BaseException:
            if not raw_future.cancel() and (not raw_future.done() or raw_future.exception() is None):
                _EXECUTOR.submit(cls._close_unclaimed, raw_future)
            raise

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify `(VID, PIDS)` and open a persistent OS HID handle at `path`.

        Accepts either a direct `/dev/hidrawN` path returned by `await cls.discover()`
        or a persistent `udev` symlink (e.g. `/dev/hidraw/by-path/<ID_PATH>`), which is
        resolved via `os.path.realpath()` before matching against `hid.enumerate()`
        while preserving the caller's `path` on `self.path`.

        Raises:
            ZwoDevicePathError: If `path` does not match an enumerated device for `cls`.
            ZwoError: If `hid.enumerate()` or `hid.Device(path=...)` fails at the OS/HID layer.
        """
        return cls(path, await cls._open_handle(path))

    def _close_device(self) -> None:
        """Close the underlying `hid.Device` handle synchronously on `_EXECUTOR`."""
        self._device.close()

    async def close(self) -> None:
        """Close the persistent OS HID device handle.

        Cancellation of the caller does not cancel the scheduled close; cleanup may
        complete after the caller receives `CancelledError`. Closing the host handle
        sends no USB packet to the MCU; any motion already in progress on the hardware
        continues autonomously until settled.
        """
        await asyncio.shield(_run_in_executor(self._close_device))

    async def __aenter__(self) -> Self:
        """Enter the async context manager, returning `self`."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the async context manager, closing the underlying HID handle."""
        await self.close()

    @classmethod
    def _discover_paths(cls) -> list[str]:
        """Enumerate `(cls.VID, pid)` synchronously on `_EXECUTOR` and return sorted decoded paths."""
        backend = _get_hid()
        return sorted({os.fsdecode(info["path"]) for pid in cls.PIDS for info in backend.enumerate(cls.VID, pid)})

    @classmethod
    async def discover(cls) -> list[str]:
        """Return sorted, deduplicated HID device paths matching `cls.VID` and `cls.PIDS`.

        Raw OS `bytes` paths from `hid.enumerate()` are decoded via `os.fsdecode()` so
        arbitrary filesystem paths round-trip losslessly when passed to `await cls.open(path)`.
        Note that ZWO USB descriptors do not expose unique USB `iSerial` strings (either
        omitted or generic `"123456"`), so multiple identical devices should be
        disambiguated via `/dev/hidraw/by-path/<ID_PATH>`, `get_serial_number()`, or
        `get_alias()`.
        """
        return await _run_in_executor(cls._discover_paths)

    def _send_sync(self, payload: bytes) -> None:
        """Frame `[03 7e 5a <payload> 00...]` to 16 bytes and send via `SET_REPORT` on `_EXECUTOR`."""
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        try:
            self._device.send_feature_report(report)
        except Exception as exc:
            raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc

    async def _send(self, payload: bytes) -> None:
        """Send a fire-and-forget 16-byte `[03 7e 5a <payload>...]` feature report.

        Executes a synchronous ~3 ms USB Control EP0 `SET_REPORT` transfer on
        `_EXECUTOR` with no post-send delay.
        """
        await _run_in_executor(self._send_sync, payload)

    def _query_sync(
        self,
        payload: bytes,
        reply_len: int = _REPLY_LEN,
        override_expected_selector: int | None = None,
    ) -> bytes:
        """Send a feature report and read back its matching `01 7e 5a <selector>` reply on `_EXECUTOR`.

        Validation order is deliberate:
        1. `len(reply) < 4` or missing `01 7e 5a` header is rejected first with
           `ZwoError` so 1-byte `b"\\x01"` or unpopulated replies from unsupported
           commands on older firmware fail before indexing `reply[3]`.
        2. `reply[3] == expected_selector` is checked *before* `len(reply) < reply_len`
           so that a cross-process collision with a shorter 12-byte identity query
           (`02 0c` / `02 0d`) while awaiting a 16-byte status query is recognized as a
           selector collision and retried rather than raising `ZwoError`.
        """
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        expected_selector = payload[-1] if override_expected_selector is None else override_expected_selector
        for _ in range(_QUERY_ATTEMPTS):
            try:
                self._device.send_feature_report(report)
                reply = bytes(self._device.get_feature_report(_REPLY_REPORT_ID, _REPLY_LEN))
            except Exception as exc:
                raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc
            if len(reply) < 4 or not reply.startswith(_REPLY_HEADER):
                raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
            # Queries echo their selector (payload[-1], unless overridden) in reply[3]; a mismatch
            # means another OS process overwrote the MCU's single reply buffer before our read.
            if reply[3] == expected_selector:
                if len(reply) < reply_len:
                    raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
                return reply
        raise ZwoQueryCollisionError(f"HID query collision for {payload.hex(' ')}: {reply.hex(' ')}")

    async def _query(
        self,
        payload: bytes,
        reply_len: int = _REPLY_LEN,
        override_expected_selector: int | None = None,
    ) -> bytes:
        """Send `[03 7e 5a <payload>...]` and read its `[01 7e 5a <selector>...]` reply.

        `send_feature_report` (`SET_REPORT`) is a synchronous USB Control Transfer (~3 ms)
        that populates the MCU's single `0x01` reply register in RAM before returning, and
        `get_feature_report` (`GET_REPORT`, ~3 ms) reads that register non-destructively
        (up to 16 bytes: 15 payload bytes + 1 Report ID byte as declared in the USB HID
        report descriptor; `get_feature_report` slices to the actual transfer length
        returned by the hardware, e.g. 16 bytes for status/firmware/serial queries and
        12 bytes for 8-byte alias queries).

        `override_expected_selector` handles protocol exceptions such as EAF Pro
        `get_battery_status()` (`02 1e`), where the MCU replies with `reply[3] == 0x00`
        instead of echoing `0x1e`.

        Within this process, `_EXECUTOR` serializes each `_query_sync` call end-to-end.

        If two independent OS processes open the same HID device and their queries with
        different selectors interleave (`A: send -> B: send -> A: get -> B: get`), `B`'s
        send overwrites the MCU reply register so both reads return `B`'s reply (never
        swapped). `B` matches `reply[3] == expected_selector` on attempt 1 and exits,
        leaving `A` uncontended to succeed on the immediate next attempt without sleep or
        backoff. Raises `ZwoQueryCollisionError` if all `_QUERY_ATTEMPTS` attempts collide.
        (Verified on Linux `hidraw` only so far; macOS/Windows HID backends are untested.)
        """
        return await _run_in_executor(self._query_sync, payload, reply_len, override_expected_selector)

    async def get_firmware_version(self) -> tuple[int, int, int]:
        """Query (`02 04`) and return the `(major, minor, build)` firmware version.

        Unpacks `reply[4:7]` and rejects `0.0.0` with `ZwoError`. Note that `reply[7:15]`
        of the underlying 16-byte report also carries the null-terminated ASCII hardware
        model tag (e.g. `"EFW-S-0"`, `"EEFW-S-0"`, `"EAF-S-0"`, `"EAF-0-0"`, `"EEAF-S-0"`).
        """
        reply = await self._query(_CMD_GET_FIRMWARE)
        major, minor, build = reply[4:7]
        if (major, minor, build) == (0, 0, 0):
            raise ZwoError(f"Invalid firmware version 0.0.0: {reply.hex(' ')}")
        return (major, minor, build)

    async def get_serial_number(self) -> str:
        """Query (`02 0c`) and return the 8-byte hardware serial number as a 16-character hex string.

        Supported on devices with firmware `> 3.0.8` (including Gen 2 EFW and EAF Pro).
        Reads the raw 8-byte MCU unique ID (`reply[4:12]`, `reply_len=12`) directly.
        Unlike the USB device descriptor `iSerial` (which is omitted or generic
        `"123456"`), this identifier is unique per MCU.
        """
        reply = await self._query(_CMD_GET_SERIAL_NUMBER, reply_len=_ID_REPLY_LEN)
        return reply[4:12].hex()

    async def get_alias(self) -> str:
        """Query (`02 0d`) and return the 8-byte ASCII flash alias (`""` if unset).

        Reads the 8-byte alias field (`reply[4:12]`, `reply_len=12`), strips trailing
        `NUL` (`\\x00`) padding at the first `NUL` byte, and decodes strict ASCII.

        Raises:
            ZwoError: If the HID query fails or MCU flash contains non-ASCII bytes.
        """
        reply = await self._query(_CMD_GET_ALIAS, reply_len=_ID_REPLY_LEN)
        raw_alias = reply[4:12].split(b"\x00", 1)[0]
        try:
            return raw_alias.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ZwoError(f"Non-ASCII alias in HID reply: {reply.hex(' ')}") from exc

    async def set_alias(self, alias: str) -> None:
        """Write a 0..8-character printable ASCII alias to non-volatile MCU flash (`0d <alias_8>`).

        Whereas `get_alias()` sends category `0x02` selector `0x0d` (`03 7e 5a 02 0d`),
        `set_alias()` places `0x0d` directly in category byte `tx[3]`
        (`03 7e 5a 0d <c0..c7> 00 00 00 00`) so all 8 ASCII bytes fit in `tx[4:12]`.
        Passing `""` clears the stored alias (`8` zero bytes). Returns after sending
        the command, without readback confirmation (call `await get_alias()` to verify
        the stored value). Because this command writes directly to non-volatile MCU
        flash, avoid calling it in tight loops.

        Raises:
            ValueError: If `alias` exceeds 8 characters or contains non-ASCII /
                non-printable characters (such as embedded `NUL` or control bytes).
            ZwoError: If the underlying HID transfer fails.
        """
        if len(alias) > _ID_LEN or not (alias.isascii() and alias.isprintable()):
            raise ValueError(f"alias must be 0..{_ID_LEN} printable ASCII characters, got {alias!r}")
        await self._send(_CMD_SET_ALIAS + alias.encode("ascii").ljust(_ID_LEN, b"\x00"))

    async def clear_error(self) -> None:
        """Send a fire-and-forget command (`01 0f`) to clear latched MCU hardware error state (`0x06`).

        Unlatches hardware fault state (`state == 0x06`, such as an EFW Hall-sensor
        timeout / motor stall or EAF fault) in the MCU without requiring a USB replug
        or power cycle. Returns after sending the command, without readback
        confirmation (call `await get_status()` to verify the cleared state).
        """
        await self._send(_CMD_CLEAR_ERROR)

    async def get_status(self) -> _StatusT:
        """Query and return the current device status snapshot (`_StatusT`)."""
        raise NotImplementedError

    async def _wait_until_settled(self, timeout: float, operation: str) -> _StatusT:
        """Poll `get_status()` every 200 ms until `status.moving` is `False` or `timeout` expires.

        - The polling deadline starts when `_wait_until_settled()` is entered (after
          the motion or calibration command has been sent).
        - Each requested sleep is capped to the remaining time (`min(0.2, remaining)`).
        - Event-loop scheduling, executor queueing, and blocking HID calls can delay
          completion; `timeout` is not a hard end-to-end deadline.
        - A settled final response (`moving=False`) takes precedence over deadline
          expiry.
        - Timeout or task cancellation ends host polling; it does not abort hardware
          motion on the MCU.
        """
        deadline = monotonic() + timeout
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{operation} timed out after {timeout:.2f}s")
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
            # The final query waking at `deadline` may finish slightly past `deadline`;
            # if the device has settled by then, return the settled status rather than raising.
            status = await self.get_status()
            if not status.moving:
                return status
