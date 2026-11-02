#!/bin/bash
# Replace the loaded NVIDIA kernel modules with the ones installed on disk,
# without ending the GNOME session. Run as root.
#
# usage: swap.sh [--check]
#   --check  only report what holds the NVIDIA GPU and whether a swap can go
#            ahead now
#
# The modules can be unloaded only after every process has closed its
# NVIDIA devices and removed its memory mappings of them. swap.sh deals
# with the processes that hold one in three ways:
# - swap.sh never revokes handles on the GPU's display device,
#   /dev/dri/cardN (its KMS node). mutter, which runs inside gnome-shell,
#   drives the monitors on the NVIDIA GPU's ports through it, and
#   systemd-logind and systemd hold copies of mutter's descriptor. mutter
#   releases the device the next time it sets the display modes. When
#   gnome-shell, systemd-logind and systemd are the only holders, swap.sh
#   first runs reapply-layout.py as gnome-shell's user. That script applies
#   the current display layout again, which makes mutter set the modes, and
#   swap.sh waits up to 10 seconds for the release. If the device has
#   another holder, if a monitor is connected to the NVIDIA GPU, or if
#   mutter does not release the device, swap.sh exits before it hides
#   files, stops services or revokes handles.
# - The other NVIDIA devices are /dev/nvidia*, /dev/nvidia-caps/* and the
#   GPU's render node. swap.sh accepts a root process as a holder of these
#   only if the process belongs to one of the services in STOPPABLE. Those
#   services only watch or tune the GPU and start again cleanly, so swap.sh
#   stops them for the swap and starts them again afterwards. swap.sh
#   refuses to swap when any other root process holds one of these devices,
#   including a process in another service, because stopping that process
#   could end someone's work.
# - For processes not owned by root, revoke.py revokes their handles on
#   these devices. A revoked process keeps running, but cannot use the
#   NVIDIA GPU until it restarts.
#
# Each swap makes mutter ask rtkit-daemon several times to take its KMS
# thread (the thread that sends display updates to the kernel) out of
# real-time scheduling and to make it real-time again. mutter takes the
# thread out while it reads a display device's state, sets its modes or
# disables it. rtkit-daemon by default allows 25 such requests per user in
# 20 seconds and refuses the rest. After a refusal the thread stays
# real-time during those calls. mutter sets RLIMIT_RTTIME to rtkit-daemon's
# RTTimeUSecMax, 200 ms by default, so the kernel kills gnome-shell if the
# real-time thread uses the CPU for 200 ms without blocking. swap.sh
# therefore records when each swap ends, and starts the next swap only
# after GAP seconds have passed since then.
#
# Every swap needs nvpcf_hook (hook/README.md), whichever fix for the
# wake-ups the driver on disk has. The hook guards NVIDIA's adapter
# shutdown. Without that guard, a firmware notification that arrives while
# NVIDIA shuts the adapter down after the last close can make the shutdown
# and the notification's handler wait for each other. Patch 0001 does not
# prevent that, because the patched driver takes the same lock before it
# defers a notification. swap.sh therefore refuses unless nvpcf_hook is
# loaded, installed for the running kernel, and in a state that allows a
# swap. It also refuses when the driver it would load fails the checks of
# the hook's loader, load-hook.sh, which it runs before any change. A swap
# runs the hook's prepare before any change, and its detach once the other
# holders are gone. It unloads NVIDIA while the hook's guard remains, then
# unloads the hook. It loads nvidia, then the hook through load-hook.sh, and
# the other NVIDIA modules only once the hook's guard is in place.
#
# Once the display device is free, a swap hides the files that register
# NVIDIA's EGL and Vulkan libraries with their loaders (HIDE), so that these
# loaders do not find the libraries during the swap. GLX, a program's own
# dlopen and direct opens of /dev/nvidia*, as by nvidia-smi, do not use
# these files. A process that holds /dev/nvidia* open when the modules are
# unloaded makes rmmod fail, and the swap stops. After hiding the files, a
# swap stops the services, revokes the handles, unloads nvidia_drm,
# nvidia_modeset, nvidia_uvm and nvidia, loads them from disk, and restores
# the files and the services. On a failure or a signal, it tries to restore
# the modules, the files and the services, and exits with a non-zero status.
# It loads from disk every NVIDIA module that was loaded at the start and is
# now missing, so a module it loads again can be a different build from the
# one it unloaded. Revoked handles stay revoked. It logs END only when every
# module it loaded, nvpcf_hook included, has the same build ID as its file
# on disk, and nvpcf_hook reports the state active. If the nvidia module on
# disk has patch 0001, the loaded driver must also contain
# RmDeferNvpcfNotifyIfIdle and RmReplayNvpcfNotify, two of the functions
# that the patch adds.
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
STAMP=/run/nvidia-live-swap.last  # when the previous swap ended (date +%s)
GAP=20                            # rtkit-daemon's --actions-burst-sec default
HOOK_WAIT=15                      # seconds for nvpcf_hook to become active
LOADER=/usr/local/lib/nvpcf-hook/load-hook.sh  # installed by install-hook.sh

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
# Decompress the file that modprobe would load for a module into a temporary
# file, and print the temporary file's name.
disk_module_copy() {
    local f t
    f=$(modinfo -n "$1" 2>/dev/null) || return 1
    t=$(mktemp) || return 1
    case "$f" in
        *.zst) zstd -dcq "$f" > "$t" ;;
        *.xz) xz -dc "$f" > "$t" ;;
        *) cat "$f" > "$t" ;;
    esac || { rm -f "$t"; return 1; }
    echo "$t"
}
disk_build_id() {
    local t
    t=$(disk_module_copy "$1") || return 1
    readelf -n "$t" | sed -n 's/.*Build ID: //p'
    rm -f "$t"
}
# Exit status 0 if the nvidia module on disk contains patch 0001, 1 if it
# does not, and 2 if the module cannot be read.
disk_has_patch() {
    local t syms
    t=$(disk_module_copy nvidia) || return 2
    syms=$(nm "$t" 2>/dev/null) || { rm -f "$t"; return 2; }
    rm -f "$t"
    grep -q ' RmDeferNvpcfNotifyIfIdle$' <<<"$syms"
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
log "NVIDIA GPU $gpu: display device ${kms[*]:-missing}, other devices ${nodes[*]}"

# Find the holders of the display device. Group the holders of the other
# devices into processes not owned by root, services to stop, and root
# processes that make swap.sh refuse.
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

# mutter runs inside gnome-shell, so gnome-shell holds mutter's descriptor
# on the display device, and systemd-logind and systemd (PID 1) hold copies
# of it. After the layout is applied again, all three close the device.
# swap.sh refuses when the device has any other holder, because applying
# the layout again would not make that holder close it.
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
# A monitor connected to the NVIDIA GPU keeps the display device in use, and
# it would lose its picture during the swap.
connected=()
for s in /sys/bus/pci/devices/"$gpu"/drm/card*/card*-*/status; do
    [ "$(cat "$s" 2>/dev/null)" = connected ] && connected+=("$(basename "$(dirname "$s")")")
done

for p in $kms_holders; do log "display device held by $(describe "$p")"; done
for c in "${connected[@]}"; do log "a monitor is connected to $c on the NVIDIA GPU; disconnect it first"; done
if [ -n "$kms_holders" ] && [ $kms_blocked = 0 ]; then
    log "the swap would first apply the current display layout again, so that mutter releases the display device"
fi
for u in "${services[@]}"; do log "service $u holds the GPU; it would be stopped during a swap"; done
for p in "${users[@]}"; do log "handles to revoke: $(describe "$p")"; done
for p in "${blockers[@]}"; do
    u=$(unit_of "$p")
    if [ -n "$u" ]; then
        log "the GPU is held by $(describe "$p" "$u"); swap.sh does not stop $u, so stop it yourself if that is safe"
    else
        log "the GPU is held by a root process outside a service: $(describe "$p" "$(sed -n 's/^0:://p' "/proc/$p/cgroup")")"
    fi
done

wait_s=0
if last=$(cat "$STAMP" 2>/dev/null) && [[ $last =~ ^[0-9]+$ ]]; then
    age=$(( $(date +%s) - last ))
    if [ "$age" -ge 0 ] && [ "$age" -lt $GAP ]; then
        wait_s=$(( GAP - age ))
        log "the previous swap ended $age s ago, so the swap would first wait $wait_s s"
    fi
fi

leftovers=()
for f in "${HIDE[@]}"; do
    [ -e "$f.hotswap" ] && leftovers+=("$f.hotswap")
done
for f in "${leftovers[@]}"; do log "left hidden by an earlier swap: $f; move it back first"; done

hook_problems=()
disk_has_patch
patch_rc=$?
case $patch_rc in
    0) log "the nvidia module on disk contains patch 0001" ;;
    1) log "the nvidia module on disk lacks patch 0001" ;;
    *) hook_problems+=("cannot read the nvidia module that modprobe would load") ;;
