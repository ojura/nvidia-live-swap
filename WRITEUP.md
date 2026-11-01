# nvidia-live-swap: technical details

[README.md](README.md) explains how to use the scripts.

## Firmware and driver

The Legion 7 16IRX9 has an embedded controller (EC), a small processor on the
mainboard that runs Lenovo's firmware next to the CPU. The EC signals events to
the kernel as numbered ACPI queries. ACPI is the standard interface between the
firmware and the operating system for power management and device configuration.
For each query, the kernel runs the matching method from the firmware's ACPI
tables.

In the `balanced` platform profile, which GNOME shows as Balanced, the EC raises
query 0x48 every 7 seconds. The matching method is Lenovo's `_Q48` in the
firmware table SSDT4. `_Q48` reads four power-limit bytes from the EC and
multiplies each byte by eight. It writes the results to the firmware's
power-limit fields. Then it sends `Notify (\_SB.NPCF, 0xC0)` every time, whether
or not the values changed. `\_SB.NPCF` is the device through which the firmware
tells the NVIDIA driver about power limits. The four bytes stay at 70, 119, 10
and 10 in a 35-second watch that reads them once a second. In 30-second counts,
the EC raises query 0x48 four times in `balanced`, and zero times in
`performance` and in `power-saver`.

The NVIDIA driver receives the notification in `nv_acpi_nvpcf_event`, which
calls the driver core's `rm_acpi_nvpcf_notify`. Before passing the event on,
`rm_acpi_nvpcf_notify` resumes the GPU from runtime suspend. Runtime suspend is
the sleep state of an idle device while the rest of the system keeps running. So
the GPU never sleeps for more than about a second. Each wake keeps a worker
thread in the kernel busy for about 2 seconds.

## Patches

`build.sh` applies the patches in `patches/` in order. Both patches apply to
595.99.02, 610.57.04, 615.71.09 and 615.78.08.

`0001-nvidia-defer-NVPCF-notifications-until-runtime-resume.diff` is pull
request #1299 to NVIDIA/open-gpu-kernel-modules, by Lars Nieradzik (lars76). The
file holds the pull request unchanged, at its head commit 555c98d1. While the
GPU is runtime-suspended, the patched driver only records that a notification
arrived. After the GPU next resumes, the driver delivers one notification,
however many arrived in between. The patch also makes `rm_acpi_nvpcf_notify`
release its power reference only when taking the reference succeeded. It adds
the functions `RmDeferNvpcfNotifyIfIdle` and `RmReplayNvpcfNotify`. `swap.sh`
looks for both functions to recognize a patched driver, and `build.sh` looks for
`RmDeferNvpcfNotifyIfIdle`.

`0002-nvidia-defer-NVPCF-notifications-only-in-fine-grained-mode.diff` limits
the deferral to the fine-grained mode of the driver's runtime power management,
`NVreg_DynamicPowerManagement=2`, which the Legion uses. In this mode, the
driver powers the GPU down after the GPU has been idle for some time. In the
coarse-grained mode, `NVreg_DynamicPowerManagement=1`, the driver powers the GPU
down only while the GPU is uninitialized, that is, while every program has
closed it. In that mode, `os_ref_dynamic_power(nv, NV_DYNAMIC_PM_FINE)`, which
the notification path calls, returns before it takes a power reference. The
pull request's `RmDeferNvpcfNotifyIfIdle` also locks `dynamic_power.mutex`
without checking the mode first. `os_ref_dynamic_power` and
`os_unref_dynamic_power` check the mode before they lock the mutex. If the
driver fails to create the mutex, it leaves the mutex NULL and turns runtime
power management off. The two existing functions then return before locking,
but the new function would dereference NULL at the next notification. `0002`
gives the new function the same mode check, so the coarse-grained and disabled
modes keep the existing notification path.

## Measurements

All measurements are from the Legion 7 16IRX9 in the `balanced` profile. Each of
the following three entries is a one-minute trace of the EC queries, the
notifications and the GPU's power state.

- Unpatched 595.91.07: two of the eight notifications take 2.0 and 2.1 seconds.
  The GPU sleeps twice, for about 0.8 seconds each time, and the two sleeps
  start about 28 seconds apart.
- 595.99.02 with patch 0001: the EC raises query 0x48 every 6.97 seconds, and
  the nine notifications take 10 to 41 microseconds each. Once the GPU is
  asleep, it stays asleep through the remaining five queries of the trace.
