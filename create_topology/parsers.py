from __future__ import annotations

import re
import ipaddress
from collections import defaultdict

from .models import Neighbor


def safe_ip(value: str) -> str:
    for candidate in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", value or ""):
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return ""


def detect_vendor(*values: str) -> str:
    blob = " ".join(value for value in values if value).lower()
    if "meraki" in blob:
        return "Meraki"
    if "cisco" in blob:
        return "Cisco"
    if "arista" in blob:
        return "Arista"
    if "juniper" in blob:
        return "Juniper"
    if "hp" in blob or "hewlett" in blob or "aruba" in blob:
        return "HP/Aruba"
    if "extreme" in blob:
        return "Extreme"
    return "Unknown"


def normalize_interface(name: str) -> str:
    value = re.sub(r"\s+", "", (name or "").strip())
    aliases = {
        "tengigabitethernet": "Te",
        "gigabitethernet": "Gi",
        "fastethernet": "Fa",
        "ethernet": "Eth",
        "port-channel": "Po",
    }
    lowered = value.lower()
    for prefix, replacement in aliases.items():
        if lowered.startswith(prefix):
            return replacement + value[len(prefix):]
    short = re.match(r"^(Te|Gi|Fa|Eth|Po|Vl|Lo)(.*)$", value, re.IGNORECASE)
    return short.group(1).title() + short.group(2) if short else value


def parse_show_version(output: str) -> dict[str, str]:
    hostname = ""
    model = ""
    software_version = ""
    hardware_version = ""

    hostname_match = re.search(r"^(\S+)\s+uptime is .+$", output, re.MULTILINE)
    if hostname_match:
        hostname = hostname_match.group(1)

    version_match = re.search(r"Version\s+([^,\s]+)", output)
    if version_match:
        software_version = version_match.group(1)

    model_patterns = [
        r"^cisco\s+(\S+)\s+\(.+?\)\s+processor",
        r"^Cisco IOS Software \[(.*?)\]",
        r"^cisco\s+(\S+?)\s+\(.+?bytes of memory",
    ]
    for pattern in model_patterns:
        match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
        if match:
            model = match.group(1)
            break

    revision_match = re.search(r"processor \(revision\s+([^)]+)\)", output, re.IGNORECASE)
    if revision_match:
        hardware_version = revision_match.group(1).strip()

    return {
        "hostname": hostname,
        "model": model,
        "software_version": software_version,
        "hardware_version": hardware_version,
    }


def parse_show_inventory(output: str) -> dict[str, str]:
    model = ""
    hardware_version = ""

    pid_match = re.search(r"PID:\s*([^,\s]+)", output)
    if pid_match:
        model = pid_match.group(1).strip()

    vid_match = re.search(r"VID:\s*([^,\s]+)", output)
    if vid_match:
        hardware_version = vid_match.group(1).strip()

    descr_match = re.search(r'DESCR:\s*"([^"]+)"', output)
    return {
        "model": model,
        "hardware_version": hardware_version or (descr_match.group(1) if descr_match else ""),
    }


