# FreeHV Manager (Track B1)

The management daemon and web console for FreeHV — the "brain" that drives
KVM and the interface that makes the whole thing feel like a product instead
of a pile of `virsh` commands.

It speaks to a hypervisor through a small backend interface, with two
implementations:

- **`kvm`** — real VMs via libvirt + qemu-kvm (the appliance / production path).
- **`mock`** — in-memory simulated VMs, so you can build and demo the UI on
  Windows/WSL or any laptop with **no KVM hardware**. State persists to JSON.

The daemon auto-detects: if libvirt is present and connectable it uses real
KVM; otherwise it drops to mock mode and says so in the UI (amber "mock mode"
badge vs. green "KVM live").

## Quick start (development, mock mode)

```sh
pip install -r requirements.txt
FREEHV_BACKEND=mock python3 app.py
# open http://localhost:5050
```

You can create, start, stop, and delete simulated VMs immediately — great for
iterating on the console.

## Running for real (on a KVM host)

```sh
sudo apt install qemu-kvm libvirt-daemon-system python3-libvirt
sudo usermod -aG libvirt,kvm $USER     # then re-login
pip install Flask
python3 app.py                          # auto-detects KVM
```

Make sure the libvirt **default network** is active so new VMs get networking:

```sh
sudo virsh net-start default
sudo virsh net-autostart default
```

Guest disks are created as qcow2 under `/var/lib/freehv/disks`
(override with `FREEHV_DISK_DIR`).

## Install as a service (appliance)

```sh
sudo cp -r . /opt/freehv/freehv-manager
sudo cp freehv-manager.service /etc/systemd/system/
sudo systemctl enable --now freehv-manager
```

## REST API

| Method | Path                          | Purpose                     |
|--------|-------------------------------|-----------------------------|
| GET    | `/api/host`                   | host info + current mode    |
| GET    | `/api/vms`                    | list all VMs                |
| POST   | `/api/vms`                    | create VM (JSON body)       |
| POST   | `/api/vms/<name>/start`       | power on                    |
| POST   | `/api/vms/<name>/shutdown`    | graceful ACPI shutdown      |
| POST   | `/api/vms/<name>/force-off`   | hard power off              |
| DELETE | `/api/vms/<name>`             | undefine (+ delete disk)    |
| GET    | `/api/vms/<name>/console-info`| is a live console available |
| WS     | `/ws/console/<name>`          | VNC stream (WebSocket↔TCP)  |
| GET    | `/api/pools`                  | storage pools + usage       |
| GET    | `/api/isos`                   | ISO images for install media|
| GET    | `/api/networks`               | virtual networks            |
| POST   | `/api/networks/default/ensure`| start/autostart default NAT |
| POST   | `/api/login`                  | sign in (sets session)      |
| POST   | `/api/logout`                 | sign out                    |
| POST   | `/api/change-password`        | rotate the admin password   |

Create body:
```json
{"name":"ubuntu","memory_mb":2048,"vcpus":2,"disk_gb":20,"iso_path":"/isos/ubuntu.iso"}
```

## In-browser console (the VNC bridge)

Click **Console** on a running VM to open a full-screen viewer at
`/console/<name>`. Under the hood:

```
browser (noVNC) ──WebSocket──► FreeHV daemon ──TCP──► QEMU VNC port
```

QEMU exposes each guest's screen as raw RFB (VNC) over TCP, but browsers can't
open raw TCP sockets — so the daemon bridges a WebSocket to that port. The
noVNC client is **vendored locally** under `static/novnc/` so the console works
on an offline appliance (no CDN, no external `websockify`). The browser never
sees the raw VNC endpoint; it only ever talks to the daemon's proxy.

**Developing the console without KVM:** run any VNC server locally and point
the mock backend at it:
```sh
FREEHV_BACKEND=mock FREEHV_MOCK_VNC_PORT=5901 python3 app.py
```
Start a (mock) VM, click Console, and you'll see that VNC server in the browser
— letting you iterate on the console UI with no hypervisor.

## Storage & networking (turnkey)

On a real KVM host, the daemon sets itself up so you don't have to learn
libvirt internals:

- On startup it ensures two **directory storage pools** exist and are
  autostarting: `freehv-disks` (guest disks, `/var/lib/freehv/disks`) and
  `freehv-isos` (drop ISOs here, `/var/lib/freehv/isos`). Override the paths
  with `FREEHV_DISK_DIR` / `FREEHV_ISO_DIR`.
- It ensures the libvirt **default NAT network** is started and autostarting,
  so new VMs get internet access with no manual `virsh net-start`.

In the Create-VM dialog you now pick the **install ISO**, **storage pool**,
and **network** from dropdowns instead of typing paths. The dashboard shows a
live Storage panel (capacity bars per pool) and a Networks panel (active state,
bridge, forward mode).

To add install media, just copy an `.iso` into `/var/lib/freehv/isos` — it
appears in the dropdown on the next refresh.

## Security (Track B5 — hardening)

The daemon is meant to sit on your management network, so it authenticates:

- **Single admin login.** On first run it reads `FREEHV_ADMIN_PASSWORD`, or
  generates a strong random password and prints it to the console/journal once.
  The password is stored only as a salted PBKDF2 hash in
  `$FREEHV_CONFIG_DIR/auth.json` (mode 0600); the Flask session secret lives
  there too. Change the password from the ⚙ button in the UI.
- **Everything is gated.** All pages, the REST API, and the VNC WebSocket
  require a valid session. The session cookie is HttpOnly + SameSite=Strict
  (which blunts CSRF), and is marked Secure when TLS is on.
- **Login throttling.** 5 failed attempts per IP triggers a 60-second lockout.
- **Guest consoles aren't exposed.** Guest VNC now listens on `127.0.0.1`
  only, so the daemon's authenticated proxy is the *only* path to a console —
  no one can hit the raw VNC ports from the network.
- **TLS:** set `FREEHV_TLS_CERT` and `FREEHV_TLS_KEY` to serve HTTPS directly,
  or terminate TLS at a reverse proxy.

Dev escape hatch: `FREEHV_AUTH=off` disables auth entirely. Use only on a
trusted local machine, never on a network.

## What's next (see project roadmap)

- **B3 — noVNC browser console.** ✅ Done.
- **B4 — storage pools & networking.** ✅ Done.
- **B5 — hardening (auth, session security, loopback VNC, TLS).** ✅ Done (this build).
- **B6 — bootable appliance image + installer**: a minimal immutable Linux
  image that boots straight into the (now authenticated) FreeHV daemon, then
  an installer ISO for bare metal — "insert USB, install, done."
- Later: multi-user/roles, snapshots, VM import (OVA/qcow2), clustering.

## Layout

```
app.py                     Flask API + serves the console
auth.py                    admin login, hashed credential, session secret
backends/base.py           the backend contract (VM, HostInfo, Backend, ...)
backends/libvirt_backend.py real KVM via libvirt
backends/mock_backend.py   simulated VMs for dev
templates/index.html       the web console (single file)
templates/login.html       sign-in page
templates/console.html     full-screen in-browser VNC viewer (noVNC)
static/novnc/              vendored noVNC client (offline-capable)
freehv-manager.service     systemd unit
```
