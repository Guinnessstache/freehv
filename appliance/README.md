# FreeHV Appliance (Track B6)

Two ways to turn bare metal into a FreeHV hypervisor. Both run the *same*
provisioner (`setup.sh`), so they install identical, tested software.

## Option A — provision an existing Debian/Ubuntu box (fastest, testable now)

If you already have (or can quickly install) a minimal Debian or Ubuntu system
on your hypervisor machine:

```sh
sudo ./setup.sh
```

This installs qemu-kvm + libvirt + the FreeHV daemon, deploys it to
`/opt/freehv`, sets up the storage dirs and default network, and enables the
`freehv-manager` service. When it finishes, the console is at
`http://<host>:5050`. Grab the initial admin password:

```sh
journalctl -u freehv-manager | grep 'Initial admin password'
```

This is the path to use for your first real-hardware test of FreeHV.

## Option B — build an unattended installer ISO ("insert USB, install, done")

Remaster a Debian netinst ISO into a self-installing FreeHV appliance:

```sh
# 1. Download a Debian netinst ISO (e.g. debian-12.x.0-amd64-netinst.iso)
# 2. Build the installer (needs xorriso: apt install xorriso)
./build-appliance.sh debian-12.x.0-amd64-netinst.iso freehv-installer.iso
# 3. Write to USB (ERASES the stick):
sudo dd if=freehv-installer.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Boot the target from USB. It installs Debian + KVM + FreeHV unattended, then
reboots into the appliance with the console on `:5050`.

### What the build does

1. Extracts the Debian ISO.
2. Drops `preseed.cfg` at the ISO root and stages the whole FreeHV repo at
   `/freehv`.
3. Patches the BIOS (isolinux) **and** UEFI (grub) boot menus to auto-launch
   the preseeded install.
4. Repacks with `xorriso`, cloning the original El Torito boot records so the
   result still boots on both BIOS and UEFI.

The preseed's `late_command` copies `/cdrom/freehv` into the target and runs
`setup.sh --in-target`, which installs the stack and enables the service for
first boot.

## Important notes & honest caveats

- **The installer ERASES the target disk.** It's meant for a dedicated box.
- **Enable VT-x/AMD-V in the machine's BIOS/UEFI**, or KVM won't load.
- **OS login:** the preseed creates user `freehv` with password `changeme`
  (and disables root login). **Change it** after first boot
  (`passwd`). This is separate from the FreeHV *web* admin password, which is
  auto-generated and printed to the journal.
- **Boot-config paths** (`install.amd/vmlinuz`, `isolinux/txt.cfg`,
  `boot/grub/grub.cfg`) match recent Debian releases. If a future Debian
  changes them, `build-appliance.sh` prints a warning and you may need to
  adjust the kernel paths — the preseed itself stays valid.
- **Validation status:** the ISO-remaster mechanics (extract → inject preseed
  + payload → patch BIOS & UEFI menus → repack with cloned boot records) are
  verified end-to-end. The full unattended *install* can only be confirmed by
  running it against a real Debian netinst ISO on real (or virtual) hardware —
  that's your first end-to-end test. Option A is the lower-risk way to get a
  working appliance immediately.

## How provisioning works (and troubleshooting)

The installer does **not** install the virtualization stack during the Debian
install itself. The installer's chroot has unreliable networking, which can
make `apt-get` fail silently and leave you with an installed box but no FreeHV
service. Instead, the preseed deploys the FreeHV files and registers a
**one-shot first-boot service** (`freehv-firstboot.service`) that runs the full
provisioner the first time the machine boots normally — downloading qemu-kvm,
libvirt, etc. over a working network — then disables itself.

So on first boot the appliance may take a couple of minutes to come up while it
provisions. You can watch it:

```sh
journalctl -u freehv-firstboot -f
```

When it completes, `freehv-manager` will be running on `:5050`.

If you ever need to (re)run provisioning manually on an installed box:

```sh
sudo bash /opt/freehv/appliance/setup.sh
```

## Files

```
setup.sh             idempotent provisioner (used by both options)
preseed.cfg          Debian unattended-install answers
build-appliance.sh   remasters a Debian ISO into the FreeHV installer
```
