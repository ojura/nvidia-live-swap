#!/bin/bash
# Replace the loaded NVIDIA kernel modules with the ones installed on disk,
# without ending the GNOME session. Run as root.
#
# usage: swap.sh [--check]
#   --check  report what holds the NVIDIA GPU and whether a swap can go
#            ahead now, and change nothing
#
# The modules can be unloaded only when no process has an NVIDIA device open
# or mapped. swap.sh sorts the processes that do:
# - The GPU's KMS node (/dev/dri/cardN) is never revoked. mutter keeps its
#   display state on it, and systemd-logind and systemd hold copies of
#   mutter's descriptor. mutter releases the node after its next display mode
#   set; until then swap.sh stops without changing anything.
# - A root process may hold the other NVIDIA nodes only as part of a systemd
#   system service, such as lactd. swap.sh stops those services for the swap
#   and starts them again afterwards. Any other root process holding a node
#   stops the swap.
# - Processes not owned by root have their handles on /dev/nvidia* and on
#   the GPU's render node revoked with revoke.py.
#
# The swap hides NVIDIA's EGL and Vulkan registration files, so that a
# process started during the swap does not open the GPU, unloads nvidia_drm,
# nvidia_modeset, nvidia_uvm and nvidia, loads them from disk, and restores
# the files and the services. If the GPU is still held after the revokes, it
# restores everything and stops before unloading.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
cd /tmp

check_only=0
[ "${1:-}" = --check ] && check_only=1

HIDE=(/usr/share/glvnd/egl_vendor.d/10_nvidia.json
      /usr/share/vulkan/implicit_layer.d/nvidia_layers.json
      /usr/share/vulkan/icd.d/nvidia_icd.json)

log() { echo "$(date +%T.%N | cut -c1-12) $*"; }

holders() {
    [ $# -gt 0 ] || return 0
    fuser "$@" 2>/dev/null | tr ' ' '\n' | grep -oE '[0-9]+' | sort -un
}
uid_of() { awk '/^Uid:/ {print $2}' "/proc/$1/status" 2>/dev/null; }
unit_of() { sed -nE 's#^0::/system\.slice/([^/]+\.service)(/.*)?$#\1#p' "/proc/$1/cgroup" 2>/dev/null; }
describe() { echo "$1 $(cat "/proc/$1/comm" 2>/dev/null) (uid $(uid_of "$1")${2:+, $2})"; }

gpu=$(nvidia_gpu) || { log "no NVIDIA GPU found"; exit 1; }
kms=() render=()
for n in /sys/bus/pci/devices/"$gpu"/drm/*; do
    case "${n##*/}" in
        card*) kms+=("/dev/dri/${n##*/}") ;;
        renderD*) render+=("/dev/dri/${n##*/}") ;;
    esac
done
nodes=()
for n in /dev/nvidia* /dev/nvidia-caps/*; do
    [ -c "$n" ] && nodes+=("$n")
done
nodes+=("${render[@]}")
log "NVIDIA GPU $gpu: KMS node ${kms[*]:-none}, other nodes ${nodes[*]}"

# Sort the current holders.
kms_holders=$(holders "${kms[@]}")
services=() users=() blockers=()
for p in $(holders "${nodes[@]}"); do
    if [ "$(uid_of "$p")" = 0 ]; then
        u=$(unit_of "$p")
        if [ -n "$u" ]; then
            services+=("$u")
        else
            blockers+=("$p")
        fi
    else
        users+=("$p")
    fi
done
mapfile -t services < <(printf '%s\n' "${services[@]}" | sed '/^$/d' | sort -u)

for p in $kms_holders; do log "KMS node held by $(describe "$p")"; done
for u in "${services[@]}"; do log "service $u holds the GPU; it is stopped for the swap"; done
for p in "${users[@]}"; do log "handles to revoke: $(describe "$p")"; done
for p in "${blockers[@]}"; do log "held by a root process outside a system service: $(describe "$p" "$(sed -n 's/^0:://p' "/proc/$p/cgroup")")"; done

if [ -n "$kms_holders" ] || [ ${#blockers[@]} -gt 0 ]; then
    [ -n "$kms_holders" ] && log "mutter releases the KMS node after its next display mode set"
    log "a swap cannot go ahead now"
    exit 1
fi
if [ $check_only = 1 ]; then
    log "a swap can go ahead now"
    exit 0
fi

# The swap.
stopped=()
restore() {
    local f u
    for f in "${HIDE[@]}"; do
        [ -e "$f.hotswap" ] && mv "$f.hotswap" "$f" && log "restored $f"
    done
    for u in "${stopped[@]}"; do
        systemctl start "$u" && log "started $u"
    done
}
abort() {
    log "$*"
    restore
    log "END (aborted, modules not unloaded)"
    exit 1
}

log "BEGIN"
for f in "${HIDE[@]}"; do
    [ -e "$f" ] && mv "$f" "$f.hotswap" && log "hid $f"
done
for u in "${services[@]}"; do
    systemctl stop "$u" && stopped+=("$u") && log "stopped $u"
done

# Check every remaining holder before revoking any, so that a root holder
# stops the swap before any process has been changed.
for p in $(holders "${nodes[@]}"); do
    if [ "$(uid_of "$p")" = 0 ]; then
        abort "still held by root process $(describe "$p")"
    fi
done
for p in $(holders "${nodes[@]}"); do
    log "revoking $(describe "$p")"
    python3 -I -B "$DIR/revoke.py" "$p" "${nodes[@]}" 2>&1 || abort "revoking $p failed"
done

left=$(holders "${kms[@]}" "${nodes[@]}")
if [ -n "$left" ]; then
    abort "still held by: $(for p in $left; do echo -n "$(describe "$p"); "; done)"
fi
lsmod | grep -E '^nvidia'

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    if rmmod "$m" 2>&1; then log "unloaded $m"; else log "rmmod $m failed"; break; fi
done
for m in nvidia nvidia_modeset nvidia_drm nvidia_uvm; do
    if modprobe "$m" 2>&1; then log "loaded $m"; else log "modprobe $m failed"; fi
done

restore
log "patched code present: $(grep -c -P ' (RmDeferNvpcfNotifyIfIdle|RmReplayNvpcfNotify)\t' /proc/kallsyms) of 2 symbols"
head -1 /proc/driver/nvidia/version
lsmod | grep -E '^nvidia'
log "GPU runtime status: $(cat "/sys/bus/pci/devices/$gpu/power/runtime_status")"
log "END"
