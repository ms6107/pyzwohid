# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Filter Wheels (EFW).

Supports single-disc ZWO filter wheels matching USB `(VID=0x03c3, PID in {0x1f01, 0x1f02})`.
Slot positions use 1-based indexing (`1..slot_count`), matching both the physical
markings on the filter wheel carousel and the raw USB HID wire protocol.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hid import ZwoError, ZwoHidDevice, ZwoHidStatus, _validate_timeout

# Status query (02 01) and reply decoding
_CMD_GET_STATUS = b"\x02\x01"
_STATE_CALIBRATING = 0x00
_STATE_SETTLED = 0x01
_STATE_ERROR = 0x06
_DUAL_DISC_OFFSET = 12
_MAX_SLOTS = 16

# Action commands (01 <subcmd> ...)
_CMD_CALIBRATE = b"\x01\x01"
_CMD_MOVE = 0x01
_DIR_BIDIRECTIONAL = 0x02
_DIR_UNIDIRECTIONAL = 0x03


@dataclass(frozen=True, slots=True)
class EfwStatus(ZwoHidStatus):
    """Snapshot of EFW status from a single `02 01` (`0x01`) status report.

    Attributes:
        moving: True while the wheel is moving or calibrating (`slot == -1`).
        slot_count: Total slots on the wheel (`1..16`), or `-1` while Hall-sensor
            calibration is counting slots (`state == 0x00`).
        slot: Settled 1-based slot (`1..slot_count`), or `-1` while moving or
            calibrating.
    """

    slot_count: int
    slot: int


