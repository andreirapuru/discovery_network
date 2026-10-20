from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Neighbor:
    local_hostname: str
    local_ip: str
    local_interface: str
    remote_hostname: str
    remote_ip: str
    remote_interface: str
    remote_platform: str
    remote_vendor: str
    protocol: str
    capabilities: str = ""


@dataclass(slots=True)
class DeviceRecord:
    hostname: str
    ip: str
    vendor: str
    model: str
    hardware_version: str
    software_version: str
    access_status: str
    access_error: str
    discovery_source: str
    discovery_depth: int = 0


@dataclass(slots=True)
class DeviceSnapshot:
    hostname: str
    ip: str
    vendor: str
    model: str
    hardware_version: str
    software_version: str
    neighbors: list[Neighbor] = field(default_factory=list)
    trunks: set[str] = field(default_factory=set)


@dataclass(slots=True)
class DiscoveryResult:
    devices: dict[str, DeviceRecord] = field(default_factory=dict)
    snapshots: dict[str, DeviceSnapshot] = field(default_factory=dict)
    neighbors: list[Neighbor] = field(default_factory=list)
