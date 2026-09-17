#!/usr/bin/env python3
"""
unifi-tools-import-wifi - Create UniFi WiFi networks (SSIDs) from a CSV file.

Reads the CSV written by unifi-tools-export-wifi.py and creates each SSID via the
official Network integration API (Network 10.1+). Uses the same connection
options and API key as the export scripts. SSIDs whose name already exists
are skipped, never changed. Standard library only, Python 3.8+.

Only "SSID Name" is required. Blank cells fall back to defaults:
    Password          blank = open network
    Network / VLAN    blank = the default network
    Broadcasting APs  blank = All
    Security          blank = WPA2_PERSONAL (OPEN when there is no password)
    Band              blank = 2.4 GHz; 5 GHz
    Hidden / Enabled  blank = No / Yes

Examples:
    python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --dry-run
    python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1
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
    UniFiError,
    WIFI_COLUMNS,
    WIFI_ENDPOINT,
    add_connection_args,
    connect,
    read_csv,
    run,
)

DEFAULT_SECURITY = "WPA2_PERSONAL"
OPEN_SECURITY = "OPEN"
DEFAULT_BANDS = [2.4, 5]
ALLOWED_BANDS = (2.4, 5, 6)  # GHz, per the API schema
PASSPHRASE_LENGTH = (8, 63)  # WPA personal limits
FAST_ROAMING = False  # 802.11r; the API requires this setting for WPA security

# Settings sent with every new SSID that the CSV does not cover. All but
# bandSteeringEnabled are required by the API. Values match an SSID created in
# the UniFi UI; change them here to suit.
WIFI_DEFAULTS = {
    "type": "STANDARD",
    "multicastToUnicastConversionEnabled": False,
    "clientIsolationEnabled": False,
    "uapsdEnabled": False,
    "channel2gLockedTo6": False,
    "dtimPeriod2gLockedTo3": False,
    "bandSteeringEnabled": True,
    "arpProxyEnabled": False,
    "bssTransitionEnabled": True,
    "advertiseDeviceName": False,
}

# Broadcasting APs label -> broadcastingDeviceFilter type (All = no filter)
FILTER_TYPES_BY_LABEL = {label.lower(): t for t, label in DEVICE_FILTER_TYPES.items()}
# broadcastingDeviceFilter type -> ID list field
FILTER_ID_FIELDS = {
    "DEVICE_TAGS": "deviceTagIds",
    "DEVICES": "deviceIds",
}

YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}


class RowError(Exception):
    """A CSV row that cannot be turned into a request."""


def parse_bool(value, default, column):
    text = value.strip().lower()
    if not text:
        return default
    if text in YES:
        return True
    if text in NO:
        return False
    raise RowError(f"{column} must be Yes or No, not '{value}'")


def split_list(value):
    return [item.strip() for item in value.split(LIST_SEPARATOR.strip()) if item.strip()]


def lookup_by_name(names, items, what, key="name"):
    """Map names (case-insensitive) to item IDs; raise if unknown or ambiguous."""
    by_name = {}
    for item in items:
        by_name.setdefault(str(item.get(key, "")).lower(), []).append(item["id"])
    ids, unknown, ambiguous = [], [], []
    for name in names:
        matches = by_name.get(name.lower(), [])
        if len(matches) == 1:
            ids.append(matches[0])
        elif matches:
            ambiguous.append(name)
        else:
            unknown.append(name)
    if unknown:
        raise RowError(f"unknown {what}: {', '.join(unknown)}")
    if ambiguous:
        raise RowError(
            f"more than one {what} named {', '.join(ambiguous)}; rename them in UniFi"
        )
    return ids


def find_network(row, networks):
    """Return the network for the Network (name) or VLAN column."""
    name, vlan = row["network"], row["vlan"]
    if name:
        matches = [n for n in networks if str(n.get("name", "")).lower() == name.lower()]
        if not matches:
            raise RowError(f"unknown network '{name}'")
        network = matches[0]
        if vlan and str(network.get("vlanId")) != vlan:
            raise RowError(
                f"network '{name}' is VLAN {network.get('vlanId')}, not {vlan}"
            )
        return network
    if vlan:
        matches = [n for n in networks if str(n.get("vlanId")) == vlan]
        if not matches:
            raise RowError(f"no network with VLAN {vlan}")
        return matches[0]
    default = next((n for n in networks if n.get("default")), None)
    if not default:
        raise RowError("no Network or VLAN given and no default network found")
    return default


def parse_bands(value):
    if not value:
        return list(DEFAULT_BANDS)
    bands = []
    for item in split_list(value.replace("+", LIST_SEPARATOR)):
        number = item.lower().replace("ghz", "").strip()
        try:
            bands.append(float(number))
        except ValueError:
            raise RowError(f"Band '{item}' is not a frequency like 2.4 or 5")
        if bands[-1] not in ALLOWED_BANDS:
            raise RowError(f"Band '{item}' must be 2.4, 5, or 6 GHz")
    if len(set(bands)) != len(bands):
        raise RowError(f"Band '{value}' lists a frequency more than once")
    return [int(b) if b.is_integer() else b for b in bands]


def build_request(row, networks, device_tags, devices):
    """Turn one CSV row into a create-WiFi-broadcast request body."""
    body = {"name": row["name"], **WIFI_DEFAULTS}

    network = find_network(row, networks)
    body["network"] = {"type": "SPECIFIC", "networkId": network["id"]}

    password = row["password"]
    security = row["security"].upper() or (
        DEFAULT_SECURITY if password else OPEN_SECURITY
    )
    if security == OPEN_SECURITY:
        if password:
            raise RowError("Security is OPEN but a Password is set")
        body["securityConfiguration"] = {"type": OPEN_SECURITY}
    else:
        low, high = PASSPHRASE_LENGTH
        if not low <= len(password) <= high:
            raise RowError(f"Password must be {low}-{high} characters for {security}")
        body["securityConfiguration"] = {
            "type": security,
            "passphrase": password,
            "fastRoamingEnabled": FAST_ROAMING,
        }

    mode = row["broadcasting_aps"].lower() or "all"
    if mode != "all":
        filter_type = FILTER_TYPES_BY_LABEL.get(mode)
        if not filter_type:
            raise RowError(
                f"Broadcasting APs must be All, Group, or Specific, not "
                f"'{row['broadcasting_aps']}'"
            )
        if filter_type == "DEVICE_TAGS":
            ids = lookup_by_name(split_list(row["ap_groups"]), device_tags, "AP group")
        else:
            ids = lookup_by_name(split_list(row["aps"]), devices, "AP")
        if not ids:
            column = "AP Groups" if filter_type == "DEVICE_TAGS" else "APs"
            raise RowError(f"Broadcasting APs is {row['broadcasting_aps']} but {column} is blank")
        body["broadcastingDeviceFilter"] = {
            "type": filter_type,
            FILTER_ID_FIELDS[filter_type]: ids,
        }

    body["broadcastingFrequenciesGHz"] = parse_bands(row["band"])
    body["hideName"] = parse_bool(row["hidden"], False, "Hidden")
    body["enabled"] = parse_bool(row["enabled"], True, "Enabled")
    return body


def redacted(body):
    """Copy of a request body with the passphrase hidden, for printing."""
    copy = json.loads(json.dumps(body))
    security = copy.get("securityConfiguration", {})
    if security.get("passphrase"):
        security["passphrase"] = "********"
    return copy


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create UniFi WiFi networks from a CSV (as written by unifi-tools-export-wifi.py)."
    )
    parser.add_argument("csv", help="CSV file to import")
    add_connection_args(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the requests that would be sent without creating anything",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        rows, missing = read_csv(args.csv, WIFI_COLUMNS)
    except OSError as err:
        raise UniFiError(f"Cannot read {args.csv}: {err}") from err
    if "SSID Name" in missing:
        raise UniFiError(f"{args.csv} has no 'SSID Name' column")

    client = connect(args)
    if not client:
        return 1

    existing = {w.get("name", "").lower() for w in client.list_site(WIFI_ENDPOINT)}
    networks = client.list_site(NETWORK_ENDPOINT)
    devices = client.list_site(DEVICE_ENDPOINT)
    device_tags = client.list_site(DEVICE_TAG_ENDPOINT)

    created = skipped = failed = 0
    seen = set()
    for line, row in enumerate(rows, start=2):  # Line 1 is the header
        name = row["name"]
        if not name:
            continue
        label = f"Line {line} '{name}'"
        if name.lower() in existing or name.lower() in seen:
            print(f"{label}: skipped, an SSID with this name already exists")
            skipped += 1
            continue
        try:
            body = build_request(row, networks, device_tags, devices)
        except RowError as err:
            print(f"{label}: error, {err}", file=sys.stderr)
            failed += 1
            continue
        seen.add(name.lower())

        if args.dry_run:
            print(f"{label}: would create")
            print(json.dumps(redacted(body), indent=2))
            created += 1
            continue
        try:
            client.post_site(WIFI_ENDPOINT, body)
        except UniFiError as err:
            print(f"{label}: error, {err}", file=sys.stderr)
            failed += 1
            continue
        print(f"{label}: created")
        created += 1

    verb = "Would create" if args.dry_run else "Created"
    print(f"{verb} {created}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(main))
