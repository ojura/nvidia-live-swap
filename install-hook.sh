#!/bin/bash
# Install nvpcf_hook, its loader and its boot unit. Run as root.
#
# usage: install-hook.sh
#
# DKMS builds each version of a module from its own source directory,
# /usr/src/<package>-<version>. This script gives each content of hook/ its
# own version: the version in hook/dkms.conf, a hyphen, and the first 12 hex
# digits of a SHA-256 over the hook's source files. It copies the source to
# /usr/src/nvpcf-hook-<that version>, and builds and installs that version
# with DKMS for every kernel that DKMS lists for the NVIDIA driver, plus the
# running kernel. A kernel keeps its installed module when the new version
# does not build or install for it, and so does a kernel whose headers are
# missing. Afterwards, the script removes every other version that is not
# installed for any kernel.
#
# It installs load-hook.sh, check_nvidia.py and nvpcf-hookctl as root-owned
# copies in /usr/local/lib/nvpcf-hook, and enables nvpcf-hook.service, which
# runs load-hook.sh at boot. It downloads NVIDIA's source archive for each
# NVIDIA version that DKMS lists into /var/cache/nvpcf-hook, so that loading
# the hook does not need the network. It does not load the hook now; running
# /usr/local/lib/nvpcf-hook/load-hook.sh does.
#
# It reads the DKMS state and works out the kernels before it changes
# anything, and stops there if a dkms status fails or the running kernel's
# headers are missing. It exits with status 1 at the end if it skipped a
# kernel, if a build, install or check failed, or if an archive could not be
# downloaded.
set -u
usage() { echo "usage: install-hook.sh" >&2; exit 2; }
[ $# = 0 ] || usage
here=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/nvpcf-hook
UNIT=/etc/systemd/system/nvpcf-hook.service
fail() { echo "$*"; exit 1; }
# Print the build ID of a module file, decompressing it first if needed.
build_id() {
    case "$1" in
        *.zst) zstd -dcq "$1" ;;
        *.xz) xz -dc "$1" ;;
        *) cat "$1" ;;
    esac > "$tmp" && readelf -n "$tmp" 2>/dev/null | sed -n 's/.*Build ID: //p' | head -1
}

base=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"$/\1/p' "$here/hook/dkms.conf")
[[ $base =~ ^[0-9]+(\.[0-9]+)*$ ]] || fail "cannot read a version from PACKAGE_VERSION in $here/hook/dkms.conf"
sources=("$here/hook/Kbuild" "$here/hook/Makefile" "$here/hook/dkms.conf" "$here"/hook/*.c "$here"/hook/*.h)
hash=$(for f in "${sources[@]}"; do printf '%s\0' "${f##*/}"; cat "$f"; done | sha256sum | cut -c1-12)
ver=$base-$hash
src=/usr/src/nvpcf-hook-$ver

status=$(dkms status nvidia) || fail "dkms status nvidia failed"
hook_status=$(dkms status nvpcf-hook) || fail "dkms status nvpcf-hook failed"
running=$(uname -r)
mapfile -t kernels < <({ printf '%s\n' "$status" | sed -nE 's#^nvidia/[0-9.]+, ([^,]+),.*#\1#p'; echo "$running"; } | sort -u)
mapfile -t versions < <(printf '%s\n' "$status" | sed -nE 's#^nvidia/([0-9.]+)[,:].*#\1#p' | sort -u)
targets=() skipped=() failed=()
for k in "${kernels[@]}"; do
    if [ -d "/lib/modules/$k/build" ]; then targets+=("$k"); else skipped+=("$k"); fi
