import csv
import difflib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

from get_site_ids import get_all_sites, write_site_codes
from mist_client import MistClient, describe_request_error
from vlan_copy import (
    build_merged_networks,
    extract_networks,
    plan_network_merge,
    verify_network_merge,
)


BASE_DIR = Path(__file__).resolve().parent
SITE_CODES_FILE = BASE_DIR / "site_codes.env"
OUTPUT_DIR = BASE_DIR / "outputs"
BACKUP_DIR = BASE_DIR / "backups"
UPLOAD_CONFIG_FILE = BASE_DIR / "upload_config.json"
REQUEST_TIMEOUT = 30

_CLIENT = None
_SETTINGS = None
VLAN_COPY_ENV_FLAG = "ENABLE_NON_PRODUCTION_VLAN_COPY"
VLAN_COPY_ACKNOWLEDGEMENT = "NON-PRODUCTION VLAN COPY"
CUSTOM_UPLOAD_ACKNOWLEDGEMENT = "APPLY CUSTOM CONFIG"


def read_settings():
    """Read settings for status display without requiring them to be complete."""
    values = dotenv_values(BASE_DIR / ".env", interpolate=False)

    def value(name):
        return str(values.get(name) or "").strip()

    return {
        "API_URL": value("API_URL"),
        "MIST_API_KEY": value("MIST_API_KEY"),
        "ORG_ID": value("ORG_ID"),
        VLAN_COPY_ENV_FLAG: value(VLAN_COPY_ENV_FLAG),
    }


def load_settings():
    """Load and validate runtime settings without side effects at import time."""
    settings = read_settings()
    missing = [
        name for name in ("API_URL", "MIST_API_KEY") if not settings[name]
    ]
    if missing:
        raise RuntimeError(f"Missing required value(s) in .env: {', '.join(missing)}")
    return settings


def configure_client(settings=None):
    """Create the shared Mist client used by interactive operations."""
    global _CLIENT, _SETTINGS
    _CLIENT = None
    _SETTINGS = None
    settings = settings or load_settings()
    _CLIENT = MistClient(
        settings["API_URL"], settings["MIST_API_KEY"], timeout=REQUEST_TIMEOUT
    )
    _SETTINGS = settings
    return _CLIENT


def get_client():
    if _CLIENT is None:
        raise RuntimeError("Mist API client has not been configured")
    return _CLIENT


def vlan_copy_enabled(settings=None):
    settings = settings or read_settings()
    value = settings.get(VLAN_COPY_ENV_FLAG, "").casefold()
    return value in {"1", "true", "yes", "on"}


def ensure_client():
    """Return a configured client or explain how to repair local settings."""
    try:
        # Re-read .env for every top-level action; never reuse a stale token.
        return configure_client()
    except (RuntimeError, ValueError) as error:
        print(f"\nAPI client is not ready: {error}")
        print("Run menu option 1 to validate the local configuration.")
        return None

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


