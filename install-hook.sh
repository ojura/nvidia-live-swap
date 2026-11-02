#!/bin/bash
# Install nvpcf_hook, its loader and its boot unit. Run as root.
#
# usage: install-hook.sh
#
# DKMS builds each version of a module from its own source directory,
# /usr/src/<package>-<version>. For a new kernel, DKMS builds the greatest
# registered version, as its VER function orders them. VER pads every number
# to three digits and compares the results as strings. The version here is
# PACKAGE_VERSION in hook/dkms.conf: dotted numbers from 0 to 999 without
# leading zeros. VER keeps the numeric order of such versions, and gives two
# of them the same rank only if they are the same version. A change to the
# hook's source needs a greater PACKAGE_VERSION. The script refuses when its
# version is registered with different source, and when another registered
# version ranks equal to it or above it, because DKMS could then build that
# version for the next kernel.
#
# The source is the files of hook/ that DKMS's build reads: Kbuild, Makefile,
# dkms.conf, nvpcf_hook.c and its headers. Files that a build in hook/
# generates, such as nvpcf_hook.mod.c, are not part of it. For a version that
# DKMS does not have registered, the script creates
# /usr/src/nvpcf-hook-<version> with mkdir, which fails if anything appeared
# there in the meantime, copies the source into it, and registers it with dkms
# add. An existing directory there, such as the copy from a run whose dkms add
# failed, is reused only if its entries are exactly these files, each a
# regular file with no other link and with hook/'s content. Otherwise, and
# when the directory or a file in it cannot be read, the script refuses, names
# the differences, and leaves the directory as it is. When it removes an older
# version, it deletes only that version's source files and an empty
# directory, and keeps anything else there.
#
# It builds and installs that version with DKMS for every kernel that DKMS
# lists for the NVIDIA driver or for nvpcf-hook, plus the running kernel. The
# kernels listed for nvpcf-hook include those whose NVIDIA build DKMS does not
# list at the moment, for example between a driver update and the dkms install
# for the other kernels. After each install, and for a kernel that DKMS lists
# as having this version installed already, it checks that the module
# modprobe finds for the kernel has the build ID of DKMS's build. That check
# needs no headers, so a kernel that passes it counts as done even without
# them. A kernel that fails it gets this version installed again.
#
# A failed build leaves the kernel's installed module in place, and so do
# missing headers. A failed install can leave the kernel without a module,
# because DKMS replaces the file before it runs depmod, and removes the new
# file when depmod fails. After a failed install or check, the script installs
# the kernel's previous version again from DKMS's build of it, checks it in the
# same way, and reports whether that worked. Older versions that no kernel has
# installed are removed only by a run in which each of these kernels,
# including the ones whose headers are missing, ends with this version
# installed and checked. After a failure or a skipped kernel they stay, also
# through later runs that find a kernel without any installed version, so that
# dkms install can still put one of their builds back.
#
# It installs the files that lib.sh lists in HOOK_TOOLS as root-owned copies
# in /usr/local/lib/nvpcf-hook: load-hook.sh, nvpcf-hookctl, check_nvidia.py,
# and two references that check_nvidia.py reads from its own directory.
# check_nvidia.py compares the dispatcher function in NVIDIA's source archive
# with expected_dispatcher.c, and NVIDIA's proc file wrappers in the
# kernel-open source with expected_procfs_wrappers.h. The script enables
# nvpcf-hook.service, which runs load-hook.sh at boot, and downloads NVIDIA's
# source archive for each NVIDIA version that DKMS lists into
# /var/cache/nvpcf-hook, so that loading the hook does not need the network.
# It does not load the hook now; running /usr/local/lib/nvpcf-hook/load-hook.sh
# does.
#
# It reads the DKMS state and works out the kernels before it changes
# anything. It stops there if a dkms status fails, or if the running kernel's
# headers are missing and that kernel does not have this version installed
# and passing the check. It exits with status 1 at the end if it skipped a
# kernel, if a build, install or check failed, or if an archive could not be
# downloaded.
set -u
usage() { echo "usage: install-hook.sh" >&2; exit 2; }
[ $# = 0 ] || usage
here=$(cd "$(dirname "$0")" && pwd)
DIR=$here
. "$here/lib.sh"
UNIT=/etc/systemd/system/nvpcf-hook.service
SOURCES=(Kbuild Makefile dkms.conf nvpcf_hook.c build_id.h lifecycle.h state.h text.h)
fail() { echo "$*"; exit 1; }

# DKMS's VER from /usr/sbin/dkms.
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
# line, and exit with status 0 only if the comparison completed and found
# none. Each file of SOURCES has to be a regular file, not a symbolic link,
# with no other hard link, and with hook/'s content: DKMS copies a link as a
# link, and another name of the file could change it later. Every other entry
# counts as a difference. The entries are read with NUL separators, so that a
# name with a space or a newline is one entry, and printed with printf %q.
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
# Remove the files of SOURCES from the source directory $1, and then the
# directory if nothing else is in it. A file that is not part of the source,
# or a link, stays, and so does the directory then.
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
# Headers are for building. The running kernel needs them unless it has
# $ver installed already and passes the check, which reads only files.
if [ ! -d "/lib/modules/$running/build" ]; then
    [ "${before[$running]:-}" = "$ver" ] && installed_is "$ver" "$running" ||
        fail "the headers of the running kernel $running are missing, and it does not have nvpcf-hook/$ver installed and checked"
fi
echo "== nvpcf-hook/$ver for kernels: ${kernels[*]}"

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
        # above, and then the script has created nothing to remove.
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

for k in "${kernels[@]}"; do
    prev=${before[$k]:-}
    # The check reads the installed file and DKMS's build, so a kernel that
    # passes it needs no headers.
    if [ "$prev" = "$ver" ]; then
        if installed_is "$ver" "$k"; then
            echo "== nvpcf-hook/$ver is installed for $k already"
            continue
        fi
        # DKMS lists $ver as installed, but modprobe finds another file. The
        # run installs $ver again, and has no other version to go back to.
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
    failed+=("$k: installing nvpcf-hook/$ver, or checking that modprobe finds it")
    if [ -z "$prev" ]; then
        failed+=("$k: had no installed nvpcf-hook version to go back to")
        continue
    fi
    if installed_is "$prev" "$k"; then
        echo "== $k still has nvpcf-hook/$prev"
    elif dkms install -m nvpcf-hook -v "$prev" -k "$k" --force && installed_is "$prev" "$k"; then
        echo "== installed nvpcf-hook/$prev for $k again, from DKMS's build of it"
    else
        failed+=("$k: installing nvpcf-hook/$prev again failed, so $k may have no working nvpcf_hook")
    fi
done

# Remove the older versions that are not installed for any kernel, but only
# when every kernel of the run has $ver installed and checked: no build,
# install or check failed, and no kernel was skipped for missing headers. A
# failed or skipped kernel can need one of their builds again, also after a
# later run that finds it without any installed version. The check above made
# every other registered version rank below $ver.
hook_status=$(dkms status nvpcf-hook) || fail "dkms status nvpcf-hook failed after the install"
mapfile -t old < <(sed -nE 's#^nvpcf-hook/([^,:]+)[,:].*#\1#p' <<<"$hook_status" | grep -vxF "$ver" | sort -u)
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

for f in "${HOOK_TOOLS[@]}"; do
    mode=0644
    [ -x "$here/$f" ] && mode=0755
    install -D -m $mode "$here/$f" "$HOOK_LIB/${f##*/}" || fail "could not install $f in $HOOK_LIB"
done
install -D -m 0644 "$here/nvpcf-hook.service" "$UNIT" || fail "could not install $UNIT"
systemctl daemon-reload && systemctl enable nvpcf-hook.service || failed+=("enabling nvpcf-hook.service")
for v in "${versions[@]}"; do
    "$HOOK_LIB/load-hook.sh" --fetch "$v" || failed+=("NVIDIA $v: source archive not downloaded")
done

[ ${#skipped[@]} -eq 0 ] || echo "== kernels whose headers are missing, not built: ${skipped[*]}"
if [ ${#failed[@]} -gt 0 ]; then
    printf '== failed: %s\n' "${failed[@]}"
    exit 1
fi
[ ${#skipped[@]} -eq 0 ] || exit 1
echo "== installed nvpcf-hook/$ver. It loads at the next boot, or now with: sudo $HOOK_LIB/load-hook.sh"
