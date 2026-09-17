# unifi-csv-tools

Export UniFi devices, clients, and WiFi networks from a UniFi OS console to timestamped CSVs, and push WiFi networks and device settings back from a CSV. Python 3.8+, no dependencies.

Works with UniFi OS consoles (UDM, UCG, UDR, Cloud Key Gen2+) using an API key.

## Commands

| Command | What it does |
|---|---|
| [`python unifi-tools-export-devices.py --host <console>`](#devices-and-clients) | Export devices (and clients with `--what both`) to CSV |
| [`python unifi-tools-export-wifi.py --host <console>`](#wifi-networks) | Export WiFi networks (SSIDs) to CSV |
| [`python unifi-tools-import-wifi.py <file.csv> --host <console>`](#importing) | Create WiFi networks from a CSV, or update them with `--update`; add `--dry-run` first |
| [`python unifi-tools-import-wifi.py --template`](#starting-from-a-template) | Write a starter CSV to fill in; no console needed |
| [`python unifi-tools-export-devices.py --host <console> --for-import`](#importing-devices) | Export devices in the shape the device import reads back |
| [`python unifi-tools-import-devices.py <file.csv> --host <console>`](#importing-devices) | Rename devices, set their IP, and set AP groups from a CSV; add `--dry-run` first |
| [`python unifi-tools-import-devices.py --template`](#importing-devices) | Write a starter device CSV to fill in; no console needed |

They all take `--host` (required, except for `--template`), `--api-key`, `--port`, `--site`, and `--verify-ssl`; add `--help` for the rest. `_unifi_tools_common.py` is shared code, not a command.

## Setup

Create an API key (Settings > Control Plane > Integrations) and put it in a `.env` file next to the scripts (all of them share it):

```
UNIFI_API_KEY=your-key-here
```

`.env` is git-ignored. A `UNIFI_API_KEY` environment variable, if set, takes precedence over the file, and `--api-key` takes precedence over both.

Note that a key passed with `--api-key` may be visible to other users in the process list and saved in shell history.

## Devices and clients

```bash
python unifi-tools-export-devices.py --host 192.168.1.1

# Export devices and clients, keep last 30 exports
python unifi-tools-export-devices.py --host 192.168.1.1 --what both --max-copies 30
```

| Option | Default | Description |
|---|---|---|
| `--host` | *(required)* | Console address |
| `--api-key` | `UNIFI_API_KEY` | API key; overrides the environment and `.env` |
| `--port` | `443` | HTTPS port |
| `--site` | `default` | Site short name (from the URL, not the display name) |
| `--what` | `devices` | `devices`, `clients`, or `both` |
| `--output-dir` | `exports/<type>` | Created if missing |
| `--max-copies` | `10` | Exports kept per site and type; `0` keeps all |
| `--uppercase-mac` | off | Write MAC addresses as `AA:BB:CC:DD:EE:FF` |
| `--for-import` | off | Write the columns `unifi-tools-import-devices.py` reads (devices only) |
| `--no-open` | off | Don't open the output folder when done |
| `--verify-ssl` | off | Enable if the console has a trusted certificate |

Defaults live in the constants at the top of `_unifi_tools_common.py` (shared by all scripts) and `unifi-tools-export-devices.py`.

## Output

`exports/<devices|clients>/unifi_<site>_<devices|clients>_YYYYMMDD_HHMMSS.csv`

- Columns: `Name, Hostname, MAC, IP, Model, Type, Version, Status, Serial, Adopted, Uptime, Last Seen` (set by `EXPORT_COLUMNS`).
- `last_seen` is local time as `YYYY-MM-DD HH:MM:SS`.
- For devices, `Status` translates the numeric `state` code (e.g. `Connected`); it is blank for clients.
- UTF-8 with BOM so Excel opens it correctly.

When the export finishes, the output folder (`--output-dir`, or `exports/<type>` by default; `exports` for `--what both`) opens in Explorer on Windows, Finder on macOS, or the default file manager on Linux.

Exit code is `0` on success and `1` on error, for use in cron / Task Scheduler. Add `--no-open` to scheduled runs so no window pops up.

## WiFi networks

`unifi-tools-export-wifi.py` exports the site's WiFi networks (SSIDs) using the official Network integration API, which needs Network 10.1 or later. It uses the same API key and takes the same connection and output options as `unifi-tools-export-devices.py`, plus `--raw`.

```bash
python unifi-tools-export-wifi.py --host 192.168.1.1

# Also save the raw API responses as JSON
python unifi-tools-export-wifi.py --host 192.168.1.1 --raw
```

Output: `exports/wifi/unifi_<site>_wifi_YYYYMMDD_HHMMSS.csv`, with columns `SSID Name, Password, Network, VLAN, Broadcasting APs, AP Groups, APs, Security, Band, Hidden, Enabled`.

- `Broadcasting APs` is `All`, `Group` (names in `AP Groups`), or `Specific` (AP names in `APs`).
- `Security` is the API's type, e.g. `WPA2_PERSONAL` or `OPEN`.
- **Passwords are written in plain text.** The `--raw` JSON also contains them. `exports/` is git-ignored, but treat these files as secrets.

### Starting from a template

You don't need to run an export first to learn the format. [`examples/wifi-template.csv`](examples/wifi-template.csv) is a filled-in starter, and `--template` writes a fresh copy wherever you want one:

```bash
python unifi-tools-import-wifi.py --template                 # writes wifi-template.csv here
python unifi-tools-import-wifi.py --template sites/acme.csv  # or to a path you choose
```

It has one row per pattern: the minimum of just a name and a password, a network by name, a network by VLAN ID, an AP group, named APs, an open network, and WPA3 on 5 and 6 GHz. Blank cells are the defaults listed below, so the first row is a plain WPA2 SSID on the default network.

Replace the rows with your own before importing. The example networks and AP groups (`IoT`, `Warehouse APs`, `AP-Lab-1`) are placeholders and will not exist on your site; a row naming something that isn't there is reported and skipped. `--template` never overwrites an existing file.

### Importing

`unifi-tools-import-wifi.py` creates SSIDs from a CSV in the same format. It takes the connection options (`--host`, `--api-key`, `--port`, `--site`, `--verify-ssl`) plus `--update` (below) and `--dry-run`, which prints each request (password hidden) without changing anything.

```bash
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --dry-run
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1

# Also change SSIDs that already exist to match the CSV
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --update --dry-run
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --update
```

- Only `SSID Name` is required. SSIDs whose name already exists are skipped, unless `--update` is given.
- Blank cells use defaults: no password = open network, no `Network`/`VLAN` = the default network, `Broadcasting APs` = All, `Security` = `WPA2_PERSONAL`, `Band` = 2.4 and 5 GHz, `Hidden` = No, `Enabled` = Yes.
- `Network` is matched by name, or by `VLAN` if the name is blank. `AP Groups` and `APs` are names separated by `;` and must match exactly one group or AP.
- `Security` is `OPEN`, `WPA2_PERSONAL`, `WPA3_PERSONAL`, or `WPA2_WPA3_PERSONAL`. The API's WPA2/WPA3 Enterprise types need a RADIUS profile that this CSV has no column for, so those rows are rejected with a note to create the SSID in UniFi; `Band` is any of 2.4, 5, and 6 GHz.
- New SSIDs take their other settings (band steering, client isolation, etc.) from `WIFI_DEFAULTS` in `unifi-tools-import-wifi.py`. `SECURITY_DEFAULTS` holds what each security type needs beyond the password and the API will not accept without: fast roaming, and for WPA3 the SAE timers and PMF mode.
- Band steering is dropped from the request when `Band` lists one frequency; the API rejects the setting outright on a single band.
- Rows with problems are reported and skipped; the exit code is `1` if any row failed.

#### Updating existing SSIDs

`--update` changes SSIDs that already exist instead of skipping them, and still creates the ones that don't. Export the site first, edit that CSV, and import it back with `--update`.

- SSIDs are matched by name, so `--update` cannot rename one; a renamed row creates a second SSID.
- Only the columns the CSV covers are changed. Everything else (band steering, client isolation, fast roaming, PMF, WPA3/SAE, and the rest) keeps its current value, so `WIFI_DEFAULTS` and `SECURITY_DEFAULTS` apply to new SSIDs only. Changing `Security` to a different type does reset the settings belonging to it.
- A blank cell is not "leave it alone", it is the default from the list above. A blank `Password` on an existing WPA network turns it into an open network.
- Rows already matching the console are reported as `unchanged` and not sent. Each changed SSID lists the fields being written, and `--dry-run` prints the full request first.
- The run is per row and not atomic: a failure partway through leaves the earlier rows changed.
- If the console answers `400 Unknown request body property '$.x'`, add `x` to `READ_ONLY_FIELDS` in `unifi-tools-import-wifi.py`: it is a field the API reports but will not accept back.

#### SSID limit per AP

UniFi rejects an SSID if any AP it would broadcast on already has the maximum (4 per radio on many models) with `too many WiFi broadcasts assigned to the device`. Disabled SSIDs count, offline adopted APs count, and a blank `Broadcasting APs` means **all** APs, so only a few "All" SSIDs fill every AP.

To stage more SSIDs before deployment, create one AP group per SSID in UniFi (groups can't be empty, so add any AP, using no AP in more than 4 groups), set `Broadcasting APs` to `Group` and `AP Groups` to that group's name, and import. Move the real APs into the groups when deploying.

## Importing devices

`unifi-tools-import-devices.py` renames adopted devices, switches their management IP between DHCP and static, and sets which AP groups an AP belongs to. Export first, edit the CSV, import it back:

```bash
python unifi-tools-export-devices.py --host 192.168.1.1 --for-import
python unifi-tools-import-devices.py exports/devices-import/unifi_default_devices-import_*.csv --host 192.168.1.1 --dry-run
python unifi-tools-import-devices.py exports/devices-import/unifi_default_devices-import_*.csv --host 192.168.1.1
```

`--for-import` writes `exports/devices-import/unifi_<site>_devices-import_YYYYMMDD_HHMMSS.csv` with columns `MAC, Name, Model, Type, IP, IP Mode, Static IP, Netmask, Gateway, DNS, AP Groups`. `--template` writes [`examples/device-template.csv`](examples/device-template.csv) instead, if you would rather start from an example — but its MACs are made up, so an export is the better start.

- **Rows are matched by MAC**, the only column that must be filled in. Device names repeat on a site (five APs called `AC Mesh` is normal), so a name is not a key. Any spelling of a MAC works: `aa:bb:...`, `AA-BB-...`, or `aabbccddeeff`.
- **A blank cell means "leave this alone"**, unlike the WiFi import where a blank cell is a default. A CSV can therefore hold just `MAC` and `Name` and nothing else will be touched.
- `Model`, `Type`, and `IP` are written by the export for you to read; the import ignores them. `IP` is the address the device is on now, which is not the same as `Static IP`: a device on DHCP keeps a stale `Static IP` in its config from whenever it was last pinned, and the import ignores that too.
- `IP Mode` is `DHCP` or `Static`. `Static` also needs `Static IP`, `Netmask`, and `Gateway`, and takes one or two `DNS` servers. A gateway outside the address's own subnet is rejected before anything is sent, as is a netmask that isn't one.
- `AP Groups` is `;` separated group names and replaces whatever groups that AP is in. `None` takes it out of every group, since blank means "leave it". The groups have to exist already — this script doesn't create them — and only APs can be in one. The automatic *All APs* group is maintained by the console and is refused.
- Devices whose row matches the console are reported as `unchanged` and nothing is sent. Rows with problems are reported and skipped, and the exit code is `1` if any row failed.
- The run is per row and not atomic: a failure partway through leaves the earlier rows changed. AP group writes happen last, because one group holds many APs.

**Changing `IP Mode` reprovisions the device.** It drops off the network for a moment and comes back at the new address. If the address, netmask, or gateway is wrong, the device is unreachable until it is factory reset — and if you do that to the console you are talking to, the run stops there. Use `--dry-run` first; it prints every change without sending anything.

This script cannot adopt, forget, or restart a device, and a MAC that is not adopted on the site is reported as an error rather than added.

### Why this one uses a different API

The other scripts use the official Network integration API. It can list devices, adopt one, restart one, and forget one — but it cannot rename a device, set its IP, or edit an AP group, so there is nothing there for this script to call. It uses the private API the UniFi UI itself uses instead (`/api/s/<site>/rest/device/<id>` and `/v2/api/site/<site>/apgroups/<id>`), which is undocumented and may change between Network releases. If a Network upgrade breaks it, that is why. The endpoints are named in one place, at the top of `_unifi_tools_common.py`.

The integration API's read-only `device-tags` are the same objects as AP groups, under a different name.

## Notes

- `clients` covers currently connected clients only.
- The console serves the integration API's OpenAPI spec at `https://<console>/proxy/network/api-docs/integration.json` (the same API key works). It is the reference for the request bodies these scripts build, and the browsable version is under Control Plane > UniFi API.
