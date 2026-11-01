#!/usr/bin/python3
"""Run build.sh against mocked commands and a fake /usr/src and /lib/modules
tree, as an ordinary user. It checks how build.sh works out the target
kernels, and that it stops before the dpkg diversion and the install when it
cannot work them out."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SRC = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix='build-test-'))
VER = '595.99.02'
RUNNING = '7.0.0-38-generic'
OTHER = '7.0.0-34-generic'
INSTALLED = (f'nvidia/{VER}, {OTHER}, x86_64: installed\n'
             f'nvidia/{VER}, {RUNNING}, x86_64: installed\n')
ADDED = f'nvidia/{VER}: added\n'
CHANGING = {'dpkg-divert --add', 'install', 'dkms build', 'dkms install'}

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
        if cfg.get('status_fails'):
            sys.exit(1)
        print(cfg['status'], end='')
    elif args[0] == 'install':
        k = args[args.index('-k') + 1]
        d = root / 'lib/modules' / k / 'updates/dkms'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'nvidia.ko').write_text('patched module')
elif cmd == 'uname':
    print(cfg['running'])
elif cmd == 'git':
    if 'clone' in args:
        if cfg.get('clone_fails'):
            sys.exit(128)
        dest = Path(args[-1])
        (dest / 'kernel-open').mkdir(parents=True)
        (dest / 'kernel-open/Makefile').write_text('same source\n')
        (dest / 'src/nvidia').mkdir(parents=True)
elif cmd == 'make':
    out = Path(args[args.index('-C') + 1]) / '_out/Linux_x86_64'
    out.mkdir(parents=True)
    (out / 'nv-kernel.o').write_text('patched object')
elif cmd == 'nm':
    print('0000000000000000 T RmDeferNvpcfNotifyIfIdle')
elif cmd == 'dpkg-divert':
    pass
elif cmd == 'install':
    Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
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
    for cmd in ('dkms', 'uname', 'git', 'make', 'nm', 'dpkg-divert', 'install'):
        (root / 'bin' / cmd).symlink_to(mock)
    src = root / f'usr/src/nvidia-{VER}'
    (src / 'nvidia').mkdir(parents=True)
    (src / 'Makefile').write_text('same source\n')
    (src / 'dkms.conf').write_text('')
    (src / 'nvidia/nv-kernel.o_binary').write_text('precompiled object')
    for k in headers:
        (root / 'lib/modules' / k / 'build').mkdir(parents=True)
    (root / 'config.json').write_text(json.dumps(dict({'running': RUNNING}, **cfg)))
    (root / 'calls.jsonl').write_text('')
    scripts = root / 'scripts'
    shutil.copytree(SRC / 'patches', scripts / 'patches')
    text = (SRC / 'build.sh').read_text()
    text = text.replace('/usr/src/nvidia-', f'{root}/usr/src/nvidia-').replace('/lib/modules/', f'{root}/lib/modules/')
    (scripts / 'build.sh').write_text(text)
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    p = subprocess.run(['/bin/bash', str(scripts / 'build.sh'), *args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    def label(c, a):
        if c == 'dkms':
            return f'dkms {a[0]}'
        if c == 'dpkg-divert':
            return 'dpkg-divert --add' if '--add' in a else 'dpkg-divert --list'
        return c
    named = [label(c, a) for c, a in calls]
    return {'case': name, 'exit': p.returncode, 'out': p.stdout, 'calls': named,
            'changing': [c for c in named if c in CHANGING],
            'built_for': [a[a.index('-k') + 1] for c, a in calls if c == 'dkms' and a[0] == 'build'],
            'object': (src / 'nvidia/nv-kernel.o_binary').read_text()}


r = run('status-fails', {'status_fails': True})
check(r, r['exit'] == 1 and 'dkms status failed' in r['out'], 'stops on a failed dkms status')
check(r, r['changing'] == [] and 'git' not in r['calls'], f"calls {r['calls']}")

r = run('added-only', {'status': ADDED, 'clone_fails': True})
check(r, f'target kernels: {RUNNING}\n' in r['out'], 'targets the running kernel')
check(r, r['exit'] != 0 and r['changing'] == [], f"exit {r['exit']}, changes {r['changing']}")

r = run('running-headers-missing', {'status': INSTALLED}, headers=(OTHER,))
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'], 'stops when the running kernel lacks headers')
check(r, r['changing'] == [] and 'git' not in r['calls'], f"calls {r['calls']}")

r = run('extra-argument', {'status': INSTALLED}, args=(VER, 'extra'))
check(r, r['exit'] == 2 and r['calls'] == [], f"exit {r['exit']}, calls {r['calls']}")

r = run('mistyped-option', {'status': INSTALLED}, args=('--chek',))
check(r, r['exit'] == 2 and r['changing'] == [] and 'git' not in r['calls'], f"exit {r['exit']}, calls {r['calls']}")

r = run('success', {'status': INSTALLED})
check(r, r['exit'] == 0 and '== done' in r['out'], 'exit 0 with the done line')
check(r, sorted(r['built_for']) == sorted([RUNNING, OTHER]), f"built for {r['built_for']}")
check(r, r['object'] == 'patched object' and 'dpkg-divert --add' in r['calls'], 'object installed behind a diversion')

r = run('other-headers-missing', {'status': INSTALLED}, headers=(RUNNING,))
check(r, r['exit'] == 1 and f'headers are missing: {OTHER}' in r['out'], 'exit 1 naming the skipped kernel')
check(r, r['built_for'] == [RUNNING], f"built for {r['built_for']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
