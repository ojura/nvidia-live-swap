#!/bin/bash
# Install nvpcf_hook, its loader and its boot unit. Run as root.
#
# usage: install-hook.sh [--no-enable]
#   --no-enable  install nvpcf-hook.service without enabling it, and leave a
#                unit that is enabled already as it is
#
# The script registers the hook's source with DKMS as the version in
# hook/dkms.conf, builds and installs it for every kernel that DKMS lists for
# the NVIDIA driver or for nvpcf-hook and for the running kernel, and checks
# the module that modprobe finds for each. A kernel that DKMS still lists but
# that was removed gets no build, and its leftover nvpcf-hook install is
# removed. The script installs the files that lib.sh lists in HOOK_TOOLS in
# /usr/local/lib/nvpcf-hook, enables nvpcf-hook.service, which loads the hook
# at boot, and downloads NVIDIA's source archive for each NVIDIA version that
# DKMS lists. It does not load the hook. WRITEUP.md describes the rules for
# versions, source directories, kernels, failed installs and old versions, and
# the exit statuses.
set -u
usage() { echo "usage: install-hook.sh [--no-enable]" >&2; exit 2; }
# The arguments are checked by count and one by one, so that an empty
# argument cannot select the default install, which enables the unit.
enable=1
case $# in
    0) ;;
    1) [ "$1" = --no-enable ] || usage; enable=0 ;;
    *) usage ;;
esac
here=$(cd "$(dirname "$0")" && pwd)
DIR=$here
. "$here/lib.sh"
UNIT=/etc/systemd/system/nvpcf-hook.service
# The files of hook/ that DKMS's build reads. Files that a build in hook/
# generates, such as nvpcf_hook.mod.c, are not part of the source.
SOURCES=(Kbuild Makefile dkms.conf nvpcf_hook.c build_id.h lifecycle.h state.h text.h)
fail() { echo "$*"; exit 1; }

