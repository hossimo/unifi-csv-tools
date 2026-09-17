#!/usr/bin/env python3
"""
unifi-tools-import-devices - Rename UniFi devices, set their management IP, and
set their AP groups from a CSV.

Reads the CSV written by unifi-tools-export-devices.py --for-import and applies
it to the devices already adopted on the site. Rows are matched to devices by
MAC, which is the only column that must be filled in: device names repeat on a
site and are not a reliable key. This script never adopts, forgets, or restarts
anything, and it cannot add a device that is not adopted yet.

Unlike the other scripts here, this one uses the private API the UniFi UI
itself uses. The official integration API can list devices but cannot rename
one, change its IP, or edit an AP group, so there is nothing else to use. Those
endpoints are undocumented and may change between Network releases.

A blank cell means "leave this as it is", so a CSV can carry only the columns
worth changing:
    Name              the device name shown in UniFi
    IP Mode           DHCP or Static; Static also needs Static IP, Netmask,
                      and Gateway, and takes an optional DNS
    AP Groups         "; " separated group names, or None for no groups.
                      Groups must already exist; APs only.
Model, Type, and IP are written by the export to read, and ignored here.

Changing IP Mode reprovisions the device, which drops it off the network for a
moment and brings it back at the new address. Get it wrong and the device is
unreachable until it is factory reset, so run --dry-run first.

--template writes a starter CSV with an example of each pattern; it is the
checked-in examples/device-template.csv. Standard library only, Python 3.8+.

Examples:
    python unifi-tools-import-devices.py devices.csv --host 192.168.1.1 --dry-run
    python unifi-tools-import-devices.py devices.csv --host 192.168.1.1
    python unifi-tools-import-devices.py --template
"""

import argparse
import json
import re
import sys
from pathlib import Path

from _unifi_tools_common import (
    AP_GROUP_ENDPOINT,
    DEVICE_COLUMNS,
    DEVICE_LIST_ENDPOINT,
    DEVICE_REST_ENDPOINT,
    LIST_SEPARATOR,
    UniFiError,
    add_connection_args,
    connect,
    read_csv,
    run,
    write_csv,
)

# Starter rows for --template, also checked in as examples/device-template.csv.
# Columns left out are written blank, which is how "leave this alone" is shown.
TEMPLATE_FILE = "device-template.csv"
TEMPLATE_ROWS = [
    # The minimum: a MAC to find the device by and the name to give it.
    # Everything else is blank, so nothing else about this device changes.
    {"mac": "78:45:58:00:00:01", "name": "AP-Lobby"},
    # Rename and pin to a static address. Static needs Netmask and Gateway;
    # DNS is optional and takes one or two servers.
    {
        "mac": "78:45:58:00:00:02",
        "name": "AP-Warehouse",
        "ip_mode": "Static",
        "static_ip": "10.0.10.21",
        "netmask": "255.255.255.0",
        "gateway": "10.0.10.1",
        "dns": "10.0.10.1; 1.1.1.1",
    },
    # Hand an address back to DHCP without touching the name
    {"mac": "78:45:58:00:00:03", "ip_mode": "DHCP"},
    # Put an AP in groups, which replaces whatever groups it is in now.
    # The groups have to exist already; this script does not create them.
    {
        "mac": "78:45:58:00:00:04",
        "name": "AP-Patio",
        "ap_groups": "Warehouse APs; Outdoor APs",
    },
    # Take an AP out of every group: None, because a blank cell means "leave it"
    {"mac": "78:45:58:00:00:05", "ap_groups": "None"},
]

DHCP = "dhcp"
STATIC = "static"
IP_MODES = (DHCP, STATIC)
STATIC_REQUIRED = ("static_ip", "netmask", "gateway")  # Row keys Static needs
DNS_FIELDS = ("dns1", "dns2")  # config_network keys the DNS column fills
NO_GROUPS = "none"  # AP Groups cell meaning "no groups", since blank means "leave"
AP_TYPE = "uap"  # Device type that can belong to an AP group

