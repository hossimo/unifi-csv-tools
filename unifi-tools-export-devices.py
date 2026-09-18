#!/usr/bin/env python3
"""
unifi-tools-export-devices - Export UniFi devices (and/or clients) to CSV.

Authenticates to a UniFi OS console (UDM, UCG, UDR, Cloud Key Gen2+) with an
API key passed via --api-key or read from the environment or .env. Shared code
lives in _unifi_tools_common.py. Standard library only, Python 3.8+.

--for-import writes the smaller set of columns that
unifi-tools-import-devices.py reads back, so a site can be exported, edited in
a spreadsheet, and imported again.

Examples:
    python unifi-tools-export-devices.py --host 192.168.1.1
    python unifi-tools-export-devices.py --host 192.168.1.1 --what both
    python unifi-tools-export-devices.py --host 192.168.1.1 --for-import
    python unifi-tools-export-devices.py --host 192.168.1.1 --check
"""

import argparse
import json
import sys
from datetime import datetime

from _unifi_tools_common import (
    AP_GROUP_ENDPOINT,
    DEVICE_COLUMNS,
    LIST_SEPARATOR,
    UniFiError,
    add_common_args,
    check_devices,
    connect,
    export_dir,
    export_stem,
    new_timestamp,
    open_folder,
    run,
    save_export,
)

DEFAULT_WHAT = "devices"  # devices | clients | both
MAX_CELL_LENGTH = 32000  # Excel cell limit is 32767 chars

# --for-import writes a different shape, so it gets its own folder and filename
# stem; mixing the two in one folder would leave --max-copies pruning a mix.
IMPORT_KIND = "devices-import"
DEFAULT_GROUP_ID = "default"  # attr_hidden_id of the automatic "All APs" group

# Network API endpoints, relative to /api/s/<site>/
ENDPOINTS = {
    "devices": "stat/device",  # Adopted UniFi hardware (APs, switches, gateways)
    "clients": "stat/sta",  # Currently connected clients
}

# Columns written to the CSV, in order, as API field -> header label.
# All other API fields are dropped.
EXPORT_COLUMNS = {
    "name": "Name",
    "hostname": "Hostname",
    "mac": "MAC",
    "ip": "IP",
    "model": "Model",
    "type": "Type",
    "version": "Version",
    "state_name": "Status",
    "serial": "Serial",
    "adopted": "Adopted",
    "uptime": "Uptime",
    "last_seen": "Last Seen",
}
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # last_seen, in local time
UPPERCASE_MAC = False  # True writes MACs as AA:BB:CC:DD:EE:FF

# Device "state" codes as reported by the Network application. Not officially
# documented; values match the DeviceState enum in aiounifi (Home Assistant).
DEVICE_STATES = {
    0: "Disconnected",
    1: "Connected",
    2: "Pending Adoption",
    3: "Firmware Mismatch",
    4: "Upgrading",
    5: "Provisioning",
    6: "Heartbeat Missed",
    7: "Adopting",
    8: "Deleting",
    9: "Inform Error",
    10: "Adoption Failed",
    11: "Isolated",
}


def flatten(obj, parent=""):
    """Flatten nested dicts into dotted keys; lists become one cell each."""
    row = {}
    for key, value in obj.items():
        name = f"{parent}.{key}" if parent else str(key)
        if isinstance(value, dict):
            row.update(flatten(value, name))
        elif isinstance(value, list):
            if all(not isinstance(v, (dict, list)) for v in value):
                row[name] = LIST_SEPARATOR.join(str(v) for v in value)
            else:
                row[name] = json.dumps(value, separators=(",", ":"))
        else:
            row[name] = value
        if isinstance(row.get(name), str) and len(row[name]) > MAX_CELL_LENGTH:
            row[name] = row[name][:MAX_CELL_LENGTH] + "...[truncated]"
    return row


def build_rows(records, kind, uppercase_mac=UPPERCASE_MAC):
    """Flatten API records and add friendly derived columns."""
    rows = []
    for record in records:
        row = flatten(record)
        if kind == "devices" and "state" in row:
            row["state_name"] = DEVICE_STATES.get(row["state"], "Unknown")
        row["last_seen"] = format_timestamp(row.get("last_seen"))
        if uppercase_mac and isinstance(row.get("mac"), str):
            row["mac"] = row["mac"].upper()
        rows.append(row)
    return rows


def ap_group_names(groups):
    """Return {device MAC: [group name]} for the groups a CSV may change."""
    names = {}
    for group in groups:
        if group.get("attr_hidden_id") == DEFAULT_GROUP_ID:
            continue  # "All APs" holds every AP and is maintained by the console
        for mac in group.get("device_macs") or []:
            names.setdefault(mac, []).append(group.get("name", ""))
    return names


