#!/bin/bash
# Install nvpcf_hook with DKMS for the installed kernels, and make it load at
# boot. Run as root.
#
# usage: install-hook.sh
#
# DKMS builds modules from source in /usr/src/<package>-<version>. This
# script copies the hook's source there, with the version from
# hook/dkms.conf. It builds and installs the module for every kernel that
# DKMS lists for the NVIDIA driver, plus the running kernel. It first
# removes an earlier copy of the same version from DKMS. It writes
# /etc/modules-load.d/nvpcf-hook.conf, so that the module loads at boot. The
# module's own soft dependency loads nvidia before it. The script does not
# load the module now. `sudo modprobe nvpcf_hook` does, and
# `hook/nvpcf-hookctl status` then shows whether the hook is active.
#
# It works out the kernels first, and stops before it changes anything if
# dkms status fails or the running kernel's headers are missing. When
# another kernel's headers are missing, it skips that kernel and exits 1 at
# the end.
set -eu
usage() { echo "usage: install-hook.sh" >&2; exit 2; }
[ $# = 0 ] || usage
here=$(cd "$(dirname "$0")" && pwd)

ver=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"$/\1/p' "$here/hook/dkms.conf")
case "$ver" in
    '' | *[!0-9.]*) echo "cannot read a version from PACKAGE_VERSION in $here/hook/dkms.conf"; exit 1 ;;
esac
src=/usr/src/nvpcf-hook-$ver

status=$(dkms status nvidia) || { echo "dkms status failed"; exit 1; }
running=$(uname -r)
mapfile -t kernels < <({ printf '%s\n' "$status" | sed -nE 's#^nvidia/[0-9.]+, ([^,]+),.*#\1#p'; echo "$running"; } | sort -u)
targets=() skipped=()
for k in "${kernels[@]}"; do
    if [ -d "/lib/modules/$k/build" ]; then targets+=("$k"); else skipped+=("$k"); fi
done
[ -d "/lib/modules/$running/build" ] || { echo "the headers of the running kernel $running are missing"; exit 1; }
echo "== target kernels: ${targets[*]}"
[ ${#skipped[@]} -eq 0 ] || echo "== skipping kernels whose headers are missing: ${skipped[*]}"

if dkms status "nvpcf-hook/$ver" | grep -q .; then
    echo "== removing the earlier nvpcf-hook/$ver from DKMS"
    dkms remove "nvpcf-hook/$ver" --all
fi
rm -rf "$src"
mkdir -p "$src"
cp "$here/hook/Kbuild" "$here/hook/Makefile" "$here/hook/dkms.conf" "$here"/hook/*.c "$here"/hook/*.h "$src/"
dkms add -m nvpcf-hook -v "$ver"
for k in "${targets[@]}"; do
    echo "== building and installing nvpcf_hook for $k"
    dkms build -m nvpcf-hook -v "$ver" -k "$k"
    dkms install -m nvpcf-hook -v "$ver" -k "$k" --force
    modinfo -k "$k" -n nvpcf_hook > /dev/null || { echo "modprobe cannot find nvpcf_hook for $k"; exit 1; }
done
echo nvpcf_hook > /etc/modules-load.d/nvpcf-hook.conf
echo "== installed nvpcf-hook/$ver; it loads at the next boot, or now with: sudo modprobe nvpcf_hook"
if [ ${#skipped[@]} -gt 0 ]; then
    echo "== kernels whose headers are missing, not built: ${skipped[*]}"
    exit 1
fi
