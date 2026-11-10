#!/usr/bin/python3
"""Run build.sh as an ordinary user, with mock commands and, for each case, a
throwaway directory in place of /usr/src, /lib/modules and /boot.

The cases check the kernels that build.sh builds the modules for. They also
check that build.sh stops before it adds the dpkg diversion or installs the
patched driver core when dkms status fails or the running kernel's headers
are missing.

Every kernel in a case has modules of its own in /lib/modules/<kernel>/kernel
unless the case leaves them out. A kernel that DKMS lists without headers and
without such modules was removed: build.sh leaves it out, does not count it as
skipped, and names it. A kernel with its own modules and without headers is
skipped, and the run exits 1.

The mock update-initramfs writes a new image, named after the old one, that
lists the nvidia modules in the kernel's updates/dkms at that moment, and
renames it over the old one, as update-initramfs does. build.sh has to run it
for every rebuilt kernel that has an image in /boot, after that kernel's dkms
install, and has to stop with status 1 and name the kernel when it fails or
exits 0 without writing a new image, as it does when /boot is read-only. A
kernel without an image gets no call."""
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
REMOVED = '6.8.0-147-generic'  # DKMS lists it, but its headers and modules are gone
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
elif cmd == 'update-initramfs':
    k = args[args.index('-k') + 1]
    if k in cfg.get('initramfs_fails', []):
        print(f'update-initramfs: failed for /boot/initrd.img-{k} with 1.', file=sys.stderr)
        sys.exit(1)
    if k in cfg.get('initramfs_unwritten', []):
        print('W: /boot is ro mounted.', file=sys.stderr)
        sys.exit(0)
    d = root / 'lib/modules' / k / 'updates/dkms'
    image = root / 'boot' / f'initrd.img-{k}'
    new = image.with_name(image.name + '.new')
    new.write_text(''.join(f'{m.name}: {m.read_text()}\n' for m in sorted(d.glob('nvidia*.ko'))) if d.is_dir() else '')
    new.replace(image)
