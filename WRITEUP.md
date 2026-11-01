# nvidia-live-swap: technical details

This is the technical side of [README.md](README.md), for the technically
minded: the firmware message and the NVIDIA driver code behind the Legion 7
16IRX9 problem, the two patches, the measurements, and what each script and
test does.

## Firmware and driver

The embedded controller (EC) is a small processor on the laptop's mainboard
that runs Lenovo's own firmware next to the main processor. In the `balanced`
power profile, which GNOME shows
as Balanced, the Legion 7 16IRX9's EC raises ACPI query 0x48 every 7 seconds.
The kernel answers the query by running Lenovo's ACPI method `_Q48`, which is
in the firmware table SSDT4. `_Q48` reads four power-limit bytes from the EC,
multiplies each by eight, writes the results to the firmware's power-limit
fields, and then always sends `Notify (\_SB.NPCF, 0xC0)`, whether or not the
values changed. `\_SB.NPCF` is the firmware device through which it tells the
NVIDIA driver about power limits. During a 35-second watch, the four bytes
stayed at 70, 119, 10 and 10.

The NVIDIA driver handles that notification in `rm_acpi_nvpcf_notify`, which
resumes the GPU from runtime suspend before passing the event on. Runtime
suspend is the sleep state the kernel puts an idle device in while the rest of
the system keeps running. The GPU therefore never sleeps for more than about a
second, and each wake keeps a kernel worker thread busy for about 2 seconds.
In the `performance` and `power-saver` profiles the EC does not raise query
0x48.

## Patches

`build.sh` applies the patches in `patches/` in order. Both apply to
595.99.02, 610.57.04, 615.71.09 and 615.78.08.

- `0001-nvidia-defer-NVPCF-notifications-until-runtime-resume.diff` (`0001`)
  is NVIDIA/open-gpu-kernel-modules pull request #1299 by Lars Nieradzik
  (lars76), head commit 555c98d1, unchanged. While the GPU is
  runtime-suspended, the patched driver records the notification and delivers
  one after the GPU next resumes, however many arrived in between. It also
  stops `rm_acpi_nvpcf_notify` from releasing a power reference it failed to
  take. It adds the functions `RmDeferNvpcfNotifyIfIdle` and
  `RmReplayNvpcfNotify`, and `build.sh` and `swap.sh` look for these names to
  recognize a patched driver.
- `0002-nvidia-defer-NVPCF-notifications-only-in-fine-grained-mode.diff`
  (`0002`) makes the deferral apply only when the driver runs fine-grained
  runtime power management (`NVreg_DynamicPowerManagement=2`, as on the Legion
  7 16IRX9 this was developed on). `RmDeferNvpcfNotifyIfIdle`, which the pull
  request adds, takes the mutex of the driver's dynamic power management
  without the mode check that the driver's own `os_ref_dynamic_power` and
  `os_unref_dynamic_power` make before they lock it. When the driver fails to
  create that mutex, it leaves the mutex NULL and turns power management off.
  The two existing functions then return before locking, but with the pull
  request alone a notification would make the new function dereference NULL.
  `0002` gives it the same mode check, so coarse-grained and disabled modes
  keep the existing notification path.

`build.sh` builds the driver from Ubuntu's package with both patches, and
`swap.sh` loads the result into the running system.

## Measurements

All in the `balanced` profile:

- With the stock 595.91.07 driver on 2026-10-15, two of eight notifications
  in a minute took 2.0 and 2.1 seconds, and the GPU slept for about 0.8
  seconds in every 28 seconds.
- On 2026-11-01, with the patched 595.99.02 after the first live swap, the EC
  raised query 0x48 every 6.97 seconds, each notification took 10 to 41
  microseconds, and the GPU suspended at 13:55:08 and stayed suspended.
- The same day, with the patched 615.71.09 after the move to that version, the
  queries came every 6.98 seconds, the nine notifications in a minute took 10
  to 29 microseconds, and the GPU suspended at 15:30:16 and stayed suspended.

## Old firmware package during a version change

