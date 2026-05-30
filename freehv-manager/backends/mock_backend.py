"""Mock backend — lets you develop the UI/API with no KVM hardware.

State persists to a JSON file so restarts keep your test VMs. This is what
runs on your Windows/WSL laptop; the real libvirt backend takes over
automatically on an actual KVM host.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import List

from .base import (Backend, BackendError, ConsoleInfo, HostInfo, Network,
                   StoragePool, StorageVolume, VM)

STATE_FILE = Path(os.environ.get("FREEHV_MOCK_STATE", "/tmp/freehv_mock.json"))


class MockBackend(Backend):
    def __init__(self) -> None:
        self._vms: dict[str, dict] = {}
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> None:
        if STATE_FILE.exists():
            try:
                self._vms = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                self._vms = {}

    def _save(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(self._vms, indent=2))
        except OSError:
            pass  # mock state is best-effort

    # --- interface -------------------------------------------------------
    def host_info(self) -> HostInfo:
        return HostInfo(
            mode="mock",
            hypervisor="FreeHV (mock — no KVM detected)",
            hostname="dev-workstation",
            cpus=os.cpu_count() or 4,
            memory_mb=32768,
            memory_free_mb=32768 - sum(v["memory_mb"] for v in self._vms.values()
                                       if v["state"] == "running"),
            vm_count=len(self._vms),
        )

    def list_vms(self) -> List[VM]:
        return [VM(**v) for v in self._vms.values()]

    def create_vm(self, name, memory_mb, vcpus, disk_gb, iso_path,
                  pool=None, network=None) -> VM:
        if name in self._vms:
            raise BackendError(f"A VM named '{name}' already exists.")
        if not name.strip():
            raise BackendError("VM name cannot be empty.")
        vm = VM(name=name, state="stopped", vcpus=vcpus, memory_mb=memory_mb,
                disk_gb=disk_gb, vnc_port=5900 + random.randint(1, 99))
        self._vms[name] = vm.to_dict()
        self._save()
        return vm

    def _require(self, name: str) -> dict:
        if name not in self._vms:
            raise BackendError(f"No VM named '{name}'.")
        return self._vms[name]

    def start_vm(self, name) -> None:
        self._require(name)["state"] = "running"
        self._save()

    def shutdown_vm(self, name) -> None:
        self._require(name)["state"] = "stopped"
        self._save()

    def force_off_vm(self, name) -> None:
        self._require(name)["state"] = "stopped"
        self._save()

    def delete_vm(self, name, delete_disk=True) -> None:
        self._require(name)
        del self._vms[name]
        self._save()

    def console_info(self, name) -> ConsoleInfo:
        vm = self._require(name)
        if vm["state"] != "running":
            raise BackendError(f"'{name}' is not running.")
        # Dev convenience: point the mock console at a real VNC server you're
        # running locally, so you can build the console UI without KVM.
        dev_port = os.environ.get("FREEHV_MOCK_VNC_PORT")
        if dev_port:
            return ConsoleInfo(host="127.0.0.1", port=int(dev_port))
        raise BackendError(
            "Console requires the KVM backend. For UI development, set "
            "FREEHV_MOCK_VNC_PORT to a running VNC server's port."
        )

    # --- storage & networking (simulated) --------------------------------
    def list_pools(self):
        used = sum(v.get("disk_gb") or 0 for v in self._vms.values())
        return [
            StoragePool("freehv-disks", "/var/lib/freehv/disks",
                        capacity_gb=500.0, available_gb=max(0, 500.0 - used),
                        active=True),
            StoragePool("freehv-isos", "/var/lib/freehv/isos",
                        capacity_gb=500.0, available_gb=476.0, active=True),
        ]

    def list_isos(self):
        # Pretend a couple of install ISOs are sitting in the ISO pool.
        return [
            StorageVolume("ubuntu-24.04.2-live-server-amd64.iso",
                          "/var/lib/freehv/isos/ubuntu-24.04.2-live-server-amd64.iso",
                          "freehv-isos", 2.7),
            StorageVolume("debian-12.5.0-amd64-netinst.iso",
                          "/var/lib/freehv/isos/debian-12.5.0-amd64-netinst.iso",
                          "freehv-isos", 0.65),
            StorageVolume("Win11_24H2_English_x64.iso",
                          "/var/lib/freehv/isos/Win11_24H2_English_x64.iso",
                          "freehv-isos", 5.8),
        ]

    def list_networks(self):
        return [
            Network("default", active=True, bridge="virbr0", forward="nat"),
            Network("isolated", active=False, bridge="virbr1", forward="isolated"),
        ]

    def ensure_default_network(self) -> None:
        pass  # always "up" in mock