esac
modinfo -n nvpcf_hook >/dev/null 2>&1 ||
    hook_problems+=("nvpcf_hook is not installed for kernel $(uname -r), so swap.sh could not load it after the swap")
if [ ! -x "$LOADER" ]; then
    hook_problems+=("$LOADER is missing; install-hook.sh installs it")
else
    # Check the driver that the swap would load before changing anything, so
    # that a driver which the hook cannot use is refused here. --check does
    # not download NVIDIA's source archive; a swap downloads it first.
    if [ $check_only = 1 ]; then
        disk_check=$("$LOADER" --check-disk --no-download 2>&1)
    else
        disk_check=$("$LOADER" --check-disk 2>&1)
    fi
    case $? in
        0) log "the nvidia module on disk passes nvpcf_hook's checks" ;;
        3) log "$disk_check; a swap would download it first" ;;
        *) hook_problems+=("the nvidia module on disk does not pass nvpcf_hook's checks: $disk_check") ;;
    esac
fi
if ! hook_loaded; then
    hook_problems+=("nvpcf_hook is not loaded; load it with $LOADER, which checks the loaded driver first")
elif ! hook_status=$(hookctl status 2>&1); then
    hook_problems+=("cannot read the status of nvpcf_hook: $hook_status")
else
    log "$(hook_summary)"
    hook_state=$(sed -n 's/^state: //p' <<<"$hook_status")
    case "$hook_state" in
        active|waiting|guard-only|quiesced) ;;
        starting|suspended) hook_problems+=("nvpcf_hook is $hook_state; check again in a few seconds") ;;
        unavailable) hook_problems+=("nvpcf_hook is unavailable with last_error $(sed -n 's/^last_error: //p' <<<"$hook_status"); its status and the kernel log say why, and swap.sh does not acknowledge a loss") ;;
        *) hook_problems+=("nvpcf_hook is ${hook_state:-in an unknown state}, so it does not guard a swap; run $DIR/hook/nvpcf-hookctl resume, or unload it and load it again with $LOADER") ;;
    esac
