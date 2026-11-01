#!/bin/bash
# Replace the loaded NVIDIA kernel modules with the ones installed on disk,
# without ending the GNOME session. Run as root.
#
# usage: swap.sh [--check]
#   --check  only report what holds the NVIDIA GPU and whether a swap can go
#            ahead now
#
# The modules can be unloaded only after every process has closed and
# unmapped its NVIDIA devices. swap.sh sorts the processes that hold one:
# - The GPU's KMS node (/dev/dri/cardN) is never revoked. mutter keeps its
#   display state on it, and systemd-logind and systemd hold copies of
#   mutter's descriptor. mutter releases the node after its next display mode
#   set. When gnome-shell, systemd-logind and systemd are the only holders,
#   the swap first runs reapply-layout.py as gnome-shell's user, which applies
#   the current display layout again and so causes that mode set, and waits
#   for the release. Another holder of the node, a monitor connected to the
#   NVIDIA GPU, or a release that does not come stops the swap before it
#   hides files, stops services or revokes handles.
# - A root process may hold the other NVIDIA nodes only as part of one of the
#   services in STOPPABLE, which only watch or tune the GPU and start again
#   cleanly. swap.sh stops those for the swap and starts them again
#   afterwards. Any other root process holding a node, including one in
#   another service, stops the swap: stopping it could end someone's work.
# - Processes not owned by root have their handles on /dev/nvidia* and on
#   the GPU's render node revoked with revoke.py. A revoked process keeps
#   running without access to the NVIDIA GPU until it restarts.
#
# The swap hides NVIDIA's EGL and Vulkan registration files, so that a
# process started during the swap does not open the GPU, unloads nvidia_drm,
# nvidia_modeset, nvidia_uvm and nvidia, loads them from disk, and restores
# the files and the services. On a failure or a signal it exits with a
# non-zero status after trying to restore three things: it loads from disk
# every NVIDIA module that was loaded at the start and is missing, which can
# be a different build from the one it unloaded; it moves the hidden files
# back; and it starts the stopped services. Revocations stay in effect. It
# logs END only when every module it loaded has the build ID of its file on
# disk and the driver contains the patch.
set -u
usage() { echo "usage: swap.sh [--check]" >&2; exit 2; }
check_only=0
case $# in
    0) ;;
    1) [ "$1" = --check ] || usage; check_only=1 ;;
    *) usage ;;
esac
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
cd /tmp

HIDE=(/usr/share/glvnd/egl_vendor.d/10_nvidia.json
      /usr/share/vulkan/implicit_layer.d/nvidia_layers.json
      /usr/share/vulkan/icd.d/nvidia_icd.json)
STOPPABLE=(lactd.service nvidia-persistenced.service nvidia-powerd.service)

log() { echo "$(date +%T.%N | cut -c1-12) $*"; }

holders() {
    [ $# -gt 0 ] || return 0
    fuser "$@" 2>/dev/null | tr ' ' '\n' | grep -oE '[0-9]+' | sort -un
}
uid_of() { awk '/^Uid:/ {print $2}' "/proc/$1/status" 2>/dev/null; }
unit_of() { sed -nE 's#^0::/system\.slice/([^/]+\.service)(/.*)?$#\1#p' "/proc/$1/cgroup" 2>/dev/null; }
describe() { echo "$1 $(cat "/proc/$1/comm" 2>/dev/null) (uid $(uid_of "$1")${2:+, $2})"; }
comm_of() { cat "/proc/$1/comm" 2>/dev/null; }
contains() {
    local x=$1 y
    shift
    for y in "$@"; do [ "$x" = "$y" ] && return 0; done
    return 1
}
# Build ID of a loaded module, and of the file modprobe would load for it.
loaded_build_id() {
    od -An -tx1 -v "/sys/module/$1/notes/.note.gnu.build-id" 2>/dev/null | tr -d ' \n' | tail -c 40
}
disk_build_id() {
    local f t
    f=$(modinfo -n "$1") || return 1
    t=$(mktemp) || return 1
    case "$f" in
        *.zst) zstd -dcq "$f" > "$t" ;;
        *.xz) xz -dc "$f" > "$t" ;;
        *) cat "$f" > "$t" ;;
    esac
    readelf -n "$t" | sed -n 's/.*Build ID: //p'
    rm -f "$t"
}

gpu=$(nvidia_gpu) || { log "cannot find the NVIDIA GPU"; exit 1; }
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
log "NVIDIA GPU $gpu: KMS node ${kms[*]:-missing}, other nodes ${nodes[*]}"

