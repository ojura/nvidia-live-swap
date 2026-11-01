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
running.

`swap.sh --check` only reads: it reports what holds the GPU and whether a
swap can go ahead now. If it reports display-device holders on an unused
NVIDIA GPU, run `./reapply-layout.py` as the desktop user, without sudo,
then repeat `sudo ./swap.sh --check`. The layout helper reapplies the active
monitor configuration temporarily; it does not change the saved layout.

## What it costs

- A program whose GPU handles were taken keeps running but cannot use the
  NVIDIA GPU until it restarts. On this laptop the internal screen runs on
  the Intel GPU and keeps working, but gnome-shell cannot drive a monitor on
  the HDMI port, or on the DisplayPort or USB-C port wired to the NVIDIA
  GPU, until the next login, and mutter may crash when one is connected.
- `swap.sh` never takes away the NVIDIA display device (`/dev/dri/cardN`):
  mutter keeps its display state on it, and systemd-logind and systemd hold
  copies of mutter's descriptor. While the device is held, `swap.sh` exits
  at that check, before its first change. mutter opens the device when it
  appears and releases its temporary reference after the next display mode
  set (`meta_kms_impl_device_notify_modes_set` in mutter 46.2).
  `reapply-layout.py` triggers that mode set with the current layout. On
  Ubuntu's mutter 46.2-1ubuntu0.24.04.18, with the built-in screen on Intel,
  this released the descriptors held by gnome-shell, systemd-logind and
  PID 1 while keeping the same resolution, refresh rate and scaling. An
  active display driven by NVIDIA still keeps its device in use.
- A root process that holds the GPU stops the swap, unless it belongs to
  `lactd`, `nvidia-persistenced` or `nvidia-powerd`.
- When a swap fails after the revokes, the revoked programs stay without the
  NVIDIA GPU, and the modules `swap.sh` loads again come from disk, which
  can be a different build from the one it unloaded.
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

Result on 2026-11-01, driver 595.99.02, `balanced`, after the live swap: the
EC raised query 0x48 every 6.97 seconds, each notification took 10 to 41
microseconds, and the GPU suspended at 13:55:08 and stayed suspended. Before
the patch, two of eight notifications in a minute took 2.0 and 2.1 seconds,
and the GPU slept for less than a second in every 28.

## Files

- `swap.sh [--check]`, as root: the live swap described above. It rejects
  any other argument. Before its first change it sorts the processes
  holding the GPU: holders of the display device stop the swap; root
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
  are the refusals (display device held, root holders, an unlisted service,
  a leftover hidden file, a mistyped or extra argument), `--check`, a full
  swap, a GPU numbered card1/renderD128, failures to hide a file, revoke,
  unload and load, a missing patch, a mismatched build ID, and SIGTERM
  after a service stop, during an `rmmod` and after one.
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
