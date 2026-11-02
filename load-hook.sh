#!/bin/bash
# Check an NVIDIA driver build, and load nvpcf_hook for it. Run as root.
#
# usage: load-hook.sh                 check the loaded nvidia and load the hook
#        load-hook.sh --check-disk [--no-download]
#                                     check the nvidia module that modprobe
#                                     would load, without loading anything,
#                                     waking the GPU or changing a setting
#        load-hook.sh --fetch VERSION download NVIDIA's source archive for
#                                     VERSION into the cache
#
# nvpcf_hook calls functions inside NVIDIA's driver, so it may attach only to
# a driver build that check_nvidia.py has checked. The checks read three
# inputs, and do not run or extract any code from them:
# - the nvidia module file with the build's GNU build ID: its symbols, its
#   ftrace instrumentation and its calls;
# - the kernel-open source of the build's version, in
#   /usr/src/nvidia-<version>: the C declarations and roles of the functions
#   that the hook uses;
# - NVIDIA's source archive for the version's tag on GitHub, for the source
#   of the precompiled driver core. It holds the dispatcher whose return the
#   hook observes.
# The script keeps the archives in /var/cache/nvpcf-hook. When an archive is
# missing, it downloads it into the cache. With --no-download it exits with
# status 3 instead. The check then could not run, and the status does not
# tell whether the driver would pass it.
#
# The default mode loads nvidia if it is not loaded, reads the loaded build
# ID from /sys/module/nvidia/notes, and checks the file with that build ID:
# the file modprobe would load, or DKMS's copy for the loaded version and
# the running kernel. It stops when it cannot find a file with that build
# ID, because a check of a different file does not cover the loaded driver.
# After the checks pass, it loads nvpcf_hook with expected_nvidia_build_id
# set to the checked build ID, and the hook refuses to attach to any other
# build. The script exits with status 0 only if the hook then reports the
# state active, or the state waiting with its guards installed and GPU
# initialization allowed. If the hook is loaded already, the script only
# checks its state.
#
# The boot unit nvpcf-hook.service and swap.sh run the copy that
# install-hook.sh puts in /usr/local/lib/nvpcf-hook, next to copies of
# check_nvidia.py and nvpcf-hookctl.
set -u
usage() { echo "usage: load-hook.sh [--check-disk [--no-download] | --fetch VERSION]" >&2; exit 2; }
mode=load download=1
case "$*" in
    '') ;;
    --check-disk) mode=check-disk ;;
    '--check-disk --no-download') mode=check-disk download=0 ;;
    *) [ $# = 2 ] && [ "$1" = --fetch ] || usage; mode=fetch ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
CHECKER=$here/check_nvidia.py HOOKCTL=$here/nvpcf-hookctl
[ -e "$CHECKER" ] || CHECKER=$here/hook/tests/check_nvidia.py  # in the repository
[ -e "$HOOKCTL" ] || HOOKCTL=$here/hook/nvpcf-hookctl
CACHE=/var/cache/nvpcf-hook

log() { echo "load-hook.sh: $*"; }
fail() { log "$*"; exit 1; }
temps=()
trap 'rm -f "${temps[@]}"' EXIT
valid_version() { [[ $1 =~ ^[0-9]+(\.[0-9]+){1,3}$ ]]; }
archive_of() { echo "$CACHE/open-gpu-kernel-modules-$1.tar.gz"; }

fetch() {
    local ver=$1 dest tmp
    valid_version "$ver" || fail "not a driver version: '$ver'"
    dest=$(archive_of "$ver")
    [ -s "$dest" ] && return 0
    mkdir -p "$CACHE" && chmod 0755 "$CACHE" || fail "cannot create $CACHE"
    tmp=$(mktemp "$CACHE/.download-XXXXXX") || fail "cannot create a temporary file in $CACHE"
    temps+=("$tmp")
    log "downloading NVIDIA's $ver source archive into $CACHE"
    curl -fsSL --proto '=https' --tlsv1.2 --max-time 600 -o "$tmp" \
        "https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/$ver.tar.gz" ||
        fail "could not download NVIDIA's $ver source archive"
    chmod 0644 "$tmp" && mv "$tmp" "$dest" || fail "could not move NVIDIA's $ver source archive into $CACHE"
}

# Decompress a module file into a temporary file, named in $copy. It runs in
# the script's own shell, so that the EXIT trap removes the file.
module_copy() {
    copy=$(mktemp) || return 1
    temps+=("$copy")
    case "$1" in
        *.zst) zstd -dcq "$1" > "$copy" ;;
        *.xz) xz -dc "$1" > "$copy" ;;
        *) cat "$1" > "$copy" ;;
    esac
}
file_build_id() { readelf -n "$1" 2>/dev/null | sed -n 's/.*Build ID: //p' | head -1 | tr 'A-F' 'a-f'; }
# Print the GNU build ID from an ELF note file, parsing each note's header.
note_build_id() {
    python3 -I -c '
import struct, sys
data = open(sys.argv[1], "rb").read()
offset = 0
while offset + 12 <= len(data):
    namesz, descsz, kind = struct.unpack_from("<III", data, offset)
    offset += 12
    name = data[offset:offset + namesz]
    offset += (namesz + 3) & ~3
    desc = data[offset:offset + descsz]
    offset += (descsz + 3) & ~3
    if offset > len(data) or len(desc) != descsz:
        break
    if kind == 3 and name == b"GNU\0" and 1 <= descsz <= 64:
        print(desc.hex())
        sys.exit(0)
sys.exit(1)' "$1"
}

