# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests and shared fake backend for pyzwohid.hid."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Self
from unittest.mock import patch

from pyzwohid import ZwoDevicePathError, ZwoError, ZwoHidDevice, ZwoHidStatus, ZwoQueryCollisionError
from pyzwohid.hid import _validate_timeout

DEV_PATH = "/dev/hidraw10"
SECOND_DEV_PATH = "/dev/hidraw11"


def _dummy_status_report(*, moving: bool = False) -> bytes:
    return bytes((0x01, 0x7E, 0x5A, 0x01, int(moving))).ljust(16, b"\x00")


def _firmware_report(*, version: tuple[int, int, int] = (3, 2, 2)) -> bytes:
    major, minor, build = version
    return bytes((0x01, 0x7E, 0x5A, 0x04, major, minor, build)).ljust(16, b"\x00")


class FakeHidBackend:
    """In-memory fake for the `hid` module and `hid.Device`."""

    DEVICES: tuple[tuple[int, int, bytes], ...] = (
        (ZwoHidDevice.VID, 0x1F01, b"/dev/hidraw11"),
        (ZwoHidDevice.VID, 0x1F01, b"/dev/hidraw10"),
        (ZwoHidDevice.VID, 0x1F02, b"/dev/hidraw10"),
        (ZwoHidDevice.VID, 0x1F10, b"/dev/hidraw12"),  # Different ZWO PID
        (0x1234, 0x1F01, b"/dev/hidraw13"),  # Non-ZWO VID with same PID
    )
    DEFAULT_REPLY: bytes = _dummy_status_report()

    def __init__(
        self,
        *,
        replies: list[bytes] | None = None,
        replies_by_path: dict[bytes, list[bytes]] | None = None,
        open_error: Exception | None = None,
        io_error: Exception | None = None,
        on_open: Callable[[bytes], None] | None = None,
        on_send: Callable[[bytes, bytes], None] | None = None,
    ) -> None:
        self.replies = list(replies or [self.DEFAULT_REPLY])
        self.replies_by_path = {path: list(path_replies) for path, path_replies in (replies_by_path or {}).items()}
        self.open_error = open_error
        self.io_error = io_error
        self.on_open = on_open
        self.on_send = on_send
        self.open_count = 0
        self.close_count = 0
        self.sent_reports: list[bytes] = []
        self.events: list[tuple[str, bytes]] = []
        self._active_calls = 0
        self.max_concurrent_calls = 0
        self._patcher = patch.dict(sys.modules, {"hid": self})

    @contextlib.contextmanager
    def _track_call(self, op: str, path: bytes = b"") -> Iterator[None]:
        self._active_calls += 1
        self.max_concurrent_calls = max(self.max_concurrent_calls, self._active_calls)
        self.events.append((op, path))
        try:
            yield
        finally:
            self._active_calls -= 1

    def __enter__(self) -> Self:
        self._patcher.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._patcher.stop()

    def enumerate(self, vid: int = 0, pid: int = 0) -> list[dict[str, int | bytes]]:
        with self._track_call("enumerate"):
            return [
                {"vendor_id": d_vid, "product_id": d_pid, "path": d_path}
                for d_vid, d_pid, d_path in self.DEVICES
                if vid in (0, d_vid) and pid in (0, d_pid)
            ]

    def Device(self, *, path: bytes) -> FakeHidDevice:
        with self._track_call("open", path):
            if self.on_open is not None:
                self.on_open(path)
            if self.open_error is not None:
                raise self.open_error
            self.open_count += 1
            return FakeHidDevice(self, path)


class FakeHidDevice:
    """HID device handle created by FakeHidBackend.Device."""

    def __init__(self, backend: FakeHidBackend, path: bytes) -> None:
        self._backend = backend
        self.path = path
        self.closed = False

    def send_feature_report(self, data: bytes) -> int:
        with self._backend._track_call("send", self.path):
            if self.closed:
                raise OSError("device closed")
            if self._backend.io_error is not None:
                raise self._backend.io_error
            payload = bytes(data)
            self._backend.sent_reports.append(payload)
            if self._backend.on_send is not None:
                self._backend.on_send(self.path, payload)
            return len(data)

    def get_feature_report(self, report_id: int, size: int) -> bytes:
        with self._backend._track_call("get", self.path):
            if self.closed:
                raise OSError("device closed")
            if self._backend.io_error is not None:
                raise self._backend.io_error
            replies = self._backend.replies_by_path.get(self.path, self._backend.replies)
            if len(replies) > 1:
                return replies.pop(0)
            return replies[0]

    def close(self) -> None:
        with self._backend._track_call("close", self.path):
            if not self.closed:
                self.closed = True
                self._backend.close_count += 1


