# ZWO Electronic Filter Wheels (`Efw`)

`pyzwohid.efw` provides [`Efw`](../pyzwohid/efw.py) and [`EfwStatus`](../pyzwohid/efw.py) for controlling single-disc ZWO USB HID Electronic Filter Wheels (`VID=0x03c3`, `PID in {0x1f01, 0x1f02}`).

## Hardware status

- [x] **ZWO EFW 7×2" (legacy revision, fw `3.0.6`, `03c3:1f01`)** — Verified on hardware (`openSUSE Tumbleweed x86_64`).
- [ ] **ZWO EFW 7×2" II (USB Type-C revision, fw `3.5.2`, `03c3:1f01`)** — Untested on hardware.
- [ ] **Other single-disc slot variants (`5×2"`, `7×36mm`, `8×1.25"`, `Mini`, `03c3:1f01` / `03c3:1f02`)** — Untested on hardware (uses the same USB HID protocol and dynamic `slot_count` discovery).

> **Note:** Dual-disc EFW report formats (`reply[12] != 0`) are not supported and raise `ZwoError`.

## Key behaviors & protocol details

- **Single-owner concurrency:** Methods assume one caller per physical device at a time. Concurrent operations across different devices are supported. Executor serialization protects individual HID transactions; it does not make whole controller operations atomic.
- **1-based slot numbering (`1 .. slot_count`):** Slot positions and arguments are 1-based, matching both the physical numbering stamped on the filter wheel carousel and the USB HID wire protocol. Total `slot_count` (`1..16`) is read dynamically from each `02 01` status report rather than hardcoded by model.
- **Status decoding (`EfwStatus`):**
  - `await wheel.get_status()` (`02 01`) returns `EfwStatus(moving, slot_count, slot)`.
  - **Settled (`moving=False`):** Reported only when `state == 0x01` *and* `target_slot == prev_slot == next_slot` (`1..slot_count`). Requiring both conditions avoids premature settled reports during the fine stepper damping window (~250–320 ms observed on tested 7-slot `fw 3.0.6` hardware, where `state == 0x04` while `reply[6:9] == (N, N, N)`) and during the inter-command handoff window (~100 ms observed on `fw 3.0.6` if a move was queued in the MCU's 1-deep buffer, where `state == 0x01` while `target_slot != prev_slot`).
  - **Moving (`moving=True, slot_count=1..16, slot=-1`):** Reported while rotating between slots or completing fine centering.
  - **Calibrating (`moving=True, slot_count=-1, slot=-1`):** During power-on or `await wheel.calibrate()` (`state == 0x00`), `reply[9]` is a live Hall-sensor magnet counter incrementing `1..slot_count` (~2.87 s/slot observed on tested 7-slot `fw 3.0.6` hardware) rather than the total slot count, while `reply[6:9]` stays frozen until the final homing step onto slot `1`.
  - **Latched hardware fault (`state == 0x06`):** Raises `ZwoError` reporting the MCU `error_code` (`reply[5]`, e.g. Hall-sensor timeout or motor jam). Can be unlatched without power-cycling via `await wheel.clear_error()` (`01 0f`), which returns after sending the command without readback confirmation—call `await wheel.get_status()` to verify that the fault has cleared.
- **Motion (`move_to_slot`):**
  - `await wheel.move_to_slot(slot, unidirectional=False, timeout=30.0)` sends `01 <02|03> <slot>` and polls until settled, returning the settled 1-based slot.
  - **Direction flag (`unidirectional`):** `unidirectional=False` (`0x02`) takes the shortest CW/CCW path; `unidirectional=True` (`0x03`) rotates forward-only (CW) in a single continuous sweep. Direction is encoded per move on the wire and is never written to NVRAM.
  - **Rejected when initial status reports moving:** `move_to_slot()` rejects motion when the initial status reports `moving=True` (the initial status query and command send are separate HID transactions). Sending a bidirectional move into the MCU's 1-deep buffer while rotating toward `<slot_A>` causes the MCU to compute the CW/CCW shortest path for `<slot_B>` relative to its *instantaneous in-flight position* at packet arrival rather than `<slot_A>`.
  - **Same-slot no-op:** Moving to the already settled slot (`slot == status.slot`) is accepted by the MCU as an immediate no-op.
  - **Polling deadline & continued hardware motion:** In both `move_to_slot()` and `calibrate()`, the `timeout` polling deadline starts after the command has been sent, and each requested sleep is capped to the remaining time (`min(0.2, remaining)`). Event-loop scheduling, executor queueing, and blocking HID calls can delay completion (`timeout` is not a hard end-to-end deadline), and a settled final response takes precedence over deadline expiry. The EFW wire protocol has no stop command: timeout (`TimeoutError`) or task cancellation (`asyncio.CancelledError`) ends host polling without aborting hardware motion, and the MCU completes the rotation or calibration autonomously. See [`README.md`](../README.md#how-it-works) for the shared polling deadline contract.
- **Calibration (`calibrate`):**
  - `await wheel.calibrate(timeout=60.0)` (`01 01`) rejects calibration when the initial status reports `moving=True` (as separate status-query and command-send HID transactions) and runs a 3-phase Hall-sensor calibration sweep (~27.7–30.6 s observed on tested 7-slot `fw 3.0.6` hardware): seeks the Hall index reference, rotates a full revolution counting magnets `1..slot_count`, and homes onto **slot `1`** regardless of the starting slot.
- **Device identity, flash alias & error clearing (inherited from [`ZwoHidDevice`](../pyzwohid/hid.py)):**
  - `await wheel.get_firmware_version()` (`02 04`) -> `(major, minor, build)`.
  - `await wheel.get_serial_number()` (`02 0c`) -> 16-character hex string of the 8-byte MCU UID (supported on `fw > 3.0.8` / Gen 2 models).
  - `await wheel.get_alias()` (`02 0d`) / `await wheel.set_alias(alias)` (`0d <alias_8>`) -> read or write a `0..8`-character printable ASCII alias in non-volatile MCU flash. `set_alias()` returns after sending the command without readback confirmation; call `await wheel.get_alias()` to verify the stored value.
  - `await wheel.clear_error()` (`01 0f`) -> send an error-clear command without readback confirmation; call `await wheel.get_status()` to verify.

## Usage

```python
import asyncio
from pyzwohid import Efw


async def main() -> None:
    paths = await Efw.discover()
    if not paths:
        return

    async with await Efw.open(paths[0]) as wheel:
        fw = await wheel.get_firmware_version()
        status = await wheel.get_status()
        print(f"EFW (fw {fw[0]}.{fw[1]}.{fw[2]}): {status}")

        settled_slot = await wheel.move_to_slot(2, unidirectional=False, timeout=30.0)
        print(f"Settled at slot {settled_slot}")


asyncio.run(main())
```

## Real-hardware tests

When [`70-pyzwohid.rules`](linux.md) and a physical ZWO EFW are installed locally:

- **Read-only status test:**
  ```bash
  PYZWOHID_RUN_HARDWARE_TESTS=1 uv run python -m unittest tests/test_efw_hardware.py -v
  ```
- **Opt-in motion test (`test_hardware_move_next_slot_and_back`):**
  ```bash
  PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 uv run python -m unittest tests/test_efw_hardware.py -v
  ```

> **Warning:** Never run motion tests on equipment mounted on an active telescope or during live observations.
