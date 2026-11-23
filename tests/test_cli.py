# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Offline CLI tests for `pyzwohid.cli`."""

from __future__ import annotations

import contextlib
import io
import os
import struct
import tempfile
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

from pyzwohid import Eaf, Efw
from pyzwohid.cli import run_cli
from tests.test_hid import FakeHidBackend, _firmware_report


@dataclass
class _SimEfw:
    path: bytes
    firmware: tuple[int, int, int] = (3, 0, 6)
    serial: bytes | None = bytes.fromhex("0123456789abcdef")
    alias: str | None = "MAIN-EFW"
    slot_count: int = 7
    slot: int = 2
    target_slot: int = 2
    moving_polls: int = 0
    calibrating: bool = False
    sent_moves: list[tuple[int, int]] = field(default_factory=list)
    calibrate_count: int = 0

    def handle_send(self, payload: bytes) -> bytes | None:
        cat, sub = payload[3], payload[4]
        if (cat, sub) == (0x02, 0x04):
            return _firmware_report(version=self.firmware)
        if (cat, sub) == (0x02, 0x0C):
            if self.serial is None:
                return b"\x01"
            return b"\x01\x7e\x5a\x0c" + self.serial
        if (cat, sub) == (0x02, 0x0D):
            if self.alias is None:
                return b"\x01"
            return b"\x01\x7e\x5a\x0d" + self.alias.encode("ascii").ljust(8, b"\x00")
        if (cat, sub) == (0x02, 0x01):
            if self.moving_polls > 0:
                self.moving_polls -= 1
                if self.moving_polls > 0:
                    if self.calibrating:
                        return bytes((0x01, 0x7E, 0x5A, 0x01, 0x00, 0x00, 0, 0, 0, 2)).ljust(16, b"\x00")
                    return bytes(
                        (
                            0x01,
                            0x7E,
                            0x5A,
                            0x01,
                            0x04,
                            0x00,
                            self.target_slot,
                            self.slot,
                            self.target_slot,
                            self.slot_count,
                        )
                    ).ljust(16, b"\x00")
                self.slot = self.target_slot
                self.calibrating = False
            return bytes((0x01, 0x7E, 0x5A, 0x01, 0x01, 0x00, self.slot, self.slot, self.slot, self.slot_count)).ljust(
                16, b"\x00"
            )
        if (cat, sub) == (0x01, 0x01):
            if payload[5] == 0x00:
                self.calibrate_count += 1
                self.calibrating = True
                self.target_slot = 1
                self.moving_polls = 2
            else:
                direction = payload[4]
                slot = payload[5]
                self.sent_moves.append((direction, slot))
                self.calibrating = False
                self.target_slot = slot
                self.moving_polls = 2 if slot != self.slot else 0
            return None
        if (cat, sub) in ((0x01, 0x02), (0x01, 0x03)):
            self.sent_moves.append((sub, payload[5]))
            self.calibrating = False
            self.target_slot = payload[5]
            self.moving_polls = 2 if payload[5] != self.slot else 0
            return None
        raise AssertionError(f"Unexpected EFW report: {payload.hex(' ')}")


