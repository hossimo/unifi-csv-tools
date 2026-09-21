#!/usr/bin/env python3
"""
unifi-tools-export-protect - Export UniFi Protect devices and settings to CSV.

Authenticates to a UniFi OS console (UDM, UCG, UDR, Cloud Key Gen2+) with an
API key passed via --api-key or read from the environment or .env. Shared code
lives in _unifi_tools_common.py. Standard library only, Python 3.8+.

Protect is a separate application behind the same console and grants API access
separately from Network, so a key that reads devices can still be refused here.
It also only serves its integration API to an API key: the private API that the
Protect UI uses answers 401, so there is no way to reach a camera's firmware
version, IP address, or recording settings. What the integration API exposes is
what this exports.

Each run writes one timestamped folder, holding one CSV per kind that has
anything on the console:
    cameras.csv, sensors.csv, chimes.csv, ...   one per device kind
    liveviews.csv, arm-profiles.csv             the saved layouts and alarm profiles
    users.csv                                   both user endpoints together

A kind gets its own file so that its columns are its own: every kind carries the
same envelope - name, model, state, MAC, id - and adds fields that mean nothing
to any other kind, so one shared file would be mostly empty cells.

--what picks what to export:
    devices       every Protect device kind (default)
    config        liveviews and arm profiles
    users         Protect users, which hold names and email addresses
    all           all three of the above
    <kind>        one kind on its own, e.g. cameras

Users are left out of "devices" on purpose: an inventory export should not
quietly collect people's email addresses. Ask for them by name.

Examples:
    python unifi-tools-export-protect.py --host 192.168.1.1
    python unifi-tools-export-protect.py --host 192.168.1.1 --what cameras
    python unifi-tools-export-protect.py --host 192.168.1.1 --what all
"""

import argparse
import re
import sys
from datetime import datetime

from _unifi_tools_common import (
    LIST_SEPARATOR,
    UniFiError,
    UniFiUnreachable,
    add_common_args,
    connect,
    export_dir,
    open_folder,
    prune_old_dirs,
    resolve_profile,
    run,
    write_csv,
)

DEFAULT_WHAT = "devices"
UPPERCASE_MAC = False  # True writes MACs as AA:BB:CC:DD:EE:FF
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # Timestamps inside a CSV, in local time
EXPORT_PREFIX = "protect"  # Folder under exports/, e.g. exports/protect/
# One folder per run. Readable and still sorts chronologically; a colon is not
# allowed in a Windows path, so the time runs together.
FOLDER_FORMAT = "%Y-%m-%d_%H%M%S"

# Protect integration endpoints, relative to /integration/v1/, as kind -> path.
# Every one is a GET that takes no parameters. The console's own spec is at
# /proxy/protect/api-docs/integration.json but it wants a browser session
# rather than an API key, so it is read from Control Plane > UniFi API.
ENDPOINTS = {
    "cameras": "cameras",
    "lights": "lights",
    "sensors": "sensors",
    "chimes": "chimes",
    "viewers": "viewers",
    "speakers": "speakers",
    "sirens": "sirens",
    "fobs": "fobs",
    "relays": "relays",
    "bridges": "bridges",
    "link-stations": "link-stations",
    "alarm-hubs": "alarm-hubs",
    "nvr": "nvrs",  # The console itself, returned as an object rather than a list
    "liveviews": "liveviews",
    "arm-profiles": "arm-profiles",
    "users": "users",
    "ulp-users": "ulp-users",
}

# Kinds that are hardware, config, and people. --what takes a group or one kind.
DEVICE_KINDS = (
    "cameras", "lights", "sensors", "chimes", "viewers", "speakers", "sirens",
    "fobs", "relays", "bridges", "link-stations", "alarm-hubs", "nvr",
)
CONFIG_KINDS = ("liveviews", "arm-profiles")
USER_KINDS = ("users", "ulp-users")
GROUPS = {
    "devices": DEVICE_KINDS,
    "config": CONFIG_KINDS,
    "users": USER_KINDS,
    "all": DEVICE_KINDS + CONFIG_KINDS + USER_KINDS,
}

# The envelope every Protect device carries, whatever kind it is.
DEVICE_BASE = {"name": "Name", "type": "Model", "state": "Status", "mac": "MAC"}

