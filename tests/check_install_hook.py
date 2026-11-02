#!/usr/bin/python3
"""Run install-hook.sh as an ordinary user, against a mock dkms that keeps
state, and a throwaway directory in place of /usr/src, /lib/modules,
/var/lib/dkms, /usr/local/lib/nvpcf-hook, /etc/systemd/system and
/var/cache/nvpcf-hook.

The mock dkms follows /usr/sbin/dkms 3.0.11. It records which versions are
added, which are built and which is installed for each kernel, and keeps the
built and installed module files. build --force drops a version's existing
build for the kernel first. install --force uninstalls the same version first,
builds a missing build, copies the build over the kernel's installed file,
and then runs depmod. When depmod fails, it removes the file it copied, so
the kernel is left with no installed version. A case can also make install
fail before the copy, or leave a file with another build ID while it reports
success. remove uninstalls a version from every kernel that has
it installed, so a premature remove shows up as a lost module. A module file
holds its build ID as text, which the mock readelf reports. uname, modinfo,
readelf, systemctl and curl are mocks too. The copied load-hook.sh reads the
build IDs for install-hook.sh, and downloads into the throwaway cache through
the mock curl.

The cases check the kernels that get the new version, that a kernel keeps
its working module when the new version fails to build for it or its headers
are missing, and that a kernel whose install fails gets its previous version
back from DKMS's build of it. When that fails too, the previous version's
registration, source and build stay. An old version is removed only when no
kernel has it installed and no failed kernel had it. A build ID that cannot
be read fails the check instead of matching another unreadable one. A failing
dkms status, a non-canonical PACKAGE_VERSION, and a registered version that
DKMS ranks equal to or above the new one stop the script before it changes
anything. Every case that registers the new version checks that the new
version is the greatest one, with VER taken from /usr/sbin/dkms when it
exists.

One case builds hook/ with the real Kbuild between two runs, as hook/README.md
describes, so the second run sees the generated files, such as
nvpcf_hook.mod.c. The second run has to find the registered source unchanged,
and a Kbuild build of the registered source has to succeed. This case needs
the running kernel's headers and a compiler.

The fresh install also runs hook/tests/check_checker.py, nvpcf_hook's tests of
its checker, in a copy of the installed /usr/local/lib/nvpcf-hook, with -I and
-B as load-hook.sh runs the checker. So the installed check_nvidia.py has to
find its data file, expected_dispatcher.c, in the directory that
install-hook.sh filled."""
import hashlib
import json
import os
from pathlib import Path
import re
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
OLD = '0.0.1'
NEW = re.search(r'^PACKAGE_VERSION="(.*)"$', (SRC / 'hook/dkms.conf').read_text(), re.M).group(1)
SOURCES = re.search(r'^SOURCES=\((.*)\)$', (SRC / 'install-hook.sh').read_text(), re.M).group(1).split()
# NEW with every number padded to three digits: another version string with
# the same DKMS rank.
ALIAS = '.'.join(c.zfill(3) for c in NEW.split('.'))


def dkms_ver(v):
    """DKMS's VER: every number padded to three digits, compared as a string."""
    if Path('/usr/sbin/dkms').exists():
        return subprocess.run(['bash', '-c', 'source <(sed -n "/^VER()/,/^}/p" /usr/sbin/dkms); VER "$1"', '_', v],
                              capture_output=True, text=True, check=True).stdout.strip()
    s = re.sub(r'([^0-9])([0-9])', r'\1 \2', v)
    s = re.sub(r'([0-9])([^0-9])', r'\1 \2', s)
    s = re.sub(r' ([0-9]) ', r' 00\1 ', f' {s} ')
    return re.sub(r' ([0-9][0-9]) ', r' 0\1 ', s).replace(' ', '')


assert ALIAS != NEW and dkms_ver(ALIAS) == dkms_ver(NEW), (NEW, ALIAS)

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
def build(st, v, k):
    p = built_path(v, k)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'BUILDID={hashlib.sha1((v + k).encode()).hexdigest()} {v}\n')
    st['built'].append([v, k])
