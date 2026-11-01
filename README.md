# nvidia-live-swap

Replaces the NVIDIA driver on a running Ubuntu GNOME desktop without logging
out or rebooting. Your windows, terminals and programs stay open, and the swap
takes about 7 seconds. Programs that were using the NVIDIA graphics chip keep
running, but need a restart to use it again.

It was built for the Lenovo Legion 7 16IRX9. In the Balanced power mode, that
laptop's NVIDIA graphics chip hardly ever gets to sleep, so the laptop runs
warm and the fans keep spinning. Every 7 seconds, the laptop's firmware sends
the NVIDIA driver the same message about power limits, whether or not the
limits have changed, and the stock driver wakes the chip to handle each one.
So the chip never sleeps for more than about a second. In the Performance and
Power Saver modes the firmware does not send the message.

This repository builds Ubuntu's NVIDIA driver with a fix. While the chip
sleeps, the driver holds the message back and passes it on when the chip next
wakes up for other work. With the fix loaded, the chip goes to sleep in
Balanced and stays asleep while the messages keep coming. The fix is pull
request #1299 to NVIDIA's open driver, by Lars Nieradzik, plus a second patch
that makes it apply only in the power management mode in which the driver
wakes the chip for these messages.

`build.sh` builds and installs the patched driver, and `swap.sh` loads it into
the running session. Together they also move a running session to a new
driver version, as they did from 595.99.02 to 615.71.09.

## Usage

The NVIDIA driver consists of four kernel modules, `nvidia`, `nvidia_modeset`,
`nvidia_drm` and `nvidia_uvm`, which run inside the Linux kernel, and of
libraries that programs load to use the chip. Ubuntu's driver package compiles
the kernel modules on your machine with DKMS, the system that builds them for
each installed kernel, but it installs the driver core already compiled.
`build.sh` compiles the core from NVIDIA's published source with the fix and
puts it in place of Ubuntu's copy. It also registers a dpkg diversion, which
tells dpkg, Ubuntu's package manager, to keep Ubuntu's copy under another
name. `swap.sh` replaces the running kernel modules with the ones on disk.

Run the scripts from the repository's directory:

- `sudo ./build.sh [version]` builds the patched driver for the NVIDIA driver
  version you give, or for the newest version DKMS lists, and installs it for
  your kernels. It loads at the next boot, or right away with `swap.sh`.
- `sudo ./swap.sh --check` reports which programs have the NVIDIA chip open,
  and whether a swap can go ahead now. It only reads, and leaves the system as
  it is.
- `sudo ./swap.sh` unloads the running NVIDIA kernel modules and loads the
  ones installed on disk. At the end it checks that the modules now running
  are the ones on disk and that they contain the fix.
- `sudo ./trace.sh [seconds]`, run with the laptop in Balanced, watches for 60
  seconds or for the number of seconds you give. It shows when the NVIDIA chip
  falls asleep or wakes up, when the firmware sent its messages, and how long
  the driver took for each. A message that wakes the chip takes about 2
  seconds. One that leaves it asleep takes microseconds.

To install the fix without logging out, run `sudo ./build.sh`, then
`sudo ./swap.sh --check`, then `sudo ./swap.sh`.

`swap.sh` needs gdb. It also runs `reapply-layout.py`, which needs Ubuntu's
`python3-gi`. `build.sh` needs git to download NVIDIA's source from GitHub.

The tools have run on a Lenovo Legion 7 16IRX9 with an RTX 4070 Laptop GPU,
under Ubuntu 24.04, kernel 7.0 and GNOME 46 on Wayland, with Ubuntu's packages
of NVIDIA's open kernel modules. On 2026-11-01 `swap.sh` replaced the stock
595.99.02 driver with the patched build of the same version in 6.7 seconds.
GNOME Shell (the desktop), Xwayland (which runs X11 programs) and every open
program kept running. Later the same day it moved the same session from
595.99.02 to 615.71.09 in 6.8 seconds.

## Side effects of a swap

Linux unloads a driver only after every program has let go of the devices the
driver provides. So `swap.sh` first takes the NVIDIA chip away from the
programs that have it open, and that has costs:

- Every program that has the chip open and does not run as root loses its
  access to it, GNOME Shell and Xwayland included. In the swaps so far, each
  was paused for 0.1 to 2 seconds while that happened. It then keeps running,
  but cannot use the NVIDIA chip until it restarts, and GNOME Shell restarts
  only when you log in again.
- On the Legion 7 16IRX9 the built-in screen runs on the Intel graphics built
  into the processor, and it keeps working. Until your next login, though,
  GNOME cannot use a monitor plugged into the HDMI port, or into the
  DisplayPort or USB-C port wired to the NVIDIA chip, and it may crash when you
  plug one in, which ends your session.
