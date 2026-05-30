"""Real KVM backend via libvirt.

This is the production path. It talks to the system libvirt daemon
(qemu:///system), creates qcow2 disks with qemu-img, generates domain XML,
and drives the VM lifecycle. Requires: libvirt, qemu-kvm, and the
libvirt-python bindings, on a host with VT-x/AMD-V.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

from .base import (Backend, BackendError, ConsoleInfo, HostInfo, Network,
                   StoragePool, StorageVolume, VM)

try:
    import libvirt  # type: ignore
    LIBVIRT_AVAILABLE = True
except ImportError:
    LIBVIRT_AVAILABLE = False

# Where guest disks and ISOs live. Override with FREEHV_DISK_DIR / FREEHV_ISO_DIR.
DISK_DIR = Path(os.environ.get("FREEHV_DISK_DIR", "/var/lib/freehv/disks"))
ISO_DIR = Path(os.environ.get("FREEHV_ISO_DIR", "/var/lib/freehv/isos"))
DISK_POOL = "freehv-disks"
ISO_POOL = "freehv-isos"

# libvirt domain state code -> our vocabulary
_STATE_MAP = {
    0: "unknown", 1: "running", 2: "running", 3: "paused",
    4: "stopped", 5: "stopped", 6: "stopped", 7: "unknown",
}

_DOMAIN_XML = """\
<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{memory_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='cdrom'/>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough' check='none' migratable='off'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    {cdrom}
    <interface type='network'>
      <source network='{network}'/>
      <model type='virtio'/>
    </interface>
    <graphics type='vnc' port='-1' listen='127.0.0.1'/>
    <video><model type='virtio'/></video>
    <console type='pty'/>
  </devices>
</domain>
"""

_CDROM_XML = """\
<disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>"""


_POOL_XML = """\
<pool type='dir'>
  <name>{name}</name>
  <target><path>{path}</path></target>
</pool>
"""


class LibvirtBackend(Backend):
    def __init__(self, uri: str = "qemu:///system") -> None:
        if not LIBVIRT_AVAILABLE:
            raise BackendError("libvirt-python is not installed.")
        self._conn = libvirt.open(uri)
        if self._conn is None:
            raise BackendError(f"Could not connect to libvirt at {uri}.")
        DISK_DIR.mkdir(parents=True, exist_ok=True)
        ISO_DIR.mkdir(parents=True, exist_ok=True)
        # Best-effort turnkey setup; never let it block startup.
        try:
            self._ensure_pool(DISK_POOL, DISK_DIR)
            self._ensure_pool(ISO_POOL, ISO_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[freehv] storage pool setup skipped: {e}", file=sys.stderr)
        try:
            self.ensure_default_network()
        except Exception as e:  # noqa: BLE001
            print(f"[freehv] default network setup skipped: {e}", file=sys.stderr)

    # --- storage helpers -------------------------------------------------
    def _ensure_pool(self, name: str, path: Path):
        try:
            pool = self._conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            pool = self._conn.storagePoolDefineXML(
                _POOL_XML.format(name=name, path=path), 0)
            try:
                pool.build(0)
            except libvirt.libvirtError:
                pass  # dir may already exist
            pool.setAutostart(True)
        if not pool.isActive():
            pool.create(0)
        return pool

    def _pool_path(self, name: str) -> Path:
        pool = self._conn.storagePoolLookupByName(name)
        root = ET.fromstring(pool.XMLDesc(0))
        p = root.findtext("./target/path")
        if not p:
            raise BackendError(f"Pool '{name}' has no target path.")
        return Path(p)


    # --- helpers ---------------------------------------------------------
    def _vm_from_domain(self, dom) -> VM:
        state = _STATE_MAP.get(dom.state()[0], "unknown")
        info = dom.info()  # [state, maxMem(KiB), mem(KiB), nrVirtCpu, cpuTime]
        return VM(
            name=dom.name(),
            state=state,
            vcpus=info[3],
            memory_mb=info[1] // 1024,
        )

    def _lookup(self, name: str):
        try:
            return self._conn.lookupByName(name)
        except libvirt.libvirtError:
            raise BackendError(f"No VM named '{name}'.")

    # --- interface -------------------------------------------------------
    def host_info(self) -> HostInfo:
        info = self._conn.getInfo()  # [model, memMB, cpus, mhz, nodes, ...]
        free_kib = self._conn.getFreeMemory() // 1024 // 1024
        return HostInfo(
            mode="kvm",
            hypervisor=f"{self._conn.getType()} {self._conn.getVersion()}",
            hostname=self._conn.getHostname(),
            cpus=info[2],
            memory_mb=info[1],
            memory_free_mb=int(free_kib),
            vm_count=self._conn.numOfDomains() + self._conn.numOfDefinedDomains(),
        )

    def list_vms(self) -> List[VM]:
        return [self._vm_from_domain(d) for d in self._conn.listAllDomains()]

    def create_vm(self, name, memory_mb, vcpus, disk_gb, iso_path,
                  pool=None, network=None) -> VM:
        pool = pool or DISK_POOL
        # Never place a VM disk in the ISO pool — that's for install media only.
        if pool == ISO_POOL:
            pool = DISK_POOL
        network = network or "default"
        try:
            disk_dir = self._pool_path(pool)
        except (libvirt.libvirtError, BackendError):
            raise BackendError(f"Storage pool '{pool}' not found.")

        disk_path = disk_dir / f"{name}.qcow2"
        if disk_path.exists():
            raise BackendError(f"Disk for '{name}' already exists.")
        try:
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2",
                 str(disk_path), f"{disk_gb}G"],
                check=True, capture_output=True, text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise BackendError(f"Failed to create disk: {e}")

        cdrom = _CDROM_XML.format(iso_path=iso_path) if iso_path else ""
        xml = _DOMAIN_XML.format(
            name=name, memory_mb=memory_mb, vcpus=vcpus,
            disk_path=disk_path, cdrom=cdrom, network=network,
        )
        try:
            dom = self._conn.defineXML(xml)
        except libvirt.libvirtError as e:
            disk_path.unlink(missing_ok=True)
            raise BackendError(f"Failed to define VM: {e}")
        # Let the pool notice the new volume.
        try:
            self._conn.storagePoolLookupByName(pool).refresh(0)
        except libvirt.libvirtError:
            pass
        return self._vm_from_domain(dom)

    def start_vm(self, name) -> None:
        try:
            self._lookup(name).create()
        except libvirt.libvirtError as e:
            raise BackendError(str(e))

    def shutdown_vm(self, name) -> None:
        try:
            self._lookup(name).shutdown()
        except libvirt.libvirtError as e:
            raise BackendError(str(e))

    def force_off_vm(self, name) -> None:
        try:
            self._lookup(name).destroy()
        except libvirt.libvirtError as e:
            raise BackendError(str(e))

    def delete_vm(self, name, delete_disk=True) -> None:
        dom = self._lookup(name)
        try:
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError:
            pass
        if delete_disk:
            (DISK_DIR / f"{name}.qcow2").unlink(missing_ok=True)
        try:
            dom.undefine()
        except libvirt.libvirtError as e:
            raise BackendError(str(e))

    def console_info(self, name) -> ConsoleInfo:
        dom = self._lookup(name)
        if not dom.isActive():
            raise BackendError(f"'{name}' is not running — start it first.")
        # The live XML carries the actual auto-assigned VNC port.
        try:
            root = ET.fromstring(dom.XMLDesc(0))
        except ET.ParseError as e:
            raise BackendError(f"Could not read domain XML: {e}")
        gfx = root.find("./devices/graphics[@type='vnc']")
        if gfx is None:
            raise BackendError(f"'{name}' has no VNC graphics device.")
        port = gfx.get("port", "-1")
        if port in ("-1", None):
            raise BackendError(f"VNC port for '{name}' not yet allocated.")
        # QEMU listens on the host; the daemon proxies to it over loopback.
        listen = gfx.get("listen") or "127.0.0.1"
        host = "127.0.0.1" if listen in ("0.0.0.0", "::") else listen
        return ConsoleInfo(host=host, port=int(port))

    # --- storage & networking --------------------------------------------
    def list_pools(self) -> List[StoragePool]:
        pools = []
        for p in self._conn.listAllStoragePools():
            try:
                active = bool(p.isActive())
                path = ET.fromstring(p.XMLDesc(0)).findtext("./target/path") or ""
                cap = avail = 0
                if active:
                    p.refresh(0)
                    _, cap, _, avail = p.info()
                pools.append(StoragePool(
                    name=p.name(), path=path,
                    capacity_gb=round(cap / 1e9, 1),
                    available_gb=round(avail / 1e9, 1),
                    active=active))
            except libvirt.libvirtError:
                continue
        return pools

    def list_isos(self) -> List[StorageVolume]:
        isos = []
        for p in self._conn.listAllStoragePools():
            if not p.isActive():
                continue
            try:
                p.refresh(0)
                for v in p.listAllVolumes():
                    if v.name().lower().endswith(".iso"):
                        size = v.info()[1]
                        isos.append(StorageVolume(
                            name=v.name(), path=v.path(), pool=p.name(),
                            size_gb=round(size / 1e9, 2)))
            except libvirt.libvirtError:
                continue
        return sorted(isos, key=lambda i: i.name.lower())

    def list_networks(self) -> List[Network]:
        nets = []
        for n in self._conn.listAllNetworks():
            try:
                root = ET.fromstring(n.XMLDesc(0))
                fwd = root.find("./forward")
                bridge = root.find("./bridge")
                nets.append(Network(
                    name=n.name(),
                    active=bool(n.isActive()),
                    bridge=bridge.get("name") if bridge is not None else None,
                    forward=(fwd.get("mode") if fwd is not None else "isolated"),
                ))
            except libvirt.libvirtError:
                continue
        return nets

    def ensure_default_network(self) -> None:
        try:
            net = self._conn.networkLookupByName("default")
        except libvirt.libvirtError:
            return  # no default network defined on this host; nothing to do
        if not net.isActive():
            net.create()
        if not net.autostart():
            net.setAutostart(True)

    # --- ISO media management --------------------------------------------
    def iso_dir(self) -> str:
        try:
            return str(self._pool_path(ISO_POOL))
        except (libvirt.libvirtError, BackendError):
            return str(ISO_DIR)

    def _qemu_owner(self):
        import pwd
        for name in ("libvirt-qemu", "qemu"):
            try:
                p = pwd.getpwnam(name)
                return p.pw_uid, p.pw_gid
            except KeyError:
                continue
        return None

    def finalize_iso(self, filename: str) -> None:
        target = Path(self.iso_dir()) / Path(filename).name
        if not target.exists():
            raise BackendError(f"ISO '{filename}' not found after transfer.")
        owner = self._qemu_owner()
        try:
            if owner:
                os.chown(target, owner[0], owner[1])
            os.chmod(target, 0o644)
        except OSError as e:
            raise BackendError(f"Could not set ISO permissions: {e}")
        try:
            self._conn.storagePoolLookupByName(ISO_POOL).refresh(0)
        except libvirt.libvirtError:
            pass

    def fetch_iso(self, url: str, filename: str | None = None) -> str:
        from urllib.parse import urlparse
        if not filename:
            filename = os.path.basename(urlparse(url).path) or "download.iso"
        if not filename.lower().endswith(".iso"):
            filename += ".iso"
        filename = os.path.basename(filename)  # strip any path components
        target = Path(self.iso_dir()) / filename
        if target.exists():
            raise BackendError(f"An ISO named '{filename}' already exists.")
        # Stream the download with curl (handles redirects, shows progress in
        # the daemon log). Run as a subprocess so a huge ISO doesn't block in
        # Python memory.
        try:
            subprocess.run(
                ["curl", "-fSL", "--connect-timeout", "20",
                 "-o", str(target), url],
                check=True, capture_output=True, text=True, timeout=7200,
            )
        except subprocess.CalledProcessError as e:
            target.unlink(missing_ok=True)
            raise BackendError(f"Download failed: {e.stderr or e}")
        except subprocess.TimeoutExpired:
            target.unlink(missing_ok=True)
            raise BackendError("Download timed out.")
        self.finalize_iso(filename)
        return filename

    def delete_iso(self, filename: str) -> None:
        target = Path(self.iso_dir()) / Path(filename).name
        if not target.exists():
            raise BackendError(f"ISO '{filename}' not found.")
        try:
            target.unlink()
        except OSError as e:
            raise BackendError(f"Could not delete ISO: {e}")
        try:
            self._conn.storagePoolLookupByName(ISO_POOL).refresh(0)
        except libvirt.libvirtError:
            pass
