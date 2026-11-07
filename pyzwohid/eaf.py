# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Automatic Focusers (EAF)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Self

from .hid import ZwoError, ZwoHidDevice, ZwoHidStatus, _validate_timeout

# Coordinate-width split (fw <= 3.3.5 vs fw > 3.3.5)
_LEGACY_FW_LIMIT = (3, 3, 5)
_LEGACY_STEP_RANGE = 60_000
_MODERN_STEP_RANGE = 600_000

# Status query (02 03) and reply decoding
_CMD_GET_STATUS = b"\x02\x03"
_STATE_SETTLED = 0x00
_STATE_MOVING = 0x01
_STATE_ERROR = 0x06
_TEMP_OFFSET = 30_000
_TEMP_SCALE = 100.0
_MIN_VALID_RAW_TEMP = 10_000
_FLAG_BEEP = 0x01
_FLAG_REVERSE = 0x02
_FLAG_HAND_CONTROLLER = 0x04

# Battery telemetry query (02 1e) and reply decoding (EAF Pro)
_CMD_GET_BATTERY_STATUS = b"\x02\x1e"
_BATTERY_REPLY_SELECTOR = 0x00
_BATTERY_TEMP_OFFSET = 100
_MIN_BATTERY_TEMP_C = -40
_MAX_BATTERY_TEMP_C = 87

# Master control frame (03 <run> <backlash> <b6..b9> <opcode> 00 00 <flags> <max_hi..lo>)
_CMD_CONTROL = 0x03
_RUN_STOP = 0x00
_RUN_MOVE = 0x01
_OPCODE_MOVE_OR_STOP = 0x00
_OPCODE_RESET_POSITION = 0x01
_OPCODE_SET_MAX_STEP = 0x02


@dataclass(frozen=True, slots=True)
class EafStatus(ZwoHidStatus):
    """Snapshot of EAF status and telemetry from a single 0x03 status report.

    Attributes:
        moving: True while the focuser is moving (via USB command or hand controller).
        step: Current focuser position in steps (0..max_step).
        max_step: Configured maximum step limit (1..step_range).
        temperature_c: Probe temperature in degrees Celsius, or None if disconnected.
        backlash: Configured backlash compensation in steps (0..255).
        reverse: True if stepper motor direction is reversed.
        beep: True if the motion buzzer is enabled.
        max_force: Configured motor torque / holding force level (0..15).
        hand_controller_active: True while a hand controller button is actively moving the focuser.
    """

    step: int
    max_step: int
    temperature_c: float | None
    backlash: int
    reverse: bool
    beep: bool
    max_force: int
    hand_controller_active: bool


@dataclass(frozen=True, slots=True)
class EafBatteryStatus:
    """Snapshot of EAF Pro battery telemetry from a single 0x1e query (`01 7e 5a 00 ...`).

    Attributes:
        percentage: Remaining state of charge in percent (0..100).
        health: Battery state of health in percent (0..100).
        temperature_c: Battery temperature in degrees Celsius (-40..87).
        voltage_mv: Battery terminal voltage in millivolts.
        charge_current_ma: Charging current in milliamperes.
        discharge_current_ma: Discharging current in milliamperes.
        charge_voltage_mv: Charging input voltage in millivolts.
    """

    percentage: int
    health: int
    temperature_c: int
    voltage_mv: int
    charge_current_ma: int
    discharge_current_ma: int
    charge_voltage_mv: int