'''

failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def run(name, cfg, headers=(RUNNING, OTHER), args=(), modules=(RUNNING, OTHER), images=(RUNNING, OTHER)):
    """headers, modules and images name the kernels that get headers, modules
    of their own in /lib/modules/<kernel>/kernel, and an initramfs image."""
    root = BASE / name
    (root / 'bin').mkdir(parents=True)
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('dkms', 'uname', 'git', 'make', 'nm', 'dpkg-divert', 'install', 'update-initramfs'):
        (root / 'bin' / cmd).symlink_to(mock)
    src = root / f'usr/src/nvidia-{VER}'
    (src / 'nvidia').mkdir(parents=True)
    (src / 'Makefile').write_text('same source\n')
    (src / 'dkms.conf').write_text('')
    (src / 'nvidia/nv-kernel.o_binary').write_text('precompiled object')
    for k in headers:
        (root / 'lib/modules' / k / 'build').mkdir(parents=True)
    for k in modules:
        (root / 'lib/modules' / k / 'kernel').mkdir(parents=True)
    (root / 'boot').mkdir()
    for k in images:
        (root / 'boot' / f'initrd.img-{k}').write_text('the previous modules\n')
    (root / 'config.json').write_text(json.dumps(dict({'running': RUNNING}, **cfg)))
    (root / 'calls.jsonl').write_text('')
    scripts = root / 'scripts'
    shutil.copytree(SRC / 'patches', scripts / 'patches')
    text = (SRC / 'build.sh').read_text()
    text = text.replace('/usr/src/nvidia-', f'{root}/usr/src/nvidia-').replace('/lib/modules/', f'{root}/lib/modules/')
    text = text.replace('/boot/', f'{root}/boot/')
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
            'initramfs': [a for c, a in calls if c == 'update-initramfs'],
            # The dkms installs and image updates in the order they ran.
            'order': [(label(c, a), a[a.index('-k') + 1]) for c, a in calls
                      if (c == 'dkms' and a[0] == 'install') or c == 'update-initramfs'],
            'images': {f.name.removeprefix('initrd.img-'): f.read_text() for f in (root / 'boot').iterdir()},
            'object': (src / 'nvidia/nv-kernel.o_binary').read_text()}


r = run('status-fails', {'status_fails': True})
check(r, r['exit'] == 1 and 'dkms status failed' in r['out'], 'exits with status 1 when dkms status fails')
check(r, r['changing'] == [] and 'git' not in r['calls'],
      f"stops before cloning or changing anything, got the calls {r['calls']}")

r = run('added-only', {'status': ADDED, 'clone_fails': True})
check(r, f'target kernels: {RUNNING}\n' in r['out'], 'targets the running kernel')
check(r, r['exit'] != 0 and r['changing'] == [],
      f"fails before changing anything, got exit status {r['exit']} and the changes {r['changing']}")

r = run('running-headers-missing', {'status': INSTALLED}, headers=(OTHER,))
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'], 'stops when the running kernel lacks headers')
check(r, r['changing'] == [] and 'git' not in r['calls'],
      f"stops before cloning or changing anything, got the calls {r['calls']}")

r = run('extra-argument', {'status': INSTALLED}, args=(VER, 'extra'))
check(r, r['exit'] == 2 and r['calls'] == [],
      f"exits with status 2 before running any command, got status {r['exit']} and the calls {r['calls']}")

r = run('mistyped-option', {'status': INSTALLED}, args=('--chek',))
check(r, r['exit'] == 2 and r['changing'] == [] and 'git' not in r['calls'],
      f"exits with status 2 before cloning or changing anything, got status {r['exit']} and the calls {r['calls']}")

r = run('success', {'status': INSTALLED})
check(r, r['exit'] == 0 and r['out'].rstrip().endswith('== done; the patched modules load at the next boot'),
      'exits with status 0 and says that the patched modules load at the next boot')
check(r, sorted(r['built_for']) == sorted([RUNNING, OTHER]), f"builds for both kernels, got {r['built_for']}")
check(r, r['object'] == 'patched object' and 'dpkg-divert --add' in r['calls'],
      'diverts Ubuntu\'s nv-kernel.o_binary and installs the patched object in its place')
check(r, sorted(r['initramfs']) == sorted([['-u', '-k', RUNNING], ['-u', '-k', OTHER]]),
      f"runs update-initramfs -u -k for each kernel, got {r['initramfs']}")
for k in (RUNNING, OTHER):
    check(r, r['order'].index(('dkms install', k)) < r['order'].index(('update-initramfs', k)),
          f"updates {k}'s initramfs after its dkms install, got the order {r['order']}")
    check(r, r['images'][k] == 'nvidia.ko: patched module\n',
          f"leaves {k}'s initramfs with the patched module, got {r['images'][k]!r}")

r = run('kernel-without-initramfs', {'status': INSTALLED}, images=(RUNNING,))
check(r, r['exit'] == 0 and r['initramfs'] == [['-u', '-k', RUNNING]],
      f"exits with status 0 and updates only the image that exists, got status {r['exit']} and {r['initramfs']}")
check(r, f'{OTHER} does not have an initramfs at' in r['out'] and OTHER not in r['images'],
      'says that the other kernel has no image, and does not create one')

r = run('initramfs-fails', {'status': INSTALLED, 'initramfs_fails': [RUNNING, OTHER]})
check(r, r['exit'] == 1 and 'update-initramfs failed for' in r['out'] and 'which the next boot of' in r['out'],
      f"exits with status 1 and says which kernel's next boot loads the previous modules, got status {r['exit']}")
check(r, len(r['initramfs']) == 1 and r['images'][r['initramfs'][0][-1]] == 'the previous modules\n',
      f"stops at the first failure and leaves that image as it was, got {r['initramfs']} and {r['images']}")
check(r, '== done' not in r['out'], 'does not report that it is done')

r = run('initramfs-not-written', {'status': INSTALLED, 'initramfs_unwritten': [RUNNING, OTHER]})
check(r, r['exit'] == 1 and 'did not write a new' in r['out'] and 'W: /boot is ro mounted.' in r['out'],
      f"exits with status 1 when update-initramfs exits 0 without writing, and shows its output, got status {r['exit']}")

r = run('removed-kernel', {'status': INSTALLED + f'nvidia/{VER}, {REMOVED}, x86_64: installed\n'})
check(r, r['exit'] == 0 and sorted(r['built_for']) == sorted([RUNNING, OTHER]),
      f"exits with status 0 without building for the removed kernel, got status {r['exit']} and {r['built_for']}")
check(r, 'leaving out removed kernels' in r['out'] and REMOVED in r['out'] and 'headers are missing' not in r['out'],
      'names the removed kernel and does not count it as skipped')
check(r, all(a[-1] != REMOVED for a in r['initramfs']), f"does not update an image for it, got {r['initramfs']}")

r = run('running-kernel-without-modules', {'status': INSTALLED}, headers=(OTHER,), modules=(OTHER,))
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'] and r['changing'] == [],
      f"still stops when the running kernel lacks headers, also without modules of its own, got status {r['exit']}")

r = run('other-headers-missing', {'status': INSTALLED}, headers=(RUNNING,))
check(r, r['exit'] == 1 and f'headers are missing: {OTHER}' in r['out'],
      'exits with status 1 and names the kernel it skipped')
check(r, r['built_for'] == [RUNNING], f"builds only for the running kernel, got {r['built_for']}")
check(r, r['initramfs'] == [['-u', '-k', RUNNING]] and r['images'][OTHER] == 'the previous modules\n',
      f"updates only the rebuilt kernel's image, got {r['initramfs']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