# Sort the current holders.
kms_holders=$(holders "${kms[@]}")
services=() users=() blockers=()
for p in $(holders "${nodes[@]}"); do
    if [ "$(uid_of "$p")" != 0 ]; then
        users+=("$p")
    elif contains "$(unit_of "$p")" "${STOPPABLE[@]}"; then
        services+=("$(unit_of "$p")")
    else
        blockers+=("$p")
    fi
done
mapfile -t services < <(printf '%s\n' "${services[@]}" | sed '/^$/d' | sort -u)

# gnome-shell holds mutter's descriptor on the KMS node, and systemd-logind
# and systemd (PID 1) hold copies of it; reapplying the layout makes mutter
# release it. Any other holder cannot be released that way.
shell="" kms_other=()
for p in $kms_holders; do
    case "$(comm_of "$p")" in
        gnome-shell) if [ -z "$shell" ]; then shell=$p; else kms_other+=("$p"); fi ;;
        systemd-logind) ;;
        systemd) [ "$p" = 1 ] || kms_other+=("$p") ;;
        *) kms_other+=("$p") ;;
    esac
done
kms_blocked=0
if [ -n "$kms_holders" ] && { [ -z "$shell" ] || [ ${#kms_other[@]} -gt 0 ]; }; then
    kms_blocked=1
fi
# A monitor on the NVIDIA GPU keeps the KMS node in use and would lose its
# picture during the swap.
connected=()
for s in /sys/bus/pci/devices/"$gpu"/drm/card*/card*-*/status; do
    [ "$(cat "$s" 2>/dev/null)" = connected ] && connected+=("$(basename "$(dirname "$s")")")
done

for p in $kms_holders; do log "KMS node held by $(describe "$p")"; done
for c in "${connected[@]}"; do log "a monitor is connected to $c on the NVIDIA GPU; disconnect it first"; done
if [ -n "$kms_holders" ] && [ $kms_blocked = 0 ]; then
    log "the swap would first apply the current display layout again, so that mutter releases the KMS node"
fi
for u in "${services[@]}"; do log "service $u holds the GPU; it would be stopped during a swap"; done
for p in "${users[@]}"; do log "handles to revoke: $(describe "$p")"; done
for p in "${blockers[@]}"; do
    u=$(unit_of "$p")
    if [ -n "$u" ]; then
        log "held by $(describe "$p" "$u"); swap.sh does not stop $u, stop it yourself if that is safe"
    else
        log "held by a root process outside a service: $(describe "$p" "$(sed -n 's/^0:://p' "/proc/$p/cgroup")")"
    fi
done

leftovers=()
for f in "${HIDE[@]}"; do
    [ -e "$f.hotswap" ] && leftovers+=("$f.hotswap")
done
for f in "${leftovers[@]}"; do log "left hidden by an earlier swap: $f; move it back first"; done

if [ $kms_blocked = 1 ] || [ ${#connected[@]} -gt 0 ] || [ ${#blockers[@]} -gt 0 ] || [ ${#leftovers[@]} -gt 0 ]; then
    log "a swap cannot go ahead now"
    exit 1
fi
if [ $check_only = 1 ]; then
    log "a swap can go ahead now"
    exit 0
fi

# The swap. The files to hide and the services to stop are recorded in hidden
# and stopped, and the NVIDIA modules loaded at the start in initial. On a
# failure the EXIT trap tries to restore them: it loads from disk every module
# of initial that is missing from /proc/modules (so a module whose rmmod was
# killed after the kernel had unloaded it is loaded too), moves the hidden
# files back and starts the stopped services. INT, TERM and HUP exit through the same trap.
# revoke.py runs in its own session, so that a signal to this script's
# process group cannot stop gdb in the middle of an injected call.
MODULES=(nvidia nvidia_modeset nvidia_drm nvidia_uvm)  # load order
hidden=() stopped=() initial=()
succeeded=0

is_loaded() { grep -q "^$1 " /proc/modules; }
restore() {
    local f u status=0
    for f in "${hidden[@]}"; do
        if [ -e "$f.hotswap" ]; then
            if mv "$f.hotswap" "$f"; then log "restored $f"; else log "could not restore $f"; status=1; fi
        elif [ ! -e "$f" ]; then
            log "$f is missing"; status=1
        fi
    done
    hidden=()
    for u in "${stopped[@]}"; do
        if systemctl start "$u"; then log "started $u"; else log "could not start $u"; status=1; fi
    done
    stopped=()
    return $status
}
cleanup() {
    local status=$? m
    trap - EXIT INT TERM HUP
    [ $succeeded = 1 ] && exit 0
    for m in "${MODULES[@]}"; do
        if contains "$m" "${initial[@]}" && ! is_loaded "$m"; then
            if modprobe "$m"; then log "loaded $m again"; else log "could not load $m again"; fi
        fi
    done
    restore
    log "END (failed)"
    [ $status = 0 ] && status=1
    exit $status
}
fail() { log "$*"; exit 1; }
# Run reapply-layout.py as gnome-shell's user on its session bus, then wait
# up to 10 seconds for mutter to release the KMS node.
release_kms() {
    local uid user home bus i
    uid=$(uid_of "$shell")
    user=$(getent passwd "$uid" | cut -d: -f1)
    home=$(getent passwd "$uid" | cut -d: -f6)
    [ -n "$user" ] || { log "cannot find the user of gnome-shell $shell"; return 1; }
    bus=$(tr '\0' '\n' < "/proc/$shell/environ" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p')
    log "applying the current display layout again as $user, so that mutter releases the KMS node"
    runuser -u "$user" -- env -i HOME="$home" PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="${bus:-unix:path=/run/user/$uid/bus}" \
        /usr/bin/python3 -I "$DIR/reapply-layout.py" 2>&1 || return 1
    for i in $(seq 100); do
        if [ -z "$(holders "${kms[@]}")" ]; then
            log "mutter released the KMS node"
            return 0
        fi
        sleep 0.1
    done
    log "the KMS node is still held by: $(for p in $(holders "${kms[@]}"); do echo -n "$(describe "$p"); "; done)"
    return 1
}
trap cleanup EXIT
trap 'fail "interrupted by SIGINT"' INT
trap 'fail "terminated by SIGTERM"' TERM
trap 'fail "hung up by SIGHUP"' HUP

log "BEGIN"
if [ -n "$kms_holders" ]; then
    release_kms || fail "mutter did not release the KMS node"
fi
for m in "${MODULES[@]}"; do
    is_loaded "$m" && initial+=("$m")
done
# Each change is recorded before it is made, so that a signal arriving
# after the change but before the command returns still gets it restored.
for f in "${HIDE[@]}"; do
    [ -e "$f" ] || continue
    hidden+=("$f")
    mv "$f" "$f.hotswap" || fail "could not hide $f"
    log "hid $f"
done
for u in "${services[@]}"; do
    stopped+=("$u")
    systemctl stop "$u" || fail "could not stop $u"
    log "stopped $u"
done

# Check every remaining holder before revoking any, so that a root holder
# stops the swap before any process has been changed.
for p in $(holders "${nodes[@]}"); do
    [ "$(uid_of "$p")" = 0 ] && fail "still held by root process $(describe "$p")"
done
for p in $(holders "${nodes[@]}"); do
    log "revoking $(describe "$p")"
    setsid --wait python3 -I -B "$DIR/revoke.py" "$p" "${nodes[@]}" 2>&1 || fail "revoking $p failed"
done
left=$(holders "${kms[@]}" "${nodes[@]}")
[ -z "$left" ] || fail "still held by: $(for p in $left; do echo -n "$(describe "$p"); "; done)"
lsmod | grep -E '^nvidia'

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    is_loaded "$m" || continue
    rmmod "$m" || fail "could not unload $m"
    log "unloaded $m"
done
for m in "${MODULES[@]}"; do
    contains "$m" "${initial[@]}" || continue
    modprobe "$m" || fail "could not load $m"
    log "loaded $m"
done

restore || fail "the modules were replaced, but restoring the files or services failed"
head -1 /proc/driver/nvidia/version
lsmod | grep -E '^nvidia'
log "GPU runtime status: $(cat "/sys/bus/pci/devices/$gpu/power/runtime_status")"
for m in "${initial[@]}"; do
    a=$(loaded_build_id "$m")
    b=$(disk_build_id "$m")
    [ -n "$a" ] && [ "$a" = "$b" ] || fail "the loaded $m (build ID ${a:-unknown}) is not the file on disk (${b:-unknown})"
done
log "every loaded module has the build ID of its file on disk"
patched=$(grep -c -P ' (RmDeferNvpcfNotifyIfIdle|RmReplayNvpcfNotify)\t' /proc/kallsyms)
[ "$patched" = 2 ] || fail "the loaded driver lacks the patch ($patched of 2 symbols found)"
log "patched code present"
succeeded=1
log "END"
