# nvidia-live-swap: technical details

The firmware and driver code behind the Legion 7 16IRX9 problem, the two
patches, the measurements, and how each script and test works.
[README.md](README.md) has the usage.

## Firmware and driver

In the `balanced` platform profile (Balanced in GNOME), the Legion 7 16IRX9's
embedded controller (EC), the mainboard processor that runs Lenovo's firmware
next to the CPU, raises ACPI query 0x48 every 7 seconds. The kernel runs
Lenovo's handler for it, the ACPI method `_Q48` in the firmware table SSDT4.
`_Q48` reads four power-limit bytes from the EC, multiplies each by eight,
writes the results to the firmware's power-limit fields, and then sends
`Notify (\_SB.NPCF, 0xC0)` every time, whether or not the values changed.
`\_SB.NPCF` is the device through which the firmware tells the NVIDIA driver
about power limits. During a 35-second watch the four bytes stayed at 70, 119,
10 and 10. In `performance` and `power-saver` the EC does not raise query
0x48.

The NVIDIA driver handles the notification in `rm_acpi_nvpcf_notify`, which
resumes the GPU from runtime suspend, the sleep state an idle device is in
while the system runs, before passing the event on. So the GPU never sleeps
for more than about a second, and each wake keeps a kernel worker thread busy
for about 2 seconds.

## Patches

`build.sh` applies the patches in `patches/` in order. Both apply to
595.99.02, 610.57.04, 615.71.09 and 615.78.08.

- `0001-nvidia-defer-NVPCF-notifications-until-runtime-resume.diff` is
  NVIDIA/open-gpu-kernel-modules pull request #1299 by Lars Nieradzik
  (lars76), head commit 555c98d1, unchanged. While the GPU is
  runtime-suspended, the driver records that a notification came and delivers
  one after the GPU next resumes, however many arrived in between. It also
  stops `rm_acpi_nvpcf_notify` from releasing a power reference it failed to
  take. It adds `RmDeferNvpcfNotifyIfIdle` and `RmReplayNvpcfNotify`, which
  `build.sh` and `swap.sh` look for to recognize a patched driver.
- `0002-nvidia-defer-NVPCF-notifications-only-in-fine-grained-mode.diff`
  limits the deferral to fine-grained runtime power management
  (`NVreg_DynamicPowerManagement=2`, which the Legion uses). The pull
  request's `RmDeferNvpcfNotifyIfIdle` locks the dynamic power management
  mutex without the mode check that `os_ref_dynamic_power` and
  `os_unref_dynamic_power` make before they lock it. If the driver fails to
  create that mutex, it leaves it NULL and turns power management off. The
  existing functions then return before locking, but the new one would
  dereference NULL on the next notification. `0002` gives it the same mode
  check, so coarse-grained and disabled modes keep the existing notification
  path.

## Measurements

All in the `balanced` profile:

- Stock 595.91.07, 2026-10-15: two of eight notifications in a minute took 2.0
  and 2.1 seconds, and the GPU slept about 0.8 seconds in every 28.
- Patched 595.99.02, 2026-11-01, after the first live swap: query 0x48 every
  6.97 seconds, each notification 10 to 41 microseconds, and the GPU suspended
  at 13:55:08 and stayed suspended.
- Patched 615.71.09, the same day, after the move to that version: queries
  every 6.98 seconds, nine notifications in a minute taking 10 to 29
  microseconds, and the GPU suspended at 15:30:16 and stayed suspended.

## Old firmware package during a version change

