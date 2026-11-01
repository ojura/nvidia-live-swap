# nvidia-live-swap

Replace the NVIDIA kernel driver on a running Ubuntu GNOME desktop without
logging out or rebooting.

`swap.sh` unloads the loaded `nvidia`, `nvidia_modeset`, `nvidia_drm` and
`nvidia_uvm` modules and loads the ones installed on disk while the session
keeps running. Linux unloads a module only after every program has closed
its devices, so `swap.sh` first takes the GPU away from the programs that
hold it. It stops the GPU monitoring services `lactd`,
`nvidia-persistenced` and `nvidia-powerd` for the duration, if they run. In
gnome-shell, Xwayland and every other program not owned by root, it
replaces each descriptor on `/dev/nvidia*` and on the NVIDIA render node
with `/dev/null`, and each mapping of those devices with zeroed memory. It
does this through gdb, which held each program stopped for 0.1 to 2 seconds
in the runs so far.

On 2026-11-01 it replaced the driver of a Legion 7 16IRX9 (Ubuntu 24.04,
kernel 7.0, GNOME 46 on Wayland, NVIDIA open kernel modules 595.99.02) in
6.7 seconds, and gnome-shell, Xwayland and every open program kept
running. Later the same day it moved the same session from 595.99.02 to
615.71.09 in 6.8 seconds.

`swap.sh --check` only reads: it reports what holds the GPU and whether a
swap can go ahead now.

## What it costs

- A program whose GPU handles were taken keeps running but cannot use the
  NVIDIA GPU until it restarts. On this laptop the internal screen runs on
  the Intel GPU and keeps working, but gnome-shell cannot drive a monitor on
  the HDMI port, or on the DisplayPort or USB-C port wired to the NVIDIA
  GPU, until the next login, and mutter may crash when one is connected.
- `swap.sh` never revokes the NVIDIA display device (`/dev/dri/cardN`):
  mutter keeps its display state on it, and systemd-logind and systemd hold
  copies of mutter's descriptor. mutter opens the device when it appears
  and releases it after its next display mode set
  (`meta_kms_impl_device_notify_modes_set` in mutter 46.2). When
  gnome-shell, systemd-logind and systemd are its only holders, the swap
  first runs `reapply-layout.py` as gnome-shell's user, which applies the
  current display layout again and so causes that mode set, and waits up
  to 10 seconds for the release. On Ubuntu's mutter
  46.2-1ubuntu0.24.04.18, with the built-in screen on Intel, applying the
  layout again released the device and kept the same resolution, refresh
  rate and scaling; through `swap.sh` itself, that step has run only in the
  mocked tests so far. Another holder of the device, a monitor connected to
  the NVIDIA GPU, or a release that does not come stops the swap before it
  hides files, stops services or revokes handles.
- A root process that holds the GPU stops the swap, unless it belongs to
  `lactd`, `nvidia-persistenced` or `nvidia-powerd`.
- When a swap fails after the revokes, the revoked programs stay without the
  NVIDIA GPU, and the modules `swap.sh` loads again come from disk, which
  can be a different build from the one it unloaded.
- A new driver version also replaces NVIDIA's user-space libraries, and
  NVIDIA's libraries refuse a kernel module of another version. Programs
  started between installing the new packages and the swap cannot use the
  NVIDIA GPU, and programs that were running before keep the old libraries
  until they restart; for gnome-shell, until the next login.

## Moving to a new driver version

This order moved the running session from 595.99.02 to 615.71.09 on
2026-11-01:

1. `sudo ./swap.sh --check`.
2. Restore Ubuntu's driver core of the installed version and remove its
   diversion, with the two commands under "After an NVIDIA driver update"
   below, so that apt removes the old package cleanly.
3. `sudo apt-get install nvidia-driver-<branch>-open`. apt replaces the old
   packages, keeps the old firmware package and marks it for autoremove.
   Keep it until after the swap: the old driver reads its GSP firmware from
   disk whenever it initializes the GPU, which happens when a program opens
   the GPU after the last one has closed it (`nv_open_device` calls
   `rm_init_adapter` while the device is uninitialized).
