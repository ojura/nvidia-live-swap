#!/bin/bash
# Check an NVIDIA driver build, and load nvpcf_hook for it. Run as root.
#
# usage: load-hook.sh                 check the loaded nvidia and load the hook
#        load-hook.sh --check-loaded  exit with status 0 if the loaded hook
#                                     guards NVIDIA's shutdown and lets NVIDIA
#                                     initialize the GPU
#        load-hook.sh --check-disk [--no-download]
#                                     check the nvidia module that modprobe
#                                     would load, without loading anything,
#                                     waking the GPU or changing a setting;
#                                     with --no-download, exit with status 3
#                                     instead of downloading a missing archive
#        load-hook.sh --fetch VERSION download NVIDIA's source archive for
#                                     VERSION into the cache
#        load-hook.sh --build-id FILE print the GNU build ID of a module file
#        load-hook.sh --loaded-build-id MODULE
#                                     print the GNU build ID of a loaded module
#
# nvpcf_hook calls functions inside NVIDIA's driver, so it may attach only to
# a driver build that check_nvidia.py has checked: the module file with the
# build's GNU build ID, the version's source in /usr/src/nvidia-<version>, and
# NVIDIA's source archive for the version, which the script keeps in
# /var/cache/nvpcf-hook. The hook then refuses to attach to any other build.
# WRITEUP.md describes the modes. nvpcf-hook.service and swap.sh run the copy
# that install-hook.sh puts in /usr/local/lib/nvpcf-hook.
set -u
usage() {
    echo "usage: load-hook.sh [--check-loaded | --check-disk [--no-download] | --fetch VERSION |" >&2
    echo "                     --build-id FILE | --loaded-build-id MODULE]" >&2
    exit 2
}
mode=load download=1 arg=
case "$*" in
    '') ;;
    --check-loaded) mode=check-loaded ;;
    --check-disk) mode=check-disk ;;
    '--check-disk --no-download') mode=check-disk download=0 ;;
    *)
        [ $# = 2 ] || usage
        case $1 in
            --fetch) mode=fetch ;;
            --build-id) mode=build-id ;;
            --loaded-build-id) [[ $2 =~ ^[A-Za-z0-9_]+$ ]] || usage; mode=loaded-build-id ;;
            *) usage ;;
        esac
        arg=$2 ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
CHECKER=$here/check_nvidia.py HOOKCTL=$here/nvpcf-hookctl
[ -e "$CHECKER" ] || CHECKER=$here/hook/tests/check_nvidia.py  # in the repository
[ -e "$HOOKCTL" ] || HOOKCTL=$here/hook/nvpcf-hookctl
CACHE=/var/cache/nvpcf-hook

# Messages go to stderr, so that the build ID is all that --build-id and
# --loaded-build-id print on stdout.
log() { echo "load-hook.sh: $*" >&2; }
fail() { log "$*"; exit 1; }
temps=()
trap 'rm -f "${temps[@]}"' EXIT
# One file for the build ID notes that copy_build_id extracts. It is made
# here, in the script's own shell, so that the EXIT trap removes it also
# when copy_build_id runs in a command substitution.
notes=$(mktemp) || { echo "load-hook.sh: cannot create a temporary file" >&2; exit 1; }
temps+=("$notes")
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
# the script's own shell, so that the EXIT trap removes the file. The name
# ends in .ko, because modinfo reads a file only by such a name.
module_copy() {
    copy=$(mktemp --suffix=.ko) || return 1
    temps+=("$copy")
    case "$1" in
        *.zst) zstd -dcq "$1" > "$copy" ;;
        *.xz) xz -dc "$1" > "$copy" ;;
        *) cat "$1" > "$copy" ;;
    esac
}
# Print the GNU build ID of a module copy. objcopy copies the bytes of its
# .note.gnu.build-id section into $notes, and note_build_id parses them, as it
# parses a loaded module's note. readelf cannot be used: GNU ld writes an ID
# whose length is not a multiple of four without padding after it, and readelf
# 2.42 then leaves the ID out. For a file without the section, objcopy exits
# with status 0 and writes an empty file, which note_build_id refuses.
copy_build_id() {
    : > "$notes" && objcopy -O binary -j .note.gnu.build-id "$1" "$notes" 2>/dev/null && note_build_id "$notes"
}
copy_version() { modinfo -F version "$1" 2>/dev/null; }
# Print the GNU build ID in a file of ELF notes in lowercase hex, and exit
# with status 1 unless the file holds exactly one GNU build ID note, of 1 to
# 64 bytes, the range that nvpcf_hook accepts. The rules are those of
# nh_note_build_id in hook/build_id.h, the hook's own parser. Every note's
# header, name with its padding, and descriptor have to be complete. A note
# followed by another note is padded to four bytes, as the ELF format
# requires. The last note may lack some or all of the padding after its
# descriptor: GNU ld writes it without that padding, and Linux exports such a
# section with the same size.
note_build_id() {
    python3 -I -c '
import struct, sys
data = open(sys.argv[1], "rb").read()
ids, offset = [], 0
while offset < len(data):
    if offset + 12 > len(data):
        sys.exit(1)
    namesz, descsz, kind = struct.unpack_from("<III", data, offset)
    name_at = offset + 12
    desc_at = name_at + ((namesz + 3) & ~3)
    desc_end = desc_at + descsz
    if desc_end > len(data):
        sys.exit(1)
    if kind == 3 and data[name_at:name_at + namesz] == b"GNU\0":
        ids.append(data[desc_at:desc_end])
    padded_end = desc_at + ((descsz + 3) & ~3)
    if padded_end >= len(data):
        break
    offset = padded_end
if len(ids) != 1 or not 1 <= len(ids[0]) <= 64:
    sys.exit(1)
print(ids[0].hex())' "$1"
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
        fail "check_nvidia.py rejects NVIDIA $ver with build ID $(copy_build_id "$ko" || echo unknown)"
}