@dataclass
class _SimEaf:
    path: bytes
    firmware: tuple[int, int, int] = (3, 2, 2)
    serial: bytes | None = bytes.fromhex("fedcba9876543210")
    alias: str | None = ""
    step: int = 12_000
    target_step: int = 12_000
    max_step: int = 60_000
    raw_temp: int = 32_345  # 23.45 C
    backlash: int = 15
    flags: int = 0x31  # beep=True, reverse=False, max_force=3
    moving_polls: int = 0
    hand_controller: bool = False
    stall_at: int | None = None
    pro: bool = False
    led_on: bool = True
    control_ops: list[tuple[int, int, int, int]] = field(default_factory=list)

    def _status_reply(self) -> bytes:
        state = 0x01 if self.moving_polls > 0 else 0x00
        flags = self.flags | (0x04 if self.hand_controller else 0x00)
        if self.firmware > (3, 3, 5):
            return bytes(
                (
                    0x01,
                    0x7E,
                    0x5A,
                    0x03,
                    state,
                    self.backlash,
                    (self.max_step >> 16) & 0xFF,
                    (self.step >> 16) & 0xFF,
                    (self.step >> 8) & 0xFF,
                    self.step & 0xFF,
                    0x00,
                    (self.raw_temp >> 8) & 0xFF,
                    self.raw_temp & 0xFF,
                    flags,
                    (self.max_step >> 8) & 0xFF,
                    self.max_step & 0xFF,
                )
            )
        return bytes(
            (
                0x01,
                0x7E,
                0x5A,
                0x03,
                state,
                self.backlash,
                0x02,
                0x00,
                (self.step >> 8) & 0xFF,
                self.step & 0xFF,
                0x00,
                (self.raw_temp >> 8) & 0xFF,
                self.raw_temp & 0xFF,
                flags,
                (self.max_step >> 8) & 0xFF,
                self.max_step & 0xFF,
            )
        )

    def handle_send(self, payload: bytes) -> bytes | None:
        cat, sub = payload[3], payload[4]
        if (cat, sub) == (0x02, 0x04):
            return _firmware_report(version=self.firmware)
        if (cat, sub) == (0x02, 0x0C):
            if self.serial is None:
                return b"\x01"
            return b"\x01\x7e\x5a\x0c" + self.serial
        if (cat, sub) == (0x02, 0x0D):
            if self.alias is None:
                return b"\x01"
            return b"\x01\x7e\x5a\x0d" + self.alias.encode("ascii").ljust(8, b"\x00")
        if (cat, sub) == (0x02, 0x03):
            if self.moving_polls > 0:
                self.moving_polls -= 1
                if self.moving_polls == 0:
                    self.step = self.target_step
            return self._status_reply()
        if (cat, sub) == (0x02, 0x1E):
            if not self.pro:
                return b"\x01"
            return b"\x01\x7e\x5a\x00\xb6" + struct.pack(">BHHBHBH", 125, 3850, 200, 82, 0, 99, 5000)
        if (cat, sub) == (0x02, 0x05):
            if not self.pro:
                return b"\x01"
            return (b"\x01\x7e\x5a\x05E00\x00").ljust(16, b"\x00")
        if (cat, sub) == (0x02, 0x1F):
            if not self.pro:
                return b"\x01"
            return bytes((0x01, 0x7E, 0x5A, 0x1F, int(self.led_on))).ljust(16, b"\x00")
        if cat == 0x03:
            run = payload[4]
            backlash = payload[5]
            if self.firmware > (3, 3, 5):
                coord = int.from_bytes(payload[7:10], "big")
                max_step = (payload[6] << 16) | int.from_bytes(payload[14:16], "big")
            else:
                coord = int.from_bytes(payload[8:10], "big")
                max_step = int.from_bytes(payload[14:16], "big")
            opcode = payload[10]
            flags = payload[13]
            self.control_ops.append((run, opcode, coord, max_step))
            if opcode == 0x00 and run == 0x01:
                self.target_step = self.stall_at if self.stall_at is not None else coord
                self.moving_polls = 2
            elif opcode == 0x01:
                self.step = coord
            elif opcode == 0x02:
                self.backlash = backlash
                self.flags = flags
                self.max_step = max_step
            return None
        raise AssertionError(f"Unexpected EAF report: {payload.hex(' ')}")


def _make_backend(efws: list[_SimEfw], eafs: list[_SimEaf]) -> FakeHidBackend:
    devices = tuple(
        [(Efw.VID, 0x1F01, efw.path) for efw in efws]
        + [(Eaf.VID, 0x1F10, eaf.path) for eaf in eafs]
        + [(0x1234, 0x1F01, b"/dev/hidraw99")]
    )
    devs: list[_SimEfw | _SimEaf] = [*efws, *eafs]
    by_path: dict[bytes, _SimEfw | _SimEaf] = {dev.path: dev for dev in devs}
    replies_by_path: dict[bytes, list[bytes]] = {path: [b"\x01"] for path in by_path}

    class _DynamicBackend(FakeHidBackend):
        DEVICES = devices

    backend = _DynamicBackend(replies_by_path=replies_by_path)

    def on_send(path: bytes, report: bytes) -> None:
        reply = by_path[path].handle_send(report)
        if reply is not None:
            backend.replies_by_path[path] = [reply]

    backend.on_send = on_send
    return backend


