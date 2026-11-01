#!/usr/bin/python3
"""Run swap.sh against mocked commands and a fake sysfs, /proc and
registration-file tree. Runs as an ordinary user.

Every command that changes the system (mv, systemctl, setsid and python3 for
revoke.py, rmmod, modprobe) is a mock on PATH, and the paths swap.sh reads or
renames under /sys, /proc and /usr/share are rewritten into a throwaway tree.
The real /dev nodes are only tested with [ -c ]."""
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
MODIFYING = {'mv', 'systemctl', 'setsid', 'python3', 'rmmod', 'modprobe'}

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
    save(st)
    if action == 'stop' and cfg.get('pause_after_stop'):
        pause()
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
    if cfg.get('rmmod_fail') == m:
        sys.exit(1)
    if cfg.get('pause_in_rmmod') == m:
        pause()
    set_loaded([x for x in loaded() if x != m])
    note = root / 'sys/module' / m / 'notes/.note.gnu.build-id'
    if note.exists():
        note.unlink()
    if cfg.get('pause_after_rmmod') == m:
        pause()
elif cmd == 'modprobe':
    m = args[0]
    if cfg.get('modprobe_fail') == m:
        sys.exit(1)
    if m not in loaded():
        set_loaded(loaded() + [m])
        set_build_id(m, 'cc' * 20 if cfg.get('stale_module') == m else 'bb' * 20)
elif cmd == 'modinfo':
    print(root / 'modules' / (args[-1] + '.ko'))
elif cmd == 'readelf':
    print('    Build ID: ' + 'bb' * 20)
elif cmd == 'lsmod':
    print(mods.read_text(), end='')
