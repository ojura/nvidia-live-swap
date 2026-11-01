# nvidia-live-swap

Replace the NVIDIA kernel driver on a running Ubuntu GNOME desktop without
logging out or rebooting.

`swap.sh` unloads the loaded `nvidia`, `nvidia_modeset`, `nvidia_drm` and
`nvidia_uvm` modules and loads the ones installed on disk while the session
keeps running. Linux unloads a module only when no program has its devices
open, so `swap.sh` first takes the GPU away from the programs that hold it.
It stops system services such as `lactd` for the duration. In gnome-shell,
Xwayland and every other program not owned by root it replaces every NVIDIA
file descriptor with `/dev/null` and every NVIDIA device mapping with empty
memory, through gdb, which holds each program stopped for 0.1 to 2 seconds in
the runs so far.

On 2026-11-01 it replaced the driver of a Legion 7 16IRX9 (Ubuntu 24.04,
kernel 7.0, GNOME 46 on Wayland, NVIDIA open kernel modules 595.99.02) in
6.7 seconds, and gnome-shell, Xwayland and every open window kept
running.

`swap.sh --check` reports what holds the GPU and whether a swap can go ahead
now, without changing anything.

## What it costs

- A program whose GPU handles were taken keeps running but cannot use the
  NVIDIA GPU until it restarts. On this laptop the internal screen runs on
  the Intel GPU, so nothing visible changes, but gnome-shell cannot drive a
  monitor on the HDMI port, or on the DisplayPort or USB-C port wired to the
  NVIDIA GPU, until the next login, and mutter may crash when one is
  connected.
- `swap.sh` never takes away the NVIDIA display device (`/dev/dri/cardN`):
  mutter keeps its display state on it, and systemd-logind and systemd hold
  copies of mutter's descriptor. While the device is held, `swap.sh` stops
  without changing anything. mutter opens the device when it appears and
  releases it after its next display mode set (in mutter 46.2,
  `meta_kms_impl_device_notify_modes_set`). The mode set at login released
  it, so the first swap of the session went through; after a swap mutter
  holds the new device again. Whether a display configuration change
  releases it has not been tested.
- A root process that holds the GPU outside a systemd system service stops
  the swap.
- Only a swap to the same driver version has been tested. A new version also
  replaces NVIDIA's user-space libraries, and NVIDIA's libraries refuse a
  kernel module of another version, so programs started between installing
  the new packages and the swap cannot use the NVIDIA GPU.

## The driver fix it was built for

In the `balanced` power profile, the Legion 7 16IRX9's embedded controller
raises ACPI query 0x48 every 7 seconds. Lenovo's `_Q48` method in SSDT4
copies four power-limit bytes from the EC and then always sends
`Notify (\_SB.NPCF, 0xC0)`, whether or not the values changed. The NVIDIA
driver handles that notification in `rm_acpi_nvpcf_notify`, which resumes
the GPU from runtime suspend before passing the event on. The GPU therefore
never sleeps for more than about a second, and each wake keeps a kernel
worker busy for about 2 seconds. In `performance` and `power-saver` the EC
does not send query 0x48.

`pr1299.diff` is NVIDIA/open-gpu-kernel-modules pull request #1299 by Lars
Nieradzik (lars76), head commit 555c98d1. While the GPU is runtime-suspended,
the patched driver records the notification and delivers one after the GPU
next resumes. It also stops the function from releasing a power reference it
failed to take. The patch applies to 595.99.02, 610.57.04, 615.71.09 and
615.78.08.

`build.sh` builds Ubuntu's driver package with this patch, and `swap.sh`
loads the result into the running system.

Result on 2026-11-01, driver 595.99.02, `balanced`, after the live swap: the
EC raised query 0x48 every 6.97 seconds, each notification took 10 to 41
microseconds, and the GPU suspended at 13:55:08 and stayed suspended. Before
the patch, two of eight notifications in a minute took 2.0 and 2.1 seconds,
and the GPU slept for less than a second in every 28.

## Files

- `swap.sh [--check]`, as root: the live swap described above. Before any
  change it sorts the processes holding the GPU: holders of the display
  device stop the swap, root processes inside a systemd system service have
  their service stopped and started again afterwards, other root processes
  stop the swap, and processes not owned by root have their handles revoked. During
  the swap it hides NVIDIA's EGL and Vulkan registration files, so that a
  program started meanwhile does not open the GPU. If the GPU is still held
  after the revokes, it restores everything and stops before unloading.
- `revoke.py [--dry-run] <pid> <path>...`, as root: replaces one process's
  file descriptors on the given paths with `/dev/null` (its own fd 0) and its
  mappings of them with anonymous memory of the same size and protection.
  gdb stops every thread of the process before the descriptors and mappings
  are read from `/proc`, and each call's result is checked before the next.
  It refuses PID 1, processes owned by root and processes whose fd 0 is not
  `/dev/null`.
- `lib.sh`: finds the NVIDIA GPU's PCI address, the first display controller
  with NVIDIA's vendor ID.
- `build.sh [version]`, as root: clones NVIDIA's tag of the version, applies
  the patch, builds the driver core and installs it in place of the
  precompiled copy in Ubuntu's DKMS source,
  `/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. A dpkg diversion
  keeps Ubuntu's copy as `nv-kernel.o_binary.ubuntu`. It then rebuilds and
  installs the DKMS modules for every kernel that has this driver version,
  checks that each installed `nvidia.ko` contains the patch, and exits 1 if
  a kernel had to be skipped for lack of headers. It refuses when Ubuntu's
  kernel-module source differs from NVIDIA's tag.
- `trace.sh`, as root: watches for 60 seconds the EC queries, the duration
  of each NVPCF notification in the driver and the GPU's runtime power
  state. Run it in `balanced`.

## After an NVIDIA driver update

A new driver version installs its own `/usr/src/nvidia-<version>` with
NVIDIA's precompiled core, so the GPU wakes again until `sudo ./build.sh`
has run for that version. A kernel update needs nothing more: DKMS builds
the new kernel's modules from `/usr/src/nvidia-<version>`, which holds the
patched core for as long as the diversion is in place.

Before a driver version whose source holds the patched core is removed,
restore Ubuntu's copy, so that the package removes its files cleanly:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

The same two commands, followed by `dkms build` and `dkms install --force`
for each kernel, undo the patch.
