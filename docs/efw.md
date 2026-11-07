# ZWO Electronic Filter Wheels (`Efw`)

`pyzwohid.efw` provides [`Efw`](../pyzwohid/efw.py) and [`EfwStatus`](../pyzwohid/efw.py) for controlling single-disc ZWO USB HID Electronic Filter Wheels (`VID=0x03c3`, `PID in {0x1f01, 0x1f02}`).

## Hardware status

- [x] **ZWO EFW 7×2" (legacy revision, fw `3.0.6`, `03c3:1f01`)** — Verified on hardware (`openSUSE Tumbleweed x86_64`).
- [ ] **ZWO EFW 7×2" II (USB Type-C revision, fw `3.5.2`, `03c3:1f01`)** — Untested on hardware.
- [ ] **Other single-disc slot variants (`5×2"`, `7×36mm`, `8×1.25"`, `Mini`, `03c3:1f01` / `03c3:1f02`)** — Untested on hardware (uses the same USB HID protocol and dynamic `slot_count` discovery).

> **Note:** Dual-disc EFW report formats (`reply[12] != 0`) are not supported and raise `ZwoError`.

## Key behaviors & protocol details

- **1-based slot numbering (`1 .. slot_count`):** Slot positions and arguments are 1-based, matching both the physical numbering stamped on the filter wheel carousel and the USB HID wire protocol. Total `slot_count` (`1..16`) is read dynamically from each `02 01` status report rather than hardcoded by model.
- **Status decoding (`EfwStatus`):**
  - `await wheel.get_status()` (`02 01`) returns `EfwStatus(moving, slot_count, slot)`.
  - **Settled (`moving=False`):** Reported only when `state == 0x01` *and* `target_slot == prev_slot == next_slot` (`1..slot_count`). Requiring both conditions avoids premature settled reports during the ~250–320 ms fine stepper damping window (`state == 0x04` while `reply[6:9] == (N, N, N)`) and during the ~100 ms inter-command handoff if a move was queued in the MCU's 1-deep buffer (`state == 0x01` while `target_slot != prev_slot`).
  - **Moving (`moving=True, slot_count=1..16, slot=-1`):** Reported while rotating between slots or completing fine centering.
  - **Calibrating (`moving=True, slot_count=-1, slot=-1`):** During power-on or `await wheel.calibrate()` (`state == 0x00`), `reply[9]` is a live Hall-sensor magnet counter incrementing `1..slot_count` (~2.87 s/slot) rather than the total slot count, while `reply[6:9]` stays frozen until the final homing step onto slot `1`.
  - **Latched hardware fault (`state == 0x06`):** Raises `ZwoError` reporting the MCU `error_code` (`reply[5]`, e.g. Hall-sensor timeout or motor jam). Can be unlatched without power-cycling via `await wheel.clear_error()` (`01 0f`).
- **Motion (`move_to_slot`):**
  - `await wheel.move_to_slot(slot, unidirectional=False, timeout=30.0)` sends `01 <02|03> <slot>` and polls every `200 ms` until settled, returning the settled 1-based slot.
  - **Direction flag (`unidirectional`):** `unidirectional=False` (`0x02`) takes the shortest CW/CCW path; `unidirectional=True` (`0x03`) rotates forward-only (CW) in a single continuous sweep. Direction is encoded per move on the wire and is never written to NVRAM.
  - **Rejected while moving:** `move_to_slot()` raises `ZwoError` if `status.moving` is `True`. Sending a bidirectional move into the MCU's 1-deep buffer while rotating toward `<slot_A>` causes the MCU to compute the CW/CCW shortest path for `<slot_B>` relative to its *instantaneous in-flight position* at packet arrival rather than `<slot_A>`.
  - **Same-slot no-op:** Moving to the already settled slot (`slot == status.slot`) is accepted by the MCU as an immediate no-op.
  - **No hardware abort:** The EFW wire protocol has no stop command; once `01 <02|03> <slot>` is sent, the MCU completes the rotation autonomously even if `move_to_slot()` raises `TimeoutError` or is cancelled.
- **Calibration (`calibrate`):**
  - `await wheel.calibrate(timeout=60.0)` (`01 01`) runs a 3-phase Hall-sensor calibration sweep (~27.7–30.6 s on a 7-slot wheel): seeks the Hall index reference, rotates a full revolution counting magnets `1..slot_count`, and homes onto **slot `1`** regardless of the starting slot.
- **Device identity & flash alias (inherited from [`ZwoHidDevice`](../pyzwohid/hid.py)):**
  - `await wheel.get_firmware_version()` (`02 04`) -> `(major, minor, build)`.
  - `await wheel.get_serial_number()` (`02 0c`) -> 16-character hex string of the 8-byte MCU UID (supported on `fw > 3.0.8` / Gen 2 models).
  - `await wheel.get_alias()` (`02 0d`) / `await wheel.set_alias(alias)` (`0d <alias_8>`) -> read or write a `0..8`-character printable ASCII alias in non-volatile MCU flash.

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
