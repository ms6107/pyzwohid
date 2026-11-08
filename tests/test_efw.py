# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwohid.efw."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from pyzwohid import Efw, EfwStatus, ZwoBusyError, ZwoDevicePathError, ZwoError
from tests.test_hid import FakeHidBackend

DEV_PATH = "/dev/hidraw10"
SLOT_COUNT = 7


def _status_report(
    *,
    state: int = 0x01,
    error_code: int = 0x00,
    slots: tuple[int, int, int] = (1, 1, 1),
    slot_count: int = SLOT_COUNT,
    dual_disc: int = 0x00,
) -> bytes:
    target_slot, prev_slot, next_slot = slots
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x01,
            state,
            error_code,
            target_slot,
            prev_slot,
            next_slot,
            slot_count,
            0,
            0,
            dual_disc,
            0,
            0x30,
            0,
        )
    )


class FakeEfwHidBackend(FakeHidBackend):
    """In-memory fake HID backend configured for ZWO EFW devices."""

    DEVICES = (
        (Efw.VID, 0x1F01, b"/dev/hidraw11"),
        (Efw.VID, 0x1F01, b"/dev/hidraw10"),
        (Efw.VID, 0x1F02, b"/dev/hidraw10"),
        (Efw.VID, 0x1F10, b"/dev/hidraw12"),  # ZWO EAF focuser (same VID, different PID)
        (0x1234, 0x1F01, b"/dev/hidraw13"),  # Non-ZWO HID device (different VID, same PID)
    )
    DEFAULT_REPLY = _status_report(slots=(3, 3, 3))


class TestEfw(unittest.IsolatedAsyncioTestCase):
    async def test_discover_and_open_efw_pids(self) -> None:
        with FakeEfwHidBackend() as backend:
            self.assertEqual(await Efw.discover(), ["/dev/hidraw10", "/dev/hidraw11"])
            with self.assertRaises(ZwoDevicePathError):
                await Efw.open("/dev/hidraw12")
            self.assertEqual(backend.open_count, 0)

    async def test_get_status_settled_calibrating_and_moving(self) -> None:
        with FakeEfwHidBackend(replies=[_status_report(slots=(5, 5, 5))]) as backend:
            async with await Efw.open(DEV_PATH) as wheel:
                status = await wheel.get_status()

        self.assertEqual(status, EfwStatus(moving=False, slot_count=SLOT_COUNT, slot=5))
        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x01".ljust(16, b"\x00")])

        calibrating_cases = [
            ("calibrating_power_on", _status_report(state=0x00, slots=(0, 0, 0), slot_count=0)),
            ("calibrating_magnet_counter", _status_report(state=0x00, slots=(3, 3, 3), slot_count=2)),
        ]
        for label, report in calibrating_cases:
            with self.subTest(case=label), FakeEfwHidBackend(replies=[report]):
                async with await Efw.open(DEV_PATH) as wheel:
                    self.assertEqual(await wheel.get_status(), EfwStatus(moving=True, slot_count=-1, slot=-1))

        moving_cases = [
            ("rotating_between_slots", _status_report(state=0x04, slots=(6, 2, 3))),
            ("fine_damping_at_target", _status_report(state=0x04, slots=(6, 6, 6))),
            ("inter_command_handoff", _status_report(state=0x01, slots=(2, 3, 3))),
        ]
        for label, report in moving_cases:
            with self.subTest(case=label), FakeEfwHidBackend(replies=[report]):
                async with await Efw.open(DEV_PATH) as wheel:
                    self.assertEqual(await wheel.get_status(), EfwStatus(moving=True, slot_count=SLOT_COUNT, slot=-1))

    async def test_get_status_efw_errors_raise_zwo_error(self) -> None:
        cases = [
            ("dual_disc", _status_report(dual_disc=1)),
            ("hw_error", _status_report(state=0x06, error_code=2)),
            ("zero_slots", _status_report(slot_count=0)),
            ("too_many_slots", _status_report(slot_count=17)),
        ]
        for label, report in cases:
            with self.subTest(case=label), FakeEfwHidBackend(replies=[report]) as backend:
                with self.assertRaises(ZwoError):
                    async with await Efw.open(DEV_PATH) as wheel:
                        await wheel.get_status()
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    async def test_move_to_slot_and_calibrate_validate_arguments_and_reject_when_moving(self) -> None:
        with FakeEfwHidBackend(replies=[_status_report(slots=(1, 1, 1))]):
            async with await Efw.open(DEV_PATH) as wheel:
                for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                    with self.subTest(timeout=bad_timeout), self.assertRaises(ValueError):
                        await wheel.move_to_slot(1, timeout=bad_timeout)
                    with self.subTest(calibrate_timeout=bad_timeout), self.assertRaises(ValueError):
                        await wheel.calibrate(timeout=bad_timeout)

                for bad_slot in (0, -1, SLOT_COUNT + 1):
                    with self.subTest(slot=bad_slot), self.assertRaises(ValueError):
                        await wheel.move_to_slot(bad_slot)

        busy_reports = [
            ("calibrating", _status_report(state=0x00, slots=(3, 3, 3), slot_count=1)),
            ("rotating", _status_report(state=0x04, slots=(6, 2, 3))),
        ]
        for label, report in busy_reports:
            with self.subTest(case=label), FakeEfwHidBackend(replies=[report]) as backend:
                async with await Efw.open(DEV_PATH) as wheel:
                    with self.assertRaises(ZwoBusyError):
                        await wheel.move_to_slot(5)
                    with self.assertRaises(ZwoBusyError):
                        await wheel.calibrate()
                self.assertEqual(len(backend.sent_reports), 2)

    async def test_move_to_slot_bidirectional_and_unidirectional(self) -> None:
        for unidirectional, expected_dir in ((False, 0x02), (True, 0x03)):
            with self.subTest(unidirectional=unidirectional):
                replies = [
                    _status_report(slots=(1, 1, 1)),
                    _status_report(state=0x04, slots=(6, 2, 3)),
                    _status_report(slots=(6, 6, 6)),
                ]
                with (
                    FakeEfwHidBackend(replies=replies) as backend,
                    patch("pyzwohid.hid.asyncio.sleep"),
                ):
                    async with await Efw.open(DEV_PATH) as wheel:
                        settled = await wheel.move_to_slot(6, unidirectional=unidirectional)

                self.assertEqual(settled, 6)
                self.assertEqual(
                    backend.sent_reports[1],
                    bytes((0x03, 0x7E, 0x5A, 0x01, expected_dir, 6)).ljust(16, b"\x00"),
                )

    async def test_calibrate_sends_command_and_polls_until_settled(self) -> None:
        replies = [
            _status_report(slots=(3, 3, 3)),
            _status_report(state=0x00, slots=(3, 3, 3), slot_count=2),
            _status_report(state=0x00, slots=(1, 3, 1), slot_count=SLOT_COUNT),
            _status_report(slots=(1, 1, 1)),
        ]
        with (
            FakeEfwHidBackend(replies=replies) as backend,
            patch("pyzwohid.hid.asyncio.sleep"),
        ):
            async with await Efw.open(DEV_PATH) as wheel:
                settled = await wheel.calibrate()

        self.assertEqual(settled, 1)
        self.assertEqual(
            backend.sent_reports[1],
            b"\x03\x7e\x5a\x01\x01".ljust(16, b"\x00"),
        )


if __name__ == "__main__":
    unittest.main()
