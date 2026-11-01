# nvidia-nvpcf-defer

Keeps the NVIDIA GPU of the Legion 7 16IRX9 asleep in the `balanced` power
profile, by building Ubuntu's NVIDIA driver with NVIDIA's open pull request
#1299.

## The problem

In `balanced`, the laptop's embedded controller raises ACPI query 0x48 every
7 seconds. Lenovo's `_Q48` method in SSDT4 copies four power-limit bytes from
the EC and then always sends `Notify (\_SB.NPCF, 0xC0)`, whether or not the
values changed. The NVIDIA driver handles that notification in
`rm_acpi_nvpcf_notify`, which resumes the GPU from runtime suspend before
passing the event on. The GPU therefore never sleeps for more than about a
second, and each wake keeps a kernel worker busy for about 2 seconds. In
`performance` and `power-saver` the EC does not send query 0x48.

`pr1299.diff` is NVIDIA/open-gpu-kernel-modules pull request #1299 by Lars
Nieradzik (lars76), head commit 555c98d1. While the GPU is runtime-suspended,
the patched driver records the notification and delivers one after the GPU
next resumes. It also stops the function from releasing a power reference it
failed to take. The patch applies to 595.99.02, 610.57.04, 615.71.09 and
615.78.08.

## Files

- `build.sh [version]`, as root: clones NVIDIA's tag of the version, applies
  the patch, builds the driver core and installs it in place of the
  precompiled copy in Ubuntu's DKMS source,
  `/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. A dpkg diversion
  keeps Ubuntu's copy as `nv-kernel.o_binary.ubuntu`. It then rebuilds and
  installs the DKMS modules for every kernel that has this driver version,
  and checks that each installed `nvidia.ko` contains the patch. It refuses
  when Ubuntu's kernel-module source differs from NVIDIA's tag.
- `swap.sh`, as root: loads the modules installed on disk without ending the
  GNOME session. It hides NVIDIA's EGL and Vulkan registration files for the
  duration, stops `lactd`, revokes the GPU handles of every process that
  holds them, unloads and reloads `nvidia_drm`, `nvidia_modeset`,
  `nvidia_uvm` and `nvidia`, and restores the files and `lactd`. If a process
  still holds the GPU after the revokes, it restores everything and stops
  before unloading.
- `revoke.py <pid>`, as root: through gdb, replaces each of the process's
  file descriptors on an NVIDIA device with `/dev/null` (its fd 0) and each
  NVIDIA device mapping with anonymous memory of the same size and
  protection.
- `trace.sh`, as root: watches for 60 seconds the EC queries, the duration of
  each NVPCF notification in the driver and the GPU's runtime power state.
  Run it in `balanced`.

## After an NVIDIA driver update

A new driver version installs its own `/usr/src/nvidia-<version>` with
NVIDIA's precompiled core, so the GPU wakes again until `sudo ./build.sh`
has run for that version. Kernel updates need nothing: DKMS rebuilds the
modules from the patched source.

Before a driver version whose source holds the patched core is removed,
restore Ubuntu's copy, so that the package removes its files cleanly:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

The same two commands, followed by `dkms build` and `dkms install --force`
for each kernel, undo the patch.

## Limits of a live swap

- A process whose handles were revoked keeps running without access to the
  NVIDIA GPU until it restarts. For gnome-shell this means that a monitor on
  the HDMI port, or on the DisplayPort or USB-C port wired to the NVIDIA GPU,
  does not work until the next login, and mutter may crash when one is
  connected.
- After the NVIDIA display device `/dev/dri/card0` reappears, mutter keeps it
  open until its next display mode set
  (`meta_kms_impl_device_notify_modes_set` in mutter 46.2), and
  systemd-logind and systemd keep copies of that file descriptor while it
  does. A second swap in the same session therefore needs a display
  configuration change first. This has not been tested.

## Result

2026-11-01, driver 595.99.02, `balanced`, after the live swap: the EC raised
query 0x48 every 6.97 seconds, each notification took 10 to 41 microseconds,
and the GPU suspended at 13:55:08 and stayed suspended. Before the patch,
two of eight notifications in a minute took 2.0 and 2.1 seconds, and the GPU
slept for less than a second in every 28.
