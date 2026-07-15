import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values, load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SITE_CODES_FILE = BASE_DIR / "site_codes.env"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_CONFIG_FILE = BASE_DIR / "upload_config.json"
REQUEST_TIMEOUT = 30

load_dotenv(BASE_DIR / ".env")
os.makedirs(OUTPUT_DIR, exist_ok=True)

API_URL = os.getenv("API_URL", "").strip()
API_KEY = os.getenv("MIST_API_KEY", "").strip()

missing_settings = [
    name
    for name, value in {"API_URL": API_URL, "MIST_API_KEY": API_KEY}.items()
    if not value
]
if missing_settings:
    raise RuntimeError(
        f"Missing required value(s) in .env: {', '.join(missing_settings)}"
    )

def get_sites():
    if not SITE_CODES_FILE.exists():
        raise RuntimeError(
            "site_codes.env was not found. Run get_site_ids.py to create it."
        )

    site_codes = dotenv_values(SITE_CODES_FILE)
    sites = []
    for key, site_id in site_codes.items():
        if not key.startswith("site_") or not site_id:
            continue
        site_name = key.removeprefix("site_").replace("_", " ")
        sites.append({"name": site_name, "id": site_id})

    if not sites:
        raise RuntimeError(
            "site_codes.env contains no valid site_<name>=<site ID> entries."
        )

    return sorted(sites, key=lambda site: site["name"].casefold())

def get_devices(site_id):
    url = f"{API_URL.rstrip('/')}/api/v1/sites/{site_id}/devices"
    headers = {"Authorization": f"Token {API_KEY}"}
    all_switches = []
    page = 1
    while True:
        params = {
            "type": "switch",
            "limit": 300,
            "page": page
        }
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        switches = response.json()
        if not switches:
            break
        all_switches.extend(switches)
        page += 1
    return all_switches

def get_device_info(site_id, device_id):
    url = f"{API_URL.rstrip('/')}/api/v1/sites/{site_id}/devices/{device_id}"
    headers = {"Authorization": f"Token {API_KEY}"}
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def save_device_config(device_info, used_filenames=None):
    device_name = str(device_info.get("name") or "unknown_device")
    invalid_filename_chars = '<>:"/\\|?*'
    safe_name = "".join(
        "_" if char in invalid_filename_chars or ord(char) < 32 else char
        for char in device_name
    ).strip(". ")
    safe_name = safe_name or "unknown_device"

    filename = f"{safe_name}.log"
    if used_filenames is not None:
        filename_key = filename.casefold()
        if filename_key in used_filenames:
            device_id = str(device_info.get("id") or "unknown")
            filename = f"{safe_name}_{device_id}.log"
            filename_key = filename.casefold()
        used_filenames.add(filename_key)

    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as output_file:
        json.dump(device_info, output_file, indent=4)
    return filepath

def export_all_switch_configs(sites):
    saved_count = 0
    failed_count = 0
    used_filenames = set()

    print("\nCollecting configurations for all switches...")
    for site in sites:
        site_id = site.get("id")
        site_name = site.get("name", "Unnamed site")
        print(f"\nSite: {site_name}")

        if not site_id:
            print("  Skipped: site has no ID")
            failed_count += 1
            continue

        try:
            switches = get_devices(site_id)
        except Exception as e:
            print(f"  Failed to fetch switches: {e}")
            failed_count += 1
            continue

        if not switches:
            print("  No switches found")
            continue

        for switch in switches:
            device_id = switch.get("id")
            switch_name = switch.get("name", "Unnamed switch")
            if not device_id:
                print(f"  Skipped {switch_name}: switch has no ID")
                failed_count += 1
                continue

            try:
                device_info = get_device_info(site_id, device_id)
                filepath = save_device_config(device_info, used_filenames)
                print(f"  Saved {switch_name} to {filepath}")
                saved_count += 1
            except Exception as e:
                print(f"  Failed to save {switch_name}: {e}")
                failed_count += 1

    print(
        f"\nExport complete: {saved_count} saved, "
        f"{failed_count} failed."
    )

# --- Wired-client inventory export -------------------------------------------
# Uses the Mist wired_clients/search endpoint - the data behind the portal's
# Wired Clients page (client MAC, IP where learned, VLAN, port, hostname).
# NOTE: stats/ports/search returns switch PORT stats, not clients, and carries
# no client IP/MAC - do not use it for this.

WIRED_CLIENT_COLUMNS = [
    "client_mac", "ip", "device_mac", "device_name", "port_id", "fpc",
    "vlan", "manufacture", "dhcp_hostname", "last_seen_utc",
]