- 615.71.09 with both patches: the EC raises query 0x48 every 6.98 seconds, and
  the nine notifications take 10 to 29 microseconds each. Once the GPU is
  asleep, it stays asleep through the remaining four queries of the trace.

A swap from the unpatched 595.99.02 to 595.99.02 with patch 0001 takes 6.7
seconds. A swap from that build to 615.71.09 with both patches takes 6.8
seconds. Two swaps that reload the same 615.71.09 build, started 17 seconds
apart, take 6.1 and 4.6 seconds. gnome-shell, Xwayland and the other open
programs keep running through all four swaps.

During the second of the two 615.71.09 swaps, rtkit-daemon refuses four of
mutter's requests, and mutter reads the state of the reloaded display device
with its KMS thread still real-time. The 20-second wait in `swap.sh` prevents
this case.

## Old firmware package during a version change

The driver loads its GSP firmware from disk whenever it initializes the GPU. GSP
is the GPU System Processor, a processor on the GPU that runs this firmware. The
driver initializes the GPU when a program opens it after the last program has
closed it. In the driver's source, `nv_open_device` calls `rm_init_adapter`
while the device is uninitialized. The old driver keeps running until the swap,
so it needs the old firmware package until then. Step 3 of [Changing the driver
version](README.md#changing-the-driver-version) in README.md keeps that package
installed.

## Scripts

### swap.sh

`swap.sh [--check]` runs as root. Any other argument makes it exit with status 2
before it looks at the system.

`swap.sh` finds the GPU's PCI address with `lib.sh`. It looks up the GPU's DRM
device files in sysfs. DRM is the kernel's graphics subsystem, and its device
files are in `/dev/dri`. The render node `/dev/dri/renderD*` is the file through
which programs draw with the GPU. The KMS node `/dev/dri/cardN` is the file
through which the display server configures the monitors on the GPU. KMS is
kernel mode setting, the kernel's interface for configuring displays. README.md
calls the KMS node the display device. A program that uses the GPU holds these
files open, together with `/dev/nvidia*` and `/dev/nvidia-caps/*`. The kernel
unloads a module only after every process has closed and unmapped these files.

`swap.sh` never revokes handles on the display device. mutter, the library in
gnome-shell that manages displays and windows, drives the monitors on the NVIDIA
GPU's ports through that device. systemd-logind and systemd (PID 1) hold copies
of mutter's descriptor.
mutter opens the device when it appears, and closes it after the next time it
sets the display modes (`meta_kms_impl_device_notify_modes_set` in mutter 46.2).
If gnome-shell holds the device, and its only other holders are systemd-logind
and PID 1, `swap.sh` runs `reapply-layout.py` as gnome-shell's user, on
gnome-shell's session bus. That script applies the current layout again, which
makes mutter set the display modes. `swap.sh` then waits up to 10 seconds for
mutter to close the device. `swap.sh` exits before it renames files, stops
services or revokes handles in each of these cases: another process holds the
device, systemd-logind or PID 1 holds it without gnome-shell, a monitor is
connected to the NVIDIA GPU, `reapply-layout.py` fails, or mutter still holds
the device after 10 seconds.

For the other device files, `swap.sh` sorts the holders into three groups:

- A root process in `lactd.service`, `nvidia-persistenced.service` or
  `nvidia-powerd.service` has its service stopped for the swap and started again
  afterwards. These services only watch or tune the GPU, and they start again
  cleanly.
- Any other root process with the GPU open makes `swap.sh` refuse to swap, also
  when the process belongs to another service, because stopping the process
  could end someone's work.
- A process not owned by root, such as gnome-shell or Xwayland, has its handles
  revoked by `revoke.py`. Each descriptor on `/dev/nvidia*`,
  `/dev/nvidia-caps/*` and the render node becomes `/dev/null`, and each mapping
  of those files becomes zeroed memory. gdb holds each program stopped for 0.1
  to 2 seconds.

After it stops the services, `swap.sh` checks all remaining holders before it
revokes any handles, so a root holder makes `swap.sh` exit before it changes any
process. It runs `revoke.py` in its own session, so that a signal to `swap.sh`'s
process group cannot stop gdb in the middle of a call that gdb injected into the
target process.

A swap runs these steps in order:

1. If the previous swap ended less than 20 seconds earlier, `swap.sh` waits for
   the rest of the 20 seconds.
2. If mutter holds the display device, `swap.sh` makes mutter release it, as
   described above.
3. It renames the registration files
   `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`,
   `/usr/share/vulkan/implicit_layer.d/nvidia_layers.json` and
   `/usr/share/vulkan/icd.d/nvidia_icd.json` by adding `.hotswap` to their
   names. The EGL and Vulkan loaders, which programs use to set up graphics on a
   GPU, find NVIDIA's libraries through these files, so they do not find them
   while the files are renamed. GLX finds its vendor library by name, and a
   program can load a library itself, so neither uses these files. Programs
   that open `/dev/nvidia*` directly, such as CUDA programs and `nvidia-smi`,
   can still open the GPU. If one holds it open when step 6 unloads the
   modules, `rmmod` fails, and `swap.sh` stops and restores what it can.
4. It stops the services.
5. It revokes the handles, and checks that every process has closed the device
   files.
6. It unloads `nvidia_drm`, `nvidia_modeset`, `nvidia_uvm` and `nvidia`, in that
   order.
7. It loads from disk each module that was loaded at the start, in the order
   `nvidia`, `nvidia_modeset`, `nvidia_drm`, `nvidia_uvm`.
8. It renames the registration files back and starts the services.
9. It checks the build IDs and the patch, as described below.

If an interrupted earlier run left one of the registration files renamed,
`swap.sh` refuses to swap.

`swap.sh` writes the end time of every swap it starts, including failed swaps,
to `/run/nvidia-live-swap.last`. It starts a new swap only 20 seconds after that
time, and `--check` reports the wait. The 20 seconds come from the rate limit
of rtkit-daemon, the service that grants real-time scheduling to threads. By
default it allows 25 requests per user in 20 seconds.

mutter runs its KMS thread, the thread that sends display updates to the
kernel, with real-time scheduling from rtkit-daemon. It also sets gnome-shell's
`RLIMIT_RTTIME` to rtkit-daemon's `RTTimeUSecMax`, 200 milliseconds by default.
While the thread is real-time, the kernel kills gnome-shell if the thread uses
the CPU for 200 milliseconds without blocking. mutter takes the thread out of
real-time scheduling while it reads a display device's state, sets its modes or
disables it, and makes it real-time again afterwards
(`meta_thread_inhibit_realtime_in_impl` in mutter 46.2). It asks rtkit-daemon
for each of these changes, and a swap makes it ask several times. When
rtkit-daemon refuses a request, the thread stays real-time during these calls.
Two swaps less than 20 seconds apart can use up the 25 requests.

On a failure or a signal (SIGINT, SIGTERM or SIGHUP), `swap.sh` tries to load
from disk every NVIDIA module that was loaded at the start and is now missing.
It tries to rename the files back and to start the stopped services, logs each
step that fails, and exits with a non-zero status. Revoked handles stay revoked.
`swap.sh` records each change before it makes the change. So when a signal
arrives after a change has taken effect, but before its command has returned,
`swap.sh` still tries to undo the change.

`swap.sh` logs `END` only when every module it loaded has the same build ID as
its file on disk, and the loaded driver contains the patch. Otherwise it logs
`END (failed)` and exits with a non-zero status. A build ID is a hash that the
linker stores in each module. `swap.sh` compares the build ID in
`/sys/module/<module>/notes` with the build ID in the file that `modprobe` would
load. To find the patch, it looks for `RmDeferNvpcfNotifyIfIdle` and
`RmReplayNvpcfNotify` in `/proc/kallsyms`, the kernel's list of symbols.

### revoke.py

`revoke.py [--dry-run] <pid> <path>...` runs as root. It replaces a process's
file descriptors on the given paths with `/dev/null`. For this, it calls `dup2`
inside the process to copy the process's own fd 0, which must be `/dev/null`,
onto each of those descriptors. It replaces the process's mappings of the paths
with anonymous memory of the same size and protection. The descriptor numbers
stay taken, so a later ioctl on one of them fails instead of reaching another
file. Code that still holds a pointer into a replaced mapping reads zeros
instead of faulting. `--dry-run` lists what `revoke.py` would replace.

The replacement runs inside gdb, which attaches to the process and injects
function calls into it. gdb stops every thread when it attaches, and
`revoke.py` reads the descriptors and mappings from `/proc` only after that. By
default, gdb resumes every thread for the duration of each injected call.
`revoke.py` turns gdb's scheduler locking on, and stops if gdb does not confirm
the setting. With scheduler locking on, the other threads stay stopped while the
injected calls run in one thread.

`revoke.py` checks each call's result before it makes the next call. If a call
fails, the calls before it stay in effect, and `revoke.py` exits with status 1.
The injected calls are `dup2`, `mmap` and `__errno_location`, which returns the
address of the thread's `errno`. They do not take user-space locks, so they
cannot wait for a lock that a stopped thread holds. `revoke.py` refuses PID 1,
processes owned by root, and processes whose fd 0 is not `/dev/null`.

### reapply-layout.py

`reapply-layout.py [--check]` runs as the desktop user. It reads the active
monitor configuration from mutter's D-Bus method `GetCurrentState` and rebuilds
it. It asks mutter to validate the rebuilt configuration. With `--check`, it
stops there. Otherwise it applies the configuration through mutter's
`ApplyMonitorsConfig` with the temporary method, which skips the confirmation
prompt and does not save the layout. Then it checks that the active layout
stayed the same. The rebuilt configuration keeps each monitor's mode, position,
scale, transform and underscanning, and the primary monitor. It also keeps the
layout mode, when mutter reports that it supports changing the layout mode.
`reapply-layout.py` needs Ubuntu's `/usr/bin/python3` and `python3-gi`.

`swap.sh` runs it as gnome-shell's user, on gnome-shell's session bus, when
mutter holds the display device. It can also be run by hand. With Ubuntu's
mutter 46.2-1ubuntu0.24.04.18 and the built-in screen on the Intel GPU, it makes
mutter release the display device on 595.99.02 and on 615.71.09. The resolution,
refresh rate and scaling stay the same. During a swap on 615.71.09, mutter
releases the device 0.45 seconds after `swap.sh` runs `reapply-layout.py`, and
the display configuration afterwards matches the configuration before.

### lib.sh

`lib.sh` finds the NVIDIA GPU's PCI address. It picks the first PCI device that
is a display controller and has NVIDIA's vendor ID, 0x10de. `swap.sh` and
`trace.sh` source it.

### build.sh

`build.sh [version]` runs as root. Without a version, it builds the newest
NVIDIA version that DKMS lists.

Ubuntu's `nvidia-dkms-<branch>-open` package builds the kernel modules from
source. Only the driver core comes precompiled, as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. The source of the driver
core is in NVIDIA's open-gpu-kernel-modules repository on GitHub, under the tag
of the same version. `build.sh` clones that tag. It compares the tag's
`kernel-open` directory with Ubuntu's source in `/usr/src/nvidia-<version>`, and
refuses to continue if they differ. The comparison skips `dkms.conf`,
`.gitignore` and the files that only Ubuntu's tree has, such as the patches with
Ubuntu's build fixes. If the sources differed, the driver core built from the
tag might not match the rest of the modules. For 615.71.09, Ubuntu's
`nvidia-dkms-615-open` adds two build fixes for 7.0 kernels. They change only
the code between the driver and the kernel, and leave the driver core as it is.

`build.sh` then applies the patches in `patches/`, builds the driver core, and
checks that the result contains `RmDeferNvpcfNotifyIfIdle`. It adds a dpkg
diversion for `nv-kernel.o_binary` unless the diversion already exists, and
installs the patched driver core in place of Ubuntu's precompiled copy. The
diversion makes dpkg put the package's own version of that file under
`nv-kernel.o_binary.ubuntu`, so package operations leave the patched file in
place.

Next, it rebuilds and installs the DKMS modules for every kernel that DKMS lists
for that driver version, and for the running kernel, which is the kernel that
`swap.sh` loads the modules into. It checks that each installed `nvidia.ko`
contains `RmDeferNvpcfNotifyIfIdle`. `build.sh` works out the list of kernels
before it changes anything. If `dkms status` fails, or the headers of the
running kernel are missing, it stops before it downloads NVIDIA's source or adds
the diversion. If the headers of another kernel are missing, it skips that
kernel and exits with status 1 at the end.

### trace.sh

`trace.sh [seconds]` runs as root, for 60 seconds by default. Run it in the
`balanced` profile. It prints the platform profile first. Then it records the EC
queries, how long the driver takes for each notification, and whether the GPU is
active or suspended.

The kernel's EC driver logs a query only as a debug message. Each such message
is turned on or off through the kernel's dynamic debug control file. `trace.sh`
turns on the EC driver's debug messages that are off, and only those. It times
the notifications with ftrace, the kernel's built-in tracer. ftrace's
`function_graph` tracer records how long each call to a traced function takes,
and `trace.sh` traces only `nv_acpi_nvpcf_event`, the driver function that
receives the notification. The trace runs in a private tracing instance, so the
global tracer settings stay as they are. It starts tracing only after the
function filter reads back as `nv_acpi_nvpcf_event` alone, because with an empty
filter `function_graph` would trace every kernel function. Before printing, it
stops the trace and turns the debug messages off again, so the output covers
exactly the measured period. On exit, including on a signal, it removes the
instance and turns off the debug messages it turned on.

## Tests

- `tests/check_swap.py` runs `swap.sh` as an ordinary user, against mock
  commands and a throwaway directory that stands in for sysfs, `/proc` and the
  registration files. Its cases cover these situations:
  - Another process holds the display device, or systemd and systemd-logind hold
    it without gnome-shell. A monitor is connected to the NVIDIA GPU. A root
    process outside a service holds the GPU. A root process in another service
    holds the GPU. A registration file is still renamed. An argument is mistyped
    or extra. In each of these cases, `swap.sh` must refuse and leave the system
    unchanged.
  - `--check` runs with and without mutter holding the display device.
  - `swap.sh` releases the display device through `reapply-layout.py` as
    gnome-shell's user. In two more cases, the release does not come, or
    `reapply-layout.py` fails.
  - A full swap runs. Another full swap runs with the GPU numbered
    card1/renderD128.
  - Renaming a file, revoking, unloading or loading fails. The patch is missing.
    A build ID does not match.
  - SIGTERM arrives after a service stop, during an `rmmod`, or after an
    `rmmod`.
  - After a recent swap, `swap.sh` waits, and `--check` reports the wait. A swap
    after an older swap starts at once. A failed swap still records its end
    time.
- `tests/check_build.py` runs `build.sh` as an ordinary user, against mock
  commands and a fake `/usr/src` and `/lib/modules`. Its cases are a failing
  `dkms status`, a version that DKMS lists only as `added`, missing headers for
  the running kernel and for another kernel, a mistyped or extra argument, and a
  full build for two kernels.
- `tests/check_layout.py` runs as an ordinary user, without a desktop. It checks
  how `reapply-layout.py` rebuilds current modes, mirrored monitors and
  underscanning, and that it sends the layout mode only when mutter supports
  changing it. It also checks the D-Bus argument types and the handling of
  invalid input. To validate the rebuilt configuration against the running
  mutter, run `reapply-layout.py --check` in the session.
- `tests/check_trace_filter.py` runs `trace.sh` as an ordinary user, with
  ordinary files in place of the tracing and debug control files, and with a
  filter write that fails. It checks that `trace.sh` stops before selecting
  `function_graph`, and that it removes the instance.
- `tests/check_revoke.py` runs as root. It runs `revoke.py` against a throwaway
  two-thread process owned by the user `nobody`. It checks that `revoke.py`
  refuses PID 1 and a root-owned process. It also checks that the revoke runs
  with scheduler locking on, and that afterwards the process keeps running and
  its second thread keeps reading through the replaced descriptor and mapping.
- `tests/check_trace.py` runs as root. It checks that `trace.sh` leaves the
  tracing and debug settings as it found them, after a normal run and after
  SIGTERM.
- `tests/check_egl.c` runs as the desktop user. It renders a shader into an
  off-screen pbuffer through NVIDIA's EGL device platform, with NVIDIA's EGL
  vendor library selected and `DISPLAY` and `WAYLAND_DISPLAY` unset. Then it
  checks the pixel it reads back. Step 8 of [Changing the driver
  version](README.md#changing-the-driver-version) in README.md has the build and
  run commands. On 615.71.09, it prints EGL 1.5 NVIDIA, the RTX 4070 Laptop GPU
  and OpenGL ES 3.2 NVIDIA 615.71.09, and it reads back 64 127 191 255. The
  expected pixel is 64 128 191 255, with one unit of tolerance per channel. The
  test checks only programs started after a swap, because programs started
  before a swap keep the old libraries.
