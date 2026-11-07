# pyzwohid

`pyzwohid` is an independent, pure-Python (`>=3.11`) interface to ZWO USB HID astronomy hardware (Electronic Filter Wheels and Electronic Automatic Focusers). It provides framework-agnostic device access and error reporting over `hid` (`hidapi`) without requiring ZWO's proprietary C shared libraries. Integrations with observatory-control frameworks are maintained separately.

> **Status:** Experimental asynchronous ZWO EFW (Electronic Filter Wheel) and EAF (Electronic Automatic Focuser) baseline.

## Table of Contents

- [Philosophy](#philosophy)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [How it works](#how-it-works)
- [Device Guides](#device-guides)
  - [ZWO Electronic Filter Wheels (`Efw`, `EfwStatus`)](docs/efw.md)
  - [ZWO Electronic Automatic Focusers (`Eaf`, `EafStatus`, `EafBatteryStatus`)](docs/eaf.md)
- [Operating System Support](#operating-system-support)
  - [Linux (`hidraw` & `udev` permissions)](docs/linux.md)
  - [macOS](docs/macos.md)
  - [Windows](docs/windows.md)
- [Quickstart](#quickstart)
- [Running tests](#running-tests)
- [License](#license)

## Philosophy

I'm building `pyzwohid` primarily for my own observatory, and I test it extensively against the hardware I actually own. At the same time, it is deliberately written **without hardcoded assumptions** tied to my specific rig—no baked-in slot counts or silent NVRAM overwrites on connect.

Every implemented module uses the device family's native USB protocol rather than model-specific constants. It started for my own setup, but it's MPL-2.0-licensed and free to grab for mainstream use.

## Prerequisites

- **Python `>= 3.11`** and **`git`** (for installing from Git)
- **System `libhidapi` shared library** (loaded at runtime by the Python `hid` package; e.g. `libhidapi-hidraw0` on openSUSE and Debian/Ubuntu, or `hidapi` on Fedora/RHEL) and non-root device permissions—see the [Linux setup guide](docs/linux.md) for `libhidapi` and `udev` installation.

## Installation

Install `pyzwohid` inside a virtual environment directly from Git (`git` is required):

```bash
python -m pip install 'git+https://github.com/ms6107/pyzwohid.git@0.2.1'
```

## How it works

`pyzwohid` communicates directly with ZWO USB HID devices via 16-byte HID Feature Reports (`03 7e 5a ...` / `01 7e 5a ...`) in [`pyzwohid/hid.py`](pyzwohid/hid.py) (`ZwoHidDevice` and `ZwoHidStatus`):

- **Path-addressed, valid-by-construction handle:** `await Efw.open(path)` and `await Eaf.open(path)` verify the USB `(VID, PID)` and open the OS HID device node (discovered via `await Efw.discover()` / `await Eaf.discover()` or a persistent `udev` symlink such as `/dev/hidraw/by-path/<ID_PATH>`), holding the handle open until `await close()` (or `async with` exit).
- **Shared device operations (`ZwoHidDevice`):** Every controller inherits `get_firmware_version()` (`02 04`), `get_serial_number()` (`02 0c`), `get_alias()` (`02 0d`), `set_alias(alias)` (`0d <alias_8>`), `clear_error()` (`01 0f`), and `get_status()`.
- **Single-owner concurrency & single-worker HID executor:** Methods assume one caller per physical device at a time. Concurrent operations across different devices are supported. All `hidapi` C calls (`discover`, `open`, `close`, `_send`, `_query`) are dispatched onto a single shared `ThreadPoolExecutor(max_workers=1)` so `hidapi` operations never race on process-global state or on an open `hid_device` handle, while lightweight (~3–6 ms) feature-report transactions across multiple ZWO HID devices interleave cleanly between `asyncio.sleep(0.2)` polling intervals. Executor serialization protects individual HID transactions; it does not make whole controller operations atomic.
- **Polling deadlines vs. blocking USB I/O:** On motion and calibration methods (`Efw.move_to_slot`, `Efw.calibrate`, `Eaf.move`, `Eaf.stop`), the `timeout` argument is a Python-level monotonic polling deadline:
  - The polling deadline starts after the outgoing command has been sent.
  - Each requested sleep between status polls is capped to the remaining time (`min(0.2, remaining)`).
  - Event-loop scheduling, executor queueing, and blocking HID calls can delay completion; `timeout` is not a hard end-to-end deadline.
  - A settled final response takes precedence over deadline expiry (if a status query completes with `moving=False`, the settled status is returned even if the deadline expired while that query was in flight).
  - Timeout (`TimeoutError`) or task cancellation (`asyncio.CancelledError`) ends host polling; it does not abort hardware motion on the MCU.

## Device Guides

- **[Electronic Filter Wheels (`Efw` — `03c3:1f01` / `03c3:1f02`)](docs/efw.md):** 1-based slot addressing (`1..slot_count`), `EfwStatus` decoding, bidirectional/unidirectional motion, Hall-sensor calibration, and hardware verification status.
- **[Electronic Automatic Focusers (`Eaf` — `03c3:1f10`)](docs/eaf.md):** Firmware-aware 16-bit (`60,000`) and 24-bit (`600,000`) step ranges, state-preserving control frames, `EafStatus` telemetry (temperature probe / internal sensor fallback, hand-controller detection, autonomous torque-stall behavior), and EAF Pro battery/error/LED support.
- **Camera Angle Adjusters (`CAA` — `03c3:1f20`):** Tentative (no hardware on hand—at most an experimental/skeleton implementation unless contributed and verified by a CAA owner).

## Operating System Support

- **[Linux (`x86_64` verified; `aarch64` / `armv7` / `armv6` / `x86` untested)](docs/linux.md):** `libhidapi-hidraw0` prerequisite, `hidraw` lifecycle, least-privilege `udev` rules ([`pyzwohid/udev/70-pyzwohid.rules`](pyzwohid/udev/70-pyzwohid.rules)), and persistent USB port symlinks.
- **[macOS](docs/macos.md):** TBD (untested on hardware).
- **[Windows](docs/windows.md):** TBD (untested on hardware).

## Quickstart

```python
import asyncio
from pyzwohid import Eaf, Efw


async def main() -> None:
    efw_paths = await Efw.discover()
    if efw_paths:
        async with await Efw.open(efw_paths[0]) as wheel:
            status = await wheel.get_status()  # EfwStatus(moving=False, slot_count=7, slot=1)
            settled_slot = await wheel.move_to_slot(2, unidirectional=False, timeout=30.0)
            print(f"EFW settled at slot {settled_slot}")

    eaf_paths = await Eaf.discover()
    if eaf_paths:
        async with await Eaf.open(eaf_paths[0]) as focuser:
            status = await focuser.get_status()  # EafStatus(moving=False, step=30068, max_step=60000, ...)
            settled_step = await focuser.move(status.step + 100, timeout=60.0)
            print(f"EAF settled at step {settled_step}")


asyncio.run(main())
```

## Running tests

The default test suite uses in-memory `hid` fakes and runs in CI without physical hardware attached:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv run mypy pyzwohid tests
```

For opt-in real-hardware tests (`PYZWOHID_RUN_HARDWARE_TESTS=1` and `PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1`), see [`docs/efw.md`](docs/efw.md#real-hardware-tests) and [`docs/eaf.md`](docs/eaf.md#real-hardware-tests).

## License

`pyzwohid` is an experimental interface to ZWO USB HID hardware. Users are encouraged to validate its behaviour with their specific equipment before relying on it for unattended operation.
Applications integrating `pyzwohid` should provide appropriate safeguards, error recovery, and supervision for their intended use.
The project is distributed under MPL-2.0. See [LICENSE](LICENSE) for warranty and liability terms.

ZWO's original SDK license notice is preserved in [`pyzwohid/LICENSE.zwo`](pyzwohid/LICENSE.zwo).
