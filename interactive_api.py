import json
import os
import sys
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
            site_id = sites[site_choice - 1]['id']

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
                device_id = devices[device_choice - 1]['id']

                # Step 3: Fetch and save device config
                try:
                    device_info = get_device_info(site_id, device_id)
                    filepath = save_device_config(device_info)
                    print(f"\nDevice configuration saved to {filepath}")
                except Exception as e:
                    print(f"Error fetching or saving device config: {e}")
                    continue

                # Step 4: Optionally upload config
                upload = input("Would you like to upload 'upload_config.json' to this device? (y/n): ").strip().lower()
                if upload == 'y':
                    try:
                        if not UPLOAD_CONFIG_FILE.exists():
                            print("❌ File 'upload_config.json' does not exist.")
                            continue
                        if UPLOAD_CONFIG_FILE.stat().st_size == 0:
                            print("❌ File 'upload_config.json' is empty.")
                            continue
                        with open(UPLOAD_CONFIG_FILE, 'r', encoding='utf-8') as f:
                            try:
                                upload_data = json.load(f)
                            except json.JSONDecodeError as jde:
                                print(f"❌ File is not valid JSON: {jde}")
                                continue
                        if not isinstance(upload_data, dict):
                            print("❌ File JSON structure is not a dictionary/object. Upload aborted.")
                            continue
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
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    main()