The driver loads its GSP firmware, for the GPU System Processor on the GPU,
from disk whenever it initializes the GPU. That happens when a program opens
the GPU after the last one has closed it (`nv_open_device` calls
`rm_init_adapter` while the device is uninitialized). So until the swap, the
old driver that is still running needs the old firmware package, which step 3
of "Changing the driver version" in
[README.md](README.md#changing-the-driver-version) keeps.

## Files

### swap.sh

`swap.sh [--check]` runs as root. Any other argument makes it exit with status
2 before it looks at the system.

It finds the GPU's PCI address with `lib.sh`, and the GPU's DRM device files
in sysfs: the render node `/dev/dri/renderD*`, through which programs draw
with the GPU, and the KMS node `/dev/dri/cardN`, which the README calls the
display device (KMS is kernel mode setting, the kernel's interface for
configuring displays). Together with `/dev/nvidia*` and `/dev/nvidia-caps/*`,
these are the files a program holds open when it uses the GPU. The kernel
unloads a module only after every process has closed and unmapped them, so
`swap.sh` sorts the holders:

- The display device is never revoked. mutter, the display and window
  management code in gnome-shell, keeps its display state on it, and
  systemd-logind and systemd (PID 1) hold copies of mutter's descriptor.
  mutter opens the device when it appears and closes it after its next display
  mode set (`meta_kms_impl_device_notify_modes_set` in mutter 46.2). If
  gnome-shell, systemd-logind and PID 1 are the only holders, `swap.sh` runs
  `reapply-layout.py` as gnome-shell's user on gnome-shell's session bus. That
  applies the current layout again, which causes the mode set, and `swap.sh`
  waits up to 10 seconds for the release. If another process holds the
  device, a monitor is connected to the NVIDIA GPU, or mutter hasn't released
  the device after 10 seconds, `swap.sh` stops before it renames files, stops
  services or revokes handles.
- Root processes in `lactd.service`, `nvidia-persistenced.service` or
  `nvidia-powerd.service`, which only watch or tune the GPU and start again
  cleanly, have their service stopped for the swap and started afterwards.
  Any other root process with the GPU open, also one in another service,
  stops the swap, because stopping it could end someone's work.
- Processes not owned by root, such as gnome-shell and Xwayland, have their
  handles revoked by `revoke.py`. Each descriptor on `/dev/nvidia*`,
  `/dev/nvidia-caps/*` and the render node becomes `/dev/null`, and each
  mapping of those devices becomes zeroed memory. gdb held each program
  stopped for 0.1 to 2 seconds in the runs so far. `swap.sh` checks all
  remaining holders before it revokes any, so a root holder stops the swap
  before any process is changed. It runs `revoke.py` in its own session, so a
  signal to `swap.sh`'s process group can't stop gdb in the middle of an
  injected call.

A swap goes in this order: release the display device; rename the
registration files `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`,
`/usr/share/vulkan/implicit_layer.d/nvidia_layers.json` and
`/usr/share/vulkan/icd.d/nvidia_icd.json` by adding `.hotswap`, so that
programs started meanwhile don't open the GPU; stop the services; revoke the
handles; unload `nvidia_drm`, `nvidia_modeset`, `nvidia_uvm` and `nvidia`;
load from disk the ones that were loaded at the start; rename the files back
and start the services. A registration file still renamed by an interrupted
earlier run stops the swap.

`swap.sh` writes the end time of every swap it starts, failed ones included,
to `/run/nvidia-live-swap.last`, and starts a swap only 20 seconds after that.
`--check` reports the wait. Each swap makes mutter ask rtkit-daemon several
times to take its KMS thread, the thread that sends display updates to the
kernel, out of real-time scheduling and back, and rtkit-daemon by default
allows 25 such requests per user in 20 seconds. Two swaps started 17 seconds
apart hit that limit: rtkit-daemon refused four requests, and mutter probed
the reloaded device with the thread still real-time. The session kept
running.

On a failure or a signal (SIGINT, SIGTERM or SIGHUP), `swap.sh` loads from
disk every NVIDIA module that was loaded at the start and is missing, renames
the files back, starts the stopped services and exits with a non-zero status.
Revoked handles stay revoked.

It logs `END` only when every module it loaded has the same build ID as its
file on disk and the driver contains the patch. The build ID is a hash the
linker stores in each module. `swap.sh` compares the one in
`/sys/module/<module>/notes` with the one in the file `modprobe` would load.
For the patch it looks for `RmDeferNvpcfNotifyIfIdle` and
`RmReplayNvpcfNotify` in `/proc/kallsyms`.

### revoke.py

`revoke.py [--dry-run] <pid> <path>...` runs as root. It replaces a process's
file descriptors on the given paths with `/dev/null`, with `dup2` from the
process's own fd 0, and its mappings of them with anonymous memory of the same
size and protection. The descriptor numbers stay taken, so a later ioctl on
one fails instead of reaching another file, and code that still holds a
pointer into a replaced mapping reads zeros instead of faulting. `--dry-run`
lists what it would replace.

The replacement runs inside gdb. gdb stops every thread when it attaches, and
`revoke.py` reads the descriptors and mappings from `/proc` only after that.
With scheduler locking on, the other threads stay stopped while the injected
calls run in one thread. Without it, gdb resumes every thread during each
call: in a test, a worker thread advanced 87 times during one injected 100 ms
call, and 0 times with locking on. `revoke.py` checks each call's result
before the next. If one fails, the earlier ones stay in effect and it exits 1.
The injected calls (`dup2`, `mmap`, `__errno_location`) don't take user-space
locks, so they can't wait for one held by a stopped thread. `revoke.py`
refuses PID 1, processes owned by root, and processes whose fd 0 isn't
`/dev/null`.

### reapply-layout.py

`reapply-layout.py [--check]` runs as the desktop user. It rebuilds the
active monitor configuration from mutter's `GetCurrentState`, asks mutter to
validate it, applies it with mutter's temporary method, which skips the
confirmation prompt and doesn't save the layout, and checks that the active
layout stayed the same. It keeps the modes, positions, scales, transforms,
primary monitor and underscanning, and the layout mode where mutter supports
changing it. `--check` stops after the validation. It needs Ubuntu's
`/usr/bin/python3` and `python3-gi`. `swap.sh` runs it as gnome-shell's user
on gnome-shell's session bus when mutter holds the display device. It also
runs by hand.

With Ubuntu's mutter 46.2-1ubuntu0.24.04.18 and the built-in screen on the
Intel GPU, running it by hand released the display device with the same
resolution, refresh rate and scaling, once on 595.99.02 and once on
615.71.09. On 2026-11-01 at 15:59 `swap.sh` ran it during a swap: mutter
released the device 0.45 seconds later, the swap went on, and the display
configuration afterwards matched the one before.

### lib.sh

Finds the NVIDIA GPU's PCI address: the first display controller with
NVIDIA's vendor ID, 0x10de. `swap.sh` and `trace.sh` source it.

### build.sh

`build.sh [version]` runs as root. Without a version it builds the newest
NVIDIA version DKMS lists.

Ubuntu's `nvidia-dkms-<branch>-open` builds the kernel modules from source,
except the driver core, which it installs precompiled as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. NVIDIA's GitHub tag of
the same version has its source. `build.sh` clones that tag and refuses if
Ubuntu's kernel-module source differs from it, apart from `dkms.conf`,
`.gitignore` and the files only Ubuntu's tree has, such as its build-fix
patches. Then it applies `patches/`, builds the driver core, checks that the
result contains the patch, and installs it in place of Ubuntu's precompiled
copy. The dpkg diversion makes dpkg put the package's own version of that file
under `nv-kernel.o_binary.ubuntu`, so package operations leave the patched
file in place.

It then rebuilds and installs the DKMS modules for every kernel DKMS lists
for that driver version, plus the running kernel, and checks that each
installed `nvidia.ko` contains the patch. If `dkms status` fails or the
running kernel's headers are missing, it stops before it downloads NVIDIA's
source or installs the diversion. If it had to skip another kernel because
its headers are missing, it exits 1 at the end.

### trace.sh

`trace.sh [seconds]` runs as root, for 60 seconds by default, in `balanced`.
It prints the platform profile, then watches the EC queries, the duration of
each notification in the driver, and the GPU's runtime power state.

The kernel's EC driver logs queries only while its dynamic-debug lines are
on, and `trace.sh` turns on just the lines that were off. It times the
notifications with ftrace's `function_graph` tracer on `nv_acpi_nvpcf_event`,
the driver function that receives the notification, in a private tracing
instance. Apart from the two writes in its exit handler, it checks every
write to a control file, and it starts tracing only after the function filter
reads back as `nv_acpi_nvpcf_event` alone, because with an empty filter
`function_graph` would trace every kernel function. It stops the trace and
turns the debug lines back off before printing, so the output covers exactly
the sample. On exit, including on a signal, it removes the instance and turns
off any debug lines it turned on.

## Tests

- `tests/check_swap.py`, as an ordinary user, runs `swap.sh` against mocked
  commands and a fake sysfs, `/proc` and registration-file tree. Its cases:
  - the refusals: the display device held by another process or without
    gnome-shell, a monitor on the NVIDIA GPU, root holders, an unlisted
    service, a file left renamed, a mistyped or extra argument
  - `--check` with and without mutter holding the display device
  - the release through `reapply-layout.py` as gnome-shell's user, a release
    that doesn't come, and a failing `reapply-layout.py`
  - a full swap, and a GPU numbered card1/renderD128
  - failures to rename a file, revoke, unload and load, a missing patch, and a
    mismatched build ID
  - SIGTERM after a service stop, during an `rmmod` and after one
  - the wait after a recent swap, in a swap and in `--check`, an immediate
    start after an older swap, and the end time recorded after a failed swap
- `tests/check_build.py`, as an ordinary user, runs `build.sh` against mocked
  commands and a fake `/usr/src` and `/lib/modules`: a failing
  `dkms status`, a version DKMS lists only as `added`, missing headers for the
  running kernel and for another kernel, a mistyped or extra argument, and a
  full build for two kernels.
- `tests/check_layout.py`, as an ordinary user, checks how
  `reapply-layout.py` rebuilds current modes, mirrored monitors,
  underscanning, layout capabilities, D-Bus argument types and invalid input,
  without a desktop. `reapply-layout.py --check` validates the rebuilt
  configuration against the running mutter.
- `tests/check_trace_filter.py`, as an ordinary user, runs `trace.sh` against
  ordinary files in place of the tracing and debug control files, with a
  filter write that fails, and checks that it stops before selecting
  `function_graph` and removes the instance.
- `tests/check_revoke.py`, as root, runs `revoke.py` against a throwaway
  two-thread process owned by the user `nobody`. It checks that `revoke.py`
  refuses PID 1 and a root-owned process, and that after the revoke the
  process keeps running and its second thread keeps reading through the
  replaced descriptor and mapping.
- `tests/check_trace.py`, as root, checks that `trace.sh` leaves the tracing
  and debug settings as it found them, after a normal run and after SIGTERM.
- `tests/check_egl.c`, as the desktop user, renders a shader into an
  off-screen pbuffer through NVIDIA's EGL device platform, with NVIDIA's EGL
  vendor library selected and `DISPLAY` and `WAYLAND_DISPLAY` unset, and
  checks the pixel it reads back. Step 8 of "Changing the driver version" in
  [README.md](README.md#changing-the-driver-version) has the build and run
  commands. After the swap to 615.71.09 it printed EGL 1.5 NVIDIA, the RTX
  4070 Laptop GPU and OpenGL ES 3.2 NVIDIA 615.71.09, and read
  64 127 191 255 (expected 64 128 191 255, with one unit of tolerance per
  channel). It covers programs started after a swap only; older ones keep the
  old libraries.