def validate_api_configuration():
    """Validate local settings, token authentication, org access, and site cache."""
    global _CLIENT, _SETTINGS
    # A failed retest must not leave a previously valid client available.
    _CLIENT = None
    _SETTINGS = None
    print("\nConfiguration and API validation")
    print("-" * 40)

    settings = read_settings()
    env_exists = (BASE_DIR / ".env").exists()
    print(f"[{'OK' if env_exists else 'FAIL'}] .env file: {BASE_DIR / '.env'}")

    missing = [
        name
        for name in ("API_URL", "MIST_API_KEY", "ORG_ID")
        if not settings.get(name)
    ]
    for name in ("API_URL", "MIST_API_KEY", "ORG_ID"):
        state = "set" if settings.get(name) else "missing"
        print(f"[{'OK' if state == 'set' else 'FAIL'}] {name}: {state}")
    print(
        f"[INFO] {VLAN_COPY_ENV_FLAG}: "
        f"{'enabled' if vlan_copy_enabled(settings) else 'disabled (safe default)'}"
    )
    if missing:
        print(f"\nValidation stopped. Missing: {', '.join(missing)}")
        return False

    try:
        client = MistClient(
            settings["API_URL"], settings["MIST_API_KEY"], timeout=REQUEST_TIMEOUT
        )
        identity = client.get_json("/api/v1/self")
        if not isinstance(identity, dict):
            raise RuntimeError("Mist returned an unexpected /self response")
        print("[OK] API token authentication: accepted by Mist")

        org_probe = client.get_json(
            f"/api/v1/orgs/{settings['ORG_ID']}/sites",
            params={"limit": 1, "page": 1},
        )
        if not isinstance(org_probe, list):
            raise RuntimeError("Mist returned an unexpected organisation response")
        print("[OK] Organisation access: site list is accessible")

        _CLIENT = client
        _SETTINGS = settings
    except requests.RequestException as error:
        print(f"[FAIL] {describe_request_error(error, settings['API_URL'])}")
        return False
    except (RuntimeError, ValueError) as error:
        print(f"[FAIL] API configuration is invalid: {error}")
        return False

    try:
        local_sites = get_sites()
        print(f"[OK] Local site catalogue: {len(local_sites)} site(s) available")
    except RuntimeError as error:
        print(f"[WARN] Local site catalogue: {error}")
        print("       Run menu option 2 to download the organisation site list.")

    print("\nValidation completed successfully. The API token itself was not displayed.")
    return True


def refresh_site_catalogue():
    """Download all organisation sites and replace the local site_codes.env cache."""
    client = ensure_client()
    if client is None:
        return False
    settings = _SETTINGS or read_settings()
    org_id = settings.get("ORG_ID", "")
    if not org_id:
        print("\nORG_ID is missing. Run menu option 1 after updating .env.")
        return False

    print("\nDownloading the organisation site catalogue...")
    try:
        sites = get_all_sites(client, org_id)
        written = write_site_codes(sites)
    except requests.RequestException as error:
        print(f"Site catalogue refresh failed: {describe_request_error(error, settings['API_URL'])}")
        return False
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Site catalogue refresh failed: {error}")
        return False

    if written == 0:
        print(
            "No sites were returned for this organisation. "
            "Initial setup is still required."
        )
        return False

    print(f"Site catalogue refreshed: {written} site(s) written to {SITE_CODES_FILE}")
    return True

def get_devices(site_id):
    path = f"/api/v1/sites/{site_id}/devices"
    all_switches = []
    seen_device_ids = set()
    seen_pages = set()
    page = 1
    while True:
        params = {
            "type": "switch",
            "limit": 300,
            "page": page
        }
        switches = get_client().get_json(path, params=params)
        if not isinstance(switches, list):
            raise RuntimeError("Mist returned an unexpected response for the device list")
        if not switches:
            break
        page_signature = json.dumps(switches, sort_keys=True, default=str)
        if page_signature in seen_pages:
            raise RuntimeError("Mist repeated a device-list page")
        seen_pages.add(page_signature)
        new_switches = []
        for switch in switches:
            if not isinstance(switch, dict):
                raise RuntimeError("Mist returned an invalid device-list entry")
            device_id = switch.get("id")
            if device_id and device_id in seen_device_ids:
                continue
            new_switches.append(switch)
            if device_id:
                seen_device_ids.add(device_id)
        if not new_switches:
            break
        all_switches.extend(new_switches)
        if len(switches) < params["limit"]:
            break
        page += 1
    return all_switches

