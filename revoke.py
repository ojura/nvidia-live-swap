# Revoke a process's handles on the NVIDIA GPU so the NVIDIA kernel modules
# can be unloaded while the process keeps running.
#
# Each file descriptor open on an NVIDIA device node is replaced with
# /dev/null (the process's fd 0, checked first) through dup2, so the number
# stays taken and a later ioctl fails instead of reaching some other file.
# Each mapping of an NVIDIA device node is replaced in place with anonymous
# zeroed memory of the same size and protection, so code that still holds a
# pointer into it reads zeros instead of faulting.
#
# The calls run inside the process through gdb, with only libc's symbols
# loaded so the process is stopped for as short a time as possible.
#
# usage (as root): revoke.py <pid> [--dry-run]
import os, re, subprocess, sys

DEVICE = re.compile(r"^/dev/(nvidia(ctl|-modeset|-uvm|-uvm-tools|[0-9]+)|dri/(card0|renderD129))$")
MAP_FIXED, MAP_PRIVATE, MAP_ANONYMOUS = 0x10, 0x02, 0x20


def device_fds(pid):
    out = []
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if DEVICE.match(target):
            out.append((int(fd), target))
    return sorted(out)


def device_maps(pid):
    out = []
    for line in open(f"/proc/{pid}/maps"):
        f = line.split()
        if len(f) < 6 or not DEVICE.match(f[5]):
            continue
        start, end = (int(x, 16) for x in f[0].split("-"))
        perms = f[1]
        prot = (1 if perms[0] == "r" else 0) | (2 if perms[1] == "w" else 0) | (4 if perms[2] == "x" else 0)
        out.append((start, end - start, prot, f[5]))
    return out


def main():
    pid = int(sys.argv[1])
    dry = "--dry-run" in sys.argv
    comm = open(f"/proc/{pid}/comm").read().strip()
    if os.readlink(f"/proc/{pid}/fd/0") != "/dev/null":
        sys.exit(f"{pid} {comm}: fd 0 is not /dev/null, refusing")
    fds, maps = device_fds(pid), device_maps(pid)
    print(f"{pid} {comm}: {len(fds)} device fds, {len(maps)} device mappings")
    if not fds and not maps:
        return
    cmds = []
    for fd, target in fds:
        cmds.append(f"call (int)dup2(0, {fd})")
    flags = MAP_FIXED | MAP_PRIVATE | MAP_ANONYMOUS
    for start, length, prot, target in maps:
        cmds.append(f"call (unsigned long)mmap({start:#x}, {length:#x}, {prot}, {flags:#x}, -1, 0)")
    if dry:
        print("\n".join(cmds))
        return
    gdb = ["gdb", "-p", str(pid), "-batch", "-nx",
           "-iex", "set debuginfod enabled off",
           "-iex", "set auto-load off",
           "-iex", "set auto-solib-add off",
           "-iex", "set pagination off",
           "-iex", "set confirm off",
           "-ex", "sharedlibrary libc.so.6"]
    for c in cmds:
        gdb += ["-ex", c]
    gdb += ["-ex", "detach"]
    r = subprocess.run(gdb, capture_output=True, text=True, timeout=120)
    results = re.findall(r"^\$\d+ = (\S+)", r.stdout, re.M)
    expected = [str(fd) for fd, _ in fds] + [str(start) for start, *_ in maps]
    if results != expected:
        print("gdb output did not match the expected return values")
        print("expected:", expected)
        print("got:     ", results)
        print(r.stdout[-3000:], r.stderr[-3000:])
        sys.exit(1)
    left_fds, left_maps = device_fds(pid), device_maps(pid)
    print(f"{pid} {comm}: done; {len(left_fds)} device fds and {len(left_maps)} device mappings left")
    if left_fds or left_maps:
        sys.exit(1)


main()
