# nvidia-live-swap

`swap.sh` replaces the loaded NVIDIA kernel driver with the driver installed on
disk, while the GNOME session keeps running. So a new or patched driver can be
loaded without a logout or a reboot. A program that had the NVIDIA GPU open
loses its access to the GPU, and gets it back when it restarts. gnome-shell, the
program that runs the GNOME desktop, gets it back at the next login.

`build.sh` builds Ubuntu's NVIDIA driver with a fix for the Lenovo Legion 7
16IRX9 laptop. In GNOME's Balanced power mode, the laptop's firmware sends its
power limits to the NVIDIA driver every 7 seconds, even when the limits stay the
same. If the GPU is asleep, the unpatched driver wakes it up to handle the
message. So the GPU never sleeps for more than about a second, and the laptop
runs warm with its fans spinning. With the fix, the driver holds the message
while the GPU sleeps, and passes it on when the GPU next wakes up. The GPU then
stays asleep. In the Performance and Power Saver modes, the firmware does not
send these messages.

The fix is [pull request #1299][pr1299] to NVIDIA's open-gpu-kernel-modules
repository, by Lars Nieradzik. A second patch from this repository fixes a crash
in the pull request's code. The crash can happen only if a memory allocation
fails while the driver loads.

The scripts are tested on a Legion 7 16IRX9 (RTX 4070 Laptop GPU) with Ubuntu
24.04, kernel 7.0, GNOME 46 on Wayland, and Ubuntu's packages of NVIDIA's open
kernel modules.

## Usage

Run the scripts from the repository directory.

- `sudo ./build.sh [version]` builds the patched driver for the installed
  kernels. Without a version, it builds the newest NVIDIA driver version that
  DKMS lists. DKMS is the system that rebuilds drivers for each installed
  kernel. The patched driver loads at the next boot, or right away with
  `swap.sh`.
- `sudo ./swap.sh --check` lists the programs that have the NVIDIA GPU open, and
  says whether a swap can go ahead now. It only reads the state of the system.
- `sudo ./swap.sh` unloads the NVIDIA kernel modules and loads the modules
  installed on disk. Then it checks that the loaded modules match the files on
  disk and contain the fix.
- `sudo ./trace.sh [seconds]` records the firmware's messages to the driver for
  60 seconds, or for the given number of seconds. It shows each message, how
  long the driver takes to handle it, and when the GPU goes to sleep or wakes
  up. Run it in the Balanced mode. A message that wakes the GPU takes about 2
  seconds to handle. A message that leaves the GPU asleep takes microseconds.

To install the fix without logging out, run `sudo ./build.sh`, then
`sudo ./swap.sh --check`, and then `sudo ./swap.sh`.

`swap.sh` needs gdb, the GNU debugger, which it uses to take the GPU away from
running programs. It also runs `reapply-layout.py`, which needs Ubuntu's
`python3-gi` package. `build.sh` needs git to download NVIDIA's source from
GitHub.

## Side effects of a swap

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
- During the swap, `swap.sh` renames three registration files, through which
  programs find NVIDIA's graphics libraries, and gives them their names back
  at the end. A program that starts during the swap therefore does not load
  NVIDIA's graphics libraries. A program that opens the GPU's device files
  directly, such as `nvidia-smi`, can still open the GPU during the swap and
  make the swap fail.

## Limitations

`swap.sh` refuses to swap in the following cases. It checks for each of them
before it renames files, stops services or takes the GPU away from any program.

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

Swaps have to be at least 20 seconds apart. During a swap, GNOME asks
rtkit-daemon, the service that grants real-time priority to threads, several
times to change the priority of its display thread. rtkit-daemon accepts 25
such requests per user in 20 seconds. When it refuses one, the kernel can kill
gnome-shell, which ends the session. [WRITEUP.md](WRITEUP.md#swapsh) explains
how. So if the previous swap ended less than 20 seconds earlier, `swap.sh`
waits for the rest of the 20 seconds. `swap.sh --check` shows how long that
wait would be.

If a swap fails or is interrupted, `swap.sh` tries to load from disk every
NVIDIA module that was loaded before the swap and is now missing. It also tries
to rename the registration files back and to start the services it stopped.
Then it exits with an error. The modules it loads come from disk, so they can be
a different build from the modules it unloaded. A program that lost its access
to the GPU gets it back only when it restarts.

A new driver version also installs new NVIDIA libraries, which programs load to
use the GPU. These libraries work only with kernel modules of the same version.
So a program started after the new packages are installed and before the swap
cannot use the GPU. A program started before the new packages were installed
keeps the old libraries until it restarts. gnome-shell keeps them until the next
login.

## Driver and kernel updates

When Ubuntu's NVIDIA driver package is installed, DKMS compiles the kernel
modules on the machine. Only the driver core, a part of the `nvidia` module,
comes precompiled in the package. `build.sh` compiles the driver core from
NVIDIA's source with the fix, and puts it in place of Ubuntu's copy. A dpkg
diversion tells dpkg, Ubuntu's package manager, to keep Ubuntu's copy under
another name. dpkg then leaves the patched file in place.

A new NVIDIA driver version comes with Ubuntu's unpatched driver core in its own
directory, `/usr/src/nvidia-<version>`. Run `sudo ./build.sh` after each driver
update. The new version's modules lack the fix until `build.sh` has run for that
version. If they load without the fix, the GPU wakes up for every firmware
message again.

A kernel update keeps the fix. DKMS builds the new kernel's modules from
`/usr/src/nvidia-<version>`, which has the patched core as long as the dpkg
diversion is in place.

## Changing the driver version

These steps change the driver version of the running session, for example from
595.99.02 to 615.71.09.

1. Run `sudo ./swap.sh --check` and check that a swap can go ahead.
2. Put Ubuntu's driver core of the installed version back and remove its
   diversion, with the two commands in [Restoring Ubuntu's driver
   core](#restoring-ubuntus-driver-core). apt can then remove the old package
   cleanly.
3. Run `sudo apt-get install nvidia-driver-<branch>-open`, where `<branch>` is
   the first number of the version, such as 615. apt replaces the old packages,
   but it keeps the old firmware package and marks it for removal by
   `apt-get autoremove`. Leave that package installed until after the swap. The
   old driver keeps running until the swap, and it reads its firmware from that
   package whenever a program opens the GPU after all other programs have closed
   it.
4. The package installation builds the new version through DKMS only for the
   running kernel. For each other kernel that should keep an NVIDIA driver, run
   `sudo dkms install -m nvidia -v <version> -k <kernel>`.
5. Run `sudo ./build.sh <version>`.
6. Run `sudo ./swap.sh`.
7. Remove the old firmware package with
   `sudo apt-get remove nvidia-firmware-<old branch>-<old version>`.
8. As the desktop user, check that a newly started program can render with the
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
these two commands as root for each kernel:

    dkms build -m nvidia -v <version> -k <kernel> --force
    dkms install -m nvidia -v <version> -k <kernel> --force

Without `--force`, `dkms build` skips a kernel whose modules are already built.
The modules without the fix load at the next boot.

## Technical details

[WRITEUP.md](WRITEUP.md) describes the firmware and driver code behind the
problem, the two patches, the measurements, and how each script and test works.

[pr1299]: https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1299
