"""Pure planning and verification helpers for safe switch-network copying."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkConflict:
    source_name: str
    vlan_id: int
    reason: str
    destination_name: str = ""


@dataclass(frozen=True)
class NetworkMergePlan:
    additions: dict
    unchanged: tuple
    conflicts: tuple


def normalise_vlan_id(value, label):
    """Return a comparable integer while allowing Mist's digit-string IDs."""
    if isinstance(value, bool):
        raise ValueError(f"{label} has an invalid vlan_id")
    if isinstance(value, int):
        vlan_id = value
    elif isinstance(value, str) and value.strip().isdigit():
        vlan_id = int(value.strip())
    else:
        raise ValueError(f"{label} has an invalid vlan_id")
    if not 1 <= vlan_id <= 4094:
        raise ValueError(f"{label} has vlan_id outside 1-4094")
    return vlan_id


def extract_networks(device_config, label):
    """Validate and copy a switch's networks map from a device response."""
    if not isinstance(device_config, dict):
        raise ValueError(f"{label} device configuration is not a JSON object")
    networks = device_config.get("networks", {})
    if networks is None:
        networks = {}
    if not isinstance(networks, dict):
        raise ValueError(f"{label} networks section is not a JSON object")

    validated = {}
    for name, definition in networks.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{label} contains a network with an invalid name")
        if not isinstance(definition, dict):
            raise ValueError(f"{label} network '{name}' is not a JSON object")
        normalise_vlan_id(definition.get("vlan_id"), f"{label} network '{name}'")
        validated[name] = deepcopy(definition)
    return validated


def plan_network_merge(source_networks, destination_networks):
    """Plan additions while refusing to modify destination names or VLAN IDs."""
    source_by_vlan = defaultdict(list)
    destination_by_vlan = defaultdict(list)
    destination_by_casefold = {}

    for name, definition in source_networks.items():
        vlan_id = normalise_vlan_id(
            definition.get("vlan_id"), f"source network '{name}'"
        )
        source_by_vlan[vlan_id].append(name)
    for name, definition in destination_networks.items():
        vlan_id = normalise_vlan_id(
            definition.get("vlan_id"), f"destination network '{name}'"
        )
        destination_by_vlan[vlan_id].append(name)
        destination_by_casefold.setdefault(name.casefold(), name)

    additions = {}
    unchanged = []
    conflicts = []

    for source_name in sorted(source_networks, key=str.casefold):
        source_definition = source_networks[source_name]
        vlan_id = normalise_vlan_id(
            source_definition.get("vlan_id"), f"source network '{source_name}'"
        )

        duplicate_source_names = source_by_vlan[vlan_id]
        if len(duplicate_source_names) > 1:
            conflicts.append(
                NetworkConflict(
                    source_name,
                    vlan_id,
                    "source contains multiple names for this VLAN ID",
                    ", ".join(sorted(duplicate_source_names, key=str.casefold)),
                )
            )
            continue

        if source_name in destination_networks:
            if destination_networks[source_name] == source_definition:
                unchanged.append(source_name)
            else:
                conflicts.append(
                    NetworkConflict(
                        source_name,
                        vlan_id,
                        "destination already has this name with different settings",
                        source_name,
                    )
                )
            continue

        casefold_match = destination_by_casefold.get(source_name.casefold())
        if casefold_match:
            conflicts.append(
                NetworkConflict(
                    source_name,
                    vlan_id,
                    "destination has a case-insensitive name collision",
                    casefold_match,
                )
            )
            continue

        vlan_matches = destination_by_vlan.get(vlan_id, [])
        if vlan_matches:
            conflicts.append(
                NetworkConflict(
                    source_name,
                    vlan_id,
                    "destination already uses this VLAN ID under another name",
                    ", ".join(sorted(vlan_matches, key=str.casefold)),
                )
            )
            continue

        additions[source_name] = deepcopy(source_definition)

    return NetworkMergePlan(
        additions=additions,
        unchanged=tuple(unchanged),
        conflicts=tuple(conflicts),
    )


def build_merged_networks(destination_networks, additions):
    """Build the full networks object required by Mist's replacement semantics."""
    overlap = set(destination_networks).intersection(additions)
    if overlap:
        names = ", ".join(sorted(overlap, key=str.casefold))
        raise ValueError(f"additions would overwrite destination networks: {names}")
    merged = deepcopy(destination_networks)
    merged.update(deepcopy(additions))
    return merged


def verify_network_merge(before, additions, after):
    """Return invariant failures after Mist applies the merged networks object."""
    failures = []
    for name, expected in before.items():
        if name not in after:
            failures.append(f"existing network '{name}' is missing")
        elif after[name] != expected:
            failures.append(f"existing network '{name}' changed")
    for name, expected in additions.items():
        if name not in after:
            failures.append(f"new network '{name}' is missing")
        elif after[name] != expected:
            failures.append(f"new network '{name}' does not match the source")

    expected_names = set(before).union(additions)
    unexpected = set(after).difference(expected_names)
    if unexpected:
        names = ", ".join(sorted(unexpected, key=str.casefold))
        failures.append(f"unexpected networks appeared: {names}")
    return failures