hook_loaded() { grep -q '^nvpcf_hook ' /proc/modules; }
field() { sed -n "s/^$2: //p" <<<"$1"; }
# Exit status 0 if the hook's state is active or waiting, with its guards
# installed, GPU initialization allowed and retirement_failed 0. Then the
# guard covers NVIDIA's shutdown, and loading NVIDIA's other modules or
# starting a service that initializes the GPU is safe.
hook_usable() {
    local s
    s=$("$HOOKCTL" status 2>&1) || { log "cannot read the status of nvpcf_hook: $s"; return 1; }
    log "nvpcf_hook is $(field "$s" state), guards_registered $(field "$s" guards_registered)," \
        "initialization_blocked $(field "$s" initialization_blocked)," \
        "retirement_failed $(field "$s" retirement_failed), last_error $(field "$s" last_error)"
    case $(field "$s" state) in active|waiting) ;; *) return 1 ;; esac
    [ "$(field "$s" guards_registered)" = 1 ] && [ "$(field "$s" initialization_blocked)" = 0 ] &&
        [ "$(field "$s" retirement_failed)" = 0 ]
}

case $mode in
    fetch)
        fetch "$arg"
        log "NVIDIA's $arg source archive is in $CACHE"
        exit 0 ;;
    build-id)
        module_copy "$arg" || fail "cannot read $arg"
        copy_build_id "$copy" || fail "cannot read a GNU build ID of 1 to 64 bytes from $arg"
        exit 0 ;;
    loaded-build-id)
        note_build_id "/sys/module/$arg/notes/.note.gnu.build-id" ||
            fail "cannot read a GNU build ID of 1 to 64 bytes for the loaded module $arg"
        exit 0 ;;
    check-loaded)
        hook_loaded || fail "nvpcf_hook is not loaded"
        hook_usable
        exit ;;
    check-disk)
        # The version and the build ID come from the copy that is checked, so
        # that a package replaced meanwhile cannot pair one file with another
        # version's source.
        f=$(modinfo -n nvidia 2>/dev/null) || fail "modprobe cannot find nvidia for kernel $(uname -r)"
        module_copy "$f" || fail "cannot read $f"
        id=$(copy_build_id "$copy") || fail "cannot read the GNU build ID of 1 to 64 bytes that nvpcf_hook requires from $f"
        ver=$(copy_version "$copy")
        check_build "$copy" "$ver"
        log "NVIDIA $ver in $f, build ID $id, passes the checks"
        exit 0 ;;
esac

if hook_loaded; then
    log "nvpcf_hook is loaded already"
    hook_usable
    exit
fi
modprobe nvidia || fail "could not load nvidia"
id=$(note_build_id /sys/module/nvidia/notes/.note.gnu.build-id) || fail "cannot read the build ID of the loaded nvidia"
loaded_ver=$(cat /sys/module/nvidia/version 2>/dev/null)
valid_version "$loaded_ver" || fail "the loaded nvidia reports '$loaded_ver', not a driver version"
ko=""
for f in "$(modinfo -n nvidia 2>/dev/null)" /var/lib/dkms/nvidia/"$loaded_ver"/"$(uname -r)"/*/module/nvidia.ko*; do
    [ -f "$f" ] || continue
    module_copy "$f" || continue
    if [ "$(copy_build_id "$copy")" = "$id" ]; then
        ko=$copy
        log "the loaded nvidia is $f"
        break
    fi
done
[ -n "$ko" ] || fail "cannot find an installed nvidia module file with the loaded build ID $id, so the loaded driver cannot be checked"
ver=$(copy_version "$ko")
[ "$ver" = "$loaded_ver" ] ||
    fail "the module file with the loaded build ID reports version '$ver', but the loaded nvidia reports $loaded_ver"
check_build "$ko" "$ver"
modprobe nvpcf_hook "expected_nvidia_build_id=$id" || fail "could not load nvpcf_hook"
hook_usable