# The group every AP is in. The console maintains it, so it is not ours to edit.
DEFAULT_GROUP_ID = "default"  # Its attr_hidden_id

MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class RowError(Exception):
    """A CSV row that cannot be turned into a change."""


def normalize_mac(value, column="MAC"):
    """Return a MAC as aa:bb:cc:dd:ee:ff, however the CSV spelled it."""
    text = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(text) != 12:
        raise RowError(f"{column} '{value}' is not a MAC address")
    mac = ":".join(text[i : i + 2] for i in range(0, 12, 2))
    if not MAC_PATTERN.match(mac):
        raise RowError(f"{column} '{value}' is not a MAC address")
    return mac


def parse_ipv4(value, column):
    """Return a dotted-quad IPv4 address, or raise RowError."""
    parts = value.split(".")
    if len(parts) != 4:
        raise RowError(f"{column} '{value}' is not an IPv4 address")
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255 or len(part) > 3:
            raise RowError(f"{column} '{value}' is not an IPv4 address")
    return ".".join(str(int(p)) for p in parts)


def mask_bits(netmask):
    """Return the prefix length of a netmask, or raise RowError if it is not one."""
    bits = 0
    for octet in (int(p) for p in netmask.split(".")):
        bits = bits << 8 | octet
    ones = f"{bits:032b}"
    if "01" in ones:  # A netmask is a run of ones then a run of zeros
        raise RowError(f"Netmask '{netmask}' is not a valid subnet mask")
    return ones.count("1")


def same_subnet(ip, gateway, netmask):
    """True if ip and gateway share a subnet under netmask."""

    def packed(address):
        value = 0
        for octet in (int(p) for p in address.split(".")):
            value = value << 8 | octet
        return value

    mask = packed(netmask)
    return packed(ip) & mask == packed(gateway) & mask


def split_list(value):
    return [item.strip() for item in value.split(LIST_SEPARATOR.strip()) if item.strip()]


def parse_ip_mode(row):
    """Return the config_network to send for a row, or None to leave it alone."""
    mode = row["ip_mode"].strip().lower()
    filled = [key for key in STATIC_REQUIRED + ("dns",) if row[key]]
    if not mode:
        if filled:
            columns = ", ".join(DEVICE_COLUMNS[key] for key in filled)
            raise RowError(f"{columns} set but IP Mode is blank; say DHCP or Static")
        return None
    if mode not in IP_MODES:
        raise RowError(f"IP Mode must be DHCP or Static, not '{row['ip_mode']}'")
    if mode == DHCP:
        if filled:
            columns = ", ".join(DEVICE_COLUMNS[key] for key in filled)
            raise RowError(f"IP Mode is DHCP, so leave {columns} blank")
        return {"type": DHCP}

    missing = [DEVICE_COLUMNS[key] for key in STATIC_REQUIRED if not row[key]]
    if missing:
        blank = "is blank" if len(missing) == 1 else "are blank"
        raise RowError(f"IP Mode is Static but {', '.join(missing)} {blank}")
    ip = parse_ipv4(row["static_ip"], "Static IP")
    netmask = parse_ipv4(row["netmask"], "Netmask")
    gateway = parse_ipv4(row["gateway"], "Gateway")
    mask_bits(netmask)
    if not same_subnet(ip, gateway, netmask):
        raise RowError(
            f"Gateway {gateway} is outside {ip}/{netmask}, so the device would "
            f"have no route off its subnet"
        )
    config = {"type": STATIC, "ip": ip, "netmask": netmask, "gateway": gateway}

    servers = split_list(row["dns"])
    if len(servers) > len(DNS_FIELDS):
        raise RowError(f"DNS takes at most {len(DNS_FIELDS)} servers")
    for field, server in zip(DNS_FIELDS, servers):
        config[field] = parse_ipv4(server, "DNS")
    return config


def config_changed(current, desired):
    """True if any field the CSV sets differs from the device's current config.

    Only the fields in desired are compared. A device left on DHCP keeps a
    stale ip from whenever it was last static, and that is not a difference.
    """
    current = current or {}
    return any(current.get(field) != value for field, value in desired.items())


