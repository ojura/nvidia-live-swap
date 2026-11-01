# nvidia-live-swap

`swap.sh` replaces the loaded NVIDIA kernel driver with the one installed on
disk without ending the GNOME session. Programs keep running. The ones that
had the NVIDIA GPU open lose access to it until they restart, gnome-shell
until the next login. On 2026-11-01 it moved a Lenovo Legion 7 16IRX9 from
NVIDIA driver 595.99.02 to 615.71.09 in 6.8 seconds.

`build.sh` builds Ubuntu's NVIDIA driver with a fix for that laptop. In the
Balanced power mode, the Legion 7 16IRX9's firmware sends the NVIDIA driver
the same power-limit message every 7 seconds, and the stock driver wakes the
sleeping GPU for each one. The GPU never sleeps for more than about a second,
so the laptop runs warm and the fans keep spinning. With the fix, the driver
holds the message while the GPU sleeps and passes it on when the GPU next
wakes up, so the GPU stays asleep. In Performance and Power Saver the firmware
does not send the message.

The fix is NVIDIA pull request #1299 by Lars Nieradzik, plus a second patch
from this repository that limits it to the power management mode in which the
driver wakes the GPU for these messages.

## Usage

Run the scripts from the repository directory:

- `sudo ./build.sh [version]` builds the patched driver for that NVIDIA driver
  version, by default the newest one DKMS lists, for the installed kernels. It
  loads at the next boot, or now with `swap.sh`.
- `sudo ./swap.sh --check` lists the programs that have the NVIDIA GPU open
  and says whether a swap can go ahead now. It only reads.
- `sudo ./swap.sh` unloads the NVIDIA kernel modules, loads the ones on disk,
  and checks that the loaded modules are the files on disk and contain the
  fix.
- `sudo ./trace.sh [seconds]` shows, for 60 seconds or the given time, each
  firmware message, how long the driver took for it, and when the GPU went to
  sleep or woke up. Run it in Balanced. A message that wakes the GPU takes
  about 2 seconds. One that leaves it asleep takes microseconds.

To install the fix without logging out, run `sudo ./build.sh`, then
`sudo ./swap.sh --check`, then `sudo ./swap.sh`.

`swap.sh` needs gdb, and Ubuntu's `python3-gi` for `reapply-layout.py`, which
it runs. `build.sh` needs git to download NVIDIA's source from GitHub.

The NVIDIA driver is four kernel modules (`nvidia`, `nvidia_modeset`,
`nvidia_drm` and `nvidia_uvm`) plus the libraries that programs load to use
the GPU. Ubuntu's driver package compiles the modules on the machine with
DKMS, the system that builds kernel modules for each installed kernel, except
the driver core, which it ships precompiled. `build.sh` compiles the core from
NVIDIA's source with the fix and puts it in place of Ubuntu's copy. A dpkg
diversion tells dpkg, Ubuntu's package manager, to keep Ubuntu's copy under
another name.

Tested on a Lenovo Legion 7 16IRX9 (RTX 4070 Laptop GPU) with Ubuntu 24.04,
kernel 7.0, GNOME 46 on Wayland and Ubuntu's packages of NVIDIA's open kernel
modules. On 2026-11-01 `swap.sh` replaced the stock 595.99.02 driver with the
patched build of the same version in 6.7 seconds, and later moved the same
session to 615.71.09 in 6.8 seconds. Both times gnome-shell, Xwayland and the
open programs kept running.

## Side effects of a swap

The kernel unloads the driver only after every program has closed the
driver's devices, so `swap.sh` first takes the GPU away from the programs that
have it open:

- Programs that don't run as root lose their access. `swap.sh` replaces their
  NVIDIA file handles with `/dev/null`, which pauses each program for 0.1 to 2
  seconds. They keep running but can't use the NVIDIA GPU until they restart.
  This includes gnome-shell and Xwayland. On the Legion the built-in screen
  runs on the Intel GPU and keeps working, but until the next login GNOME
  can't use a monitor on the HDMI port or on the DisplayPort/USB-C port wired
  to the NVIDIA GPU. Plugging one in may crash gnome-shell, which ends the
  session.
