# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwohid.eaf."""

from __future__ import annotations

import asyncio
import math
import threading
import unittest
from typing import Any
from unittest.mock import patch

from pyzwohid import Eaf, EafStatus, ZwoDevicePathError, ZwoError
from tests.test_hid import FakeHidBackend, _firmware_report

DEV_PATH = "/dev/hidraw14"


def _status_report_legacy(
    *,
    state: int = 0x00,
    backlash: int = 0,
    speed: int = 512,
    step: int = 30_068,
    raw_temp: int = 0x82DD,
    flags: int = 0x32,
    max_step: int = 60_000,
) -> bytes:
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x03,
            state,
            backlash,
            (speed >> 8) & 0xFF,
            speed & 0xFF,
            (step >> 8) & 0xFF,
            step & 0xFF,
            0x00,
            (raw_temp >> 8) & 0xFF,
            raw_temp & 0xFF,
            flags,
            (max_step >> 8) & 0xFF,
            max_step & 0xFF,
        )
    )


def _status_report_modern(
    *,
    state: int = 0x00,
    backlash: int = 0,
    step: int = 120_000,
    raw_temp: int = 0x82DD,
    flags: int = 0x30,
    max_step: int = 300_000,
) -> bytes:
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x03,
            state,
            backlash,
            (max_step >> 16) & 0xFF,
            (step >> 16) & 0xFF,
            (step >> 8) & 0xFF,
            step & 0xFF,
            0x00,
            (raw_temp >> 8) & 0xFF,
            raw_temp & 0xFF,
            flags,
            (max_step >> 8) & 0xFF,
            max_step & 0xFF,
        )
    )


class FakeEafHidBackend(FakeHidBackend):
    """In-memory fake HID backend configured for ZWO EAF devices."""

    DEVICES = (
        (Eaf.VID, 0x1F10, b"/dev/hidraw15"),
        (Eaf.VID, 0x1F10, b"/dev/hidraw14"),
        (Eaf.VID, 0x1F01, b"/dev/hidraw10"),  # ZWO EFW filter wheel (same VID, different PID)
        (0x1234, 0x1F10, b"/dev/hidraw16"),  # Non-ZWO HID device (different VID, same PID)
    )

    def __init__(self, *, replies: list[bytes] | None = None, **kwargs: Any) -> None:
        super().__init__(replies=replies or [_firmware_report(), _status_report_legacy()], **kwargs)


