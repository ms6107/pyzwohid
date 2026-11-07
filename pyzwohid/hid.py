# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based USB HID transport for ZWO astronomy devices.

ZWO USB HID devices communicate via HID Feature Reports over USB Control
Endpoint 0 using a shared `7e 5a` (`"~Z"`) framing protocol:

- **Host -> Device (`SET_REPORT`, Report ID `0x03`, 16 bytes):**
  `03 7e 5a <payload> 00 ...` (zero-padded to 16 bytes).
- **Device -> Host (`GET_REPORT`, Report ID `0x01`, 12–16 bytes):**
  `01 7e 5a <selector> <data ...>`, where `reply[3]` echoes `payload[-1]`.
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
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, func, *args)


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")


class ZwoError(Exception):
    """Error raised when a ZWO USB HID operation or protocol check fails."""


class ZwoDevicePathError(ZwoError):
    """Error raised when a path does not match an enumerated device for the ZWO HID class."""


class ZwoQueryCollisionError(ZwoError):
    """Error raised when concurrent queries from another OS process repeatedly overwrite the MCU reply buffer."""


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
    last-close penalty (`hid_hw_open` / `usb_kill_urb` on the interrupt URB), and
    all `hidapi` C calls are serialized on `_EXECUTOR` (1 worker thread).
    """

    VID = 0x03C3
    PIDS: tuple[int, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.PIDS:
            raise TypeError(f"{cls.__name__} must define a non-empty PIDS tuple")

    def __init__(self, path: str, device: Any) -> None:
        self.path = path
        self._device = device

    @classmethod
    def _open_device(cls, path: str) -> Any:
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
        if raw_future.exception() is None:
            raw_future.result().close()

    @classmethod
    async def _open_handle(cls, path: str) -> Any:
        raw_future = _EXECUTOR.submit(cls._open_device, path)
        try:
            return await asyncio.shield(asyncio.wrap_future(raw_future))
        except BaseException:
            if not raw_future.cancel() and (not raw_future.done() or raw_future.exception() is None):
                _EXECUTOR.submit(cls._close_unclaimed, raw_future)
            raise

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify and open the OS HID device node at `path`."""
        return cls(path, await cls._open_handle(path))

    def _close_device(self) -> None:
        self._device.close()

    async def close(self) -> None:
        """Close the underlying HID device handle."""
        await asyncio.shield(_run_in_executor(self._close_device))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @classmethod
    def _discover_paths(cls) -> list[str]:
        backend = _get_hid()
        return sorted({os.fsdecode(info["path"]) for pid in cls.PIDS for info in backend.enumerate(cls.VID, pid)})

    @classmethod
    async def discover(cls) -> list[str]:
        """Return sorted HID device paths matching `cls.VID` and `cls.PIDS`."""
        return await _run_in_executor(cls._discover_paths)

    def _send_sync(self, payload: bytes) -> None:
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        try:
            self._device.send_feature_report(report)
        except Exception as exc:
            raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc

    async def _send(self, payload: bytes) -> None:
        """Send a 16-byte `[03 7e 5a <payload>...]` feature report."""
        await _run_in_executor(self._send_sync, payload)

    def _query_sync(self, payload: bytes, reply_len: int = _REPLY_LEN) -> bytes:
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        for _ in range(_QUERY_ATTEMPTS):
            try:
                self._device.send_feature_report(report)
                reply = bytes(self._device.get_feature_report(_REPLY_REPORT_ID, _REPLY_LEN))
            except Exception as exc:
                raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc
            if len(reply) < reply_len or not reply.startswith(_REPLY_HEADER):
                raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
            # Every query echoes its selector (payload[-1]) in reply[3]; a mismatch means
            # another OS process overwrote the MCU's single reply buffer before our read.
            if reply[3] == payload[-1]:
                return reply
        raise ZwoQueryCollisionError(f"HID query collision for {payload.hex(' ')}: {reply.hex(' ')}")

    async def _query(self, payload: bytes, reply_len: int = _REPLY_LEN) -> bytes:
        """Send `[03 7e 5a <payload>...]` and read its `[01 7e 5a <selector>...]` reply.

        `send_feature_report` (`SET_REPORT`) is a synchronous USB Control Transfer (~3 ms)
        that populates the MCU's single `0x01` reply register in RAM before returning, and
        `get_feature_report` (`GET_REPORT`, ~3 ms) reads that register non-destructively
        (up to 16 bytes: 15 payload bytes + 1 Report ID byte as declared in the USB HID
        report descriptor; `get_feature_report` slices to the actual transfer length
        returned by the hardware, e.g. 16 bytes for status/firmware/serial queries and
        12 bytes for 8-byte alias queries).

        Within this process, `_EXECUTOR` serializes each `_query_sync` call end-to-end.

        If two independent OS processes open the same HID device and their queries with
        different selectors interleave (`A: send -> B: send -> A: get -> B: get`), `B`'s
        send overwrites the MCU reply register so both reads return `B`'s reply (never
        swapped). `B` matches `reply[3] == payload[-1]` on attempt 1 and exits, leaving
        `A` uncontended to succeed on the immediate next attempt without sleep or backoff.
        Raises `ZwoQueryCollisionError` if all `_QUERY_ATTEMPTS` attempts collide.
        (Verified on Linux `hidraw` only so far; macOS/Windows HID backends are untested.)
        """
        return await _run_in_executor(self._query_sync, payload, reply_len)

    async def get_firmware_version(self) -> tuple[int, int, int]:
        """Query (`02 04`) and return the `(major, minor, build)` firmware version."""
        reply = await self._query(_CMD_GET_FIRMWARE)
        major, minor, build = reply[4:7]
        if (major, minor, build) == (0, 0, 0):
            raise ZwoError(f"Invalid firmware version 0.0.0: {reply.hex(' ')}")
        return (major, minor, build)

    async def get_serial_number(self) -> str:
        """Query (`02 0c`) and return the 8-byte hardware serial number as a 16-character hex string."""
        reply = await self._query(_CMD_GET_SERIAL_NUMBER, reply_len=_ID_REPLY_LEN)
        return reply[4:12].hex()

    async def get_alias(self) -> str:
        """Query (`02 0d`) and return the 8-byte ASCII flash alias (`""` if unset)."""
        reply = await self._query(_CMD_GET_ALIAS, reply_len=_ID_REPLY_LEN)
        raw_alias = reply[4:12].split(b"\x00", 1)[0]
        try:
            return raw_alias.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ZwoError(f"Non-ASCII alias in HID reply: {reply.hex(' ')}") from exc

    async def set_alias(self, alias: str) -> None:
        """Write an 0..8-character printable ASCII alias to MCU flash (`0d <alias_8>`)."""
        if len(alias) > _ID_LEN or not (alias.isascii() and alias.isprintable()):
            raise ValueError(f"alias must be 0..{_ID_LEN} printable ASCII characters, got {alias!r}")
        await self._send(_CMD_SET_ALIAS + alias.encode("ascii").ljust(_ID_LEN, b"\x00"))

    async def clear_error(self) -> None:
        """Send a fire-and-forget command (`01 0f`) to clear latched MCU hardware error state (`0x06`)."""
        await self._send(_CMD_CLEAR_ERROR)

    async def get_status(self) -> _StatusT:
        """Query and return the current device status snapshot."""
        raise NotImplementedError

    async def _wait_until_settled(self, timeout: float, operation: str) -> _StatusT:
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
