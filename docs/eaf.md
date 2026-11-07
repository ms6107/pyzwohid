# ZWO Electronic Automatic Focusers (`Eaf`)

`pyzwohid.eaf` provides [`Eaf`](../pyzwohid/eaf.py), [`EafStatus`](../pyzwohid/eaf.py), and [`EafBatteryStatus`](../pyzwohid/eaf.py) for controlling ZWO USB HID Electronic Automatic Focusers (`VID=0x03c3`, `PID=0x1f10`).

## Hardware status

- [x] **ZWO EAF (12V revision, fw `3.1.9`, and 5V revision, fw `3.2.2`, `03c3:1f10`)** — Verified on hardware (`openSUSE Tumbleweed x86_64`).
- [ ] **ZWO EAF 24-bit firmware (`fw > 3.3.5`, `600,000` step range) / EAF Pro (USB mode, `03c3:1f10`)** — Untested on hardware.

## Key behaviors & protocol details

- **Mechanical gearing & firmware coordinate width:**
  - Output shaft gearing is `5,760` steps per full 360° revolution (16 steps/°; ~341.7 steps/s USB slew rate on `fw 3.1.9`).
  - `await Eaf.open(path)` is strictly read-only: it queries `get_firmware_version()` (`02 04`) once to set `focuser.firmware_version` and `focuser.step_range` (`60_000` with 16-bit coordinates on `fw <= 3.3.5`, `600_000` with 24-bit coordinates on `fw > 3.3.5`), without overwriting device coordinates or NVRAM settings.
- **Status & telemetry (`EafStatus`):**
  - `await focuser.get_status()` (`02 03`) returns `EafStatus`:
    - `moving`: `True` while moving via a USB command (`reply[4] == 0x01`) or a physical hand-controller button press (`hand_controller_active`).
    - `step`: Current focuser position in steps (`0..max_step`).
    - `max_step`: Configured maximum step limit (`1..step_range`).
    - `temperature_c`: Temperature in °C (`(raw - 30_000) / 100.0`), or `None` if out of valid bounds. When the external 3.5 mm probe is plugged in, reports the probe temperature (~23 °C, emitting a brief buzzer beep on plug-in even when `beep=False`); when unplugged, hardware seamlessly falls back to an internal PCB/MCU sensor (~31–35 °C) without changing any flag byte.
    - `backlash`: Configured backlash compensation in steps (`0..255`).
    - `reverse`: `True` if stepper motor direction is reversed (`reply[13] & 0x02`).
    - `beep`: `True` if the motion buzzer is enabled (`reply[13] & 0x01`).
    - `max_force`: Raw 4-bit motor torque / holding force nibble (`reply[13] >> 4`, `0..15`; configurable range via `configure()` is `1..5`).
    - `hand_controller_active`: `True` while an operator holds the physical IN/OUT button on the 3.5 mm hand controller (`reply[13] & 0x04`). During hand-controller motion, `reply[4]` stays `0x00`, the motor slews at slow (~36.5 steps/s) then fast (~285 steps/s) speed, and the MCU holds the cached temperature reading in `reply[11:13]` until button release.
- **State-preserving control (`move`, `stop`, `reset_position`, `configure`):**
  - Every category-`0x03` command reads a fresh `02 03` status report first and preserves live `backlash`, `speed`/`max_step`, and `flags` bytes (while masking out read-only bit `0x04` from outgoing `tx[13]`) so unspecified NVRAM settings are never overwritten.
  - **Autonomous torque-stall / forced-stop behavior (`move`):** If mechanical load exceeds the configured `max_force` level (`1..5`), the MCU emits an audible beep (even when `beep=False`), halts the stepper motor autonomously within < 6 ms (1 step) without requiring a host `stop()` command, and transitions `reply[4]` directly from `0x01` (`moving=True`) to `0x00` (`moving=False`) at the stalled coordinate *without* setting `reply[4] = 0x06` (`_STATE_ERROR`). Callers can detect a mechanical stall by checking whether the settled step returned by `await focuser.move(target_step)` differs from `target_step`.
  - **Stopping (`stop`):** `await focuser.stop(timeout=5.0)` transmits `03 00 ...` (`opcode=0x00`) and returns immediately if already settled, or polls every `200 ms` until settled. Note that USB `stop()` cannot override a physically held hand-controller button until the operator releases it.
  - **Coordinate reset (`reset_position`) & atomic configuration (`configure`):** `await focuser.reset_position(step)` (`opcode=0x01`) updates the MCU step counter in place without moving the motor. `await focuser.configure(max_step=..., backlash=..., reverse=..., beep=..., max_force=...)` validates bounds (`backlash` `0..255`, `max_force` `1..5`, `max(1, status.step) <= max_step <= step_range`) and applies all specified fields atomically in a single category-`0x03` frame.
- **EAF Pro telemetry & controls:**
  - `await focuser.get_battery_status()` (`02 1e`, which replies with selector `reply[3] == 0x00` rather than `0x1e`) returns `EafBatteryStatus(percentage, health, temperature_c, voltage_mv, charge_current_ma, discharge_current_ma, charge_voltage_mv)`.
  - `await focuser.get_error_code()` (`02 05`, unpacking `b"E<motor><battery>\x00"` in `reply[4:8]`) returns `(motor_error_code, battery_error_code)` (e.g. `("E0", "E0")`, where motor codes are `"E0"` normal / `"E5"` stall and battery codes are `"E0"` normal / `"E6"` charge over-temp / `"E7"` discharge over-temp / `"E8"` charge under-temp).
  - `await focuser.get_led_state()` (`02 1f`) / `await focuser.set_led_state(enabled)` (`20 <00|01>`) read and control the EAF Pro status LED.
- **Device identity & error recovery (inherited from [`ZwoHidDevice`](../pyzwohid/hid.py)):**
  - `await focuser.get_firmware_version()` (`02 04`), `await focuser.get_serial_number()` (`02 0c`), `await focuser.get_alias()` (`02 0d`), `await focuser.set_alias(alias)` (`0d <alias_8>`), and `await focuser.clear_error()` (`01 0f`).

## Usage

```python
import asyncio
from pyzwohid import Eaf


async def main() -> None:
    paths = await Eaf.discover()
    if not paths:
        return

    async with await Eaf.open(paths[0]) as focuser:
        status = await focuser.get_status()
        print(f"EAF (fw {focuser.firmware_version}, range {focuser.step_range}): {status}")

        settled_step = await focuser.move(status.step + 100, timeout=60.0)
        print(f"Settled at step {settled_step}")


asyncio.run(main())
```

## Real-hardware tests

When [`70-pyzwohid.rules`](linux.md) and a physical ZWO EAF are installed locally:

- **Read-only status test:**
  ```bash
  PYZWOHID_RUN_HARDWARE_TESTS=1 uv run python -m unittest tests/test_eaf_hardware.py -v
  ```
- **Opt-in motion test (`test_hardware_move_and_back`):**
  ```bash
  PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 uv run python -m unittest tests/test_eaf_hardware.py -v
  ```

> **Warning:** Never run motion tests on equipment mounted on an active telescope or during live observations.
