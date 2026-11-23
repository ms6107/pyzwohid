# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Entry point for `python -m pyzwohid`."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
