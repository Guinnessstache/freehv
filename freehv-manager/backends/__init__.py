"""Backend selection.

Prefer real KVM via libvirt; fall back to the mock backend so the daemon
runs anywhere for development. Force a backend with FREEHV_BACKEND=mock|kvm.
"""

from __future__ import annotations

import os
import sys

from .base import (Backend, BackendError, ConsoleInfo, HostInfo, Network,
                   StoragePool, StorageVolume, VM)
from .mock_backend import MockBackend


def get_backend() -> Backend:
    forced = os.environ.get("FREEHV_BACKEND", "").lower()

    if forced == "mock":
        return MockBackend()

    if forced in ("", "kvm"):
        try:
            from .libvirt_backend import LibvirtBackend, LIBVIRT_AVAILABLE
            if LIBVIRT_AVAILABLE:
                return LibvirtBackend()
        except BackendError as e:
            if forced == "kvm":
                print(f"[freehv] KVM backend forced but unavailable: {e}",
                      file=sys.stderr)
                raise
            print(f"[freehv] libvirt unavailable ({e}); using mock backend.",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — never let backend init crash boot
            print(f"[freehv] libvirt init failed ({e}); using mock backend.",
                  file=sys.stderr)

    print("[freehv] running in MOCK mode (no KVM). UI is fully usable; "
          "VMs are simulated.", file=sys.stderr)
    return MockBackend()


__all__ = ["get_backend", "Backend", "BackendError", "ConsoleInfo",
           "HostInfo", "Network", "StoragePool", "StorageVolume", "VM"]
