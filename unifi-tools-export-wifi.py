#!/usr/bin/env python3
"""
unifi-tools-export-wifi - Export UniFi WiFi networks (SSIDs) to CSV.

Uses the official Network integration API (Network 10.1+) with the same
connection options and API key as the other scripts; shared code lives in
_unifi_tools_common.py. The CSV columns are the ones
unifi-tools-import-wifi.py reads back. Standard library only, Python 3.8+.

The CSV contains WiFi passwords in plain text. Store it accordingly.

Examples:
    python unifi-tools-export-wifi.py --host 192.168.1.1
    python unifi-tools-export-wifi.py --host 192.168.1.1 --raw
"""

import argparse
import json
import sys

from _unifi_tools_common import (
    DEVICE_ENDPOINT,
    DEVICE_FILTER_TYPES,
    DEVICE_TAG_ENDPOINT,
    LIST_SEPARATOR,
    NETWORK_ENDPOINT,
    WIFI_COLUMNS,
    WIFI_ENDPOINT,
    UniFiError,
    UniFiUnreachable,
    add_common_args,
    connect,
    export_dir,
    export_stem,
    new_timestamp,
    open_folder,
    run,
    save_export,
)

def yes_no(value):
    return "Yes" if value else "No"


def filter_ids(device_filter):
    """Return the IDs listed in a broadcastingDeviceFilter (e.g. deviceTagIds)."""
    for key, value in device_filter.items():
        if key.endswith("Ids") and isinstance(value, list):
            return value
    return []


def build_rows(broadcasts, networks, device_tags, devices):
    """Turn WiFi broadcast records into CSV rows with names instead of IDs."""
    networks_by_id = {n.get("id"): n for n in networks}
    tag_names = {t.get("id"): t.get("name", "") for t in device_tags}
    device_names = {
        d.get("id"): d.get("name") or d.get("macAddress", "") for d in devices
    }

    rows = []
    for wifi in broadcasts:
        network_ref = wifi.get("network") or {}
        network = networks_by_id.get(network_ref.get("networkId"), {})
        if not network and network_ref.get("type") == "NATIVE":
            network = next((n for n in networks if n.get("default")), {})

        security = wifi.get("securityConfiguration") or {}
        device_filter = wifi.get("broadcastingDeviceFilter") or {}
        filter_type = device_filter.get("type")
        ids = filter_ids(device_filter)

        rows.append(
            {
                "name": wifi.get("name", ""),
                "password": security.get("passphrase", ""),
                "network": network.get("name", ""),
                "vlan": network.get("vlanId", ""),
                "broadcasting_aps": (
                    DEVICE_FILTER_TYPES.get(filter_type, filter_type)
                    if filter_type
                    else "All"
                ),
                "ap_groups": (
                    LIST_SEPARATOR.join(tag_names.get(i, i) for i in ids)
                    if filter_type == "DEVICE_TAGS"
                    else ""
                ),
                "aps": (
                    LIST_SEPARATOR.join(device_names.get(i, i) for i in ids)
                    if filter_type == "DEVICES"
                    else ""
                ),
                "security": security.get("type", ""),
                "band": LIST_SEPARATOR.join(
                    f"{f:g} GHz"
                    for f in sorted(wifi.get("broadcastingFrequenciesGHz") or [])
                ),
                "hidden": yes_no(wifi.get("hideName")),
                "enabled": yes_no(wifi.get("enabled", True)),
            }
        )
    return rows


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export UniFi WiFi networks to a timestamped CSV."
    )
    add_common_args(parser)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also save the raw API responses as JSON (for inspecting fields)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    client = connect(args)
    if not client:
        return 1

    # The list may omit details such as the passphrase, so fetch each one
    broadcasts = [
        client.get_site(f"{WIFI_ENDPOINT}/{summary['id']}")
        for summary in client.list_site(WIFI_ENDPOINT)
    ]
    networks = client.list_site(NETWORK_ENDPOINT)
    devices = client.list_site(DEVICE_ENDPOINT)
    try:
        device_tags = client.list_site(DEVICE_TAG_ENDPOINT)
    except UniFiUnreachable:
        raise
    except UniFiError as err:
        print(f"Warning: could not read AP groups: {err}", file=sys.stderr)
        device_tags = []

    output_dir = export_dir(args, "wifi")
    timestamp = new_timestamp()
    stem = export_stem(args.site, "wifi")
    rows = build_rows(broadcasts, networks, device_tags, devices)
    path = save_export(rows, WIFI_COLUMNS, output_dir, stem, timestamp, args.max_copies)
    print(f"Wrote {len(rows)} WiFi networks to {path}")

    if args.raw:
        raw_path = output_dir / f"{stem}_raw_{timestamp}.json"
        raw = {
            "wifi_broadcasts": broadcasts,
            "networks": networks,
            "device_tags": device_tags,
            "devices": devices,
        }
        raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        print(f"Wrote raw API data to {raw_path}")

    if args.open_folder:
        open_folder(output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(run(main))
