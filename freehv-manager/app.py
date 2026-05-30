#!/usr/bin/env python3
"""FreeHV management daemon.

A thin REST API + web console over a hypervisor backend (real KVM via
libvirt, or mock for development). This is the "brain" of the FreeHV
appliance.

New in v3 (Track B3): an in-browser VNC console. QEMU exposes each guest's
screen as raw RFB (VNC) over a TCP socket; browsers can't open raw TCP, so
this daemon bridges a WebSocket to that TCP port. The noVNC client (vendored
under static/novnc) renders it. No external websockify needed.

Run:  python3 app.py           (auto-detects KVM, falls back to mock)
      FREEHV_BACKEND=mock python3 app.py   (force mock for UI dev)
Open: http://localhost:5050
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   session)
from flask_sock import Sock

from auth import Auth
from backends import get_backend, BackendError
import updater

app = Flask(__name__)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)
backend = get_backend()

auth = Auth()
app.secret_key = auth.secret_key
_tls = bool(os.environ.get("FREEHV_TLS_CERT") and os.environ.get("FREEHV_TLS_KEY"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",   # blunts CSRF on the cookie-auth'd API
    SESSION_COOKIE_SECURE=_tls,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12h
    MAX_CONTENT_LENGTH=None,            # allow multi-GB ISO uploads
)

# --- simple per-IP login throttle (anti-brute-force) -----------------------
_LOGIN_FAILS: dict[str, list] = {}
_LOGIN_LOCK = threading.Lock()
_MAX_FAILS, _LOCK_SECONDS = 5, 60


def _login_locked(ip: str) -> int:
    with _LOGIN_LOCK:
        fails = _LOGIN_FAILS.get(ip, [])
        fails = [t for t in fails if time.time() - t < _LOCK_SECONDS]
        _LOGIN_FAILS[ip] = fails
        if len(fails) >= _MAX_FAILS:
            return int(_LOCK_SECONDS - (time.time() - fails[0]))
        return 0


def _login_fail(ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def _login_reset(ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)


def _err(message: str, code: int = 400):
    return jsonify({"error": message}), code


@app.before_request
def _gate():
    """Require a logged-in session for everything except the login flow and
    static assets. The WebSocket console is checked inside its handler."""
    if not auth.enabled:
        return
    open_endpoints = {"login_page", "api_login", "static"}
    if request.endpoint in open_endpoints or request.endpoint == "ws_console":
        return
    if not session.get("authed"):
        if request.path.startswith("/api/"):
            return _err("Unauthorized.", 401)
        return redirect("/login")


# --- auth routes -----------------------------------------------------------
@app.route("/login")
def login_page():
    if not auth.enabled or session.get("authed"):
        return redirect("/")
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    ip = request.remote_addr or "?"
    wait = _login_locked(ip)
    if wait > 0:
        return _err(f"Too many attempts. Try again in {wait}s.", 429)
    password = (request.get_json(silent=True) or {}).get("password", "")
    if auth.verify(password):
        _login_reset(ip)
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    _login_fail(ip)
    return _err("Incorrect password.", 401)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/change-password", methods=["POST"])
def api_change_password():
    data = request.get_json(silent=True) or {}
    if not auth.verify(data.get("current", "")):
        return _err("Current password is incorrect.", 403)
    new = data.get("new", "")
    if len(new) < 8:
        return _err("New password must be at least 8 characters.")
    auth.set_password(new)
    return jsonify({"ok": True})


# --- pages -----------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/console/<name>")
def console_page(name):
    return render_template("console.html", vm_name=name)


# --- host + VM REST --------------------------------------------------------
@app.route("/api/host")
def api_host():
    return jsonify(backend.host_info().to_dict())


@app.route("/api/vms")
def api_list_vms():
    return jsonify([vm.to_dict() for vm in backend.list_vms()])


@app.route("/api/vms", methods=["POST"])
def api_create_vm():
    data = request.get_json(silent=True) or {}
    try:
        name = str(data.get("name", "")).strip()
        memory_mb = int(data.get("memory_mb", 2048))
        vcpus = int(data.get("vcpus", 2))
        disk_gb = int(data.get("disk_gb", 20))
        iso_path = data.get("iso_path") or None
        pool = data.get("pool") or None
        network = data.get("network") or None
    except (TypeError, ValueError):
        return _err("Invalid VM parameters.")

    if not name:
        return _err("VM name is required.")
    if memory_mb < 128:
        return _err("Memory must be at least 128 MB.")
    if not (1 <= vcpus <= 256):
        return _err("vCPUs must be between 1 and 256.")

    try:
        vm = backend.create_vm(name, memory_mb, vcpus, disk_gb, iso_path,
                               pool=pool, network=network)
    except BackendError as e:
        return _err(str(e))
    return jsonify(vm.to_dict()), 201


@app.route("/api/vms/<name>/start", methods=["POST"])
def api_start(name):
    try:
        backend.start_vm(name)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/vms/<name>/shutdown", methods=["POST"])
def api_shutdown(name):
    try:
        backend.shutdown_vm(name)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/vms/<name>/force-off", methods=["POST"])
def api_force_off(name):
    try:
        backend.force_off_vm(name)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/vms/<name>", methods=["DELETE"])
def api_delete(name):
    delete_disk = request.args.get("delete_disk", "true").lower() != "false"
    try:
        backend.delete_vm(name, delete_disk=delete_disk)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/pools")
def api_pools():
    return jsonify([p.to_dict() for p in backend.list_pools()])


@app.route("/api/isos")
def api_isos():
    return jsonify([i.to_dict() for i in backend.list_isos()])


@app.route("/api/networks")
def api_networks():
    return jsonify([n.to_dict() for n in backend.list_networks()])


@app.route("/api/networks/default/ensure", methods=["POST"])
def api_ensure_default_net():
    try:
        backend.ensure_default_network()
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/isos/fetch", methods=["POST"])
def api_fetch_iso():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return _err("Please provide a valid http(s) URL.")
    try:
        name = backend.fetch_iso(url, data.get("filename") or None)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True, "filename": name})


@app.route("/api/isos/upload", methods=["POST"])
def api_upload_iso():
    # Stream the uploaded file straight to the ISO pool dir, chunk by chunk,
    # so a multi-GB ISO never has to fit in memory.
    f = request.files.get("file")
    if not f or not f.filename:
        return _err("No file provided.")
    fname = os.path.basename(f.filename)
    if not fname.lower().endswith(".iso"):
        return _err("File must be a .iso image.")
    dest = os.path.join(backend.iso_dir(), fname)
    if os.path.exists(dest):
        return _err(f"An ISO named '{fname}' already exists.")
    try:
        f.save(dest)              # werkzeug streams to disk
        backend.finalize_iso(fname)
    except (OSError, BackendError) as e:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return _err(f"Upload failed: {e}")
    return jsonify({"ok": True, "filename": fname})


@app.route("/api/isos/<filename>", methods=["DELETE"])
def api_delete_iso(filename):
    try:
        backend.delete_iso(filename)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/update/check")
def api_update_check():
    return jsonify(updater.check())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    result = updater.apply_update()
    if not result.get("ok"):
        return jsonify(result), 400
    # Schedule a service restart shortly after responding, so the client gets
    # confirmation before the daemon goes down and systemd brings it back up.
    def _restart():
        time.sleep(1.5)
        try:
            subprocess.Popen(["systemctl", "restart", "freehv-manager"])
        except Exception:
            os._exit(0)  # fallback: exit; systemd Restart=on-failure recovers
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify(result)


@app.route("/api/update/system", methods=["POST"])
def api_update_system():
    result = updater.system_update()
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/vms/<name>/eject-iso", methods=["POST"])
def api_eject_iso(name):
    try:
        backend.eject_iso(name)
    except BackendError as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/vms/<name>/console-info")
def api_console_info(name):
    """Tell the UI whether a live console is available (without exposing the
    raw VNC endpoint to the browser — the browser only ever talks to our
    WebSocket proxy)."""
    try:
        backend.console_info(name)
    except BackendError as e:
        return jsonify({"available": False, "reason": str(e)}), 200
    return jsonify({"available": True, "ws_path": f"/ws/console/{name}"})


# --- the VNC WebSocket <-> TCP proxy --------------------------------------
@sock.route("/ws/console/<name>")
def ws_console(ws, name):
    """Bridge one browser WebSocket to one guest's VNC TCP port.

    Two pumps run concurrently: the background thread copies guest->browser,
    the main loop copies browser->guest. Either side closing tears down both.
    """
    if auth.enabled and not session.get("authed"):
        print(f"[freehv] rejected unauthenticated console request for '{name}'",
              file=sys.stderr)
        return  # close without proxying

    try:
        ci = backend.console_info(name)
    except BackendError as e:
        print(f"[freehv] console denied for '{name}': {e}", file=sys.stderr)
        return  # returning closes the WebSocket

    try:
        tcp = socket.create_connection((ci.host, ci.port), timeout=5)
    except OSError as e:
        print(f"[freehv] VNC connect failed {ci.host}:{ci.port}: {e}",
              file=sys.stderr)
        return

    stop = threading.Event()

    def guest_to_browser():
        try:
            while not stop.is_set():
                data = tcp.recv(65536)
                if not data:
                    break
                ws.send(data)               # binary RFB frame to noVNC
        except Exception:
            pass
        finally:
            stop.set()

    pump = threading.Thread(target=guest_to_browser, daemon=True)
    pump.start()

    try:
        while not stop.is_set():
            msg = ws.receive()              # blocks; None on browser close
            if msg is None:
                break
            if isinstance(msg, str):
                msg = msg.encode()
            tcp.sendall(msg)                # browser keystrokes/clicks to guest
    except Exception:
        pass
    finally:
        stop.set()
        try:
            tcp.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        tcp.close()


if __name__ == "__main__":
    # threaded=True so the WebSocket proxy and REST calls don't block each
    # other on the dev server. For production behind the appliance, run under
    # gunicorn with a threaded/gevent worker.
    ssl_ctx = None
    if _tls:
        ssl_ctx = (os.environ["FREEHV_TLS_CERT"], os.environ["FREEHV_TLS_KEY"])
        print("[freehv] TLS enabled.", file=sys.stderr)
    app.run(host="0.0.0.0", port=5050, threaded=True, debug=False,
            ssl_context=ssl_ctx)
