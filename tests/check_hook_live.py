#!/usr/bin/python3
"""Check nvpcf_hook on the Legion's GPU. Run as root.

usage: check_hook_live.py [--user NAME]
       check_hook_live.py --at-next-boot [--user NAME]

The check records the kernel, how the boot started, the end of the previous
boot's journal, the loaded NVIDIA driver, the run of nvpcf-hook.service and
the order of the boot's NVIDIA events. It waits up to ten minutes for the hook
to become active, and ends there when the hook does not. Then it switches the
platform profile to balanced, waits for the GPU to suspend, traces the
firmware's notifications for 60 seconds with trace.sh, and wakes the GPU once
with tests/check_egl.c, which it runs as the desktop user. When the GPU wakes
during a trace and every call of nv_acpi_nvpcf_event returned within 100 ms,
something other than the firmware's notifications woke it, and the check
traces again, up to three times in all. When the check ends, also after a
failed step or a signal, it switches the profile back, unless the profile is
no longer balanced. A criterion prints PASS or FAIL, a fact prints INFO, and
the check exits with status 1 when a criterion fails. --user names the desktop
user, by default the owner of the repository.

--at-next-boot copies the check, tests/check_egl.c, trace.sh, lib.sh and
hook/nvpcf-hookctl to /usr/local/lib/nvpcf-hook/live-check, owned by root, and
installs nvpcf-hook-live-check.service, which runs that copy after the next
boot. So the run at boot is the code that was scheduled, and root does not run
files that the desktop user can change. That run disables the service before
it starts checking, so that a crash or a hang during the check does not repeat
it at the following boot. When a signal ends the run, for example at a
shutdown, it enables the service again, so that the check runs at the
following boot. Otherwise it removes the service and the copy at the end. The
run also requires Ubuntu's unpatched driver core, the installed nvidia module
and a run of nvpcf-hook.service, which a run started by hand only reports.
Before it waits for the hook, it waits up to 30 minutes for the desktop user's
graphical session and then one more minute. The hook stays waiting until a
program initializes the GPU, which at boot can be the login, and the login
must not wake the GPU during a trace. At the end the run sends the desktop
user a notification with the result.
journalctl -t nvpcf-hook-live-check shows its output."""
import argparse
import glob
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

REPO = Path(__file__).resolve().parent.parent
UNIT = 'nvpcf-hook-live-check.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
LOG_ID = 'nvpcf-hook-live-check'
# --at-next-boot copies these files of the repository to LIVE_DIR, with the
# same layout, and the unit runs the copy.
LIVE_DIR = Path('/usr/local/lib/nvpcf-hook/live-check')
LIVE_FILES = ('tests/check_hook_live.py', 'tests/check_egl.c', 'trace.sh', 'lib.sh', 'hook/nvpcf-hookctl')
HOOKCTL = '/usr/local/lib/nvpcf-hook/nvpcf-hookctl'
LOADER = '/usr/local/lib/nvpcf-hook/load-hook.sh'
PATCH_SYMBOLS = ('RmDeferNvpcfNotifyIfIdle', 'RmReplayNvpcfNotify')
KPROBES = '/sys/kernel/debug/kprobes/list'
TRACES = 3
# A call that the hook holds takes microseconds. One that wakes the GPU takes
# about 2 seconds, and one that the driver handles with the GPU awake takes
# milliseconds at most.
HELD_US = 1000
WAKING_US = 100000
ERROR_FIELDS = ('dispatch_failures', 'observer_missed', 'power_failures', 'delivery_failures',
                'acknowledged_loss', 'stale_pending')

failed, passed = [], []


class Stopped(Exception):
    """A signal ended the check."""


def stop(signum, frame):
    raise Stopped(signal.Signals(signum).name)


def log(kind, text):
    print(f"{time.strftime('%H:%M:%S')} {kind} {text}", flush=True)


def info(text):
    log('INFO', text)


def result(ok, text):
    (passed if ok else failed).append(text)
    log('PASS' if ok else 'FAIL', text)