def _first(value):
    """First non-empty item if value is a list, else the value (never None)."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value if value is not None else ""


def _normalise_mac(mac):
    """Lower-case, strip separators: 'D4:99:6C-AA' -> 'd4996caa'."""
    return str(mac or "").lower().replace(":", "").replace("-", "")


def _safe_component(name):
    """Make a string safe for use inside a filename."""
    invalid = '<>:"/\\|?*'
    cleaned = "".join(
        "_" if char in invalid or ord(char) < 32 else char for char in str(name)
    )
    cleaned = "_".join(cleaned.split())  # collapse whitespace
    return cleaned.strip("._") or "unknown"


def derive_fpc(port_id):
    """Juniper interface -> FPC (stack member) number: 'ge-3/0/45' -> '3'.

    Aggregated / non-physical interfaces (e.g. 'ae0') have no FPC -> ''.
    """
    match = re.match(r"^[a-z]+-(\d+)/", str(port_id or ""))
    return match.group(1) if match else ""


def get_wired_clients(site_id, start_epoch, end_epoch):
    """Return every wired-client row for a site over [start_epoch, end_epoch].

    Follows the response 'next' cursor (search_after paging) until exhausted.
    """
    headers = {"Authorization": f"Token {API_KEY}"}
    base = f"{API_URL.rstrip('/')}/api/v1"
    path = f"/sites/{site_id}/wired_clients/search"
    params = {"start": int(start_epoch), "end": int(end_epoch), "limit": 1000}

    clients = []
    while True:
        response = requests.get(
            f"{base}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        clients.extend(payload.get("results", []))

        next_cursor = payload.get("next")
        if not next_cursor:
            break
        # 'next' is a full '/api/v1/...' path carrying its own query string.
        path, params = next_cursor.replace("/api/v1", "", 1), {}

    return clients


def wired_client_to_row(client, device_name):
    """Map one wired_clients/search record onto the CSV columns."""
    port_id = client.get("last_port_id") or _first(client.get("port_id"))
    ip = client.get("last_ip") or _first(client.get("ip"))
    hostname = client.get("last_hostname") or _first(client.get("hostname"))
    vlan = client.get("last_vlan")
    timestamp = client.get("timestamp")
    last_seen = (
        datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if timestamp
        else ""
    )
    return {
        "client_mac": client.get("mac", ""),
        "ip": ip or "",
        "device_mac": client.get("last_device_mac", ""),
        "device_name": device_name,
        "port_id": port_id or "",
        "fpc": derive_fpc(port_id),
        "vlan": "" if vlan is None else vlan,
        "manufacture": client.get("manufacture", ""),
        "dhcp_hostname": hostname or "",
        "last_seen_utc": last_seen,
    }


def write_wired_clients_csv(site_name, scope_name, rows):
    """Write mapped rows to outputs/<site>_<scope>_wired_clients_<stamp>.csv."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    filename = (
        f"{_safe_component(site_name)}_{_safe_component(scope_name)}"
        f"_wired_clients_{stamp}.csv"
    )
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=WIRED_CLIENT_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    return filepath


def _prompt_lookback_days(default=1):
    """Ask for a lookback window in days; blank/invalid falls back to default."""
    raw = input(f"Lookback window in days [default {default}]: ").strip()
    if not raw:
        return default
    try:
        days = int(raw)
        if days < 1:
            raise ValueError
        return days
    except ValueError:
        print(f"Invalid number of days; using {default}.")
        return default


def export_wired_clients_for_device(site, device):
    """Pull wired clients for one switch/stack and write them to CSV."""
    site_id = site.get("id")
    site_name = site.get("name", "Unnamed site")
    device_name = device.get("name") or "unknown_device"

    device_mac = _normalise_mac(device.get("mac"))
    if not device_mac:
        # Device list didn't carry a MAC; fall back to a device-info lookup.
        try:
            info = get_device_info(site_id, device["id"])
            device_mac = _normalise_mac(info.get("mac"))
        except Exception as error:
            print(f"Could not determine the switch MAC: {error}")
            return
    if not device_mac:
        print("This device has no MAC address; cannot scope the client search.")
        return

    days = _prompt_lookback_days()
    end_epoch = int(time.time())
    start_epoch = end_epoch - days * 86400

    print(f"\nFetching wired clients for '{device_name}' (last {days} day(s))...")
    try:
        all_clients = get_wired_clients(site_id, start_epoch, end_epoch)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "?"
        if status == 429:
            print("Rate limited by Mist (HTTP 429). Wait a moment and try again.")
        else:
            print(f"Mist API error fetching wired clients (HTTP {status}).")
        return
    except Exception as error:
        print(f"Failed to fetch wired clients: {error}")
        return

    rows = [
        wired_client_to_row(client, device_name)
        for client in all_clients
        if _normalise_mac(client.get("last_device_mac")) == device_mac
    ]
    rows.sort(key=lambda row: (row["fpc"], row["port_id"], row["client_mac"]))

    filepath = write_wired_clients_csv(site_name, device_name, rows)
    if rows:
        with_ip = sum(1 for row in rows if row["ip"])
        print(
            f"Saved {len(rows)} wired client(s) ({with_ip} with an IP) to {filepath}"
        )
    else:
        print(
            f"No wired clients found on '{device_name}' in the last {days} day(s). "
            f"Wrote a header-only CSV to {filepath}"
        )