- `lactd` (LACT's daemon), `nvidia-persistenced` and `nvidia-powerd` are
  stopped for the swap if they have the GPU open, and started again
  afterwards.
- GNOME keeps the GPU's display device, `/dev/dri/cardN`, open while every
  monitor runs on the Intel GPU, and closes it only the next time it sets up
  the screens. `swap.sh` makes it do that by applying the current
  screen layout again, with the same resolution, refresh rate and scaling, and
  waits up to 10 seconds. GNOME opens the new display device right after the
  swap, so every later swap repeats this step.
- During the swap, NVIDIA's EGL and Vulkan registration files are renamed, so
  that programs started meanwhile don't open the GPU.

## Limitations

`swap.sh` refuses to swap, before it renames files, stops services or takes
away any program's access, when:

- A monitor is connected to the NVIDIA GPU. That includes the built-in screen
  when the laptop is set to drive it from the NVIDIA GPU.
- Something other than gnome-shell holds the display device, or GNOME hasn't
  closed it 10 seconds after the layout was applied again. systemd and
  systemd-logind hold copies of gnome-shell's handle, and `swap.sh` accepts
  those.
- A root process other than the three services above has the GPU open.
  `swap.sh --check` names it. `swap.sh` does not stop it, because that could
  end someone's work.
- A registration file is still renamed from an interrupted earlier swap.
  `swap.sh --check` names it. Remove its `.hotswap` suffix before the next
  swap.

Swaps have to be at least 20 seconds apart. Each swap makes GNOME ask
rtkit-daemon, the service that hands out real-time priority, several times to
change its display thread's priority, and rtkit-daemon accepts 25 such
requests per user in 20 seconds. `swap.sh` waits out the rest, and
`swap.sh --check` shows how long.

If a swap fails or is interrupted, `swap.sh` loads back from disk any NVIDIA
module that was loaded before and is now missing, renames the files back,
restarts the stopped services and exits with an error. The modules come from
disk, so they can be a different build from the ones it unloaded. Programs
that lost their access stay without the GPU.

A new driver version also brings new NVIDIA libraries, and those refuse
kernel modules of another version. Programs started between installing the
new packages and the swap can't use the GPU. Programs started before keep the
old libraries until they restart, gnome-shell until the next login.

## Driver and kernel updates

A new NVIDIA driver version comes with Ubuntu's unpatched driver core in its
own `/usr/src/nvidia-<version>`, so the GPU starts waking again until
`sudo ./build.sh` has run for that version.

A kernel update keeps the fix. DKMS builds the new kernel's modules from
`/usr/src/nvidia-<version>`, which has the patched core as long as the dpkg
diversion is in place.

## Changing the driver version

This is how the running session moved from 595.99.02 to 615.71.09 on
2026-11-01:

1. `sudo ./swap.sh --check`.
2. Put Ubuntu's driver core of the installed version back and remove its
   diversion, with the two commands under "Restoring Ubuntu's driver core",
   so that apt can remove the old package cleanly.
3. `sudo apt-get install nvidia-driver-<branch>-open`. apt replaces the old
   packages but keeps the old firmware package, marked for autoremove. Leave
   it until after the swap. The old driver is still running, and it reads its
   firmware from that package whenever a program opens the GPU after all
   others have closed it.
4. Ubuntu's DKMS hook builds the new version only for the running kernel. For
   each other kernel that should keep an NVIDIA driver, run
   `sudo dkms install -m nvidia -v <version> -k <kernel>`.
5. `sudo ./build.sh <version>`.
6. `sudo ./swap.sh`.
7. `sudo apt-get remove nvidia-firmware-<old branch>-<old version>`.
8. As the desktop user, check that a new program renders with the new driver:

   ```
   cc -std=c11 -Wall -Wextra -Werror -O2 tests/check_egl.c -o /tmp/check-nvidia-egl $(pkg-config --cflags --libs egl glesv2)
   /tmp/check-nvidia-egl
   ```

For 615.71.09, apt replaced 20 NVIDIA packages with 21, including the 32-bit
libraries. Ubuntu's `nvidia-dkms-615-open` adds two build fixes for 7.0
kernels. They touch only the code between the driver and the kernel, not the
driver core that `build.sh` replaces. The swap only had to stop `lactd`,
because the earlier swap had already taken the GPU from everything else.

## Restoring Ubuntu's driver core

`build.sh` installs the patched core as
`/usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary` and keeps Ubuntu's copy
as `nv-kernel.o_binary.ubuntu` through a dpkg diversion. Before apt removes a
driver version that has the patched core, put Ubuntu's copy back, so that the
package removes its files cleanly. As root:

    rm /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary
    dpkg-divert --rename --remove /usr/src/nvidia-<version>/nvidia/nv-kernel.o_binary

To remove the fix itself, run the same two commands, then for each kernel:

    dkms build -m nvidia -v <version> -k <kernel> --force
    dkms install -m nvidia -v <version> -k <kernel> --force

Without `--force`, `dkms build` skips a kernel whose modules are already
built. The modules without the fix load at the next boot.

## Technical details

[WRITEUP.md](WRITEUP.md) covers the firmware and driver code behind the
problem, the two patches, the measurements, and how each script and test
works.
