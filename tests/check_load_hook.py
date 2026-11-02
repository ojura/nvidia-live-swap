#!/usr/bin/python3
"""Run load-hook.sh as an ordinary user, with mock commands and, for each
case, a throwaway directory in place of /sys/module, /proc/modules,
/var/cache/nvpcf-hook, /var/lib/dkms, /lib/modules and /usr/src.

modprobe, modinfo, zstd, readelf, curl and uname are mocks placed first on
PATH. check_nvidia.py and nvpcf-hookctl are mocks placed next to the copied
script, where the installed script finds its copies. A mock module file
holds its build ID and version as text, which the mock readelf and modinfo
report for that file, so the cases can tell which file the script checked
and where a version came from. Asked for nvidia's version by name, the mock
modinfo answers with a different version, which a case would notice. The
loaded nvidia's build ID is a real GNU note under the throwaway /sys/module,
which the script parses itself.

The cases for --build-id and --loaded-build-id read the script's stdout
apart from its messages on stderr, and check that it prints IDs of 16, 20, 32
and 64 bytes in lowercase, and refuses an ID of 0 or 65 bytes, a file
without an ID, a missing file, and a loaded module without a note. The cases
for --check-loaded and for the end of a load check that the hook counts as
usable only when it is active or waiting, with its guards registered, GPU
initialization allowed and retirement_failed 0."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

SRC = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix='load-hook-test-'))
RUNNING = '7.0.0-38-generic'
VER = '615.71.09'
LOADED = 'B90B27F7877E92D38D7B045E3DF6C91E381A42B1'.lower()
OTHER = 'aa' * 20

MOCK = r'''#!/usr/bin/python3 -I
import json, os, struct, sys
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
mods = root / 'proc/modules'
def loaded(): return [l.split()[0] for l in mods.read_text().splitlines() if l.strip()]
def note(hexid):
    desc = bytes.fromhex(hexid)
    pad = b'\0' * (-len(desc) % 4)
    return struct.pack('<III', 4, len(desc), 3) + b'GNU\0' + desc + pad
def set_nvidia_loaded():
    d = root / 'sys/module/nvidia'
    (d / 'notes').mkdir(parents=True, exist_ok=True)
    (d / 'notes/.note.gnu.build-id').write_bytes(note(cfg['loaded_id']))
    (d / 'version').write_text(cfg.get('loaded_version', cfg['version']) + '\n')
    mods.write_text(mods.read_text() + 'nvidia 1 0 - Live 0x0\n')
if cmd == 'modprobe':
    if args == ['nvidia']:
        if 'nvidia' not in loaded():
            set_nvidia_loaded()
    elif args[0] == 'nvpcf_hook':
        if cfg.get('hook_load_fails'):
            sys.exit(1)
        mods.write_text(mods.read_text() + 'nvpcf_hook 1 0 - Live 0x0\n')
        st = load()
        st['hook'] = cfg.get('hook_after_load', ['active', 1, 0])
        save(st)
    else:
        sys.exit(1)
elif cmd == 'modinfo':
    if cfg.get('modinfo_missing'):
        sys.exit(1)
    if args[:2] == ['-F', 'version']:
        if args[2] == 'nvidia':
            print('9.9.9')
        else:
            text = Path(args[2]).read_text()
            if 'VERSION=' in text:
                print(text.split('VERSION=')[1].split()[0])
    else:
        print(root / 'lib/modules' / cfg['running'] / 'updates/dkms/nvidia.ko.zst')
elif cmd == 'zstd':
    sys.stdout.write(Path(args[-1]).read_text())
elif cmd == 'readelf':
    text = Path(args[-1]).read_text()
    if 'BUILDID=' in text:
        print('    Build ID: ' + text.split('BUILDID=')[1].split()[0])
elif cmd == 'curl':
    out = Path(args[args.index('-o') + 1])
    if cfg.get('curl_fails'):
        out.write_text('partial')
        sys.exit(22)
    out.write_text('archive of ' + args[-1])
elif cmd == 'uname':
    print(cfg['running'])
elif cmd == 'check_nvidia.py':
    i = args.index('--module-object')
    st = load()
    st['checked'] = Path(args[i + 1]).read_text().strip()
    save(st)
    sys.exit(1 if cfg.get('checker_fails') else 0)
elif cmd == 'nvpcf-hookctl':
    st = load()
    state, guards, blocked, *rest = st['hook']
    print(f'state: {state}\nguards_registered: {guards}\ninitialization_blocked: {blocked}\n'
          f'retirement_failed: {rest[0] if rest else 0}\nlast_error: 0')
'''

failures = []


def check(r, cond, what):
    if not cond:
        failures.append(f"{r['case']}: {what}")
        print(f"FAIL {r['case']}: {what}\n{r['out']}")


def module_text(build_id, version, where):
    return (f'BUILDID={build_id} ' if build_id else '') + f'VERSION={version} {where}\n'


def run(name, cfg=None, args=(), nvidia_loaded=True, hook_loaded=False, disk_id=LOADED, dkms_id=None,
        source_tree=True, cached=True, disk_version=VER, versions=(VER,)):
    cfg = dict({'running': RUNNING, 'version': VER, 'loaded_id': LOADED}, **(cfg or {}))
    root = BASE / name
    (root / 'bin').mkdir(parents=True)
    mock = root / 'mock'
    mock.write_text(MOCK)
    mock.chmod(0o755)
    for cmd in ('modprobe', 'modinfo', 'zstd', 'readelf', 'curl', 'uname'):
        (root / 'bin' / cmd).symlink_to(mock)
    (root / 'proc').mkdir()
    (root / 'proc/modules').write_text('')
    (root / 'config.json').write_text(json.dumps(cfg))
    (root / 'state.json').write_text(json.dumps({'hook': ['absent', 0, 1], 'checked': None}))
    (root / 'calls.jsonl').write_text('')
    disk = root / 'lib/modules' / RUNNING / 'updates/dkms/nvidia.ko.zst'
    disk.parent.mkdir(parents=True)
    disk.write_text(module_text(disk_id, disk_version, 'disk'))
    if dkms_id:
        d = root / 'var/lib/dkms/nvidia' / VER / RUNNING / 'x86_64/module'
        d.mkdir(parents=True)
        (d / 'nvidia.ko.zst').write_text(module_text(dkms_id, VER, 'dkms'))
    (root / 'cache').mkdir()
    for v in versions:
        if source_tree:
            (root / f'usr/src/nvidia-{v}').mkdir(parents=True)
        if cached:
            (root / f'cache/open-gpu-kernel-modules-{v}.tar.gz').write_text('cached archive')
    scripts = root / 'scripts'
    scripts.mkdir()
    text = (SRC / 'load-hook.sh').read_text()
    for old, new in (('/sys/module/', f'{root}/sys/module/'), ('/proc/modules', f'{root}/proc/modules'),
                     ('/var/cache/nvpcf-hook', f'{root}/cache'), ('/var/lib/dkms/', f'{root}/var/lib/dkms/'),
                     ('/usr/src/nvidia-', f'{root}/usr/src/nvidia-')):
        text = text.replace(old, new)
    (scripts / 'load-hook.sh').write_text(text)
    for name_ in ('check_nvidia.py', 'nvpcf-hookctl'):
        (scripts / name_).symlink_to(mock)
    if nvidia_loaded:
        env0 = dict(os.environ, MOCK_ROOT=str(root))
        subprocess.run([str(mock)], env=env0, check=False)  # records nothing useful; set up below
        (root / 'calls.jsonl').write_text('')
        d = root / 'sys/module/nvidia'
        (d / 'notes').mkdir(parents=True)
        desc = bytes.fromhex(cfg['loaded_id'])
        (d / 'notes/.note.gnu.build-id').write_bytes(
            struct.pack('<III', 4, len(desc), 3) + b'GNU\0' + desc + b'\0' * (-len(desc) % 4))
        (d / 'version').write_text(cfg.get('loaded_version', VER) + '\n')
        (root / 'proc/modules').write_text('nvidia 1 0 - Live 0x0\n')
    if hook_loaded:
        (root / 'proc/modules').write_text((root / 'proc/modules').read_text() + 'nvpcf_hook 1 0 - Live 0x0\n')
        (root / 'state.json').write_text(json.dumps({'hook': cfg.get('hook_before', ['active', 1, 0]),
                                                     'checked': None}))
    env = dict(os.environ, PATH=f'{root}/bin:/usr/bin:/bin', MOCK_ROOT=str(root))
    args = [a.format(root=root) for a in args]
    p = subprocess.run(['/bin/bash', str(scripts / 'load-hook.sh'), *args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    calls = [json.loads(l) for l in (root / 'calls.jsonl').read_text().splitlines()]
    st = json.loads((root / 'state.json').read_text())
    return {'case': name, 'exit': p.returncode, 'out': p.stdout + p.stderr, 'stdout': p.stdout,
            'modprobe': [a for c, a in calls if c == 'modprobe'],
            'curl': [a for c, a in calls if c == 'curl'],
            'checker': [a for c, a in calls if c == 'check_nvidia.py'],
            'checked': st['checked'],
            'cache': sorted(f.name for f in (root / 'cache').iterdir()),
            'root': root}


HOOK_LOAD = ['nvpcf_hook', f'expected_nvidia_build_id={LOADED}']

r = run('load-success')
check(r, r['exit'] == 0, f"exits with status 0, got {r['exit']}")
check(r, r['modprobe'] == [['nvidia'], HOOK_LOAD],
      f"loads nvpcf_hook with the loaded build ID in lowercase, got {r['modprobe']}")
a = r['checker'][0] if r['checker'] else []
check(r, r['checked'] == module_text(LOADED, VER, 'disk').strip(),
      f"checks the module file with the loaded build ID, got {r['checked']}")
check(r, f"{r['root']}/usr/src/nvidia-{VER}" in a and f"{r['root']}/cache/open-gpu-kernel-modules-{VER}.tar.gz" in a,
      f"checks the version's kernel-open source and the cached source archive, got {a}")
check(r, r['curl'] == [], 'does not download an archive that is cached')

r = run('nvidia-not-loaded', nvidia_loaded=False)
check(r, r['exit'] == 0 and r['modprobe'][0] == ['nvidia'] and r['modprobe'][-1] == HOOK_LOAD,
      f"loads nvidia first, then the hook, got status {r['exit']} and {r['modprobe']}")

r = run('download', cached=False)
check(r, r['exit'] == 0 and len(r['curl']) == 1, f"downloads the archive once, got status {r['exit']} and {r['curl']}")
check(r, r['curl'] and r['curl'][0][-1] == f'https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/{VER}.tar.gz'
      and '--proto' in r['curl'][0], f"downloads the version's tag over https only, got {r['curl']}")
check(r, r['cache'] == [f'open-gpu-kernel-modules-{VER}.tar.gz'],
      f"leaves only the finished archive in the cache, got {r['cache']}")

r = run('download-fails', {'curl_fails': True}, cached=False)
check(r, r['exit'] == 1 and HOOK_LOAD not in r['modprobe'] and r['checker'] == [],
      f"exits with status 1 before checking or loading the hook, got status {r['exit']}")
check(r, r['cache'] == [], f"leaves no partial download in the cache, got {r['cache']}")

r = run('no-matching-file', disk_id=OTHER)
check(r, r['exit'] == 1 and 'cannot find an installed nvidia module file with the loaded build ID' in r['out'],
      f"exits with status 1 when no file has the loaded build ID, got status {r['exit']}")
check(r, r['checker'] == [] and HOOK_LOAD not in r['modprobe'], 'checks nothing in its place and does not load the hook')

r = run('dkms-copy-matches', disk_id=OTHER, dkms_id=LOADED)
check(r, r['exit'] == 0 and r['checked'] == module_text(LOADED, VER, 'dkms').strip(),
      f"checks DKMS's copy, which has the loaded build ID, got status {r['exit']} and {r['checked']}")

r = run('checker-fails', {'checker_fails': True})
check(r, r['exit'] == 1 and 'check_nvidia.py rejects NVIDIA' in r['out'] and HOOK_LOAD not in r['modprobe'],
      f"exits with status 1 without loading the hook, got status {r['exit']}")

r = run('bad-version', {'loaded_version': '615.71.09/../../x'})
check(r, r['exit'] == 1 and 'not a driver version' in r['out'] and r['curl'] == [] and r['checker'] == [],
      f"refuses a version string that is not a version before downloading or checking, got status {r['exit']}")

r = run('source-tree-missing', source_tree=False)
check(r, r['exit'] == 1 and f'/usr/src/nvidia-{VER} is missing' in r['out'] and HOOK_LOAD not in r['modprobe'],
      f"exits with status 1 when the kernel-open source is missing, got status {r['exit']}")

r = run('hook-load-fails', {'hook_load_fails': True})
check(r, r['exit'] == 1 and 'could not load nvpcf_hook' in r['out'], f"exits with status 1, got {r['exit']}")

for state, ok in ((['waiting', 1, 0], True), (['waiting', 0, 0], False), (['waiting', 1, 1], False),
                  (['active', 0, 0], False), (['active', 1, 1], False), (['active', 1, 0, 1], False),
                  (['waiting', 1, 0, 1], False),
                  (['quiesced', 1, 0], False), (['guard-only', 1, 1], False), (['unavailable', 0, 1], False)):
    r = run('state-' + '-'.join(map(str, state)), {'hook_after_load': state})
    check(r, (r['exit'] == 0) == ok,
          f"exits with status {0 if ok else 1} when the hook ends {state[0]} with guards {state[1]}, "
          f"initialization blocked {state[2]} and retirement_failed {state[3] if len(state) > 3 else 0}, got {r['exit']}")

r = run('already-loaded', hook_loaded=True)
check(r, r['exit'] == 0 and r['modprobe'] == [] and r['checker'] == [],
      f"only reads the state of a loaded hook, got status {r['exit']} and {r['modprobe']}")

r = run('check-loaded', args=('--check-loaded',), hook_loaded=True)
check(r, r['exit'] == 0 and r['modprobe'] == [] and r['checker'] == [] and r['curl'] == [],
      f"only reads the state of a usable hook, got status {r['exit']}, {r['modprobe']} and {r['checker']}")

r = run('check-loaded-retirement-failed', {'hook_before': ['active', 1, 0, 1]}, args=('--check-loaded',), hook_loaded=True)
check(r, r['exit'] == 1 and 'retirement_failed 1' in r['out'],
      f"exits with status 1 for an active hook with retirement_failed 1, and says so, got status {r['exit']}")

r = run('check-loaded-not-loaded', args=('--check-loaded',))
check(r, r['exit'] == 1 and 'nvpcf_hook is not loaded' in r['out'] and r['modprobe'] == [],
      f"exits with status 1 without loading anything, got status {r['exit']} and {r['modprobe']}")

DISK = '{root}/lib/modules/' + RUNNING + '/updates/dkms/nvidia.ko.zst'
for size in (16, 20, 32, 64):
    hexid = bytes(range(size)).hex()
    r = run(f'build-id-{size}-bytes', args=('--build-id', DISK), disk_id=hexid.upper())
    check(r, r['exit'] == 0 and r['stdout'] == hexid + '\n',
          f"prints the {size}-byte build ID in lowercase and nothing else, got status {r['exit']} and {r['stdout']!r}")
    r = run(f'loaded-build-id-{size}-bytes', {'loaded_id': hexid}, args=('--loaded-build-id', 'nvidia'))
    check(r, r['exit'] == 0 and r['stdout'] == hexid + '\n',
          f"prints the loaded {size}-byte build ID and nothing else, got status {r['exit']} and {r['stdout']!r}")

for name, disk_id in (('build-id-65-bytes', 'ab' * 65), ('build-id-missing', None)):
    r = run(name, args=('--build-id', DISK), disk_id=disk_id)
    check(r, r['exit'] == 1 and r['stdout'] == '',
          f"exits with status 1 and prints no build ID, got status {r['exit']} and {r['stdout']!r}")

r = run('build-id-no-file', args=('--build-id', '{root}/no-such-module.ko'))
check(r, r['exit'] == 1 and r['stdout'] == '', f"exits with status 1 for a missing file, got status {r['exit']}")

for name, loaded_id in (('loaded-build-id-0-bytes', ''), ('loaded-build-id-65-bytes', 'ab' * 65)):
    r = run(name, {'loaded_id': loaded_id}, args=('--loaded-build-id', 'nvidia'))
    check(r, r['exit'] == 1 and r['stdout'] == '',
          f"exits with status 1 and prints no build ID, got status {r['exit']} and {r['stdout']!r}")

r = run('loaded-build-id-no-note', args=('--loaded-build-id', 'nvidia'), nvidia_loaded=False)
check(r, r['exit'] == 1 and r['stdout'] == '' and r['modprobe'] == [],
      f"exits with status 1 for a module without a note, without loading it, got status {r['exit']}")

for name, args in (('loaded-build-id-path', ('--loaded-build-id', '../nvidia')), ('build-id-without-file', ('--build-id',))):
    r = run(name, args=args)
    check(r, r['exit'] == 2, f"exits with status 2 for the arguments {args}, got {r['exit']}")

r = run('already-loaded-unusable', {'hook_before': ['unavailable', 0, 1]}, hook_loaded=True)
check(r, r['exit'] == 1, f"exits with status 1 for a loaded hook that is unavailable, got {r['exit']}")

r = run('check-disk', args=('--check-disk',), nvidia_loaded=False)
check(r, r['exit'] == 0 and r['modprobe'] == [] and r['checked'] == module_text(LOADED, VER, 'disk').strip(),
      f"checks the file modprobe would load without loading anything, got status {r['exit']} and {r['modprobe']}")

for case, build_id in (('no-build-id', None), ('malformed-build-id', 'zz' * 20), ('long-build-id', 'ab' * 65)):
    r = run(f'check-disk-{case}', args=('--check-disk',), nvidia_loaded=False, disk_id=build_id)
    check(r, r['exit'] == 1 and 'no GNU build ID of 1 to 64 bytes' in r['out'] and r['checker'] == [],
          f"refuses a candidate without a usable build ID before checking it, got status {r['exit']}")

r = run('check-disk-version-from-file', args=('--check-disk',), nvidia_loaded=False, disk_version='615.78.08',
        versions=(VER, '615.78.08'))
a = r['checker'][0] if r['checker'] else []
check(r, r['exit'] == 0 and any(x.endswith('/usr/src/nvidia-615.78.08') for x in a)
      and any(x.endswith('open-gpu-kernel-modules-615.78.08.tar.gz') for x in a),
      f"checks the source of the version that the checked file reports, got status {r['exit']} and {a}")

r = run('loaded-version-differs', disk_version='615.78.08', versions=(VER, '615.78.08'))
check(r, r['exit'] == 1 and "reports version '615.78.08', but the loaded nvidia reports 615.71.09" in r['out']
      and r['checker'] == [] and HOOK_LOAD not in r['modprobe'],
      f"refuses when the file with the loaded build ID reports another version, got status {r['exit']}")

r = run('check-disk-no-download', args=('--check-disk', '--no-download'), cached=False)
check(r, r['exit'] == 3 and r['curl'] == [] and 'cannot be checked without a download' in r['out'],
      f"exits with status 3, which means the check could not run, without downloading, got status {r['exit']} "
      f"and {r['curl']}")

r = run('fetch', args=('--fetch', VER), cached=False)
check(r, r['exit'] == 0 and len(r['curl']) == 1 and r['modprobe'] == [],
      f"downloads the archive and loads nothing, got status {r['exit']}")

r = run('fetch-bad-version', args=('--fetch', '../../etc'), cached=False)
check(r, r['exit'] == 1 and r['curl'] == [], f"refuses a version that is not a version, got status {r['exit']}")

r = run('bad-arguments', args=('--check',))
check(r, r['exit'] == 2 and r['modprobe'] == [], f"exits with status 2 for an unknown option, got {r['exit']}")

if failures:
    print(f'{len(failures)} failures; the cases are in {BASE}')
    sys.exit(1)
shutil.rmtree(BASE)
print('all cases passed')