def _parse_neighbor_blocks(output: str, protocol: str) -> list[dict[str, str]]:
    normalized = output.replace("\r\n", "\n")
    blocks = re.split(r"\n-{3,}\n|\n(?=Device ID: |\n?Local Intf: )", normalized)
    records: list[dict[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block or ("Device ID:" not in block and "System Name:" not in block):
            continue

        record = defaultdict(str)
        if protocol == "CDP":
            patterns = {
                "remote_hostname": r"Device ID:\s*(.+)",
                "remote_ip": r"IP address:\s*(.+)",
                "local_interface": r"Interface:\s*([^,]+)",
                "remote_interface": r"Port ID \(outgoing port\):\s*(.+)",
                "remote_platform": r"Platform:\s*([^,]+)",
                "capabilities": r"Capabilities:\s*(.+)",
            }
        else:
            patterns = {
                "remote_hostname": r"System Name:\s*(.+)",
                "remote_ip": r"Management Address:\s*(.+)",
                "local_interface": r"Local Intf:\s*(.+)",
                "remote_interface": r"Port id:\s*(.+)",
                "remote_platform": r"System Description:\s*(.+)",
                "capabilities": r"Enabled Capabilities:\s*(.+)",
            }

        for key, pattern in patterns.items():
            match = re.search(pattern, block)
            if match:
                record[key] = match.group(1).strip()

        if not record["remote_hostname"] and protocol == "LLDP":
            fallback = re.search(r"Chassis id:\s*(.+)", block)
            if fallback:
                record["remote_hostname"] = fallback.group(1).strip()

        records.append(dict(record))
    return records


def parse_cdp_neighbors(output: str, local_hostname: str, local_ip: str) -> list[Neighbor]:
    neighbors: list[Neighbor] = []
    for record in _parse_neighbor_blocks(output, "CDP"):
        neighbors.append(
            Neighbor(
                local_hostname=local_hostname,
                local_ip=local_ip,
                local_interface=normalize_interface(record.get("local_interface", "")),
                remote_hostname=record.get("remote_hostname", ""),
                remote_ip=safe_ip(record.get("remote_ip", "")),
                remote_interface=normalize_interface(record.get("remote_interface", "")),
                remote_platform=record.get("remote_platform", ""),
                remote_vendor=detect_vendor("cisco", record.get("remote_platform", "")),
                protocol="CDP",
                capabilities=record.get("capabilities", ""),
            )
        )
    return neighbors


def parse_lldp_neighbors(output: str, local_hostname: str, local_ip: str) -> list[Neighbor]:
    neighbors: list[Neighbor] = []
    for record in _parse_neighbor_blocks(output, "LLDP"):
        vendor = detect_vendor(record.get("remote_platform", ""), record.get("capabilities", ""))
        neighbors.append(
            Neighbor(
                local_hostname=local_hostname,
                local_ip=local_ip,
                local_interface=normalize_interface(record.get("local_interface", "")),
                remote_hostname=record.get("remote_hostname", ""),
                remote_ip=safe_ip(record.get("remote_ip", "")),
                remote_interface=normalize_interface(record.get("remote_interface", "")),
                remote_platform=record.get("remote_platform", ""),
                remote_vendor=vendor,
                protocol="LLDP",
                capabilities=record.get("capabilities", ""),
            )
        )
    return neighbors


def parse_interfaces_trunk(output: str) -> set[str]:
    trunks: set[str] = set()
    for line in output.splitlines():
        line = line.rstrip()
        if not line or line.startswith("Port") or line.startswith("---"):
            continue
        if re.match(r"^\S+\s+\S+\s+\S+\s+\S+", line):
            trunks.add(normalize_interface(line.split()[0]))
    return trunks


def parse_switchport_trunks(output: str) -> set[str]:
    trunks: set[str] = set()
    blocks = re.split(r"\n(?=Name: )", output.replace("\r\n", "\n"))
    for block in blocks:
        name_match = re.search(r"Name:\s*(.+)", block)
        mode_match = re.search(r"Operational Mode:\s*(.+)", block)
        if name_match and mode_match and "trunk" in mode_match.group(1).lower():
            trunks.add(normalize_interface(name_match.group(1)))
    return trunks


def merge_neighbors(neighbors: list[Neighbor]) -> list[Neighbor]:
    merged: dict[tuple[str, str, str, str], Neighbor] = {}
    for neighbor in neighbors:
        key = (
            neighbor.local_hostname or neighbor.local_ip,
            neighbor.local_interface,
            neighbor.remote_hostname or neighbor.remote_ip,
            neighbor.remote_interface,
        )
        existing = merged.get(key)
        if existing:
            _merge_neighbor_data(existing, neighbor)
            continue
        merged[key] = neighbor

    records = list(merged.values())
    by_local_port: dict[tuple[str, str], list[Neighbor]] = defaultdict(list)
    for neighbor in records:
        by_local_port[(neighbor.local_ip or neighbor.local_hostname, neighbor.local_interface)].append(
            neighbor
        )

    result: list[Neighbor] = []
    consumed: set[int] = set()
    for group in by_local_port.values():
        if len(group) == 2:
            first_protocols = set(group[0].protocol.split("+"))
            second_protocols = set(group[1].protocol.split("+"))
            if first_protocols.isdisjoint(second_protocols) and first_protocols | second_protocols == {
                "CDP",
                "LLDP",
            }:
                preferred, other = sorted(
                    group, key=lambda item: _hostname_quality(item.remote_hostname), reverse=True
                )
                _merge_neighbor_data(preferred, other)
                result.append(preferred)
                consumed.update(id(item) for item in group)

    result.extend(item for item in records if id(item) not in consumed)
    return result


def _merge_neighbor_data(target: Neighbor, source: Neighbor) -> None:
    protocols = {
        item.strip()
        for item in (target.protocol + "+" + source.protocol).split("+")
        if item.strip()
    }
    target.protocol = "+".join(sorted(protocols))
    if not target.remote_ip:
        target.remote_ip = source.remote_ip
    if _hostname_quality(source.remote_hostname) > _hostname_quality(target.remote_hostname):
        target.remote_hostname = source.remote_hostname
    if target.remote_vendor in ("", "Unknown", "Cisco") and source.remote_vendor not in (
        "",
        "Unknown",
    ):
        target.remote_vendor = source.remote_vendor
    if len(source.remote_platform) > len(target.remote_platform):
        target.remote_platform = source.remote_platform
    if target.remote_interface in ("", "0") and source.remote_interface not in ("", "0"):
        target.remote_interface = source.remote_interface
    if not target.capabilities:
        target.capabilities = source.capabilities


def _hostname_quality(value: str) -> int:
    name = (value or "").strip().rstrip(".")
    compact = re.sub(r"[^0-9a-f]", "", name.lower())
    if not name:
        return 0
    if len(compact) >= 12 and re.fullmatch(r"[0-9a-f:.-]+", name.lower()):
        return 1
    return 2
