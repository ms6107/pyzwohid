# Linux Support & Device Permissions (`udev`)

## Architecture status

- [x] **Linux `x86_64`** — Verified on physical hardware (`openSUSE Tumbleweed 20261008`, kernel `7.2.9-1-default x86_64`, and `openSUSE Leap 16.1`, kernel `6.12.0-160100.8-default x86_64`).
- [ ] **Linux `aarch64` / `armv7` / `armv6` / `x86`** — Untested on hardware.

## System prerequisites (`libhidapi`)

The PyPI `hid` package is a `ctypes` wrapper that dynamically loads the system `libhidapi` shared library (`libhidapi-hidraw.so.0`) at runtime. Ensure the native library is installed on your Linux host:

- **openSUSE Tumbleweed / Leap:** `sudo zypper install libhidapi-hidraw0`
- **Debian / Ubuntu:** `sudo apt install libhidapi-hidraw0`
- **Fedora / RHEL:** `sudo dnf install hidapi`

## Linux `hidraw` transport notes

- **`/dev/hidraw*` only:** On Linux, `hid` (`hidapi-hidraw`) communicates exclusively through `/dev/hidraw*` device nodes using `HIDIOCSFEATURE` (`SET_REPORT`) and `HIDIOCGFEATURE` (`GET_REPORT`) `ioctl` calls over USB Control Endpoint 0.
- **Persistent handle lifecycle:** `await Efw.open(path)` and `await Eaf.open(path)` hold the `/dev/hidrawN` file descriptor open until `await close()` (or `async with` exit). This avoids the ~45–50 ms Linux kernel penalty on the first open and last close of a `hidraw` node (`hid_hw_open` / `usb_kill_urb` on the interrupt URB), keeping steady-state feature-report queries at ~6 ms (`3 ms` `SET_REPORT` + `3 ms` `GET_REPORT`).
- **Multi-process query collision recovery:** ZWO MCUs stage replies to `02 <selector>` queries in a single shared `0x01` SRAM reply buffer. If another OS process opens the same `/dev/hidrawN` node and its query interleaves between `send_feature_report` and `get_feature_report`, `ZwoHidDevice._query()` detects the mismatched selector byte in `reply[3]` and immediately retries (up to 3 attempts), raising `ZwoQueryCollisionError` only if contention persists across all attempts.

## Least-privilege `udev` permissions

`pyzwohid` never auto-installs system rules and never requires running the Python process as `root`.

Unlike the upstream ZWO SDK ruleset, [`pyzwohid/udev/70-pyzwohid.rules`](../pyzwohid/udev/70-pyzwohid.rules) does **not** grant world-writable (`0666`) access to USB buses, `/sys/bus/usb/drivers/usb/bind` or `unbind`, `drivers_autoprobe`, hub `power/level` attributes, or `usbcore.usbfs_memory_mb`. Instead, it scopes access strictly to ZWO USB HID nodes (`SUBSYSTEM=="hidraw"`, `ATTRS{idVendor}=="03c3"`):

- **Attended desktop Linux sessions:** `TAG+="uaccess"` lets `systemd-logind` dynamically grant a POSIX ACL on the matching `/dev/hidraw*` node to the active local seat user (ordered at `70-` so it runs before `73-seat-late.rules`).
- **Headless observatory services / daemons / SSH sessions:** Desktop seat ACLs (`uaccess`) do not apply when no local seat session is active. The rule assigns `GROUP="zwo", MODE="0660"` so only `root` and members of the `zwo` group can open the device node.
- **Automatic `/dev/hidraw/by-path/<ID_PATH>` symlinks:** Because standard `systemd-udevd` (`60-persistent-input.rules`) skips non-`input` `hidraw` nodes, `70-pyzwohid.rules` runs `IMPORT{builtin}="path_id"` and creates `/dev/hidraw/by-path/<ID_PATH>` symlinks automatically.

### Installation

1. Create the `zwo` system group and add your operator or headless service user (e.g. `obs` / `pyobs`):
   ```bash
   sudo groupadd --system zwo
   sudo usermod -aG zwo "$USER"
   ```
2. Install `70-pyzwohid.rules` and reload `systemd-udevd`:
   - **From a local git checkout:**
     ```bash
     sudo install -m 0644 pyzwohid/udev/70-pyzwohid.rules /etc/udev/rules.d/70-pyzwohid.rules
     ```
   - **From an installed package / virtual environment (without a repo checkout):**
     ```bash
     RULE_PATH="$(python3 -c 'import importlib.resources as r; print(r.files("pyzwohid") / "udev/70-pyzwohid.rules")')"
     sudo install -m 0644 "$RULE_PATH" /etc/udev/rules.d/70-pyzwohid.rules
     ```
   - **Reload and trigger `udev`:**
     ```bash
     sudo udevadm control --reload-rules
     sudo udevadm trigger --subsystem-match=hidraw
     ```

After adding a user to the `zwo` group, log out and log back in (or restart the systemd service with `SupplementaryGroups=zwo`) so the new group membership takes effect, or replug the USB device if `udevadm trigger` was not run.

## Persistent USB port symlinks & multi-device setups

Because `/dev/hidrawN` indices can shift across reboots and USB descriptors on ZWO HID devices do not expose a unique `iSerial` string (either omitted or generic `"123456"`), you can disambiguate multiple devices in two ways:

1. **Physical USB port symlinks (`udev`):**
   - Pass the `/dev/hidraw/by-path/<ID_PATH>` symlink created by `70-pyzwohid.rules` directly to `await Efw.open(...)` / `await Eaf.open(...)` (symlinks are resolved via `os.path.realpath()` before matching `hid.enumerate()` while preserving `self.path`).
   - Or walk the parent USB hierarchy in `sysfs` using the `-a` (`--attribute-walk`) flag to find the parent USB port's `KERNELS` identifier (e.g. `KERNELS=="1-3"` or `KERNELS=="3-2.3"`):
     ```bash
     udevadm info -a /dev/hidraw0
     ```
     and add a port-pinned `SYMLINK+="..."` rule in `/etc/udev/rules.d/70-pyzwohid.rules`:
     ```udev
     SUBSYSTEM=="hidraw", KERNEL=="hidraw*", SUBSYSTEMS=="usb", ATTRS{idVendor}=="03c3", ATTRS{idProduct}=="1f01", KERNELS=="3-2.3", GROUP="zwo", MODE="0660", TAG+="uaccess", SYMLINK+="zwo-efw"
     SUBSYSTEM=="hidraw", KERNEL=="hidraw*", SUBSYSTEMS=="usb", ATTRS{idVendor}=="03c3", ATTRS{idProduct}=="1f10", KERNELS=="3-2.4", GROUP="zwo", MODE="0660", TAG+="uaccess", SYMLINK+="zwo-eaf"
     ```
2. **In-band MCU identifiers:**
   Query `await device.get_alias()` (`02 0d`, configured via `await device.set_alias("...")`) or `await device.get_serial_number()` (`02 0c`, supported on `fw > 3.0.8` / Gen 2 / Pro models) after opening each path returned by `discover()`.
