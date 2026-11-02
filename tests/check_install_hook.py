#!/usr/bin/python3
"""Run install-hook.sh as an ordinary user, with a mock dkms, uname and
modinfo and, for each case, a throwaway directory in place of /usr/src,
/lib/modules and /etc/modules-load.d.

The cases check the kernels that install-hook.sh builds the module for, that
it replaces an earlier copy of the same version, and that it stops before it
changes anything when dkms status fails or the running kernel's headers are
missing."""
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
VER = next(l.split('"')[1] for l in (SRC / 'hook/dkms.conf').read_text().splitlines()
           if l.startswith('PACKAGE_VERSION='))
CHANGING = {'dkms remove', 'dkms add', 'dkms build', 'dkms install'}

MOCK = r'''#!/usr/bin/python3 -I
import json, os, sys
from pathlib import Path
root = Path(os.environ['MOCK_ROOT'])
cfg = json.loads((root / 'config.json').read_text())
cmd = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps([cmd, args]) + '\n')
if cmd == 'dkms':
    if args[0] == 'status':
        if args[1:] == ['nvidia']:
            if cfg.get('status_fails'):
                sys.exit(1)
            print(cfg['status'], end='')
        else:
            print(cfg.get('hook_status', ''), end='')
    elif args[0] == 'install':
        k = args[args.index('-k') + 1]
        d = root / 'lib/modules' / k / 'updates/dkms'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'nvpcf_hook.ko').write_text('module')
elif cmd == 'uname':
    print(cfg['running'])
elif cmd == 'modinfo':
    k = args[args.index('-k') + 1]
    f = root / 'lib/modules' / k / 'updates/dkms/nvpcf_hook.ko'
    if not f.exists():
        sys.exit(1)
    print(f)
'''

failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def run(name, cfg, headers=(RUNNING, OTHER), args=()):
    root = BASE / name
    (root / 'bin').mkdir(parents=True)
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('dkms', 'uname', 'modinfo'):
        (root / 'bin' / cmd).symlink_to(mock)
    for k in headers:
        (root / 'lib/modules' / k / 'build').mkdir(parents=True)
    (root / 'etc/modules-load.d').mkdir(parents=True)
    stale = root / f'usr/src/nvpcf-hook-{VER}'
    stale.mkdir(parents=True)
    (stale / 'old-file.c').write_text('from an earlier install')
    (root / 'config.json').write_text(json.dumps(dict({'running': RUNNING}, **cfg)))
    (root / 'calls.jsonl').write_text('')
    scripts = root / 'scripts'
    shutil.copytree(SRC / 'hook', scripts / 'hook', ignore=shutil.ignore_patterns('tests', '*.o', '*.ko'))
    text = (SRC / 'install-hook.sh').read_text()
    for path in ('/usr/src/', '/lib/modules/', '/etc/modules-load.d/'):
        text = text.replace(path, f'{root}{path}')
    (scripts / 'install-hook.sh').write_text(text)
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    p = subprocess.run(['/bin/bash', str(scripts / 'install-hook.sh'), *args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    named = [f'dkms {a[0]}' if c == 'dkms' else c for c, a in calls]
    conf = root / 'etc/modules-load.d/nvpcf-hook.conf'
    return {'case': name, 'exit': p.returncode, 'out': p.stdout, 'calls': named,
            'changing': [c for c in named if c in CHANGING],
            'built_for': [a[a.index('-k') + 1] for c, a in calls if c == 'dkms' and a[0] == 'build'],
            'source': sorted(f.name for f in stale.iterdir()),
            'autoload': conf.read_text() if conf.exists() else None}


r = run('status-fails', {'status_fails': True})
check(r, r['exit'] == 1 and 'dkms status failed' in r['out'], 'exits with status 1 when dkms status fails')
check(r, r['changing'] == [] and r['autoload'] is None and r['source'] == ['old-file.c'],
      f"stops before changing anything, got the calls {r['calls']}")

r = run('running-headers-missing', {'status': NVIDIA}, headers=(OTHER,))
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'], 'stops when the running kernel lacks headers')
check(r, r['changing'] == [] and r['autoload'] is None, f"stops before changing anything, got the calls {r['calls']}")

r = run('extra-argument', {'status': NVIDIA}, args=('now',))
check(r, r['exit'] == 2 and r['calls'] == [],
      f"exits with status 2 before running any command, got status {r['exit']} and the calls {r['calls']}")

r = run('success', {'status': NVIDIA})
check(r, r['exit'] == 0 and f'installed nvpcf-hook/{VER}' in r['out'], 'exits with status 0 and reports the install')
check(r, sorted(r['built_for']) == sorted([RUNNING, OTHER]), f"builds for both kernels, got {r['built_for']}")
check(r, 'dkms remove' not in r['calls'] and r['calls'].index('dkms add') < r['calls'].index('dkms build'),
      f"adds the source to DKMS before building, got the calls {r['calls']}")
check(r, 'nvpcf_hook.c' in r['source'] and 'dkms.conf' in r['source'] and 'old-file.c' not in r['source']
      and not any(f.startswith('check_') or f == 'tests' for f in r['source']),
      f"replaces the source copy with the hook's source files, got {r['source']}")
check(r, r['autoload'] == 'nvpcf_hook\n', f"makes the module load at boot, got {r['autoload']!r}")

r = run('reinstall', {'status': NVIDIA, 'hook_status': f'nvpcf-hook/{VER}, {RUNNING}, x86_64: installed\n'})
check(r, r['exit'] == 0 and r['calls'].index('dkms remove') < r['calls'].index('dkms add'),
      f"removes the earlier copy of the same version before adding the new one, got the calls {r['calls']}")

r = run('other-headers-missing', {'status': NVIDIA}, headers=(RUNNING,))
check(r, r['exit'] == 1 and f'headers are missing, not built: {OTHER}' in r['out'],
      'exits with status 1 and names the kernel it skipped')
check(r, r['built_for'] == [RUNNING], f"builds only for the running kernel, got {r['built_for']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