class Eaf(ZwoHidDevice[EafStatus]):
    """Asynchronous ZWO EAF focuser controller."""

    PIDS = (0x1F10,)

    def __init__(
        self,
        path: str,
        device: Any,
        firmware_version: tuple[int, int, int],
    ) -> None:
        super().__init__(path, device)
        self.firmware_version = firmware_version
        self.step_range = _MODERN_STEP_RANGE if firmware_version > _LEGACY_FW_LIMIT else _LEGACY_STEP_RANGE

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify and open the EAF at `path`, reading its firmware version."""
        transport = ZwoHidDevice[EafStatus](path, await cls._open_handle(path))
        try:
            firmware_version = await transport.get_firmware_version()
        except BaseException:
            await transport.close()
            raise
        return cls(path, transport._device, firmware_version)

    async def _read_status_report(self) -> tuple[EafStatus, bytes]:
        reply = await self._query(_CMD_GET_STATUS)
        state, backlash = reply[4:6]
        if state == _STATE_ERROR:
            raise ZwoError(f"EAF hardware error: {reply.hex(' ')}")
        if state not in (_STATE_SETTLED, _STATE_MOVING):
            raise ZwoError(f"Unexpected EAF state=0x{state:02x}: {reply.hex(' ')}")

        if self.firmware_version > _LEGACY_FW_LIMIT:
            step = int.from_bytes(reply[7:10], "big")
            max_step = (reply[6] << 16) | int.from_bytes(reply[14:16], "big")
        else:
            step = int.from_bytes(reply[8:10], "big")
            max_step = int.from_bytes(reply[14:16], "big")

        if not (1 <= max_step <= self.step_range):
            raise ZwoError(f"Invalid EAF max_step={max_step} (step_range={self.step_range}): {reply.hex(' ')}")
        if not (0 <= step <= max_step):
            raise ZwoError(f"Invalid EAF step={step} (max_step={max_step}): {reply.hex(' ')}")

        raw_temp = int.from_bytes(reply[11:13], "big")
        temperature_c = (raw_temp - _TEMP_OFFSET) / _TEMP_SCALE if _MIN_VALID_RAW_TEMP < raw_temp < 0xFFFF else None
        flags = reply[13]
        hand_controller_active = bool(flags & _FLAG_HAND_CONTROLLER)
        status = EafStatus(
            moving=state == _STATE_MOVING or hand_controller_active,
            step=step,
            max_step=max_step,
            temperature_c=temperature_c,
            backlash=backlash,
            reverse=bool(flags & _FLAG_REVERSE),
            beep=bool(flags & _FLAG_BEEP),
            max_force=flags >> 4,
            hand_controller_active=hand_controller_active,
        )
        return status, reply

    async def get_status(self) -> EafStatus:
        """Return EafStatus from a 0x03 status report."""
        status, _ = await self._read_status_report()
        return status

    async def get_battery_status(self) -> EafBatteryStatus:
        """Query (`02 1e`) and return `EafBatteryStatus` on battery-equipped EAF Pro models."""
        reply = await self._query(_CMD_GET_BATTERY_STATUS, override_expected_selector=_BATTERY_REPLY_SELECTOR)
        (
            raw_temp,
            voltage_mv,
            charge_current_ma,
            percentage,
            discharge_current_ma,
            health,
            charge_voltage_mv,
        ) = struct.unpack(">BHHBHBH", reply[5:16])
        temperature_c = raw_temp - _BATTERY_TEMP_OFFSET
        if not (_MIN_BATTERY_TEMP_C <= temperature_c <= _MAX_BATTERY_TEMP_C) or percentage > 100 or health > 100:
            raise ZwoError(f"Invalid EAF battery status: {reply.hex(' ')}")
        return EafBatteryStatus(
            percentage=percentage,
            health=health,
            temperature_c=temperature_c,
            voltage_mv=voltage_mv,
            charge_current_ma=charge_current_ma,
            discharge_current_ma=discharge_current_ma,
            charge_voltage_mv=charge_voltage_mv,
        )

    def _build_control_payload(
        self,
        status_reply: bytes,
        *,
        run: int,
        step: int,
        opcode: int,
        backlash: int | None = None,
        flags: int | None = None,
        max_step: int | None = None,
    ) -> bytes:
        actual_backlash = status_reply[5] if backlash is None else backlash
        actual_flags = (status_reply[13] if flags is None else flags) & ~_FLAG_HAND_CONTROLLER
        max_step_lo = status_reply[14:16] if max_step is None else (max_step & 0xFFFF).to_bytes(2, "big")
        if self.firmware_version > _LEGACY_FW_LIMIT:
            max_step_hi = status_reply[6] if max_step is None else (max_step >> 16) & 0xFF
            coord_bytes = bytes((max_step_hi,)) + step.to_bytes(3, "big")
        else:
            coord_bytes = status_reply[6:8] + step.to_bytes(2, "big")
        return (
            bytes((_CMD_CONTROL, run, actual_backlash))
            + coord_bytes
            + bytes((opcode, 0x00, 0x00, actual_flags))
            + max_step_lo
        )

    async def move(self, step: int, *, timeout: float = 60.0) -> int:
        """Move to step (0..max_step) and poll until settled. Returns settled step."""
        _validate_timeout(timeout)

        status, reply = await self._read_status_report()
        if status.moving:
            raise ZwoError("EAF is already moving")
        if not (0 <= step <= status.max_step):
            raise ValueError(f"step={step} outside allowed range [0, {status.max_step}]")

        await self._send(self._build_control_payload(reply, run=_RUN_MOVE, step=step, opcode=_OPCODE_MOVE_OR_STOP))
        return (await self._wait_until_settled(timeout, f"move({step})")).step

    async def stop(self, *, timeout: float = 5.0) -> int:
        """Send a stop command (`03 00 ...`) and poll until settled. Returns settled step."""
        _validate_timeout(timeout)

        status, reply = await self._read_status_report()
        await self._send(
            self._build_control_payload(reply, run=_RUN_STOP, step=status.step, opcode=_OPCODE_MOVE_OR_STOP)
        )
        if not status.moving:
            return status.step
        return (await self._wait_until_settled(timeout, "stop()")).step

    async def reset_position(self, step: int) -> None:
        """Overwrite the current focuser step coordinate (0..max_step) in the MCU without moving."""
        status, reply = await self._read_status_report()
        if status.moving:
            raise ZwoError("EAF is already moving")
        if not (0 <= step <= status.max_step):
            raise ValueError(f"step={step} outside allowed range [0, {status.max_step}]")

        await self._send(self._build_control_payload(reply, run=_RUN_STOP, step=step, opcode=_OPCODE_RESET_POSITION))

    async def configure(
        self,
        *,
        max_step: int | None = None,
        backlash: int | None = None,
        reverse: bool | None = None,
        beep: bool | None = None,
        max_force: int | None = None,
    ) -> None:
        """Update one or more EAF configuration settings while preserving unspecified fields."""
        if backlash is not None and not (0 <= backlash <= 255):
            raise ValueError(f"backlash={backlash} outside allowed range [0, 255]")
        if max_force is not None and not (1 <= max_force <= 5):
            raise ValueError(f"max_force={max_force} outside allowed range [1, 5]")

        status, reply = await self._read_status_report()
        if status.moving:
            raise ZwoError("EAF is already moving")
        if max_step is not None:
            min_max_step = max(1, status.step)
            if not (min_max_step <= max_step <= self.step_range):
                raise ValueError(f"max_step={max_step} outside allowed range [{min_max_step}, {self.step_range}]")

        flags = reply[13]
        if beep is not None:
            flags = (flags | _FLAG_BEEP) if beep else (flags & ~_FLAG_BEEP)
        if reverse is not None:
            flags = (flags | _FLAG_REVERSE) if reverse else (flags & ~_FLAG_REVERSE)
        if max_force is not None:
            flags = (flags & 0x0F) | (max_force << 4)

        opcode = _OPCODE_SET_MAX_STEP if max_step is not None else _OPCODE_MOVE_OR_STOP
        await self._send(
            self._build_control_payload(
                reply,
                run=_RUN_STOP,
                step=status.step,
                opcode=opcode,
                backlash=backlash,
                flags=flags,
                max_step=max_step,
            )
        )
