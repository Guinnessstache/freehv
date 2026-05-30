"""Backend contract for FreeHV.

Any hypervisor backend (real libvirt/KVM, or the in-memory mock used for
UI development) implements this interface. The Flask app talks only to this
abstraction, so the UI and API are identical whether you're on a real KVM
host or developing on your laptop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import List


@dataclass
class VM:
    name: str
    state: str            # "running" | "stopped" | "paused" | "unknown"
    vcpus: int
    memory_mb: int
    disk_gb: int | None = None
    vnc_port: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConsoleInfo:
    host: str             # where the VNC server listens (proxied via daemon)
    port: int             # the live VNC TCP port for this VM

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StoragePool:
    name: str
    path: str
    capacity_gb: float
    available_gb: float
    active: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StorageVolume:
    name: str
    path: str
    pool: str
    size_gb: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Network:
    name: str
    active: bool
    bridge: str | None = None
    forward: str | None = None   # nat | route | bridge | isolated

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HostInfo:
    mode: str             # "kvm" | "mock"
    hypervisor: str
    hostname: str
    cpus: int
    memory_mb: int
    memory_free_mb: int
    vm_count: int

    def to_dict(self) -> dict:
        return asdict(self)


class Backend(ABC):
    """Operations the management daemon needs from a hypervisor."""

    @abstractmethod
    def host_info(self) -> HostInfo: ...

    @abstractmethod
    def list_vms(self) -> List[VM]: ...

    @abstractmethod
    def create_vm(self, name: str, memory_mb: int, vcpus: int,
                  disk_gb: int, iso_path: str | None,
                  pool: str | None = None, network: str | None = None) -> VM: ...

    @abstractmethod
    def start_vm(self, name: str) -> None: ...

    @abstractmethod
    def shutdown_vm(self, name: str) -> None:
        """Graceful ACPI shutdown."""

    @abstractmethod
    def force_off_vm(self, name: str) -> None:
        """Pull the virtual power cord."""

    @abstractmethod
    def delete_vm(self, name: str, delete_disk: bool = True) -> None: ...

    @abstractmethod
    def console_info(self, name: str) -> "ConsoleInfo":
        """Return the live VNC endpoint for a running VM.

        Raises BackendError if the VM isn't running or has no graphics."""

    # --- storage & networking (Track B4) ---------------------------------
    @abstractmethod
    def list_pools(self) -> "List[StoragePool]": ...

    @abstractmethod
    def list_isos(self) -> "List[StorageVolume]":
        """ISO images available to attach as install media."""

    @abstractmethod
    def list_networks(self) -> "List[Network]": ...

    @abstractmethod
    def ensure_default_network(self) -> None:
        """Make sure the libvirt 'default' NAT network is up & autostarting,
        so new VMs get connectivity without manual setup."""


class BackendError(Exception):
    """Raised for any backend-level failure; carries a user-facing message."""