# The NVR is the console itself rather than something attached to it, so it has
# no connection state and would otherwise carry an always-empty Status column.
NO_STATE_KINDS = frozenset({"nvr"})

# The fields each kind adds over the envelope, as a dotted path -> label, which
# become that kind's columns after the envelope. cameras, ring_settings,
# liveview_name and camera are not API paths: derive() fills those in.
DETAILS = {
    "cameras": {
        "videoMode": "Video Mode",
        "hdrType": "HDR",
        "isMicEnabled": "Mic Enabled",
        "micVolume": "Mic Volume",
        "smartDetectSettings.objectTypes": "Smart Detections",
        "smartDetectSettings.audioTypes": "Audio Detections",
        "osdSettings.isNameEnabled": "OSD Name",
        "osdSettings.isDateEnabled": "OSD Date",
        "ledSettings.isEnabled": "Status LED",
        "hasPackageCamera": "Package Camera",
    },
    "lights": {
        "isLightOn": "Light On",
        "isDark": "Is Dark",
        "isPirMotionDetected": "Motion Detected",
        "isLightForceEnabled": "Force On",
        "camera": "Paired Camera",
        "lastMotion": "Last Motion",
    },
    "sensors": {
        "mountType": "Mount Type",
        "batteryStatus.percentage": "Battery %",
        "batteryStatus.isLow": "Battery Low",
        "motionSettings.isEnabled": "Motion",
        "temperatureSettings.isEnabled": "Temperature",
        "humiditySettings.isEnabled": "Humidity",
        "leakSettings.isInternalEnabled": "Leak",
        "glassBreakSettings.isEnabled": "Glass Break",
        "alarmSettings.isEnabled": "Alarm",
        "scheduleMode": "Schedule Mode",
    },
    "chimes": {"cameras": "Cameras", "ring_settings": "Ring Settings"},
    "viewers": {"liveview_name": "Liveview", "streamLimit": "Stream Limit"},
    "speakers": {
        "volume": "Volume",
        "micVolume": "Mic Volume",
        "isMicEnabled": "Mic Enabled",
    },
    "sirens": {"volume": "Volume", "connectionType": "Connection"},
    "fobs": {"awayState": "Away State", "buttonLabels": "Buttons"},
    "relays": {},
    "bridges": {"platform": "Platform", "maxClients": "Max Clients"},
    "link-stations": {"isAlarmHub": "Alarm Hub"},
    "alarm-hubs": {"isAlarmHub": "Alarm Hub"},
    "nvr": {
        "armMode.status": "Arm Status",
        "armMode.armProfileId": "Arm Profile",
        "doorbellSettings.defaultMessageText": "Doorbell Message",
    },
}

# The kinds that are not devices, as kind -> its own columns.
OTHER_COLUMNS = {
    "liveviews": {
        "name": "Name",
        "isDefault": "Default",
        "isGlobal": "Shared",
        "layout": "Layout",
        "cameras": "Cameras",
        "id": "ID",
    },
    "arm-profiles": {
        "name": "Name",
        "recordEverything": "Record Everything",
        "automations": "Automations",
        "createdAt": "Created",
        "updatedAt": "Updated",
        "id": "ID",
    },
}

# users.csv holds both user endpoints. They describe the same people from two
# directions - Protect's own accounts and the UniFi identity ones - so Source
# says which produced a row rather than leaving two near-identical files.
USER_COLUMNS = {
    "source": "Source",
    "name": "Name",
    "firstName": "First Name",
    "lastName": "Last Name",
    "email": "Email",
    "status": "Status",
    "id": "ID",
}

# Fields holding an epoch-milliseconds timestamp, which is what Protect sends.
TIMESTAMP_FIELDS = frozenset({"lastMotion", "createdAt", "updatedAt"})

# Kinds needing another endpoint to turn an id into a name, as the lookup ->
# the kinds wanting it. name_lookups() fetches only the ones called for.
LOOKUPS = {
    "cameras": frozenset({"chimes", "liveviews", "lights"}),
    "liveviews": frozenset({"viewers"}),
    "arm-profiles": frozenset({"nvr"}),
}


def pick(record, path):
    """Return a dotted path's value from a nested record; blank if it is missing."""
    value = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    if isinstance(value, list):
        return LIST_SEPARATOR.join(str(v) for v in value)
    return "" if value is None else value


