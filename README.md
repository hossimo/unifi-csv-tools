# unifi-csv-tools

Export UniFi devices, clients, and WiFi networks from a UniFi OS console to timestamped CSVs, and create WiFi networks back from a CSV. Python 3.8+, no dependencies.

Works with UniFi OS consoles (UDM, UCG, UDR, Cloud Key Gen2+) using an API key.

## Commands

| Command | What it does |
|---|---|
| [`python unifi-tools-export-devices.py --host <console>`](#devices-and-clients) | Export devices (and clients with `--what both`) to CSV |
| [`python unifi-tools-export-wifi.py --host <console>`](#wifi-networks) | Export WiFi networks (SSIDs) to CSV |
| [`python unifi-tools-import-wifi.py <file.csv> --host <console>`](#importing) | Create WiFi networks from a CSV; add `--dry-run` first |

All three take `--host` (required), `--api-key`, `--port`, `--site`, and `--verify-ssl`; add `--help` for the rest. `_unifi_tools_common.py` is shared code, not a command.

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

### Importing

`unifi-tools-import-wifi.py` creates SSIDs from a CSV in the same format. It takes the connection options (`--host`, `--api-key`, `--port`, `--site`, `--verify-ssl`) plus `--dry-run`, which prints each request (password hidden) without changing anything.

```bash
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1 --dry-run
python unifi-tools-import-wifi.py wifi.csv --host 192.168.1.1
```

- Only `SSID Name` is required. SSIDs whose name already exists are skipped, never changed.
- Blank cells use defaults: no password = open network, no `Network`/`VLAN` = the default network, `Broadcasting APs` = All, `Security` = `WPA2_PERSONAL`, `Band` = 2.4 and 5 GHz, `Hidden` = No, `Enabled` = Yes.
- `Network` is matched by name, or by `VLAN` if the name is blank. `AP Groups` and `APs` are names separated by `;` and must match exactly one group or AP.
- Other settings (band steering, client isolation, etc.) come from `WIFI_DEFAULTS` in `unifi-tools-import-wifi.py`.
- Rows with problems are reported and skipped; the exit code is `1` if any row failed.

#### SSID limit per AP

UniFi rejects an SSID if any AP it would broadcast on already has the maximum (4 per radio on many models) with `too many WiFi broadcasts assigned to the device`. Disabled SSIDs count, offline adopted APs count, and a blank `Broadcasting APs` means **all** APs, so only a few "All" SSIDs fill every AP.

To stage more SSIDs before deployment, create one AP group per SSID in UniFi (groups can't be empty, so add any AP, using no AP in more than 4 groups), set `Broadcasting APs` to `Group` and `AP Groups` to that group's name, and import. Move the real APs into the groups when deploying.

## Notes

- `clients` covers currently connected clients only.