def run(args, timeout=120, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def run_child(args, timeout, **kw):
    """Run a command that cleans up after itself on SIGTERM. When the check
    ends early, it sends the command SIGTERM and waits for it, where
    subprocess.run would send SIGKILL. The command runs in its own session, so
    that a Ctrl-C in the terminal reaches only the check, and the unit has
    KillMode=mixed, so that systemd's SIGTERM reaches only the check. A second
    SIGTERM could end the command's cleanup, which resets its traps first."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=True, **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except BaseException:
        p.terminate()
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        raise


def journal(*args):
    return run(['journalctl', '--no-pager', '-q', *args], timeout=300).stdout.splitlines()


def gpu_device():
    for d in sorted(glob.glob('/sys/bus/pci/devices/*')):
        try:
            if (Path(d, 'vendor').read_text().strip() == '0x10de'
                    and Path(d, 'class').read_text().startswith('0x03')):
                return d
        except OSError:
            pass
    return None


def runtime_status(gpu):
    return Path(gpu, 'power/runtime_status').read_text().strip()


def hook_loaded():
    return any(line.startswith('nvpcf_hook ') for line in Path('/proc/modules').read_text().splitlines())


def hook_status():
    """The hook's status fields, or None when it is not loaded or its status
    cannot be read."""
    if not hook_loaded():
        return None
    p = run([HOOKCTL, 'status'])
    if p.returncode != 0:
        info(f'nvpcf-hookctl status failed: {p.stdout.strip()} {p.stderr.strip()}')
        return None
    return dict(line.split(': ', 1) for line in p.stdout.splitlines() if ': ' in line)


def field(st, key):
    try:
        return int((st or {}).get(key, ''))
    except ValueError:
        return None


def delta(before, after, key):
    a, b = field(after, key), field(before, key)
    return None if a is None or b is None else a - b


def summary(st):
    if st is None:
        return 'nvpcf_hook is not loaded' if not hook_loaded() else 'the status of nvpcf_hook cannot be read'
    keys = ('state', 'build_id_verified', 'guards_registered', 'initialization_blocked', 'retirement_failed',
            'received_requests', 'deferred_requests', 'pending_requests', 'requests_dispatched',
            'dispatch_batches', 'dispatch_attempts', *ERROR_FIELDS, 'last_error')
    return ', '.join(f'{k} {st.get(k)}' for k in keys)


def kprobe_line():
    try:
        lines = Path(KPROBES).read_text().splitlines()
    except OSError as e:
        return f'unreadable: {e}'
    found = [line for line in lines if 'gpuNotifySubDeviceEvent_IMPL' in line]
    return found[0].strip() if found else None


def check_kprobe(when):
    line = kprobe_line()
    result(bool(line) and '[DISABLED]' not in line and not line.startswith('unreadable'),
           f'{when}, the kprobe on gpuNotifySubDeviceEvent_IMPL is registered and armed: {line or "not listed"}')


def gpu_holders():
    holders = set()
    for fd in glob.glob('/proc/[0-9]*/fd/*'):
        try:
            if os.readlink(fd).startswith('/dev/nvidia'):
                pid = fd.split('/')[2]
                holders.add(f"{pid} {Path('/proc', pid, 'comm').read_text().strip()}")
        except OSError:
            pass
    return ', '.join(sorted(holders, key=lambda s: int(s.split()[0]))) or 'none'


def report_boot():
    info(f'kernel {os.uname().release}')
    lines = journal('-b', '0', '-k', '-o', 'short-monotonic')
    resume = [line for line in lines
              if re.search(r'PM: Image(?! loading progress)|PM: hibernation: resume|Failed to load image'
                           r'|PM: .*returns -\d+', line)]
    if resume:
        for line in resume[:10]:
            info(f'this boot, resume: {line}')
    else:
        info('the kernel log of this boot does not mention a hibernation image')
    try:
        boots = {b['index']: b for b in json.loads(run(['journalctl', '--list-boots', '-o', 'json']).stdout)}
        last, first = boots[-1]['last_entry'] / 1e6, boots[0]['first_entry'] / 1e6
    except (ValueError, KeyError, TypeError):
        info('the journal does not list a previous boot')
        return
    for line in journal('-b', '-1', '-n', '12', '-o', 'short-iso'):
        info(f'previous boot, end: {line}')
    info(f'{first - last:.0f} s passed between the last journal entry of the previous boot and the first of this one')
    bad = [line for line in journal('-b', '-1', '-k', '-o', 'short-iso', '--since', f'@{int(last) - 600}')
           if re.search(r'nvpcf_hook|NVRM|WARNING|BUG:|Oops|blocked for more than|hung_task', line)]
    info(f'previous boot, kernel lines about the hook, NVIDIA or warnings in its last 10 minutes: {len(bad)}')
    for line in bad[-20:]:
        info(f'previous boot: {line}')


def report_driver(boot_run):
    version = Path('/proc/driver/nvidia/version')
    info(version.read_text().splitlines()[0] if version.exists() else 'the nvidia module is not loaded')
    names = set()
    for line in Path('/proc/kallsyms').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in PATCH_SYMBOLS:
            names.add(parts[2])
    loaded = run([LOADER, '--loaded-build-id', 'nvidia']).stdout.strip()
    f = run(['modinfo', '-n', 'nvidia']).stdout.strip()
    disk = run([LOADER, '--build-id', f]).stdout.strip() if f else ''
    ids = (f'loaded nvidia has build ID {loaded or "unreadable"}; modprobe finds {f or "no module"}, '
           f'build ID {disk or "unreadable"}')
    if boot_run:
        result(not names, "the loaded NVIDIA driver core is Ubuntu's: kallsyms has neither "
               + ' nor '.join(PATCH_SYMBOLS) + (f', found {sorted(names)}' if names else ''))
        result(bool(loaded) and loaded == disk, f'the loaded nvidia is the installed module: {ids}')
    else:
        info('the loaded NVIDIA driver core ' + (f'has patch 0001 ({", ".join(sorted(names))} in kallsyms)' if names
                                                 else "is Ubuntu's, without patch 0001's functions in kallsyms"))
        info(ids)


def report_unit(boot_run):
    info(f"nvpcf-hook.service is {run(['systemctl', 'is-enabled', 'nvpcf-hook.service']).stdout.strip()}")
    keys = 'ActiveState,SubState,Result,ExecMainStatus,ExecMainStartTimestampMonotonic'
    deadline = time.monotonic() + 600
    while True:
        st = dict(line.split('=', 1) for line in
                  run(['systemctl', 'show', '-p', keys, 'nvpcf-hook.service']).stdout.splitlines() if '=' in line)
        if st.get('ActiveState') != 'activating' or time.monotonic() > deadline:
            break
        time.sleep(5)
    if st.get('ExecMainStartTimestampMonotonic', '0') not in ('', '0'):
        result(st.get('Result') == 'success' and st.get('ExecMainStatus') == '0',
               f"nvpcf-hook.service ran the loader: {st.get('ActiveState')} ({st.get('SubState')}), "
               f"result {st.get('Result')}, exit status {st.get('ExecMainStatus')}")
    elif boot_run:
        result(False, 'nvpcf-hook.service has not run in this boot')
    else:
        info('nvpcf-hook.service has not run in this boot')
    for line in journal('-b', '0', '-u', 'nvpcf-hook.service', '-o', 'short-monotonic'):
        info(f'loader: {line}')
    info(f'hook status after the unit: {summary(hook_status())}')


def report_events():
    pattern = re.compile(r'NVRM: loading|Initialized nvidia-drm|nvpcf-hook\.service|nvpcf_hook: |gdm\.service - '
                         r'|lactd\.service - |Reached target graphical\.target|New session \S+ of user')
    for line in journal('-b', '0', '-o', 'short-monotonic'):
        # The check's own lines name nvpcf-hook.service too.
        if pattern.search(line) and f' {LOG_ID}[' not in line:
            info(f'event: {line}')


def wait_active(seconds):
    deadline = time.monotonic() + seconds
    st = hook_status()
    while (st or {}).get('state') != 'active' and time.monotonic() < deadline and hook_loaded():
        time.sleep(5)
        st = hook_status()
    return st


def wait_session(user, timeout=1800, settle=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = run(['loginctl', 'show-user', user, '-p', 'Display', '--value'])
        if p.returncode == 0 and p.stdout.strip():
            info(f'{user} has the graphical session {p.stdout.strip()}; the check waits {settle} s more')
            time.sleep(settle)
            return
        time.sleep(10)
    info(f'{user} did not log in to a graphical session within {timeout // 60} minutes; the check goes on')


def wait_suspended(gpu, hold=15, timeout=300):
    start, since = time.monotonic(), None
    while time.monotonic() - start < timeout:
        now = time.monotonic()
        if runtime_status(gpu) == 'suspended':
            since = since if since is not None else now
            if now - since >= hold:
                return True
        else:
            since = None
        time.sleep(0.2)
    return False


class Profile:
    """Switches the platform profile to balanced and back."""

    def __init__(self):
        self.old = None

    def balanced(self):
        old = run(['powerprofilesctl', 'get']).stdout.strip()
        if old == 'balanced':
            info('the platform profile is balanced already')
            return True
        p = run(['powerprofilesctl', 'set', 'balanced'])
        if p.returncode != 0:
            result(False, f'could not switch the platform profile from {old} to balanced: {p.stderr.strip()}')
            return False
        self.old = old
        info(f'switched the platform profile from {old} to balanced')
        return True

    def restore(self):
        old, self.old = self.old, None
        if not old:
            return
        now = run(['powerprofilesctl', 'get']).stdout.strip()
        if now != 'balanced':
            info(f'the platform profile is {now} now, so the check does not switch it back to {old}')
            return
        p = run(['powerprofilesctl', 'set', old])
        info(f'switched the platform profile back to {old}'
             + (f', which failed: {p.stderr.strip()}' if p.returncode else ''))


def parse_trace(out):
    """The number of EC queries 0x48, the durations of the calls of
    nv_acpi_nvpcf_event, the call records without a duration, and the GPU's
    states in the output of trace.sh."""
    queries = len(re.findall(r'Query\(0x48\)', out))
    records, inside = [], False
    for line in out.splitlines():
        if line.startswith('--- calls of nv_acpi_nvpcf_event'):
            inside = True
        elif re.fullmatch(r'\d+ calls of nv_acpi_nvpcf_event', line):
            inside = False
        elif inside:
            records.append(line)
    durations, unparsed = [], []
    for r in records:
        m = re.search(r'([\d.]+) us\s+\|', r)
        if m:
            durations.append(float(m.group(1)))
        else:
            unparsed.append(r)
    states = re.findall(r'^\d\d:\d\d:\d\d\.\d+ gpu (\S+)$', out, re.M)
    return queries, durations, unparsed, states


def trace(gpu):
    """Trace until the GPU stays suspended through a trace or a call of
    nv_acpi_nvpcf_event takes long enough to have woken it. Returns the hook's
    status after the last trace, or None when the check did not trace."""
    for attempt in range(1, TRACES + 1):
        if not wait_suspended(gpu):
            result(False, 'the GPU did not stay suspended for 15 s within 300 s, so the check does not trace; '
                   f'processes with the NVIDIA device files open: {gpu_holders()}')
            return None
        info(f'trace {attempt} of at most {TRACES}: the GPU stayed suspended for 15 s')
        before = hook_status()
        info(f'before the trace: {summary(before)}')
        code, out = run_child(['/bin/bash', str(REPO / 'trace.sh'), '60'], timeout=240, cwd=str(REPO))
        for line in out.splitlines():
            info(f'trace: {line}')
        after = hook_status()
        info(f'after the trace: {summary(after)}')
        if code != 0:
            result(False, f'trace.sh exited with status {code}')
            return after
        queries, durations, unparsed, states = parse_trace(out)
        stayed = bool(states) and all(s == 'suspended' for s in states)
        waking = bool(unparsed) or any(d >= WAKING_US for d in durations)
        if not stayed and not waking and attempt < TRACES:
            info(f'the GPU woke during trace {attempt}, and every call of nv_acpi_nvpcf_event returned within '
                 f'{WAKING_US // 1000} ms, so something other than the firmware\'s notifications woke it; '
                 f'processes with the NVIDIA device files open: {gpu_holders()}')
            continue
        result(queries > 0, f'the firmware raised query 0x48 {queries} times in 60 s of balanced')
        result(bool(durations) and not unparsed and max(durations) < HELD_US,
               f'each of the {len(durations) + len(unparsed)} calls of nv_acpi_nvpcf_event returned within '
               f'{HELD_US} us; the longest took {max(durations, default=0):.1f} us'
               + (f', and {len(unparsed)} records have no duration: {unparsed}' if unparsed else ''))
        result(stayed, f'the GPU stayed suspended through the trace, states {states}')
        if not stayed:
            info(f'processes with the NVIDIA device files open: {gpu_holders()}')
        received = delta(before, after, 'received_requests')
        result(received is not None and received >= queries,
               f'received_requests grew by {received} for {queries} notifications')
        attempts = delta(before, after, 'dispatch_attempts')
        if stayed:
            result(attempts == 0, f'dispatch_attempts grew by {attempts} while the GPU slept')
        else:
            info(f'dispatch_attempts grew by {attempts} during the trace')
        return after
    return None


def wake(gpu, user, before):
    tmp = tempfile.mkdtemp(prefix='nvpcf-live-check-')
    transitions, stop_watch = [], threading.Event()
    try:
        os.chmod(tmp, 0o755)
        exe = os.path.join(tmp, 'check-nvidia-egl')
        flags = run(['pkg-config', '--cflags', '--libs', 'egl', 'glesv2']).stdout.split()
        c = run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2', str(REPO / 'tests/check_egl.c'),
                 '-o', exe, *flags])
        if c.returncode != 0:
            result(False, f'could not build tests/check_egl.c: {c.stdout.strip()} {c.stderr.strip()}')
            return
        os.chmod(exe, 0o755)
        t0 = time.monotonic()

        def watch():
            last = None
            while not stop_watch.is_set():
                s = runtime_status(gpu)
                if s != last:
                    transitions.append((round(time.monotonic() - t0, 2), s))
                    last = s
                time.sleep(0.02)
        threading.Thread(target=watch, daemon=True).start()
        time.sleep(0.5)
        p = run(['runuser', '-u', user, '--', exe], timeout=120)
        result(p.returncode == 0, f'check_egl rendered on the NVIDIA GPU as {user}: '
               f'{(p.stdout.strip().splitlines() or p.stderr.strip().splitlines() or ["no output"])[-1]}')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            active = [t for t, s in transitions if s == 'active']
            if active and any(t > active[0] and s == 'suspended' for t, s in transitions):
                break
            time.sleep(0.2)
        time.sleep(1)
    finally:
        stop_watch.set()
        shutil.rmtree(tmp, ignore_errors=True)
    info(f'GPU states after the wake started (s, state): {transitions}')
    active = [t for t, s in transitions if s == 'active']
    again = [t for t, s in transitions if active and t > active[0] and s == 'suspended']
    result(bool(again) and again[0] - active[0] <= 90,
           'the GPU became active and suspended again '
           + (f'{again[0] - active[0]:.1f} s later' if again else 'within 120 s' if active else 'after it woke'))
    after = hook_status()
    info(f'after the wake: {summary(after)}')
    held = field(before, 'pending_requests')
    delivered = delta(before, after, 'requests_dispatched')
    result(held is not None and delivered is not None and delivered >= held,
           f'the wake delivered the {held} requests that the hook held: requests_dispatched grew by {delivered}')
    result(after is not None and all(field(after, k) == 0 for k in ERROR_FIELDS),
           ', '.join(f'{k} {(after or {}).get(k)}' for k in ERROR_FIELDS))
    info(f"pending_requests after the GPU slept again: {(after or {}).get('pending_requests')}")
    check_kprobe('after the wake')


def check(args, profile):
    info(f'the check runs from {REPO}')
    report_boot()
    report_driver(args.boot_run)
    report_unit(args.boot_run)
    report_events()
    gpu = gpu_device()
    if gpu is None:
        result(False, 'there is no NVIDIA display device in /sys/bus/pci/devices')
        return
    if args.boot_run:
        wait_session(args.user)
    st = wait_active(600)
    result((st or {}).get('state') == 'active', f'nvpcf_hook is active: {summary(st)}')
    if (st or {}).get('state') != 'active':
        return
    check_kprobe('before the trace')
    if not profile.balanced():
        return
    after = trace(gpu)
    if after is not None:
        wake(gpu, args.user, after)


def disable_trigger():
    """Remove the unit's link in graphical.target.wants, so that a crash or a
    hang during the check does not run it again at the following boot. The
    unit file stays until the end of the run: after a daemon-reload without
    the file, systemd would load the running unit again with the default
    KillMode, and a stop would then send SIGTERM to trace.sh too."""
    p = run(['systemctl', 'disable', '--no-reload', UNIT], timeout=30)
    info(f'disabled {UNIT}, so that the check does not run again at the next boot'
         + (f'; systemctl disable: {p.stderr.strip()}' if p.returncode else ''))


def enable_trigger():
    p = run(['systemctl', 'enable', '--no-reload', UNIT], timeout=30)
    info(f'enabled {UNIT} again, so that the check runs at the next boot' if p.returncode == 0
         else f'could not enable {UNIT} again: {p.stderr.strip()}')


def remove_trigger():
    try:
        UNIT_PATH.unlink()
    except FileNotFoundError:
        pass
    copy = REPO == LIVE_DIR
    if copy:
        shutil.rmtree(LIVE_DIR, ignore_errors=True)
    r = run(['systemctl', 'daemon-reload'], timeout=30)
    info(f'removed {UNIT_PATH}' + (f' and {LIVE_DIR}' if copy else '')
         + (f'; systemctl daemon-reload: {r.stderr.strip()}' if r.returncode else ''))


def end_boot_run(user, stopped):
    """Send the result and remove the unit and the copy, or, when a signal
    ended the check, enable the unit again, so that the check runs at the next
    boot."""
    if stopped is None:
        text = (f'{len(failed)} of {len(passed) + len(failed)} checks failed.' if failed
                else f'All {len(passed)} checks passed.')
        try:
            notify(user, text + ' journalctl -t nvpcf-hook-live-check shows the details.',
                   'critical' if failed else 'normal')
        except Stopped as e:
            info(f'received {e} while it waited to send the notification')
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, signal.SIG_IGN)
    if stopped is None:
        remove_trigger()
    else:
        enable_trigger()


def notify(user, text, urgency):
    uid = pwd.getpwnam(user).pw_uid
    bus = f'/run/user/{uid}/bus'
    deadline = time.monotonic() + 1800
    while not os.path.exists(bus) and time.monotonic() < deadline:
        time.sleep(10)
    if not os.path.exists(bus):
        info(f'{user} did not log in within 30 minutes, so the check does not send a notification')
        return
    p = run(['runuser', '-u', user, '--', 'env', f'DBUS_SESSION_BUS_ADDRESS=unix:path={bus}',
             'notify-send', '-u', urgency, '-a', 'nvpcf_hook check', 'nvpcf_hook check after boot', text])
    info('sent the notification' if p.returncode == 0 else f'notify-send failed: {p.stderr.strip()}')


def schedule(user):
    if REPO == LIVE_DIR:
        sys.exit(f'run --at-next-boot from the repository; {LIVE_DIR} holds the copy that it installs')
    for f in (HOOKCTL, LOADER):
        if not os.access(f, os.X_OK):
            sys.exit(f'{f} is missing; install the hook with install-hook.sh first')
    shutil.rmtree(LIVE_DIR, ignore_errors=True)
    for rel in LIVE_FILES:
        src, dst = REPO / rel, LIVE_DIR / rel
        for d in reversed(dst.relative_to(LIVE_DIR.parent).parents[:-1]):
            (LIVE_DIR.parent / d).mkdir(exist_ok=True)
            (LIVE_DIR.parent / d).chmod(0o755)
        shutil.copyfile(src, dst)
        dst.chmod(0o755 if os.access(src, os.X_OK) else 0o644)
    UNIT_PATH.write_text(f"""[Unit]
Description=One run of nvidia-live-swap's check of nvpcf_hook after boot
After=nvpcf-hook.service graphical.target

[Service]
Type=exec
ExecStart=/usr/bin/python3 -I -B {LIVE_DIR / LIVE_FILES[0]} --boot-run --user {user}
SyslogIdentifier={LOG_ID}
KillMode=mixed

[Install]
WantedBy=graphical.target
""")
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', UNIT], check=True)
    hook = run(['systemctl', 'is-enabled', 'nvpcf-hook.service']).stdout.strip()
    print(f'{UNIT} runs the copy in {LIVE_DIR} once after the next boot; journalctl -t nvpcf-hook-live-check '
          f'shows its output. nvpcf-hook.service, which the check expects to load the hook at boot, is {hook}.')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--user', default=pwd.getpwuid(REPO.stat().st_uid).pw_name)
    ap.add_argument('--at-next-boot', action='store_true')
    ap.add_argument('--boot-run', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit('check_hook_live.py runs as root')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user):
        sys.exit(f'not a user name: {args.user!r}')
    pwd.getpwnam(args.user)
    if args.at_next_boot:
        schedule(args.user)
        return
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, stop)
    profile = Profile()
    stopped = None
    try:
        if args.boot_run:
            disable_trigger()
        check(args, profile)
    except Stopped as e:
        stopped = str(e)
        result(False, f'the check received {stopped} before it finished')
    except Exception:
        traceback.print_exc()
        result(False, 'the check stopped with an exception: ' + traceback.format_exc().strip().splitlines()[-1])
    finally:
        profile.restore()
        info(f'{len(passed)} criteria passed and {len(failed)} failed')
        for text in failed:
            info(f'failed: {text}')
        if args.boot_run:
            end_boot_run(args.user, stopped)


if __name__ == '__main__':
    main()
    sys.exit(1 if failed else 0)