def format_mac(value, uppercase=UPPERCASE_MAC):
    """Protect writes MACs as AABBCCDDEEFF; match the device export's aa:bb:cc."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    if len(text) != 12:
        return str(value or "")  # Not a MAC; pass it through rather than mangle it
    mac = ":".join(text[i : i + 2] for i in range(0, 12, 2))
    return mac.upper() if uppercase else mac.lower()


def format_timestamp(value):
    """Convert epoch milliseconds to DATETIME_FORMAT; blank if missing/invalid."""
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime(DATETIME_FORMAT)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def derive(record, kind, names):
    """Fields that need another endpoint to mean anything, e.g. an id -> a name.

    names holds {"cameras": {id: name}, ...}. An id with no match is left as
    the id rather than blanked, so nothing goes missing when a camera is
    removed between the two calls.
    """
    cameras = names.get("cameras", {})
    if kind == "chimes":
        rings = [
            f"{cameras.get(r.get('cameraId'), r.get('cameraId'))}: "
            f"volume {r.get('volume')}, repeat {r.get('repeatTimes')}"
            for r in record.get("ringSettings") or []
        ]
        return {
            "cameras": LIST_SEPARATOR.join(
                cameras.get(i, i) for i in record.get("cameraIds") or []
            ),
            "ring_settings": LIST_SEPARATOR.join(rings),
        }
    if kind == "liveviews":
        return {
            "cameras": LIST_SEPARATOR.join(
                cameras.get(cid, cid)
                for slot in record.get("slots") or []
                for cid in slot.get("cameras") or []
            )
        }
    if kind == "lights":
        camera_id = record.get("camera")
        return {"camera": cameras.get(camera_id, camera_id or "")}
    if kind == "viewers":
        liveview_id = record.get("liveview")
        return {
            "liveview_name": names.get("liveviews", {}).get(
                liveview_id, liveview_id or ""
            )
        }
    if kind == "nvr":
        profile_id = pick(record, "armMode.armProfileId")
        return {
            "armMode.armProfileId": names.get("arm-profiles", {}).get(
                profile_id, profile_id
            )
        }
    return {}


def device_columns(kind):
    """Columns for one kind's CSV: the envelope, the fields it adds, then ID."""
    columns = {
        key: label
        for key, label in DEVICE_BASE.items()
        if not (key == "state" and kind in NO_STATE_KINDS)
    }
    columns.update(DETAILS.get(kind, {}))
    columns["id"] = "ID"
    return columns


def kind_fields(record, kind, names):
    """Return the kind's own fields as {path: value}, blanks and all removed."""
    derived = derive(record, kind, names)
    fields = {}
    for path in DETAILS.get(kind, {}):
        value = derived[path] if path in derived else pick(record, path)
        if path in TIMESTAMP_FIELDS:
            value = format_timestamp(record.get(path))
        if value != "":
            fields[path] = value
    return fields


def build_device_rows(records, kind, names, uppercase_mac=UPPERCASE_MAC):
    """Rows for one kind's CSV: the shared envelope plus the fields it adds."""
    rows = []
    for record in records:
        row = {
            "name": pick(record, "name"),
            "type": pick(record, "type"),
            "state": pick(record, "state"),
            "mac": format_mac(pick(record, "mac"), uppercase_mac),
            "id": pick(record, "id"),
        }
        row.update(kind_fields(record, kind, names))
        rows.append(row)
    return rows


def build_other_rows(records, kind, names):
    """Rows for liveviews.csv and arm-profiles.csv."""
    columns = OTHER_COLUMNS[kind]
    rows = []
    for record in records:
        row = {key: pick(record, key) for key in columns}
        row.update(derive(record, kind, names))
        for field in TIMESTAMP_FIELDS & set(columns):
            row[field] = format_timestamp(record.get(field))
        rows.append(row)
    return rows


def build_user_rows(records, kind):
    """Rows for users.csv. Both endpoints answer here, tagged by Source."""
    rows = []
    for record in records:
        rows.append(
            {
                "source": kind,
                # /users calls it name, /ulp-users calls it fullName
                "name": pick(record, "name") or pick(record, "fullName"),
                "firstName": pick(record, "firstName"),
                "lastName": pick(record, "lastName"),
                "email": pick(record, "email"),
                "status": pick(record, "status"),
                "id": pick(record, "id"),
            }
        )
    return rows


