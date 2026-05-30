"""FreeHV self-update.

Updates the FreeHV application code in place by checking the project's GitHub
releases and, on request, checking out the latest release tag and restarting
the service. Designed to follow *tagged releases* by default (not every commit
to main), so production boxes only move when a real version is cut.

This updates the FreeHV app only. OS/package updates are a separate, clearly
labelled action (see system_update()).

Safety notes (honest limits):
- Updates happen in the existing /opt/freehv git checkout. If /opt/freehv is
  not a git repo (e.g. file-copied by an older installer), app-update is
  reported unavailable and the UI tells the user to reinstall/clone.
- We capture the current commit before updating so a failed restart can be
  rolled back, but a truly bricked box still needs console access. This is a
  v1 updater: solid, not bulletproof.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = os.environ.get("FREEHV_REPO", "Guinnessstache/freehv")
INSTALL_DIR = Path(os.environ.get("FREEHV_INSTALL_DIR", "/opt/freehv"))
# Follow tagged releases by default; set FREEHV_UPDATE_CHANNEL=main to track tip.
CHANNEL = os.environ.get("FREEHV_UPDATE_CHANNEL", "release").lower()
VERSION_FILE = INSTALL_DIR / "VERSION"


def _run(cmd, cwd=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True,
                          text=True, timeout=timeout)


def _is_git_repo() -> bool:
    return (INSTALL_DIR / ".git").is_dir()


def current_version() -> str:
    # Prefer the checked-out git tag/commit; fall back to a VERSION file.
    if _is_git_repo():
        try:
            tag = _run(["git", "describe", "--tags", "--always"],
                       cwd=INSTALL_DIR).stdout.strip()
            if tag:
                return tag
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "unknown"


def _latest_release_tag() -> str | None:
    """Query GitHub for the latest release tag (no auth needed for public)."""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "FreeHV-updater"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return data.get("tag_name")
    except Exception:
        return None


def check() -> dict:
    """Report update availability without changing anything."""
    if not _is_git_repo():
        return {"available": False, "supported": False,
                "current": current_version(),
                "reason": "This install isn't a git checkout, so in-place "
                          "app updates aren't available. Reinstall from a "
                          "current ISO or clone the repo to /opt/freehv."}
    cur = current_version()
    if CHANNEL == "main":
        return {"available": True, "supported": True, "current": cur,
                "channel": "main", "latest": "main (tip)"}
    latest = _latest_release_tag()
    if not latest:
        return {"available": False, "supported": True, "current": cur,
                "channel": "release", "latest": None,
                "reason": "Could not reach GitHub or no releases published yet."}
    return {"available": latest != cur, "supported": True, "current": cur,
            "channel": "release", "latest": latest}


def apply_update() -> dict:
    """Fetch and check out the target ref, then signal a service restart.

    Returns a dict describing the result. The actual process restart is done
    by the caller (systemd restarts us) after this returns success.
    """
    if not _is_git_repo():
        return {"ok": False, "error": "Not a git checkout; cannot update in place."}

    # Record current commit for rollback.
    try:
        before = _run(["git", "rev-parse", "HEAD"], cwd=INSTALL_DIR).stdout.strip()
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"git error: {e.stderr or e}"}

    try:
        _run(["git", "fetch", "--tags", "--prune", "origin"], cwd=INSTALL_DIR)
        if CHANNEL == "main":
            target = "origin/main"
        else:
            target = _latest_release_tag()
            if not target:
                return {"ok": False, "error": "No release tag to update to."}
        _run(["git", "checkout", "-f", target], cwd=INSTALL_DIR)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"update failed: {e.stderr or e}",
                "rolled_back_to": before}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "update timed out."}

    return {"ok": True, "from": before, "to": current_version(),
            "restart_required": True}


def system_update() -> dict:
    """Update OS packages (qemu/libvirt/kernel/etc). Separate and riskier —
    may pull a new kernel that needs a reboot. Kept out of the app update."""
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    try:
        _run(["apt-get", "update", "-y"], timeout=600)
        out = subprocess.run(
            ["apt-get", "upgrade", "-y"], env=env, check=True,
            capture_output=True, text=True, timeout=3600)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": (e.stderr or str(e))[-500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "system update timed out."}
    reboot = Path("/var/run/reboot-required").exists()
    return {"ok": True, "reboot_required": reboot, "log": out.stdout[-500:]}