done
[ -d "/lib/modules/$running/build" ] || fail "the headers of the running kernel $running are missing"
echo "== nvpcf-hook/$ver for kernels: ${targets[*]}"
[ ${#skipped[@]} -eq 0 ] || echo "== skipping kernels whose headers are missing: ${skipped[*]}"
tmp=$(mktemp) || fail "cannot create a temporary file"
trap 'rm -f "$tmp"' EXIT

if grep -q "^nvpcf-hook/$ver[,:]" <<<"$hook_status"; then
    for f in "${sources[@]}"; do
        [ "${f##*/}" = dkms.conf ] && continue
        cmp -s "$f" "$src/${f##*/}" || fail "$src differs from hook/; remove it with: dkms remove nvpcf-hook/$ver --all"
    done
else
    rm -rf "$src"
    mkdir -p "$src" && cp "${sources[@]}" "$src/" || fail "could not copy the hook's source to $src"
    sed -i "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=\"$ver\"/" "$src/dkms.conf" || fail "could not set the version in $src/dkms.conf"
    dkms add -m nvpcf-hook -v "$ver" || fail "dkms add nvpcf-hook/$ver failed"
fi
for k in "${targets[@]}"; do
    if grep -q "^nvpcf-hook/$ver, $k, .*: installed" <<<"$hook_status"; then
        echo "== nvpcf-hook/$ver is installed for $k already"
        continue
    fi
    echo "== building and installing nvpcf-hook/$ver for $k"
    if ! dkms build -m nvpcf-hook -v "$ver" -k "$k" --force; then failed+=("$k: build"); continue; fi
    if ! dkms install -m nvpcf-hook -v "$ver" -k "$k" --force; then failed+=("$k: install"); continue; fi
    installed=$(modinfo -k "$k" -n nvpcf_hook 2>/dev/null)
    built=$(ls /var/lib/dkms/nvpcf-hook/"$ver"/"$k"/*/module/nvpcf_hook.ko* 2>/dev/null | head -1)
    if [ -z "$installed" ] || [ -z "$built" ] || [ "$(build_id "$installed")" != "$(build_id "$built")" ]; then
        failed+=("$k: the module modprobe finds is not the one DKMS built")
    fi
done

# Remove the versions that are not installed for any kernel. A version that
# a skipped or failed kernel still has installed stays.
hook_status=$(dkms status nvpcf-hook) || fail "dkms status nvpcf-hook failed after the install"
mapfile -t old < <(sed -nE 's#^nvpcf-hook/([^,:]+)[,:].*#\1#p' <<<"$hook_status" | sort -u)
for v in "${old[@]}"; do
    [ "$v" = "$ver" ] && continue
    grep -q "^nvpcf-hook/$v, .*: installed" <<<"$hook_status" && continue
    [[ $v =~ ^[0-9.]+-[0-9a-f]+$ ]] || { failed+=("nvpcf-hook/$v: not removed, unexpected version"); continue; }
    echo "== removing nvpcf-hook/$v, which is not installed for any kernel"
    if dkms remove "nvpcf-hook/$v" --all; then rm -rf "/usr/src/nvpcf-hook-$v"; else failed+=("nvpcf-hook/$v: remove"); fi
done

install -D -m 0755 "$here/load-hook.sh" "$LIB/load-hook.sh" &&
    install -D -m 0644 "$here/hook/tests/check_nvidia.py" "$LIB/check_nvidia.py" &&
    install -D -m 0755 "$here/hook/nvpcf-hookctl" "$LIB/nvpcf-hookctl" &&
    install -D -m 0644 "$here/nvpcf-hook.service" "$UNIT" || fail "could not install the loader in $LIB"
systemctl daemon-reload && systemctl enable nvpcf-hook.service || failed+=("enabling nvpcf-hook.service")
for v in "${versions[@]}"; do
    "$LIB/load-hook.sh" --fetch "$v" || failed+=("NVIDIA $v: source archive not downloaded")
done

[ ${#skipped[@]} -eq 0 ] || echo "== kernels whose headers are missing, not built: ${skipped[*]}"
if [ ${#failed[@]} -gt 0 ]; then
    printf '== failed: %s\n' "${failed[@]}"
    exit 1
fi
[ ${#skipped[@]} -eq 0 ] || exit 1
echo "== installed nvpcf-hook/$ver. It loads at the next boot, or now with: sudo $LIB/load-hook.sh"
