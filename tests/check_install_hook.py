#!/usr/bin/python3
"""Run install-hook.sh as an ordinary user, against a mock dkms that keeps
state, and a throwaway directory in place of /usr/src, /lib/modules,
/var/lib/dkms, /usr/local/lib/nvpcf-hook, /etc/systemd/system and
/var/cache/nvpcf-hook.

The mock dkms records which versions are added, which are built and which is
installed for each kernel, and keeps the installed module files. Its remove
uninstalls a version from every kernel that has it installed, as DKMS does,
so a premature remove shows up as a lost module. A module file holds its
build ID as text, which the mock readelf reports. uname, modinfo, readelf,
systemctl and curl are mocks too. The copied load-hook.sh downloads into the
throwaway cache through the mock curl.

The cases check the kernels that get the new version, that a kernel keeps
its working module when the new version fails for it or its headers are
missing, that an old version is removed only when no kernel has it installed,
and that a failing dkms status stops the script before it changes
anything."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SRC = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix='install-hook-test-'))
RUNNING = '7.0.0-38-generic'
OTHER = '6.8.0-147-generic'
NVIDIA = (f'nvidia/615.71.09, {OTHER}, x86_64: installed\n'
          f'nvidia/615.71.09, {RUNNING}, x86_64: installed\n')
OLD = '0.1-0123456789ab'

MOCK = r'''#!/usr/bin/python3 -I
import hashlib, json, os, re, shutil, sys
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
def opt(name): return args[args.index(name) + 1]
def built_path(v, k): return root / 'var/lib/dkms/nvpcf-hook' / v / k / 'x86_64/module/nvpcf_hook.ko'
def installed_path(k): return root / 'lib/modules' / k / 'updates/dkms/nvpcf_hook.ko'
if cmd == 'dkms':
    st = load()
    op = args[0]
    if op == 'status':
        if args[1] == 'nvidia':
            if cfg.get('status_fails'):
                sys.exit(1)
            print(cfg['status'], end='')
        else:
            if cfg.get('hook_status_fails'):
                sys.exit(1)
            for v in sorted(st['added']):
                ks = sorted(k for v2, k in st['built'] if v2 == v)
                if not ks:
                    print(f'nvpcf-hook/{v}: added')
                for k in ks:
                    print(f"nvpcf-hook/{v}, {k}, x86_64: {'installed' if st['installed'].get(k) == v else 'built'}")
    elif op == 'add':
        v = opt('-v')
        conf = root / f'usr/src/nvpcf-hook-{v}/dkms.conf'
        if not conf.exists() or f'PACKAGE_VERSION="{v}"' not in conf.read_text():
            sys.exit(1)
        st['added'].append(v)
    elif op == 'build':
        v, k = opt('-v'), opt('-k')
        if k in cfg.get('build_fails', []):
            sys.exit(10)
        p = built_path(v, k)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'BUILDID={hashlib.sha1((v + k).encode()).hexdigest()} {v}\n')
        if [v, k] not in st['built']:
            st['built'].append([v, k])
    elif op == 'install':
        v, k = opt('-v'), opt('-k')
        if k in cfg.get('install_fails', []):
            sys.exit(6)
        p = installed_path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(built_path(v, k), p)
        st['installed'][k] = v
    elif op == 'remove':
        v = args[1].split('/')[1]
        for k, iv in list(st['installed'].items()):
            if iv == v:
                installed_path(k).unlink()
                del st['installed'][k]
        st['built'] = [b for b in st['built'] if b[0] != v]
        st['added'] = [a for a in st['added'] if a != v]
    save(st)
elif cmd == 'uname':
    print(cfg['running'])
elif cmd == 'modinfo':
    p = installed_path(opt('-k'))
    if not p.exists():
        sys.exit(1)
    print(p)
elif cmd == 'readelf':
    print('    Build ID: ' + Path(args[-1]).read_text().split('BUILDID=')[1].split()[0])
elif cmd == 'curl':
    out = Path(args[args.index('-o') + 1])
    if cfg.get('fetch_fails'):
        sys.exit(22)
    out.write_text('archive')
'''

failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def run(name, cfg, headers=(RUNNING, OTHER), args=(), installed=None, runs=1):
    """installed maps kernels to an old version that is built and installed
    for them before the run."""
    root = BASE / name
    (root / 'bin').mkdir(parents=True)
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('dkms', 'uname', 'modinfo', 'readelf', 'systemctl', 'curl'):
        (root / 'bin' / cmd).symlink_to(mock)
    for k in headers:
        (root / 'lib/modules' / k / 'build').mkdir(parents=True)
    st = {'added': [], 'built': [], 'installed': {}}
    for k, v in (installed or {}).items():
        if v not in st['added']:
            st['added'].append(v)
            (root / f'usr/src/nvpcf-hook-{v}').mkdir(parents=True, exist_ok=True)
        st['built'].append([v, k])
        st['installed'][k] = v
        for p in (root / 'var/lib/dkms/nvpcf-hook' / v / k / 'x86_64/module/nvpcf_hook.ko',
                  root / 'lib/modules' / k / 'updates/dkms/nvpcf_hook.ko'):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f'BUILDID={hashlib.sha1((v + k).encode()).hexdigest()} {v}\n')
    (root / 'state.json').write_text(json.dumps(st))
    (root / 'config.json').write_text(json.dumps(dict({'running': RUNNING, 'status': NVIDIA}, **cfg)))
    (root / 'calls.jsonl').write_text('')
    (root / 'cache').mkdir()
    scripts = root / 'scripts'
    shutil.copytree(SRC / 'hook', scripts / 'hook', ignore=shutil.ignore_patterns('*.o', '*.ko', 'state_test'))
    shutil.copy(SRC / 'nvpcf-hook.service', scripts / 'nvpcf-hook.service')
    paths = (('/usr/local/lib/nvpcf-hook', f'{root}/usr/local/lib/nvpcf-hook'),
             ('/etc/systemd/system/', f'{root}/etc/systemd/system/'),
             ('/usr/src/', f'{root}/usr/src/'), ('/lib/modules/', f'{root}/lib/modules/'),
             ('/var/lib/dkms/', f'{root}/var/lib/dkms/'), ('/var/cache/nvpcf-hook', f'{root}/cache'))
    for f in ('install-hook.sh', 'load-hook.sh'):
        text = (SRC / f).read_text()
        for old, new in paths:
            text = text.replace(old, new)
        (scripts / f).write_text(text)
        (scripts / f).chmod(0o755)
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    for _ in range(runs):
        (root / 'calls.jsonl').write_text('')
        p = subprocess.run(['/bin/bash', str(scripts / 'install-hook.sh'), *args], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    st = json.loads((root / 'state.json').read_text())
    named = [f'dkms {a[0]}' if c == 'dkms' else c for c, a in calls]
    lib = root / 'usr/local/lib/nvpcf-hook'
    return {'case': name, 'exit': p.returncode, 'out': p.stdout, 'calls': named, 'state': st,
            'changing': [c for c in named if c in ('dkms add', 'dkms build', 'dkms install', 'dkms remove')],
            'removed': [a[1] for c, a in calls if c == 'dkms' and a[0] == 'remove'],
            'new': next((v for v in st['added'] if v != OLD), None),
            'loader': sorted(f.name for f in lib.iterdir()) if lib.exists() else [],
            'unit': (root / 'etc/systemd/system/nvpcf-hook.service').exists(),
            'enabled': ['enable', 'nvpcf-hook.service'] in [a for c, a in calls if c == 'systemctl'],
            'archives': sorted(f.name for f in (root / 'cache').iterdir()),
            'sources': sorted(d.name for d in (root / 'usr/src').iterdir()) if (root / 'usr/src').exists() else []}


r = run('fresh-install', {})
check(r, r['exit'] == 0 and r['new'] is not None, f"exits with status 0 and adds a new version, got status {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got {r['state']['installed']}")
check(r, r['loader'] == ['check_nvidia.py', 'load-hook.sh', 'nvpcf-hookctl'] and r['unit'] and r['enabled'],
      f"installs the loader, its checker and the control client, and enables the boot unit, got {r['loader']}")
check(r, r['archives'] == ['open-gpu-kernel-modules-615.71.09.tar.gz'],
      f"downloads NVIDIA's source archive for the installed version, got {r['archives']}")
check(r, r['new'] is not None and r['new'].startswith('0.1-') and len(r['new']) == len('0.1-') + 12,
      f"names the version after hook/dkms.conf and 12 hex digits of the source hash, got {r['new']}")

r = run('upgrade', {}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 0 and r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got status {r['exit']} and {r['state']['installed']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'] and OLD not in r['state']['added']
      and f'nvpcf-hook-{OLD}' not in r['sources'],
      f"removes the old version and its source once no kernel has it installed, got {r['removed']}")
c = r['calls']
check(r, c.index('dkms remove') > max(i for i, x in enumerate(c) if x == 'dkms install'),
      f"removes the old version only after the installs, got the calls {c}")

r = run('build-fails-first', {'build_fails': [OTHER]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'{OTHER}: build' in r['out'], f"exits with status 1 and names the failure, got {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: OLD},
      f"keeps the old module on the kernel whose build failed, got {r['state']['installed']}")
check(r, r['removed'] == [] and OLD in r['state']['added'], 'keeps the old version, which a kernel still has installed')

r = run('all-builds-fail', {'build_fails': [RUNNING, OTHER]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and r['state']['installed'] == {RUNNING: OLD, OTHER: OLD} and r['removed'] == [],
      f"leaves both kernels with their working modules, got {r['state']['installed']}")

r = run('install-fails', {'install_fails': [RUNNING]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and r['state']['installed'][RUNNING] == OLD and r['removed'] == [],
      f"keeps the old module on the kernel whose install failed, got {r['state']['installed']}")

r = run('other-headers-missing', {}, headers=(RUNNING,), installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'headers are missing, not built: {OTHER}' in r['out'],
      f"exits with status 1 and names the skipped kernel, got {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: OLD} and r['removed'] == [],
      f"keeps the skipped kernel's module and the old version, got {r['state']['installed']}")

r = run('again', {}, runs=2)
check(r, r['exit'] == 0 and r['changing'] == [],
      f"changes nothing on a second run with the same source, got status {r['exit']} and {r['changing']}")

r = run('status-fails', {'status_fails': True}, installed={RUNNING: OLD})
check(r, r['exit'] == 1 and r['changing'] == [] and r['state']['installed'] == {RUNNING: OLD} and r['loader'] == [],
      f"stops before changing anything when dkms status nvidia fails, got {r['changing']}")

r = run('hook-status-fails', {'hook_status_fails': True}, installed={RUNNING: OLD})
check(r, r['exit'] == 1 and 'dkms status nvpcf-hook failed' in r['out'] and r['changing'] == []
      and r['sources'] == [f'nvpcf-hook-{OLD}'],
      f"stops before changing anything when dkms status nvpcf-hook fails, got {r['changing']}")

r = run('running-headers-missing', {}, headers=(OTHER,))
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'] and r['changing'] == [],
      f"stops before changing anything when the running kernel lacks headers, got {r['changing']}")

r = run('fetch-fails', {'fetch_fails': True})
check(r, r['exit'] == 1 and 'source archive not downloaded' in r['out']
      and r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the module, but exits with status 1 when an archive cannot be downloaded, got {r['exit']}")

r = run('extra-argument', {}, args=('now',))
check(r, r['exit'] == 2 and r['calls'] == [], f"exits with status 2 before running any command, got {r['exit']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
