# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Command-line interface for ZWO USB HID filter wheels and focusers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Sequence
from typing import Any, TypeVar

from .eaf import Eaf
from .efw import Efw
from .hid import ZwoDevicePathError, ZwoError, ZwoHidDevice

_T = TypeVar("_T")
_COMMAND_NAMES = ("list", "status", "filter", "calibrate", "focus", "limits")


def _format_firmware(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def _format_serial(serial: str | None) -> str:
    return serial if serial is not None else "n/a"


def _format_alias(alias: str | None) -> str:
    if alias is None:
        return "n/a"
    return alias if alias else "(none)"


async def _query_optional(awaitable: Awaitable[_T]) -> _T | None:
    try:
        return await awaitable
    except ZwoError:
        return None


async def _resolve_device_path(
    device_cls: type[ZwoHidDevice[Any]],
    label: str,
    explicit_path: str | None,
) -> str:
    if explicit_path is not None:
        return explicit_path
    paths = await device_cls.discover()
    if len(paths) == 1:
        return paths[0]
    if not paths:
        raise ZwoError(f"No {label} device found.")
    raise ZwoError(f"Multiple {label} devices found ({', '.join(paths)}); specify --device <path>.")


async def _format_efw_status(path: str) -> str:
    async with await Efw.open(path) as wheel:
        firmware = await wheel.get_firmware_version()
        serial = await _query_optional(wheel.get_serial_number())
        alias = await _query_optional(wheel.get_alias())
        status = await wheel.get_status()

    if status.slot_count == -1:
        state = "calibrating"
        slot_text = "unknown"
    elif status.moving:
        state = "moving"
        slot_text = f"moving / {status.slot_count}"
    else:
        state = "idle"
        slot_text = f"{status.slot} / {status.slot_count}"

    lines = [
        f"EFW {path}",
        f"  firmware : {_format_firmware(firmware)}",
        f"  serial   : {_format_serial(serial)}",
        f"  alias    : {_format_alias(alias)}",
        f"  state    : {state}",
        f"  slot     : {slot_text}",
    ]
    return "\n".join(lines)


async def _format_eaf_status(path: str) -> str:
    async with await Eaf.open(path) as focuser:
        serial = await _query_optional(focuser.get_serial_number())
        alias = await _query_optional(focuser.get_alias())
        status = await focuser.get_status()
        battery = await _query_optional(focuser.get_battery_status())
        error_code = await _query_optional(focuser.get_error_code())
        led_state = await _query_optional(focuser.get_led_state())

    if status.hand_controller_active:
        state = "moving (hand controller)"
    elif status.moving:
        state = "moving"
    else:
        state = "idle"

    temp_text = f"{status.temperature_c:.2f} C" if status.temperature_c is not None else "n/a"
    lines = [
        f"EAF {path}",
        f"  firmware   : {_format_firmware(focuser.firmware_version)} (step range: {focuser.step_range})",
        f"  serial     : {_format_serial(serial)}",
        f"  alias      : {_format_alias(alias)}",
        f"  state      : {state}",
        f"  position   : {status.step} / {status.max_step}",
        f"  temperature: {temp_text}",
        f"  backlash   : {status.backlash}",
        f"  reverse    : {'on' if status.reverse else 'off'}",
        f"  beep       : {'on' if status.beep else 'off'}",
        f"  max_force  : {status.max_force}",
    ]
    if error_code is not None:
        lines.append(f"  error_code : motor={error_code[0]} battery={error_code[1]}")
    if led_state is not None:
        lines.append(f"  led        : {'on' if led_state else 'off'}")
    if battery is not None:
        lines.append(
            f"  battery    : {battery.percentage}% (health {battery.health}%, "
            f"{battery.voltage_mv} mV, {battery.temperature_c} C)"
        )
    return "\n".join(lines)


async def _cmd_list(args: argparse.Namespace) -> int:
    device_class: str | None = args.device_class
    rows: list[str] = []
    if device_class in (None, "efw"):
        rows.extend(f"efw  {path}" for path in await Efw.discover())
    if device_class in (None, "eaf"):
        rows.extend(f"eaf  {path}" for path in await Eaf.discover())
    if not rows:
        scope = device_class.upper() if device_class is not None else "ZWO HID"
        print(f"No {scope} devices found.")
        return 0
    print("\n".join(rows))
    return 0


async def _cmd_status(args: argparse.Namespace) -> int:
    device_class: str | None = args.device_class
    explicit: str | None = args.device

    if device_class == "efw":
        path = await _resolve_device_path(Efw, "EFW", explicit)
        print(await _format_efw_status(path))
        return 0
    if device_class == "eaf":
        path = await _resolve_device_path(Eaf, "EAF", explicit)
        print(await _format_eaf_status(path))
        return 0

    if explicit is not None:
        resolved = os.path.realpath(explicit) if os.path.islink(explicit) else explicit
        if resolved in await Efw.discover():
            print(await _format_efw_status(explicit))
            return 0
        if resolved in await Eaf.discover():
            print(await _format_eaf_status(explicit))
            return 0
        raise ZwoDevicePathError(f"Device at {explicit!r} does not match any connected EFW or EAF")

    efw_paths = await Efw.discover()
    eaf_paths = await Eaf.discover()
    if not efw_paths and not eaf_paths:
        raise ZwoError("No ZWO HID devices found.")

    blocks: list[str] = []
    for path in efw_paths:
        blocks.append(await _format_efw_status(path))
    for path in eaf_paths:
        blocks.append(await _format_eaf_status(path))
    print("\n\n".join(blocks))
    return 0


async def _cmd_filter(args: argparse.Namespace) -> int:
    path = await _resolve_device_path(Efw, "EFW", args.device)
    async with await Efw.open(path) as wheel:
        settled = await wheel.move_to_slot(
            int(args.slot),
            unidirectional=bool(args.unidirectional),
            timeout=float(args.timeout),
        )
    print(f"EFW {path}: settled at slot {settled}")
    return 0


async def _cmd_calibrate(args: argparse.Namespace) -> int:
    path = await _resolve_device_path(Efw, "EFW", args.device)
    async with await Efw.open(path) as wheel:
        settled = await wheel.calibrate(timeout=float(args.timeout))
    print(f"EFW {path}: calibrated, settled at slot {settled}")
    return 0


async def _cmd_focus(args: argparse.Namespace) -> int:
    path = await _resolve_device_path(Eaf, "EAF", args.device)
    async with await Eaf.open(path) as focuser:
        if args.relative:
            status = await focuser.get_status()
            target = status.step + int(args.step)
        else:
            target = int(args.step)
        settled = await focuser.move(target, timeout=float(args.timeout))
    if settled != target:
        print(f"EAF {path}: stopped early at step {settled} (target {target})", file=sys.stderr)
        return 1
    print(f"EAF {path}: settled at step {settled}")
    return 0


async def _cmd_limits(args: argparse.Namespace) -> int:
    max_step: int | None = args.max_step
    position: int | None = args.position
    if max_step is not None and position is not None and (max_step < 1 or not (0 <= position <= max_step)):
        raise ValueError(
            f"Invalid limits: require 0 <= position <= max_step and max_step >= 1 "
            f"(got position={position}, max_step={max_step})"
        )
    path = await _resolve_device_path(Eaf, "EAF", args.device)
    async with await Eaf.open(path) as focuser:
        if max_step is not None and position is not None and max_step < (await focuser.get_status()).max_step:
            await focuser.reset_position(position)
            await focuser.configure(max_step=max_step)
        else:
            if max_step is not None:
                await focuser.configure(max_step=max_step)
            if position is not None:
                await focuser.reset_position(position)
        updated = await focuser.get_status()
    print(f"EAF {path}: position {updated.step} / {updated.max_step} (step range: {focuser.step_range})")
    return 0


def _build_parser_with_subparsers() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="pyzwohid",
        description="CLI for ZWO USB HID filter wheels (EFW) and focusers (EAF).",
        epilog=(
            "Use 'pyzwohid <command> -h' or 'pyzwohid -h <command>' for command options.\n\n"
            "Device selection:\n"
            "  When --device is omitted, commands open the sole matching EFW or EAF\n"
            "  automatically and fail if zero or multiple devices of that class exist.\n\n"
            "Examples:\n"
            "  pyzwohid list\n"
            "  pyzwohid status\n"
            "  pyzwohid status eaf\n"
            "  pyzwohid filter 3\n"
            "  pyzwohid calibrate\n"
            "  pyzwohid focus 25000\n"
            "  pyzwohid focus --relative -500\n"
            "  pyzwohid limits --max-step 60000 --position 0"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparser_map: dict[str, argparse.ArgumentParser] = {}

    p_list = subparsers.add_parser(
        "list",
        help="List connected EFW and EAF HID device paths",
        description="List connected ZWO EFW and EAF HID device paths without opening them.",
    )
    p_list.add_argument(
        "device_class",
        nargs="?",
        choices=("efw", "eaf"),
        help="Optional device class filter (efw or eaf)",
    )
    subparser_map["list"] = p_list

    p_status = subparsers.add_parser(
        "status",
        help="Show consolidated status and info for EFW/EAF devices",
        description="Query firmware, serial/alias identity, and live state for one or all devices.",
    )
    p_status.add_argument(
        "device_class",
        nargs="?",
        choices=("efw", "eaf"),
        help="Optional device class (efw or eaf)",
    )
    p_status.add_argument("-d", "--device", default=None, help="Explicit HID device path or symlink")
    subparser_map["status"] = p_status

    p_filter = subparsers.add_parser(
        "filter",
        help="Move filter wheel to a 1-based slot (optional --unidirectional)",
        description="Move EFW to a 1-based slot and wait until settled.",
    )
    p_filter.add_argument("slot", type=int, help="Target 1-based filter slot")
    p_filter.add_argument("-d", "--device", default=None, help="Explicit EFW HID device path or symlink")
    p_filter.add_argument(
        "--unidirectional",
        action="store_true",
        help="Force forward-only CW rotation instead of shortest path",
    )
    p_filter.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Move polling timeout in seconds (default: 30.0)",
    )
    subparser_map["filter"] = p_filter

    p_calibrate = subparsers.add_parser(
        "calibrate",
        help="Calibrate filter wheel Hall sensors",
        description="Run EFW Hall-sensor calibration and wait until settled at slot 1.",
    )
    p_calibrate.add_argument("-d", "--device", default=None, help="Explicit EFW HID device path or symlink")
    p_calibrate.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Calibration polling timeout in seconds (default: 60.0)",
    )
    subparser_map["calibrate"] = p_calibrate

    p_focus = subparsers.add_parser(
        "focus",
        help="Move focuser to an absolute or relative (-r/--relative) step",
        description="Move EAF to an absolute step or relative offset and wait until settled.",
    )
    p_focus.add_argument("step", type=int, help="Target step, or signed offset with -r/--relative")
    p_focus.add_argument(
        "-r",
        "--relative",
        action="store_true",
        help="Interpret step as a signed offset from current position",
    )
    p_focus.add_argument("-d", "--device", default=None, help="Explicit EAF HID device path or symlink")
    p_focus.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Move polling timeout in seconds (default: 60.0)",
    )
    subparser_map["focus"] = p_focus

    p_limits = subparsers.add_parser(
        "limits",
        help="Set focuser --max-step limit and/or --position",
        description="Update EAF max-step upper limit and/or overwrite current step position without moving.",
    )
    p_limits.add_argument("--max-step", type=int, default=None, help="New upper step limit (max_step)")
    p_limits.add_argument("--position", type=int, default=None, help="Overwrite current step coordinate (0..max_step)")
    p_limits.add_argument("-d", "--device", default=None, help="Explicit EAF HID device path or symlink")
    subparser_map["limits"] = p_limits

    p_help = subparsers.add_parser(
        "help",
        help="Show top-level help or help for a subcommand",
        description="Print usage help for pyzwohid or one of its subcommands.",
    )
    p_help.add_argument(
        "topic",
        nargs="?",
        choices=_COMMAND_NAMES,
        help="Optional subcommand name",
    )
    subparser_map["help"] = p_help

    return parser, subparser_map


def build_parser() -> argparse.ArgumentParser:
    """Build and return the root `pyzwohid` CLI ArgumentParser."""
    parser, _ = _build_parser_with_subparsers()
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "list": _cmd_list,
        "status": _cmd_status,
        "filter": _cmd_filter,
        "calibrate": _cmd_calibrate,
        "focus": _cmd_focus,
        "limits": _cmd_limits,
    }
    return await handlers[args.command](args)


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the selected `pyzwohid` command, and return the exit code."""
    parser, subparser_map = _build_parser_with_subparsers()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) >= 2 and raw_argv[0] in ("-h", "--help") and raw_argv[1] in subparser_map:
        raw_argv = [raw_argv[1], raw_argv[0], *raw_argv[2:]]
    args = parser.parse_args(raw_argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help":
        topic: str | None = args.topic
        if topic is None:
            parser.print_help()
        else:
            subparser_map[topic].print_help()
        return 0
    if args.command == "limits" and args.max_step is None and args.position is None:
        subparser_map["limits"].error("Provide at least one of --max-step or --position.")

    try:
        return asyncio.run(_dispatch(args))
    except (ZwoError, ValueError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main() -> None:
    """Console script entry point for `pyzwohid`."""
    raise SystemExit(run_cli())
