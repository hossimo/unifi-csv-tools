#!/usr/bin/env python3
"""
unifi-tools-import-wifi - Create or update UniFi WiFi networks (SSIDs) from a CSV.

Reads the CSV written by unifi-tools-export-wifi.py and creates each SSID via the
official Network integration API (Network 10.1+). Uses the same connection
options and API key as the export scripts. SSIDs whose name already exists are
skipped unless --update is given, which changes them to match the CSV instead;
SSIDs are matched by name, so --update cannot rename one. Settings the CSV does
not cover keep their current values. Standard library only, Python 3.8+.

--template writes a starter CSV with an example of each pattern, so there is no
need to run an export first; it is the checked-in examples/wifi-template.csv.

Only "SSID Name" is required. Blank cells fall back to defaults:
    Password          blank = open network
    Network / VLAN    blank = the default network
    Broadcasting APs  blank = All
    Security          blank = WPA2_PERSONAL (OPEN when there is no password);
                      OPEN, WPA2_PERSONAL, WPA3_PERSONAL, WPA2_WPA3_PERSONAL
    Band              blank = 2.4 GHz; 5 GHz
    Hidden / Enabled  blank = No / Yes

Examples:
    python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --dry-run
    python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1
    python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --update
    python unifi-tools-import-wifi.py --template
"""

import argparse
import json
import sys
from pathlib import Path

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
    write_csv,
)

# Starter rows for --template, also checked in as examples/wifi-template.csv.
# Columns left out are written blank, which is how the defaults are shown.
TEMPLATE_FILE = "wifi-template.csv"
TEMPLATE_ROWS = [
    # The minimum: a name and a password. Everything else takes its default,
    # so this is a WPA2 network on the default network, on 2.4 and 5 GHz.
    {"name": "Office", "password": "ChangeMe123"},
    # A VLAN network chosen by name, on one band
    {
        "name": "Office-IoT",
        "password": "ChangeMe456",
        "network": "IoT",
        "broadcasting_aps": "All",
        "security": "WPA2_PERSONAL",
        "band": "2.4 GHz",
        "hidden": "No",
        "enabled": "Yes",
    },
    # The same by VLAN ID instead of name, broadcast from one AP group
    {
        "name": "Warehouse",
        "password": "ChangeMe789",
        "vlan": "30",
        "broadcasting_aps": "Group",
        "ap_groups": "Warehouse APs",
        "band": "2.4 GHz; 5 GHz",
    },
    # Named APs, hidden and turned off: the staging pattern described in README
    {
        "name": "Lab-Staging",
        "password": "ChangeMe321",
        "network": "Lab",
        "broadcasting_aps": "Specific",
        "aps": "AP-Lab-1; AP-Lab-2",
        "band": "5 GHz",
        "hidden": "Yes",
        "enabled": "No",
    },
    # No password, so an open network; Security must say so
    {
        "name": "Guest-Open",
        "network": "Guest",
        "broadcasting_aps": "All",
        "security": "OPEN",
        "enabled": "Yes",
    },
    # WPA3 on the newer bands
    {
        "name": "Modern",
        "password": "ChangeMe654",
        "security": "WPA3_PERSONAL",
        "band": "5 GHz; 6 GHz",
    },
]

DEFAULT_SECURITY = "WPA2_PERSONAL"
OPEN_SECURITY = "OPEN"
DEFAULT_BANDS = [2.4, 5]
ALLOWED_BANDS = (2.4, 5, 6)  # GHz, per the API schema
PASSPHRASE_LENGTH = (8, 63)  # WPA personal limits
FAST_ROAMING = False  # 802.11r
SAE_CONFIGURATION = {"anticloggingThresholdSeconds": 5, "syncTimeSeconds": 5}  # 1-60
PMF_MODE = "OPTIONAL"  # Protected Management Frames: REQUIRED or OPTIONAL

# What each security type needs beyond the CSV's Password. The API rejects an
# SSID that leaves any of it out, so these are sent with every new SSID; as with
# WIFI_DEFAULTS, --update keeps whatever an existing SSID is set to. Values
# match the UniFi UI. Security types missing here are rejected before any
# request is sent, so the CSV cannot produce a body the API will refuse.
SECURITY_DEFAULTS = {
    OPEN_SECURITY: {},
    "WPA2_PERSONAL": {"fastRoamingEnabled": FAST_ROAMING},
    "WPA3_PERSONAL": {
        "fastRoamingEnabled": FAST_ROAMING,
        "saeConfiguration": SAE_CONFIGURATION,
    },
    "WPA2_WPA3_PERSONAL": {
        "fastRoamingEnabled": FAST_ROAMING,
        "wpa3FastRoamingEnabled": FAST_ROAMING,
        "pmfMode": PMF_MODE,
        "saeConfiguration": SAE_CONFIGURATION,
    },
}

