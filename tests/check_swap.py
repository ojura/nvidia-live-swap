#!/usr/bin/python3
"""Run swap.sh as an ordinary user, with mock commands and, for each case, a
throwaway directory in place of /sys, /proc and NVIDIA's EGL and Vulkan
registration files.

Every command that changes the system (mv, systemctl, runuser for
reapply-layout.py, setsid and python3 for revoke.py, rmmod, modprobe,
nvpcf-hookctl for anything but status, and load-hook.sh for anything but
--check-disk) is a mock. Most are placed first on PATH. nvpcf-hookctl is in the
copied scripts' hook directory, and load-hook.sh where swap.sh looks for the
installed copy. The mock load-hook.sh loads nvpcf_hook; r['modprobe'] lists
those loads as nvpcf_hook among the modprobe calls, and swap.sh must never
run modprobe nvpcf_hook itself. The test runs copies of
swap.sh and lib.sh, edited so that the files they read, write or rename under
/sys, /proc, /run and /usr/share are the ones in the throwaway directory.
swap.sh still looks in the real /dev, but only to test with [ -c ] which
NVIDIA device nodes exist.

The mock nvpcf-hookctl keeps the hook's state in the throwaway directory. Its
detach moves the state to guard-only. The mock rmmod of nvidia then plays
NVIDIA's unload notification, which moves a guard-only hook to detached. The
mock rmmod of nvpcf_hook refuses unless the hook is detached, as the pinned
module does. Every case starts with the hook loaded and active, unless it
says otherwise. A newly loaded hook is active at once, as when gnome-shell
opens the new display device, unless a case makes it wait. A waiting hook
becomes active when lactd.service starts and initializes the GPU.

Loading nvidia_modeset, nvidia_drm or nvidia_uvm, or starting a service, can
make NVIDIA initialize the GPU. The mock records each such step taken while
the hook does not guard NVIDIA's shutdown, and every case fails if it finds
one."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SRC = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix='swap-test-'))
HIDE = ['/usr/share/glvnd/egl_vendor.d/10_nvidia.json',
        '/usr/share/vulkan/implicit_layer.d/nvidia_layers.json',
        '/usr/share/vulkan/icd.d/nvidia_icd.json']
MODULES = ['nvidia', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm']
USER_SLICE = '/user.slice/user-1000.slice/user@1000.service/session.slice/org.gnome.Shell@wayland.service'
PROCS = {
    1: ('systemd', 0, '/init.scope'),
    2045: ('systemd-logind', 0, '/system.slice/systemd-logind.service'),
    4525: ('gnome-shell', 1000, USER_SLICE),
    5019: ('Xwayland', 1000, USER_SLICE),
    777: ('lact', 0, '/system.slice/lactd.service'),
    778: ('cuda-job', 0, '/system.slice/cuda-job.service'),
    999: ('rogue', 0, '/init.scope'),
}
OLD_ID, DISK_ID, STALE_ID = 'aa' * 20, 'bb' * 20, 'cc' * 20
MODIFYING = {'mv', 'systemctl', 'setsid', 'python3', 'rmmod', 'modprobe', 'runuser', 'nvpcf-hookctl', 'load-hook.sh'}

MOCK = r'''#!/usr/bin/python3 -I
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['MOCK_ROOT'])
cfg = json.loads((root / 'config.json').read_text())
state_path = root / 'state.json'
def load(): return json.loads(state_path.read_text())
def save(st): state_path.write_text(json.dumps(st))
cmd = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps([cmd, args]) + '\n')
mods = root / 'proc' / 'modules'
def loaded(): return [l.split()[0] for l in mods.read_text().splitlines() if l.strip()]
def set_loaded(ms): mods.write_text(''.join(f'{m} 1 0 - Live 0x0\n' for m in ms))
def pause():
    (root / 'paused').touch()
    time.sleep(30)
def guarded(st):
    return ('nvpcf_hook' in loaded() and st['hook_state'] in ('active', 'waiting')
            and st.get('hook_guards', 1) == 1)
def set_build_id(m, hexid):
    d = root / 'sys/module' / m / 'notes'
    d.mkdir(parents=True, exist_ok=True)
    (d / '.note.gnu.build-id').write_bytes(bytes.fromhex('040000001400000003000000474e5500' + hexid))
if cmd == 'mv':
    a, b = Path(args[0]), Path(args[1])
    assert a.is_relative_to(root / 'files') and b.is_relative_to(root / 'files'), args
    if cfg.get('fail_hide') and str(b).endswith('.hotswap'):
        sys.exit(1)
    try:
        a.rename(b)
    except FileNotFoundError:
        sys.exit(1)
elif cmd == 'systemctl':
    st = load()
    action, unit = args[0], args[1]
    st['active'][unit] = action == 'start'
    if action == 'stop':
        gone = cfg.get('service_holders', {}).get(unit, [])
        st['holders'] = [p for p in st['holders'] if p not in gone]
    if action == 'start' and cfg.get('start_fails'):
        sys.exit(1)
    if action == 'start' and not guarded(st):
        st['unguarded'].append(f'systemctl start {unit}')
    if (action == 'start' and 'nvpcf_hook' in loaded() and st['hook_state'] == 'waiting'
            and not cfg.get('hook_stays_waiting')):
        st['hook_state'] = 'active'
    save(st)
    if action == 'stop' and cfg.get('pause_after_stop'):
        pause()
elif cmd == 'load-hook.sh':
    if args and args[0] == '--check-disk':
        if cfg.get('disk_check') == 'reject':
            print('load-hook.sh: check_nvidia.py rejects NVIDIA 615.71.09')
            sys.exit(1)
        if cfg.get('disk_check') == 'uncached' and '--no-download' in args:
            print("load-hook.sh: NVIDIA's 615.71.09 source archive is not in the cache yet")
            sys.exit(3)
        sys.exit(0)
    if cfg.get('modprobe_fail') == 'nvpcf_hook':
        sys.exit(1)
    st = load()
    if 'nvpcf_hook' not in loaded():
        set_loaded(loaded() + ['nvpcf_hook'])
        set_build_id('nvpcf_hook', 'bb' * 20)
        if cfg.get('hook_waits_for_init') or cfg.get('hook_stays_waiting'):
            st['hook_state'], st['hook_error'] = 'waiting', -11
        else:
            st['hook_state'], st['hook_error'] = 'active', 0
        st['hook_guards'] = 0 if cfg.get('hook_no_guard_after_load') else 1
        save(st)
    usable = st['hook_state'] == 'active' or (st['hook_state'] == 'waiting' and st['hook_guards'] == 1)
    sys.exit(0 if usable else 1)
elif cmd == 'nvpcf-hookctl':
    st = load()
    op = args[0]
    def refuse(msg):
        print(f'nvpcf-hookctl: {op} failed: {msg}', file=sys.stderr)
        sys.exit(1)
    if op == 'status':
        if cfg.get('hook_status_fail') or 'nvpcf_hook' not in loaded():
            refuse('[Errno 2] No such file or directory')
        guards = 1 if st['hook_state'] in ('active', 'waiting', 'guard-only', 'quiesced') else 0
        if st['hook_state'] in ('active', 'waiting'):
            guards = st.get('hook_guards', 1)
        blocked = 1 if st['hook_state'] in ('guard-only', 'detached', 'unavailable') else 0
        print(f"state: {st['hook_state']}\npci: 0000:01:00.0\nguards_registered: {guards}\n"
              f"initialization_blocked: {blocked}\npending_requests: 0\n"
              f"received_requests: 3\ndeferred_requests: 3\nrequests_dispatched: 3\n"
              f"dispatch_failures: 0\nretirement_failed: {1 if cfg.get('hook_retirement_failed') else 0}\n"
              f"last_error: {st['hook_error']}")
    elif op == 'prepare':
        if cfg.get('hook_prepare_fails') or st['hook_state'] == 'unavailable':
            refuse('[Errno 117] Structure needs cleaning')
    elif op == 'detach':
        if cfg.get('hook_detach_fails'):
            refuse('[Errno 11] Resource temporarily unavailable')
        st['hook_state'] = 'guard-only'
        save(st)
        if cfg.get('pause_after_detach'):
            pause()
    elif op == 'resume':
        if st['hook_state'] == 'unavailable':
            refuse('[Errno 117] Structure needs cleaning')
        if st['hook_state'] != 'active':
            st['hook_state'], st['hook_guards'] = 'waiting', 1
        save(st)
elif cmd == 'fuser':
    st = load()
    out = []
    if any(a.startswith('/dev/dri/card') for a in args):
        out += st['kms_holders']
    if any(not a.startswith('/dev/dri/card') for a in args):
        out += st['holders']
    if not out:
        sys.exit(1)
    print(' '.join(str(p) for p in out))
elif cmd == 'setsid':
    assert args[0] == '--wait', args
    os.execvp(args[1], args[1:])
elif cmd == 'python3':
    i = next(i for i, a in enumerate(args) if a.endswith('revoke.py'))
    pid = int(args[i + 1])
    st = load()
    st['holders'] = [p for p in st['holders'] if p != pid]
    st['revoked'].append(pid)
    save(st)
    sys.exit(cfg.get('revoke_rc', 0))
elif cmd == 'rmmod':
    m = args[0]
    st = load()
    if cfg.get('rmmod_fail') == m:
        sys.exit(1)
    hook_in = 'nvpcf_hook' in loaded()
    # The hook holds NVIDIA until its detach, and pins itself until NVIDIA's
    # unload notification has moved it to detached.
    if m == 'nvidia' and hook_in and st['hook_state'] not in ('guard-only', 'detached'):
        sys.exit(1)
    if m == 'nvpcf_hook' and st['hook_state'] != 'detached':
        sys.exit(1)
    if cfg.get('pause_in_rmmod') == m:
        pause()
    set_loaded([x for x in loaded() if x != m])
    note = root / 'sys/module' / m / 'notes/.note.gnu.build-id'
    if note.exists():
        note.unlink()
    if m == 'nvidia' and hook_in and st['hook_state'] == 'guard-only':
        if cfg.get('going_unavailable'):
            st['hook_state'], st['hook_error'] = 'unavailable', -5
        else:
            st['hook_state'] = 'detached'
        save(st)
    if cfg.get('pause_after_rmmod') == m:
        pause()
elif cmd == 'modprobe':
    m = args[0]
    if m in ('nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'):
        st = load()
        if not guarded(st):
            st['unguarded'].append(f'modprobe {m}')
            save(st)
    if cfg.get('modprobe_fail') == m:
        sys.exit(1)
    if m not in loaded():
        set_loaded(loaded() + [m])
        set_build_id(m, 'cc' * 20 if cfg.get('stale_module') == m else 'bb' * 20)
        if m == 'nvpcf_hook':
            st = load()
            if cfg.get('hook_waits_for_init') or cfg.get('hook_stays_waiting'):
                st['hook_state'], st['hook_error'] = 'waiting', -11
            else:
                st['hook_state'], st['hook_error'] = 'active', 0
            st['hook_guards'] = 0 if cfg.get('hook_no_guard_after_load') else 1
            save(st)
elif cmd == 'modinfo':
    if args[-1] in cfg.get('modinfo_missing', []):
        sys.exit(1)
    print(root / 'modules' / (args[-1] + '.ko'))
elif cmd == 'nm':
    if cfg.get('disk_patched', True):
        print('0000000000000000 T RmDeferNvpcfNotifyIfIdle')
    print('0000000000000000 T nv_acpi_nvpcf_event')
elif cmd == 'readelf':
    print('    Build ID: ' + 'bb' * 20)
elif cmd == 'runuser':
    st = load()
    if cfg.get('reapply_releases', True) and cfg.get('reapply_rc', 0) == 0:
        st['kms_holders'] = []
    save(st)
    sys.exit(cfg.get('reapply_rc', 0))
elif cmd == 'sleep':
    pass
elif cmd == 'getent':
    if args == ['passwd', '1000']:
        print('desktop:x:1000:1000::/home/desktop:/bin/bash')
    else:
        sys.exit(2)
elif cmd == 'lsmod':
    print(mods.read_text(), end='')
'''


def build_tree(root, nvidia_drm, patched, leftover, connected=False, hook=False):
    sysd = root / 'sys/bus/pci/devices'
    intel = sysd / '0000:00:02.0'
    (intel / 'drm').mkdir(parents=True)
    (intel / 'vendor').write_text('0x8086\n')
    (intel / 'class').write_text('0x030000\n')
    nv = sysd / '0000:01:00.0'
    for node in nvidia_drm:
        (nv / 'drm' / node).mkdir(parents=True)
        if node.startswith('card'):
            for port in ('HDMI-A-3', 'DP-3'):
                c = nv / 'drm' / node / f'{node}-{port}'
                c.mkdir()
                (c / 'status').write_text('connected\n' if connected and port == 'HDMI-A-3' else 'disconnected\n')
    (nv / 'power').mkdir()
    (nv / 'vendor').write_text('0x10de\n')
    (nv / 'class').write_text('0x030200\n')
    (nv / 'power/runtime_status').write_text('suspended\n')
    proc = root / 'proc'
    for pid, (comm, uid, cgroup) in PROCS.items():
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / 'comm').write_text(comm + '\n')
        (d / 'status').write_text(f'Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        (d / 'cgroup').write_text(f'0::{cgroup}\n')
        (d / 'environ').write_bytes(b'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\0XDG_RUNTIME_DIR=/run/user/1000\0')
    present = MODULES + (['nvpcf_hook'] if hook else [])
    (proc / 'modules').write_text(''.join(f'{m} 1 0 - Live 0x0\n' for m in present))
    for m in MODULES + ['nvpcf_hook']:
        (root / 'modules').mkdir(exist_ok=True)
        (root / 'modules' / f'{m}.ko').write_bytes(b'module')
    for m in present:
        notes = root / 'sys/module' / m / 'notes'
        notes.mkdir(parents=True)
        (notes / '.note.gnu.build-id').write_bytes(bytes.fromhex('040000001400000003000000474e5500' + OLD_ID))
    syms = ('ffffffffc0000001 t RmDeferNvpcfNotifyIfIdle\t[nvidia]\n'
            'ffffffffc0000002 t RmReplayNvpcfNotify\t[nvidia]\n') if patched else ''
    (proc / 'kallsyms').write_text('ffffffffc0000000 t nv_acpi_nvpcf_event\t[nvidia]\n' + syms)
    (proc / 'driver/nvidia').mkdir(parents=True)
    (proc / 'driver/nvidia/version').write_text('NVRM version: test module\n')
    for f in HIDE:
        dest = root / 'files' / f.lstrip('/')
        dest.parent.mkdir(parents=True, exist_ok=True)
        if leftover and f == HIDE[0]:
            dest.with_name(dest.name + '.hotswap').write_text('{}')
        else:
            dest.write_text('{}')


def rewrite(text, root):
    text = text.replace('/run/nvidia-live-swap.last', str(root / 'last-swap'))
    text = text.replace('/usr/local/lib/nvpcf-hook/', f'{root}/lib-nvpcf-hook/')
    for f in HIDE:
        text = text.replace(f, str(root / 'files' / f.lstrip('/')))
    return text.replace('/proc/', f'{root}/proc/').replace('/sys/', f'{root}/sys/')


def run(name, cfg=None, kms_holders=(), holders=(), args=(), interrupt=False,
        nvidia_drm=('card0', 'renderD129'), patched=True, leftover=False, connected=False,
        last_swap_age=None, hook='active'):
    """hook is None when nvpcf_hook is not loaded, else its starting state."""
    cfg = cfg or {}
    root = Path(tempfile.mkdtemp(prefix=name + '-', dir=BASE))
    build_tree(root, nvidia_drm, patched, leftover, connected, hook is not None)
    if last_swap_age is not None:
        (root / 'last-swap').write_text(f'{int(time.time()) - last_swap_age}\n')
    (root / 'bin').mkdir()
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('mv', 'systemctl', 'fuser', 'setsid', 'python3', 'rmmod', 'modprobe', 'lsmod',
                'modinfo', 'readelf', 'runuser', 'getent', 'sleep', 'nm'):
        (root / 'bin' / cmd).symlink_to(mock)
    (root / 'config.json').write_text(json.dumps(cfg))
    (root / 'state.json').write_text(json.dumps({
        'kms_holders': list(kms_holders), 'holders': list(holders), 'revoked': [],
        'active': {'lactd.service': True}, 'hook_state': hook or 'absent', 'hook_error': 0,
        'unguarded': []}))
    (root / 'calls.jsonl').write_text('')
    script_dir = root / 'scripts'
    script_dir.mkdir()
    for f in ('swap.sh', 'lib.sh'):
        (script_dir / f).write_text(rewrite((SRC / f).read_text(), root))
    (script_dir / 'hook').mkdir()
    (script_dir / 'hook' / 'nvpcf-hookctl').symlink_to(mock)
    (root / 'lib-nvpcf-hook').mkdir()
    if not cfg.get('loader_missing'):
        (root / 'lib-nvpcf-hook' / 'load-hook.sh').symlink_to(mock)
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    p = subprocess.Popen(['/bin/bash', str(script_dir / 'swap.sh'), *args], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=True)
    if interrupt:
        deadline = time.monotonic() + 10
        while not (root / 'paused').exists():
            if p.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f'{name}: swap.sh did not reach the mock command that pauses it')
            time.sleep(0.01)
        # start_new_session gave swap.sh a process group whose ID is its PID,
        # so this signals swap.sh and the paused mock command.
        os.killpg(p.pid, signal.SIGTERM)
    out, _ = p.communicate(timeout=30)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    st = json.loads((root / 'state.json').read_text())
    # Reading the hook's status does not change anything; its other commands do.
    def changing(c, a):
        if c == 'nvpcf-hookctl':
            return a[0] != 'status'
        if c == 'load-hook.sh':
            return not a or a[0] != '--check-disk'
        return c in MODIFYING
    changes = [f'hookctl {a[0]}' if c == 'nvpcf-hookctl' else c for c, a in calls if changing(c, a)]
    r = {
        'case': name,
        'exit': p.returncode,
        'modifying': changes,
        'fuser_args': [a for c, a in calls if c == 'fuser'],
        'revoke_args': [a for c, a in calls if c == 'python3'],
        'runuser_args': [a for c, a in calls if c == 'runuser'],
        'long_sleeps': [float(a[0]) for c, a in calls if c == 'sleep' and float(a[0]) >= 1],
        'stamp': (root / 'last-swap').read_text().strip() if (root / 'last-swap').exists() else None,
        'order': changes,
        'hookctl': [a[0] for c, a in calls if c == 'nvpcf-hookctl' and a[0] != 'status'],
        'hook_state': st['hook_state'],
        'unguarded': st['unguarded'],
        'revoked': st['revoked'],
        'rmmod': [a[0] for c, a in calls if c == 'rmmod'],
        'modprobe': [a[0] if c == 'modprobe' else 'nvpcf_hook' for c, a in calls
                     if c == 'modprobe' or (c == 'load-hook.sh' and not a)],
        'disk_checks': [a for c, a in calls if c == 'load-hook.sh' and a],
        'hidden': sorted(str(x) for x in (root / 'files').rglob('*.hotswap')),
        'lactd_active': st['active'].get('lactd.service'),
        'loaded': [l.split()[0] for l in (root / 'proc/modules').read_text().splitlines()],
        'out': out,
    }
    if any(c == 'modprobe' and a[0] == 'nvpcf_hook' for c, a in calls):
        failures.append(f"{name}: runs modprobe nvpcf_hook itself instead of load-hook.sh")
        print(f"FAIL {name}: runs modprobe nvpcf_hook itself instead of load-hook.sh\n{out}")
    if r['unguarded']:
        failures.append(f"{name}: lets NVIDIA initialize the GPU without the hook's guard: {r['unguarded']}")
        print(f"FAIL {name}: lets NVIDIA initialize the GPU without the hook's guard: {r['unguarded']}\n{out}")
    return r


failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def expect_untouched(r):
    check(r, r['modifying'] == [], f"leaves the system unchanged, got the commands {r['modifying']}")


SVC = {'service_holders': {'lactd.service': [777]}}

r = run('kms-held-by-another-process', kms_holders=[1, 2045, 4525, 999])
check(r, r['exit'] == 1, 'exits with status 1'); expect_untouched(r)
check(r, 'a swap cannot go ahead now' in r['out'], 'says that a swap cannot go ahead now')

r = run('kms-held-without-gnome-shell', kms_holders=[1, 2045])
check(r, r['exit'] == 1, 'exits with status 1'); expect_untouched(r)

r = run('kms-held-check', kms_holders=[1, 2045, 4525], args=['--check'])
check(r, r['exit'] == 0, f"exits with status 0, got {r['exit']}"); expect_untouched(r)
check(r, 'would first apply the current display layout again' in r['out'],
      'says that the swap would first apply the display layout again')

r = run('kms-released-by-reapply', SVC, kms_holders=[1, 2045, 4525], holders=[777, 4525])
check(r, r['exit'] == 0 and r['out'].rstrip().endswith('END'),
      f"exits with status 0 and ends with END, got status {r['exit']}")
ru = r['runuser_args']
check(r, len(ru) == 1 and ru[0][:3] == ['-u', 'desktop', '--'], f'calls runuser once with -u desktop, got {ru}')
check(r, any(a == 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus' for a in ru[0]) and ru[0][-1].endswith('reapply-layout.py'),
      'runs reapply-layout.py on gnome-shell\'s session bus')
check(r, r['order'][:2] == ['hookctl prepare', 'runuser'],
      f"runs reapply-layout.py right after the hook's prepare, before any other change, got the order {r['order']}")

r = run('kms-not-released', {'reapply_releases': False}, kms_holders=[1, 2045, 4525])
check(r, r['exit'] == 1 and 'still held' in r['out'],
      f"exits with status 1 and says the KMS node is still held, got status {r['exit']}")
check(r, r['modifying'] == ['hookctl prepare', 'runuser'],
      f"stops after running reapply-layout.py, got the commands {r['modifying']}")

r = run('reapply-fails', {'reapply_rc': 1}, kms_holders=[1, 2045, 4525])
check(r, r['exit'] == 1 and r['modifying'] == ['hookctl prepare', 'runuser'],
      f"stops with status 1 after reapply-layout.py fails, got status {r['exit']} and the commands {r['modifying']}")

r = run('nvidia-monitor-connected', kms_holders=[1, 2045, 4525], connected=True)
check(r, r['exit'] == 1 and 'disconnect it first' in r['out'],
      f"exits with status 1 and says to disconnect the monitor first, got status {r['exit']}"); expect_untouched(r)

r = run('mistyped-option', args=['--chek'])
check(r, r['exit'] == 2, f"exits with status 2, got {r['exit']}"); expect_untouched(r)
check(r, r['fuser_args'] == [], 'stops before it runs fuser to look for processes that hold the GPU')

r = run('extra-argument', args=['--check', 'now'])
check(r, r['exit'] == 2, f"exits with status 2, got {r['exit']}"); expect_untouched(r)

r = run('check-ok', args=['--check'])
check(r, r['exit'] == 0, 'exits with status 0'); expect_untouched(r)
check(r, 'a swap can go ahead now' in r['out'], 'says that a swap can go ahead now')

r = run('check-with-holders', SVC, holders=[777, 4525], args=['--check'])
check(r, r['exit'] == 0, 'exits with status 0'); expect_untouched(r)
check(r, 'service lactd.service holds the GPU' in r['out'] and 'handles to revoke: 4525' in r['out'],
      'reports that lactd.service would be stopped and gnome-shell\'s handles revoked')

r = run('root-outside-service', holders=[999])
check(r, r['exit'] == 1, 'exits with status 1'); expect_untouched(r)

r = run('unknown-service', holders=[778])
check(r, r['exit'] == 1, 'exits with status 1'); expect_untouched(r)
check(r, 'does not stop cuda-job.service' in r['out'], 'names the service it will not stop')

r = run('leftover', leftover=True)
check(r, r['exit'] == 1, 'exits with status 1'); expect_untouched(r)
check(r, 'left hidden by an earlier swap' in r['out'], 'reports the file an earlier swap left hidden')

r = run('success', SVC, holders=[777, 4525, 5019])
check(r, r['exit'] == 0, 'exits with status 0')
check(r, r['revoked'] == [4525, 5019], f"revokes the handles of 4525 and 5019, got {r['revoked']}")
check(r, r['rmmod'] == ['nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia', 'nvpcf_hook'],
      f"unloads nvidia_drm, nvidia_modeset, nvidia_uvm, nvidia and nvpcf_hook in that order, got {r['rmmod']}")
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'],
      f"loads nvidia, then nvpcf_hook, then nvidia_modeset, nvidia_drm and nvidia_uvm, got {r['modprobe']}")
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'moves the files back and starts lactd.service again')
check(r, sorted(r['loaded']) == sorted(MODULES + ['nvpcf_hook']), f"ends with all five modules loaded, got {r['loaded']}")
check(r, 'the loaded driver contains the patch' in r['out'] and r['hookctl'] == ['prepare', 'detach'],
      f"checks the patch and prepares and detaches the hook, got {r['hookctl']}")
check(r, r['out'].rstrip().endswith('END'), 'ends with END')
check(r, 'every loaded module has the build ID of its file on disk' in r['out'],
      'reports that the build IDs match the files on disk')

r = run('stale-module', dict(SVC, stale_module='nvidia'), holders=[777])
check(r, r['exit'] == 1, 'exits with status 1')
check(r, 'is not the file on disk' in r['out'] and 'END (failed)' in r['out'],
      'reports that the loaded nvidia module does not match its file on disk, and ends with END (failed)')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'moves the files back and starts lactd.service again')

r = run('nvidia-card1', holders=[4525], nvidia_drm=('card1', 'renderD128'))
check(r, r['exit'] == 0, 'exits with status 0')
used = [x for args in r['fuser_args'] + r['revoke_args'] for x in args]
check(r, '/dev/dri/card0' not in used and '/dev/dri/renderD129' not in used,
      'does not pass the Intel GPU\'s card0 or renderD129 to fuser or revoke.py')
check(r, '/dev/dri/card1' in used and all('/dev/dri/renderD128' in a for a in r['revoke_args']),
      'passes card1 to fuser and renderD128 to every revoke.py call')

r = run('failed-hide', dict(SVC, fail_hide=True), holders=[777])
check(r, r['exit'] == 1, 'exits with status 1')
check(r, 'systemctl' not in r['modifying'] and r['rmmod'] == [] and r['revoked'] == [],
      'exits before stopping services, revoking handles or unloading modules')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'leaves the files in place and lactd.service running')

r = run('failed-revoke', dict(SVC, revoke_rc=1), holders=[777, 4525])
check(r, r['exit'] == 1, 'exits with status 1')
check(r, r['rmmod'] == [], 'leaves the modules loaded')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'moves the files back and starts lactd.service again')

r = run('failed-unload', {'rmmod_fail': 'nvidia_modeset'})
check(r, r['exit'] == 1, 'exits with status 1')
check(r, r['modprobe'] == ['nvidia_drm'], f"loads only nvidia_drm again, got {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES + ['nvpcf_hook']), f"ends with all five modules loaded, got {r['loaded']}")
check(r, r['hidden'] == [], 'moves the files back')

r = run('failed-load', {'modprobe_fail': 'nvidia_modeset'})
check(r, r['exit'] == 1, 'exits with status 1')
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvidia_modeset', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'],
      'after nvidia_modeset fails to load, tries it again and loads nvidia_drm and nvidia_uvm, '
      f"all behind the hook, got {r['modprobe']}")
check(r, r['hidden'] == [] and 'END (failed)' in r['out'], 'moves the files back and ends with END (failed)')

r = run('missing-patch', SVC, holders=[777], patched=False)
check(r, r['exit'] == 1, 'exits with status 1')
check(r, 'lacks the patch' in r['out'] and 'END (failed)' in r['out'],
      'reports the missing patch and ends with END (failed)')
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'],
      f"loads nvidia, then nvpcf_hook, then nvidia_modeset, nvidia_drm and nvidia_uvm, got {r['modprobe']}")
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'moves the files back and starts lactd.service again')

r = run('interrupted-after-stop', dict(SVC, pause_after_stop=True), holders=[777], interrupt=True)
check(r, r['exit'] == 1, f"exits with status 1, got {r['exit']}")
check(r, r['rmmod'] == [] and r['hidden'] == [] and r['lactd_active'] is True,
      'moves the files back, starts lactd.service again and leaves the modules loaded')

r = run('interrupted-in-rmmod', {'pause_in_rmmod': 'nvidia_modeset'}, interrupt=True)
check(r, r['exit'] == 1, f"exits with status 1, got {r['exit']}")
check(r, r['modprobe'] == ['nvidia_drm'], f"loads only nvidia_drm again, got {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES + ['nvpcf_hook']) and r['hidden'] == [],
      'ends with all five modules loaded and the files moved back')

r = run('interrupted-after-rmmod', {'pause_after_rmmod': 'nvidia_modeset'}, interrupt=True)
check(r, r['exit'] == 1, f"exits with status 1, got {r['exit']}")
check(r, r['modprobe'] == ['nvidia_modeset', 'nvidia_drm'],
      f"loads nvidia_modeset and nvidia_drm again, got {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES + ['nvpcf_hook']) and r['hidden'] == [],
      'ends with all five modules loaded and the files moved back')

r = run('recent-swap-waits', last_swap_age=5)
check(r, r['exit'] == 0 and r['out'].rstrip().endswith('END'),
      f"exits with status 0 and ends with END, got status {r['exit']}")
check(r, len(r['long_sleeps']) == 1 and 14 <= r['long_sleeps'][0] <= 16,
      f"waits once, for about 15 seconds, got {r['long_sleeps']}")
check(r, r['stamp'] is not None and abs(int(r['stamp']) - time.time()) < 30,
      f"records when the run ended, got {r['stamp']}")

r = run('recent-swap-check', last_swap_age=5, args=['--check'])
check(r, r['exit'] == 0 and 'would first wait' in r['out'],
      f"exits with status 0 and says it would first wait, got status {r['exit']}"); expect_untouched(r)
check(r, r['long_sleeps'] == [], f"does not wait with --check, got {r['long_sleeps']}")

r = run('old-swap-no-wait', last_swap_age=60)
check(r, r['exit'] == 0 and r['long_sleeps'] == [],
      f"exits with status 0 without waiting, got status {r['exit']} and the sleeps {r['long_sleeps']}")

r = run('stamp-after-failure', {'fail_hide': True})
check(r, r['exit'] == 1 and r['stamp'] is not None,
      f"exits with status 1 and still records when the run ended, got status {r['exit']} and end time {r['stamp']}")

# nvpcf_hook. UNPATCHED is a driver on disk without patch 0001, so the swap
# needs the hook.
UNPATCHED = dict(SVC, disk_patched=False)

r = run('hook-success', dict(UNPATCHED, hook_waits_for_init=True), holders=[777, 4525], patched=False)
check(r, r['exit'] == 0 and r['out'].rstrip().endswith('END'),
      f"exits with status 0 and ends with END, got status {r['exit']}")
check(r, r['order'] == ['hookctl prepare', 'mv', 'mv', 'mv', 'systemctl', 'setsid', 'python3', 'hookctl detach',
                        'rmmod', 'rmmod', 'rmmod', 'rmmod', 'rmmod', 'modprobe', 'load-hook.sh', 'modprobe',
                        'modprobe', 'modprobe', 'mv', 'mv', 'mv', 'systemctl'],
      'prepares the hook before any change, detaches it after the revocation, unloads it after NVIDIA, '
      f"and loads it after NVIDIA, got the order {r['order']}")
check(r, r['rmmod'] == ['nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia', 'nvpcf_hook'],
      f"unloads nvpcf_hook after nvidia, got {r['rmmod']}")
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'],
      f"loads nvpcf_hook right after nvidia, before the modules that initialize the GPU, got {r['modprobe']}")
check(r, 'nvpcf_hook: state active' in r['out'].splitlines()[-2], 'reports the active hook just before END')
check(r, 'lacks the patch' not in r['out'], 'does not ask the loaded driver for patch 0001')
check(r, r['hook_state'] == 'active', f"leaves the hook active, got {r['hook_state']}")

r = run('hook-check', UNPATCHED, holders=[777, 4525], args=['--check'], patched=False)
check(r, r['exit'] == 0 and 'a swap can go ahead now' in r['out'],
      f"exits with status 0 and says a swap can go ahead, got status {r['exit']}"); expect_untouched(r)
check(r, 'the nvidia module on disk lacks patch 0001' in r['out'] and 'nvpcf_hook: state active' in r['out'],
      'reports that the driver on disk lacks the patch, and the hook\'s state')

r = run('hook-not-loaded', UNPATCHED, holders=[777], patched=False, hook=None)
check(r, r['exit'] == 1 and 'nvpcf_hook is not loaded; load it with' in r['out'] and 'load-hook.sh' in r['out'],
      f"exits with status 1 and names the loader, got status {r['exit']}"); expect_untouched(r)

r = run('patched-without-hook', SVC, holders=[777], hook=None)
check(r, r['exit'] == 1 and 'nvpcf_hook is not loaded' in r['out'],
      f"refuses with a patched driver on disk too, because the hook guards the shutdown, got status {r['exit']}")
expect_untouched(r)

r = run('hook-not-installed', dict(UNPATCHED, modinfo_missing=['nvpcf_hook']), holders=[777], patched=False)
check(r, r['exit'] == 1 and 'nvpcf_hook is not installed for kernel' in r['out'],
      f"exits with status 1 and says the hook is not installed for the kernel, got status {r['exit']}")
expect_untouched(r)

r = run('hook-status-unreadable', dict(UNPATCHED, hook_status_fail=True), holders=[777], patched=False)
check(r, r['exit'] == 1 and 'cannot read the status of nvpcf_hook: nvpcf-hookctl: status failed' in r['out'],
      f"exits with status 1 and reports why the status cannot be read, got status {r['exit']}"); expect_untouched(r)

r = run('hook-unavailable', UNPATCHED, holders=[777], patched=False, hook='unavailable')
check(r, r['exit'] == 1 and 'swap.sh does not acknowledge a loss' in r['out'],
      f"exits with status 1 and says it does not acknowledge a loss, got status {r['exit']}"); expect_untouched(r)

r = run('hook-retirement-failed', dict(UNPATCHED, hook_retirement_failed=True), holders=[777], patched=False,
        hook='unavailable')
check(r, r['exit'] == 1 and 'could not remove its interception' in r['out'] and 'nvpcf-hookctl detach retries the removal' in r['out']
      and 'retirement_failed 1' in r['out'],
      f"exits with status 1, names the failed removal and the command that retries it, got status {r['exit']}")
expect_untouched(r)

r = run('hook-detached', UNPATCHED, holders=[777], patched=False, hook='detached')
check(r, r['exit'] == 1 and 'nvpcf-hookctl resume, or unload it and load it again' in r['out'],
      f"exits with status 1 and says how to attach the hook again, got status {r['exit']}"); expect_untouched(r)

r = run('hook-starting', UNPATCHED, holders=[777], patched=False, hook='starting')
check(r, r['exit'] == 1 and 'check again in a few seconds' in r['out'],
      f"exits with status 1 and asks to check again, got status {r['exit']}"); expect_untouched(r)

r = run('hook-prepare-fails', dict(UNPATCHED, hook_prepare_fails=True), kms_holders=[1, 2045, 4525],
        holders=[777, 4525], patched=False)
check(r, r['exit'] == 1 and 'stops before it changes anything' in r['out'] and 'END (failed)' in r['out'],
      f"exits with status 1 and ends with END (failed), got status {r['exit']}")
check(r, r['modifying'] == ['hookctl prepare'],
      f"changes nothing after the failed prepare, not even the display layout, got the commands {r['modifying']}")

r = run('hook-detach-fails', dict(UNPATCHED, hook_detach_fails=True), holders=[777, 4525], patched=False)
check(r, r['exit'] == 1 and r['rmmod'] == [], f"exits with status 1 without unloading anything, got {r['rmmod']}")
check(r, r['hookctl'] == ['prepare', 'detach', 'resume'], f"resumes the hook after the failed detach, got {r['hookctl']}")
check(r, r['order'].index('hookctl resume') < len(r['order']) - 1 - r['order'][::-1].index('systemctl'),
      f"resumes the hook before it starts lactd.service again, got the order {r['order']}")
check(r, r['hidden'] == [] and r['lactd_active'] is True and r['hook_state'] == 'active',
      'moves the files back, starts lactd.service again and leaves the hook active')

r = run('hook-nvidia-unload-fails', dict(UNPATCHED, rmmod_fail='nvidia_modeset'), holders=[777], patched=False)
check(r, r['exit'] == 1 and r['modprobe'] == ['nvidia_drm'],
      f"exits with status 1 and loads only nvidia_drm again, got status {r['exit']} and {r['modprobe']}")
o = r['order']
check(r, 'hookctl resume' in o and o.index('hookctl resume') < o.index('modprobe'),
      f"resumes the guard-only hook before it loads nvidia_drm again, got the order {o}")
check(r, r['hook_state'] == 'active' and r['lactd_active'] is True,
      f"leaves the hook active and lactd.service running, got {r['hook_state']}")

r = run('hook-unavailable-after-nvidia', dict(UNPATCHED, going_unavailable=True), holders=[777], patched=False)
check(r, r['exit'] == 1 and 'could not unload nvpcf_hook after NVIDIA' in r['out'],
      f"exits with status 1 when the hook stays pinned, got status {r['exit']}")
check(r, r['modprobe'] == ['nvidia'], f"loads only nvidia again, got {r['modprobe']}")
check(r, 'could not resume nvpcf_hook' in r['out'] and 'acknowledge-loss' not in r['hookctl'],
      f"reports that the hook cannot resume and does not acknowledge the loss, got {r['hookctl']}")
check(r, 'not loaded again: nvidia_modeset nvidia_drm nvidia_uvm' in r['out']
      and 'not started again: lactd.service' in r['out'] and r['lactd_active'] is False and r['hidden'] == [],
      'moves the files back, but leaves the other modules unloaded and lactd.service stopped, and says so')
check(r, 'nvpcf_hook: state unavailable' in r['out'] and r['out'].rstrip().endswith('END (failed)'),
      'reports the unavailable hook and ends with END (failed)')

r = run('hook-load-fails', dict(UNPATCHED, modprobe_fail='nvpcf_hook'), holders=[777], patched=False)
check(r, r['exit'] == 1 and r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvpcf_hook'],
      f"exits with status 1 after trying to load the hook again, got status {r['exit']} and {r['modprobe']}")
check(r, 'could not load nvpcf_hook again' in r['out'] and 'nvpcf_hook is not loaded' in r['out'],
      'reports that the hook could not be loaded again')
check(r, r['lactd_active'] is False and 'not loaded again: nvidia_modeset nvidia_drm nvidia_uvm' in r['out'],
      'leaves the other modules unloaded and lactd.service stopped')

r = run('hook-stays-waiting', dict(UNPATCHED, hook_stays_waiting=True), holders=[777], patched=False)
check(r, r['exit'] == 1 and f'nvpcf_hook is not active 15 s after the swap: nvpcf_hook: state waiting' in r['out'],
      f"exits with status 1 and reports the state of the hook that did not become active, got status {r['exit']}")
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'], f"does not load any module again, got {r['modprobe']}")
check(r, r['hookctl'] == ['prepare', 'detach'], f"does not resume the newly loaded hook, got {r['hookctl']}")

r = run('hook-no-guard-after-load', dict(UNPATCHED, hook_no_guard_after_load=True), holders=[777], patched=False)
check(r, r['exit'] == 1 and 'stops before the modules that initialize the GPU' in r['out'],
      f"exits with status 1 before the modules that initialize the GPU, got status {r['exit']}")
check(r, r['modprobe'] == ['nvidia', 'nvpcf_hook'] and r['lactd_active'] is False and r['hidden'] == [],
      f"moves the files back, but loads no other module and leaves lactd.service stopped, got {r['modprobe']}")

r = run('interrupted-after-nvidia-unload', dict(UNPATCHED, pause_after_rmmod='nvidia'), holders=[777],
        patched=False, interrupt=True)
check(r, r['exit'] == 1 and r['modprobe'] == MODULES,
      f"loads nvidia, resumes the hook, then loads the other modules, got status {r['exit']} and {r['modprobe']}")
check(r, r['hookctl'] == ['prepare', 'detach', 'resume'] and r['lactd_active'] is True,
      f"resumes the detached hook and starts lactd.service again, got {r['hookctl']}")

r = run('disk-check-rejects', dict(SVC, disk_check='reject'), holders=[777])
check(r, r['exit'] == 1 and "does not pass nvpcf_hook's checks" in r['out'],
      f"exits with status 1 when the driver on disk fails the hook's checks, got status {r['exit']}")
expect_untouched(r)

r = run('check-uncached', dict(SVC, disk_check='uncached'), holders=[777], args=['--check'])
check(r, r['exit'] == 0 and 'a swap would download it first' in r['out'] and 'a swap can go ahead now' in r['out'],
      f"reports the missing archive as a check still to do, not a refusal, got status {r['exit']}")
check(r, r['disk_checks'] == [['--check-disk', '--no-download']],
      f"does not let --check download, got {r['disk_checks']}"); expect_untouched(r)

r = run('swap-uncached', dict(SVC, disk_check='uncached'), holders=[777])
check(r, r['exit'] == 0 and r['disk_checks'] == [['--check-disk']],
      f"lets a swap download the archive before its first change, got status {r['exit']} and {r['disk_checks']}")

r = run('loader-missing', dict(SVC, loader_missing=True), holders=[777])
check(r, r['exit'] == 1 and 'load-hook.sh is missing; install-hook.sh installs it' in r['out'],
      f"exits with status 1 when the loader is not installed, got status {r['exit']}"); expect_untouched(r)

r = run('service-start-fails', dict(SVC, start_fails=True), holders=[777])
check(r, r['exit'] == 1 and 'could not start lactd.service' in r['out'] and r['out'].rstrip().endswith('END (failed)'),
      f"ends with END (failed) and status 1 when a service does not start, got status {r['exit']}")

r = run('hook-interrupted-after-detach', dict(UNPATCHED, pause_after_detach=True), holders=[777], patched=False, interrupt=True)
check(r, r['exit'] == 1 and r['rmmod'] == [], f"exits with status 1 without unloading anything, got {r['rmmod']}")
check(r, r['hookctl'] == ['prepare', 'detach', 'resume'] and r['hook_state'] == 'active' and r['lactd_active'] is True,
      f"resumes the hook and starts lactd.service again, got {r['hookctl']} and {r['hook_state']}")


if failures:
    print(f"{len(failures)} failures; the cases are in {BASE}")
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
