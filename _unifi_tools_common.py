"""
_unifi_tools_common - Shared support code for the unifi-tools scripts.

Holds the connection defaults, API key lookup, API client, command-line
options, CSV/output helpers, and the WiFi CSV format shared by
unifi-tools-export-wifi.py and unifi-tools-import-wifi.py.
Standard library only, Python 3.8+.
"""

import csv
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (defaults can be overridden on the command line)
# ---------------------------------------------------------------------------
DEFAULT_PORT = 443
DEFAULT_SITE = "default"  # Site short name, not the display name
DEFAULT_OUTPUT_DIR = "exports"  # Each export type gets a subfolder, e.g. exports/wifi
DEFAULT_MAX_COPIES = 10  # 0 = keep every export
VERIFY_SSL = False  # Consoles ship with self-signed certs
OPEN_FOLDER = True  # Open the output folder in Explorer/Finder when done
REQUEST_TIMEOUT = 30  # Seconds

FILE_PREFIX = "unifi"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"  # Must sort chronologically
CSV_ENCODING = "utf-8-sig"  # BOM so Excel detects UTF-8
LIST_SEPARATOR = "; "  # Joins simple lists (e.g. tags) in one cell

# Unless --api-key is given, the API key is read from this environment variable,
# which may be set in a .env file next to these scripts. Real environment
# variables take precedence over .env.
ENV_API_KEY = "UNIFI_API_KEY"
ENV_FILE = Path(__file__).resolve().parent / ".env"

# Network application API, proxied through UniFi OS
API_PREFIX = "/proxy/network"
# Official Network integration API (Network 10.1+), relative to API_PREFIX
INTEGRATION_PREFIX = "/integration/v1"
PAGE_LIMIT = 200  # Largest page the integration API allows


# Integration API endpoints, relative to /integration/v1/sites/<siteId>/
WIFI_ENDPOINT = "wifi/broadcasts"
NETWORK_ENDPOINT = "networks"  # For network names and VLAN IDs
DEVICE_TAG_ENDPOINT = "device-tags"  # AP groups
DEVICE_ENDPOINT = "devices"  # For AP names

# WiFi CSV columns, in order, as row key -> header label.
WIFI_COLUMNS = {
    "name": "SSID Name",
    "password": "Password",
    "network": "Network",
    "vlan": "VLAN",
    "broadcasting_aps": "Broadcasting APs",
    "ap_groups": "AP Groups",
    "aps": "APs",
    "security": "Security",
    "band": "Band",
    "hidden": "Hidden",
    "enabled": "Enabled",
}

# broadcastingDeviceFilter "type" -> Broadcasting APs label; no filter = All
DEVICE_FILTER_TYPES = {
    "DEVICE_TAGS": "Group",
    "DEVICES": "Specific",
}


class UniFiError(Exception):
    """Raised for connection, authentication, or API failures."""


