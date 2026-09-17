#!/usr/bin/env python3
"""
unifi-tools-export-devices - Export UniFi devices (and/or clients) to CSV.

Authenticates to a UniFi OS console (UDM, UCG, UDR, Cloud Key Gen2+) with an
API key passed via --api-key or read from the environment or .env. Shared code
lives in _unifi_tools_common.py. Standard library only, Python 3.8+.

Examples:
    python unifi-tools-export-devices.py --host 192.168.1.1
    python unifi-tools-export-devices.py --host 192.168.1.1 --what both
"""

import argparse
import json
import sys
from datetime import datetime

from _unifi_tools_common import (
    LIST_SEPARATOR,
    add_common_args,
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
    return parser.parse_args()


def main():
    args = parse_args()
    client = connect(args)
    if not client:
        return 1

    kinds = ["devices", "clients"] if args.what == "both" else [args.what]
    timestamp = new_timestamp()
    folders = []
    for kind in kinds:
        output_dir = export_dir(args, kind)
        folders.append(output_dir)
        rows = build_rows(client.get(ENDPOINTS[kind]), kind, args.uppercase_mac)
        stem = export_stem(args.site, kind)
        path = save_export(
            rows, EXPORT_COLUMNS, output_dir, stem, timestamp, args.max_copies
        )
        print(f"Wrote {len(rows)} {kind} to {path}")
    if args.open_folder:
        # One folder per type; with --what both open their shared parent
        open_folder(folders[0] if len(set(folders)) == 1 else folders[0].parent)
    return 0


if __name__ == "__main__":
    sys.exit(run(main))