def build_import_rows(devices, groups):
    """Turn device records into the rows unifi-tools-import-devices.py reads."""
    groups_by_mac = ap_group_names(groups)
    rows = []
    for device in devices:
        mac = device.get("mac", "")
        config = device.get("config_network") or {}
        # A device on DHCP keeps whatever ip it was last given statically, so
        # those columns are only filled in when the setting is actually static.
        static = config.get("type") == "static"
        dns = [config.get(field) for field in ("dns1", "dns2")] if static else []
        rows.append(
            {
                "mac": mac,
                "name": device.get("name") or "",
                "model": device.get("model") or "",
                "type": device.get("type") or "",
                "ip": device.get("ip") or "",
                "ip_mode": "Static" if static else "DHCP",
                "static_ip": config.get("ip", "") if static else "",
                "netmask": config.get("netmask", "") if static else "",
                "gateway": config.get("gateway", "") if static else "",
                "dns": LIST_SEPARATOR.join(d for d in dns if d),
                # Blank, not None, for an AP in no group: both mean the same
                # thing on the way back in, and blank cannot surprise anyone.
                "ap_groups": LIST_SEPARATOR.join(sorted(groups_by_mac.get(mac, []))),
            }
        )
    return rows


def format_timestamp(value):
    """Convert Unix epoch seconds to DATETIME_FORMAT; blank if missing/invalid."""
    try:
        return datetime.fromtimestamp(int(value)).strftime(DATETIME_FORMAT)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export UniFi devices/clients to a timestamped CSV."
    )
    add_common_args(parser)
    parser.add_argument(
        "--what",
        choices=["devices", "clients", "both"],
        default=DEFAULT_WHAT,
        help=f"What to export (default: {DEFAULT_WHAT})",
    )
    parser.add_argument(
        "--uppercase-mac",
        action="store_true",
        default=UPPERCASE_MAC,
        help="Write MAC addresses in uppercase",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Also ask the console whether each device's saved settings are "
        "still valid, and exit non-zero if any are not. Sends one no-op write "
        "per device, which changes nothing and does not reprovision anything",
    )
    parser.add_argument(
        "--for-import",
        action="store_true",
        help="Write the columns unifi-tools-import-devices.py reads back "
        "(devices only) instead of the full export",
    )
    args = parser.parse_args()
    if args.for_import and args.what != "devices":
        parser.error(
            f"--for-import has nothing to say about clients; drop "
            f"--what {args.what}"
        )
    if args.check and args.what == "clients":
        parser.error("--check reads device settings; drop --what clients")
    return args


def run_check(client, devices):
    """Report the devices the console would refuse to save; returns the failures.

    A device is checked by writing its own name back to it. That leaves an
    empty delta, so nothing about the device changes, but the console still
    validates the whole stored document on the way through and says so when
    something in it has gone out of bounds.
    """
    print(f"Checking {len(devices)} devices...")
    failures = check_devices(client, devices)
    if failures:
        print(
            f"Check: {len(failures)} of {len(devices)} devices cannot be saved "
            f"as they are. Until each is fixed in UniFi, importing any change "
            f"to it will fail.",
            file=sys.stderr,
        )
    else:
        print(f"Check: all {len(devices)} devices are in a saveable state.")
    return failures


def export_for_import(args, client, timestamp):
    """Write the import-shaped device CSV; returns an exit code."""
    devices = client.get(ENDPOINTS["devices"])
    try:
        groups = client.get_v2(AP_GROUP_ENDPOINT)
    except UniFiError as err:
        print(f"Warning: could not read AP groups: {err}", file=sys.stderr)
        groups = []

    output_dir = export_dir(args, IMPORT_KIND)
    stem = export_stem(args.site, IMPORT_KIND)
    rows = build_import_rows(devices, groups)
    path = save_export(
        rows, DEVICE_COLUMNS, output_dir, stem, timestamp, args.max_copies
    )
    print(f"Wrote {len(rows)} devices to {path}")
    print("Edit it, then apply it:")
    print(f"  python unifi-tools-import-devices.py {path} --host {args.host} --dry-run")
    failures = run_check(client, devices) if args.check else []
    if args.open_folder:
        open_folder(output_dir)
    return 1 if failures else 0


def main():
    args = parse_args()
    client = connect(args)
    if not client:
        return 1

    timestamp = new_timestamp()
    if args.for_import:
        return export_for_import(args, client, timestamp)

    kinds = ["devices", "clients"] if args.what == "both" else [args.what]
    folders = []
    failures = []
    for kind in kinds:
        output_dir = export_dir(args, kind)
        folders.append(output_dir)
        records = client.get(ENDPOINTS[kind])
        rows = build_rows(records, kind, args.uppercase_mac)
        stem = export_stem(args.site, kind)
        path = save_export(
            rows, EXPORT_COLUMNS, output_dir, stem, timestamp, args.max_copies
        )
        print(f"Wrote {len(rows)} {kind} to {path}")
        # After the write, so a refused device never costs anyone the export
        if args.check and kind == "devices":
            failures = run_check(client, records)
    if args.open_folder:
        # One folder per type; with --what both open their shared parent
        open_folder(folders[0] if len(set(folders)) == 1 else folders[0].parent)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run(main))
