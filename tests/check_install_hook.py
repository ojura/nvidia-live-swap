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
the kernel is left without an installed version. A case can also make install
fail before the copy, or leave a file with another build ID while it reports
success. remove uninstalls a version from every kernel that has
it installed, so a premature remove shows up as a lost module. A module file
holds its build ID as text, which the mock objcopy turns into the note that
GNU ld writes. uname, modinfo, objcopy, systemctl and curl are mocks too. The
copied load-hook.sh reads build IDs for install-hook.sh and downloads into the
throwaway cache through the mock curl.

The cases check the kernels that get the new version, that a kernel keeps
its working module when the new version fails to build for it or its headers
are missing, and that a kernel whose install fails gets its previous version
back from DKMS's build of it. When that fails too, the previous version's
registration, source and build stay. An older version that is not installed for
any kernel is removed only after every kernel in the run has the new version
installed and checked. A failed or skipped kernel keeps the older
registrations, source and builds, also on a second run that fails again for a
kernel that the first run left without a module. A build ID that cannot be
read fails the check instead of matching another unreadable one. A failing
dkms status, a PACKAGE_VERSION that is not dotted numbers from 0 to 999
without leading zeros, and a registered version that DKMS ranks equal to or
above the new one stop the script before it changes anything. Every case that
registers the new version checks that the new version is the greatest one,
with VER taken from /usr/sbin/dkms when it exists.

With --no-enable, the script installs the unit without enabling it, and
leaves a unit that is enabled already as it is; a case covers each. An empty
argument, also next to --no-enable, and an extra argument make the script
exit with status 2 before it runs any command.

The source-directory cases cover a directory that matches and one that
differs, a source file that is a symbolic or hard link to an identical file,
extra names with a space and with a newline that read like source names, a
listing and a file read that fail, a file that another process creates at the
directory's path just before mkdir, a failed copy, and an older version's
source directory with an extra file. mkdir, cp, find and cmp are mocks only in
the cases that need them. A running kernel without headers passes when it has
the version installed and checked, and stops the script otherwise.

One case builds hook/ with the real Kbuild between two installer runs, so the
second run sees generated files such as nvpcf_hook.mod.c. The second run has to
find the registered source unchanged, and a Kbuild build of the registered
source has to succeed. This case needs the running kernel's headers and a
compiler.

