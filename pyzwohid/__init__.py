# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pure-Python USB HID interface to ZWO astronomy hardware.

Provides path-based asynchronous controllers for ZWO USB HID devices over `hid`
(`hidapi`) without proprietary C/C++ vendor shared libraries:

- **Base transport (`pyzwohid.hid`):** `ZwoHidDevice`, `ZwoHidStatus`, `ZwoError`,
  `ZwoBusyError`, `ZwoDevicePathError`, and `ZwoQueryCollisionError`.
- **Electronic Filter Wheels (`pyzwohid.efw`):** `Efw` and `EfwStatus`.
- **Electronic Automatic Focusers (`pyzwohid.eaf`):** `Eaf`, `EafStatus`, and
  `EafBatteryStatus`.
"""

from __future__ import annotations

from .eaf import Eaf, EafBatteryStatus, EafStatus
from .efw import Efw, EfwStatus
from .hid import ZwoBusyError, ZwoDevicePathError, ZwoError, ZwoHidDevice, ZwoHidStatus, ZwoQueryCollisionError

__all__ = [
    "Eaf",
    "EafBatteryStatus",
    "EafStatus",
    "Efw",
    "EfwStatus",
    "ZwoBusyError",
    "ZwoDevicePathError",
    "ZwoError",
    "ZwoHidDevice",
    "ZwoHidStatus",
    "ZwoQueryCollisionError",
]