def _run_captured(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_cli(argv)
    return code, out.getvalue(), err.getvalue()


async def _no_sleep(_seconds: float) -> None:
    return None


class TestCli(unittest.TestCase):
    def test_list_devices_scopes_and_empty(self) -> None:
        efw = _SimEfw(b"/dev/hidraw10")
        eaf = _SimEaf(b"/dev/hidraw20")
        with _make_backend([efw], [eaf]):
            code, out, err = _run_captured(["list"])
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.splitlines(), ["efw  /dev/hidraw10", "eaf  /dev/hidraw20"])

            code, out, _ = _run_captured(["list", "efw"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "efw  /dev/hidraw10")

            code, out, _ = _run_captured(["list", "eaf"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "eaf  /dev/hidraw20")

        with _make_backend([], []):
            code, out, _ = _run_captured(["list"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "No ZWO HID devices found.")

            code, out, _ = _run_captured(["list", "efw"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "No EFW devices found.")

    def test_status_consolidated_legacy_pro_and_symlink(self) -> None:
        efw = _SimEfw(b"/dev/hidraw10", serial=None, alias=None)
        eaf_pro = _SimEaf(
            b"/dev/hidraw20",
            firmware=(3, 3, 7),
            step=120_000,
            max_step=300_000,
            pro=True,
            led_on=False,
        )
        with _make_backend([efw], [eaf_pro]):
            code, out, err = _run_captured(["status"])
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("EFW /dev/hidraw10", out)
            self.assertIn("firmware : 3.0.6", out)
            self.assertIn("serial   : n/a", out)
            self.assertIn("alias    : n/a", out)
            self.assertIn("slot     : 2 / 7", out)
            self.assertIn("EAF /dev/hidraw20", out)
            self.assertIn("firmware   : 3.3.7 (step range: 600000)", out)
            self.assertIn("alias      : (none)", out)
            self.assertIn("position   : 120000 / 300000", out)
            self.assertIn("temperature: 23.45 C", out)
            self.assertIn("error_code : motor=E0 battery=E0", out)
            self.assertIn("led        : off", out)
            self.assertIn("battery    : 82% (health 99%, 3850 mV, 25 C)", out)

            with tempfile.TemporaryDirectory() as tmp_dir:
                link_path = os.path.join(tmp_dir, "focuser-link")
                os.symlink("/dev/hidraw20", link_path)
                code, link_out, _ = _run_captured(["status", "-d", link_path])
                self.assertEqual(code, 0)
                self.assertIn(f"EAF {link_path}", link_out)

            code, _, err = _run_captured(["status", "-d", "/dev/hidraw99"])
            self.assertEqual(code, 1)
            self.assertIn("does not match any connected EFW or EAF", err)

        with _make_backend([], []):
            code, _, err = _run_captured(["status"])
            self.assertEqual(code, 1)
            self.assertIn("No ZWO HID devices found", err)

    def test_auto_select_rejects_multiple_and_accepts_explicit_device(self) -> None:
        efw1 = _SimEfw(b"/dev/hidraw10", slot=1)
        efw2 = _SimEfw(b"/dev/hidraw11", slot=4)
        with _make_backend([efw1, efw2], []), patch("pyzwohid.hid.asyncio.sleep", side_effect=_no_sleep):
            code, _, err = _run_captured(["filter", "3"])
            self.assertEqual(code, 1)
            self.assertIn("Multiple EFW devices found", err)
            self.assertIn("/dev/hidraw10", err)
            self.assertIn("/dev/hidraw11", err)

            code, out, _ = _run_captured(["filter", "-d", "/dev/hidraw11", "3"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EFW /dev/hidraw11: settled at slot 3")
            self.assertEqual(efw1.slot, 1)
            self.assertEqual(efw2.slot, 3)

    def test_filter_and_calibrate_single_efw(self) -> None:
        efw = _SimEfw(b"/dev/hidraw10", slot=1)
        with _make_backend([efw], []), patch("pyzwohid.hid.asyncio.sleep", side_effect=_no_sleep):
            code, out, _ = _run_captured(["filter", "5", "--unidirectional"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EFW /dev/hidraw10: settled at slot 5")
            self.assertEqual(efw.sent_moves, [(0x03, 5)])

            code, out, _ = _run_captured(["calibrate"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EFW /dev/hidraw10: calibrated, settled at slot 1")
            self.assertEqual(efw.calibrate_count, 1)
            self.assertEqual(efw.slot, 1)

            code, _, err = _run_captured(["filter", "9"])
            self.assertEqual(code, 1)
            self.assertIn("outside allowed range", err)

    def test_focus_absolute_relative_negative_and_early_stall(self) -> None:
        eaf = _SimEaf(b"/dev/hidraw20", step=10_000, max_step=60_000)
        with _make_backend([], [eaf]), patch("pyzwohid.hid.asyncio.sleep", side_effect=_no_sleep):
            code, out, _ = _run_captured(["focus", "25000"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw20: settled at step 25000")
            self.assertEqual(eaf.step, 25_000)

            code, out, _ = _run_captured(["focus", "-r", "-500"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw20: settled at step 24500")
            self.assertEqual(eaf.step, 24_500)

            code, out, _ = _run_captured(["focus", "-r", "100"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw20: settled at step 24600")
            self.assertEqual(eaf.step, 24_600)

            eaf.stall_at = 24_800
            code, out, err = _run_captured(["focus", "26000"])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("stopped early at step 24800 (target 26000)", err)

    def test_limits_validation_and_safe_ordering(self) -> None:
        eaf_shrink = _SimEaf(b"/dev/hidraw20", step=50_000, max_step=60_000)
        with _make_backend([], [eaf_shrink]):
            code, out, _ = _run_captured(["limits", "--max-step", "40000", "--position", "1000"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw20: position 1000 / 40000 (step range: 60000)")
            self.assertEqual([op[1] for op in eaf_shrink.control_ops], [0x01, 0x02])

        eaf_expand = _SimEaf(b"/dev/hidraw21", firmware=(3, 3, 7), step=50_000, max_step=60_000)
        with _make_backend([], [eaf_expand]):
            code, out, _ = _run_captured(["limits", "--max-step", "120000", "--position", "90000"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw21: position 90000 / 120000 (step range: 600000)")
            self.assertEqual([op[1] for op in eaf_expand.control_ops], [0x02, 0x01])

            code, out, _ = _run_captured(["limits", "--position", "0"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw21: position 0 / 120000 (step range: 600000)")

            code, out, _ = _run_captured(["limits", "--max-step", "80000"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "EAF /dev/hidraw21: position 0 / 80000 (step range: 600000)")

            eaf_expand.control_ops.clear()
            code, _, err = _run_captured(["limits", "--max-step", "5000", "--position", "6000"])
            self.assertEqual(code, 1)
            self.assertIn("Invalid limits", err)
            self.assertEqual(eaf_expand.control_ops, [])

            code, _, err = _run_captured(["limits", "--max-step", "0", "--position", "0"])
            self.assertEqual(code, 1)
            self.assertIn("Invalid limits", err)
            self.assertEqual(eaf_expand.control_ops, [])

            eaf_expand.moving_polls = 5
            code, _, err = _run_captured(["limits", "--max-step", "10000", "--position", "0"])
            self.assertEqual(code, 1)
            self.assertIn("EAF is already moving", err)
            self.assertEqual(eaf_expand.control_ops, [])

            with self.assertRaises(SystemExit) as exc_info:
                _run_captured(["limits"])
            self.assertEqual(exc_info.exception.code, 2)

    def test_help_and_empty_invocation(self) -> None:
        for argv, expected in (
            ([], "Device selection:"),
            (["help"], "Device selection:"),
            (["help", "limits"], "--max-step"),
        ):
            code, out, err = _run_captured(argv)
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn(expected, out)

        for argv, expected in (
            (["--help"], "Device selection:"),
            (["-h", "focus"], "--relative"),
            (["--help", "limits"], "--max-step"),
        ):
            help_out = io.StringIO()
            with contextlib.redirect_stdout(help_out), self.assertRaises(SystemExit) as exc_info:
                run_cli(argv)
            self.assertEqual(exc_info.exception.code, 0)
            rendered = help_out.getvalue()
            self.assertIn(expected, rendered)
            if len(argv) == 2:
                self.assertNotIn("Device selection:", rendered)