# Run check_nvidia.py on a module copy ($1) of driver version $2.
check_build() {
    local ko=$1 ver=$2 archive
    valid_version "$ver" || fail "not a driver version: '$ver'"
    [ -d "/usr/src/nvidia-$ver" ] || fail "/usr/src/nvidia-$ver is missing, so NVIDIA $ver's kernel-open source cannot be checked"
    archive=$(archive_of "$ver")
    if [ ! -s "$archive" ]; then
        if [ $download = 0 ]; then
            log "NVIDIA's $ver source archive is not in $CACHE yet, so NVIDIA $ver cannot be checked without a download"
            exit 3
        fi
        fetch "$ver"
    fi
    python3 -I -B "$CHECKER" --module-object "$ko" --kernel-source "/usr/src/nvidia-$ver" --source-tar "$archive" ||
        fail "check_nvidia.py rejects NVIDIA $ver with build ID $(file_build_id "$ko")"
}

hook_loaded() { grep -q '^nvpcf_hook ' /proc/modules; }
# Exit status 0 if the hook's state is active, or waiting with its guards
# installed and GPU initialization allowed.
hook_usable() {
    local s state
    s=$("$HOOKCTL" status 2>&1) || { log "cannot read the status of nvpcf_hook: $s"; return 1; }
    state=$(sed -n 's/^state: //p' <<<"$s")
    log "nvpcf_hook is $state, last_error $(sed -n 's/^last_error: //p' <<<"$s")"
    [ "$state" = active ] && return 0
    [ "$state" = waiting ] && [ "$(sed -n 's/^guards_registered: //p' <<<"$s")" = 1 ] &&
        [ "$(sed -n 's/^initialization_blocked: //p' <<<"$s")" = 0 ]
}

case $mode in
    fetch)
        fetch "$2"
        log "NVIDIA's $2 source archive is in $CACHE"
        exit 0 ;;
    check-disk)
        f=$(modinfo -n nvidia 2>/dev/null) || fail "modprobe cannot find nvidia for kernel $(uname -r)"
        ver=$(modinfo -F version nvidia 2>/dev/null)
        module_copy "$f" || fail "cannot read $f"
        check_build "$copy" "$ver"
        log "NVIDIA $ver in $f passes the checks"
        exit 0 ;;
esac

if hook_loaded; then
    log "nvpcf_hook is loaded already"
    hook_usable
    exit
fi
modprobe nvidia || fail "could not load nvidia"
id=$(note_build_id /sys/module/nvidia/notes/.note.gnu.build-id) || fail "cannot read the build ID of the loaded nvidia"
ver=$(cat /sys/module/nvidia/version 2>/dev/null)
valid_version "$ver" || fail "the loaded nvidia reports '$ver', not a driver version"
ko=""
for f in "$(modinfo -n nvidia 2>/dev/null)" /var/lib/dkms/nvidia/"$ver"/"$(uname -r)"/*/module/nvidia.ko*; do
    [ -f "$f" ] || continue
    module_copy "$f" || continue
    if [ "$(file_build_id "$copy")" = "$id" ]; then
        ko=$copy
        log "the loaded nvidia is $f"
        break
    fi
done
[ -n "$ko" ] || fail "cannot find an installed nvidia module file with the loaded build ID $id, so the loaded driver cannot be checked"
check_build "$ko" "$ver"
modprobe nvpcf_hook "expected_nvidia_build_id=$id" || fail "could not load nvpcf_hook"
hook_usable