- The services `lactd` (LACT's daemon), `nvidia-persistenced` and
  `nvidia-powerd`, which only watch or tune the chip, are stopped during the
  swap if they have the chip open, and started again afterwards.
- GNOME keeps the chip's display device, `/dev/dri/cardN`, open even while
  all screens run on the Intel graphics. That is the device file GNOME would
  use for screens connected to the chip. GNOME lets go of it the next time it
  sets up the screens. So `swap.sh` first asks GNOME to apply the current
  screen layout again, and waits up to 10 seconds for GNOME to let go. The
  resolution, refresh rate and scaling stay as they were. GNOME opens the new
  display device as soon as the new driver is loaded, so the next swap goes
  through this step again.
- During the swap, `swap.sh` hides NVIDIA's registration files, which tell
  graphics programs where to find NVIDIA's libraries, so that programs started
  in the meantime do not open the chip.

## Limitations

`swap.sh` refuses to swap in the following cases, and stops before it hides
files, stops services or takes away any program's access:

- A screen is connected to the NVIDIA chip. That can be a monitor on one of
  the ports wired to it, or the built-in screen when the laptop is set to run
  it on the NVIDIA chip. Unplug the monitor first.
- The display device is held open by a program other than GNOME Shell, so
  applying the layout again would not release it. (systemd and systemd-logind
  keep copies of GNOME Shell's handle on it. `swap.sh` accepts those while
  GNOME Shell holds the device too.)
- GNOME has not let go of the display device 10 seconds after applying the
  layout again.
- A program running as root has the chip open and is not part of one of the
  three services above. `swap.sh` leaves such programs alone, also when they
  run in another service, because stopping them could end someone's work.
  `swap.sh --check` names them, and you can stop them yourself if that is
  safe.
- A registration file is still hidden from an earlier swap that was
  interrupted. `swap.sh --check` names the hidden file. Rename it back,
  without its `.hotswap` suffix, before you swap.

Swaps have to be at least 20 seconds apart. Each swap makes GNOME ask
rtkit-daemon several times to switch its display thread out of real-time
priority and back. rtkit-daemon is the system service that grants real-time
priority, and by default it accepts only 25 such requests per user in 20
seconds. So `swap.sh` waits until 20 seconds have passed since the previous
swap ended, and `swap.sh --check` tells you how long it would wait.

If a swap fails or is interrupted, `swap.sh` exits with an error after trying
to put back what it changed. It loads from disk every NVIDIA kernel module
that was loaded before and is now missing, moves the hidden files back and
starts the stopped services. Programs whose access it took away stay without
the NVIDIA chip. The modules it loads again come from disk, which can be a
different build from the one it unloaded.

A new driver version also replaces NVIDIA's libraries, and those libraries
refuse to work with kernel modules of another version. Programs started
between installing the new packages and the swap cannot use the NVIDIA chip.
Programs that were running before keep the old libraries until they restart,
and GNOME Shell keeps them until your next login.

## Driver and kernel updates

Each new NVIDIA driver version installs its own `/usr/src/nvidia-<version>`
with NVIDIA's precompiled driver core, which lacks the fix. The chip starts
waking up again until you run `sudo ./build.sh` for that version.

After a kernel update, DKMS builds the new kernel's modules from
`/usr/src/nvidia-<version>`. That directory holds the patched core for as long
as the dpkg diversion is in place, so the new kernel gets the fix too.

## Changing the driver version

These steps moved the running session from 595.99.02 to 615.71.09 on
2026-11-01:

1. Check that a swap can go ahead: `sudo ./swap.sh --check`.
2. Put back Ubuntu's driver core for the installed version and remove its
   diversion, with the two commands under "Restoring Ubuntu's driver core"
   below, so that apt removes the old package cleanly.
3. Install the new version: `sudo apt-get install nvidia-driver-<branch>-open`.
   apt replaces the old packages, keeps the old firmware package and marks it
   for autoremove. Keep that package until after the swap, because the old
   driver, which is still running, loads the chip's firmware from it each time
   a program opens the chip after all others have closed it.
4. Ubuntu's DKMS hook builds the new version only for the running kernel. For
   every other kernel that should keep an NVIDIA driver, run
   `sudo dkms install -m nvidia -v <version> -k <kernel>`.
5. Build the patched driver for the new version: `sudo ./build.sh <version>`.
6. Swap: `sudo ./swap.sh`.
7. Remove the old firmware package:
   `sudo apt-get remove nvidia-firmware-<old branch>-<old version>`.
8. Check that a program started after the swap renders with the new driver.
   As the desktop user, build and run `tests/check_egl.c`:

   ```
   cc -std=c11 -Wall -Wextra -Werror -O2 tests/check_egl.c -o /tmp/check-nvidia-egl $(pkg-config --cflags --libs egl glesv2)
   /tmp/check-nvidia-egl
   ```

In the move to 615.71.09, apt replaced 20 NVIDIA packages with 21, including
the 32-bit libraries. Ubuntu's `nvidia-dkms-615-open` adds two build fixes for
7.0 kernels. They change only the code that connects the driver to the Linux
kernel, and leave the driver core, which `build.sh` replaces, untouched. The
swap had to stop only `lactd`, because the earlier swap had already taken the
chip away from the other programs that held it.

## Restoring Ubuntu's driver core

`build.sh` installs the patched core as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary`, and its dpkg diversion
keeps Ubuntu's copy as `nv-kernel.o_binary.ubuntu`. Before apt removes a
driver version whose source holds the patched core, put Ubuntu's copy back, so
that the package removes its files cleanly. As root:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

To remove the fix itself, run the same two commands, then `dkms build --force`
and `dkms install --force` for each kernel:

    dkms build -m nvidia -v <version> -k <kernel> --force
    dkms install -m nvidia -v <version> -k <kernel> --force

Without `--force`, `dkms build` skips a kernel whose modules are already
built. The modules without the fix load at the next boot.

## Technical details

[WRITEUP.md](WRITEUP.md) is for the technically minded: the firmware message
and the NVIDIA driver code behind the problem, the two patches, the
measurements, and what each script and test does.
