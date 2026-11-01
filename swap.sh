#!/bin/bash
# Replace the loaded NVIDIA kernel modules with the ones installed on disk,
# without ending the GNOME session. Runs as root, detached, so it finishes
# even if gnome-shell does not survive.
#
# 1. Hide NVIDIA's EGL and Vulkan registration files, so a process started
#    during the swap does not open the GPU.
# 2. Stop lactd and revoke the GPU handles of every remaining holder
#    (revoke.py).
# 3. If nothing holds the GPU, unload nvidia_drm, nvidia_modeset, nvidia_uvm
#    and nvidia, and load them again from disk.
# 4. Restore the registration files and start lactd again.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
cd /tmp
HIDE=(/usr/share/glvnd/egl_vendor.d/10_nvidia.json
      /usr/share/vulkan/implicit_layer.d/nvidia_layers.json
      /usr/share/vulkan/icd.d/nvidia_icd.json)
NODES="/dev/nvidia* /dev/dri/card0 /dev/dri/renderD129"

log() { echo "$(date +%T.%N | cut -c1-12) $*"; }

restore() {
    for f in "${HIDE[@]}"; do
        [ -e "$f.hotswap" ] && mv "$f.hotswap" "$f" && log "restored $f"
    done
    systemctl start lactd && log "lactd started"
}

holders() { fuser $NODES 2>/dev/null | tr ' ' '\n' | grep -oE '[0-9]+' | sort -un; }

log "BEGIN"
for f in "${HIDE[@]}"; do mv "$f" "$f.hotswap" && log "hid $f"; done
systemctl stop lactd && log "lactd stopped"

for pid in $(holders); do
    log "revoking $pid $(cat /proc/$pid/comm)"
    python3 -I -B $DIR/revoke.py "$pid" 2>&1
done

left=$(holders)
if [ -n "$left" ]; then
    log "still held by: $(for p in $left; do echo -n "$p $(cat /proc/$p/comm); "; done)"
    log "not unloading"
    restore
    log "END (aborted)"
    exit 1
fi
lsmod | grep -E '^nvidia'

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    if rmmod $m 2>&1; then log "unloaded $m"; else log "rmmod $m failed"; break; fi
done
for m in nvidia nvidia_modeset nvidia_drm nvidia_uvm; do
    if modprobe $m 2>&1; then log "loaded $m"; else log "modprobe $m failed"; fi
done

restore
log "patched code present: $(grep -c -P ' (RmDeferNvpcfNotifyIfIdle|RmReplayNvpcfNotify)\t' /proc/kallsyms) of 2 symbols"
head -1 /proc/driver/nvidia/version
lsmod | grep -E '^nvidia'
log "GPU runtime status: $(cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status)"
log "END"
