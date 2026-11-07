# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Automatic Focusers (EAF).

Supports ZWO USB HID focusers matching `(VID=0x03c3, PID=0x1f10)` across both
legacy 16-bit firmware (`fw <= 3.3.5`, `step_range = 60_000`) and modern 24-bit
firmware (`fw > 3.3.5`, `step_range = 600_000`), including EAF Pro battery,
error-code, and LED extensions.
"""

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

# EAF Pro telemetry and LED control (02 1e, 02 05, 02 1f, 20 <00|01>)
_CMD_GET_BATTERY_STATUS = b"\x02\x1e"
_BATTERY_REPLY_SELECTOR = 0x00
_BATTERY_TEMP_OFFSET = 100
_MIN_BATTERY_TEMP_C = -40
_MAX_BATTERY_TEMP_C = 87
_CMD_GET_ERROR_CODE = b"\x02\x05"
_CMD_GET_LED_STATE = b"\x02\x1f"
_CMD_SET_LED_STATE = 0x20

# Master control frame (03 <run> <backlash> <b6..b9> <opcode> 00 00 <flags> <max_hi..lo>)
_CMD_CONTROL = 0x03
_RUN_STOP = 0x00
_RUN_MOVE = 0x01
_OPCODE_MOVE_OR_STOP = 0x00
_OPCODE_RESET_POSITION = 0x01
_OPCODE_SET_MAX_STEP = 0x02


@dataclass(frozen=True, slots=True)
class EafStatus(ZwoHidStatus):
    """Snapshot of EAF status and telemetry from a single `02 03` (`0x03`) status report.

    Attributes:
        moving: True while the focuser is moving via a USB move command
            (`reply[4] == 0x01`) or a physical hand-controller button press
            (`hand_controller_active`).
        step: Current focuser position in steps (`0..max_step`).
        max_step: Configured maximum step limit (`1..step_range`).
        temperature_c: Temperature in degrees Celsius (`(raw - 30_000) / 100.0`), or
            `None` if the raw word is out of valid bounds (`<= 10_000` or `0xFFFF`).
            On tested EAF hardware (`fw 3.1.9` / `3.2.2`), reports the external
            3.5 mm probe temperature when plugged in and seamlessly falls back to an
            internal MCU/PCB temperature sensor when unplugged without changing any
            flag byte.
        backlash: Configured backlash compensation in steps (`0..255`).
        reverse: True if stepper motor direction is reversed (`reply[13] & 0x02`).
        beep: True if the motion buzzer is enabled (`reply[13] & 0x01`).
        max_force: Raw 4-bit motor torque / holding force nibble (`reply[13] >> 4`,
            `0..15`; configurable range via `configure()` is `1..5`).
        hand_controller_active: True while a physical IN/OUT button on the 3.5 mm
            hand controller is actively moving the focuser (`reply[13] & 0x04`),
            during which `reply[4]` remains `0x00` (observed on `fw 3.1.9`).
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
    """Snapshot of EAF Pro battery telemetry from a single `02 1e` query (`01 7e 5a 00 ...`).

    Attributes:
        percentage: Remaining state of charge in percent (`0..100`).
        health: Battery state of health in percent (`0..100`).
        temperature_c: Battery temperature in degrees Celsius (`reply[5] - 100`,
            validated within `-40..87`).
        voltage_mv: Battery terminal voltage in millivolts (`reply[6:8]`).
        charge_current_ma: Charging current in milliamperes (`reply[8:10]`).
        discharge_current_ma: Discharging current in milliamperes (`reply[11:13]`).
        charge_voltage_mv: Charging input voltage in millivolts (`reply[14:16]`).
    """

    percentage: int
    health: int
    temperature_c: int
    voltage_mv: int
    charge_current_ma: int
    discharge_current_ma: int
    charge_voltage_mv: int