# The API also has WPA2/WPA3 Enterprise, which need a RADIUS profile that this
# CSV has no column for. They are named so the error can say why.
ENTERPRISE_SECURITY = ("WPA2_ENTERPRISE", "WPA3_ENTERPRISE", "WPA2_WPA3_ENTERPRISE")

# Settings sent with every new SSID that the CSV does not cover. All but
# bandSteeringEnabled are required by the API. Values match an SSID created in
# the UniFi UI; change them here to suit. --update leaves these alone on an
# SSID that already exists, keeping whatever it is set to now.
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

# Fields the API reports but rejects in an update body ("Unknown request body
# property"). Add any others the console complains about here.
READ_ONLY_FIELDS = {"id", "metadata"}

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
    # Sorted so a row and the SSID it matches compare equal whatever their order
    return [int(b) if b.is_integer() else b for b in sorted(bands)]


def build_fields(row, networks, device_tags, devices):
    """Turn one CSV row into the WiFi broadcast fields the CSV controls."""
    body = {"name": row["name"]}

    network = find_network(row, networks)
    body["network"] = {"type": "SPECIFIC", "networkId": network["id"]}

    password = row["password"]
    security = row["security"].upper() or (
        DEFAULT_SECURITY if password else OPEN_SECURITY
    )
    if security in ENTERPRISE_SECURITY:
        raise RowError(
            f"Security {security} needs a RADIUS profile, which this CSV cannot "
            f"describe; create the SSID in UniFi instead"
        )
    if security not in SECURITY_DEFAULTS:
        raise RowError(
            f"Security '{row['security']}' is not one of "
            f"{', '.join(SECURITY_DEFAULTS)}"
        )
    if security == OPEN_SECURITY:
        if password:
            raise RowError("Security is OPEN but a Password is set")
        body["securityConfiguration"] = {"type": OPEN_SECURITY}
    else:
        low, high = PASSPHRASE_LENGTH
        if not low <= len(password) <= high:
            raise RowError(f"Password must be {low}-{high} characters for {security}")
        body["securityConfiguration"] = {"type": security, "passphrase": password}

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
            FILTER_ID_FIELDS[filter_type]: sorted(ids),
        }

    body["broadcastingFrequenciesGHz"] = parse_bands(row["band"])
    body["hideName"] = parse_bool(row["hidden"], False, "Hidden")
    body["enabled"] = parse_bool(row["enabled"], True, "Enabled")
    return body


def full_security(security, current=None):
    """Security settings to send: the CSV's over the current ones, else defaults."""
    # Keep what the CSV does not cover (PMF mode, fast roaming, SAE, open
    # network encryption) when the SSID already uses this security type;
    # fall back to what the type needs when it does not.
    if current and current.get("type") == security.get("type"):
        base = current
    else:
        base = SECURITY_DEFAULTS.get(security.get("type"), {})
    return {**base, **security}


def apply_api_rules(body):
    """Settle settings the API refuses to accept together; returns the body."""
    if len(body.get("broadcastingFrequenciesGHz") or []) < 2:
        # The API rejects the setting outright on one band, even when it is off:
        # "band steering setting requires broadcasting on multiple bands"
        body.pop("bandSteeringEnabled", None)
    return body


def create_body(fields):
    """Request body for a new SSID: the CSV fields over the shared defaults."""
    body = {**WIFI_DEFAULTS, **fields}
    body["securityConfiguration"] = full_security(fields["securityConfiguration"])
    return apply_api_rules(body)


def update_body(current, fields, default_network_id=None):
    """Request body that changes only what the CSV covers, keeping the rest."""
    body = {k: v for k, v in current.items() if k not in READ_ONLY_FIELDS}
    if "broadcastingDeviceFilter" not in fields:
        body.pop("broadcastingDeviceFilter", None)  # Broadcasting APs is All
    body.update(fields)

    body["securityConfiguration"] = full_security(
        fields["securityConfiguration"], current.get("securityConfiguration")
    )

    # An SSID on the default network reports it as NATIVE rather than by ID
    network = current.get("network") or {}
    if network.get("type") == "NATIVE" and (
        (fields.get("network") or {}).get("networkId") == default_network_id
    ):
        body["network"] = network
    return apply_api_rules(body)


def normalized(field, value):
    """A field value with list order dropped where the order means nothing."""
    if field == "broadcastingFrequenciesGHz" and isinstance(value, list):
        return sorted(value)
    if field == "broadcastingDeviceFilter" and isinstance(value, dict):
        return {k: sorted(v) if isinstance(v, list) else v for k, v in value.items()}
    return value


def changed_fields(current, body):
    """Names of the fields where body differs from the SSID on the console."""
    now = {k: v for k, v in current.items() if k not in READ_ONLY_FIELDS}
    return sorted(
        k
        for k in set(body) | set(now)
        if normalized(k, body.get(k)) != normalized(k, now.get(k))
    )