def get_device_info(site_id, device_id):
    path = f"/api/v1/sites/{site_id}/devices/{device_id}"
    device = get_client().get_json(path)
    if not isinstance(device, dict):
        raise RuntimeError("Mist returned an unexpected device response")
    return device

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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename
    with filepath.open("w", encoding="utf-8") as output_file:
        json.dump(device_info, output_file, indent=4)
    return str(filepath)

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
    "client_mac", "ip", "switch_name", "port_id", "vlan", "manufacture",
    "dhcp_hostname", "last_seen_utc",
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
    path = f"/api/v1/sites/{site_id}/wired_clients/search"
    params = {"start": int(start_epoch), "end": int(end_epoch), "limit": 1000}

    clients = []
    seen_cursors = set()
    while True:
        payload = get_client().get_json(path, params=params)
        if not isinstance(payload, dict):
            raise RuntimeError("Mist returned an unexpected wired-client response")
        results = payload.get("results", [])
        if not isinstance(results, list) or not all(
            isinstance(item, dict) for item in results
        ):
            raise RuntimeError("Mist returned invalid wired-client results")
        clients.extend(results)

        next_cursor = payload.get("next")
        if not next_cursor:
            break
        if not isinstance(next_cursor, str):
            raise RuntimeError("Mist returned an invalid pagination cursor")
        if next_cursor in seen_cursors:
            raise RuntimeError("Mist repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        path, params = next_cursor, None

    return clients


def wired_client_to_row(client, switch_name):
    """Map one wired_clients/search record onto the CSV columns."""
    port_id = client.get("last_port_id") or _first(client.get("port_id"))
    ip = client.get("last_ip") or _first(client.get("ip"))
    hostname = client.get("last_hostname") or _first(client.get("hostname"))
    vlan = client.get("last_vlan")
    timestamp = client.get("timestamp")
    if timestamp in (None, ""):
        last_seen = ""
    else:
        try:
            last_seen = datetime.fromtimestamp(
                float(timestamp), timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OverflowError, OSError):
            last_seen = ""
    return {
        "client_mac": client.get("mac", ""),
        "ip": ip or "",
        "switch_name": switch_name,
        "port_id": port_id or "",
        "vlan": "" if vlan is None else vlan,
        "manufacture": client.get("manufacture", ""),
        "dhcp_hostname": hostname or "",
        "last_seen_utc": last_seen,
    }


def write_wired_clients_csv(site_name, scope_name, rows):
    """Write mapped rows to outputs/<site>_<scope>_wired_clients_<stamp>.csv."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_name = (
        f"{_safe_component(site_name)}_{_safe_component(scope_name)}"
        f"_wired_clients_{stamp}"
    )
    filepath = OUTPUT_DIR / f"{base_name}.csv"
    suffix = 2
    while filepath.exists():
        filepath = OUTPUT_DIR / f"{base_name}_{suffix}.csv"
        suffix += 1
    with filepath.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=WIRED_CLIENT_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    return str(filepath)


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


def wired_client_rows_for_device(clients, device_mac, device_name):
    """Filter site results to one switch MAC and map them to CSV rows."""
    normalised_device_mac = _normalise_mac(device_mac)
    return [
        wired_client_to_row(client, device_name)
        for client in clients
        if _normalise_mac(client.get("last_device_mac")) == normalised_device_mac
    ]


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
    except requests.RequestException as error:
        print(f"Failed to fetch wired clients: {describe_request_error(error, read_settings()['API_URL'])}")
        return
    except Exception as error:
        print(f"Failed to fetch wired clients: {error}")
        return

    rows = wired_client_rows_for_device(all_clients, device_mac, device_name)
    rows.sort(
        key=lambda row: (
            derive_fpc(row["port_id"]),
            row["port_id"],
            row["client_mac"],
        )
    )

    try:
        filepath = write_wired_clients_csv(site_name, device_name, rows)
    except OSError as error:
        print(f"Failed to write the wired-client CSV: {error}")
        return
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


def build_config_diff(current_config, proposed_changes):
    """Return a unified diff limited to fields present in the proposed payload."""
    current_subset = {
        key: current_config.get(key) for key in sorted(proposed_changes)
    }
    proposed_subset = dict(current_subset)
    proposed_subset.update(proposed_changes)
    before = json.dumps(current_subset, indent=2, sort_keys=True).splitlines()
    after = json.dumps(proposed_subset, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(
            before,
            after,
            fromfile="current target",
            tofile="proposed target",
            lineterm="",
        )
    )


def save_timestamped_backup(device_info):
    """Save the target's current configuration immediately before a PUT."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    device_name = _safe_component(device_info.get("name") or "unknown_device")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filepath = BACKUP_DIR / f"{device_name}_{stamp}.json"
    suffix = 2
    while filepath.exists():
        filepath = BACKUP_DIR / f"{device_name}_{stamp}_{suffix}.json"
        suffix += 1
    filepath.write_text(
        json.dumps(device_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return filepath


def load_upload_payload(path=UPLOAD_CONFIG_FILE):
    """Read and validate the optional partial device-configuration payload."""
    if not path.exists():
        raise RuntimeError("File 'upload_config.json' does not exist.")
    if path.stat().st_size == 0:
        raise RuntimeError("File 'upload_config.json' is empty.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"File is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("File JSON structure is not an object.")
    if not payload:
        raise RuntimeError("File JSON object contains no changes.")
    return payload


def upload_config_with_preview(site_id, device_id, current_config):
    """Preview, confirm, back up, PUT, then verify a partial config payload."""
    try:
        upload_data = load_upload_payload()
    except (OSError, RuntimeError) as error:
        print(f"Upload aborted: {error}")
        return

    preview = build_config_diff(current_config, upload_data)
    if not preview:
        print("Upload aborted: the payload would not change the selected switch.")
        return

    device_name = str(current_config.get("name") or "unknown_device")
    print("\nDry-run preview (only payload fields are shown):")
    print(preview)
    confirmation = input(
        f"\nType the target switch name '{device_name}' to apply this PUT: "
    ).strip()
    if confirmation != device_name:
        print("Upload cancelled: target name did not match.")
        return

    try:
        backup_path = save_timestamped_backup(current_config)
        print(f"Pre-change backup saved to {backup_path}")
        path = f"/api/v1/sites/{site_id}/devices/{device_id}"
        get_client().put_json(path, upload_data)
        print("PUT accepted by Mist; verifying the requested fields...")

        verified_config = get_device_info(site_id, device_id)
        mismatched = [
            key
            for key, expected in upload_data.items()
            if verified_config.get(key) != expected
        ]
        if mismatched:
            print(
                "Verification warning: these fields do not exactly match the "
                f"requested values: {', '.join(mismatched)}"
            )
        else:
            print("Verification succeeded: all uploaded fields match Mist.")
    except requests.RequestException as error:
        print(
            f"PUT failed: {describe_request_error(error, read_settings()['API_URL'])} "
            "The backup was retained."
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"PUT failed: {error}")


def download_switch_config(site_id, device_id):
    """Read-only download of one switch configuration."""
    try:
        device_info = get_device_info(site_id, device_id)
        filepath = save_device_config(device_info)
        print(f"\nDevice configuration saved to {filepath}")
    except Exception as e:
        print(f"Error fetching or saving device config: {e}")
        return


def select_site(sites, heading, prompt, marked_site_id=None):
    """Prompt for one site and return None on cancellation."""
    print(f"\n{heading}")
    for index, site in enumerate(sites, 1):
        marker = " (source site)" if site["id"] == marked_site_id else ""
        print(f"  {index}: {site['name']}{marker}")
    print("  0: Cancel")

    site_choice = input(prompt).strip()
    if not site_choice.isdigit() or int(site_choice) == 0:
        return None
    site_index = int(site_choice)
    if not 1 <= site_index <= len(sites):
        print("Invalid site selection.")
        return None
    return sites[site_index - 1]


def select_switch(site, heading, prompt, excluded_device_id=None):
    """Prompt for one switch at a site and return None on cancellation."""
    devices = get_devices(site["id"])
    candidates = [
        device
        for device in devices
        if device.get("id") != excluded_device_id
    ]
    if not candidates:
        print("No eligible switches were found in that site.")
        return None

    print(f"\n{heading}")
    for index, device in enumerate(candidates, 1):
        print(f"  {index}: {device.get('name', 'Unnamed')}")
    print("  0: Cancel")
    device_choice = input(prompt).strip()
    if not device_choice.isdigit() or int(device_choice) == 0:
        return None
    device_index = int(device_choice)
    if not 1 <= device_index <= len(candidates):
        print("Invalid switch selection.")
        return None
    return candidates[device_index - 1]


def select_destination_switch(sites, source_site, source_device):
    """Prompt for a destination site and switch, excluding the source device."""
    destination_site = select_site(
        sites,
        "Destination sites",
        "Select the destination site: ",
        marked_site_id=source_site["id"],
    )
    if destination_site is None:
        print("VLAN copy cancelled.")
        return None, None
    excluded_id = (
        source_device.get("id")
        if destination_site["id"] == source_site["id"]
        else None
    )
    destination_device = select_switch(
        destination_site,
        "Destination switches",
        "Select the destination switch: ",
        excluded_device_id=excluded_id,
    )
    if destination_device is None:
        print("VLAN copy cancelled.")
        return None, None
    return destination_site, destination_device


def print_network_merge_plan(plan, source_count, destination_count):
    """Display the additions-only diff and every skipped conflict."""
    print("\nVLAN comparison summary:")
    print(f"  Source networks:      {source_count}")
    print(f"  Destination networks: {destination_count}")
    print(f"  Already identical:    {len(plan.unchanged)}")
    print(f"  Missing / to add:     {len(plan.additions)}")
    print(f"  Conflicts / skipped:  {len(plan.conflicts)}")

    if plan.conflicts:
        print("\nConflicts that will NOT be copied or modified:")
        for conflict in plan.conflicts:
            destination = (
                f"; destination: {conflict.destination_name}"
                if conflict.destination_name
                else ""
            )
            print(
                f"  ! {conflict.source_name} (VLAN {conflict.vlan_id}): "
                f"{conflict.reason}{destination}"
            )

    if plan.additions:
        print("\nAdditions-only JSON diff:")
        print(json.dumps({"networks": plan.additions}, indent=2, sort_keys=True))


def copy_missing_vlans(sites, source_site, source_device):
    """Copy only absent, non-conflicting source networks to another switch."""
    try:
        destination_site, destination_device = select_destination_switch(
            sites, source_site, source_device
        )
    except Exception as error:
        print(f"Failed to list destination switches: {error}")
        return
    if destination_device is None:
        return

    source_name = source_device.get("name") or "Unnamed source"
    destination_name = destination_device.get("name") or "Unnamed destination"
    print(f"\nComparing VLANs: '{source_name}' -> '{destination_name}'...")

    try:
        source_config = get_device_info(source_site["id"], source_device["id"])
        destination_config = get_device_info(
            destination_site["id"], destination_device["id"]
        )
        source_networks = extract_networks(source_config, "source")
        destination_networks = extract_networks(destination_config, "destination")
        plan = plan_network_merge(source_networks, destination_networks)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        print(f"VLAN comparison failed: {error}")
        return

    print_network_merge_plan(plan, len(source_networks), len(destination_networks))
    if not plan.additions:
        print("\nNo safe missing VLANs were found. Nothing will be sent to Mist.")
        return

    print(
        "\nSafety note: Mist replaces nested objects supplied in a PUT. The request "
        "will therefore contain the complete destination networks map plus only the "
        "additions shown above. No other device fields will be sent. Even with this "
        "merge, VLAN configuration may be temporarily removed and reapplied while "
        "Mist processes the change, causing traffic loss. This is non-production only."
    )
    confirmation = input(
        f"\nType the destination switch name '{destination_name}' to continue: "
    ).strip()
    if confirmation != destination_name:
        print("VLAN copy cancelled: destination name did not match.")
        return

    try:
        fresh_destination = get_device_info(
            destination_site["id"], destination_device["id"]
        )
        fresh_networks = extract_networks(fresh_destination, "destination")
        if fresh_networks != destination_networks:
            print(
                "VLAN copy aborted: the destination networks changed during the "
                "preview. Run the comparison again."
            )
            return

        merged_networks = build_merged_networks(fresh_networks, plan.additions)
        backup_path = save_timestamped_backup(fresh_destination)
        print(f"Pre-change destination backup saved to {backup_path}")

        path = (
            f"/api/v1/sites/{destination_site['id']}/devices/"
            f"{destination_device['id']}"
        )
        get_client().put_json(path, {"networks": merged_networks})

        verified_config = get_device_info(
            destination_site["id"], destination_device["id"]
        )
        verified_networks = extract_networks(verified_config, "verified destination")
        failures = verify_network_merge(
            fresh_networks, plan.additions, verified_networks
        )
        if failures:
            print("CRITICAL: Mist accepted the PUT but verification found problems:")
            for failure in failures:
                print(f"  ! {failure}")
            print(f"Use the retained backup for investigation: {backup_path}")
            return

        print(
            f"VLAN copy succeeded: {len(plan.additions)} missing network(s) added; "
            f"all {len(fresh_networks)} pre-existing destination network(s) verified unchanged."
        )
    except requests.RequestException as error:
        print(
            "VLAN copy request failed: "
            f"{describe_request_error(error, read_settings()['API_URL'])}"
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"VLAN copy failed: {error}")


def load_sites_for_action():
    """Load the local site catalogue with a menu-oriented error message."""
    try:
        return get_sites()
    except RuntimeError as error:
        print(f"\nLocal site catalogue is unavailable: {error}")
        print("Return to the menu to open initial setup and download the site list.")
        return None


def run_export_all_action():
    if ensure_client() is None:
        return
    sites = load_sites_for_action()
    if not sites:
        return
    confirmation = input(
        f"\nExport switch configurations from all {len(sites)} local site(s)? (y/N): "
    ).strip().casefold()
    if confirmation != "y":
        print("Bulk export cancelled.")
        return
    export_all_switch_configs(sites)


def run_device_tools_action():
    """Select one switch and offer read-only device operations."""
    if ensure_client() is None:
        return
    sites = load_sites_for_action()
    if not sites:
        return
    try:
        site = select_site(sites, "Sites", "Select a site for device tools: ")
        if site is None:
            return
        device = select_switch(
            site, "Switches", "Select a switch for read-only tools: "
        )
    except Exception as error:
        print(f"Failed to list switches: {error}")
        return
    if device is None:
        return

    print("\nRead-only device tools")
    print(f"  Site:   {site['name']}")
    print(f"  Switch: {device.get('name', 'Unnamed')}")
    print("  1: Download this switch configuration")
    print("  2: Export wired clients to CSV")
    print("  0: Cancel")
    choice = input("Choose a read-only action: ").strip()
    if choice == "1":
        download_switch_config(site["id"], device["id"])
    elif choice == "2":
        export_wired_clients_for_device(site, device)
    elif choice != "0":
        print("Invalid read-only action.")


def run_vlan_copy_action():
    """Apply the non-production feature flag and typed risk acknowledgement."""
    settings = read_settings()
    if not vlan_copy_enabled(settings):
        print("\nVLAN copy is DISABLED by default.")
        print(
            f"To expose this non-production feature, set {VLAN_COPY_ENV_FLAG}=true "
            "in .env and restart the tool."
        )
        return
    if ensure_client() is None:
        return

    print("\n" + "!" * 72)
    print("DANGER — NON-PRODUCTION VLAN COPY ONLY")
    print("!" * 72)
    print(
        "Mist replaces the complete nested networks object during this PUT. The tool "
        "builds a safe merged map and verifies it afterwards, but VLANs may still be "
        "temporarily removed/reapplied while the API change is processed. This can "
        "interrupt switching and drop production traffic."
    )
    print("DO NOT USE THIS FEATURE ON A PRODUCTION SWITCH.")
    acknowledgement = input(
        f"\nType '{VLAN_COPY_ACKNOWLEDGEMENT}' to acknowledge and continue: "
    ).strip()
    if acknowledgement != VLAN_COPY_ACKNOWLEDGEMENT:
        print("VLAN copy cancelled: risk acknowledgement did not match.")
        return

    sites = load_sites_for_action()
    if not sites:
        return
    try:
        source_site = select_site(
            sites, "Source sites", "Select the NON-PRODUCTION source site: "
        )
        if source_site is None:
            return
        source_device = select_switch(
            source_site,
            "Source switches",
            "Select the NON-PRODUCTION source switch: ",
        )
    except Exception as error:
        print(f"Failed to list source switches: {error}")
        return
    if source_device is None:
        return
    copy_missing_vlans(sites, source_site, source_device)


def run_custom_upload_action():
    """Keep arbitrary configuration PUTs behind a dedicated danger gate."""
    if ensure_client() is None:
        return
    print("\n" + "!" * 72)
    print("DANGER — CUSTOM SWITCH CONFIGURATION PUT")
    print("!" * 72)
    print(
        "This action changes a switch configuration and may interrupt production. "
        "Use it only with an approved, reviewed payload and change plan."
    )
    acknowledgement = input(
        f"\nType '{CUSTOM_UPLOAD_ACKNOWLEDGEMENT}' to choose a target: "
    ).strip()
    if acknowledgement != CUSTOM_UPLOAD_ACKNOWLEDGEMENT:
        print("Custom upload cancelled: risk acknowledgement did not match.")
        return

    sites = load_sites_for_action()
    if not sites:
        return
    try:
        site = select_site(sites, "Target sites", "Select the target site: ")
        if site is None:
            return
        device = select_switch(site, "Target switches", "Select the target switch: ")
    except Exception as error:
        print(f"Failed to list target switches: {error}")
        return
    if device is None:
        return

    try:
        current_config = get_device_info(site["id"], device["id"])
        snapshot = save_device_config(current_config)
        print(f"Current target configuration saved to {snapshot}")
    except Exception as error:
        print(f"Could not read and save the target configuration: {error}")
        return
    upload_config_with_preview(site["id"], device["id"], current_config)


def print_initial_setup_menu(catalogue_error):
    """Show only the actions needed before device operations are available."""
    print("\n" + "=" * 72)
    print("Welcome to the Securitas Juniper Mist API Tool")
    print("=" * 72)
    print("\nYou do not currently have any local Mist sites configured.")
    print(f"Catalogue status: {catalogue_error}")
    print("\nInitial setup")
    print("  1: Validate .env settings, API token, and organisation access")
    print("  2: Download the local Mist site catalogue")
    print("\n  0: Exit")


def print_main_menu(sites):
    """Show operational actions after the local site catalogue is available."""
    settings = read_settings()
    vlan_status = "ARMED — NON-PRODUCTION ONLY" if vlan_copy_enabled(settings) else "disabled"

    print("\n" + "=" * 72)
    print("Welcome to the Securitas Juniper Mist API Tool")
    print("=" * 72)
    print(f"Local site catalogue: {len(sites)} configured site(s)")
    print(f"VLAN copy safety gate: {vlan_status}")
    print("\nRead-only operations")
    print("  1: Export all switch configurations to outputs/")
    print("  2: Open read-only device tools")
    print("\nConfiguration changes")
    print("  3: Copy missing VLANs [NON-PRODUCTION ONLY]")
    print("  4: Apply upload_config.json to one switch [DANGER]")
    print("\n  0: Exit")


def main():
    try:
        configure_client()
    except (RuntimeError, ValueError):
        # Keep the menu available so option 1 can explain incomplete settings.
        pass

    while True:
        try:
            sites = get_sites()
        except RuntimeError as error:
            print_initial_setup_menu(error)
            choice = input("\nChoose a setup option: ").strip()
            if choice == "0":
                print("Exiting.")
                return
            if choice == "1":
                validate_api_configuration()
            elif choice == "2":
                refresh_site_catalogue()
            else:
                print("Invalid selection. Enter 0, 1, or 2.")
            continue

        print_main_menu(sites)
        choice = input("\nChoose an option: ").strip()
        if choice == "0":
            print("Exiting.")
            return
        if choice == "1":
            run_export_all_action()
        elif choice == "2":
            run_device_tools_action()
        elif choice == "3":
            run_vlan_copy_action()
        elif choice == "4":
            run_custom_upload_action()
        else:
            print("Invalid selection. Enter a number from 0 to 4.")

if __name__ == "__main__":
    main()