def build_changes(row, device):
    """Return the legacy device fields to PUT for a row; empty if nothing differs."""
    changes = {}
    name = row["name"]
    if name and name != (device.get("name") or ""):
        changes["name"] = name

    config = parse_ip_mode(row)
    if config and config_changed(device.get("config_network"), config):
        changes["config_network"] = config
    return changes


def describe_config(current, desired):
    """One line summary of an IP change, e.g. 'DHCP -> Static 10.0.10.21'."""

    def label(config):
        config = config or {}
        if config.get("type") == STATIC:
            return f"Static {config.get('ip', '?')}"
        return "DHCP"

    return f"{label(current)} -> {label(desired)}"


def editable_groups(groups):
    """The AP groups a CSV may change: every one but the automatic 'All APs'."""
    return [g for g in groups if g.get("attr_hidden_id") != DEFAULT_GROUP_ID]


def find_groups(names, groups):
    """Map AP group names (case-insensitive) to IDs; raise if unusable."""
    by_name = {}
    for group in groups:
        by_name.setdefault(str(group.get("name", "")).lower(), []).append(group)
    ids, unknown, ambiguous, automatic = [], [], [], []
    for name in names:
        matches = by_name.get(name.lower(), [])
        if len(matches) > 1:
            ambiguous.append(name)
        elif not matches:
            unknown.append(name)
        elif matches[0].get("attr_hidden_id") == DEFAULT_GROUP_ID:
            automatic.append(name)
        else:
            ids.append(matches[0]["_id"])
    if unknown:
        raise RowError(
            f"unknown AP group: {', '.join(unknown)}; create it in UniFi first"
        )
    if ambiguous:
        raise RowError(
            f"more than one AP group named {', '.join(ambiguous)}; rename them in UniFi"
        )
    if automatic:
        raise RowError(
            f"AP group '{', '.join(automatic)}' holds every AP and is maintained "
            f"by the console, so it cannot be set here"
        )
    return ids


def parse_ap_groups(row, device, groups):
    """Return the group IDs an AP should be in, or None to leave it alone."""
    value = row["ap_groups"].strip()
    if not value:
        return None
    if device.get("type") != AP_TYPE:
        raise RowError(
            f"AP Groups is set but this is a {device.get('type') or 'non-AP'} "
            f"device, and only APs belong to AP groups"
        )
    if value.lower() == NO_GROUPS:
        return []
    return sorted(set(find_groups(split_list(value), groups)))