def fetch(client, kind):
    """Return a kind's records as a list. /nvrs answers with one object, not a list."""
    payload = client.get_protect(ENDPOINTS[kind])
    if isinstance(payload, list):
        return payload
    return [payload] if isinstance(payload, dict) else []


def fetch_all(client, kinds):
    """Fetch every selected kind; returns ({kind: records}, empty, skipped)."""
    records, empty = {}, []
    for kind in kinds:
        try:
            found = fetch(client, kind)
        except UniFiUnreachable:
            raise
        except UniFiError as err:
            # One missing endpoint should not cost the whole export; an older
            # Protect serves fewer of them than this script knows about.
            print(f"Warning: skipping {kind}: {err}", file=sys.stderr)
            continue
        if found:
            records[kind] = found
        else:
            empty.append(kind)
    return records, empty


def name_lookups(client, kinds):
    """Build the {id: name} maps the selected kinds need, fetching only those.

    A lookup that cannot be fetched leaves the ids in place rather than
    stopping the export, which is what an older Protect without one of these
    endpoints would otherwise do.
    """
    names = {}
    for source, wanted_by in LOOKUPS.items():
        if not set(kinds) & wanted_by:
            continue
        try:
            names[source] = {
                item["id"]: item.get("name") or item["id"]
                for item in fetch(client, source)
                if item.get("id")
            }
        except UniFiUnreachable:
            raise
        except UniFiError as err:
            print(f"Warning: cannot resolve {source} names: {err}", file=sys.stderr)
    return names


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export UniFi Protect devices and settings to a timestamped folder."
    )
    add_common_args(parser)
    parser.add_argument(
        "--what",
        choices=sorted(set(GROUPS) | set(ENDPOINTS)),
        default=DEFAULT_WHAT,
        help=f"What to export: a group (devices, config, users, all) or one "
        f"kind (default: {DEFAULT_WHAT}). 'devices' leaves out users, which "
        f"hold names and email addresses; ask for those by name",
    )
    parser.add_argument(
        "--uppercase-mac",
        action="store_true",
        default=UPPERCASE_MAC,
        help="Write MAC addresses in uppercase",
    )
    args = parser.parse_args()
    # --site is a Network idea; Protect is one application per console. Only
    # the option is refused: a profile's site is there for the other scripts.
    if args.site is not None:
        parser.error("Protect has no sites, so --site does not apply here")
    return resolve_profile(args, parser)


def write(folder, filename, rows, columns):
    """Write one CSV into the run's folder and say so."""
    path = folder / filename
    write_csv(rows, path, columns)
    print(f"Wrote {len(rows)} {path.stem} to {path}")


def main():
    args = parse_args()
    client = connect(args)
    if not client:
        return 1

    kinds = GROUPS.get(args.what, (args.what,))
    names = name_lookups(client, kinds)
    records, empty = fetch_all(client, kinds)
    if not records:
        if empty:
            print(f"Nothing on this console for: {', '.join(empty)}")
        print("Nothing to export.")
        return 0

    parent = export_dir(args, EXPORT_PREFIX)
    folder = parent / datetime.now().strftime(FOLDER_FORMAT)
    folder.mkdir(parents=True, exist_ok=True)

    for kind in DEVICE_KINDS:
        if kind in records:
            write(
                folder,
                f"{kind}.csv",
                build_device_rows(records[kind], kind, names, args.uppercase_mac),
                device_columns(kind),
            )

    for kind in OTHER_COLUMNS:
        if kind in records:
            write(
                folder,
                f"{kind}.csv",
                build_other_rows(records[kind], kind, names),
                OTHER_COLUMNS[kind],
            )

    user_rows = []
    for kind in USER_KINDS:
        if kind in records:
            user_rows += build_user_rows(records[kind], kind)
    if user_rows:
        write(folder, "users.csv", user_rows, USER_COLUMNS)

    if empty:
        print(f"Nothing on this console for: {', '.join(empty)}")
    for old in prune_old_dirs(parent, args.max_copies):
        print(f"Removed old export {old}")
    if args.open_folder:
        open_folder(folder)
    return 0


if __name__ == "__main__":
    sys.exit(run(main))