class UniFiClient:
    """Minimal API-key client for the UniFi Network API."""

    def __init__(
        self, host, port, site, api_key, verify_ssl=VERIFY_SSL, timeout=REQUEST_TIMEOUT
    ):
        self.base_url = f"https://{host}:{port}"
        self.site = site
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "X-API-KEY": api_key}
        self._site_id = None

        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

    def _request(self, path, method="GET", body=None):
        """Send a request with an optional JSON body; returns (status, JSON or None)."""
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except (urllib.error.URLError, OSError) as err:
            raise UniFiError(f"Cannot reach {self.base_url}: {err}") from err

        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None  # HTML or redirect page, not an API response
        return status, payload

    @staticmethod
    def _check_status(path, status):
        if status in (401, 403):
            raise UniFiError(
                f"Not authorized for {path} (HTTP {status}). Check the API key."
            )
        if status == 429:
            raise UniFiError("Rate limited by the console, wait and retry.")

    def get(self, endpoint):
        """Return the 'data' list for a site endpoint such as 'stat/device'."""
        path = f"{API_PREFIX}/api/s/{self.site}/{endpoint}"
        status, payload = self._request(path)
        self._check_status(path, status)
        if status != 200 or not isinstance(payload, dict):
            msg = (
                (payload or {}).get("meta", {}).get("msg", "")
                if isinstance(payload, dict)
                else ""
            )
            hint = " (check --site)" if "NoSiteContext" in msg else ""
            raise UniFiError(f"GET {path} failed: HTTP {status} {msg}{hint}".rstrip())
        return payload.get("data", [])

    def get_integration(self, endpoint):
        """GET an official integration API path such as 'sites'; returns the JSON."""
        path = f"{API_PREFIX}{INTEGRATION_PREFIX}/{endpoint}"
        status, payload = self._request(path)
        self._check_status(path, status)
        if status != 200 or payload is None:
            msg = payload.get("message", "") if isinstance(payload, dict) else ""
            raise UniFiError(f"GET {path} failed: HTTP {status} {msg}".rstrip())
        return payload

    def send_integration(self, endpoint, body, method="POST"):
        """Send a JSON body to an integration API path; returns the saved object."""
        path = f"{API_PREFIX}{INTEGRATION_PREFIX}/{endpoint}"
        status, payload = self._request(path, method, body)
        self._check_status(path, status)
        if status not in (200, 201, 204):
            if isinstance(payload, dict) and payload.get("message"):
                detail = payload["message"]
            else:
                detail = json.dumps(payload) if payload is not None else ""
            raise UniFiError(f"{method} {path} failed: HTTP {status} {detail}".rstrip())
        return payload

    def post_integration(self, endpoint, body):
        """POST a JSON body to an integration API path; returns the created object."""
        return self.send_integration(endpoint, body, "POST")

    def put_integration(self, endpoint, body):
        """PUT a JSON body to an integration API path; returns the updated object."""
        return self.send_integration(endpoint, body, "PUT")

    def list_integration(self, endpoint):
        """Return every item from a paginated integration API list endpoint."""
        items, offset = [], 0
        while True:
            query = urllib.parse.urlencode({"offset": offset, "limit": PAGE_LIMIT})
            page = self.get_integration(f"{endpoint}?{query}")
            data = page.get("data", []) if isinstance(page, dict) else []
            items.extend(data)
            offset += len(data)
            if not data or offset >= page.get("totalCount", 0):
                return items

    def site_id(self):
        """Return the integration API site UUID for --site (short name or UUID)."""
        if self._site_id is None:
            sites = self.list_integration("sites")
            for s in sites:
                if self.site in (s.get("internalReference"), s.get("id")):
                    self._site_id = s["id"]
                    break
            else:
                names = ", ".join(str(s.get("internalReference")) for s in sites)
                raise UniFiError(f"Site '{self.site}' not found (have: {names})")
        return self._site_id

    def list_site(self, endpoint):
        """Return every item from a site-scoped integration list, e.g. 'devices'."""
        return self.list_integration(f"sites/{self.site_id()}/{endpoint}")

    def get_site(self, endpoint):
        """GET a single site-scoped integration resource, e.g. 'wifi/broadcasts/<id>'."""
        return self.get_integration(f"sites/{self.site_id()}/{endpoint}")

    def post_site(self, endpoint, body):
        """POST to a site-scoped integration endpoint, e.g. 'wifi/broadcasts'."""
        return self.post_integration(f"sites/{self.site_id()}/{endpoint}", body)

    def put_site(self, endpoint, body):
        """PUT to a site-scoped resource, e.g. 'wifi/broadcasts/<id>'."""
        return self.put_integration(f"sites/{self.site_id()}/{endpoint}", body)


def load_env_file(path):
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def add_common_args(parser):
    """Add the connection and output options shared by the export scripts."""
    add_connection_args(parser)
    add_output_args(parser)


def add_connection_args(parser):
    """Add the console connection options shared by all scripts."""
    parser.epilog = (
        f"The API key is taken from --api-key, else {ENV_API_KEY} "
        "(environment or .env file)."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Console address, e.g. 192.168.1.1",
    )
    parser.add_argument(
        "--api-key",
        help=f"API key (default: {ENV_API_KEY} from environment or .env)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTPS port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--site",
        default=DEFAULT_SITE,
        help=f"Site short name (default: {DEFAULT_SITE})",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        default=VERIFY_SSL,
        help="Verify the TLS certificate",
    )