class Eaf(ZwoHidDevice[EafStatus]):
    """Asynchronous ZWO EAF focuser controller (`03c3:1f10`).

    On tested standard EAF hardware (`fw 3.1.9` / `3.2.2`), mechanical gearing is
    5,760 steps per full 360° output shaft revolution (16 steps/°; ~341.7 steps/s
    USB slew rate observed on `fw 3.1.9`).

    `await Eaf.open(path)` queries `get_firmware_version()` (`02 04`) once to set
    `firmware_version` and `step_range` (`60_000` with 16-bit coordinates on
    `fw <= 3.3.5`, `600_000` with 24-bit coordinates on `fw > 3.3.5`). All category-`0x03`
    control commands (`move`, `stop`, `reset_position`, `configure`) read a fresh
    `02 03` status report first and echo the focuser's live `backlash`, `speed`/`max_step`,
    and `flags` bytes (while masking out read-only bit `0x04`) so unspecified NVRAM
    settings are never overwritten.

    Methods assume one caller per physical device at a time. Concurrent operations
    across different devices are supported. Executor serialization protects
    individual HID transactions; it does not make whole controller operations atomic.
    """

    PIDS = (0x1F10,)

    def __init__(
        self,
        path: str,
        device: Any,
        firmware_version: tuple[int, int, int],
    ) -> None:
        """Initialize with `path`, an open `hid.Device`, and `firmware_version` (prefer `await Eaf.open(path)`)."""
        super().__init__(path, device)
        self.firmware_version = firmware_version
        self.step_range = _MODERN_STEP_RANGE if firmware_version > _LEGACY_FW_LIMIT else _LEGACY_STEP_RANGE

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify and open the EAF at `path`, reading its firmware version (`02 04`) once.

        Determines `self.firmware_version` and `self.step_range` (`60_000` for
        `fw <= 3.3.5`, `600_000` for `fw > 3.3.5`) used to pack and unpack 16-bit vs.
        24-bit coordinates. If `get_firmware_version()` fails or is cancelled, the
        underlying HID handle is closed before re-raising. This operation is strictly
        read-only and never modifies device coordinates or NVRAM settings.
        """
        transport = ZwoHidDevice[EafStatus](path, await cls._open_handle(path))
        try:
            firmware_version = await transport.get_firmware_version()
        except BaseException:
            await transport.close()
            raise
        return cls(path, transport._device, firmware_version)

    async def _read_status_report(self) -> tuple[EafStatus, bytes]:
        """Query `02 03` and return both the decoded `EafStatus` and raw 16-byte `reply`.

        Returning the raw `reply` alongside `EafStatus` allows `_build_control_payload()`
        to preserve unmodified wire fields (such as 16-bit motor `speed` in `reply[6:8]`
        on `fw <= 3.3.5` and reserved flag bits in `reply[13]`) when constructing a
        subsequent category-`0x03` control report.
        """
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
        """Query (`02 03`) and return `EafStatus` telemetry.

        Decodes `reply[4:16]` according to `self.firmware_version`:

        - **Coordinate width:** On `fw <= 3.3.5`, `reply[6:8]` is 16-bit motor speed,
          `reply[8:10]` is 16-bit `step`, and `reply[14:16]` is 16-bit `max_step`. On
          `fw > 3.3.5`, `reply[6]` is `max_step` bits `16..23`, `reply[7:10]` is 24-bit
          `step`, and `reply[14:16]` is `max_step` bits `0..15`.
        - **Hand-controller motion (`reply[13] & 0x04`):** On tested hardware
          (`fw 3.1.9`), while an operator presses the physical IN/OUT buttons on the
          3.5 mm hand controller, `reply[4]` (`state`) stays `0x00` (`_STATE_SETTLED`)
          and only bit `0x04` of `reply[13]` asserts (`hand_controller_active=True`),
          slewing at slow (~36.5 steps/s) then fast (~285 steps/s) speed and holding
          the cached `raw_temp` word in `reply[11:13]` until button release.
          `EafStatus.moving` is `True` when either `reply[4] == 0x01` or
          `hand_controller_active` is `True`.
        - **Temperature fallback (`reply[11:13]`):** Decoded as
          `(raw_temp - 30_000) / 100.0` °C when `10_000 < raw_temp < 0xFFFF` (or `None`
          otherwise). On tested EAF hardware (`fw 3.1.9` / `3.2.2`), unplugging the
          external 3.5 mm probe does not produce `None`—the MCU seamlessly reports its
          internal PCB/MCU sensor (~31–35 °C) and switches back to the external probe
          (~23 °C, with a brief buzzer beep even if `beep=False`) when plugged in,
          without changing any flag byte.

        Raises:
            ZwoError: If `state == 0x06` (latched hardware error, clearable via
                `await clear_error()`), `state` is unknown, or `step`/`max_step` is out
                of bounds.
        """
        status, _ = await self._read_status_report()
        return status

    async def get_battery_status(self) -> EafBatteryStatus:
        """Query (`02 1e`) and return `EafBatteryStatus` on battery-equipped EAF Pro models.

        Unlike standard `02 <selector>` queries that echo `<selector>` in `reply[3]`,
        the EAF Pro MCU replies to `02 1e` with `reply[3] == 0x00`
        (`01 7e 5a 00 <pad> ...`), matched via `override_expected_selector=0x00`.
        Unpacks `reply[5:16]` as big-endian `>BHHBHBH` and converts `reply[5]` via
        `temperature_c = raw_temp - 100` (valid range `-40..87` °C).

        Raises:
            ZwoError: If the device does not support battery telemetry (unpopulated /
                out-of-range fields) or the HID query fails.
        """
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

    async def get_error_code(self) -> tuple[str, str]:
        """Query (`02 05`) and return `(motor_error_code, battery_error_code)` on EAF Pro models.

        Unpacks ASCII `b"E<motor_digit><battery_digit>\\x00"` in `reply[4:8]` into a
        pair of `"E<digit>"` strings, e.g. `("E0", "E0")`:
        - **Motor error codes:** `"E0"` (normal), `"E5"` (motor stall / position lost).
        - **Battery error codes:** `"E0"` (normal), `"E6"` (charge over-temperature),
          `"E7"` (discharge over-temperature), `"E8"` (charge under-temperature).

        Raises:
            ZwoError: If the firmware does not implement `02 05` (legacy EAF returns a
                1-byte `b"\\x01"` or unpopulated buffer) or the reply format is invalid.
        """
        reply = await self._query(_CMD_GET_ERROR_CODE)
        if reply[4] != ord("E") or not reply[5:7].isdigit():
            raise ZwoError(f"Invalid EAF error code: {reply.hex(' ')}")
        motor_digit, battery_digit = reply[5:7]
        return (f"E{chr(motor_digit)}", f"E{chr(battery_digit)}")

    async def get_led_state(self) -> bool:
        """Query (`02 1f`) and return whether the EAF Pro status LED is enabled (`reply[4] == 1`).

        Raises:
            ZwoError: If `reply[4]` is not `0` or `1` or the HID query fails.
        """
        reply = await self._query(_CMD_GET_LED_STATE)
        state = reply[4]
        if state not in (0, 1):
            raise ZwoError(f"Invalid EAF LED state=0x{state:02x}: {reply.hex(' ')}")
        return bool(state)

    async def set_led_state(self, enabled: bool) -> None:
        """Enable (`20 01`) or disable (`20 00`) the EAF Pro status LED.

        Uses category byte `0x20` in `tx[3]` (`03 7e 5a 20 <00|01>`), whereas reading
        the LED uses `02 1f` (category `0x1f` in `tx[3]` is the EAF Pro shipping-mode
        power-down command). Returns after sending the command, without readback
        confirmation (call `await get_led_state()` to verify).
        """
        await self._send(bytes((_CMD_SET_LED_STATE, 1 if enabled else 0)))

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
        """Construct a 13-byte category-`0x03` master control payload (`tx[3:16]`).

        Format: `03 <run> <backlash> <coord_4_bytes> <opcode> 00 00 <flags> <max_lo_2_bytes>`.
        Preserves live `backlash` (`status_reply[5]`), `speed` (`status_reply[6:8]` on
        `fw <= 3.3.5`) or `max_step` upper byte (`status_reply[6]` on `fw > 3.3.5`),
        `flags` (`status_reply[13]`), and `max_step` lower 16 bits (`status_reply[14:16]`)
        from `status_reply` whenever not explicitly overridden. Always masks out
        `_FLAG_HAND_CONTROLLER` (`0x04`) from outgoing `tx[13]` because incoming
        `reply[13] & 0x04` is read-only hand-controller state while outgoing
        `tx[13] & 0x04` is interpreted by the MCU as `bAutoClearErr`.
        """
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
        """Move to `step` (`0..max_step`), poll every 200 ms until settled, and return the settled step.

        Reads a fresh `02 03` status report first to reject motion when the initial
        status reports `moving=True` (the initial status query and move command send
        are separate HID transactions), validate `0 <= step <= status.max_step`, and
        preserve live `backlash`, `speed`, `flags`, and `max_step` in the outgoing
        `03 01 ...` (`opcode=0x00`) control frame.

        - **Polling deadline & continued hardware motion:** The `timeout` deadline
          starts after the move command has been sent; each requested sleep is capped to
          the remaining time, a settled final response takes precedence over deadline
          expiry, and event-loop scheduling, executor queueing, and blocking HID calls
          can delay completion (`timeout` is not a hard end-to-end deadline). Timeout or
          cancellation ends host polling only; it does not abort MCU motion on hardware
          (call `await stop()` to halt an active USB move).
        - **Incomplete moves & hardware torque-stall observations:** A returned settled
          position differing from `step` indicates an incomplete move (for example due
          to a mechanical torque stall or external stop). On tested EAF hardware
          (`fw 3.1.9` / `3.2.2`), when mechanical load exceeded the configured
          `max_force` level (`1..5`), the MCU emitted an audible buzzer beep (even when
          `beep=False`), halted the stepper motor autonomously within 1 step (observed
          within a single ~6 ms USB query interval) without requiring a host `stop()`
          command, and transitioned `reply[4]` directly from `0x01` (`moving=True`) to
          `0x00` (`moving=False`) at the stopped coordinate *without* setting
          `reply[4] = 0x06` (`_STATE_ERROR`).

        Raises:
            ValueError: If `timeout <= 0`, `timeout` is non-finite, or `step` is outside
                `[0, status.max_step]`.
            ZwoError: If the initial status reports moving or a HID/hardware fault
                occurs.
            TimeoutError: If the focuser does not settle before `timeout` expires.
        """
        _validate_timeout(timeout)

        status, reply = await self._read_status_report()
        if status.moving:
            raise ZwoError("EAF is already moving")
        if not (0 <= step <= status.max_step):
            raise ValueError(f"step={step} outside allowed range [0, {status.max_step}]")

        await self._send(self._build_control_payload(reply, run=_RUN_MOVE, step=step, opcode=_OPCODE_MOVE_OR_STOP))
        return (await self._wait_until_settled(timeout, f"move({step})")).step

    async def stop(self, *, timeout: float = 5.0) -> int:
        """Send a stop command (`03 00 ...`, `opcode=0x00`), poll until settled, and return the settled step.

        After timeout validation and a successful status read, transmits a stop command
        (`03 00 ...`, `opcode=0x00`) preserving live telemetry settings even if already
        settled. If that initial `02 03` status read reported `moving=False`, returns
        `status.step` immediately without sleeping; otherwise polls every 200 ms until
        `moving` becomes `False` (subject to the same polling deadline semantics as
        `move()`). On tested hardware (`fw 3.1.9`), if `moving` is `True` because an
        operator is physically holding a hand-controller button
        (`hand_controller_active=True`), USB `stop()` cannot override the physical
        button press until it is released.

        Raises:
            ValueError: If `timeout <= 0` or `timeout` is non-finite.
            ZwoError: If a HID or hardware fault occurs.
            TimeoutError: If the focuser does not settle before `timeout` expires.
        """
        _validate_timeout(timeout)

        status, reply = await self._read_status_report()
        await self._send(
            self._build_control_payload(reply, run=_RUN_STOP, step=status.step, opcode=_OPCODE_MOVE_OR_STOP)
        )
        if not status.moving:
            return status.step
        return (await self._wait_until_settled(timeout, "stop()")).step

    async def reset_position(self, step: int) -> None:
        """Overwrite the current focuser step coordinate (`0..max_step`) in the MCU without moving.

        Rejects the operation when the initial status reports `moving=True`. Sends a
        category-`0x03` control report with `run=0x00` and `opcode=0x01`
        (`_OPCODE_RESET_POSITION`), updating the MCU's step counter in place while
        preserving live `backlash`, `speed`, `flags`, and `max_step`. Returns after
        sending the command, without readback confirmation (call `await get_status()`
        to verify `status.step`).

        Raises:
            ValueError: If `step` is outside `[0, status.max_step]`.
            ZwoError: If the initial status reports moving or a HID/hardware fault
                occurs.
        """
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
        """Update one or more EAF configuration settings in a single outgoing control frame.

        Reads a fresh `02 03` status report (rejecting if `status.moving` is `True`),
        merges the requested updates into the live telemetry bytes while preserving
        unspecified fields, and sends a single category-`0x03` control report with
        `run=0x00` and `opcode=0x02` (`_OPCODE_SET_MAX_STEP`) when `max_step` is
        specified or `opcode=0x00` (`_OPCODE_MOVE_OR_STOP`) otherwise. On tested EAF
        firmware (`fw 3.1.9`), a single `run=0x00, opcode=0x02` report applies
        `max_step`, `backlash` (`tx[5]`), and `flags` (`tx[13]`: `beep`, `reverse`,
        `max_force`) in one outgoing frame. Note that the initial status read and
        control-frame send are separate HID transactions (not an atomic read-modify-write
        against concurrent callers or processes), and `configure()` returns after
        sending the command without readback confirmation (call `await get_status()` to
        verify the updated fields).

        Raises:
            ValueError: If `backlash` is outside `[0, 255]`, `max_force` is outside
                `[1, 5]`, or `max_step` is outside `[max(1, status.step), self.step_range]`.
            ZwoError: If the initial status reports moving or a HID/hardware fault
                occurs.
        """
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