def redacted(body):
    """Copy of a request body with the passphrase hidden, for printing."""
    copy = json.loads(json.dumps(body))
    security = copy.get("securityConfiguration", {})
    if security.get("passphrase"):
        security["passphrase"] = "********"
    return copy


def write_template(path):
    """Write the starter CSV to path; returns an exit code."""
    path = Path(path)
    if path.exists():
        print(
            f"Error: {path} already exists. Delete it or give another path.",
            file=sys.stderr,
        )
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_csv(TEMPLATE_ROWS, path, WIFI_COLUMNS)
    except OSError as err:
        raise UniFiError(f"Cannot write {path}: {err}") from err
    print(f"Wrote {path} with {len(TEMPLATE_ROWS)} example rows.")
    print("Edit it to match your site, then import it:")
    print(f"  python {Path(sys.argv[0]).name} {path} --host <console> --dry-run")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create or update UniFi WiFi networks from a CSV (as written by unifi-tools-export-wifi.py)."
    )
    parser.add_argument("csv", nargs="?", help="CSV file to import")
    add_connection_args(parser, host_required=False)
    parser.add_argument(
        "--template",
        nargs="?",
        const=TEMPLATE_FILE,
        metavar="PATH",
        help=f"Write a starter CSV to PATH (default: {TEMPLATE_FILE}) and exit; "
        "needs no console",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Change SSIDs that already exist to match the CSV (default: skip them)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the requests that would be sent without changing anything",
    )
    args = parser.parse_args()
    if args.template and args.csv:
        parser.error(
            f"--template writes {args.template} and reads no csv; "
            f"drop '{args.csv}' or the option"
        )
    if not args.template:
        if not args.csv:
            parser.error("the csv argument is required (or use --template)")
        if not args.host:
            parser.error("--host is required")
    return args


def main():
    args = parse_args()
    if args.template:
        return write_template(args.template)
    try:
        rows, missing = read_csv(args.csv, WIFI_COLUMNS)
    except OSError as err:
        raise UniFiError(f"Cannot read {args.csv}: {err}") from err
    if "SSID Name" in missing:
        raise UniFiError(f"{args.csv} has no 'SSID Name' column")

    client = connect(args)
    if not client:
        return 1

    # SSIDs are matched by name; the first one wins if the site has duplicates
    existing = {}
    for wifi in client.list_site(WIFI_ENDPOINT):
        existing.setdefault(wifi.get("name", "").lower(), wifi)
    networks = client.list_site(NETWORK_ENDPOINT)
    devices = client.list_site(DEVICE_ENDPOINT)
    device_tags = client.list_site(DEVICE_TAG_ENDPOINT)
    default_network_id = next((n["id"] for n in networks if n.get("default")), None)

    created = updated = unchanged = skipped = failed = 0
    seen = set()
    for line, row in enumerate(rows, start=2):  # Line 1 is the header
        name = row["name"]
        if not name:
            continue
        label = f"Line {line} '{name}'"
        key = name.lower()
        if key in seen:
            print(f"{label}: skipped, an earlier line in this file uses the same name")
            skipped += 1
            continue
        current = existing.get(key)
        if current and not args.update:
            print(
                f"{label}: skipped, an SSID with this name already exists "
                f"(use --update to change it)"
            )
            skipped += 1
            continue
        try:
            fields = build_fields(row, networks, device_tags, devices)
        except RowError as err:
            print(f"{label}: error, {err}", file=sys.stderr)
            failed += 1
            continue
        seen.add(key)

        if current:
            # The list may omit details such as the passphrase, so fetch the SSID
            try:
                detail = client.get_site(f"{WIFI_ENDPOINT}/{current['id']}")
            except UniFiError as err:
                print(f"{label}: error, {err}", file=sys.stderr)
                failed += 1
                continue
            body = update_body(detail, fields, default_network_id)
            changes = changed_fields(detail, body)
            if not changes:
                print(f"{label}: unchanged")
                unchanged += 1
                continue
            changed = ", ".join(changes)
            if args.dry_run:
                print(f"{label}: would update ({changed})")
                print(json.dumps(redacted(body), indent=2))
                updated += 1
                continue
            try:
                client.put_site(f"{WIFI_ENDPOINT}/{current['id']}", body)
            except UniFiError as err:
                print(f"{label}: error, {err}", file=sys.stderr)
                failed += 1
                continue
            print(f"{label}: updated ({changed})")
            updated += 1
            continue

        body = create_body(fields)
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

    totals = [f"{'Would create' if args.dry_run else 'Created'} {created}"]
    if args.update:
        totals.append(f"{'update' if args.dry_run else 'updated'} {updated}")
        totals.append(f"unchanged {unchanged}")
    totals += [f"skipped {skipped}", f"failed {failed}"]
    print(", ".join(totals))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(main))