class TestEaf(unittest.IsolatedAsyncioTestCase):
    async def test_discover_and_open_eaf_pids(self) -> None:
        with FakeEafHidBackend() as backend:
            self.assertEqual(await Eaf.discover(), ["/dev/hidraw14", "/dev/hidraw15"])
            with self.assertRaises(ZwoDevicePathError):
                await Eaf.open("/dev/hidraw10")
            self.assertEqual(backend.open_count, 0)

    async def test_open_reads_firmware_for_legacy_and_modern_versions(self) -> None:
        cases = [
            ("legacy_3_2_2", (3, 2, 2), 60_000),
            ("legacy_boundary_3_3_5", (3, 3, 5), 60_000),
            ("modern_3_3_7", (3, 3, 7), 600_000),
        ]
        for label, version, expected_range in cases:
            with (
                self.subTest(case=label),
                FakeEafHidBackend(replies=[_firmware_report(version=version)]) as backend,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    self.assertEqual(focuser.path, DEV_PATH)
                    self.assertEqual(focuser.firmware_version, version)
                    self.assertEqual(focuser.step_range, expected_range)

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00")])

    async def test_open_closes_handle_on_firmware_query_failure_or_cancellation(self) -> None:
        with FakeEafHidBackend(replies=[_firmware_report(version=(0, 0, 0))]) as backend:
            with self.assertRaises(ZwoError):
                await Eaf.open(DEV_PATH)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

        entered = threading.Event()
        release = threading.Event()

        def block_send(_path: bytes, _payload: bytes) -> None:
            entered.set()
            release.wait(timeout=5.0)

        with FakeEafHidBackend(on_send=block_send) as backend:
            open_task = asyncio.create_task(Eaf.open(DEV_PATH))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

            open_task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(backend.close_count, 0)

            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await open_task

            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)
            self.assertEqual(backend.max_concurrent_calls, 1)

    async def test_get_status_decodes_legacy_16bit_and_modern_24bit_reports(self) -> None:
        cases = [
            (
                "legacy_settled",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(
                    state=0x00,
                    backlash=12,
                    step=30_068,
                    raw_temp=0x82DD,
                    flags=0x32,
                    max_step=60_000,
                ),
                EafStatus(
                    moving=False,
                    step=30_068,
                    max_step=60_000,
                    temperature_c=35.01,
                    backlash=12,
                    reverse=True,
                    beep=False,
                    max_force=3,
                    hand_controller_active=False,
                ),
            ),
            (
                "legacy_hand_controller_moving_state_00_flags_bit2",
                _firmware_report(version=(3, 1, 9)),
                _status_report_legacy(
                    state=0x00,
                    backlash=0,
                    step=1_476,
                    raw_temp=0x8222,
                    flags=0x26,
                    max_step=23_000,
                ),
                EafStatus(
                    moving=True,
                    step=1_476,
                    max_step=23_000,
                    temperature_c=33.14,
                    backlash=0,
                    reverse=True,
                    beep=False,
                    max_force=2,
                    hand_controller_active=True,
                ),
            ),
            (
                "legacy_motor_moving_temp_disconnected_zero",
                _firmware_report(version=(3, 3, 5)),
                _status_report_legacy(
                    state=0x01,
                    backlash=0,
                    step=12_345,
                    raw_temp=0x0000,
                    flags=0x51,
                    max_step=50_000,
                ),
                EafStatus(
                    moving=True,
                    step=12_345,
                    max_step=50_000,
                    temperature_c=None,
                    backlash=0,
                    reverse=False,
                    beep=True,
                    max_force=5,
                    hand_controller_active=False,
                ),
            ),
            (
                "modern_settled_24bit_subzero_temp",
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(
                    state=0x00,
                    backlash=25,
                    step=123_456,
                    raw_temp=29_450,
                    flags=0x43,
                    max_step=600_000,
                ),
                EafStatus(
                    moving=False,
                    step=123_456,
                    max_step=600_000,
                    temperature_c=-5.5,
                    backlash=25,
                    reverse=True,
                    beep=True,
                    max_force=4,
                    hand_controller_active=False,
                ),
            ),
            (
                "modern_motor_moving_24bit_temp_disconnected_ffff",
                _firmware_report(version=(3, 4, 0)),
                _status_report_modern(
                    state=0x01,
                    backlash=5,
                    step=250_000,
                    raw_temp=0xFFFF,
                    flags=0x00,
                    max_step=400_000,
                ),
                EafStatus(
                    moving=True,
                    step=250_000,
                    max_step=400_000,
                    temperature_c=None,
                    backlash=5,
                    reverse=False,
                    beep=False,
                    max_force=0,
                    hand_controller_active=False,
                ),
            ),
        ]
        for label, fw_report, status_report, expected in cases:
            with self.subTest(case=label), FakeEafHidBackend(replies=[fw_report, status_report]) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    status = await focuser.get_status()
                self.assertEqual(status, expected)
                self.assertEqual(
                    backend.sent_reports,
                    [
                        b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00"),
                        b"\x03\x7e\x5a\x02\x03".ljust(16, b"\x00"),
                    ],
                )

    async def test_get_status_eaf_errors_raise_zwo_error(self) -> None:
        cases = [
            ("hardware_error_state_06", _firmware_report(version=(3, 2, 2)), _status_report_legacy(state=0x06)),
            ("unknown_state_02", _firmware_report(version=(3, 2, 2)), _status_report_legacy(state=0x02)),
            ("zero_max_step", _firmware_report(version=(3, 2, 2)), _status_report_legacy(step=0, max_step=0)),
            (
                "legacy_max_step_exceeds_60k",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(step=0, max_step=60_001),
            ),
            (
                "modern_max_step_exceeds_600k",
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(step=0, max_step=600_001),
            ),
            (
                "step_exceeds_max_step",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(step=40_001, max_step=40_000),
            ),
        ]
        for label, fw_report, bad_status in cases:
            with self.subTest(case=label), FakeEafHidBackend(replies=[fw_report, bad_status]) as backend:
                with self.assertRaises(ZwoError):
                    async with await Eaf.open(DEV_PATH) as focuser:
                        await focuser.get_status()
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    async def test_reset_position_legacy_and_modern_preserves_device_settings(self) -> None:
        with self.subTest("legacy_16bit"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=15, speed=512, step=30_068, flags=0x32, max_step=60_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.reset_position(25_000)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        15,  # backlash preserved
                        0x02,
                        0x00,  # speed = 512 preserved
                        (25_000 >> 8) & 0xFF,
                        25_000 & 0xFF,
                        0x01,  # opcode = reset_position
                        0x00,
                        0x00,
                        0x32,  # flags preserved
                        0xEA,
                        0x60,  # max_step = 60_000 preserved
                    )
                ),
            )

        with self.subTest("modern_24bit"):
            replies = [
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(state=0x00, backlash=8, step=120_000, flags=0x41, max_step=450_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.reset_position(200_000)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        8,  # backlash preserved
                        (450_000 >> 16) & 0xFF,  # max_step upper byte preserved
                        (200_000 >> 16) & 0xFF,
                        (200_000 >> 8) & 0xFF,
                        200_000 & 0xFF,
                        0x01,  # opcode = reset_position
                        0x00,
                        0x00,
                        0x41,  # flags preserved
                        (450_000 >> 8) & 0xFF,
                        450_000 & 0xFF,  # max_step lower 16 bits preserved
                    )
                ),
            )

    async def test_reset_position_validates_bounds_and_rejects_when_moving(self) -> None:
        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                for bad_step in (-1, 40_001):
                    with self.subTest(step=bad_step), self.assertRaises(ValueError):
                        await focuser.reset_position(bad_step)
            self.assertEqual(len(backend.sent_reports), 3)

        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                with self.assertRaises(ZwoError):
                    await focuser.reset_position(10_000)
            self.assertEqual(len(backend.sent_reports), 2)

    async def test_move_legacy_and_modern_sends_command_and_polls_until_settled(self) -> None:
        with self.subTest("legacy_16bit"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=4, speed=512, step=30_000, flags=0x32, max_step=60_000),
                _status_report_legacy(state=0x01, backlash=4, speed=512, step=30_050, flags=0x32, max_step=60_000),
                _status_report_legacy(state=0x00, backlash=4, speed=512, step=30_100, flags=0x32, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep"),
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.move(30_100)

            self.assertEqual(settled, 30_100)
            self.assertEqual(
                backend.sent_reports[2],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x01,  # run = 1
                        4,  # backlash preserved
                        0x02,
                        0x00,  # speed = 512 preserved
                        (30_100 >> 8) & 0xFF,
                        30_100 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x32,  # flags preserved
                        0xEA,
                        0x60,  # max_step = 60_000 preserved
                    )
                ),
            )

        with self.subTest("modern_24bit"):
            replies = [
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(state=0x00, backlash=10, step=100_000, flags=0x40, max_step=500_000),
                _status_report_modern(state=0x01, backlash=10, step=150_000, flags=0x40, max_step=500_000),
                _status_report_modern(state=0x00, backlash=10, step=200_000, flags=0x40, max_step=500_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep"),
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.move(200_000)

            self.assertEqual(settled, 200_000)
            self.assertEqual(
                backend.sent_reports[2],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x01,  # run = 1
                        10,  # backlash preserved
                        (500_000 >> 16) & 0xFF,
                        (200_000 >> 16) & 0xFF,
                        (200_000 >> 8) & 0xFF,
                        200_000 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x40,
                        (500_000 >> 8) & 0xFF,
                        500_000 & 0xFF,
                    )
                ),
            )

    async def test_stop_when_idle_and_while_moving(self) -> None:
        with self.subTest("already_settled"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=2, speed=512, step=30_068, flags=0x32, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.stop()

            self.assertEqual(settled, 30_068)
            mock_sleep.assert_not_awaited()
            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        2,
                        0x02,
                        0x00,
                        (30_068 >> 8) & 0xFF,
                        30_068 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x32,
                        0xEA,
                        0x60,
                    )
                ),
            )

        with self.subTest("moving_polls_until_settled"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=30_050, max_step=60_000),
                _status_report_legacy(state=0x01, step=30_055, max_step=60_000),
                _status_report_legacy(state=0x00, step=30_058, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.stop()

            self.assertEqual(settled, 30_058)
            self.assertEqual(mock_sleep.await_count, 2)

        with self.subTest("hand_controller_moving_masks_flag_and_polls_until_settled"):
            replies = [
                _firmware_report(version=(3, 1, 9)),
                _status_report_legacy(state=0x00, backlash=0, speed=9, step=1_470, flags=0x26, max_step=23_000),
                _status_report_legacy(state=0x00, backlash=0, speed=9, step=1_476, flags=0x22, max_step=23_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.stop()

            self.assertEqual(settled, 1_476)
            self.assertEqual(mock_sleep.await_count, 1)
            # Outgoing tx[13] masks out _FLAG_HAND_CONTROLLER (0x26 & ~0x04 == 0x22)
            self.assertEqual(backend.sent_reports[2][13], 0x22)

    async def test_move_and_stop_validate_arguments_and_reject_busy_move(self) -> None:
        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
            ]
        ):
            async with await Eaf.open(DEV_PATH) as focuser:
                for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                    with self.subTest(move_timeout=bad_timeout), self.assertRaises(ValueError):
                        await focuser.move(10_000, timeout=bad_timeout)
                    with self.subTest(stop_timeout=bad_timeout), self.assertRaises(ValueError):
                        await focuser.stop(timeout=bad_timeout)

                for bad_step in (-1, 40_001):
                    with self.subTest(step=bad_step), self.assertRaises(ValueError):
                        await focuser.move(bad_step)

        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                with self.assertRaises(ZwoError):
                    await focuser.move(15_000)
            self.assertEqual(len(backend.sent_reports), 2)

    async def test_configure_legacy_and_modern_updates_settings_and_preserves_unspecified(self) -> None:
        with self.subTest("legacy_flags_and_backlash_opcode_00"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(
                    state=0x00,
                    backlash=12,
                    speed=512,
                    step=30_068,
                    flags=0x3A,  # max_force=3, bit3=1, reverse=1, beep=0
                    max_step=60_000,
                ),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.configure(backlash=25, reverse=False, beep=True, max_force=2)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        25,  # updated backlash
                        0x02,
                        0x00,  # speed = 512 preserved
                        (30_068 >> 8) & 0xFF,
                        30_068 & 0xFF,  # step preserved
                        0x00,  # opcode = 0x00 (max_step not changed)
                        0x00,
                        0x00,
                        0x29,  # max_force=2, bit3=1 preserved, reverse=0, beep=1
                        0xEA,
                        0x60,  # max_step = 60_000 preserved
                    )
                ),
            )

        with self.subTest("legacy_max_step_opcode_02"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=10, speed=512, step=4_000, flags=0x22, max_step=23_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.configure(max_step=45_000)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,
                        10,
                        0x02,
                        0x00,
                        (4_000 >> 8) & 0xFF,
                        4_000 & 0xFF,
                        0x02,  # opcode = 0x02 (set_max_step)
                        0x00,
                        0x00,
                        0x22,
                        (45_000 >> 8) & 0xFF,
                        45_000 & 0xFF,
                    )
                ),
            )

        with self.subTest("modern_24bit_combined_opcode_02"):
            replies = [
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(state=0x00, backlash=5, step=120_000, flags=0x31, max_step=300_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.configure(max_step=500_000, beep=False, reverse=True, max_force=4)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,
                        5,  # backlash preserved
                        (500_000 >> 16) & 0xFF,
                        (120_000 >> 16) & 0xFF,
                        (120_000 >> 8) & 0xFF,
                        120_000 & 0xFF,
                        0x02,  # opcode = 0x02 (set_max_step)
                        0x00,
                        0x00,
                        0x42,  # max_force=4, reverse=1, beep=0
                        (500_000 >> 8) & 0xFF,
                        500_000 & 0xFF,
                    )
                ),
            )

    async def test_configure_validates_bounds_and_rejects_when_moving(self) -> None:
        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=0, max_step=40_000),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                for bad_backlash in (-1, 256):
                    with self.subTest(backlash=bad_backlash), self.assertRaises(ValueError):
                        await focuser.configure(backlash=bad_backlash)
                for bad_force in (0, 6):
                    with self.subTest(max_force=bad_force), self.assertRaises(ValueError):
                        await focuser.configure(max_force=bad_force)

                # Before any status query is sent for the above invalid backlash/max_force checks:
                self.assertEqual(len(backend.sent_reports), 1)

                # When step=0, max_step=0 is rejected (must be >= 1)
                with self.assertRaises(ValueError):
                    await focuser.configure(max_step=0)
                # When step=10_000, max_step < 10_000 or > 60_000 is rejected
                for bad_max_step in (9_999, 60_001):
                    with self.subTest(max_step=bad_max_step), self.assertRaises(ValueError):
                        await focuser.configure(max_step=bad_max_step)

        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                with self.assertRaises(ZwoError):
                    await focuser.configure(reverse=True)
            self.assertEqual(len(backend.sent_reports), 2)


if __name__ == "__main__":
    unittest.main()