# DKMS's VER from /usr/sbin/dkms. For a new kernel, DKMS builds the greatest
# registered version in this order: every number padded to three digits, and
# the results compared as strings.
VER() {
    echo "$1" | sed -e 's:\([^0-9]\)\([0-9]\):\1 \2:g' \
        -e 's:\([0-9]\)\([^0-9]\):\1 \2:g' \
        -e 's:\(.*\): \1 :' \
        -e 's: \([0-9]\) : 00\1 :g' \
        -e 's: \([0-9][0-9]\) : 0\1 :g' \
        -e 's: ::g'
}
# Print the build ID of a module file. load-hook.sh decompresses the file and
# prints an ID only if it is 1 to 64 bytes, as nvpcf_hook requires.
build_id() { "$here/load-hook.sh" --build-id "$1"; }
# Exit status 0 if the nvpcf_hook file that modprobe finds for kernel $2 has
# the build ID of DKMS's build of version $1 for that kernel.
installed_is() {
    local f b x y
    f=$(modinfo -k "$2" -n nvpcf_hook 2>/dev/null) || return 1
    b=$(ls /var/lib/dkms/nvpcf-hook/"$1"/"$2"/*/module/nvpcf_hook.ko* 2>/dev/null | head -1)
    [ -n "$b" ] && x=$(build_id "$f") && y=$(build_id "$b") && [ "$x" = "$y" ]
}
# Exit status 0 if $1 equals one of the other arguments.
contains() {
    local x=$1 y
    shift
    for y in "$@"; do [ "$x" = "$y" ] && return 0; done
    return 1
}
# Compare the directory $src with hook/'s source, print each difference on a
# line, and exit with status 0 only if the comparison completed and every
# entry matched. Each file of SOURCES has to be a regular file, not a symbolic
# link, with a link count of one, and with hook/'s content: DKMS copies a link
# as a link, and another name of the file could change it later. Every other
# entry counts as a difference. The entries are read with NUL separators, so
# that a name with a space or a newline is one entry, and printed with printf
# %q.
# A file or a listing that cannot be read makes the comparison fail.
compare_source() {
    local f n entries=() status=0
    if [ -L "$src" ] || [ ! -d "$src" ]; then
        echo "$src is not a directory"
        return 1
    fi
    for f in "${SOURCES[@]}"; do
        if [ -L "$src/$f" ]; then
            echo "$f is a symbolic link"; status=1
        elif [ ! -e "$src/$f" ]; then
            echo "$f is missing"; status=1
        elif [ ! -f "$src/$f" ]; then
            echo "$f is not a regular file"; status=1
        elif ! n=$(stat -c %h -- "$src/$f"); then
            echo "cannot read $f"; status=1
        elif [ "$n" != 1 ]; then
            echo "$f has another hard link"; status=1
        else
            cmp -s -- "$here/hook/$f" "$src/$f"
            case $? in
                0) ;;
                1) echo "$f differs"; status=1 ;;
                *) echo "cannot read $f"; status=1 ;;
            esac
        fi
    done
    mapfile -d '' -t entries < <(find "$src" -mindepth 1 -maxdepth 1 -printf '%f\0')
    wait $! || { echo "cannot list $src"; return 1; }
    for f in "${entries[@]}"; do
        contains "$f" "${SOURCES[@]}" || { printf '%q is not part of the source\n' "$f"; status=1; }
    done
    return $status
}
# Remove the files of SOURCES from the source directory $1, then remove the
# directory if it is empty. Keep links and files outside SOURCES; their
# presence also keeps the directory.
remove_source() {
    local d=$1 f
    [ -d "$d" ] && [ ! -L "$d" ] || return 0
    for f in "${SOURCES[@]}"; do
        if [ -f "$d/$f" ] && [ ! -L "$d/$f" ]; then rm -f -- "$d/$f"; fi
    done
    rmdir -- "$d" 2>/dev/null || echo "== kept $d, which holds files that are not nvpcf-hook's source"
}
# Exit status 0 if version $1 is installed for some kernel.
installed_anywhere() {
    local line
    while IFS= read -r line; do
        [[ $line == "nvpcf-hook/$1, "*": installed"* ]] && return 0
    done <<<"$hook_status"
    return 1
}

# For dotted numbers from 0 to 999 without leading zeros, VER's order is the
# numeric one, and two versions get the same rank only if they are the same.
# With leading zeros, 0.1 and 0.001 would get the same rank.
ver=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"$/\1/p' "$here/hook/dkms.conf")
[[ $ver =~ ^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2}))*$ ]] ||
    fail "PACKAGE_VERSION in $here/hook/dkms.conf must be dotted numbers from 0 to 999 without leading zeros, got '$ver'"
for f in "${SOURCES[@]}"; do
    [ -f "$here/hook/$f" ] || fail "$here/hook/$f is missing"
done
src=/usr/src/nvpcf-hook-$ver

status=$(dkms status nvidia) || fail "dkms status nvidia failed"
hook_status=$(dkms status nvpcf-hook) || fail "dkms status nvpcf-hook failed"
running=$(uname -r)
mapfile -t kernels < <({ printf '%s\n' "$status" | sed -nE 's#^nvidia/[0-9.]+, ([^,]+),.*#\1#p'
                         printf '%s\n' "$hook_status" | sed -nE 's#^nvpcf-hook/[^,]+, ([^,]+),.*#\1#p'
                         echo "$running"; } | sort -u)
mapfile -t versions < <(printf '%s\n' "$status" | sed -nE 's#^nvidia/([0-9.]+)[,:].*#\1#p' | sort -u)
skipped=() failed=()
# The version that each kernel has installed before the run.
declare -A before=()
while IFS= read -r line; do
    [[ $line =~ ^nvpcf-hook/([^,]+),\ ([^,]+),\ [^:]+:\ installed ]] && before[${BASH_REMATCH[2]}]=${BASH_REMATCH[1]}
done <<<"$hook_status"
# A kernel that DKMS lists but that has neither headers nor modules of its own
# was removed. Only the removal of a kernel image runs DKMS's
# /etc/kernel/prerm.d/dkms, so DKMS keeps listing what it installed for a
# kernel whose headers went after its image. The running kernel never counts
# as removed.
targets=() removed=()
for k in "${kernels[@]}"; do
    if [ "$k" != "$running" ] && [ ! -d "/lib/modules/$k/build" ] && [ ! -d "/lib/modules/$k/kernel" ]; then
        removed+=("$k")
    else
        targets+=("$k")
    fi
done
# Headers are for building. The running kernel needs them unless it has
# $ver installed already and passes the check, which reads only files.
if [ ! -d "/lib/modules/$running/build" ]; then
    [ "${before[$running]:-}" = "$ver" ] && installed_is "$ver" "$running" ||
        fail "the headers of the running kernel $running are missing, and it does not have nvpcf-hook/$ver installed and checked"
fi
echo "== nvpcf-hook/$ver for kernels: ${targets[*]}"
[ ${#removed[@]} -eq 0 ] ||
    echo "== leaving out removed kernels, which DKMS still lists but which have neither headers nor modules in /lib/modules/<kernel>/kernel: ${removed[*]}"

# Another registered version that ranks equal to $ver or above it could be the
# one that DKMS builds for the next kernel.
mapfile -t registered < <(sed -nE 's#^nvpcf-hook/([^,:]+)[,:].*#\1#p' <<<"$hook_status" | sort -u)
for v in "${registered[@]}"; do
    [ "$v" = "$ver" ] && continue
    [[ "$(VER "$v")" < "$(VER "$ver")" ]] ||
        fail "nvpcf-hook/$v is registered, and DKMS ranks it equal to or above hook/'s $ver, so DKMS could build $v for the next kernel. Raise PACKAGE_VERSION in hook/dkms.conf, or remove $v with: dkms remove nvpcf-hook/$v --all"
done
# An existing $src of an unregistered version can hold the copy from a run
# whose dkms add failed, or someone's work. The script reuses it only if it
# holds exactly hook/'s source, and otherwise leaves it as it is.
if contains "$ver" "${registered[@]}"; then
    differences=$(compare_source) ||
        fail "nvpcf-hook/$ver is registered with source that differs from hook/: $(paste -sd ';' <<<"$differences" | sed 's/;/; /g'). A changed hook needs a greater PACKAGE_VERSION in hook/dkms.conf"
else
    if [ -L "$src" ] || { [ -e "$src" ] && [ ! -d "$src" ]; }; then
        fail "$src exists and is not a directory. DKMS does not have nvpcf-hook/$ver registered, and the script leaves $src as it is; move it away to install"
    elif [ -d "$src" ]; then
        differences=$(compare_source) ||
            fail "$src exists, but DKMS does not have nvpcf-hook/$ver registered, and it differs from hook/'s source: $(paste -sd ';' <<<"$differences" | sed 's/;/; /g'). The script leaves it as it is; move it away to install"
        echo "== $src holds hook/'s source already, and the script registers it"
    else
        # mkdir without -p fails if anything appeared at $src since the check
        # above. If mkdir fails because an entry appeared, this run must not
        # remove that entry.
        mkdir -p -- "${src%/*}" && mkdir -- "$src" ||
            fail "could not create $src. The script leaves anything there as it is"
        if ! (cd "$here/hook" && cp -- "${SOURCES[@]}" "$src/"); then
            remove_source "$src"
            fail "could not copy the hook's source to $src"
        fi
    fi
    dkms add -m nvpcf-hook -v "$ver" ||
        fail "dkms add nvpcf-hook/$ver failed, before any module was built or installed. $src keeps hook/'s source, and the next run registers it while it still matches"
fi

for k in "${targets[@]}"; do
    prev=${before[$k]:-}
    # The check reads the installed file and DKMS's build, so a kernel that
    # passes it does not need headers.
    if [ "$prev" = "$ver" ]; then
        if installed_is "$ver" "$k"; then
            echo "== nvpcf-hook/$ver is installed for $k already"
            continue
        fi
        # DKMS lists $ver as installed, but the file that modprobe finds does
        # not match DKMS's build of it. The run installs $ver again, and has
        # no earlier version to restore if that fails.
        prev=""
    fi
    if [ ! -d "/lib/modules/$k/build" ]; then
        skipped+=("$k")
        continue
    fi
    echo "== building and installing nvpcf-hook/$ver for $k"
    if ! dkms build -m nvpcf-hook -v "$ver" -k "$k" --force; then
        failed+=("$k: building nvpcf-hook/$ver${prev:+; nvpcf-hook/$prev stays installed}")
        continue
    fi
    dkms install -m nvpcf-hook -v "$ver" -k "$k" --force && installed_is "$ver" "$k" && continue
    # dkms install copies the new file over the old one before it runs depmod,
    # and removes the new file when depmod fails, so a failed install can
    # leave the kernel without a module. The script then installs the previous
    # version again from DKMS's build of it.
    failed+=("$k: installing nvpcf-hook/$ver, or checking that modprobe finds it")
    if [ -z "$prev" ]; then
        failed+=("$k: did not have an installed nvpcf-hook version to restore")
        continue
    fi
    if installed_is "$prev" "$k"; then
        echo "== $k still has nvpcf-hook/$prev"
    elif dkms install -m nvpcf-hook -v "$prev" -k "$k" --force && installed_is "$prev" "$k"; then
        echo "== installed nvpcf-hook/$prev for $k again, from DKMS's build of it"
    else
        failed+=("$k: installing nvpcf-hook/$prev again failed, so nvpcf_hook may not be installed or working for $k")
    fi
done

# Remove nvpcf-hook's install for each removed kernel, as /etc/kernel/prerm.d/dkms
# does when a kernel image is removed, so that a version installed there does
# not count as installed. The nvidia entries for those kernels stay.
cleared=()
for k in "${removed[@]}"; do
    while IFS= read -r line; do
        [[ $line =~ ^nvpcf-hook/([^,]+),\ ([^,]+),\ [^:]+:\ installed ]] && [ "${BASH_REMATCH[2]}" = "$k" ] || continue
        v=${BASH_REMATCH[1]}
        [[ $v =~ ^[0-9A-Za-z.+~-]+$ ]] || { failed+=("$k: nvpcf-hook/$v not removed, unexpected version"); continue; }
        echo "== removing the install of nvpcf-hook/$v for the removed kernel $k"
        if dkms remove -m nvpcf-hook -v "$v" -k "$k"; then
            cleared+=("$v")
        else
            failed+=("$k: removing the install of nvpcf-hook/$v for this removed kernel")
        fi
    done <<<"$hook_status"
done

# Remove older versions only after every kernel in this run has $ver installed
# and checked. After a failed build, install or check, or a kernel skipped for
# missing headers, keep them, so that dkms install can still put an older
# build back on a kernel that a failed install left without a module. The
# check above made every other registered version rank below $ver.
hook_status=$(dkms status nvpcf-hook) || fail "dkms status nvpcf-hook failed after the install"
mapfile -t old < <(sed -nE 's#^nvpcf-hook/([^,:]+)[,:].*#\1#p' <<<"$hook_status" | grep -vxF "$ver" | sort -u)
# dkms remove -k deletes a version from the DKMS tree when no other kernel has
# a build of it, and leaves its source. A rollback installs an older version
# from DKMS's build of it, and the last build of that version is gone.
for v in "${cleared[@]}"; do
    [ "$v" != "$ver" ] && ! contains "$v" "${old[@]}" || continue
    echo "== DKMS deleted nvpcf-hook/$v, which was installed only for removed kernels; removing its source"
    remove_source "/usr/src/nvpcf-hook-$v"
done
if [ ${#failed[@]} -gt 0 ] || [ ${#skipped[@]} -gt 0 ]; then
    [ ${#old[@]} -eq 0 ] ||
        echo "== keeping ${old[*]/#/nvpcf-hook/} until every kernel, including the skipped ones, has nvpcf-hook/$ver"
else
    for v in "${old[@]}"; do
        installed_anywhere "$v" && continue
        [[ $v =~ ^[0-9A-Za-z.+~-]+$ ]] || { failed+=("nvpcf-hook/$v: not removed, unexpected version"); continue; }
        echo "== removing nvpcf-hook/$v, which is not installed for any kernel"
        if dkms remove "nvpcf-hook/$v" --all; then remove_source "/usr/src/nvpcf-hook-$v"; else failed+=("nvpcf-hook/$v: remove"); fi
    done
fi

# check_nvidia.py reads expected_dispatcher.c and expected_procfs_wrappers.h
# from its own directory, so they go next to it.
for f in "${HOOK_TOOLS[@]}"; do
    mode=0644
    [ -x "$here/$f" ] && mode=0755
    install -D -m $mode "$here/$f" "$HOOK_LIB/${f##*/}" || fail "could not install $f in $HOOK_LIB"
done
install -D -m 0644 "$here/nvpcf-hook.service" "$UNIT" || fail "could not install $UNIT"
if [ $enable = 1 ]; then
    systemctl daemon-reload && systemctl enable nvpcf-hook.service || failed+=("enabling nvpcf-hook.service")
else
    systemctl daemon-reload || failed+=("systemctl daemon-reload")
    echo "== installed nvpcf-hook.service without enabling it; it is $(systemctl is-enabled nvpcf-hook.service 2>/dev/null || true)"
fi
for v in "${versions[@]}"; do
    "$HOOK_LIB/load-hook.sh" --fetch "$v" || failed+=("NVIDIA $v: source archive not downloaded")
done

[ ${#skipped[@]} -eq 0 ] || echo "== kernels whose headers are missing, not built: ${skipped[*]}"
if [ ${#failed[@]} -gt 0 ]; then
    printf '== failed: %s\n' "${failed[@]}"
    exit 1
fi
[ ${#skipped[@]} -eq 0 ] || exit 1
if [ $enable = 1 ]; then
    echo "== installed nvpcf-hook/$ver. It loads at the next boot, or now with: sudo $HOOK_LIB/load-hook.sh"
else
    echo "== installed nvpcf-hook/$ver. It loads now with: sudo $HOOK_LIB/load-hook.sh, and at boot while nvpcf-hook.service is enabled"
fi