fi
for x in "${hook_problems[@]}"; do log "$x"; done

if [ $kms_blocked = 1 ] || [ ${#connected[@]} -gt 0 ] || [ ${#blockers[@]} -gt 0 ] || [ ${#leftovers[@]} -gt 0 ] ||
   [ ${#hook_problems[@]} -gt 0 ]; then
    log "a swap cannot go ahead now"
    exit 1
fi
if [ $check_only = 1 ]; then
    log "a swap can go ahead now"
    exit 0
fi

# The rest of the script does the swap. It records the files it hides in
# the array hidden, the services it stops in the array stopped, and the
# NVIDIA modules loaded at the start in the array initial. On a failure, the
# EXIT trap tries to restore all three: it loads from disk every module in
# initial that is missing from /proc/modules (this also reloads a module
# whose rmmod was killed after the kernel had already unloaded it), moves
# the hidden files back and starts the stopped services. SIGINT, SIGTERM
# and SIGHUP make the script exit, which runs the same trap. revoke.py runs
# in its own session, so that a signal to this script's process group
# cannot stop gdb in the middle of a call it injected into the target
# process.
#
# nvpcf_hook has to guard NVIDIA's shutdown before anything makes NVIDIA
# initialize the GPU, because a failed initialization ends in that shutdown.
# Loading nvidia_drm initializes the GPU: it sets up an NVKMS device, and
# frees it again if its setup fails. A started service such as lactd does
# too. So the swap and the trap load nvidia first, then load or resume the
# hook, and check from its status that its guard is installed and
# initialization is allowed. Only then do they load the other modules and
# start the services. If the hook does not reach that state, the trap leaves
# those modules unloaded and the services stopped, and reports them. After
# its detach, the hook refuses initialization until it resumes.
# hook_detached records the detach before it runs, so that the trap resumes
# the hook.
MODULES=(nvidia nvidia_modeset nvidia_drm nvidia_uvm)  # load order
hidden=() stopped=() initial=()
hook_detached=0
succeeded=0

is_loaded() { grep -q "^$1 " /proc/modules; }
restore_files() {
    local f status=0
    for f in "${hidden[@]}"; do
        if [ -e "$f.hotswap" ]; then
            if mv "$f.hotswap" "$f"; then log "restored $f"; else log "could not restore $f"; status=1; fi
        elif [ ! -e "$f" ]; then
            log "$f is missing"; status=1
        fi
    done
    hidden=()
    return $status
}
restore_services() {
    local u status=0
    for u in "${stopped[@]}"; do
        if systemctl start "$u"; then log "started $u"; else log "could not start $u"; status=1; fi
    done
    stopped=()
    return $status
}
restore() {
    local status=0
    restore_files || status=1
    restore_services || status=1
    return $status
}
# Exit status 0 if nvpcf_hook reports its shutdown guard installed and GPU
# initialization allowed, as in the states waiting and active.
hook_guarding() {
    local s
    s=$(hookctl status 2>/dev/null) || return 1
    case "$(sed -n 's/^state: //p' <<<"$s")" in
        active|waiting) ;;
        *) return 1 ;;
    esac
    [ "$(sed -n 's/^guards_registered: //p' <<<"$s")" = 1 ] &&
        [ "$(sed -n 's/^initialization_blocked: //p' <<<"$s")" = 0 ]
}
cleanup() {
    local status=$? m missing=()
    trap - EXIT INT TERM HUP
    date +%s > "$STAMP" || log "could not write $STAMP"
    [ $succeeded = 1 ] && exit 0
    if contains nvidia "${initial[@]}" && ! is_loaded nvidia; then
        if modprobe nvidia; then log "loaded nvidia again"; else log "could not load nvidia again"; fi
    fi
    if is_loaded nvidia; then
        if ! hook_loaded; then
            if "$LOADER" 2>&1; then log "loaded nvpcf_hook again"; else log "could not load nvpcf_hook again"; fi
        elif [ $hook_detached = 1 ]; then
            if hookctl resume 2>&1; then log "resumed nvpcf_hook"; else log "could not resume nvpcf_hook"; fi
        fi
    fi
    if hook_guarding; then
        for m in "${MODULES[@]}"; do
            if contains "$m" "${initial[@]}" && ! is_loaded "$m"; then
                if modprobe "$m"; then log "loaded $m again"; else log "could not load $m again"; fi
            fi
        done
        restore
    else
        restore_files
        for m in "${MODULES[@]}"; do
            if contains "$m" "${initial[@]}" && ! is_loaded "$m"; then missing+=("$m"); fi
        done
        log "nvpcf_hook does not guard NVIDIA's shutdown, so swap.sh does not make NVIDIA initialize the GPU"
        [ ${#missing[@]} -eq 0 ] || log "not loaded again: ${missing[*]}"
        [ ${#stopped[@]} -eq 0 ] || log "not started again: ${stopped[*]}"
    fi
    log "$(hook_summary)"
    log "END (failed)"
    [ $status = 0 ] && status=1
    exit $status
}
fail() { log "$*"; exit 1; }
# Run reapply-layout.py as gnome-shell's user, on gnome-shell's session bus,
# then wait up to 10 seconds for mutter to release the display device.
release_kms() {
    local uid user home bus i
    uid=$(uid_of "$shell")
    user=$(getent passwd "$uid" | cut -d: -f1)
    home=$(getent passwd "$uid" | cut -d: -f6)
    [ -n "$user" ] || { log "cannot find the user of gnome-shell $shell"; return 1; }
    bus=$(tr '\0' '\n' < "/proc/$shell/environ" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p')
    log "applying the current display layout again as $user, so that mutter releases the display device"
    runuser -u "$user" -- env -i HOME="$home" PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="${bus:-unix:path=/run/user/$uid/bus}" \
        /usr/bin/python3 -I "$DIR/reapply-layout.py" 2>&1 || return 1
    for i in $(seq 100); do
        if [ -z "$(holders "${kms[@]}")" ]; then
            log "mutter released the display device"
            return 0
        fi
        sleep 0.1
    done
    log "the display device is still held by: $(for p in $(holders "${kms[@]}"); do echo -n "$(describe "$p"); "; done)"
    return 1
}
trap cleanup EXIT
trap 'fail "interrupted by SIGINT"' INT
trap 'fail "terminated by SIGTERM"' TERM
trap 'fail "hung up by SIGHUP"' HUP

log "BEGIN"
if [ $wait_s -gt 0 ]; then
    log "waiting $wait_s s after the previous swap"
    sleep "$wait_s"
fi
hookctl prepare 2>&1 || fail "nvpcf_hook's prepare failed, so the swap stops before it changes anything: $(hook_summary)"
log "prepared $(hook_summary)"
if [ -n "$kms_holders" ]; then
    release_kms || fail "mutter did not release the display device"
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
hook_detached=1
hookctl detach 2>&1 || fail "nvpcf_hook's detach failed: $(hook_summary)"
log "detached $(hook_summary)"
lsmod | grep -E '^nvidia'

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    is_loaded "$m" || continue
    rmmod "$m" || fail "could not unload $m"
    log "unloaded $m"
done
rmmod nvpcf_hook || fail "could not unload nvpcf_hook after NVIDIA: $(hook_summary)"
hook_detached=0  # the detached instance is gone; the trap loads a new one
log "unloaded nvpcf_hook"
for m in "${MODULES[@]}"; do
    contains "$m" "${initial[@]}" || continue
    modprobe "$m" || fail "could not load $m"
    log "loaded $m"
    if [ "$m" = nvidia ]; then
        "$LOADER" 2>&1 || fail "could not load nvpcf_hook for the new driver, so the swap stops before the modules that initialize the GPU: $(hook_summary)"
        hook_guarding || fail "nvpcf_hook does not guard NVIDIA's shutdown, so the swap stops before the modules that initialize the GPU: $(hook_summary)"
        log "loaded $(hook_summary)"
    fi
done

restore || fail "the modules were replaced, but restoring the files or services failed"
head -1 /proc/driver/nvidia/version
lsmod | grep -E '^nvidia'
log "GPU runtime status: $(cat "/sys/bus/pci/devices/$gpu/power/runtime_status")"
for m in "${initial[@]}" nvpcf_hook; do
    a=$(loaded_build_id "$m")
    b=$(disk_build_id "$m")
    [ -n "$a" ] && [ "$a" = "$b" ] || fail "the loaded $m (build ID ${a:-unknown}) is not the file on disk (${b:-unknown})"
done
log "every loaded module has the build ID of its file on disk"
if [ $patch_rc = 0 ]; then
    patched=$(grep -c -P ' (RmDeferNvpcfNotifyIfIdle|RmReplayNvpcfNotify)\t' /proc/kallsyms)
    [ "$patched" = 2 ] || fail "the loaded driver lacks the patch ($patched of 2 symbols found)"
    log "the loaded driver contains the patch"
fi
# The hook attaches once something initializes the GPU, usually gnome-shell
# opening the new display device or a restarted service.
for _ in $(seq $((HOOK_WAIT * 2))); do
    [ "$(hookctl status 2>/dev/null | sed -n 's/^state: //p')" = active ] && break
    sleep 0.5
done
hook_status=$(hookctl status 2>&1) || fail "cannot read the status of nvpcf_hook after the swap: $hook_status"
[ "$(sed -n 's/^state: //p' <<<"$hook_status")" = active ] ||
    fail "nvpcf_hook is not active $HOOK_WAIT s after the swap: $(hook_summary)"
log "$(hook_summary)"
succeeded=1
log "END"