4. Ubuntu's DKMS hook builds the new version only for the running kernel.
   For every other kernel that should keep an NVIDIA driver, run
   `sudo dkms install -m nvidia -v <version> -k <kernel>`.
5. `sudo ./build.sh <version>`.
6. `sudo ./swap.sh`.
7. `sudo apt-get remove nvidia-firmware-<old branch>-<old version>`.

For 615.71.09, apt replaced 20 NVIDIA packages with 21, including the
32-bit libraries. `nvidia-dkms-615-open` adds two Ubuntu build fixes for
7.0 kernels, which change only the Linux interface code, not the driver
core. The swap needed only `lactd` stopped, because the earlier swap had
already revoked the other holders.

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

`build.sh` applies the patches in `patches/` in order:

- `0001` is NVIDIA/open-gpu-kernel-modules pull request #1299 by Lars
  Nieradzik (lars76), head commit 555c98d1, unchanged. While the GPU is
  runtime-suspended, the patched driver records the notification and
  delivers one after the GPU next resumes. It also stops the function from
  releasing a power reference it failed to take.
- `0002` makes the deferral apply only when the driver runs fine-grained
  runtime power management (`NVreg_DynamicPowerManagement=2`, as here). The
  PR's new helper takes the dynamic-power mutex unconditionally, but the
  driver leaves that mutex NULL and turns power management off when
  creating it fails, so a notification would dereference NULL. In coarse
  mode the original path does not resume the GPU for a notification, so
  deferring it would only delay it.

Both apply to 595.99.02, 610.57.04, 615.71.09 and 615.78.08. `build.sh`
builds Ubuntu's driver package with them, and `swap.sh` loads the result
into the running system.

Results on 2026-11-01 in `balanced`, after each live swap: with 595.99.02
the EC raised query 0x48 every 6.97 seconds, each notification took 10 to
41 microseconds, and the GPU suspended at 13:55:08 and stayed suspended.
With 615.71.09 the queries came every 6.98 seconds, the nine notifications
in a minute took 10 to 29 microseconds, and the GPU suspended at 15:30:16
and stayed suspended. Before the patch, two of eight notifications in a
minute took 2.0 and 2.1 seconds, and the GPU slept for less than a second
in every 28.

## Files

- `swap.sh [--check]`, as root: the live swap described above. It rejects
  any other argument. It first sorts the processes holding the GPU:
  mutter's hold on the display device is released as described above, and
  any other holder of it stops the swap; root
  processes in `lactd`, `nvidia-persistenced` or `nvidia-powerd`, which
  only watch or tune the GPU, have their service stopped and started again
  afterwards; any other root process stops the swap, including one in
  another service, because stopping that could end someone's work; and
  processes not owned by root have their handles revoked. A registration
  file left hidden by an interrupted earlier run also stops it. During the
  swap it hides NVIDIA's EGL and Vulkan registration files, so that a
  program started meanwhile does not open the GPU. On a failure or a signal
  it exits with a non-zero status after trying to restore what it changed:
  it loads from disk every NVIDIA module that was loaded at the start and
  is missing, moves the files back and starts the stopped services.
  Revoked handles stay revoked. It logs `END` only when every module it
  loaded has the build ID of its file on disk and the driver contains the
  patch.
- `revoke.py [--dry-run] <pid> <path>...`, as root: replaces one process's
  file descriptors on the given paths with `/dev/null` (its own fd 0) and
  its mappings of them with anonymous memory of the same size and
  protection. gdb stops every thread of the process before the descriptors
  and mappings are read from `/proc`, and scheduler locking keeps the other
  threads stopped while the injected calls run in one thread; each call's
  result is checked before the next. The injected calls (`dup2`, `mmap`,
  `__errno_location`) do not take user-space locks, so they cannot wait for
  one held by a stopped thread. It refuses PID 1, processes owned by root
  and processes whose fd 0 is not `/dev/null`.