class _DummyHidDevice(ZwoHidDevice[ZwoHidStatus]):
    """Concrete ZwoHidDevice subclass for testing base transport and polling behavior."""

    PIDS = (0x1F01, 0x1F02)

    async def get_status(self) -> ZwoHidStatus:
        reply = await self._query(b"\x02\x01")
        return ZwoHidStatus(moving=reply[4] != 0)


class TestZwoHidDevice(unittest.IsolatedAsyncioTestCase):
    def test_subclass_requires_non_empty_pids(self) -> None:
        with self.assertRaises(TypeError):

            class _MissingPidsDevice(ZwoHidDevice[ZwoHidStatus]):
                pass

    async def test_discover_deduplicates_and_sorts_paths(self) -> None:
        with FakeHidBackend():
            self.assertEqual(await _DummyHidDevice.discover(), ["/dev/hidraw10", "/dev/hidraw11"])

    async def test_open_rejects_non_matching_device_path_and_resolves_symlink(self) -> None:
        for path in (
            "/dev/hidraw12",  # ZWO device with different PID (0x1F10)
            "/dev/hidraw13",  # Non-ZWO VID (0x1234) with matching PID (0x1F01)
            "/dev/hidraw99",  # Non-existent path
        ):
            with self.subTest(path=path), FakeHidBackend() as backend:
                with self.assertRaises(ZwoError) as ctx:
                    await _DummyHidDevice.open(path)
                self.assertIsInstance(ctx.exception, ZwoDevicePathError)
                self.assertEqual(backend.open_count, 0)

        with tempfile.TemporaryDirectory() as tmpdir, FakeHidBackend() as backend:
            link_path = os.path.join(tmpdir, "zwo-hid")
            os.symlink(DEV_PATH, link_path)
            async with await _DummyHidDevice.open(link_path) as dev:
                self.assertEqual(dev.path, link_path)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    async def test_open_close_and_io_exceptions_wrap_into_zwo_error(self) -> None:
        with FakeHidBackend(open_error=OSError("permission denied")), self.assertRaises(ZwoError):
            await _DummyHidDevice.open(DEV_PATH)

        with FakeHidBackend(io_error=OSError("input/output error")) as io_backend:
            with self.assertRaises(ZwoError):
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    await dev.get_status()
            self.assertEqual(io_backend.open_count, 1)
            self.assertEqual(io_backend.close_count, 1)

        with FakeHidBackend() as backend:
            closed_dev = await _DummyHidDevice.open(DEV_PATH)
            await closed_dev.close()
            await closed_dev.close()  # idempotent second close
            self.assertEqual(backend.close_count, 1)
            with self.assertRaises(ZwoError):
                await closed_dev.get_status()

    async def test_query_rejects_bad_header_and_truncated_reply(self) -> None:
        for label, report in (
            ("bad_header", bytes(16)),
            ("truncated_header_only", b"\x01\x7e\x5a"),
            ("truncated_15_bytes", _dummy_status_report()[:15]),
        ):
            with self.subTest(case=label), FakeHidBackend(replies=[report]) as backend:
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    with self.assertRaises(ZwoError) as ctx:
                        await dev.get_status()
                self.assertNotIsInstance(ctx.exception, ZwoQueryCollisionError)
                self.assertEqual(len(backend.sent_reports), 1)

    async def test_query_retries_on_selector_collision_and_raises_after_exhausting_retries(self) -> None:
        other_reply_16 = b"\x01\x7e\x5a\x04".ljust(16, b"\x00")
        other_reply_12 = b"\x01\x7e\x5a\x0d".ljust(12, b"\x00")
        replies = [other_reply_16, other_reply_12, _dummy_status_report(moving=False)]
        with FakeHidBackend(replies=replies) as backend:
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                self.assertEqual(await dev.get_status(), ZwoHidStatus(moving=False))
            self.assertEqual(len(backend.sent_reports), 3)

        with FakeHidBackend(replies=[other_reply_12]) as backend:
            with self.assertRaises(ZwoError) as ctx:
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    await dev.get_status()
            self.assertIsInstance(ctx.exception, ZwoQueryCollisionError)
            self.assertEqual(len(backend.sent_reports), 3)

        overridden_reply = b"\x01\x7e\x5a\x00".ljust(16, b"\x00")
        unoverridden_reply = b"\x01\x7e\x5a\x1e".ljust(16, b"\x00")
        with FakeHidBackend(replies=[unoverridden_reply, overridden_reply]) as backend:
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                self.assertEqual(
                    await dev._query(b"\x02\x1e", override_expected_selector=0x00),
                    overridden_reply,
                )
            self.assertEqual(len(backend.sent_reports), 2)

    async def test_get_firmware_version_and_rejects_zero_version(self) -> None:
        with FakeHidBackend(replies=[_firmware_report(version=(3, 5, 2))]) as backend:
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                self.assertEqual(await dev.get_firmware_version(), (3, 5, 2))
            self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00")])

        with FakeHidBackend(replies=[_firmware_report(version=(0, 0, 0))]):
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                with self.assertRaises(ZwoError):
                    await dev.get_firmware_version()

    async def test_get_serial_number_returns_8_byte_hex_and_rejects_short_reply(self) -> None:
        raw_sn = bytes.fromhex("000f020102000800")
        for reply_len in (12, 16):
            reply = (b"\x01\x7e\x5a\x0c" + raw_sn).ljust(reply_len, b"\xaa")
            with self.subTest(reply_len=reply_len), FakeHidBackend(replies=[reply]) as backend:
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    self.assertEqual(await dev.get_serial_number(), "000f020102000800")
                self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x0c".ljust(16, b"\x00")])

        with FakeHidBackend(replies=[b"\x01\x7e\x5a\x0c" + raw_sn[:7]]):
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                with self.assertRaises(ZwoError):
                    await dev.get_serial_number()

    async def test_get_and_set_alias_validates_and_formats_8_byte_ascii(self) -> None:
        read_cases = [
            ("unset_12_bytes", b"\x01\x7e\x5a\x0d" + bytes(8), ""),
            ("short_12_bytes", b"\x01\x7e\x5a\x0d5v1\x00\x00\x00\x00\x00", "5v1"),
            ("full_8_chars_16_bytes", (b"\x01\x7e\x5a\x0dABCDEFGH").ljust(16, b"\x00"), "ABCDEFGH"),
            ("stops_at_first_nul", b"\x01\x7e\x5a\x0d5v1\x00junk", "5v1"),
        ]
        for label, reply, expected in read_cases:
            with self.subTest(case=label), FakeHidBackend(replies=[reply]) as backend:
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    self.assertEqual(await dev.get_alias(), expected)
                self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x0d".ljust(16, b"\x00")])

        for bad_label, bad_reply in (
            ("truncated_11_bytes", b"\x01\x7e\x5a\x0d" + bytes(7)),
            ("non_ascii_bytes", b"\x01\x7e\x5a\x0d\xff\x00\x00\x00\x00\x00\x00\x00"),
        ):
            with self.subTest(bad_reply=bad_label), FakeHidBackend(replies=[bad_reply]):
                async with await _DummyHidDevice.open(DEV_PATH) as dev:
                    with self.assertRaises(ZwoError):
                        await dev.get_alias()

        with FakeHidBackend() as backend:
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                await dev.set_alias("5v1")
                await dev.set_alias("")
                await dev.set_alias("12345678")
                for bad_alias in ("123456789", "a\x00b", "a\nb", "é"):
                    with self.subTest(bad_alias=bad_alias), self.assertRaises(ValueError):
                        await dev.set_alias(bad_alias)

            self.assertEqual(
                backend.sent_reports,
                [
                    b"\x03\x7e\x5a\x0d5v1".ljust(16, b"\x00"),
                    b"\x03\x7e\x5a\x0d".ljust(16, b"\x00"),
                    b"\x03\x7e\x5a\x0d12345678".ljust(16, b"\x00"),
                ],
            )

    async def test_clear_error_sends_command(self) -> None:
        with FakeHidBackend() as backend:
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                await dev.clear_error()
            self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x01\x0f".ljust(16, b"\x00")])

    async def test_validate_timeout_and_wait_until_settled_polls_and_times_out(self) -> None:
        for bad_timeout in (0.0, -1.0, math.nan, math.inf):
            with self.subTest(timeout=bad_timeout), self.assertRaises(ValueError):
                _validate_timeout(bad_timeout)

        class _UnimplementedStatusDevice(ZwoHidDevice[ZwoHidStatus]):
            PIDS = (0x1F01,)

        with FakeHidBackend():
            async with await _UnimplementedStatusDevice.open(DEV_PATH) as raw_dev:
                with self.assertRaises(NotImplementedError):
                    await raw_dev.get_status()

        replies = [
            _dummy_status_report(moving=True),
            _dummy_status_report(moving=False),
        ]
        with (
            FakeHidBackend(replies=replies),
            patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
        ):
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                settled = await dev._wait_until_settled(5.0, "test_op()")
            self.assertEqual(settled, ZwoHidStatus(moving=False))
            self.assertEqual(mock_sleep.await_count, 2)

        with (
            FakeHidBackend(replies=[_dummy_status_report(moving=True)]),
            patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            patch("pyzwohid.hid.monotonic", side_effect=[0.0, 0.0, 0.01]),
            self.assertRaises(TimeoutError),
        ):
            async with await _DummyHidDevice.open(DEV_PATH) as dev:
                await dev._wait_until_settled(0.01, "test_op()")
        mock_sleep.assert_awaited_once_with(0.01)

    async def test_executor_queues_and_serializes_concurrent_calls_across_devices(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        first_send = True

        def block_first_send(path: bytes, _payload: bytes) -> None:
            nonlocal first_send
            if first_send and path == b"/dev/hidraw10":
                first_send = False
                entered.set()
                release.wait(timeout=5.0)

        with FakeHidBackend(
            replies_by_path={
                b"/dev/hidraw10": [_dummy_status_report(moving=False)],
                b"/dev/hidraw11": [_dummy_status_report(moving=True)],
            },
            on_send=block_first_send,
        ) as backend:
            dev1 = await _DummyHidDevice.open(DEV_PATH)
            dev2 = await _DummyHidDevice.open(SECOND_DEV_PATH)
            backend.events.clear()

            task1 = asyncio.create_task(dev1.get_status())
            self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

            task2 = asyncio.create_task(dev2.get_status())
            task3 = asyncio.create_task(dev1.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertFalse(task1.done())
            self.assertFalse(task2.done())
            self.assertFalse(task3.done())
            self.assertEqual(backend.close_count, 0)

            release.set()
            status1, status2, _ = await asyncio.gather(task1, task2, task3)
            await dev2.close()

        self.assertEqual(status1, ZwoHidStatus(moving=False))
        self.assertEqual(status2, ZwoHidStatus(moving=True))
        self.assertEqual(backend.max_concurrent_calls, 1)
        self.assertEqual(
            backend.events,
            [
                ("send", b"/dev/hidraw10"),
                ("get", b"/dev/hidraw10"),
                ("send", b"/dev/hidraw11"),
                ("get", b"/dev/hidraw11"),
                ("close", b"/dev/hidraw10"),
                ("close", b"/dev/hidraw11"),
            ],
        )

    async def test_concurrent_wait_until_settled_interleaves_polls_across_devices(self) -> None:
        poll_barrier = asyncio.Barrier(2)

        async def synchronized_sleep(_delay: float) -> None:
            await poll_barrier.wait()

        with (
            FakeHidBackend(
                replies_by_path={
                    b"/dev/hidraw10": [
                        _dummy_status_report(moving=True),
                        _dummy_status_report(moving=False),
                    ],
                    b"/dev/hidraw11": [
                        _dummy_status_report(moving=True),
                        _dummy_status_report(moving=False),
                    ],
                }
            ) as backend,
            patch("pyzwohid.hid.asyncio.sleep", side_effect=synchronized_sleep),
        ):
            async with (
                await _DummyHidDevice.open(DEV_PATH) as dev1,
                await _DummyHidDevice.open(SECOND_DEV_PATH) as dev2,
            ):
                backend.events.clear()
                settled1, settled2 = await asyncio.gather(
                    dev1._wait_until_settled(5.0, "op1"),
                    dev2._wait_until_settled(5.0, "op2"),
                )

        self.assertEqual((settled1, settled2), (ZwoHidStatus(moving=False), ZwoHidStatus(moving=False)))
        self.assertEqual(backend.max_concurrent_calls, 1)
        send_paths = [path for op, path in backend.events if op == "send"]
        self.assertEqual(len(send_paths), 4)
        self.assertEqual(set(send_paths[0:2]), {b"/dev/hidraw10", b"/dev/hidraw11"})
        self.assertEqual(set(send_paths[2:4]), {b"/dev/hidraw10", b"/dev/hidraw11"})

    async def test_cancellation_during_poll_query_open_and_close_closes_handle_safely(self) -> None:
        with self.subTest("cancelled_during_poll_sleep"):
            sleeping = asyncio.Event()
            never: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            async def block_in_sleep(_delay: float) -> None:
                sleeping.set()
                await never

            with (
                FakeHidBackend(replies=[_dummy_status_report(moving=True)]) as backend,
                patch("pyzwohid.hid.asyncio.sleep", side_effect=block_in_sleep),
            ):

                async def run_wait() -> ZwoHidStatus:
                    async with await _DummyHidDevice.open(DEV_PATH) as dev:
                        return await dev._wait_until_settled(5.0, "wait")

                task = asyncio.create_task(run_wait())
                await sleeping.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)

        with self.subTest("cancelled_during_in_flight_executor_query"):
            entered = threading.Event()
            release = threading.Event()

            def block_send(_path: bytes, _payload: bytes) -> None:
                entered.set()
                release.wait(timeout=5.0)

            with FakeHidBackend(on_send=block_send) as backend:

                async def run_query() -> ZwoHidStatus:
                    async with await _DummyHidDevice.open(DEV_PATH) as dev:
                        return await dev.get_status()

                query_task = asyncio.create_task(run_query())
                self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

                query_task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(backend.close_count, 0)

                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await query_task

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)
                self.assertEqual(
                    [(op, path) for op, path in backend.events if op != "enumerate"],
                    [
                        ("open", b"/dev/hidraw10"),
                        ("send", b"/dev/hidraw10"),
                        ("get", b"/dev/hidraw10"),
                        ("close", b"/dev/hidraw10"),
                    ],
                )

        with self.subTest("cancelled_during_in_flight_executor_open"):
            open_entered = threading.Event()
            open_release = threading.Event()

            def block_open(_path: bytes) -> None:
                open_entered.set()
                open_release.wait(timeout=5.0)

            with FakeHidBackend(on_open=block_open) as backend:

                async def run_open() -> ZwoHidStatus:
                    async with await _DummyHidDevice.open(DEV_PATH) as dev:
                        return await dev.get_status()

                open_task = asyncio.create_task(run_open())
                self.assertTrue(await asyncio.to_thread(open_entered.wait, 5.0))

                open_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await open_task

                open_release.set()
                await _DummyHidDevice.discover()

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)
                self.assertEqual(
                    [(op, path) for op, path in backend.events if op != "enumerate"],
                    [
                        ("open", b"/dev/hidraw10"),
                        ("close", b"/dev/hidraw10"),
                    ],
                )

        with self.subTest("cancelled_during_queued_aexit_close"):
            b_send_entered = threading.Event()
            b_send_release = threading.Event()

            def block_b_send(path: bytes, _payload: bytes) -> None:
                if path == os.fsencode(SECOND_DEV_PATH):
                    b_send_entered.set()
                    b_send_release.wait(timeout=5.0)

            with FakeHidBackend(
                replies_by_path={
                    os.fsencode(DEV_PATH): [_dummy_status_report(moving=False)],
                    os.fsencode(SECOND_DEV_PATH): [_dummy_status_report(moving=False)],
                },
                on_send=block_b_send,
            ) as backend:
                dev_a = await _DummyHidDevice.open(DEV_PATH)
                dev_b = await _DummyHidDevice.open(SECOND_DEV_PATH)

                async def exit_dev_a() -> None:
                    async with dev_a:
                        pass

                b_task = asyncio.create_task(dev_b.get_status())
                self.assertTrue(await asyncio.to_thread(b_send_entered.wait, 5.0))

                a_task = asyncio.create_task(exit_dev_a())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(backend.close_count, 0)

                a_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await a_task

                b_send_release.set()
                self.assertEqual(await b_task, ZwoHidStatus(moving=False))
                await dev_b.close()

                self.assertEqual(backend.open_count, 2)
                self.assertEqual(backend.close_count, 2)
                self.assertEqual(backend.max_concurrent_calls, 1)
                with self.assertRaises(ZwoError):
                    await dev_a.get_status()


if __name__ == "__main__":
    unittest.main()
