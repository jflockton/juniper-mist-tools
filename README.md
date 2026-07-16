# Mist Switch Configuration Utility

This project provides a small Python utility for working with Juniper Mist switch configurations and wired-client inventory:

- `get_site_ids.py` retrieves all sites in a Mist organisation and generates `site_codes.env`.
- `interactive_api.py` lets an operator select a site and switch, download switch configuration, export wired clients to CSV, safely copy missing VLANs to another switch, export configurations for every switch, or preview and optionally upload JSON configuration to one switch.
- `mist_client.py` provides shared authentication, timeout, pagination-URL safety, and JSON handling for both scripts.

There is no compilation or packaging step. Set up Python, install the dependencies, configure the Mist credentials, and run the scripts from the project directory.

## Index

| Section | Description |
|---|---|
| [Prerequisites](#prerequisites) | Required Python version, Mist access, token, and organisation information |
| [Files to copy](#files-to-copy) | Files required when moving the utility to another environment |
| [1. Create a virtual environment](#1-create-a-virtual-environment) | Create `.venv` and install the Python packages |
| [2. Create `.env`](#2-create-env) | Configure the API URL, API token, and organisation ID |
| [Obtain the Mist API values](#obtain-the-mist-api-values) | Instructions for `MIST_API_KEY`, `ORG_ID`, and `API_URL` |
| [Official Juniper reference URLs](#official-juniper-reference-urls) | Direct links to the relevant Juniper Mist documentation |
| [3. Generate the site-code file](#3-generate-the-site-code-file) | Create or refresh `site_codes.env` |
| [4. Run the interactive utility](#4-run-the-interactive-utility) | Select sites and switches or export all switch configurations |
| [Optional configuration upload](#optional-configuration-upload) | Preview, back up, apply, and verify `upload_config.json` |
| [Export wired clients to CSV](#export-wired-clients-to-csv) | Export the selected switch's wired-client inventory |
| [Copy missing VLANs](#copy-missing-vlans) | Safely add absent source VLANs to another switch |
| [Run the tests](#run-the-tests) | Run the offline mocked unit-test suite |
| [Refreshing sites](#refreshing-sites) | Update local site IDs after Mist changes |
| [VS Code](#vs-code) | Select the virtual environment and handle `.env` integration |
| [Troubleshooting](#troubleshooting) | Resolve common setup, API, and connectivity problems |
| [Security and generated files](#security-and-generated-files) | Protect tokens and downloaded configurations |

## Prerequisites

- Python 3.10 or newer (tested with Python 3.13)
- Network access to the correct Mist cloud API
- A Mist API token with access to the required organisation and sites
- The Mist organisation ID

The API URL depends on the Mist cloud hosting the organisation. For example, an EMEA organisation might use `https://api.eu.mist.com`. Use the URL appropriate for the target environment.

## Files to copy

Copy these files into a new project directory:

```text
get_site_ids.py
interactive_api.py
mist_client.py
vlan_copy.py
requirements.txt
.gitignore
.env.example               # Safe template for local Mist settings
upload_config.example.json  # Safe template for optional configuration uploads
```

Do not copy `.venv`, `.env`, `site_codes.env`, `outputs`, `backups`, or
`__pycache__` between environments. They should be created locally.

## 1. Create a virtual environment

Open PowerShell in the project directory:

```powershell
py -3 -m venv .venv
```

If the Python launcher is unavailable, use:

```powershell
python -m venv .venv
```

Install the required packages using the virtual environment directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Using the interpreter directly avoids PowerShell activation-policy problems. To activate the environment instead, run:

```powershell
.\.venv\Scripts\Activate.ps1
```

On Linux or macOS, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.

## 2. Create `.env`

Copy `.env.example` to `.env` in the same directory as the Python scripts, then
replace its placeholder values:

```powershell
Copy-Item .env.example .env
```

The template contains:

```env
API_URL=https://api.eu.mist.com
MIST_API_KEY=replace-with-api-token
ORG_ID=replace-with-organisation-id
ENABLE_NON_PRODUCTION_VLAN_COPY=false
```

`get_site_ids.py` requires the first three values. `interactive_api.py` uses `ORG_ID`
for its validation and site-catalogue refresh actions. VLAN copy is locked unless
`ENABLE_NON_PRODUCTION_VLAN_COPY=true`; leave it false or absent on production
workstations.

The `.env` file is authoritative and is read again for every top-level API action.
An older value inherited from VS Code or the process environment cannot override a
changed file value. A failed validation also clears any previously valid in-memory
API client.

### Obtain the Mist API values

#### `MIST_API_KEY`

Juniper calls the API key an **API token**. For a shared application, an organisation token is normally more suitable than a token tied to one user:

1. Sign in to the Juniper Mist portal.
2. Select **Organization > Admin > Settings**.
3. Find **API Token** and select **Create Token**.
4. Choose the minimum access level the utility needs, then generate the token.
5. Copy the complete key immediately and store it securely. Mist does not display the complete key again after creation.
6. Put the key itself in `MIST_API_KEY`; do not include the word `Token`, quotation marks, or other prefixes.

Juniper also documents user-token creation through **My Account**, which is useful for scripts operated by one person. See [Create API Tokens — Juniper Mist documentation](https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/task/create-token-for-rest-api.html).

#### `ORG_ID`

1. In the Mist portal, select **Organization > Admin > Settings**.
2. Find **Organization ID** near the top of the page.
3. Use the copy button and put that UUID in `ORG_ID`.

The ID is generated by Mist and cannot be changed. See [Find Your Organization ID — Juniper Mist documentation](https://www.juniper.net/documentation/us/en/software/mist/mist-management/topics/task/find-org-id.html).

If the token can access several organisations and you are unsure which ID to use, Juniper documents using **API > Self > Account > Get Self** in the API Reference to inspect the organisations and sites available to the authenticated token. See [Additional RESTful API Documentation — Juniper Mist](https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/concept/addition-restful-documentation.html).

#### `API_URL`

Use the API endpoint for the Mist cloud region containing the organisation. The portal hostname normally indicates the matching region; for example, `manage.eu.mist.com` corresponds to `https://api.eu.mist.com`. Check the current mapping in [API Endpoints and Global Regions — Juniper Mist](https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/topic-map/api-endpoint-url-global-regions.html).

### Official Juniper reference URLs

- Create API tokens: <https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/task/create-token-for-rest-api.html>
- Find the organisation ID: <https://www.juniper.net/documentation/us/en/software/mist/mist-management/topics/task/find-org-id.html>
- Mist API `GET /api/v1/self` reference: <https://www.juniper.net/documentation/us/en/software/mist/api/http/api/self/account/get-self>
- Use the Mist API Reference for testing: <https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/task/use-mist-api-reference.html>
- Mist API endpoints and global regions: <https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/topic-map/api-endpoint-url-global-regions.html>

Do not add quotes unless they are genuinely part of a value. Do not commit `.env` because it contains the API token.

## 3. Generate the site-code file

Run:

```powershell
.\.venv\Scripts\python.exe .\get_site_ids.py
```

The script requests every site from the configured Mist organisation and creates `site_codes.env`. Entries use this format:

```env
site_Manchester=00000000-0000-0000-0000-000000000000
site_Milton_Keynes=00000000-0000-0000-0000-000000000000
```

Spaces and punctuation in Mist site names are converted to underscores. Site-name casing is preserved. If two names produce the same key, a numeric suffix is added.

Running the script again replaces `site_codes.env` with the current site list. Regenerate it whenever sites are added, removed, or renamed in Mist.

## 4. Run the interactive utility

Run the utility from the project directory:

```powershell
.\.venv\Scripts\python.exe .\interactive_api.py
```

The startup menu adapts to the local site catalogue. If `site_codes.env` is missing
or contains no valid sites, only the initial setup actions are shown:

```text
Welcome to the Securitas Juniper Mist API Tool

You do not currently have any local Mist sites configured.

Initial setup
  1: Validate .env settings, API token, and organisation access
  2: Download the local Mist site catalogue

  0: Exit
```

After at least one site has been downloaded, the program switches automatically to
the operational menu and hides the initial setup actions:

```text
Welcome to the Securitas Juniper Mist API Tool

Read-only operations
  1: Export all switch configurations to outputs/
  2: Open read-only device tools

Configuration changes
  3: Copy missing VLANs [NON-PRODUCTION ONLY]
  4: Apply upload_config.json to one switch [DANGER]

  0: Exit
```

Initial-setup option `1` checks that `.env` exists and has the required values, calls
`GET /api/v1/self` to validate the token, probes the configured organisation's site
list, and reports whether the local catalogue is available. The token is never
printed. Initial-setup option `2` downloads every site and replaces `site_codes.env`.

Operational option `1` exports all switch configurations. Option `2` asks for a site and switch,
then offers only these read-only actions:

```text
1: Download this switch configuration
2: Export wired clients to CSV
0: Cancel
```

A single-switch configuration download is written as JSON to:

```text
outputs/<switch-name>.log
```

The `outputs` directory is created automatically. Invalid filename characters are replaced, and the device ID is added when duplicate switch names would otherwise produce the same filename.

## Optional configuration upload

Top-level option `6` previews and applies the partial JSON object in
`upload_config.json` to one explicitly selected switch. It is intentionally separate
from all download/read-only paths.

To use this feature:

1. Copy `upload_config.example.json` to the ignored file `upload_config.json`.
2. Ensure it contains valid JSON whose top-level value is an object.
3. Choose option `6` and type `APPLY CUSTOM CONFIG` at the danger gate.
4. Select the intended site and switch carefully.
5. Review the unified dry-run diff limited to the payload fields.
6. Type the target switch name exactly to approve the PUT.

Immediately before the PUT, the utility writes a timestamped copy of the target's
current configuration to `backups/`. After the PUT, it reads the switch again and
checks that every uploaded field matches the requested value. Both `backups/` and
`upload_config.json` should be handled as sensitive operational data. Leave
`upload_config.json` empty or answer `n` when only collecting configurations.

## Export wired clients to CSV

Read-only device-tool option `2` pulls every wired client seen on the selected
switch/stack and writes them to:

```text
outputs/<site>_<switch>_wired_clients_<YYYYMMDD-HHMMSS>.csv
```

You are prompted for a lookback window in days (default `1`). Columns: `client_mac, ip, switch_name, port_id, vlan, manufacture, dhcp_hostname, last_seen_utc`.

Notes:

- The client IP is only present where Mist has learned one (DHCP snooping or the switch ARP table), so the `ip` column is often blank — this is expected, not an error.
- Rows are ordered by switch port (stack members grouped together).
- The export mirrors the portal's Wired Clients page (data comes from the Mist `wired_clients/search` endpoint).
- Repeated exports never silently overwrite a CSV; a numeric suffix is added if a timestamped name already exists.

## Copy missing VLANs

Top-level option `5` is disabled by default. To make it available for a controlled
non-production test, set this explicitly; the menu re-reads `.env` on its next loop:

```env
ENABLE_NON_PRODUCTION_VLAN_COPY=true
```

The tool then displays a prominent warning that VLAN configuration may be temporarily
removed/reapplied while Mist processes the PUT, potentially interrupting switching
and dropping traffic. It requires the exact phrase `NON-PRODUCTION VLAN COPY` before
source selection. **Do not enable or use this action for production switches.**

After the gate, the selected switch becomes the source. The tool prompts for a
destination site and switch, so controlled copying can be performed within one site
or across sites.

The comparison uses both the network name and `vlan_id`:

- An identical name and definition is already present and is skipped.
- A missing name with an unused VLAN ID is safe and is proposed as an addition.
- The same name with different settings is a conflict and is skipped.
- A VLAN ID already used under another name is a conflict and is skipped.
- Case-insensitive name collisions and duplicate source VLAN IDs are conflicts and
  are skipped.

The utility prints a summary plus an additions-only JSON diff. If there are safe
additions, the operator must type the destination switch name exactly.

Mist `PUT` semantics require special care: a nested object included in a request
replaces that object in its entirety. Sending only the missing entries inside
`networks` could therefore remove the destination's existing networks. The utility
instead sends exactly one top-level field whose value is the complete safe merge:

```json
{
  "networks": {
    "<every existing destination network>": {},
    "<safe missing source networks>": {}
  }
}
```

Immediately before the PUT, the utility reads the destination again and aborts if
its networks changed after the preview. It saves a timestamped destination backup,
applies the merged map, reads the device back, and verifies that every pre-existing
network is unchanged and every proposed addition matches the source. No other device
configuration fields are sent. See Juniper's
[RESTful API overview](https://www.juniper.net/documentation/us/en/software/mist/automation-integration/topics/concept/restful-api-overview.html)
and [Update Site Device API reference](https://www.juniper.net/documentation/us/en/software/mist/api/http/api/sites/devices/update-site-device).

## Run the tests

The test suite uses mocked Mist responses and does not require a token or network
access:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

The 46 tests cover site-key generation, pagination and cursor safeguards, response
mapping, CSV output, MAC handling, upload-payload/diff validation, VLAN conflict
classification, full-map merge construction, concurrent-change aborts, and
post-PUT preservation checks. Menu tests also enforce the default-disabled VLAN gate,
the exact warning phrase, read-only/write separation, token-redaction behaviour, and
protection against stale environment tokens masking edits to `.env`.

## Refreshing sites

When the Mist site list changes, regenerate `site_codes.env` before opening the interactive utility:

```powershell
.\.venv\Scripts\python.exe .\get_site_ids.py
```

The interactive utility normally uses the generated file. If the file is missing or
empty, its initial-setup menu can query the organisation site-list endpoint and
rebuild it with option `2`.

## VS Code

Select the project virtual environment as the Python interpreter:

```text
<project directory>\.venv\Scripts\python.exe
```

The scripts load `.env` themselves with `python-dotenv`. VS Code terminal environment injection is therefore optional and can remain disabled.

## Troubleshooting

### Missing required values

```text
Missing required value(s) in .env
```

Confirm `.env` is alongside the scripts and contains non-empty `API_URL`, `MIST_API_KEY`, and, when generating site codes, `ORG_ID` values.

### `site_codes.env` was not found

Start `interactive_api.py` and use initial-setup option `2`, or run
`get_site_ids.py` directly.

### HTTP 401 or 403

Confirm that the API token is valid and has access to the configured organisation and sites. Also confirm that `API_URL` points to the Mist cloud containing that organisation.

### A site is missing or has an unexpected name

Run `get_site_ids.py` again. The generated keys reflect the current site names returned by Mist, not historical aliases stored elsewhere.

### Connection or timeout errors

Confirm internet access, DNS resolution, proxy/firewall rules, and connectivity to the configured Mist API URL.

The interactive validator converts low-level networking exceptions into concise
operator messages. Examples include:

```text
API hostname 'api.example.invalid' could not be resolved. Check API_URL and the Mist cloud region.
Connection to 'api.eu.mist.com' timed out. Check network access and API_URL.
TLS validation failed for 'api.eu.mist.com'. Check API_URL and certificates.
The API token was rejected (HTTP 401). Check MIST_API_KEY.
Mist denied access (HTTP 403). Check token permissions and ORG_ID.
```

### PowerShell blocks virtual-environment activation

Activation is not required. Run scripts using `.\.venv\Scripts\python.exe` as shown above.

## Security and generated files

The following files and directories should remain excluded from Git:

```text
.env
site_codes.env
outputs/
backups/
__pycache__/
```

Treat downloaded switch configurations as sensitive operational data. Store, share, and delete them according to the organisation's security requirements.