class Efw(ZwoHidDevice[EfwStatus]):
    """Asynchronous ZWO EFW filter wheel controller (`03c3:1f01`, `03c3:1f02`).

    All slot arguments and status fields are 1-based (`1..slot_count`), matching
    physical wheel numbering and the HID wire protocol. Total `slot_count` (`5`, `7`,
    `8`, etc.) is read dynamically from each `02 01` status report rather than
    hardcoded by model.

    Methods assume one caller per physical device at a time. Concurrent operations
    across different devices are supported. Executor serialization protects
    individual HID transactions; it does not make whole controller operations atomic.
    """

    PIDS = (0x1F01, 0x1F02)

    async def get_status(self) -> EfwStatus:
        """Query (`02 01`) and return `EfwStatus(moving, slot_count, slot)`.

        Unpacks `state, error_code, target_slot, prev_slot, next_slot, slot_count = reply[4:10]`:

        - **Dual-disc guard (`reply[12] != 0`):** Rejects unsupported dual-disc report
          formats (`reply[10:12]`, `reply[13]`) with `ZwoError`.
        - **Latched hardware error (`state == 0x06`):** Raises `ZwoError` with the MCU
          `error_code` (`reply[5]`, e.g. Hall-sensor timeout or motor stall). Can be
          unlatched via `await clear_error()` (`01 0f`).
        - **Hall-sensor calibration (`state == 0x00`):** Returns
          `EfwStatus(moving=True, slot_count=-1, slot=-1)` *before* checking
          `1 <= slot_count <= 16`. During power-on or `calibrate()`, `reply[9]` is not
          the total slot count—it is a live Hall-sensor magnet counter incrementing
          `0/1 -> 2 -> ... -> slot_count` (~2.87 s/slot observed on 7-slot `fw 3.0.6`),
          while `reply[6:9]` remains frozen at `(0, 0, 0)` or the pre-calibration slot
          `(K, K, K)` until the final homing step onto slot `1`.
        - **Settled detection (`state == 0x01` and `target_slot == prev_slot == next_slot`):**
          `(prev_slot, next_slot)` in `reply[7:9]` bounds the current carousel position:
          `(N, N)` when centered over slot `N`'s Hall magnet, or `(N, N + 1)` /
          `(slot_count, 1)` when in the inter-slot gap (ordered by slot number,
          independent of CW/CCW rotation direction). Both `state == 0x01` and
          `target_slot == prev_slot == next_slot` (`1..slot_count`) are required:
          1. *Fine settling / damping (`state == 0x04`, `slots == (N, N, N)`):* when
             arriving at target slot `N`, `reply[6:9]` reaches `(N, N, N)` (~250–320 ms
             before `state` transitions from `0x04` to `0x01` on tested `fw 3.0.6`) while
             the stepper motor completes fine centering.
          2. *Inter-command handoff (`state == 0x01`, `slots == (new_target, old_slot, old_slot)`):*
             if a second move was buffered in the MCU's 1-deep move queue, `state`
             briefly pulses `0x01` (~100 ms on tested `fw 3.0.6`) at `old_slot` with
             `target_slot == new_target != prev_slot` before resuming `0x04`.
        """
        reply = await self._query(_CMD_GET_STATUS)
        # (prev_slot, next_slot) is the bounding slot pair of the current carousel position:
        # (N, N) when centered over slot N, or (N, N + 1) / (slot_count, 1) when between slots
        # (ordered by slot number, independent of CW/CCW rotation direction).
        state, error_code, target_slot, prev_slot, next_slot, slot_count = reply[4:10]
        if reply[_DUAL_DISC_OFFSET] != 0:
            raise ZwoError(f"Dual-disc EFW format not supported: {reply.hex(' ')}")
        if state == _STATE_ERROR:
            raise ZwoError(f"EFW hardware error (code={error_code}): {reply.hex(' ')}")
        if state == _STATE_CALIBRATING:
            # During Hall-sensor calibration (power-on or calibrate()), reply[9] is the live
            # magnet counter (1..slot_count) rather than the total slot count, and reply[6:9]
            # stays frozen at the pre-calibration slot until the final homing step onto slot 1.
            return EfwStatus(moving=True, slot_count=-1, slot=-1)
        if not (1 <= slot_count <= _MAX_SLOTS):
            raise ZwoError(f"Invalid EFW slot_count={slot_count}: {reply.hex(' ')}")
        if state == _STATE_SETTLED and target_slot == prev_slot == next_slot and 1 <= target_slot <= slot_count:
            return EfwStatus(moving=False, slot_count=slot_count, slot=target_slot)
        return EfwStatus(moving=True, slot_count=slot_count, slot=-1)

    async def move_to_slot(
        self,
        slot: int,
        *,
        unidirectional: bool = False,
        timeout: float = 30.0,
    ) -> int:
        """Move to 1-based `slot` (`1..slot_count`), poll every 200 ms until settled, and return the settled slot.

        Sends `01 <02|03> <slot>` (`0x02` = bidirectional shortest-path CW/CCW,
        `0x03` = unidirectional forward-only CW):

        - **Rejected when initial status reports moving:** Rejects motion when the
          initial `get_status()` query reports `moving=True` (note that the initial
          status query and move command send are separate HID transactions). Although
          the EFW MCU has a 1-deep move queue, sending a bidirectional move
          (`01 02 <slot_B>`) mid-flight while rotating toward `<slot_A>` causes the MCU
          to compute the CW/CCW shortest path for `<slot_B>` against its *instantaneous
          in-flight carousel position* at packet arrival rather than against `<slot_A>`,
          making the subsequent move take the long way around.
        - **Per-command direction byte:** `unidirectional` is encoded per move in
          `tx[4]` (`0x03` vs `0x02`) and is never written to NVRAM. Single-command
          `(slot_count - 1)` unidirectional moves execute natively in one continuous
          sweep.
        - **Same-slot no-op:** Moving to the already settled slot (`slot == status.slot`)
          is accepted by the MCU as an immediate no-op.
        - **Polling deadline & no hardware abort:** The `timeout` deadline starts after
          the move command has been sent; each requested sleep is capped to the
          remaining time, a settled final response takes precedence over deadline
          expiry, and event-loop scheduling, executor queueing, and blocking HID calls
          can delay completion (`timeout` is not a hard end-to-end deadline). Timeout or
          cancellation ends host polling only; the EFW wire protocol has no stop
          command, so the MCU completes the rotation autonomously.

        Raises:
            ValueError: If `timeout <= 0`, `timeout` is non-finite, or `slot` is outside
                `[1, status.slot_count]`.
            ZwoError: If the initial status reports moving/calibrating or a HID/hardware
                fault occurs.
            TimeoutError: If the wheel does not settle before `timeout` expires.
        """
        _validate_timeout(timeout)

        status = await self.get_status()
        if status.moving:
            raise ZwoError("EFW is already moving")
        if not (1 <= slot <= status.slot_count):
            raise ValueError(f"slot={slot} outside allowed range [1, {status.slot_count}]")

        direction = _DIR_UNIDIRECTIONAL if unidirectional else _DIR_BIDIRECTIONAL
        await self._send(bytes((_CMD_MOVE, direction, slot)))
        return (await self._wait_until_settled(timeout, f"move_to_slot({slot})")).slot

    async def calibrate(self, timeout: float = 60.0) -> int:
        """Run a full Hall-sensor calibration sweep (`01 01`), poll until settled, and return the settled slot (`1`).

        Rejects calibration when the initial `get_status()` query reports `moving=True`.
        Executes a 3-phase sweep (~27.7–30.6 s observed on a 7-slot wheel, `fw 3.0.6`):
        1. Rotates to locate the Hall-sensor index reference.
        2. Completes a full revolution under `state == 0x00`
           (`EfwStatus(moving=True, slot_count=-1, slot=-1)`), counting magnets
           `1..slot_count` in `reply[9]`.
        3. Homes with fine damping (`state == 0x04 -> 0x01`) onto **slot `1`**
           regardless of which slot was selected before calibration.

        The `timeout` deadline starts after `01 01` is sent; each requested sleep is
        capped to the remaining time, a settled final response takes precedence over
        deadline expiry, and timeout or cancellation ends host polling without aborting
        the MCU calibration sweep.

        Raises:
            ValueError: If `timeout <= 0` or `timeout` is non-finite.
            ZwoError: If the initial status reports moving/calibrating or a HID/hardware
                fault occurs.
            TimeoutError: If calibration does not settle before `timeout` expires.
        """
        _validate_timeout(timeout)

        if (await self.get_status()).moving:
            raise ZwoError("EFW is already moving")

        await self._send(_CMD_CALIBRATE)
        return (await self._wait_until_settled(timeout, "calibrate()")).slot
