from __future__ import annotations

import logging
from collections import deque

from .models import DeviceRecord, DeviceSnapshot, DiscoveryResult, Neighbor
from .parsers import (
    detect_vendor,
    merge_neighbors,
    parse_cdp_neighbors,
    parse_interfaces_trunk,
    parse_lldp_neighbors,
    parse_show_inventory,
    parse_show_version,
    parse_switchport_trunks,
)
from .ssh_client import CiscoSSHClient, CommandOutputs, DeviceConnectionError


class DiscoveryEngine:
    def __init__(self, client: CiscoSSHClient, logger: logging.Logger) -> None:
        self.client = client
        self.logger = logger

    def discover(self, seed_ip: str, max_depth: int = 3) -> DiscoveryResult:
        if max_depth < 0:
            raise ValueError("max_depth must be zero or greater")
        result = DiscoveryResult()
        queue: deque[tuple[str, str, int]] = deque([(seed_ip, "seed", 0)])
        visited: set[str] = set()
        hostname_ips: dict[str, str] = {}

        while queue:
            ip, source, depth = queue.popleft()
            if not ip or ip in visited:
                continue

            self.logger.info(
                "[DISCOVERY] Accessing IP %s | depth=%s | discovered_by=%s", ip, depth, source
            )
            visited.add(ip)

            try:
                outputs = self.client.collect(ip)
                snapshot = self._build_snapshot(ip, outputs)
                hostname_key = snapshot.hostname.casefold()
                known_ip = hostname_ips.get(hostname_key)
                if known_ip and known_ip != ip:
                    raise DeviceConnectionError(
                        f"Session identity mismatch: {snapshot.hostname} was already collected at "
                        f"{known_ip}, but appeared while accessing {ip}",
                        "jump",
                    )
                hostname_ips[hostname_key] = ip
                result.snapshots[ip] = snapshot
                result.devices[ip] = DeviceRecord(
                    hostname=snapshot.hostname,
                    ip=ip,
                    vendor=snapshot.vendor,
                    model=snapshot.model,
                    hardware_version=snapshot.hardware_version,
                    software_version=snapshot.software_version,
                    access_status="success",
                    access_error="",
                    discovery_source=source,
                    discovery_depth=depth,
                )
                result.neighbors.extend(snapshot.neighbors)
                self.logger.info(
                    "[DEVICE] Collected %s | ip=%s | model=%s | neighbors=%s",
                    snapshot.hostname,
                    ip,
                    snapshot.model or "unknown",
                    len(snapshot.neighbors),
                )
                if snapshot.neighbors:
                    self.logger.info("[NEIGHBORS] Devices reported by %s:", snapshot.hostname)
                    for neighbor in snapshot.neighbors:
                        self.logger.info(
                            "[NEIGHBOR] local=%s %s | remote=%s | ip=%s | port=%s | protocol=%s",
                            snapshot.hostname,
                            neighbor.local_interface or "unknown",
                            neighbor.remote_hostname or "unknown",
                            neighbor.remote_ip or "no-ip",
                            neighbor.remote_interface or "unknown",
                            neighbor.protocol,
                        )
                else:
                    self.logger.info("[NEIGHBORS] No CDP/LLDP neighbors reported by %s", snapshot.hostname)
            except DeviceConnectionError as exc:
                self.logger.warning(
                    "[FAILED] ip=%s | category=%s | reason=%s",
                    ip,
                    exc.category,
                    " ".join(str(exc).split()),
                )
                learned_neighbor = next(
                    (neighbor for neighbor in result.neighbors if neighbor.remote_ip == ip),
                    None,
                )
                result.devices[ip] = DeviceRecord(
                    hostname=learned_neighbor.remote_hostname if learned_neighbor else "",
                    ip=ip,
                    vendor=(learned_neighbor.remote_vendor if learned_neighbor else "Cisco") or "Cisco",
                    model=learned_neighbor.remote_platform if learned_neighbor else "",
                    hardware_version="",
                    software_version="",
                    access_status="failed",
                    access_error=f"{exc.category}: {exc}",
                    discovery_source=source,
                    discovery_depth=depth,
                )
                continue

            for neighbor in snapshot.neighbors:
                if self._is_cisco_neighbor(neighbor):
                    if neighbor.remote_ip:
                        next_depth = depth + 1
                        next_source = f"{neighbor.protocol}:{snapshot.hostname or ip}"
                        if max_depth == 0 or next_depth <= max_depth:
                            self.logger.info(
                                "[QUEUE] Added Cisco neighbor %s (%s) at depth %s",
                                neighbor.remote_hostname or "unknown",
                                neighbor.remote_ip,
                                next_depth,
                            )
                            queue.append((neighbor.remote_ip, next_source, next_depth))
                        elif neighbor.remote_ip not in result.devices:
                            self.logger.info(
                                "[SKIP] Depth limit reached for %s (%s)",
                                neighbor.remote_hostname or "unknown",
                                neighbor.remote_ip,
                            )
                            result.devices[neighbor.remote_ip] = DeviceRecord(
                                hostname=neighbor.remote_hostname,
                                ip=neighbor.remote_ip,
                                vendor="Cisco",
                                model=neighbor.remote_platform,
                                hardware_version="",
                                software_version="",
                                access_status="depth_limit",
                                access_error="Maximum discovery depth reached",
                                discovery_source=next_source,
                                discovery_depth=next_depth,
                            )
                    else:
                        key = neighbor.remote_hostname or f"unknown-{neighbor.local_interface}"
                        inaccessible_id = f"unreachable::{key}"
                        if inaccessible_id not in result.devices:
                            result.devices[inaccessible_id] = DeviceRecord(
                                hostname=neighbor.remote_hostname,
                                ip="",
                                vendor="Cisco",
                                model=neighbor.remote_platform,
                                hardware_version="",
                                software_version="",
                                access_status="discovered_no_ip",
                                access_error="Neighbor discovered without management IP",
                                discovery_source=f"{neighbor.protocol}:{snapshot.hostname or ip}",
                                discovery_depth=depth + 1,
                            )
                else:
                    self.logger.info(
                        "[SKIP] Neighbor %s (%s) is not Cisco IOS; SSH was not attempted",
                        neighbor.remote_hostname or "unknown",
                        neighbor.remote_ip or "no-ip",
                    )
                    key = neighbor.remote_ip or f"external::{neighbor.remote_hostname or neighbor.local_interface}"
                    if key not in result.devices:
                        result.devices[key] = DeviceRecord(
                            hostname=neighbor.remote_hostname,
                            ip=neighbor.remote_ip,
                            vendor=neighbor.remote_vendor or "Unknown",
                            model=neighbor.remote_platform,
                            hardware_version="",
                            software_version="",
                            access_status="external",
                            access_error="Non-Cisco neighbor; SSH discovery not attempted",
                            discovery_source=f"{neighbor.protocol}:{snapshot.hostname or ip}",
                            discovery_depth=depth + 1,
                        )

        result.neighbors = merge_neighbors(result.neighbors)
        return result

    def _build_snapshot(self, ip: str, outputs: CommandOutputs) -> DeviceSnapshot:
        version_info = parse_show_version(outputs.show_version)
        inventory_info = parse_show_inventory(outputs.show_inventory)
        hostname = version_info["hostname"] or ip
        model = inventory_info["model"] or version_info["model"]
        hardware_version = inventory_info["hardware_version"] or version_info["hardware_version"]

        neighbors = merge_neighbors(
            parse_cdp_neighbors(outputs.show_cdp_neighbors_detail, hostname, ip)
            + parse_lldp_neighbors(outputs.show_lldp_neighbors_detail, hostname, ip)
        )
        trunks = parse_interfaces_trunk(outputs.show_interfaces_trunk)
        if not trunks:
            trunks = parse_switchport_trunks(outputs.show_interface_switchport)

        return DeviceSnapshot(
            hostname=hostname,
            ip=ip,
            vendor=detect_vendor("cisco", model),
            model=model,
            hardware_version=hardware_version,
            software_version=version_info["software_version"],
            neighbors=neighbors,
            trunks=trunks,
        )

    @staticmethod
    def _is_cisco_neighbor(neighbor: Neighbor) -> bool:
        if neighbor.remote_vendor.lower() == "meraki" or "meraki" in neighbor.remote_platform.lower():
            return False
        details = " ".join(
            [
                neighbor.remote_vendor,
                neighbor.remote_platform,
                neighbor.capabilities,
                neighbor.protocol,
            ]
        ).lower()
        return "cisco" in details or neighbor.protocol == "CDP"