'''


def build_tree(root, nvidia_drm, patched, leftover):
    sysd = root / 'sys/bus/pci/devices'
    intel = sysd / '0000:00:02.0'
    (intel / 'drm').mkdir(parents=True)
    (intel / 'vendor').write_text('0x8086\n')
    (intel / 'class').write_text('0x030000\n')
    nv = sysd / '0000:01:00.0'
    for node in nvidia_drm:
        (nv / 'drm' / node).mkdir(parents=True)
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
    (proc / 'modules').write_text(''.join(f'{m} 1 0 - Live 0x0\n' for m in MODULES))
    for m in MODULES:
        notes = root / 'sys/module' / m / 'notes'
        notes.mkdir(parents=True)
        (notes / '.note.gnu.build-id').write_bytes(bytes.fromhex('040000001400000003000000474e5500' + OLD_ID))
        (root / 'modules').mkdir(exist_ok=True)
        (root / 'modules' / f'{m}.ko').write_bytes(b'module')
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
    for f in HIDE:
        text = text.replace(f, str(root / 'files' / f.lstrip('/')))
    return text.replace('/proc/', f'{root}/proc/').replace('/sys/', f'{root}/sys/')


def run(name, cfg=None, kms_holders=(), holders=(), args=(), interrupt=False,
        nvidia_drm=('card0', 'renderD129'), patched=True, leftover=False):
    cfg = cfg or {}
    root = Path(tempfile.mkdtemp(prefix=name + '-', dir=BASE))
    build_tree(root, nvidia_drm, patched, leftover)
    (root / 'bin').mkdir()
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('mv', 'systemctl', 'fuser', 'setsid', 'python3', 'rmmod', 'modprobe', 'lsmod',
                'modinfo', 'readelf'):
        (root / 'bin' / cmd).symlink_to(mock)
    (root / 'config.json').write_text(json.dumps(cfg))
    (root / 'state.json').write_text(json.dumps({
        'kms_holders': list(kms_holders), 'holders': list(holders), 'revoked': [],
        'active': {'lactd.service': True}}))
    (root / 'calls.jsonl').write_text('')
    script_dir = root / 'scripts'
    script_dir.mkdir()
    for f in ('swap.sh', 'lib.sh'):
        (script_dir / f).write_text(rewrite((SRC / f).read_text(), root))
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    p = subprocess.Popen(['/bin/bash', str(script_dir / 'swap.sh'), *args], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=True)
    if interrupt:
        deadline = time.monotonic() + 10
        while not (root / 'paused').exists():
            if p.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f'{name}: the script did not reach the pause')
            time.sleep(0.01)
        os.killpg(p.pid, signal.SIGTERM)  # the session Popen created above
    out, _ = p.communicate(timeout=30)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    st = json.loads((root / 'state.json').read_text())
    r = {
        'case': name,
        'exit': p.returncode,
        'modifying': [c for c, _ in calls if c in MODIFYING],
        'fuser_args': [a for c, a in calls if c == 'fuser'],
        'revoke_args': [a for c, a in calls if c == 'python3'],
        'revoked': st['revoked'],
        'rmmod': [a[0] for c, a in calls if c == 'rmmod'],
        'modprobe': [a[0] for c, a in calls if c == 'modprobe'],
        'hidden': sorted(str(x) for x in (root / 'files').rglob('*.hotswap')),
        'lactd_active': st['active'].get('lactd.service'),
        'loaded': [l.split()[0] for l in (root / 'proc/modules').read_text().splitlines()],
        'out': out,
    }
    return r


failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def expect_untouched(r):
    check(r, r['modifying'] == [], f"modifying commands ran: {r['modifying']}")


SVC = {'service_holders': {'lactd.service': [777]}}

r = run('kms-held', kms_holders=[1, 2045, 4525])
check(r, r['exit'] == 1, 'exit 1'); expect_untouched(r)
check(r, 'a swap cannot go ahead now' in r['out'], 'refusal message')

r = run('mistyped-option', args=['--chek'])
check(r, r['exit'] == 2, f"exit {r['exit']}"); expect_untouched(r)
check(r, r['fuser_args'] == [], 'stops before inspecting holders')

r = run('extra-argument', args=['--check', 'now'])
check(r, r['exit'] == 2, f"exit {r['exit']}"); expect_untouched(r)

r = run('check-ok', args=['--check'])
check(r, r['exit'] == 0, 'exit 0'); expect_untouched(r)
check(r, 'a swap can go ahead now' in r['out'], 'go-ahead message')

r = run('check-with-holders', SVC, holders=[777, 4525], args=['--check'])
check(r, r['exit'] == 0, 'exit 0'); expect_untouched(r)
check(r, 'service lactd.service holds the GPU' in r['out'] and 'handles to revoke: 4525' in r['out'], 'holder report')

r = run('root-outside-service', holders=[999])
check(r, r['exit'] == 1, 'exit 1'); expect_untouched(r)

r = run('unknown-service', holders=[778])
check(r, r['exit'] == 1, 'exit 1'); expect_untouched(r)
check(r, 'does not stop cuda-job.service' in r['out'], 'names the service it will not stop')

r = run('leftover', leftover=True)
check(r, r['exit'] == 1, 'exit 1'); expect_untouched(r)
check(r, 'left hidden by an earlier swap' in r['out'], 'leftover message')

r = run('success', SVC, holders=[777, 4525, 5019])
check(r, r['exit'] == 0, 'exit 0')
check(r, r['revoked'] == [4525, 5019], f"revoked {r['revoked']}")
check(r, r['rmmod'] == ['nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia'], f"rmmod {r['rmmod']}")
check(r, r['modprobe'] == MODULES, f"modprobe {r['modprobe']}")
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'files and service restored')
check(r, sorted(r['loaded']) == sorted(MODULES), f"loaded {r['loaded']}")
check(r, r['out'].rstrip().endswith('END'), 'ends with END')
check(r, 'every loaded module has the build ID of its file on disk' in r['out'], 'build IDs checked')

r = run('stale-module', dict(SVC, stale_module='nvidia'), holders=[777])
check(r, r['exit'] == 1, 'exit 1')
check(r, 'is not the file on disk' in r['out'] and 'END (failed)' in r['out'], 'mismatch reported')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'files and service restored')

r = run('nvidia-card1', holders=[4525], nvidia_drm=('card1', 'renderD128'))
check(r, r['exit'] == 0, 'exit 0')
used = [x for args in r['fuser_args'] + r['revoke_args'] for x in args]
check(r, '/dev/dri/card0' not in used and '/dev/dri/renderD129' not in used, 'never touches the Intel nodes')
check(r, '/dev/dri/card1' in used and all('/dev/dri/renderD128' in a for a in r['revoke_args']), 'uses card1 and renderD128')

r = run('failed-hide', dict(SVC, fail_hide=True), holders=[777])
check(r, r['exit'] == 1, 'exit 1')
check(r, 'systemctl' not in r['modifying'] and r['rmmod'] == [] and r['revoked'] == [], 'stopped before services, revokes and modules')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'files and service as before')

r = run('failed-revoke', dict(SVC, revoke_rc=1), holders=[777, 4525])
check(r, r['exit'] == 1, 'exit 1')
check(r, r['rmmod'] == [], 'modules left loaded')
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'files and service restored')

r = run('failed-unload', {'rmmod_fail': 'nvidia_modeset'})
check(r, r['exit'] == 1, 'exit 1')
check(r, r['modprobe'] == ['nvidia_drm'], f"reloaded {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES), f"loaded {r['loaded']}")
check(r, r['hidden'] == [], 'files restored')

r = run('failed-load', {'modprobe_fail': 'nvidia_modeset'})
check(r, r['exit'] == 1, 'exit 1')
check(r, r['modprobe'] == ['nvidia', 'nvidia_modeset', 'nvidia_modeset', 'nvidia_drm', 'nvidia_uvm'], f"modprobe {r['modprobe']}")
check(r, r['hidden'] == [] and 'END (failed)' in r['out'], 'files restored, failure reported')

r = run('missing-patch', SVC, holders=[777], patched=False)
check(r, r['exit'] == 1, 'exit 1')
check(r, 'lacks the patch' in r['out'] and 'END (failed)' in r['out'], 'failure reported')
check(r, r['modprobe'] == MODULES, f"modprobe {r['modprobe']}")
check(r, r['hidden'] == [] and r['lactd_active'] is True, 'files and service restored')

r = run('interrupted-after-stop', dict(SVC, pause_after_stop=True), holders=[777], interrupt=True)
check(r, r['exit'] == 1, f"exit {r['exit']}")
check(r, r['rmmod'] == [] and r['hidden'] == [] and r['lactd_active'] is True, 'files and service restored, modules left loaded')

r = run('interrupted-in-rmmod', {'pause_in_rmmod': 'nvidia_modeset'}, interrupt=True)
check(r, r['exit'] == 1, f"exit {r['exit']}")
check(r, r['modprobe'] == ['nvidia_drm'], f"reloaded {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES) and r['hidden'] == [], 'modules and files restored')

r = run('interrupted-after-rmmod', {'pause_after_rmmod': 'nvidia_modeset'}, interrupt=True)
check(r, r['exit'] == 1, f"exit {r['exit']}")
check(r, r['modprobe'] == ['nvidia_modeset', 'nvidia_drm'], f"reloaded {r['modprobe']}")
check(r, sorted(r['loaded']) == sorted(MODULES) and r['hidden'] == [], 'modules and files restored')

if failures:
    print(f"{len(failures)} failures; the cases are in {BASE}")
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