The fresh install checks that every file that lib.sh lists in HOOK_TOOLS is
installed unchanged, with its mode. It also runs hook/tests/check_checker.py,
nvpcf_hook's tests of its checker, in a copy of the installed
/usr/local/lib/nvpcf-hook, with -I and -B as load-hook.sh runs the checker,
and runs that installed check_nvidia.py with --kernel-source on each NVIDIA
kernel-open tree in /usr/src. So the installed checker has to find its
references, expected_dispatcher.c and expected_procfs_wrappers.h, in the
directory that install-hook.sh filled. This needs at least one
/usr/src/nvidia-<version> tree."""
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
THIRD = '6.11.0-29-generic'  # a kernel that DKMS lists only for nvpcf-hook
NVIDIA = (f'nvidia/615.71.09, {OTHER}, x86_64: installed\n'
          f'nvidia/615.71.09, {RUNNING}, x86_64: installed\n')
OLD = '0.0.1'
NEW = re.search(r'^PACKAGE_VERSION="(.*)"$', (SRC / 'hook/dkms.conf').read_text(), re.M).group(1)
SOURCES = re.search(r'^SOURCES=\((.*)\)$', (SRC / 'install-hook.sh').read_text(), re.M).group(1).split()
# The repository files that install-hook.sh installs, as lib.sh lists them.
TOOLS = re.search(r'^HOOK_TOOLS=\((.*?)\)$', (SRC / 'lib.sh').read_text(), re.M | re.S).group(1).split()
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
import hashlib, json, os, re, shutil, struct, sys
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
        if not conf.exists() or f'PACKAGE_VERSION="{v}"' not in conf.read_text() or cfg.get('add_fails'):
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
elif cmd == 'systemctl':
    if args[:1] == ['is-enabled']:
        print('enabled' if cfg.get('unit_enabled') else 'disabled')
        sys.exit(0 if cfg.get('unit_enabled') else 1)
elif cmd == 'uname':
    print(cfg['running'])
elif cmd == 'modinfo':
    p = installed_path(opt('-k'))
    if not p.exists():
        sys.exit(1)
    print(p)
elif cmd == 'objcopy':
    # A file without the section: objcopy exits with status 0 and writes an
    # empty file.
    out = Path(args[-1])
    if cfg.get('build_id_section_missing'):
        out.write_bytes(b'')
    else:
        desc = bytes.fromhex(Path(args[-2]).read_text().split('BUILDID=')[1].split()[0])
        out.write_bytes(struct.pack('<III', 4, len(desc), 3) + b'GNU\0' + desc)
elif cmd == 'curl':
    out = Path(args[args.index('-o') + 1])
    if cfg.get('fetch_fails'):
        sys.exit(22)
    out.write_text('archive')
elif cmd == 'mkdir':
    # Another process creates a file at the source directory's path between
    # install-hook.sh's check and its mkdir; then the real mkdir runs.
    target = Path(args[-1])
    if target.name == cfg.get('mkdir_race') and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('another process made this\n')
    os.execv('/bin/mkdir', ['mkdir', *args])
elif cmd == 'find':
    # The listing of the source directory fails, as with an I/O error.
    if Path(args[0]).name == cfg.get('find_fails'):
        print(f'find: {args[0]}: Input/output error', file=sys.stderr)
        sys.exit(1)
    os.execv('/usr/bin/find', ['find', *args])
elif cmd == 'cmp':
    # Reading a source file fails, which cmp reports with status 2.
    if Path(args[-1]).name == cfg.get('cmp_fails'):
        print(f'cmp: {args[-1]}: Input/output error', file=sys.stderr)
        sys.exit(2)
    os.execv('/usr/bin/cmp', ['cmp', *args])
elif cmd == 'cp':
    # Copy the first file, then fail, as a full disk would.
    sources, dest = args[args.index('--') + 1:-1], Path(args[-1])
    shutil.copy(sources[0], dest / Path(sources[0]).name)
    sys.exit(1)
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
        package_version=None, between=None, unregistered=None, prepare=None, mocks=()):
    """installed maps kernels to an old version that is built and installed
    for them before the run. registered maps versions that are only added to
    the text of their nvpcf_hook.c, or to None for hook/'s text.
    package_version replaces PACKAGE_VERSION in the copied hook/dkms.conf.
    between(scripts, i) runs before every run i after the first, with the
    copied scripts directory. unregistered maps a version to the files that
    its /usr/src directory gets without a DKMS registration: hook/'s source
    with the given files replaced, or deleted for None, or to 'file' for a
    regular file in place of the directory. prepare(root) runs once the
    throwaway tree exists. mocks names more commands for the mock, such as
    mkdir or cp."""
    root = BASE / name
    (root / 'bin').mkdir(parents=True)
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('dkms', 'uname', 'modinfo', 'objcopy', 'systemctl', 'curl', *mocks):
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
    for v, files in (unregistered or {}).items():
        d = root / f'usr/src/nvpcf-hook-{v}'
        d.parent.mkdir(parents=True, exist_ok=True)
        if files == 'file':
            d.write_text('not a directory\n')
            continue
        d.mkdir()
        for f in SOURCES:
            shutil.copy(SRC / 'hook' / f, d / f)
        for f, text in files.items():
            if text is None:
                (d / f).unlink()
            else:
                (d / f).write_text(text)
    for v, text in (registered or {}).items():
        if v not in st['added']:
            st['added'].append(v)
        d = root / f'usr/src/nvpcf-hook-{v}'
        d.mkdir(parents=True, exist_ok=True)
        for f in SOURCES:
            shutil.copy(SRC / 'hook' / f, d / f)
        if text is not None:
            (d / 'nvpcf_hook.c').write_text(text)
    if prepare:
        prepare(root)
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
        if i > 0 and between:
            between(scripts, i)
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
            'systemctl': [a for c, a in calls if c == 'systemctl'],
            'archives': sorted(f.name for f in (root / 'cache').iterdir()),
            'sources': sorted(d.name for d in (root / 'usr/src').iterdir()) if (root / 'usr/src').exists() else [],
            'registered_new': sorted(f.name for f in registered_new.iterdir()) if registered_new.is_dir() else None,
            'root': root, 'scripts': scripts}


def latest_is_new(r):
    check(r, r['latest'] == NEW, f"leaves {NEW} as the greatest registered version for DKMS, got {r['latest']}")


def keeps_old(r):
    check(r, OLD in r['state']['added'] and f'nvpcf-hook-{OLD}' in r['sources'] and r['removed'] == []
          and all(built_path_exists(r, OLD, k) for k in (RUNNING, OTHER)),
          f"keeps {OLD}'s registration, source and builds, got {r['state']['added']}, {r['sources']} and {r['removed']}")


def built_path_exists(r, v, k):
    return (r['root'] / 'var/lib/dkms/nvpcf-hook' / v / k / 'x86_64/module/nvpcf_hook.ko').exists()


def clearing(key, before_run):
    """A between function that removes a fault from the case's config before
    run number before_run, counted from 0."""
    def clear(scripts, i):
        if i == before_run:
            conf = scripts.parent / 'config.json'
            c = json.loads(conf.read_text())
            del c[key]
            conf.write_text(json.dumps(c))
    return clear


r = run('fresh-install', {})
check(r, r['exit'] == 0 and r['new'] is not None, f"exits with status 0 and adds a new version, got status {r['exit']}")
check(r, r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got {r['state']['installed']}")
check(r, r['registered_new'] == sorted(SOURCES),
      f"registers exactly the source files that DKMS's build reads, got {r['registered_new']}")
check(r, r['loader'] == sorted(Path(f).name for f in TOOLS) and r['unit'] and r['enabled'],
      f"installs the files that lib.sh lists in HOOK_TOOLS, and enables the boot unit, got {r['loader']}")
lib = r['root'] / 'usr/local/lib/nvpcf-hook'
for f in TOOLS:
    # The copied scripts are what install-hook.sh installs from; only their
    # paths differ from the repository's files.
    copy, source = lib / Path(f).name, r['scripts'] / f
    mode = 0o755 if os.access(source, os.X_OK) else 0o644
    check(r, copy.is_file() and copy.read_bytes() == source.read_bytes() and copy.stat().st_mode & 0o777 == mode,
          f'installs an unchanged copy of {f} with mode {mode:o}')
layout = BASE / 'installed-checker'
shutil.copytree(lib, layout)
shutil.copy(SRC / 'hook/tests/check_checker.py', layout)
p = subprocess.run(['python3', '-I', '-B', str(layout / 'check_checker.py')],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
check(r, p.returncode == 0,
      f"the installed checker passes nvpcf_hook's checker tests with its installed references, got status {p.returncode}:\n{p.stdout}")
trees = sorted(d for d in Path('/usr/src').glob('nvidia-*') if (d / 'nvidia/nv-procfs.c').is_file())
check(r, trees != [], 'finds an NVIDIA kernel-open tree in /usr/src, which this case needs')
for tree in trees:
    p = subprocess.run(['python3', '-I', '-B', str(layout / 'check_nvidia.py'), '--kernel-source', str(tree)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    check(r, p.returncode == 0, f"the installed checker passes {tree} with its installed references, got status {p.returncode}:\n{p.stdout}")
check(r, r['archives'] == ['open-gpu-kernel-modules-615.71.09.tar.gz'],
      f"downloads NVIDIA's source archive for the installed version, got {r['archives']}")
check(r, r['new'] == NEW, f"registers PACKAGE_VERSION from hook/dkms.conf, {NEW}, got {r['state']['added']}")
latest_is_new(r)

r = run('upgrade', {}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 0 and r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the new version for both kernels, got status {r['exit']} and {r['state']['installed']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'] and OLD not in r['state']['added']
      and f'nvpcf-hook-{OLD}' not in r['sources'],
      f"removes the old version and its source once it is not installed for any kernel, got {r['removed']}")
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

r = run('persistent-failed-rollback-retry', {'depmod_fails': [RUNNING]}, installed={RUNNING: OLD, OTHER: OLD}, runs=2)
check(r, r['exits'] == [1, 1] and RUNNING not in r['state']['installed'] and r['state']['installed'].get(OTHER) == NEW,
      f"fails both runs and leaves the failed kernel without a module, got {r['exits']} and {r['state']['installed']}")
check(r, f'keeping nvpcf-hook/{OLD} until every kernel' in r['out']
      and sorted(r['state']['added']) == sorted([OLD, NEW]),
      f"keeps {OLD} on the second run too, although it is not installed for any kernel, got {r['state']['added']}")
keeps_old(r)

def headers_gone(gone_before, back_before=None):
    """A between function that removes OTHER's headers and the depmod fault
    before run gone_before, and puts the headers back before run back_before."""
    def step(scripts, i):
        root = scripts.parent
        if i == gone_before:
            shutil.rmtree(root / 'lib/modules' / OTHER / 'build')
            conf = root / 'config.json'
            c = json.loads(conf.read_text())
            del c['depmod_fails']
            conf.write_text(json.dumps(c))
        if i == back_before:
            (root / 'lib/modules' / OTHER / 'build').mkdir()
    return step


r = run('failed-then-skipped', {'depmod_fails': [OTHER]}, installed={RUNNING: OLD, OTHER: OLD}, runs=2,
        between=headers_gone(1))
check(r, r['exits'] == [1, 1] and OTHER not in r['state']['installed'] and r['state']['installed'].get(RUNNING) == NEW,
      f"fails both runs and leaves the skipped kernel without a module, got {r['exits']} and {r['state']['installed']}")
check(r, f'headers are missing, not built: {OTHER}' in r['out'] and f'keeping nvpcf-hook/{OLD} until every kernel' in r['out'],
      f"keeps {OLD} while the kernel that needs it is skipped")
keeps_old(r)

r = run('failed-skipped-then-repaired', {'depmod_fails': [OTHER]}, installed={RUNNING: OLD, OTHER: OLD}, runs=3,
        between=headers_gone(1, 2))
check(r, r['exits'] == [1, 1, 0] and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW},
      f"installs the new version on the kernel once its headers are back, got {r['exits']} and {r['state']['installed']}")
c = r['calls']
check(r, r['installs'] == [(NEW, OTHER)] and r['removed'] == [f'nvpcf-hook/{OLD}']
      and c.index('dkms remove') > c.index('dkms install') and OLD not in r['state']['added'],
      f"installs and checks the repaired kernel before it removes {OLD}, got the installs {r['installs']} and the calls {c}")

r = run('hook-only-kernel', {}, headers=(RUNNING, OTHER, THIRD), installed={RUNNING: OLD, OTHER: OLD, THIRD: OLD})
check(r, r['exit'] == 0 and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW, THIRD: NEW},
      f"installs the new version also for a kernel that DKMS lists only for nvpcf-hook, got {r['state']['installed']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'], f"removes {OLD} once all three kernels have the new version, got {r['removed']}")

r = run('hook-only-kernel-without-headers', {}, installed={RUNNING: OLD, OTHER: OLD, THIRD: OLD})
check(r, r['exit'] == 1 and f'headers are missing, not built: {THIRD}' in r['out']
      and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW, THIRD: OLD} and r['removed'] == [],
      f"counts the kernel that DKMS lists only for nvpcf-hook as skipped, and keeps {OLD}, got {r['state']['installed']}")

r = run('installed-kernel-without-headers', {}, headers=(RUNNING,), registered={NEW: None},
        installed={RUNNING: OLD, OTHER: NEW})
check(r, r['exit'] == 0 and f'nvpcf-hook/{NEW} is installed for {OTHER} already' in r['out']
      and 'headers are missing' not in r['out'] and r['installs'] == [(NEW, RUNNING)],
      f"checks the installed module of a kernel without headers instead of skipping it, got status {r['exit']} and {r['installs']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'], f"removes {OLD} once both kernels have the new version, got {r['removed']}")

r = run('reinstall-fails-then-recovers', {'depmod_fails': [RUNNING]}, installed={RUNNING: OLD, OTHER: OLD}, runs=3,
        between=clearing('depmod_fails', 2))
check(r, r['exits'] == [1, 1, 0] and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW},
      f"installs the new version everywhere once depmod works again, got {r['exits']} and {r['state']['installed']}")
check(r, r['removed'] == [f'nvpcf-hook/{OLD}'] and OLD not in r['state']['added'] and f'nvpcf-hook-{OLD}' not in r['sources'],
      f"removes {OLD} only in the run that installs the new version everywhere, got {r['removed']}")

r = run('build-id-unreadable', {'build_id_section_missing': True}, installed={RUNNING: OLD, OTHER: OLD})
check(r, r['exit'] == 1 and f'{RUNNING}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out']
      and f'{OTHER}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out'],
      'fails the check when the build ID cannot be read, instead of matching two empty IDs')
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

def source_files(r, v=NEW):
    d = r['root'] / f'usr/src/nvpcf-hook-{v}'
    return {f.name: f.read_text() for f in d.iterdir()} if d.is_dir() else None


r = run('unregistered-matching-source', {}, unregistered={NEW: {}})
check(r, r['exit'] == 0 and 'holds hook/\'s source already' in r['out'] and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW},
      f"registers and installs the existing copy of hook/'s source, got status {r['exit']} and {r['state']['installed']}")
check(r, r['registered_new'] == sorted(SOURCES), f"leaves the matching directory as it was, got {r['registered_new']}")

r = run('unregistered-conflicting-source', {},
        unregistered={NEW: {'nvpcf_hook.c': '/* local work */\n', 'notes.txt': 'keep me\n', 'text.h': None}})
check(r, r['exit'] == 1 and r['changing'] == [] and 'leaves it as it is' in r['out']
      and 'nvpcf_hook.c differs' in r['out'] and 'text.h is missing' in r['out'] and 'notes.txt is not part of the source' in r['out'],
      f"refuses, names every difference and leaves the source and DKMS state unchanged, got status {r['exit']} and {r['changing']}")
files = source_files(r)
check(r, files is not None and files.get('nvpcf_hook.c') == '/* local work */\n' and files.get('notes.txt') == 'keep me\n'
      and 'text.h' not in files, f"leaves the conflicting directory as it was, got {sorted(files or [])}")

r = run('unregistered-source-is-a-file', {}, unregistered={NEW: 'file'})
check(r, r['exit'] == 1 and r['changing'] == [] and 'is not a directory' in r['out']
      and (r['root'] / f'usr/src/nvpcf-hook-{NEW}').read_text() == 'not a directory\n',
      f"refuses and leaves a file in the source directory's place, got status {r['exit']}")

def entries(r, v=NEW):
    """The entries of a version's source directory and their types, or None
    when the directory is gone."""
    d = r['root'] / f'usr/src/nvpcf-hook-{v}'
    if not d.is_dir():
        return None
    return {e.name: ('link' if e.is_symlink() else 'file' if e.is_file() else 'other') for e in d.iterdir()}


def linked(kind):
    """A prepare function that replaces build_id.h in the unregistered source
    directory with a link to an identical file next to the directory."""
    def prepare(root):
        d = root / f'usr/src/nvpcf-hook-{NEW}'
        outside = d.parent / 'build_id.h.elsewhere'
        shutil.copy(SRC / 'hook/build_id.h', outside)
        (d / 'build_id.h').unlink()
        if kind == 'symlink':
            (d / 'build_id.h').symlink_to('../build_id.h.elsewhere')
        else:
            os.link(outside, d / 'build_id.h')
    return prepare


for kind, words in (('symlink', 'build_id.h is a symbolic link'), ('hardlink', 'build_id.h has another hard link')):
    r = run(f'unregistered-source-{kind}', {}, unregistered={NEW: {}}, prepare=linked(kind))
    check(r, r['exit'] == 1 and r['changing'] == [] and words in r['out'] and 'leaves it as it is' in r['out'],
          f"refuses a source directory whose build_id.h is a {kind} to an identical file, got status {r['exit']}")
    check(r, (entries(r) or {}).get('build_id.h') == ('link' if kind == 'symlink' else 'file'),
          f"leaves the {kind} as it was, got {entries(r)}")

for label, extra in (('space', 'Makefile dkms.conf'), ('newline', 'Kbuild\nMakefile')):
    r = run(f'unregistered-source-{label}-name', {}, unregistered={NEW: {extra: 'not part of the source\n'}})
    check(r, r['exit'] == 1 and r['changing'] == [] and 'is not part of the source' in r['out'],
          f"refuses an extra file whose name has a {label} and matches source names, got status {r['exit']}")
    check(r, extra in (entries(r) or {}), f"leaves the extra file as it was, got {entries(r)}")

r = run('source-listing-fails', {'find_fails': f'nvpcf-hook-{NEW}'}, mocks=('find',),
        unregistered={NEW: {'notes.txt': 'keep me\n'}})
check(r, r['exit'] == 1 and r['changing'] == [] and 'cannot list' in r['out']
      and (entries(r) or {}).get('notes.txt') == 'file',
      f"refuses the directory when its listing fails, instead of treating a failed listing as an empty matching directory, got status {r['exit']}")

r = run('source-read-fails', {'cmp_fails': 'nvpcf_hook.c'}, mocks=('cmp',), unregistered={NEW: {}})
check(r, r['exit'] == 1 and r['changing'] == [] and 'cannot read nvpcf_hook.c' in r['out'],
      f"refuses the directory when a source file cannot be read, got status {r['exit']}")

r = run('mkdir-race', {'mkdir_race': f'nvpcf-hook-{NEW}'}, mocks=('mkdir',))
src_path = r['root'] / f'usr/src/nvpcf-hook-{NEW}'
check(r, r['exit'] == 1 and r['changing'] == [] and 'could not create' in r['out']
      and src_path.is_file() and src_path.read_text() == 'another process made this\n',
      f"leaves the file that another process made at the source path, got status {r['exit']} and {r['changing']}")

r = run('copy-fails', {}, mocks=('cp',))
check(r, r['exit'] == 1 and r['changing'] == [] and 'could not copy' in r['out']
      and not (r['root'] / f'usr/src/nvpcf-hook-{NEW}').exists(),
      f"removes the directory and the partial copy that the run made, got status {r['exit']}")


def old_source_with_notes(root):
    d = root / f'usr/src/nvpcf-hook-{OLD}'
    for f in SOURCES:
        shutil.copy(SRC / 'hook' / f, d / f)
    (d / 'notes.txt').write_text('keep me\n')


r = run('old-source-with-extra-file', {}, installed={RUNNING: OLD, OTHER: OLD}, prepare=old_source_with_notes)
check(r, r['exit'] == 0 and r['removed'] == [f'nvpcf-hook/{OLD}'] and OLD not in r['state']['added']
      and entries(r, OLD) == {'notes.txt': 'file'} and 'kept' in r['out'],
      f"removes the old version's source files but keeps the directory with the extra file, got {r['exit']} and {entries(r, OLD)}")

r = run('add-fails-then-retry', {'add_fails': True}, runs=2, between=clearing('add_fails', 1))
check(r, r['exits'] == [1, 0] and 'holds hook/\'s source already' in r['out']
      and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW} and r['registered_new'] == sorted(SOURCES),
      f"reuses the copy that the failed dkms add left, got {r['exits']} and {r['state']['installed']}")

r = run('add-fails', {'add_fails': True})
check(r, r['exit'] == 1 and 'before any module was built or installed' in r['out'] and 'keeps hook/\'s source' in r['out']
      and r['registered_new'] == sorted(SOURCES) and r['state']['installed'] == {}
      and [c for c in r['changing'] if c != 'dkms add'] == [],
      f"stops after the failed dkms add and reports the source it kept, got status {r['exit']} and {r['changing']}")

r = run('again', {}, runs=2)
check(r, r['exit'] == 0 and r['changing'] == [],
      f"leaves the source and DKMS state unchanged on a second run with the same source, got status {r['exit']} and {r['changing']}")

r = run('wrong-file-stays', {'wrong_file': [RUNNING]}, runs=2)
check(r, r['exits'] == [1, 1] and f'{RUNNING}: installing nvpcf-hook/{NEW}, or checking that modprobe finds it' in r['out'],
      f"fails both runs while modprobe finds another file than DKMS's build, got the exit statuses {r['exits']}")
check(r, r['installs'] == [(NEW, RUNNING)],
      f"checks the kernel that DKMS lists as installed again, and installs it again, got the installs {r['installs']}")


r = run('wrong-file-repaired', {'wrong_file': [RUNNING]}, runs=2, between=clearing('wrong_file', 1))
installed_file = r['root'] / 'lib/modules' / RUNNING / 'updates/dkms/nvpcf_hook.ko'
built_file = r['root'] / 'var/lib/dkms/nvpcf-hook' / NEW / RUNNING / 'x86_64/module/nvpcf_hook.ko'
check(r, r['exits'] == [1, 0] and r['installs'] == [(NEW, RUNNING)]
      and installed_file.read_text() == built_file.read_text(),
      f"repairs the kernel on the second run and checks it, got the exit statuses {r['exits']} and the installs {r['installs']}")

built = {}
def build_in_hook(scripts, i):
    built['result'] = kbuild(scripts / 'hook')
    built['generated'] = (scripts / 'hook/nvpcf_hook.mod.c').exists()

r = run('kbuild-between-runs', {}, runs=2, between=build_in_hook)
check(r, built['result'].returncode == 0 and built['generated'],
      f"builds hook/ with Kbuild between the runs, got status {built['result'].returncode}:\n{built['result'].stdout}")
check(r, r['exit'] == 0 and r['changing'] == [],
      f"leaves the source and DKMS state unchanged on the second run after the build, got status {r['exit']} and {r['changing']}")
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

r = run('running-installed-without-headers', {}, headers=(OTHER,), registered={NEW: None},
        installed={RUNNING: NEW, OTHER: OLD})
check(r, r['exit'] == 0 and f'nvpcf-hook/{NEW} is installed for {RUNNING} already' in r['out']
      and r['installs'] == [(NEW, OTHER)] and r['removed'] == [f'nvpcf-hook/{OLD}'],
      f"goes ahead when the running kernel has the version installed and checked, got status {r['exit']} and {r['installs']}")


def running_file_differs(root):
    (root / 'lib/modules' / RUNNING / 'updates/dkms/nvpcf_hook.ko').write_text(
        f"BUILDID={hashlib.sha1(b'another build').hexdigest()} {NEW}\n")


r = run('running-unchecked-without-headers', {}, headers=(OTHER,), registered={NEW: None},
        installed={RUNNING: NEW}, prepare=running_file_differs)
check(r, r['exit'] == 1 and 'headers of the running kernel' in r['out'] and r['changing'] == [],
      f"stops before changing anything when the running kernel needs a new install but lacks headers, got {r['changing']}")

r = run('fetch-fails', {'fetch_fails': True})
check(r, r['exit'] == 1 and 'source archive not downloaded' in r['out']
      and r['state']['installed'] == {RUNNING: r['new'], OTHER: r['new']},
      f"installs the module, but exits with status 1 when an archive cannot be downloaded, got {r['exit']}")

r = run('extra-argument', {}, args=('now',))
check(r, r['exit'] == 2 and r['calls'] == [], f"exits with status 2 before running any command, got {r['exit']}")

r = run('no-enable-extra-argument', {}, args=('--no-enable', 'now'))
check(r, r['exit'] == 2 and r['calls'] == [], f"exits with status 2 before running any command, got {r['exit']}")

for name, args in (('empty-argument', ('',)), ('two-empty-arguments', ('', '')),
                   ('no-enable-empty-argument', ('--no-enable', '')), ('empty-argument-no-enable', ('', '--no-enable'))):
    r = run(name, {}, args=args)
    check(r, r['exit'] == 2 and r['calls'] == [] and 'usage: install-hook.sh' in r['out'],
          f"exits with status 2 before running any command for the arguments {args!r}, got status {r['exit']} and {r['calls']}")

r = run('no-enable', {}, args=('--no-enable',))
check(r, r['exit'] == 0 and r['state']['installed'] == {RUNNING: NEW, OTHER: NEW} and r['unit'],
      f"installs the module and the unit, got status {r['exit']} and {r['state']['installed']}")
check(r, not any(a[:1] in (['enable'], ['disable']) for a in r['systemctl']) and ['daemon-reload'] in r['systemctl']
      and 'without enabling it; it is disabled' in r['out'],
      f"reloads systemd but neither enables nor disables the unit, and says that it is disabled, got {r['systemctl']}")

r = run('no-enable-already-enabled', {'unit_enabled': True}, args=('--no-enable',))
check(r, r['exit'] == 0 and not any(a[:1] in (['enable'], ['disable']) for a in r['systemctl'])
      and 'without enabling it; it is enabled' in r['out'],
      f"leaves an enabled unit enabled, and says so, got {r['systemctl']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
