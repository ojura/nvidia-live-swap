#!/bin/bash
# Build NVIDIA's driver core with pr1299.diff and install it through Ubuntu's
# DKMS package for every kernel that has this driver version.
#
# Ubuntu's nvidia-dkms-<branch>-open builds the kernel modules from source,
# except for the driver core, which it ships precompiled as
# /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary. NVIDIA's GitHub tag of
# the same version holds the source of that object. This script builds it
# from the tag with the patch, puts it in place of Ubuntu's copy (a dpkg
# diversion keeps Ubuntu's copy as nv-kernel.o_binary.ubuntu), and rebuilds
# the DKMS modules. The new modules load at the next boot, or now with
# swap.sh.
#
# usage (as root): build.sh [version]
#   version defaults to the newest nvidia version DKMS knows about.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
ver=${1:-$(dkms status nvidia | sed -nE 's#^nvidia/([0-9.]+),.*#\1#p' | sort -uV | tail -1)}
src=/usr/src/nvidia-$ver
obj=$src/nvidia/nv-kernel.o_binary
[ -d "$src" ] || { echo "$src does not exist"; exit 1; }

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

echo "== applying pr1299.diff and building the driver core"
git -C "$work/ogkm" apply "$here/pr1299.diff"
make -C "$work/ogkm/src/nvidia" -j"$(nproc)" > "$work/build.log" 2>&1 || { tail -30 "$work/build.log"; exit 1; }
built=$work/ogkm/src/nvidia/_out/Linux_x86_64/nv-kernel.o
nm "$built" | grep -q ' RmDeferNvpcfNotifyIfIdle$' || { echo "built object lacks the patch"; exit 1; }

if ! dpkg-divert --list "$obj" | grep -q .; then
    dpkg-divert --local --rename --divert "$obj.ubuntu" --add "$obj"
fi
install -m 644 -o root -g root "$built" "$obj"
echo "== installed the patched driver core as $obj"

for k in $(dkms status "nvidia/$ver" | sed -nE "s#^nvidia/$ver, ([^,]+),.*#\1#p"); do
    [ -d "/lib/modules/$k/build" ] || { echo "== $k: no headers, skipped"; continue; }
    echo "== $k"
    dkms build -m nvidia -v "$ver" -k "$k" --force > "$work/dkms-$k.log" 2>&1 || { tail -20 "$work/dkms-$k.log"; exit 1; }
    dkms install -m nvidia -v "$ver" -k "$k" --force > "$work/dkms-install-$k.log" 2>&1 || { tail -20 "$work/dkms-install-$k.log"; exit 1; }
    ko=$(find "/lib/modules/$k/updates/dkms" -name 'nvidia.ko*' | head -1)
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
echo "== done; the patched modules load at the next boot, or now with $here/swap.sh"