def uninstall(st, k):
    installed_path(k).unlink(missing_ok=True)
    st['installed'].pop(k, None)
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
                ks = sorted({k for v2, k in st['built'] if v2 == v} | {k for k, v2 in st['installed'].items() if v2 == v})
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
        if [v, k] in st['built']:
            built_path(v, k).unlink(missing_ok=True)
            st['built'].remove([v, k])
        if k in cfg.get('build_fails', []):
            save(st)
            sys.exit(10)
        build(st, v, k)
    elif op == 'install':
        v, k = opt('-v'), opt('-k')
        if st['installed'].get(k) == v:
            uninstall(st, k)
        if [v, k] not in st['built']:
            build(st, v, k)
        if k in cfg.get('install_fails', []):
            save(st)
            sys.exit(101)
        p = installed_path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(built_path(v, k), p)
        st['installed'][k] = v
        if k in cfg.get('wrong_file', []):
            p.write_text(f"BUILDID={hashlib.sha1(b'another build').hexdigest()} {v}\n")
        if k in cfg.get('depmod_fails', []) or [v, k] in cfg.get('depmod_fails_for', []):
            uninstall(st, k)
            save(st)
            sys.exit(6)
    elif op == 'remove':
        v = args[1].split('/')[1]
        for k, iv in list(st['installed'].items()):
            if iv == v:
                uninstall(st, k)
        for b in st['built']:
            if b[0] == v:
                built_path(*b).unlink(missing_ok=True)
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
    if cfg.get('readelf_fails'):
        sys.exit(1)
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


def kbuild(directory):
    """Build the module in directory with the running kernel's Kbuild."""
    return subprocess.run(['make', '-s', '-C', str(directory), 'modules'],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)


