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
while the device is uninitialized. The old driver keeps running until the
reboot, so it needs the old firmware package until then. Step 2 of [Changing
the driver version](README.md#changing-the-driver-version) in README.md keeps
that package installed.

## The hang in NVIDIA's shutdown

When the last program closes the GPU, NVIDIA's `nv_stop_device` shuts down its
state for the GPU through `nv_shutdown_adapter` and `rm_shutdown_adapter`
(`kernel-open/nvidia/nv.c:2195,2215-2255` in 615.71.09). `rm_shutdown_adapter`
takes the RM API lock, the main lock of NVIDIA's driver core, and keeps it while
`RmShutdownAdapter` takes the GPU lock and calls `RmUnInitAcpiMethods`
(`src/nvidia/arch/nvalloc/unix/src/osapi.c:1928-1950` and
`osinit.c:2425-2506`). That function removes NVIDIA's ACPI notification
handlers through `nv_acpi_methods_uninit` (`kernel-open/nvidia/nv-acpi.c`,
lines 222-245 and 370-391). The kernel's `acpi_remove_notify_handler` then
waits until every notification handler that is already running has returned
(`drivers/acpi/acpica/evxface.c:304-320` and `drivers/acpi/osl.c:1164-1174` in
Ubuntu's 7.0.0-38 kernel).

Two of those handlers take the RM API lock. The NVPCF handler takes it in
`rm_acpi_nvpcf_notify` (`osapi.c:6179-6211`). The AC adapter handler takes it
in `rm_power_source_change_event` when the power source changes
(`osapi.c:4590-4640`). If one of them starts just before the shutdown takes the
lock, it waits for the lock, and the shutdown waits for it. The program whose
close started the shutdown then hangs in the kernel. In the Balanced profile
the NVPCF handler runs every 7 seconds. Patch 0001 does not change this,
because `rm_acpi_nvpcf_notify` takes the RM API lock before the patch's
`RmDeferNvpcfNotifyIfIdle` runs. This sequence is read from NVIDIA's 615.71.09
source and has not been reproduced.

nvpcf_hook intercepts `nv_shutdown_adapter` at its entry, before those locks,
and calls NVIDIA's own `nv_acpi_methods_uninit` there. That removes both
handlers and waits for running handlers before the shutdown takes the RM API
lock. NVIDIA's later cleanup finds the handlers already removed and skips
them.

## Fences that outlive nvidia_drm

nvidia_drm creates dma fences, the kernel objects through which drivers and
programs wait for GPU work. Each fence points to a table of functions in the
module. nvidia_drm's two tables, `nv_drm_gem_prime_fence_ops` and
`nv_drm_semsurf_fence_ops` (`nvidia-drm/nvidia-drm-fence.c:141-147,1373-1382` in
615.71.09), both implement `release` and `wait`. The 7.0.0-38 kernel's
`include/linux/dma-fence.h` (lines 222-224 and 237-242) says that such a fence
cannot detach from its module after it is signaled, so the module has to stay
loaded as long as the fence exists. The 6.8.0-147 and 7.0.0-34 headers do not
have this rule; there, a fence can use its module's functions until it is
freed.

nvidia_drm does not take module references for its fences. `nvidia-drm/` calls
neither `try_module_get` nor `module_put`, and its only `THIS_MODULE` is the
owner of its DRM device's file operations (`nvidia-drm-drv.c:1815`), which keeps
the module loaded only while a DRM device file is open. Unloading it does not
wait for fences that others hold. Module exit unregisters and destroys the
devices (`nvidia-drm-linux.c:69-73`, `nvidia-drm-drv.c:2290-2296`), and
destroying a prime fence context signals its pending fences
(`nvidia-drm-fence.c:157` and 212-228), but the references that other holders
have stay.

The fences reach other holders in several ways. nvidia_drm adds them to the
reservation objects of buffers, including buffers that another driver exported
(`nvidia-drm-fence.c:506-508`). It turns them into sync files (lines 1532 and
1977) and adds them to DRM sync objects (line 1913). Other drivers and programs
can copy them from there. When one of them releases a fence after nvidia_drm
was unloaded, the kernel reads the fence's functions from memory that was freed
with the module.

The kernel log of this laptop shows such a crash. At 2026-11-02 08:17:43, a day
after the swaps at 13:53, 15:28 and 15:59 on 2026-11-01, gnome-shell closed a
sync file during a logout. `dma_fence_release` read the fence's `release`
function from an unmapped page in the module area (`BUG: unable to handle page
fault for address: ffffffffc2124008`, with the call trace `__x64_sys_close`,
`sync_file_release`, `dma_fence_release`), and gnome-shell died in the kernel.
Which module the fence came from is not known. nvidia_drm had been unloaded
four times since boot, and it is the only NVIDIA module that creates fences,
but the old modules' addresses were not recorded. A module's layout within its
pages is the same at every load of the same file. The function table's page
offset, 0xfe0, matches neither fence table of the 615.71.09 nvidia_drm, which
`/proc/kallsyms` shows at page offsets 0x80 (`nv_drm_semsurf_fence_ops`) and
0x100 (`nv_drm_gem_prime_fence_ops`), so the fence did not come from the two
615.71.09 instances. If it came from nvidia_drm, it came from one of the two
595.99.02 builds, unloaded at 13:53:49 and 15:28:55, which were removed from
disk with their packages.

Both release functions also free the fence at once, with `nv_drm_free`, which
is a plain `kfree` (`nvidia-drm-os-interface.c:63-70`), while
`dma-fence.h:241-242` requires RCU-safe freeing when `release` is implemented.
The crash evidence does not establish this second problem as its cause.

Checking the programs' open files cannot establish that every fence was
released. Kernel objects, such as other drivers' buffers, keep references that
the processes' files do not show, and inspecting a fence that may be stale can
run its functions or drop its last reference. So `swap.sh` refuses every swap
that would unload nvidia_drm. After a swap, stale fences can remain until the
next reboot.

Making such swaps safe needs a change in nvidia_drm or in the kernel. It has to
keep the module's code available until the last call into it from a fence has
returned, and free fence memory as the kernel's RCU rules require. It also has
to cover fences that exist before the unload begins or are created while it
runs, and signaled fences, which keep their references. It has to work on the
6.8 and 7.0.0-34 kernels as well, whose headers lack 7.0.0-38's rules for
detaching fences. Such a change has not been written or tested.

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
of mutter's descriptor. mutter opens the device when it appears, and closes it
after the next time it sets the display modes
(`meta_kms_impl_device_notify_modes_set` in mutter 46.2).
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

`swap.sh` refuses every swap while nvidia_drm is loaded, for the reasons in
[Fences that outlive nvidia_drm](#fences-that-outlive-nvidia_drm). It checks
`/proc/modules` before it changes anything, also with `--check`, and it
refuses in the same way when `/proc/modules` cannot be read. A refused run
checks the driver on disk with `--no-download`, so it does not download
anything either. If nvidia_drm is loaded after those checks, the unload step
stops when it reaches nvidia_drm, before any module is unloaded.

Every swap needs nvpcf_hook to prevent the shutdown deadlock described in [The
hang in NVIDIA's shutdown](#the-hang-in-nvidias-shutdown). The hook supports
only one board configuration, which [nvpcf_hook](#nvpcf_hook) names, so
`swap.sh` works only on that configuration.
`swap.sh` refuses unless the hook is loaded, installed for the running kernel,
and in the state `active`, `waiting`, `guard-only` or `quiesced`. It reads the
hook's state with `nvpcf-hookctl status`, which does not change the hook.
[hook/README.md](hook/README.md) describes the states. A hook whose status
shows `retirement_failed: 1` could not finish a cleanup: removing its
interception of NVIDIA's functions, releasing its references to the GPU, or
undoing a failed setup. `swap.sh` then refuses in every state, and tells the
user to run `nvpcf-hookctl detach` to retry the cleanup. For the check after
the swap, `swap.sh` also decompresses the nvidia module that `modprobe` would
load and looks for `RmDeferNvpcfNotifyIfIdle` with `nm`.

`swap.sh` runs the installed `load-hook.sh`, which runs the installed checker,
and the loaded hook comes from the installed DKMS module. So `swap.sh` refuses
while a copy of `load-hook.sh`, `nvpcf-hookctl`, `check_nvidia.py`,
`expected_dispatcher.c` or `expected_procfs_wrappers.h` in
`/usr/local/lib/nvpcf-hook` is missing, differs from the repository's file, or
lacks the executable bit that the repository's file has. It names
`install-hook.sh`, which installs this version. `lib.sh` lists these files
for both scripts.

Before the swap changes anything, `swap.sh` runs `load-hook.sh --check-disk`,
which checks the nvidia module that `modprobe` would load in the same way as the
loader checks a loaded one, and `swap.sh` refuses if the check fails. So a
driver that the hook cannot use is found before the working one is unloaded.
With `--check`, `swap.sh` adds `--no-download`. If NVIDIA's source archive for
that version is not in the cache yet, `--check` then reports that the check
could not run, and a swap downloads the archive before its first change.

A swap runs these steps in order. With nvidia_drm loaded, `swap.sh` refuses
before step 1. In a swap that goes ahead, nvidia_drm is not loaded, so the GPU
does not have a display device, and the swap skips step 3 and the reload of
`nvidia_drm` in step 12. The tests run every step with nvidia_drm loaded, on a
copy of `swap.sh` without the refusal.

1. If the previous swap ended less than 20 seconds earlier, `swap.sh` waits for
   the rest of the 20 seconds.
2. It runs the hook's `prepare`, which wakes the GPU and delivers any held
   message. If `prepare` fails, `swap.sh` stops before it changes anything.
3. If mutter holds the display device, `swap.sh` makes mutter release it, as
   described above.
4. It renames the registration files
   `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`,
   `/usr/share/vulkan/implicit_layer.d/nvidia_layers.json` and
   `/usr/share/vulkan/icd.d/nvidia_icd.json` by adding `.hotswap` to their
   names. The EGL and Vulkan loaders, which programs use to set up graphics on a
   GPU, find NVIDIA's libraries through these files, so they do not find them
   while the files are renamed. GLX finds its vendor library by name, and a
   program can load a library itself, so neither uses these files. Programs
   that open `/dev/nvidia*` directly, such as CUDA programs and `nvidia-smi`,
   can still open the GPU. If one holds it open when step 8 unloads the
   modules, `rmmod` fails, and `swap.sh` stops and restores what it can.
5. It stops the services.
6. It revokes the handles, and checks that every process has closed the device
   files.
7. It runs the hook's `detach`. The hook delivers any held message, releases
   its reference to the GPU if it holds one, and keeps only its shutdown guard,
   in the state `guard-only`. When the hook held a reference, that release is
   the last close of the GPU. Otherwise the last close came earlier, during the
   revocations. The guard is in place in both cases, because the hook
   registers it when it loads, also when it then waits.
8. It unloads `nvidia_drm`, `nvidia_modeset`, `nvidia_uvm` and `nvidia`, in that
   order. If `nvidia_drm` was loaded after the checks, `swap.sh` refuses to
   unload it and stops before the first unload.
9. It unloads `nvpcf_hook`. The hook keeps itself loaded until NVIDIA's
   unload has finished, and then moves to `detached`.
10. It loads `nvidia` from disk.
11. It runs the loader, `load-hook.sh`, which checks the loaded `nvidia` and
    loads the hook for it. The loader exits with status 0 only if the hook is
    then `active` or `waiting`, with its guards registered, GPU
    initialization allowed and `retirement_failed` 0. Otherwise `swap.sh`
    stops here.
12. It loads from disk the other modules that were loaded at the start, in the
    order `nvidia_modeset`, `nvidia_drm`, `nvidia_uvm`. Loading `nvidia_drm`
    initializes the GPU. It sets up an NVKMS device and frees it again if its
    setup fails, which ends in NVIDIA's shutdown, so the guard has to be in
    place by then.
13. It renames the registration files back and starts the services.
14. It checks the build IDs and the fix, as described below.

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

The hook's guard has to be in place before anything makes NVIDIA initialize the
GPU: loading `nvidia_modeset` or `nvidia_drm`, or starting a service such as
`lactd`. After its `detach`, the hook also refuses that initialization until
its `resume` has checked that the GPU and NVIDIA's driver are eligible. So on
a failure, `swap.sh` first loads `nvidia` again if it is missing. Then it runs
the loader if the hook is not loaded, or the hook's `resume` if the swap had
detached it. If `load-hook.sh --check-loaded` then finds the hook `active` or
`waiting`, with its guards registered, initialization allowed and
`retirement_failed` 0, it loads the other missing modules and starts the
stopped services. Otherwise it leaves those modules unloaded and those
services stopped, and names them. In both cases it renames the registration
files back. It never runs `acknowledge-loss`.

`swap.sh` logs `END` only when every module it loaded, including `nvpcf_hook`,
has the same build ID as its file on disk, and the fix is in place. Otherwise
it logs `END (failed)` and exits with a non-zero status. A build ID is a hash
that the linker stores in each module. `swap.sh` compares the build ID in
`/sys/module/<module>/notes` with the build ID in the file that `modprobe` would
load. It reads both through `load-hook.sh --loaded-build-id` and `--build-id`,
which accept IDs of 1 to 64 bytes, as the hook does, and fail when they
cannot read one. `swap.sh` then reports the module whose ID it could not
read. If the nvidia module on disk has patch 0001, `swap.sh` looks for
`RmDeferNvpcfNotifyIfIdle` and `RmReplayNvpcfNotify` in `/proc/kallsyms`, the
kernel's list of symbols. `swap.sh` waits up to 15 seconds for the hook's state
to become `active`. The hook attaches once something initializes the GPU,
usually gnome-shell opening the new display device or a restarted service.

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
is a display controller and has NVIDIA's vendor ID, 0x10de. It also lists, in
`HOOK_TOOLS`, the files that `install-hook.sh` installs in
`/usr/local/lib/nvpcf-hook`. `swap.sh`, `trace.sh` and `install-hook.sh` source
it.

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
for that driver version, and for the running kernel. It checks that each
installed `nvidia.ko` contains `RmDeferNvpcfNotifyIfIdle`. `build.sh` works out
the list of kernels before it changes anything. If `dkms status` fails, or the
headers of the running kernel are missing, it stops before it downloads
NVIDIA's source or adds the diversion. If the headers of another kernel are
missing, it skips that kernel and exits with status 1 at the end.

A kernel that DKMS lists, but that has neither headers nor modules of its own in
`/lib/modules/<kernel>/kernel`, was removed. DKMS builds the modules for a
kernel when the kernel's headers are installed
(`/etc/kernel/header_postinst.d/dkms`), and removes them only when the kernel
image is removed (`/etc/kernel/prerm.d/dkms`). When the headers go after the
image, DKMS keeps listing the modules it had installed for that kernel. On the
Legion, apt replaced the 6.8.0-147 headers with the 6.8.0-149 headers on
2026-11-09, when the only 6.8 packages installed were headers, and DKMS still
lists nvidia and nvpcf-hook for 6.8.0-147. `build.sh` leaves such a kernel out
and names it, without counting it as skipped. `sudo dkms remove -m nvidia -v
<version> -k <kernel>` removes the leftover. The running kernel never counts as
removed.

After it installs the modules for a kernel, `build.sh` updates that kernel's
initramfs, the image that the boot loader loads with the kernel and that the
kernel runs before it mounts the root filesystem. Ubuntu's
`nvidia-kernel-common-<branch>` package installs
`/usr/share/initramfs-tools/hooks/framebuffer-nvidia`, which copies each
uncompressed `nvidia*.ko` file in `/lib/modules/<kernel>/updates/dkms` into the
initramfs when the initramfs gets a framebuffer. The `cryptsetup` and `plymouth`
packages ask for one with `FRAMEBUFFER=y` in
`/usr/share/initramfs-tools/conf-hooks.d`. With the modules in the initramfs,
the kernel loads nvidia from there: in the boot of 2026-11-10 at 03:55, nvidia
loaded 2.5 seconds after the kernel started, and the encrypted root filesystem
was mounted at 33.7 seconds. The driver that runs is then the initramfs's copy,
not the file on the root filesystem. `dkms install` does not rebuild the
initramfs; DKMS 3.0.11 only warns that `REMAKE_INITRD` is deprecated
(`/usr/sbin/dkms`, line 440). So without the update, the next boot loads the
modules from before the rebuild.

DKMS compresses a kernel's modules with zstd when that kernel's `modules.dep`
lists compressed modules at build time (`/usr/sbin/dkms`, lines 218-227), and
the hook skips compressed files. On the Legion, the 7.0.0-42 initramfs holds
the five nvidia modules, which DKMS built uncompressed, and the 7.0.0-38
initramfs holds none, because DKMS compressed them. `build.sh` updates every
image either way, so that each matches the installed files.

`build.sh` runs `update-initramfs -u -k <kernel>` for each kernel whose
`/boot/initrd.img-<kernel>` exists, right after that kernel's `dkms install`.
`update-initramfs` writes the new image under another name and renames it over
the old one, and keeps the old image when it fails. It exits with status 0
without writing when `/boot` is mounted read-only or `update_initramfs=no` is
set. So `build.sh` compares the image's inode and modification time before
and after, and stops with status 1 when `update-initramfs` fails or does not
write a new image, naming the kernel whose next boot would load the previous
modules. A kernel update does not need this step: the kernel package runs
`/etc/kernel/postinst.d/dkms` before `/etc/kernel/postinst.d/initramfs-tools`,
so the new kernel's initramfs gets the modules that DKMS has just built.

### nvpcf_hook

`hook/` holds nvpcf_hook, a kernel module that holds NVPCF notifications for an
NVIDIA driver without patch 0001. [hook/README.md](hook/README.md) describes it
in detail. In short:

- It intercepts `nv_acpi_nvpcf_event` with ftrace. While the GPU is
  runtime-suspended, it records a notification instead of passing it on. After
  the GPU's next wake-up, a worker passes one notification on for all that
  arrived in between. The worker holds a runtime power reference only if the
  GPU is already awake, so it does not wake the GPU. The hook's `prepare` and
  `detach` do wake it, to deliver what is still held.
- It confirms each delivery with a kretprobe, which sees
  `gpuNotifySubDeviceEvent_IMPL`, a function in NVIDIA's precompiled core,
  return. A notification stays pending until the hook sees that return. If the
  probe misses a return, a retry can therefore deliver the notification twice.
- While it is attached, it holds a reference to the GPU through NVIDIA's
  in-kernel `nvidia_dev_get_uuid` and `nvidia_dev_put_uuid`. The reference
  keeps the data that NVIDIA's notification handler uses allocated, so that a
  held notification can still be delivered, but it does not keep the GPU
  powered.
- It intercepts `nv_shutdown_adapter`, as described in [The hang in NVIDIA's
  shutdown](#the-hang-in-nvidias-shutdown).
- It attaches only to the configuration it was checked for: the Legion 7
  16IRX9, DMI vendor `LENOVO` and product `83FD`, whose one NVIDIA GPU,
  `10de:2860`, sits directly on the CPU's PCIe root port, Intel `8086:a70d`, as
  [hook/README.md](hook/README.md#supported-pci-configuration) specifies. The
  GPU has to be in fine-grained power mode, NVIDIA has to use its kernel suspend
  notifiers, and the firmware must not have NVIDIA's legacy `NVIF` and `WMMX`
  methods. The hook uses functions of NVIDIA's open layer and one symbol of
  NVIDIA's core. A new NVIDIA version that changes them can need a change to the
  hook.

Its control client, `hook/nvpcf-hookctl`, has the commands `status`, `prepare`,
`detach`, `resume` and `acknowledge-loss`. `swap.sh` uses the first four.
`acknowledge-loss` is for a person, after the hook has reported that it could
not deliver the notifications it held, for example because the GPU
disappeared.

### load-hook.sh and nvpcf-hook.service

`load-hook.sh` runs as root, and is the only way that loads the hook. The hook
calls functions inside NVIDIA's driver, so it may attach only to a driver build
whose code `hook/tests/check_nvidia.py` has checked. The loader runs that
checker on three inputs, and does not run or extract any code from them:

- the nvidia module file with the build's GNU build ID, for its symbols, its
  ftrace instrumentation and its calls;
- the version's kernel-open source in `/usr/src/nvidia-<version>`, for the C
  declarations and roles of the functions that the hook uses, and for the
  functions behind NVIDIA's GPU files in `/proc` that the hook reads;
- NVIDIA's source archive for the version's tag on GitHub, for the source of
  the precompiled driver core, where the dispatcher that the hook observes
  lives.

The archives are kept in `/var/cache/nvpcf-hook`, downloaded over https when one
is missing, and checked again at every load.

By default, the loader loads `nvidia` if needed, and reads the loaded build ID
from `/sys/module/nvidia/notes`. It checks the module file with that build ID,
which is the file `modprobe` would load, or DKMS's copy for the loaded version
and the running kernel. It stops when it cannot find a file with the loaded
build ID, because a check of a different file does not cover the loaded driver.
One cause is an initramfs that holds an older copy of the driver than the
installed files, as the [build.sh](#buildsh) section explains, so the message
names `update-initramfs -u -k <kernel>` and a reboot. The version comes from the
matched file itself, and has to equal the loaded module's. After the checks
pass, it loads `nvpcf_hook` with `expected_nvidia_build_id` set to the checked
build ID, and the hook refuses to attach to any other build. The loader exits
with status 0 only if the hook then reports `active` or `waiting`, with its
guards registered, GPU initialization allowed and `retirement_failed` 0.
`swap.sh` relies on that status: it loads the modules and starts the services
that make NVIDIA initialize the GPU only after the loader succeeds.

If the hook is loaded already, the loader only checks that, as
`--check-loaded` does. `swap.sh` uses `--check-loaded` before it lets NVIDIA
initialize the GPU after a failure.

`--check-disk` checks the file that `modprobe` would load without loading
anything. It reads the version and build ID from the decompressed copy that it
checks. Replacing a package during the check therefore cannot pair one module
with another version's source. The build ID must be 1 to 64 bytes, which is
the range the hook accepts.

`--no-download` makes a missing archive end the run with status 3, which means
the check could not run. `--fetch` only downloads the archive of the version
given after it.

`--build-id FILE` prints the GNU build ID of a module file. It decompresses
the file, and copies the bytes of its `.note.gnu.build-id` section into a
temporary file with `objcopy -O binary -j .note.gnu.build-id`.
`--loaded-build-id MODULE` prints the build ID of a loaded module, from the
note in `/sys/module/<module>/notes`. Both parse the notes with the rules of
`nh_note_build_id` in `hook/build_id.h`, the hook's own parser:

- every note's header, name with its padding, and ID have to be complete;
- a note followed by another note is padded to four bytes;
- the last note may lack some or all of the padding after its ID.

They print the ID in lowercase hex only if the notes hold exactly one GNU
build ID note, of 1 to 64 bytes, and exit with status 1 otherwise.

GNU ld 2.42 writes the note of an ID whose length is not a multiple of four
with 16 bytes before the ID and without padding after it, and Linux exports
the section with that size. `readelf -n` then leaves the ID out, which is why
the loader does not use it. For a file without the section, `objcopy` exits
with status 0 and writes an empty file, which the parser rejects. The messages
of both modes go to stderr, so stdout holds only the ID. `install-hook.sh` and
`swap.sh` read build IDs through these two modes, so that the three scripts
accept the same IDs.

`nvpcf-hook.service` runs the installed loader once at boot, after the network
is up, because a new driver version can need its archive. A unit runs it rather
than `/etc/modules-load.d`, and `swap.sh` calls the loader directly rather than
`modprobe`, because a plain `modprobe nvpcf_hook` lacks the build ID and fails.

### install-hook.sh

`install-hook.sh` runs as root. DKMS builds each version of a module from its
own source directory, `/usr/src/<package>-<version>`, and for a new kernel it
builds the greatest registered version, in the order of the `VER` function in
`/usr/sbin/dkms`. That function pads every number to three digits and compares
the results as strings. So 0.1 and 0.001 get the same rank, and when two
registered versions have the same rank, DKMS builds the one it lists first.
The version is `PACKAGE_VERSION` in `hook/dkms.conf`: dotted numbers from 0
to 999 without leading zeros. `VER` keeps the numeric order of such versions,
in the C locale and in `en_US.UTF-8`, and gives two of them the same rank only
if they are the same version. A change to the hook's source needs a greater
`PACKAGE_VERSION`. The script refuses when its version is registered with
different source, and when another registered version ranks equal to it or
above it, because DKMS could then build that version for the next kernel.

The source is the files of `hook/` that DKMS's build reads: `Kbuild`,
`Makefile`, `dkms.conf`, `nvpcf_hook.c` and its headers. The script copies
them to `/usr/src/nvpcf-hook-<version>`. A build in `hook/` generates files
such as `nvpcf_hook.mod.c`, which are not part of the source. A registered
version has the same source only if its directory holds exactly these files,
with the same content.

If the version is not registered, the script reuses an existing source
directory only when it contains exactly the expected source files. This allows
a retry after `dkms add` failed. Each entry must be a regular file with a link
count of one, and its content must match `hook/`. A symbolic link is rejected
because DKMS copies the source with `cp -a`, which keeps the link, so the build
would read the file that the link points to. A file with another hard link is
rejected because the other name could later be used to change it.

The script reads directory entries with NUL separators and compares complete
names. Spaces and newlines within a name therefore cannot make one entry look
like two expected files. If a file differs, or a file or the directory listing
cannot be read, the script reports each problem and leaves the directory
unchanged, because it can contain someone's work.

When the directory does not exist, the script creates it with `mkdir` without
`-p`, which fails if anything appeared there in the meantime. A failed copy
removes only the files that the run copied and the directory it created. A
failed `dkms add` stops the run before any module is built or installed, and
leaves the copied source for the next run. When the script removes an older
version, it deletes that version's source files and then removes the
directory only if it is empty.

The script builds and installs the version with DKMS for every kernel that
DKMS lists for the NVIDIA driver or for nvpcf-hook, plus the running kernel.
The kernels listed for nvpcf-hook include those whose NVIDIA build DKMS does
not list at the moment, for example between a driver update and the
`dkms install` for the other kernels. After each install, and for a kernel
that DKMS lists as having the version installed already, it checks that the
module `modprobe` finds for the kernel has the build ID of DKMS's build. The
check reads only the installed file and DKMS's build, so a kernel that passes
it counts as done even without headers. A kernel that fails the check gets
the version installed again. A failed build leaves the kernel's installed
module in place, and so do missing headers. A failed install can leave the
kernel without a module: `dkms install` copies the new file over the old one
before it runs depmod, and removes the new file when depmod fails
(`/usr/sbin/dkms`, lines 1345-1380 in DKMS 3.0.11). After a failed install or
check, the script installs the kernel's previous version again from DKMS's
build of it, checks it in the same way, and reports whether that worked.

A kernel that DKMS lists, but that has neither headers nor modules of its own in
`/lib/modules/<kernel>/kernel`, was removed, as the [build.sh](#buildsh) section
explains, and the running kernel never counts as removed. The script leaves such
a kernel out and names it. It removes nvpcf-hook's install for the kernel with
`dkms remove -m nvpcf-hook -v <version> -k <kernel>`, as
`/etc/kernel/prerm.d/dkms` does when a kernel image is removed, and leaves the
kernel's nvidia entries as they are. Counted as skipped, a removed kernel would
keep every older version registered and end every run with status 1, and a
version still installed for it would count as installed. When the removed kernel
had the last build of a version, DKMS deletes that version from its tree, and
the script removes its source directory: a rollback installs an older version
from DKMS's build of it, and the last build of that version is gone. A failed
removal ends the run with status 1 and keeps the older versions. A kernel that
has its own modules but no headers is skipped. The initramfs hook copies only
modules whose names start with `nvidia`, so the initramfs does not hold
nvpcf_hook, and the script does not update it.

An older version that is not installed for any kernel is removed only after
every kernel in the run has the new version installed and checked, including
kernels whose headers are missing. If a kernel fails or is skipped, the script
keeps the older registrations, source and builds, so that `dkms install` can
still put an older build back on a kernel that a failed install left without
a module.

It installs the files that `lib.sh` lists in `HOOK_TOOLS` as root-owned
copies in `/usr/local/lib/nvpcf-hook`: `load-hook.sh`, `nvpcf-hookctl`,
`check_nvidia.py` and two references that `check_nvidia.py` reads from its own
directory. The checker compares the dispatcher function in NVIDIA's source
archive with `expected_dispatcher.c`, and NVIDIA's wrappers for its GPU files
in `/proc` in the kernel-open source with `expected_procfs_wrappers.h`. The
script installs `nvpcf-hook.service` and enables it. With `--no-enable`, it
does not enable the unit, and leaves a unit that is enabled already as it is,
so a first install can be tested before the hook loads at every boot. The
script downloads the source archive for each NVIDIA version that DKMS lists.
It does not load the hook.

It reads the DKMS state and works out the kernels before it changes anything.
It stops there if a `dkms status` fails, or if the running kernel's headers
are missing and that kernel does not have the version installed and passing
the check. It exits with status 1 at the end if it skipped a kernel, if a build,
install or check failed, or if an archive could not be downloaded.

### trace.sh

`trace.sh [seconds]` runs as root, for 60 seconds by default. Run it in the
`balanced` profile. It prints the platform profile first. Then it records the EC
queries, each call of the driver's notification handler with its duration, and
whether the GPU is active or suspended.

The kernel's EC driver logs a query only as a debug message. Each such message
is turned on or off through the kernel's dynamic debug control file. `trace.sh`
turns on the EC driver's debug messages that are off, and only those. It times
the handler's calls with ftrace, the kernel's built-in tracer. ftrace's
`function_graph` tracer records how long each call to a traced function takes,
and `trace.sh` traces only `nv_acpi_nvpcf_event`, the driver function that
receives the notification. The trace runs in a private tracing instance, so the
global tracer settings stay as they are. It starts tracing only after the
function filter reads back as `nv_acpi_nvpcf_event` alone, because with an empty
filter `function_graph` would trace every kernel function. Before printing, it
stops the trace and turns the debug messages off again, so the output covers
exactly the measured period. On exit, including on a signal, it removes the
instance and turns off the debug messages it turned on.

Before and after the trace, `trace.sh` prints nvpcf_hook's state and request
counts, or why they are unknown. With the hook loaded, `nv_acpi_nvpcf_event`
runs for each firmware notification, as the hook's short replacement, and
again for each delivery the hook makes, as NVIDIA's handler. So `trace.sh`
labels its records as calls of `nv_acpi_nvpcf_event`. Between the two
printouts, the growth of `received_requests` is the number of firmware
notifications that the hook caught, and the growth of `dispatch_attempts` is
the number of deliveries.

## Tests

- `tests/check_swap.py` runs `swap.sh` as an ordinary user, against mock
  commands and a throwaway directory that stands in for sysfs, `/proc` and the
  registration files. The throwaway `/proc/modules` lists nvidia_drm unless a
  case says otherwise. Every case except the refusal cases runs a copy of
  `swap.sh` in which `nvidia_drm_absent` always succeeds and the unload step
  does not stop at nvidia_drm, so that these cases keep testing the checks, the
  order and the recovery of a swap. The test stops if either piece of text that
  the copy replaces is missing from `swap.sh`. The refusal cases run
  `swap.sh` as it is:
  - With nvidia_drm loaded, `swap.sh` and `swap.sh --check` refuse, name the
    reason, leave the system unchanged, check the driver on disk with
    `--no-download` and do not record a swap. An unreadable `/proc/modules` is
    refused in the same way.
  - Without nvidia_drm, a swap goes ahead, unloads and loads every other
    NVIDIA module and the hook, and leaves nvidia_drm unloaded.
  - When nvidia_drm appears during a swap, `swap.sh` stops before it unloads
    any module, resumes the hook, moves the registration files back, starts
    lactd again and logs `END (failed)`.

  The other cases cover these situations:
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
  - With nvpcf_hook, a mock `nvpcf-hookctl` keeps the hook's state, and a mock
    `load-hook.sh` loads the hook. For `--build-id` and `--loaded-build-id`,
    the mock runs a copy of the real `load-hook.sh`, which parses the GNU notes
    of the loaded modules. Every case starts with the hook loaded and active.
    The mock records each module load or service start that could make NVIDIA
    initialize the GPU while the hook's guard is missing. Every case fails if
    it finds one, or if `swap.sh` runs `modprobe nvpcf_hook` itself.
  - A full swap prepares, detaches, unloads and loads the hook in the order
    described above, and ends with the hook active.
  - `swap.sh` refuses, without changing anything, when the hook is not loaded,
    even if the driver on disk is patched, when the hook is not installed,
    when its status cannot be read, and when it is unavailable, detached or
    starting. It also refuses when the loader is missing, when the driver on
    disk fails the loader's check, and while an installed tool differs from
    the repository's or is not executable. When the hook reports
    `retirement_failed: 1`, the refusal names `nvpcf-hookctl detach`, which
    retries the cleanup, also while the hook reports `active`.
  - With `--check`, an archive that is not cached yet is reported as a check
    still to run, and a swap lets the loader download it first.
  - A failed `prepare` stops the swap before any change. After a failed
    `detach`, a failed NVIDIA unload, or a SIGTERM after the detach or after
    `nvidia`'s unload, `swap.sh` brings the hook back before it loads another
    module or starts a service.
  - When the hook cannot guard NVIDIA's shutdown, `swap.sh` leaves the other
    modules unloaded and the services stopped, says so, and never runs
    `acknowledge-loss`. The cases are a hook that NVIDIA's unload left
    unavailable, a hook that could not load again, one that lacks its guards
    after loading, and one that reports `active` with `retirement_failed` set
    after a failed `detach`.
  - A hook that does not become active, or a service that does not start,
    ends the swap with `END (failed)`. Full swaps compare build IDs of 1, 16,
    17, 32 and 64 bytes, whose notes end without padding as GNU ld writes
    them. A loaded module without a build ID note ends the swap with
    `END (failed)` and names the module.
- `tests/check_load_hook.py` runs `load-hook.sh` as an ordinary user, with a
  throwaway directory in place of `/sys/module`, `/proc/modules`, the cache,
  `/var/lib/dkms`, `/lib/modules` and `/usr/src`. Unless a case says otherwise,
  `modprobe`, `modinfo`, `zstd`, `objcopy`, `curl`, `uname`, the checker and the
  control client are mocks, and the loaded build ID is a real GNU note that the
  script parses.
  - The cases verify that the loader checks the file with the loaded build ID,
    or DKMS's copy when only that matches, and that it stops without a check
    when neither matches, with a message that names the initramfs update. They
    verify that the version comes from the checked file, that a file whose
    version differs from the loaded module's is refused, and that `--check-disk`
    refuses a candidate whose build ID is missing, malformed or longer than 64
    bytes. They verify that the loader passes the build ID to `nvpcf_hook` in
    lowercase.
  - The loader downloads a missing archive over https into the cache without
    leaving a partial file. A failed download, a driver that the checker
    rejects, a missing source tree and a version string that is not a version
    each end the run with status 1 before the hook loads, and a failed load of
    the hook ends it with status 1. Other cases cover `--check-disk`,
    `--no-download`, `--fetch` and an already loaded hook.
  - The loader's status is 0 only for `active` or `waiting`, with guards
    registered, initialization allowed and `retirement_failed` 0, at the end of
    a load and with `--check-loaded`.
  - An unknown option, an empty argument, an extra argument, an option with an
    empty value, and two options in one argument make the loader exit with
    status 2 before it runs any command.
  - For `--build-id` and `--loaded-build-id`, the test reads stdout apart from
    the messages. With the mock `objcopy`, it checks IDs of 16, 20, 32 and 64
    bytes, IDs of 0 and 65 bytes, a file without an ID, a missing file, a
    module without a note, and a module name with a path.
  - With the real `objcopy`, it reads objects that `ld -r --build-id=0x...`
    builds for every ID length from 1 to 65 bytes, whose notes ld writes
    without padding after the ID. It also reads objects without the section,
    with two build ID notes in it, and with a truncated note at the end, and it
    checks that the objects stay unchanged. The same note bytes, with and
    without final padding, test `--loaded-build-id`.
  - A differential test compiles `nh_note_build_id` from `hook/build_id.h`, the
    hook's own parser, into a small program, and checks that the loader accepts
    the same notes and prints the same IDs. It compares them through
    `--loaded-build-id` for every ID length from 1 to 64 with its final padding
    absent, partly present or complete, and with the loader's parser code for
    4000 seeded random note sequences, some cut short or with a byte changed.
- `tests/check_install_hook.py` runs `install-hook.sh` as an ordinary user,
  against a mock `dkms` that follows DKMS 3.0.11. It keeps the added, built
  and installed versions. Its `build --force` drops the existing build, its
  `install` copies the build over the kernel's installed file before depmod and
  removes the new file when depmod fails. Its `remove --all` uninstalls a
  version from every kernel that has it, and its `remove -k` removes one
  kernel's install and build, and deletes the version when no other kernel has a
  build of it. Every kernel has modules of its own in
  `/lib/modules/<kernel>/kernel` unless a case leaves them out. The cases are:
  - a fresh install, an upgrade that removes the old version only after the
    new one is installed everywhere, a second run with the same source, and a
    failed archive download;
  - a build failure on one kernel and on both, and a kernel with missing
    headers, which keep their working modules. A kernel without headers that
    has the new version installed and passes the check counts as done;
  - a kernel that DKMS lists only for nvpcf-hook, which gets the new version
    like the others, or counts as skipped when its headers are missing;
  - a removed kernel that DKMS lists for nvidia and nvpcf-hook, or only for
    nvpcf-hook, which gets no build, and whose nvpcf-hook install the script
    removes with `dkms remove -k`, after which the old version goes; a removed
    kernel that had the last install of the old version, after which DKMS
    deletes that version and the script removes its source; and a failed
    removal, which keeps the old version and ends the run with status 1. A
    kernel with headers but without modules of its own gets the new version, and
    a running kernel without headers stops the script also when it lacks modules
    of its own;
  - an install that fails before the copy, which leaves the old module, and a
    depmod failure after the copy, after which the kernel gets its previous
    version back from DKMS's build. When depmod fails for that reinstall too,
    the previous version's registration, source and builds stay, also through
    a second run under the same fault, which finds the kernel without any
    installed version. A third run without the fault installs the new version
    everywhere and only then removes the old one. The old version also stays
    when the failed kernel loses its headers before the second run, and goes
    only after a third run with the headers back has installed and checked the
    new version there;
  - a module file without a build ID section, for which `objcopy` exits with
    status 0 and writes an empty file, which fails the check;
  - an install that DKMS reports as successful but that leaves another file,
    which fails two runs in a row while the fault stays, and is installed
    again and passes when it goes away;
  - a registered version that DKMS ranks above the new one or equal to it, a
    `PACKAGE_VERSION` with leading zeros, and the same version with other
    source, which stop the script before it changes anything;
  - an unregistered source directory that holds exactly the source, which the
    script registers and installs, and one with a changed, a missing and an
    extra file, or a file in the directory's place, which stop the script with
    every difference named and stay as they were. The same holds for a
    directory whose `build_id.h` is a symbolic link or a hard link to an
    identical file, for extra names with a space and with a newline that read
    like source names, and for a directory whose listing or one of whose files
    cannot be read;
  - another process creating a file at the source directory's path just before
    the script's `mkdir`, which leaves that file as it was, and a failed copy,
    which removes the run's own directory;
  - an older version whose source directory holds an extra file, whose source
    files go while the directory and that file stay;
  - a failed `dkms add`, which keeps the copied source, and a retry that
    registers that copy;
  - a real Kbuild build in `hook/` between two runs, after which the second
    run finds the registered source unchanged, and the registered source
    builds with Kbuild. This case needs the running kernel's headers;
  - a failing `dkms status` for `nvidia` and for `nvpcf-hook`, missing headers
    for the running kernel, and an empty or extra argument. A running kernel
    without headers that has the new version installed and passes the check
    does not stop the script; one whose installed file does not pass it does;
  - `--no-enable`, which installs the unit without enabling it, and leaves a
    unit that is enabled already as it is.

  Every case that registers the new version checks, with `VER` from
  `/usr/sbin/dkms`, that DKMS would build the new version for the next
  kernel. The fresh install checks that every file in `HOOK_TOOLS` is
  installed unchanged, with its mode. It also runs
  `hook/tests/check_checker.py`, nvpcf_hook's tests of its checker, in a copy
  of the installed directory, and runs that installed `check_nvidia.py` with
  `--kernel-source` on each NVIDIA kernel-open tree in `/usr/src`. So the
  installed checker has to find `expected_dispatcher.c` and
  `expected_procfs_wrappers.h` where `install-hook.sh` put them. This needs at
  least one `/usr/src/nvidia-<version>` tree.
- `make -C hook check` runs nvpcf_hook's own tests, which
  [hook/README.md](hook/README.md) describes.
- `tests/check_build.py` runs `build.sh` as an ordinary user, against mock
  commands and a fake `/usr/src`, `/lib/modules` and `/boot`. Its cases are a
  failing `dkms status`, a version that DKMS lists only as `added`, missing
  headers for the running kernel and for another kernel, a mistyped or extra
  argument, and a full build for two kernels. A mock `update-initramfs` writes a
  new image and renames it over the old one, as `update-initramfs` does. The
  full build has to update each kernel's image after that kernel's `dkms
  install`. Further cases cover a kernel without an image, which gets no update,
  an `update-initramfs` that fails and one that exits 0 without writing, which
  both end the run with status 1, a removed kernel, which `build.sh` leaves out
  without counting it as skipped, and a running kernel without headers or
  modules of its own, which stops the run before any change.
- `tests/check_layout.py` runs as an ordinary user, without a desktop. It checks
  how `reapply-layout.py` rebuilds current modes, mirrored monitors and
  underscanning, and that it sends the layout mode only when mutter supports
  changing it. It also checks the D-Bus argument types and the handling of
  invalid input. To validate the rebuilt configuration against the running
  mutter, run `reapply-layout.py --check` in the session.
- `tests/check_trace_filter.py` runs `trace.sh` as an ordinary user, with
  ordinary files in place of the tracing and debug control files, and with a
  filter write that fails. It checks that `trace.sh` stops before selecting
  `function_graph`, and that it removes the instance. An empty, extra or
  non-numeric argument makes `trace.sh` exit with status 2 before it creates
  an instance.
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
  checks the pixel it reads back. Step 7 of [Changing the driver
  version](README.md#changing-the-driver-version) in README.md has the build and
  run commands. On 615.71.09, it prints EGL 1.5 NVIDIA, the RTX 4070 Laptop GPU
  and OpenGL ES 3.2 NVIDIA 615.71.09, and it reads back 64 127 191 255. The
  expected pixel is 64 128 191 255, with one unit of tolerance per channel. The
  test checks only programs started after a swap, because programs started
  before a swap keep the old libraries.
