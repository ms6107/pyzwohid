# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pure-Python USB HID interface to ZWO astronomy hardware."""

from __future__ import annotations

from .eaf import Eaf, EafBatteryStatus, EafStatus
from .efw import Efw, EfwStatus
from .hid import ZwoDevicePathError, ZwoError, ZwoHidDevice, ZwoHidStatus, ZwoQueryCollisionError

__all__ = [
    "Eaf",
    "EafBatteryStatus",
    "EafStatus",
    "Efw",
    "EfwStatus",
    "ZwoDevicePathError",
    "ZwoError",
    "ZwoHidDevice",
    "ZwoHidStatus",
    "ZwoQueryCollisionError",
]