def run(name, cfg, headers=(RUNNING, OTHER), args=(), installed=None, runs=1, registered=None,
        package_version=None, between=None):
    """installed maps kernels to an old version that is built and installed
    for them before the run. registered maps versions that are only added to
    the text of their nvpcf_hook.c, or to None for hook/'s text.
    package_version replaces PACKAGE_VERSION in the copied hook/dkms.conf.
    between runs with the copied scripts directory after the first run."""
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
    for v, text in (registered or {}).items():
        if v not in st['added']:
            st['added'].append(v)
        d = root / f'usr/src/nvpcf-hook-{v}'
        d.mkdir(parents=True, exist_ok=True)
        for f in SOURCES:
            shutil.copy(SRC / 'hook' / f, d / f)
        if text is not None:
            (d / 'nvpcf_hook.c').write_text(text)
    (root / 'state.json').write_text(json.dumps(st))
    (root / 'config.json').write_text(json.dumps(dict({'running': RUNNING, 'status': NVIDIA}, **cfg)))
    (root / 'calls.jsonl').write_text('')
    (root / 'cache').mkdir()
    scripts = root / 'scripts'
    shutil.copytree(SRC / 'hook', scripts / 'hook', ignore=shutil.ignore_patterns('*.o', '*.ko', 'state_test'))
    if package_version is not None:
        conf = scripts / 'hook/dkms.conf'
        conf.write_text(re.sub(r'^PACKAGE_VERSION=".*"$', f'PACKAGE_VERSION="{package_version}"',
                               conf.read_text(), flags=re.M))
    shutil.copy(SRC / 'nvpcf-hook.service', scripts / 'nvpcf-hook.service')
    paths = (('/usr/local/lib/nvpcf-hook', f'{root}/usr/local/lib/nvpcf-hook'),
             ('/etc/systemd/system/', f'{root}/etc/systemd/system/'),
             ('/usr/src/', f'{root}/usr/src/'), ('/lib/modules/', f'{root}/lib/modules/'),
             ('/var/lib/dkms/', f'{root}/var/lib/dkms/'), ('/var/cache/nvpcf-hook', f'{root}/cache'))
    for f in ('install-hook.sh', 'load-hook.sh', 'lib.sh'):
        text = (SRC / f).read_text()
        for old, new in paths:
            text = text.replace(old, new)
        (scripts / f).write_text(text)
        (scripts / f).chmod(0o755)
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    exits = []
    for i in range(runs):
        if i == 1 and between:
            between(scripts)
        (root / 'calls.jsonl').write_text('')
        p = subprocess.run(['/bin/bash', str(scripts / 'install-hook.sh'), *args], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        exits.append(p.returncode)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    st = json.loads((root / 'state.json').read_text())
    named = [f'dkms {a[0]}' if c == 'dkms' else c for c, a in calls]
    lib = root / 'usr/local/lib/nvpcf-hook'
    registered_new = root / f'usr/src/nvpcf-hook-{NEW}'
    return {'case': name, 'exit': p.returncode, 'exits': exits, 'out': p.stdout, 'calls': named, 'state': st,
            'changing': [c for c in named if c in ('dkms add', 'dkms build', 'dkms install', 'dkms remove')],
            'removed': [a[1] for c, a in calls if c == 'dkms' and a[0] == 'remove'],
            'installs': [(a[a.index('-v') + 1], a[a.index('-k') + 1]) for c, a in calls if c == 'dkms' and a[0] == 'install'],
            'new': NEW if NEW in st['added'] else None,
            'latest': max(st['added'], key=dkms_ver) if st['added'] else None,
            'loader': sorted(f.name for f in lib.iterdir()) if lib.exists() else [],
            'unit': (root / 'etc/systemd/system/nvpcf-hook.service').exists(),
            'enabled': ['enable', 'nvpcf-hook.service'] in [a for c, a in calls if c == 'systemctl'],
            'archives': sorted(f.name for f in (root / 'cache').iterdir()),
            'sources': sorted(d.name for d in (root / 'usr/src').iterdir()) if (root / 'usr/src').exists() else [],
            'registered_new': sorted(f.name for f in registered_new.iterdir()) if registered_new.exists() else None,
            'root': root, 'scripts': scripts}


def latest_is_new(r):
    check(r, r['latest'] == NEW, f"leaves {NEW} as the greatest registered version for DKMS, got {r['latest']}")


def keeps_old(r):
    check(r, OLD in r['state']['added'] and f'nvpcf-hook-{OLD}' in r['sources'] and r['removed'] == []
          and all(built_path_exists(r, OLD, k) for k in (RUNNING, OTHER)),
          f"keeps {OLD}'s registration, source and builds, got {r['state']['added']}, {r['sources']} and {r['removed']}")


def built_path_exists(r, v, k):
    return (r['root'] / 'var/lib/dkms/nvpcf-hook' / v / k / 'x86_64/module/nvpcf_hook.ko').exists()


r = run('fresh-install', {})
check(r, r['exit'] == 0 and r['new'] is not None, f"exits with status 0 and adds a new version, got status {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got {r['state']['installed']}")
check(r, r['registered_new'] == sorted(SOURCES),
      f"registers exactly the source files that DKMS's build reads, got {r['registered_new']}")
check(r, r['loader'] == ['check_nvidia.py', 'expected_dispatcher.c', 'load-hook.sh', 'nvpcf-hookctl']
      and r['unit'] and r['enabled'],
      f"installs the loader, its checker with the checker's data file and the control client, and enables the boot unit, got {r['loader']}")
lib = r['root'] / 'usr/local/lib/nvpcf-hook'
data = lib / 'expected_dispatcher.c'
check(r, data.is_file() and data.read_bytes() == (SRC / 'hook/tests/expected_dispatcher.c').read_bytes()
      and data.stat().st_mode & 0o777 == 0o644,
      'installs an unchanged copy of hook/tests/expected_dispatcher.c with mode 0644')
layout = BASE / 'installed-checker'
shutil.copytree(lib, layout)
shutil.copy(SRC / 'hook/tests/check_checker.py', layout)
p = subprocess.run(['python3', '-I', '-B', str(layout / 'check_checker.py')],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
check(r, p.returncode == 0,
      f"the installed checker passes nvpcf_hook's checker tests with its installed data file, got status {p.returncode}:\n{p.stdout}")
check(r, r['archives'] == ['open-gpu-kernel-modules-615.71.09.tar.gz'],
      f"downloads NVIDIA's source archive for the installed version, got {r['archives']}")
check(r, r['new'] == NEW, f"registers PACKAGE_VERSION from hook/dkms.conf, {NEW}, got {r['state']['added']}")
latest_is_new(r)

r = run('upgrade', {}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 0 and r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got status {r['exit']} and {r['state']['installed']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'] and OLD not in r['state']['added']
      and f'nvpcf-hook-{OLD}' not in r['sources'],
      f"removes the old version and its source once no kernel has it installed, got {r['removed']}")
c = r['calls']
check(r, c.index('dkms remove') > max(i for i, x in enumerate(c) if x == 'dkms install'),
      f"removes the old version only after the installs, got the calls {c}")
latest_is_new(r)

r = run('build-fails-first', {'build_fails': [OTHER]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'{OTHER}: building nvpcf-hook/{NEW}; nvpcf-hook/{OLD} stays installed' in r['out'],
      f"exits with status 1 and names the failure, got {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: OLD},
      f"keeps the old module on the kernel whose build failed, got {r['state']['installed']}")
check(r, r['removed'] == [] and OLD in r['state']['added'], 'keeps the old version, which a kernel still has installed')
latest_is_new(r)

r = run('all-builds-fail', {'build_fails': [RUNNING, OTHER]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and r['state']['installed'] == {RUNNING: OLD, OTHER: OLD} and r['removed'] == [],
      f"leaves both kernels with their working modules, got {r['state']['installed']}")

r = run('install-fails-before-copy', {'install_fails': [RUNNING]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and r['state']['installed'] == {RUNNING: OLD, OTHER: NEW},
      f"leaves the old module on the kernel whose install failed before copying, got {r['state']['installed']}")
check(r, f'{RUNNING} still has nvpcf-hook/{OLD}' in r['out'] and (OLD, RUNNING) not in r['installs'],
      f"checks the old module and leaves it in place, got the installs {r['installs']}")
keeps_old(r)

r = run('depmod-fails-after-copy', {'depmod_fails_for': [[NEW, RUNNING]]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and r['state']['installed'] == {RUNNING: OLD, OTHER: NEW},
      f"installs the old version again on the kernel whose new module DKMS removed, got {r['state']['installed']}")
check(r, f'installed nvpcf-hook/{OLD} for {RUNNING} again' in r['out']
      and r['installs'] == [(NEW, OTHER), (NEW, RUNNING), (OLD, RUNNING)],
      f"installs the old version again only on the failed kernel, and says so, got the installs {r['installs']}")
keeps_old(r)
latest_is_new(r)

r = run('reinstall-fails', {'depmod_fails': [RUNNING]}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and RUNNING not in r['state']['installed'] and r['state']['installed'].get(OTHER) == NEW,
      f"leaves the kernel whose installs both failed without a module, got {r['state']['installed']}")
check(r, f'installing nvpcf-hook/{OLD} again failed' in r['out'] and f'keeping nvpcf-hook/{OLD}' in r['out'],
      'reports the failed reinstall and keeps the old version')
keeps_old(r)

r = run('build-id-unreadable', {'readelf_fails': True}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'{RUNNING}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out']
      and f'{OTHER}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out'],
      'fails the check when no build ID can be read, instead of matching two empty IDs')
keeps_old(r)

r = run('other-headers-missing', {}, headers=(RUNNING,), installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'headers are missing, not built: {OTHER}' in r['out'],
      f"exits with status 1 and names the skipped kernel, got {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: OLD} and r['removed'] == [],
      f"keeps the skipped kernel's module and the old version, got {r['state']['installed']}")
latest_is_new(r)

r = run('newer-registered', {}, registered={'0.2': None}, installed={RUNNING: OLD})
check(r, r['exit'] == 1 and 'nvpcf-hook/0.2 is registered, and DKMS ranks it equal to or above' in r['out']
      and r['changing'] == [],
      f"refuses before changing anything when DKMS would build a greater version for the next kernel, got {r['changing']}")

r = run('equal-rank-registered', {}, registered={ALIAS: None}, installed={RUNNING: ALIAS})
check(r, r['exit'] == 1 and f'nvpcf-hook/{ALIAS} is registered, and DKMS ranks it equal to or above' in r['out']
      and r['changing'] == [],
      f"refuses before changing anything when {ALIAS} has the same DKMS rank as {NEW}, got {r['changing']}")

r = run('noncanonical-version', {}, package_version=ALIAS)
check(r, r['exit'] == 1 and 'without leading zeros' in r['out'] and r['calls'] == [],
      f"refuses PACKAGE_VERSION {ALIAS} before running any command, got {r['calls']}")

r = run('same-version-other-source', {}, registered={NEW: '/* other source */\n'})
check(r, r['exit'] == 1 and 'registered with source that differs' in r['out'] and r['changing'] == [],
      f"refuses before changing anything when its version is registered with other source, got {r['changing']}")

r = run('again', {}, runs=2)
check(r, r['exit'] == 0 and r['changing'] == [],
      f"changes nothing on a second run with the same source, got status {r['exit']} and {r['changing']}")

r = run('wrong-file-stays', {'wrong_file': [RUNNING]}, runs=2)
check(r, r['exits'] == [1, 1] and f'{RUNNING}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out'],
      f"fails both runs while modprobe finds another file than DKMS's build, got the exit statuses {r['exits']}")
check(r, r['installs'] == [(NEW, RUNNING)],
      f"checks the kernel that DKMS lists as installed again, and installs it again, got the installs {r['installs']}")


def clear_wrong_file(scripts):
    conf = scripts.parent / 'config.json'
    c = json.loads(conf.read_text())
    del c['wrong_file']
    conf.write_text(json.dumps(c))


r = run('wrong-file-repaired', {'wrong_file': [RUNNING]}, runs=2, between=clear_wrong_file)
installed_file = r['root'] / 'lib/modules' / RUNNING / 'updates/dkms/nvpcf_hook.ko'
built_file = r['root'] / 'var/lib/dkms/nvpcf-hook' / NEW / RUNNING / 'x86_64/module/nvpcf_hook.ko'
check(r, r['exits'] == [1, 0] and r['installs'] == [(NEW, RUNNING)]
      and installed_file.read_text() == built_file.read_text(),
      f"repairs the kernel on the second run and checks it, got the exit statuses {r['exits']} and the installs {r['installs']}")

built = {}
def build_in_hook(scripts):
    built['result'] = kbuild(scripts / 'hook')
    built['generated'] = (scripts / 'hook/nvpcf_hook.mod.c').exists()

r = run('kbuild-between-runs', {}, runs=2, between=build_in_hook)
check(r, built['result'].returncode == 0 and built['generated'],
      f"builds hook/ with Kbuild between the runs, got status {built['result'].returncode}:\n{built['result'].stdout}")
check(r, r['exit'] == 0 and r['changing'] == [],
      f"changes nothing on the second run after the build, got status {r['exit']} and {r['changing']}")
check(r, r['registered_new'] == sorted(SOURCES),
      f"keeps exactly the authored source files registered, got {r['registered_new']}")
copy = BASE / 'registered-source-build'
shutil.copytree(r['root'] / f'usr/src/nvpcf-hook-{NEW}', copy)
p = kbuild(copy)
check(r, p.returncode == 0 and (copy / 'nvpcf_hook.ko').exists(),
      f"the registered source builds with Kbuild, got status {p.returncode}:\n{p.stdout}")

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