- `reapply-layout.py [--check]`, as the desktop user: reconstructs the
  active monitor configuration from Mutter's `GetCurrentState`, asks
  Mutter to validate it, then applies it temporarily and checks that the
  active layout stayed the same. It preserves the active modes, positions,
  scales, transforms, primary monitor and underscanning. `--check` stops
  after validation. It uses Ubuntu's `/usr/bin/python3` and `python3-gi`.
  `swap.sh` runs it as gnome-shell's user, on gnome-shell's session bus,
  when mutter holds the display device; it also runs on its own.
- `lib.sh`: finds the NVIDIA GPU's PCI address, the first display
  controller with NVIDIA's vendor ID.
- `build.sh [version]`, as root: clones NVIDIA's tag of the version, applies
  the patches in `patches/`, builds the driver core and installs it in
  place of the precompiled copy in Ubuntu's DKMS source,
  `/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. A dpkg diversion
  keeps Ubuntu's copy as `nv-kernel.o_binary.ubuntu`. It then rebuilds and
  installs the DKMS modules for every kernel that has this driver version,
  checks that each installed `nvidia.ko` contains the patch, and exits 1 if
  it had to skip a kernel whose headers are missing. It refuses when
  Ubuntu's kernel-module source differs from NVIDIA's tag.
- `trace.sh [seconds]`, as root: watches, for 60 seconds by default, the EC
  queries, the duration of each NVPCF notification in the driver and the
  GPU's runtime power state. Run it in `balanced`. The function trace runs
  in a private tracing instance, and only the EC driver's debug lines that
  were off get turned on. It checks every write to a control file and
  starts the trace only after the function filter reads back as
  `nv_acpi_nvpcf_event` alone, because with an empty filter
  `function_graph` would trace every kernel function. It stops the trace and
  turns those debug lines off again before printing, so the output covers
  exactly the sample. On exit, including on a signal, the instance is
  removed.
- `tests/check_swap.py`, as an ordinary user: runs `swap.sh` against mocked
  commands and a fake sysfs, `/proc` and registration-file tree. The cases
  are the refusals (display device held by another process or without
  gnome-shell, a monitor on the NVIDIA GPU, root holders, an unlisted
  service, a leftover hidden file, a mistyped or extra argument), `--check`
  with and without mutter holding the display device, the release through
  `reapply-layout.py` run as gnome-shell's user, a release that does not
  come, a failing layout helper, a full swap, a GPU numbered
  card1/renderD128, failures to hide a file, revoke, unload and load, a
  missing patch, a mismatched build ID, and SIGTERM after a service stop,
  during an `rmmod` and after one.
- `tests/check_build.py`, as an ordinary user: runs `build.sh` against
  mocked commands and a fake `/usr/src` and `/lib/modules` tree. The cases
  are a failing `dkms status`, a version DKMS lists only as `added`, missing
  headers for the running kernel and for another kernel, a mistyped or
  extra argument, and a full build for two kernels.
- `tests/check_layout.py`, as an ordinary user: checks reconstruction of
  current modes, mirrored monitors, underscanning, layout capabilities,
  D-Bus argument types and invalid inputs. `reapply-layout.py --check`
  validates the reconstructed configuration against the running Mutter.
- `tests/check_trace_filter.py`, as an ordinary user: runs `trace.sh`
  against ordinary files in place of the tracing and debug control files,
  with a filter write that fails, and checks that it stops before selecting
  `function_graph` and removes the instance.
- `tests/check_revoke.py` and `tests/check_trace.py`, as root: run
  `revoke.py` against a throwaway two-thread process owned by the user
  `nobody`, and check that `trace.sh` leaves the tracing and debug settings
  as it found them, after a normal run and after SIGTERM.

## After an NVIDIA driver update

A new driver version installs its own `/usr/src/nvidia-<version>` with
NVIDIA's precompiled core, so the GPU wakes again until `sudo ./build.sh`
has run for that version. After a kernel update, DKMS builds the new
kernel's modules from `/usr/src/nvidia-<version>`, which holds the patched
core for as long as the diversion is in place.

Before a driver version whose source holds the patched core is removed,
restore Ubuntu's copy, so that the package removes its files cleanly:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

The same two commands, followed by `dkms build --force` and
`dkms install --force` for each kernel, undo the patch. Without `--force`,
`dkms build` skips a kernel whose modules are already built.
