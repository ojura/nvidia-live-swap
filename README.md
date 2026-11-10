# nvidia-live-swap

`swap.sh` replaces the loaded NVIDIA kernel driver with the driver installed on
disk, while the GNOME session keeps running. So a new or patched driver can be
loaded without a logout or a reboot. A program that had the NVIDIA GPU open
loses its access to the GPU, and gets it back when it restarts. gnome-shell, the
program that runs the GNOME desktop, gets it back at the next login. `swap.sh`
refuses every swap while nvidia_drm, the NVIDIA module behind the GPU's display
and render devices, is loaded. On the Legion it is always loaded, so `swap.sh`
refuses every swap there. [Limitations](#limitations) explains why.

The repository also has a fix for the Lenovo Legion 7 16IRX9 laptop. In GNOME's
Balanced power mode, the laptop's firmware sends its power limits to the NVIDIA
driver every 7 seconds, even when the limits stay the same. If the GPU is
asleep, the unpatched driver wakes it up to handle the message. So the GPU never
sleeps for more than about a second, and the laptop runs warm with its fans
spinning. With the fix, the message is held while the GPU sleeps, and passed on
when the GPU next wakes up. The GPU then stays asleep. In the Performance and
Power Saver modes, the firmware does not send these messages.

There are two ways to install the fix:

- `build.sh` builds Ubuntu's NVIDIA driver with [pull request #1299][pr1299] to
  NVIDIA's open-gpu-kernel-modules repository, by Lars Nieradzik, and a second
  patch from this repository. The second patch fixes a crash in the pull
  request's code, which can happen only if a memory allocation fails while the
  driver loads. Each new driver version needs `build.sh` again.
- `install-hook.sh` installs nvpcf_hook, a separate kernel module that catches
  the messages before Ubuntu's unchanged driver handles them. When Ubuntu
  installs a new driver version, for example through automatic updates, the
  hook's loader checks the new driver's code before the hook attaches to it, so
  the fix works after the update without rebuilding the driver core. The hook
  also guards NVIDIA's shutdown of the GPU against a hang described in
  [Limitations](#limitations).

nvpcf_hook is experimental. Version 0.1.1 passes the automated tests and builds
for kernels 6.8.0-147, 7.0.0-34 and 7.0.0-38, but has not been loaded into a
running kernel. Live tests of idle deferral, delivery after a wake, detach and
resume, system suspend and recovery, and operation with Ubuntu's unpatched
driver core remain incomplete. During testing, install with `--no-enable` and
load the hook by hand. Leave the existing driver-core patch in place for the
initial tests.

nvpcf_hook supports only the Legion 7 16IRX9 configuration that
[hook/README.md](hook/README.md#supported-pci-configuration) specifies: DMI
vendor `LENOVO` and product `83FD`, with the NVIDIA GPU `10de:2860` (RTX 4070
Laptop GPU) directly on the CPU's PCIe root port, Intel `8086:a70d`. It refuses
to load on other hardware. Every swap needs the hook, so `swap.sh` also works
only on that configuration. `build.sh` does not depend on the hook. The scripts
are tested on that laptop with Ubuntu 24.04, kernel 7.0, GNOME 46 on Wayland,
and Ubuntu's packages of NVIDIA's open kernel modules.

## Usage

Run the scripts from the repository directory.

- `sudo ./build.sh [version]` builds the patched driver for the installed
  kernels. Without a version, it builds the newest NVIDIA driver version that
  DKMS lists. DKMS is the system that rebuilds drivers for each installed
  kernel. `build.sh` also updates each of these kernels' initramfs, the image
  that the kernel runs before it mounts the root filesystem, because that
  image can hold its own copy of the driver. The patched driver loads at the
  next boot.
- `sudo ./swap.sh --check` lists the programs that have the NVIDIA GPU open, and
  says whether a swap can go ahead now. It only reads the state of the system.
- `sudo ./swap.sh` unloads the NVIDIA kernel modules and loads the modules
  installed on disk. Then it checks that the loaded modules match the files on
  disk, and that nvpcf_hook is active. Every swap needs nvpcf_hook, and
  `swap.sh` refuses every swap while nvidia_drm is loaded, as
  [Limitations](#limitations) explains.
- `sudo ./install-hook.sh` installs nvpcf_hook with DKMS for the installed
  kernels, together with its loader and a systemd unit that runs the loader at
  boot. With `--no-enable`, it installs the unit without enabling it; a unit
  that is already enabled stays enabled. `sudo
  /usr/local/lib/nvpcf-hook/load-hook.sh` loads the hook now. The loader checks
  the loaded NVIDIA driver's code first, and loads the hook only if the checks
  pass.
- `sudo ./hook/nvpcf-hookctl status` shows nvpcf_hook's state and counts. In the
  state `active`, the hook holds messages while the GPU sleeps, and passes them
  on when the GPU wakes up. In `waiting`, it waits for a program to start the
  GPU. [hook/README.md](hook/README.md) describes the other states and the
  counts.
- `sudo ./trace.sh [seconds]` records, for 60 seconds or the given number of
  seconds, the firmware's queries, each call of NVIDIA's handler for the
  messages with its duration, and when the GPU goes to sleep or wakes up. Run
  it in the Balanced mode. A call that wakes the GPU takes about 2 seconds. A
  call that leaves the GPU asleep takes microseconds. With nvpcf_hook loaded,
  the handler also runs when the hook passes held messages on. `trace.sh` then
  prints the hook's counts before and after the trace, which tell the two
  apart.

Loading nvpcf_hook applies the fix to the running driver without a swap. For a
live test, run `sudo ./install-hook.sh --no-enable`, then
`sudo /usr/local/lib/nvpcf-hook/load-hook.sh`, which reports the hook's state.

`swap.sh` needs gdb, the GNU debugger, which it uses to take the GPU away from
running programs. It also runs `reapply-layout.py`, which needs Ubuntu's
`python3-gi` package. `build.sh` needs git to download NVIDIA's source from
GitHub. `install-hook.sh` needs the kernel headers of each kernel it builds
the hook for.

## Side effects of a swap

`swap.sh` refuses every swap while nvidia_drm is loaded, and on the Legion it is
always loaded, so these effects do not occur there now. They describe the swap
code, which the tests check with the refusal removed.

A swap takes 5 to 7 seconds. The kernel unloads the driver only after every
program has closed the driver's device files. So `swap.sh` first takes the GPU
away from the programs that have it open.

- A program that does not run as root loses its access to the GPU. `swap.sh`
  replaces the program's NVIDIA file handles with handles to `/dev/null`, an
  empty device, and the program pauses for 0.1 to 2 seconds while this happens.
  `swap.sh` does not stop the program, but the program's later calls to the
  NVIDIA GPU fail until it restarts. A program that does not handle these
  failures can crash. On the Legion, gnome-shell, Xwayland and two Electron
  programs keep running.
- gnome-shell and Xwayland lose their access too. Xwayland is the server that
  lets X11 programs run on Wayland. On Wayland, gnome-shell is the display
  server itself, so restarting it ends everything that draws on the screen.
  gnome-shell therefore gets the GPU back only at the next login.
- On the Legion, the built-in screen runs on the Intel GPU and keeps working.
  The HDMI port and one of the DisplayPort/USB-C ports are wired to the NVIDIA
  GPU. Until the next login, GNOME cannot use a monitor on these two ports.
  Plugging a monitor into one of them may crash gnome-shell, which ends the
  session.
- `swap.sh` stops LACT's service `lactd` and NVIDIA's services
  `nvidia-persistenced` and `nvidia-powerd` if they have the GPU open, and
  starts them again after the swap. LACT is a tool that monitors and tunes the
  GPU.
- GNOME keeps the NVIDIA GPU's display device, `/dev/dri/cardN`, open even while
  every monitor runs on the Intel GPU, and opens it again right after each swap.
  `swap.sh` makes GNOME close it by applying the current screen layout again,
  with the same resolution, refresh rate and scaling.
- During the swap, `swap.sh` renames the three registration files through
  which the EGL and Vulkan loaders find NVIDIA's graphics libraries, and gives
  them their names back at the end. While the files are renamed, these loaders
  do not find NVIDIA's libraries. A program that opens the GPU's device files
  directly, such as `nvidia-smi`, can still open the GPU during the swap, and
  can make the swap fail if it holds them open when the driver is unloaded.
- `swap.sh` prepares nvpcf_hook before it changes anything, and detaches it
  once the other programs have let go of the GPU. It unloads the hook after
  NVIDIA's driver. Then it loads NVIDIA's main module, the hook, and the
  modules that start the GPU, in that order, so that the hook's guard is in
  place before the GPU starts. The swap succeeds only if the hook is active
  afterwards.

## Limitations

`swap.sh` refuses to swap in the following cases. It checks for each of them
before it renames files, stops services or takes the GPU away from any program.

- nvidia_drm is loaded. The paragraphs after this list explain why `swap.sh`
  does not unload it.
- A monitor is connected to the NVIDIA GPU. This includes the built-in screen
  when the laptop is set to drive it from the NVIDIA GPU.
- A program other than gnome-shell has the display device open. systemd and
  systemd-logind keep copies of gnome-shell's handle to the device, and
  `swap.sh` accepts those copies.
- Applying the screen layout again fails, or GNOME still has the display device
  open 10 seconds afterwards.
- A process that runs as root, other than the three services above, has the GPU
  open. `swap.sh --check` names the process. `swap.sh` does not stop it, because
  stopping it could end someone's work.
- A registration file is still renamed from an interrupted earlier swap.
  `swap.sh --check` names the file. Remove its `.hotswap` suffix before the next
  swap.
- The driver that the swap would load fails nvpcf_hook's checks. The loader
  runs them on that driver before the swap changes anything.
- nvpcf_hook is not loaded, not installed for the running kernel, or in a state
  that does not allow a swap. `swap.sh --check` says which. If the hook lost
  messages it held, for example because the GPU disappeared, it stays
  `unavailable` until a person runs `sudo ./hook/nvpcf-hookctl acknowledge-loss`
  after reading its status and the kernel log. `swap.sh` never runs that
  command. If the hook could not finish a cleanup, such as removing its
  interception of NVIDIA's functions, its status shows `retirement_failed: 1`,
  and `sudo ./hook/nvpcf-hookctl detach` retries the cleanup.
- A file that `install-hook.sh` installs in `/usr/local/lib/nvpcf-hook` is
  missing there, or differs from the repository's file, for example after a
  `git pull`. Run `sudo ./install-hook.sh` first.

`swap.sh` does not unload nvidia_drm. nvidia_drm creates dma fences, the kernel
objects through which drivers and programs wait for GPU work, and each fence
points to functions in nvidia_drm's module. The kernel requires a module whose
fences have a release function to stay loaded as long as any of its fences
exists, but nvidia_drm does not take module references for its fences. A program
or another driver can still hold one after the unload, in a sync file, a DRM
sync object or another driver's buffer. Releasing it then makes the kernel use
memory that was freed with the module. On the Legion, gnome-shell crashed in the
kernel at a logout on 2026-11-02, a day after the swaps of 2026-11-01, while it
released a fence whose functions pointed into unmapped module memory. Which
module that fence came from is not known. Its function table does not sit at the
page offset of either fence table of the 615.71.09 nvidia_drm, so if it came
from nvidia_drm, it came from one of the two 595.99.02 builds unloaded that day,
which were removed from disk with their packages. Checking the programs' open
files cannot establish that every fence was released, because kernel objects
such as other drivers' buffers keep references too, and inspecting a stale fence
can run its functions. Swaps need a fix in nvidia_drm or in the kernel that
keeps the module's code available as long as its fences can call it. Until then,
a reboot loads a new NVIDIA driver or a new version of nvpcf_hook. After a swap,
stale fences can remain until the next reboot.
[WRITEUP.md](WRITEUP.md#fences-that-outlive-nvidia_drm) has the details.

A swap without nvpcf_hook could hang. When the last program closes the GPU,
NVIDIA shuts down its state for the GPU, and holds a lock while it does. If a
firmware message about the power limits or the AC adapter arrives just then,
its handler waits for that lock, and the shutdown waits for the handler. The
program that closed the GPU last then hangs. During a swap that is often
gnome-shell, which freezes the desktop. nvpcf_hook removes NVIDIA's firmware
message handlers before the shutdown takes the lock. Patch 0001 does not help
here, because the patched driver takes the same lock before it holds a
message. So `swap.sh` refuses to swap without the hook, which means that it
refuses every swap on hardware that the hook does not support. The hang is
inferred from NVIDIA's source and has not been reproduced.
[WRITEUP.md](WRITEUP.md#the-hang-in-nvidias-shutdown) has the details.

Swaps have to be at least 20 seconds apart. During a swap, GNOME asks
rtkit-daemon, the service that grants real-time priority to threads, several
times to change the priority of its display thread. rtkit-daemon accepts 25
such requests per user in 20 seconds. When it refuses one, the kernel can kill
gnome-shell, which ends the session. [WRITEUP.md](WRITEUP.md#swapsh) explains
how. So if the previous swap ended less than 20 seconds earlier, `swap.sh`
waits for the rest of the 20 seconds. `swap.sh --check` shows how long that
wait would be.

If a swap fails or is interrupted, `swap.sh` first tries to restore `nvidia` and
its hook. It restores the other missing NVIDIA modules and the services it
stopped only after the loader confirms that the hook guards NVIDIA's shutdown
and allows GPU initialization. Otherwise it leaves those modules unloaded and
those services stopped, and names them. It tries to rename the registration
files back in either case, then exits with an error. Modules restored from disk
can differ from the modules it unloaded. A program that lost its access to the
GPU gets it back only when it restarts.

A new driver version also installs new NVIDIA libraries, which programs load to
use the GPU. These libraries work only with kernel modules of the same version.
So a program started after the new packages are installed and before the swap
cannot use the GPU. A program started before the new packages were installed
keeps the old libraries until it restarts. gnome-shell keeps them until the next
login.

## Driver and kernel updates

With nvpcf_hook, a new NVIDIA driver version keeps the fix without a rebuild.
Each time the hook loads, its loader checks the code of the loaded driver. It
reads the module file with the same build ID, the version's source in
`/usr/src`, and NVIDIA's source archive for the version, which it downloads once
and keeps in `/var/cache/nvpcf-hook`. It loads the hook only if the checks pass,
and the hook then refuses to attach to any other driver build. DKMS builds the
hook for each new kernel.

If the checks fail, or the hook cannot attach, the GPU wakes up for every
firmware message again. The loader's output, `journalctl -u nvpcf-hook` after a
boot, and the kernel log say why. The checks look at the functions, calls and
source that [hook/README.md](hook/README.md) lists. A change in NVIDIA's driver
outside those can still break the hook.

With `build.sh`, the fix is part of the driver. When Ubuntu's NVIDIA driver
package is installed, DKMS compiles the kernel modules on the machine. Only the
driver core, a part of the `nvidia` module, comes precompiled in the package.
`build.sh` compiles the driver core from NVIDIA's source with the fix, and puts
it in place of Ubuntu's copy. A dpkg diversion tells dpkg, Ubuntu's package
manager, to keep Ubuntu's copy under another name. dpkg then leaves the patched
file in place.

A new NVIDIA driver version comes with Ubuntu's unpatched driver core in its own
directory, `/usr/src/nvidia-<version>`. Run `sudo ./build.sh` after each driver
update. The new version's modules lack the fix until `build.sh` has run for that
version. If they load without the fix, the GPU wakes up for every firmware
message again.

A kernel update keeps the fix. DKMS builds the new kernel's modules from
`/usr/src/nvidia-<version>`, which has the patched core as long as the dpkg
diversion is in place. The kernel package builds the new kernel's initramfs
after DKMS has built the modules, so the initramfs gets the patched copies.

## Switching from build.sh to nvpcf_hook

1. Run `sudo ./install-hook.sh`.
2. Put Ubuntu's driver core back, and rebuild the driver and the initramfs for
   each kernel, with the commands in [Restoring Ubuntu's driver
   core](#restoring-ubuntus-driver-core).
3. Reboot. Ubuntu's unchanged driver loads, and `nvpcf-hook.service` checks it
   and loads the hook.
4. Run `sudo ./hook/nvpcf-hookctl status`, and check that the state is
   `active`.

## Changing the driver version

These steps change the driver version, for example from 595.99.02 to
615.71.09. They end with a reboot, because `swap.sh` refuses while nvidia_drm is
loaded.

1. If the installed version has the patched core from `build.sh`, put Ubuntu's
   driver core back and remove its diversion, with the two commands in
   [Restoring Ubuntu's driver core](#restoring-ubuntus-driver-core). apt can
   then remove the old package cleanly.
2. Run `sudo apt-get install nvidia-driver-<branch>-open`, where `<branch>` is
   the first number of the version, such as 615. apt replaces the old packages,
   but it keeps the old firmware package and marks it for removal by
   `apt-get autoremove`. Leave that package installed until after the reboot.
   The old driver keeps running until the reboot, and it reads its firmware from
   that package whenever a program opens the GPU after all other programs have
   closed it.
3. The package installation builds the new version through DKMS only for the
   running kernel. For each other kernel that should keep an NVIDIA driver, run
   `sudo dkms install -m nvidia -v <version> -k <kernel>`. Then run
   `sudo update-initramfs -u -k <kernel>` for each of these kernels and for the
   running kernel, if `/boot/initrd.img-<kernel>` exists, so that each
   initramfs holds the new version's modules.
4. With `build.sh`, run `sudo ./build.sh <version>`. With nvpcf_hook, skip this
   step.
5. Reboot.
6. Remove the old firmware package with
   `sudo apt-get remove nvidia-firmware-<old branch>-<old version>`.
7. As the desktop user, check that a newly started program can render with the
   new driver:

   ```
   cc -std=c11 -Wall -Wextra -Werror -O2 tests/check_egl.c -o /tmp/check-nvidia-egl $(pkg-config --cflags --libs egl glesv2)
   /tmp/check-nvidia-egl
   ```

## Restoring Ubuntu's driver core

`build.sh` installs the patched core as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`. The dpkg diversion keeps
Ubuntu's copy as `nv-kernel.o_binary.ubuntu`. Before apt removes a driver
version that has the patched core, put Ubuntu's copy back, so that the package
removes its files cleanly. Run these two commands as root:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

To remove the fix from a driver version, run the same two commands. Then run
these commands as root for each kernel:

    dkms build -m nvidia -v <version> -k <kernel> --force
    dkms install -m nvidia -v <version> -k <kernel> --force
    update-initramfs -u -k <kernel>

Without `--force`, `dkms build` skips a kernel whose modules are already built.
Leave out `update-initramfs` for a kernel that does not have
`/boot/initrd.img-<kernel>`. The initramfs can hold its own copy of the nvidia
modules, which the kernel loads before it mounts the root filesystem, so
without the update the next boot still loads the modules with the fix. After
these commands, the next boot loads the modules without the fix.

## Technical details

[WRITEUP.md](WRITEUP.md) describes the firmware and driver code behind the
problem, the two patches, the measurements, and how each script and test works.
[hook/README.md](hook/README.md) describes nvpcf_hook's design and its checks.

[pr1299]: https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1299