The old driver reads its GSP firmware from disk whenever it initializes the
GPU. GSP is the GPU System Processor, a processor on the GPU itself, and the
driver loads this firmware into it. Initialization happens when a program
opens the GPU after the last one has closed it (`nv_open_device` calls
`rm_init_adapter` while the device is uninitialized). So until the swap, the
running old driver needs the files of the old firmware package, which step 3
of "Changing the driver version" in
[README.md](README.md#changing-the-driver-version) keeps.

## Files

### swap.sh

`swap.sh [--check]` runs as root and does the swap described in
[README.md](README.md#usage). It rejects any other argument with exit
status 2, before it looks at the system.

It finds the GPU's PCI address with `lib.sh`, and the GPU's DRM device files in
sysfs: the render node `/dev/dri/renderD*`, through which programs draw with
the GPU, and the KMS node `/dev/dri/cardN`, which this README calls the
display device. KMS is kernel mode setting, the kernel's interface for
configuring displays. Together with NVIDIA's own device files, `/dev/nvidia*`
and `/dev/nvidia-caps/*`, these are what a program holds open when it uses the
GPU. Linux unloads a module only after every process has closed and unmapped
the device files the module provides, so `swap.sh` sorts the processes that
hold one:

- The display device is never revoked. mutter is the display and window
  management code inside gnome-shell, GNOME Shell's process. It keeps its
  display state on the display device, and systemd-logind and systemd (PID 1)
  hold copies of mutter's descriptor. When the device appears, mutter opens
  it. mutter releases it again after its next display mode set, that is, the
  next time it sets the screens' modes through KMS
  (`meta_kms_impl_device_notify_modes_set` in mutter 46.2). When gnome-shell,
  systemd-logind and PID 1 are the device's only holders, the swap first runs
  `reapply-layout.py` as gnome-shell's user, on gnome-shell's session bus.
  That applies the current display layout again, which causes the mode set.
  The swap then waits up to 10 seconds for the release. `swap.sh` stops before
  it hides files, stops services or revokes handles when another process holds
  the device, when a monitor is connected to the NVIDIA GPU, or when mutter has
  not released the device after 10 seconds.
- Root processes in `lactd.service`, `nvidia-persistenced.service` or
  `nvidia-powerd.service`, which only watch or tune the GPU and start again
  cleanly, have their service stopped for the swap and started again
  afterwards. Any other root process holding the GPU stops the swap, including
  one in another service, because stopping that could end someone's work.
- Processes not owned by root, such as gnome-shell and Xwayland, have their
  handles revoked by `revoke.py`: each descriptor on `/dev/nvidia*`,
  `/dev/nvidia-caps/*` and the render node is replaced with `/dev/null`, and
  each mapping of those devices with zeroed memory. gdb held each program
  stopped for 0.1 to 2 seconds in the runs so far. `swap.sh` checks every
  remaining holder before it revokes any, so that a root holder stops the swap
  before any process has been changed. It runs `revoke.py` in its own session,
  so that a signal to `swap.sh`'s process group cannot stop gdb in the middle
  of an injected call.

A swap then runs in this order. It releases the display device as described
above, hides NVIDIA's EGL and Vulkan registration files, stops the services and
revokes the handles. It unloads `nvidia_drm`, `nvidia_modeset`, `nvidia_uvm`
and `nvidia`, loads from disk those of them that were loaded at the start,
moves the files back and starts the services again. It hides the registration
files `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`,
`/usr/share/vulkan/implicit_layer.d/nvidia_layers.json` and
`/usr/share/vulkan/icd.d/nvidia_icd.json` by adding `.hotswap` to their
names, so that a program started meanwhile does not open the GPU. A
registration file left hidden by an interrupted earlier run also stops the
swap.

`swap.sh` writes the end time of every swap it starts, failed ones included,
to `/run/nvidia-live-swap.last`, and starts a swap only when 20 seconds have
passed since then. `--check` reports the wait. The wait is needed because each
swap makes mutter switch its KMS thread, the thread that sends its display
updates to the kernel, out of real-time scheduling and back several times
through rtkit-daemon, and rtkit-daemon by default allows 25 such requests per
user in 20 seconds. Two swaps started 17 seconds apart reached that limit, and
mutter probed the reloaded device with the thread still real-time. The session
kept running.

On a failure or a signal (SIGINT, SIGTERM or SIGHUP), `swap.sh` exits with a
non-zero status after trying to restore what it changed. It loads from disk
every NVIDIA module that was loaded at the start and is missing, moves the
files back and starts the stopped services. Revoked handles stay revoked.

It logs `END` only when every module it loaded has the build ID of its file on
disk and the driver contains the patch. The build ID is an identifier that the
linker computes from a module's contents and stores in it. `swap.sh` compares
the one in `/sys/module/<module>/notes` with the one in the file `modprobe`
would load. For the patch, it looks for `RmDeferNvpcfNotifyIfIdle` and
`RmReplayNvpcfNotify` in `/proc/kallsyms`.

### revoke.py

`revoke.py [--dry-run] <pid> <path>...`, as root, replaces one process's file
descriptors on the given paths with `/dev/null`, by calling `dup2` from the
process's own fd 0, and its mappings of them with anonymous memory of the same
size and protection. The descriptor numbers stay taken, so a later ioctl on
one fails instead of reaching another file, and code that still holds a
pointer into a replaced mapping reads zeros instead of faulting. `--dry-run`
lists the descriptors and mappings without replacing them.

The replacement runs inside gdb. Attaching stops every thread of the process,
and only then does `revoke.py` read the descriptors and mappings from `/proc`.
With scheduler locking on, the other threads stay stopped while the injected
calls run in one thread. `revoke.py` checks each call's result before it makes
the next call. If a call fails, the calls before it stay in effect and
`revoke.py` exits 1. The injected calls (`dup2`, `mmap`, `__errno_location`)
do not take user-space locks, so they cannot wait for one held by a stopped
thread. `revoke.py` refuses PID 1, processes owned by root and processes whose
fd 0 is not `/dev/null`.

### reapply-layout.py

`reapply-layout.py [--check]`, as the desktop user, reconstructs the active
monitor configuration from Mutter's `GetCurrentState`, asks Mutter to validate
it, then applies it with Mutter's temporary method, which applies a layout
without the confirmation prompt and without saving it, and checks that the
active layout stayed the same. It keeps the active modes, positions, scales,
transforms, primary monitor and underscanning, and the layout mode where
Mutter supports changing it. `--check` stops after validation. It uses
Ubuntu's `/usr/bin/python3` and `python3-gi`. `swap.sh` runs it as
gnome-shell's user, on gnome-shell's session bus, when mutter holds the
display device. You can also run it by hand.

With Ubuntu's mutter 46.2-1ubuntu0.24.04.18 and the built-in screen on the
Intel GPU, applying the layout again released the display device and kept the
same resolution, refresh rate and scaling, once with 595.99.02 and once with
615.71.09. Both times `reapply-layout.py` was started by hand. On 2026-11-01 at
15:59 a swap released the device by itself. `swap.sh` ran `reapply-layout.py`
as the desktop user, mutter released the device 0.45 seconds later, the swap
went on, and the display configuration afterwards was the same as before.

### lib.sh

`lib.sh` finds the NVIDIA GPU's PCI address by looking for the first display
controller with NVIDIA's vendor ID, 0x10de. `swap.sh` and `trace.sh` source
it.

### build.sh

`build.sh [version]` runs as root. Without a version it builds the newest
NVIDIA version DKMS lists.

Ubuntu's `nvidia-dkms-<branch>-open` builds the kernel modules from source,
except the driver core, which it installs precompiled as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. NVIDIA's GitHub tag of
the same version has the source of that object. `build.sh` clones NVIDIA's tag
of the version and refuses when Ubuntu's kernel-module source differs from the
tag's. The comparison leaves out `dkms.conf`, `.gitignore` and the files that
only Ubuntu's tree has, such as the patches with Ubuntu's build fixes.
`build.sh` then applies the patches in `patches/`, builds the driver core,
checks that the result contains the patch, and installs it in place of the
precompiled copy in Ubuntu's DKMS source. A dpkg diversion keeps Ubuntu's copy
as `nv-kernel.o_binary.ubuntu`. The diversion tells dpkg to put the package's
own version of the file under that name, so the patched file stays in place.

It then rebuilds and installs the DKMS modules for every kernel that DKMS
lists for this driver version and for the running kernel, and checks that each
installed `nvidia.ko` contains the patch. When `dkms status` fails or the
running kernel's headers are missing, it stops before it downloads NVIDIA's
source or installs the diversion. It exits 1 at the end if it had to skip
another kernel whose headers are missing.

### trace.sh

`trace.sh [seconds]`, as root, watches the EC queries, the duration of each
NVPCF notification in the driver, and the GPU's runtime power state, for 60
seconds by default. Run it in `balanced`. It prints the current platform
profile first.

The kernel's EC driver logs each query only while its debug lines are turned
on through dynamic debug, and `trace.sh` turns on only those of the lines that
were off. It times the notifications with ftrace's `function_graph` tracer on
`nv_acpi_nvpcf_event`, the function in the driver that receives the
notification, in a private tracing instance. It checks every write to a
control file and starts the trace only after the function filter reads back
as `nv_acpi_nvpcf_event` alone, because with an empty filter `function_graph`
would trace every kernel function. It stops the trace and turns those debug
lines off again before printing, so the output covers exactly the sample. On
exit, including on a signal, it removes the instance and turns off any debug
lines it turned on.

## Tests

- `tests/check_swap.py`, as an ordinary user, runs `swap.sh` against mocked
  commands and a fake sysfs, `/proc` and tree of registration files. The cases
  are: - the refusals: the display device held by another process or held
  without gnome-shell, a monitor on the NVIDIA GPU, root holders, an unlisted
  service, a leftover hidden file, and a mistyped or extra argument -
  `--check` with and without mutter holding the display device - the release
  through `reapply-layout.py` run as gnome-shell's user, mutter not releasing
  the device, and a failing `reapply-layout.py` - a full swap, and a GPU
  numbered card1/renderD128 - failures to hide a file, revoke, unload and
  load, a missing patch, and a mismatched build ID - SIGTERM after a service
  stop, during an `rmmod` and after one - the wait after a recent swap, in a
  swap and in `--check`, an immediate start after an older swap, and the end
  time recorded after a failed swap - `tests/check_build.py`, as an ordinary
  user, runs `build.sh` against mocked commands and a fake `/usr/src` and
  `/lib/modules` tree. The cases are a failing `dkms status`, a version DKMS
  lists only as `added`, missing headers for the running kernel and for
  another kernel, a mistyped or extra argument, and a full build for two
  kernels. - `tests/check_layout.py`, as an ordinary user, checks how
  `reapply-layout.py` reconstructs current modes, mirrored monitors,
  underscanning, layout capabilities, D-Bus argument types and invalid inputs,
  without connecting to a desktop. `reapply-layout.py --check` validates the
  reconstructed configuration against the running Mutter. -
  `tests/check_trace_filter.py`, as an ordinary user, runs `trace.sh` against
  ordinary files in place of the tracing and debug control files, with a
  filter write that fails, and checks that it stops before selecting
  `function_graph` and removes the instance. - `tests/check_revoke.py`, as
  root, runs `revoke.py` against a throwaway two-thread process owned by the
  user `nobody`. It checks that `revoke.py` refuses PID 1 and a root-owned
  process, and that after the revoke the process keeps running and its second
  thread keeps reading through the replaced descriptor and mapping. -
  `tests/check_trace.py`, as root, checks that `trace.sh` leaves the tracing
  and debug settings as it found them, after a normal run and after SIGTERM. -
  `tests/check_egl.c`, as the desktop user, renders a shader into a pbuffer,
  an off-screen drawing surface, through NVIDIA's EGL device platform, with
  NVIDIA's EGL vendor library selected and `DISPLAY` and `WAYLAND_DISPLAY`
  unset, so it uses the GPU without Wayland or X11. It then checks the pixel
  it reads back. Step 8 of "Changing the driver version" in
  [README.md](README.md#changing-the-driver-version) has the commands that
  build and run it. After the swap to 615.71.09 it printed EGL 1.5 NVIDIA, the
  RTX 4070 Laptop GPU and OpenGL ES 3.2 NVIDIA 615.71.09, and read back the
  pixel 64 127 191 255, within the one unit per channel that the test allows
  around the expected 64 128 191 255. Programs started before a swap keep the
  old libraries, so this checks new programs only.