def download_switch_config(site_id, device_id):
    """Download one switch's config to outputs/, then optionally PUT an upload."""
    try:
        device_info = get_device_info(site_id, device_id)
        filepath = save_device_config(device_info)
        print(f"\nDevice configuration saved to {filepath}")
    except Exception as e:
        print(f"Error fetching or saving device config: {e}")
        return

    # Optionally upload config
    upload = input("Would you like to upload 'upload_config.json' to this device? (y/n): ").strip().lower()
    if upload == 'y':
        try:
            if not UPLOAD_CONFIG_FILE.exists():
                print("❌ File 'upload_config.json' does not exist.")
                return
            if UPLOAD_CONFIG_FILE.stat().st_size == 0:
                print("❌ File 'upload_config.json' is empty.")
                return
            with open(UPLOAD_CONFIG_FILE, 'r', encoding='utf-8') as f:
                try:
                    upload_data = json.load(f)
                except json.JSONDecodeError as jde:
                    print(f"❌ File is not valid JSON: {jde}")
                    return
            if not isinstance(upload_data, dict):
                print("❌ File JSON structure is not a dictionary/object. Upload aborted.")
                return
            put_url = f"{API_URL.rstrip('/')}/api/v1/sites/{site_id}/devices/{device_id}"
            print(f"➡️  PUT to API endpoint: {put_url}")
            headers = {
                "Authorization": f"Token {API_KEY}",
                "Content-Type": "application/json"
            }
            put_response = requests.put(put_url, headers=headers, data=json.dumps(upload_data), timeout=REQUEST_TIMEOUT)
            print(f"\n➡️  PUT data sent to device ID: {device_id} at endpoint: {put_url}")
            if put_response.ok:
                print(f"✅ PUT succeeded! Status code: {put_response.status_code}")
            else:
                print(f"❌ PUT failed! Status code: {put_response.status_code}")
            try:
                response_json = put_response.json()
                print("API Response:", json.dumps(response_json, indent=4))
                # Print a summary if possible
                if "msg" in response_json:
                    print(f"➡️  API Message: {response_json['msg']}")
                elif "message" in response_json:
                    print(f"➡️  API Message: {response_json['message']}")
                elif "error" in response_json:
                    print(f"❌ API Error: {response_json['error']}")
                else:
                    print("✅ PUT completed. See above for full API response.")
            except Exception:
                print("Raw response:", put_response.text)
                print("⚠️  Could not decode JSON from API response.")
        except Exception as e:
            print(f"❌ Failed to upload config: {e}")


def main():
    try:
        while True:
            # Step 1: List sites
            try:
                sites = get_sites()
            except Exception as e:
                print(f"Error loading sites: {e}")
                return

            print("Available Sites:")
            for idx, site in enumerate(sites, 1):
                print(f"{idx}: {site['name']}")
            print("A: Save configurations for all switches")
            print("0: Exit")
            site_choice_str = input(
                "Select a site, A to save all switch configs, or 0 to exit: "
            ).strip()
            if site_choice_str.casefold() == "a":
                export_all_switch_configs(sites)
                continue
            if not site_choice_str.isdigit():
                print("Invalid input. Please enter a site number, A, or 0.")
                continue
            site_choice = int(site_choice_str)
            if site_choice == 0:
                print("Exiting.")
                return
            if not (1 <= site_choice <= len(sites)):
                print("Invalid selection. Try again.")
                continue
            selected_site = sites[site_choice - 1]
            site_id = selected_site['id']

            while True:
                # Step 2: List devices in site
                try:
                    devices = get_devices(site_id)
                except Exception as e:
                    print(f"Error fetching devices: {e}")
                    break

                if not devices:
                    print("\nNo devices found in this site.")
                    break
                print("\nDevices in selected site:")
                for idx, device in enumerate(devices, 1):
                    print(f"{idx}: {device.get('name', 'Unnamed')}")
                print("0: Return to site menu")
                print("99: Exit")
                device_choice_str = input("Select a device by number (0 to return, 99 to exit): ").strip()
                if not device_choice_str.isdigit():
                    print("Invalid input. Please enter a number.")
                    continue
                device_choice = int(device_choice_str)
                if device_choice == 0:
                    break
                if device_choice == 99:
                    print("Exiting.")
                    sys.exit(0)
                if not (1 <= device_choice <= len(devices)):
                    print("Invalid selection. Try again.")
                    continue
                selected_device = devices[device_choice - 1]
                device_id = selected_device['id']

                # Step 3: Choose an action for the selected switch
                while True:
                    print(f"\nSelected switch: {selected_device.get('name', 'Unnamed')}")
                    print("  1: Download switch configuration (JSON -> outputs/)")
                    print("  2: Export wired clients (IP/MAC) to CSV -> outputs/")
                    print("  0: Back to device list")
                    action = input("Choose an action (1, 2, or 0): ").strip()
                    if action == '0':
                        break
                    if action == '1':
                        download_switch_config(site_id, device_id)
                    elif action == '2':
                        export_wired_clients_for_device(selected_site, selected_device)
                    else:
                        print("Invalid input. Please enter 1, 2, or 0.")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    main()
