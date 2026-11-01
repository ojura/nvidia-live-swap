#!/usr/bin/env python3
# Revoke a process's handles on device nodes, so the kernel module behind
# them can be unloaded while the process keeps running.
#
# usage (as root): revoke.py [--dry-run] <pid> <path> [<path> ...]
#
# Every file descriptor the process has open on one of the paths is replaced
# with /dev/null through dup2 from the process's own fd 0, which must be
# /dev/null. The number stays taken, so a later ioctl on it fails instead of
# reaching some other file. Every mapping of one of the paths is replaced in
# place with anonymous zeroed memory of the same size and protection, so code
# that still holds a pointer into it reads zeros instead of faulting.
#
# The replacement runs inside gdb. Attaching stops every thread of the
# process, and with scheduler locking on, an injected call runs only the
# thread gdb selected while the others stay stopped; without it, gdb resumes
# every thread for the duration of each call. The descriptors and mappings
# are read from /proc after the attach, so no thread can open, close or map
# anything between the reading and the last call. The injected calls are
# dup2, mmap and __errno_location: system-call wrappers and a thread-local
# read that take no locks, so they cannot wait for a lock held by a stopped
# thread. Each call's result is checked before the next; if a call fails,
# the calls before it stay in effect and the script exits 1. gdb loads only
# libc's symbols, which keeps the stop short.
#
# Refuses PID 1, processes whose real user is root, and processes whose fd 0
# is not /dev/null.
#
# gdb runs this same file (gdb -x); the code at the bottom chooses between
# the command-line entry point and the part that runs inside gdb.
import json, os, re, subprocess, sys

MAP_FIXED, MAP_PRIVATE, MAP_ANONYMOUS = 0x10, 0x02, 0x20


def read_fds(pid, paths):
    out = []
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if target in paths:
            out.append((int(fd), target))
    return sorted(out)


def read_maps(pid, paths):
    out = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            fields = line.rstrip("\n").split(maxsplit=5)
            if len(fields) < 6 or fields[5] not in paths:
                continue
            start, end = (int(x, 16) for x in fields[0].split("-"))
            perms = fields[1]
            prot = ((1 if perms[0] == "r" else 0) | (2 if perms[1] == "w" else 0)
                    | (4 if perms[2] == "x" else 0))
            out.append((start, end - start, prot, fields[5]))
    return out


def inside_gdb(gdb):
    args = json.loads(os.environ["REVOKE_ARGS"])
    pid, paths = args["pid"], set(args["paths"])
    result = {"fds": [], "maps": [], "error": None}

    def errno():
        return int(gdb.parse_and_eval("*(int *)__errno_location()"))

    attached = False
    try:
        gdb.execute(f"attach {pid}", to_string=True)
        attached = True
        gdb.execute("set scheduler-locking on", to_string=True)
        if gdb.parameter("scheduler-locking") != "on":
            raise RuntimeError("could not turn scheduler locking on")
        result["scheduler_locking"] = gdb.parameter("scheduler-locking")
        gdb.execute("sharedlibrary libc.so.6", to_string=True)
        if os.readlink(f"/proc/{pid}/fd/0") != "/dev/null":
            raise RuntimeError("fd 0 is not /dev/null")
        for fd, target in read_fds(pid, paths):
            r = int(gdb.parse_and_eval(f"(int)dup2(0, {fd})"))
            if r != fd:
                raise RuntimeError(f"dup2(0, {fd}) on {target} returned {r}, errno {errno()}")
            result["fds"].append([fd, target])
        flags = MAP_FIXED | MAP_PRIVATE | MAP_ANONYMOUS
        for start, length, prot, target in read_maps(pid, paths):
            r = int(gdb.parse_and_eval(
                f"(unsigned long)mmap({start:#x}, {length:#x}, {prot}, {flags:#x}, -1, 0)"))
            if r != start:
                raise RuntimeError(f"mmap over {target} at {start:#x} returned {r:#x}, errno {errno()}")
            result["maps"].append([start, length, target])
    except Exception as e:
        result["error"] = str(e)
    finally:
        if attached:
            gdb.execute("detach", to_string=True)
    print("REVOKE-RESULT " + json.dumps(result), flush=True)


def real_uid(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("Uid:"):
                return int(line.split()[1])
    raise RuntimeError(f"no Uid line for {pid}")


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if len(argv) < 2:
        sys.exit("usage: revoke.py [--dry-run] <pid> <path> [<path> ...]")
    pid, paths = int(argv[0]), sorted(set(argv[1:]))
    comm = open(f"/proc/{pid}/comm").read().strip()
    if pid == 1:
        sys.exit(f"{pid} {comm}: refusing PID 1")
    if real_uid(pid) == 0:
        sys.exit(f"{pid} {comm}: refusing a process owned by root")
    if os.readlink(f"/proc/{pid}/fd/0") != "/dev/null":
        sys.exit(f"{pid} {comm}: refusing, fd 0 is not /dev/null")

    fds, maps = read_fds(pid, paths), read_maps(pid, paths)
    print(f"{pid} {comm}: {len(fds)} descriptors and {len(maps)} mappings on {' '.join(paths)}")
    if dry:
        for fd, target in fds:
            print(f"  fd {fd} -> {target}")
        for start, length, prot, target in maps:
            print(f"  map {start:#x} +{length:#x} prot {prot} {target}")
        return
    if not fds and not maps:
        return

    env = dict(os.environ, REVOKE_ARGS=json.dumps({"pid": pid, "paths": paths}))
    cmd = ["gdb", "-batch", "-nx",
           "-iex", "set debuginfod enabled off",
           "-iex", "set auto-load off",
           "-iex", "set auto-solib-add off",
           "-iex", "set pagination off",
           "-iex", "set confirm off",
           "-x", os.path.abspath(__file__)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    m = re.search(r"^REVOKE-RESULT (.*)$", r.stdout, re.M)
    if not m:
        print(f"{pid} {comm}: gdb did not report a result")
        print(r.stdout[-3000:], r.stderr[-3000:])
        sys.exit(1)
    res = json.loads(m.group(1))
    print(f"{pid} {comm}: replaced {len(res['fds'])} descriptors and {len(res['maps'])} mappings"
          f" (scheduler locking {res.get('scheduler_locking', 'not set')})")
    if res["error"]:
        print(f"{pid} {comm}: stopped at an error: {res['error']}")
        sys.exit(1)
    left_fds, left_maps = read_fds(pid, paths), read_maps(pid, paths)
    if left_fds or left_maps:
        print(f"{pid} {comm}: {len(left_fds)} descriptors and {len(left_maps)} mappings still open")
        sys.exit(1)


try:
    import gdb  # only importable when gdb runs this file
except ImportError:
    gdb = None

if gdb is None:
    main()
else:
    inside_gdb(gdb)
