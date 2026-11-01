#!/bin/bash
# Build Ubuntu's NVIDIA kernel modules with the patches in patches/, which
# stop the driver from waking the sleeping GPU for each power-limit
# notification from the firmware.
#
# Ubuntu's nvidia-dkms-<branch>-open package builds the kernel modules from
# source, except for the driver core, which the package contains only as a
# precompiled object, /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary.
# The tag of the same version in NVIDIA's open-gpu-kernel-modules repository
# on GitHub holds the source of that object. This script builds the object
# from the tag with the patches, puts it in place of Ubuntu's copy (a dpkg
# diversion keeps Ubuntu's copy as nv-kernel.o_binary.ubuntu), and rebuilds
# the DKMS modules. The new modules load at the next boot, or now with
# swap.sh.
#
# usage (as root): build.sh [version]
#   version defaults to the newest nvidia version DKMS lists.
#
# build.sh rebuilds the modules for every kernel DKMS lists for this driver
# version, plus the running kernel, which is the one swap.sh loads them
# into. It works out these kernels first, and stops before it diverts and
# replaces the object if dkms status fails or the running kernel's headers
# are missing. When another kernel's headers are missing, it skips that
# kernel and exits 1 at the end.
set -eu
usage() { echo "usage: build.sh [version]" >&2; exit 2; }
[ $# -le 1 ] || usage
here=$(cd "$(dirname "$0")" && pwd)

status=$(dkms status nvidia) || { echo "dkms status failed"; exit 1; }
if [ $# = 1 ]; then
    ver=$1
else
    ver=$(printf '%s\n' "$status" | sed -nE 's#^nvidia/([0-9.]+)[,:].*#\1#p' | sort -uV | tail -1)
fi
case "$ver" in
    '' | *[!0-9.]*) echo "cannot tell which driver version to build (got '$ver')"; usage ;;
esac
src=/usr/src/nvidia-$ver
obj=$src/nvidia/nv-kernel.o_binary
[ -d "$src" ] || { echo "$src does not exist"; exit 1; }

running=$(uname -r)
ver_re=${ver//./\\.}
mapfile -t kernels < <({ printf '%s\n' "$status" | sed -nE "s#^nvidia/$ver_re, ([^,]+),.*#\1#p"; echo "$running"; } | sort -u)
targets=() skipped=()
for k in "${kernels[@]}"; do
    if [ -d "/lib/modules/$k/build" ]; then targets+=("$k"); else skipped+=("$k"); fi
done
[ -d "/lib/modules/$running/build" ] || { echo "the headers of the running kernel $running are missing"; exit 1; }
echo "== target kernels: ${targets[*]}"
[ ${#skipped[@]} -eq 0 ] || echo "== skipping kernels whose headers are missing: ${skipped[*]}"

work=$(mktemp -d "/tmp/nvidia-nvpcf-$ver-XXXX")
trap 'rm -rf "$work"' EXIT
echo "== cloning NVIDIA's $ver tag"
git -c advice.detachedHead=false clone -q --depth 1 --branch "$ver" https://github.com/NVIDIA/open-gpu-kernel-modules.git "$work/ogkm"

# The tag's kernel-open directory must be the source Ubuntu packaged;
# otherwise the object built here may not match the rest of the modules.
differences=$(diff -rq --exclude=dkms.conf --exclude=.gitignore "$work/ogkm/kernel-open" "$src" | grep -v "^Only in $src" || true)
if [ -n "$differences" ]; then
    echo "Ubuntu's $src differs from NVIDIA's tag:"
    echo "$differences"
    exit 1
fi

echo "== applying patches/ and building the driver core"
for patch in "$here"/patches/*.diff; do
    git -C "$work/ogkm" apply "$patch"
    echo "   applied ${patch##*/}"
done
make -C "$work/ogkm/src/nvidia" -j"$(nproc)" > "$work/build.log" 2>&1 || { tail -30 "$work/build.log"; exit 1; }
built=$work/ogkm/src/nvidia/_out/Linux_x86_64/nv-kernel.o
nm "$built" | grep -q ' RmDeferNvpcfNotifyIfIdle$' || { echo "the built object lacks the patch"; exit 1; }

if ! dpkg-divert --list "$obj" | grep -q .; then
    dpkg-divert --local --rename --divert "$obj.ubuntu" --add "$obj"
fi
install -m 644 -o root -g root "$built" "$obj"
echo "== installed the patched driver core as $obj"

for k in "${targets[@]}"; do
    echo "== $k"
    dkms build -m nvidia -v "$ver" -k "$k" --force > "$work/dkms-$k.log" 2>&1 || { tail -20 "$work/dkms-$k.log"; exit 1; }
    dkms install -m nvidia -v "$ver" -k "$k" --force > "$work/dkms-install-$k.log" 2>&1 || { tail -20 "$work/dkms-install-$k.log"; exit 1; }
    ko=$(find "/lib/modules/$k/updates/dkms" -name 'nvidia.ko*' | head -1)
    [ -n "$ko" ] || { echo "   cannot find nvidia.ko for $k after dkms install"; exit 1; }
    case "$ko" in
        *.zst) zstd -dcq "$ko" > "$work/nvidia.ko" ;;
        *) cp "$ko" "$work/nvidia.ko" ;;
    esac
    if nm "$work/nvidia.ko" | grep -q ' RmDeferNvpcfNotifyIfIdle$'; then
        echo "   $ko has the patch"
    else
        echo "   $ko lacks the patch"; exit 1
    fi
done
if [ ${#skipped[@]} -gt 0 ]; then
    echo "== these kernels were not rebuilt because their headers are missing: ${skipped[*]}"
    exit 1
fi
echo "== done; the patched modules load at the next boot, or now with $here/swap.sh"
