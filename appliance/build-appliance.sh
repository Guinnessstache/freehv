#!/usr/bin/env bash
#
# build-appliance.sh — remaster a Debian netinst ISO into an unattended
# FreeHV installer ISO.
#
# It injects the preseed and the FreeHV repo onto the ISO, patches the boot
# menus to auto-start the preseeded install, and repacks while cloning the
# original El Torito boot records (so the result still boots on BIOS + UEFI).
#
# Usage:
#   ./build-appliance.sh <debian-netinst.iso> [output.iso]
#
# Requires: xorriso. Run on a Linux box (your WSL works fine).

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-freehv-installer.iso}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"     # the FreeHV repo root (manager + appliance)
PRESEED="$SCRIPT_DIR/preseed.cfg"

log(){ printf '\033[1;33m[build]\033[0m %s\n' "$*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }

[[ -n "$INPUT" ]] || die "usage: $0 <debian-netinst.iso> [output.iso]"
[[ -f "$INPUT" ]] || die "input ISO not found: $INPUT"
[[ -f "$PRESEED" ]] || die "preseed.cfg not found next to this script"
command -v xorriso >/dev/null || die "xorriso is required (apt install xorriso)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log "Working in $WORK"

# --- 1. extract the ISO ---------------------------------------------------
log "Extracting $INPUT …"
xorriso -osirrox on -indev "$INPUT" -extract / "$WORK/iso" >/dev/null 2>&1
chmod -R u+w "$WORK/iso"

# --- 2. inject preseed + FreeHV payload -----------------------------------
log "Injecting preseed and FreeHV payload…"
cp "$PRESEED" "$WORK/iso/preseed.cfg"
# Stage ONLY the files the appliance needs, via an explicit allowlist. This is
# deliberately strict: the repo root in CI also contains the downloaded Debian
# ISO, .git metadata, checksums, and prior build output, none of which belong
# on the installer media. Copying the whole repo would bloat and corrupt the
# payload, so we copy just the daemon, the appliance scripts, and the license.
mkdir -p "$WORK/iso/freehv"
PAYLOAD_ITEMS=( freehv-manager appliance LICENSE )
for item in "${PAYLOAD_ITEMS[@]}"; do
  if [[ ! -e "$REPO_DIR/$item" ]]; then
    die "expected payload item missing from repo: $item"
  fi
  cp -a "$REPO_DIR/$item" "$WORK/iso/freehv/$item"
done
# Scrub anything that shouldn't ship even from within those dirs.
find "$WORK/iso/freehv" \( -name '__pycache__' -o -name '*.pyc' \
     -o -name '*.iso' -o -name 'auth.json' -o -name '*.qcow2' \) \
     -exec rm -rf {} + 2>/dev/null || true
log "staged payload: ${PAYLOAD_ITEMS[*]} ($(du -sh "$WORK/iso/freehv" | cut -f1))"

# --- 3. patch the boot menus to auto-run the preseed ----------------------
# The exact boot-config paths vary slightly between Debian releases, so we
# patch every variant we find. The kernel params make the installer pick up
# the preseed automatically and run non-interactively.
AUTO_PARAMS="auto=true priority=critical preseed/file=/cdrom/preseed.cfg debian-installer/locale=en_US.UTF-8 keyboard-configuration/xkb-keymap=us"

patched=0
# 3a. isolinux (BIOS). Prepend an auto entry and make it the default.
for cfg in "$WORK"/iso/isolinux/txt.cfg "$WORK"/iso/isolinux/gtk.cfg; do
  [[ -f "$cfg" ]] || continue
  {
    echo "default freehvauto"
    echo "label freehvauto"
    echo "    menu label ^FreeHV automated install"
    echo "    kernel /install.amd/vmlinuz"
    echo "    append vga=788 initrd=/install.amd/initrd.gz $AUTO_PARAMS --- quiet"
    cat "$cfg"
  } > "$cfg.new" && mv "$cfg.new" "$cfg"
  patched=1
  log "patched $(basename "$(dirname "$cfg")")/$(basename "$cfg")"
done
# Shorten the isolinux timeout if present.
[[ -f "$WORK/iso/isolinux/isolinux.cfg" ]] && \
  sed -i 's/^timeout .*/timeout 30/' "$WORK/iso/isolinux/isolinux.cfg" || true

# 3b. GRUB (UEFI). Insert a default auto entry at the top of the menu.
for grub in "$WORK"/iso/boot/grub/grub.cfg; do
  [[ -f "$grub" ]] || continue
  cat > "$WORK/grub.head" <<EOF
set default=0
set timeout=3
menuentry "FreeHV automated install" {
    set background_color=black
    linux    /install.amd/vmlinuz $AUTO_PARAMS --- quiet
    initrd   /install.amd/initrd.gz
}
EOF
  cat "$grub" >> "$WORK/grub.head"
  mv "$WORK/grub.head" "$grub"
  patched=1
  log "patched boot/grub/grub.cfg"
done

[[ $patched -eq 1 ]] || log "WARNING: no known boot configs found to patch — \
the ISO will still contain the preseed, but you may need to add \
'$AUTO_PARAMS' to the boot command line manually."

# --- 4. repack, cloning the original boot setup ---------------------------
log "Repacking → $OUTPUT (cloning original boot records)…"
xorriso -indev "$INPUT" -outdev "$OUTPUT" \
        -boot_image any replay \
        -volid "FREEHV_INSTALL" \
        -overwrite on \
        -update_r "$WORK/iso" / \
        -commit >/dev/null

# isohybrid for USB booting on BIOS systems (best-effort; replay usually
# already handles this on modern Debian images).
if command -v isohybrid >/dev/null 2>&1; then
  isohybrid "$OUTPUT" 2>/dev/null || true
fi

SIZE="$(du -h "$OUTPUT" | cut -f1)"
log "Done. Wrote $OUTPUT ($SIZE)."
cat <<EOF

  Write it to a USB stick (this ERASES the stick):
      sudo dd if=$OUTPUT of=/dev/sdX bs=4M status=progress oflag=sync

  Boot the target machine from the USB. It will install Debian + KVM + FreeHV
  unattended, then reboot into the appliance. The FreeHV console will be at
      http://<appliance-ip>:5050
  Get the initial admin password on the appliance with:
      journalctl -u freehv-manager | grep 'Initial admin password'

EOF