def group_updates(wanted_by_mac, groups):
    """Return [(group, new_macs)] for the groups whose membership the CSV changes.

    Only the MACs named in the CSV move. Every other AP keeps the groups it is
    in, so a partial CSV cannot empty a group by leaving devices out.
    """
    managed = set(wanted_by_mac)
    updates = []
    for group in editable_groups(groups):
        current = list(group.get("device_macs") or [])
        kept = [mac for mac in current if mac not in managed]
        added = [mac for mac, ids in wanted_by_mac.items() if group["_id"] in ids]
        new = sorted(set(kept) | set(added))
        if new != sorted(set(current)):
            updates.append((group, new))
    return updates


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
        write_csv(TEMPLATE_ROWS, path, DEVICE_COLUMNS)
    except OSError as err:
        raise UniFiError(f"Cannot write {path}: {err}") from err
    print(f"Wrote {path} with {len(TEMPLATE_ROWS)} example rows.")
    print("The MACs in it are examples. Export your own devices to get real ones:")
    print("  python unifi-tools-export-devices.py --host <console> --for-import")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Rename UniFi devices, set their IP, and set their AP groups "
        "from a CSV (as written by unifi-tools-export-devices.py --for-import)."
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
        "--dry-run",
        action="store_true",
        help="Show what would change without changing anything",
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
        rows, missing = read_csv(args.csv, DEVICE_COLUMNS)
    except OSError as err:
        raise UniFiError(f"Cannot read {args.csv}: {err}") from err
    if "MAC" in missing:
        raise UniFiError(f"{args.csv} has no 'MAC' column")

    client = connect(args)
    if not client:
        return 1

    devices = {d["mac"].lower(): d for d in client.get(DEVICE_LIST_ENDPOINT) if d.get("mac")}
    groups = client.get_v2(AP_GROUP_ENDPOINT)

    changed = unchanged = skipped = failed = 0
    wanted_by_mac = {}  # MAC -> AP group IDs, for the group writes after the loop
    group_notes = {}  # MAC -> what to print for its AP group change
    seen = set()
    for line, row in enumerate(rows, start=2):  # Line 1 is the header
        if not row["mac"]:
            continue
        label = f"Line {line}"
        try:
            mac = normalize_mac(row["mac"])
        except RowError as err:
            print(f"{label}: error, {err}", file=sys.stderr)
            failed += 1
            continue

        label = f"{label} {mac}"
        if mac in seen:
            print(f"{label}: skipped, an earlier line in this file has the same MAC")
            skipped += 1
            continue
        device = devices.get(mac)
        if not device:
            print(
                f"{label}: error, no adopted device with this MAC on site "
                f"'{args.site}'",
                file=sys.stderr,
            )
            failed += 1
            continue

        name = device.get("name") or device.get("model") or ""
        label = f"{label} '{name}'" if name else label
        try:
            changes = build_changes(row, device)
            wanted = parse_ap_groups(row, device, groups)
        except RowError as err:
            print(f"{label}: error, {err}", file=sys.stderr)
            failed += 1
            continue
        # Only once the row is usable, so a bad line does not shadow a good one
        # later in the file, which is how unifi-tools-import-wifi.py reads too
        seen.add(mac)

        described = []
        if "name" in changes:
            described.append(f"name '{name}' -> '{changes['name']}'")
        if "config_network" in changes:
            described.append(
                describe_config(device.get("config_network"), changes["config_network"])
            )
        if wanted is not None:
            current_ids = sorted(
                g["_id"] for g in editable_groups(groups) if mac in (g.get("device_macs") or [])
            )
            if wanted != current_ids:
                names = {g["_id"]: g.get("name", g["_id"]) for g in groups}
                shown = LIST_SEPARATOR.join(names[i] for i in wanted) or "no groups"
                described.append(f"AP groups -> {shown}")
                wanted_by_mac[mac] = wanted
                group_notes[mac] = label

        if not described:
            print(f"{label}: unchanged")
            unchanged += 1
            continue

        print(f"{label}: {'would change' if args.dry_run else 'changing'} ({'; '.join(described)})")
        if changes and not args.dry_run:
            try:
                client.put_legacy(f"{DEVICE_REST_ENDPOINT}/{device['_id']}", changes)
            except UniFiError as err:
                print(f"{label}: error, {err}", file=sys.stderr)
                failed += 1
                wanted_by_mac.pop(mac, None)
                continue
        changed += 1

    # AP group membership lives on the group, not the device, so one group write
    # can settle several rows. These run last so a failed device write drops out.
    groups_changed = 0
    for group, macs in group_updates(wanted_by_mac, groups):
        note = f"AP group '{group.get('name')}': {len(macs)} AP(s)"
        if args.dry_run:
            print(f"{note} (would update)")
            groups_changed += 1
            continue
        body = {**group, "device_macs": macs}
        try:
            client.put_v2(f"{AP_GROUP_ENDPOINT}/{group['_id']}", body)
        except UniFiError as err:
            print(f"AP group '{group.get('name')}': error, {err}", file=sys.stderr)
            failed += 1
            continue
        print(f"{note} (updated)")
        groups_changed += 1

    totals = [
        f"{'Would change' if args.dry_run else 'Changed'} {changed}",
        f"unchanged {unchanged}",
        f"skipped {skipped}",
        f"failed {failed}",
    ]
    if groups_changed:
        totals.append(f"AP groups {'to update' if args.dry_run else 'updated'} {groups_changed}")
    print(", ".join(totals))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(main))