def add_output_args(parser):
    """Add the export output options."""
    parser.add_argument(
        "--output-dir",
        help=f"Output folder (default: {DEFAULT_OUTPUT_DIR}/<type>, e.g. "
        f"{DEFAULT_OUTPUT_DIR}/wifi)",
    )
    parser.add_argument(
        "--max-copies",
        type=int,
        default=DEFAULT_MAX_COPIES,
        help=f"Exports to keep per type, 0 = unlimited (default: {DEFAULT_MAX_COPIES})",
    )
    parser.add_argument(
        "--no-open",
        dest="open_folder",
        action="store_false",
        default=OPEN_FOLDER,
        help="Don't open the output folder when done (e.g. for scheduled runs)",
    )


def get_api_key(args):
    """Return the API key from --api-key, the environment, or .env; None if unset."""
    api_key = (args.api_key or "").strip()
    if not api_key:
        load_env_file(ENV_FILE)
        api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        print(
            f"Error: no API key. Use --api-key or set {ENV_API_KEY} "
            f"in the environment or {ENV_FILE}.",
            file=sys.stderr,
        )
        return None
    return api_key


def connect(args):
    """Build a UniFiClient from parsed common args; None if there is no API key."""
    api_key = get_api_key(args)
    if not api_key:
        return None
    return UniFiClient(args.host, args.port, args.site, api_key, args.verify_ssl)


def export_dir(args, kind):
    """Folder for an export type: --output-dir if given, else exports/<kind>."""
    if args.output_dir:
        return Path(args.output_dir)
    return Path(DEFAULT_OUTPUT_DIR) / kind


def new_timestamp():
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def export_stem(site, kind):
    """Filename stem for an export, e.g. unifi_default_devices."""
    return f"{FILE_PREFIX}_{safe_name(site)}_{kind}"


def read_csv(path, columns):
    """Read a CSV written by write_csv; returns dicts keyed by row key, not label."""
    keys_by_label = {label.strip().lower(): key for key, label in columns.items()}
    with open(path, newline="", encoding=CSV_ENCODING) as fh:
        reader = csv.DictReader(fh)
        missing = [
            label
            for label in columns.values()
            if label.lower() not in {h.strip().lower() for h in reader.fieldnames or []}
        ]
        rows = []
        for record in reader:
            row = {key: "" for key in columns}
            for header, value in record.items():
                key = keys_by_label.get((header or "").strip().lower())
                if key:
                    row[key] = (value or "").strip()
            rows.append(row)
    return rows, missing


def write_csv(rows, path, columns):
    """Write rows to path; columns maps row keys to header labels, in order."""
    with open(path, "w", newline="", encoding=CSV_ENCODING) as fh:
        writer = csv.DictWriter(
            fh, fieldnames=columns, restval="", extrasaction="ignore"
        )
        writer.writerow(columns)
        writer.writerows(rows)


def prune_old_copies(output_dir, stem, max_copies):
    """Delete the oldest exports matching stem so only max_copies remain."""
    if max_copies <= 0:
        return []
    files = sorted(output_dir.glob(f"{stem}_*.csv"), key=lambda p: p.name, reverse=True)
    removed = []
    for old in files[max_copies:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError as err:
            print(f"Warning: could not delete {old}: {err}", file=sys.stderr)
    return removed


def save_export(rows, columns, output_dir, stem, timestamp, max_copies):
    """Write a timestamped CSV, prune old copies, and return its path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stem}_{timestamp}.csv"
    write_csv(rows, path, columns)
    for old in prune_old_copies(output_dir, stem, max_copies):
        print(f"Removed old export {old}")
    return path


def open_folder(path):
    """Open a folder in Explorer, Finder, or the Linux default file manager."""
    path = Path(path).resolve()
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as err:
        print(f"Warning: could not open {path}: {err}", file=sys.stderr)


def safe_name(text):
    """Make a string safe for use in a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)


def run(main):
    """Run a script's main() with the shared error handling and exit code."""
    try:
        return main()
    except UniFiError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    print(
        """This file is shared support code and does nothing on its own.
Run one of these instead (add --help for options):

  unifi-tools-export-devices.py   Export devices and/or clients to CSV
  unifi-tools-export-wifi.py      Export WiFi networks (SSIDs) to CSV
  unifi-tools-import-wifi.py      Create or update WiFi networks from a CSV

Example: python unifi-tools-export-wifi.py --host 192.168.1.1"""
    )
