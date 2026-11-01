#!/usr/bin/python3
"""Check revoke.py, as root, against a throwaway process owned by the user
nobody.

revoke.py must refuse PID 1 and root-owned processes without attaching, and
must replace the descriptors and mappings a two-thread process holds on a
test file, with scheduler locking on, while the process keeps running and
its second thread keeps reading through them."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

REVOKE = Path(__file__).resolve().parent.parent / 'revoke.py'
HOLDER = r'''
import mmap, os, sys, threading, time
path, logpath = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_RDWR)
m = mmap.mmap(fd, 65536)
m[:5] = b"HELLO"
log = open(logpath, "w", buffering=1)
count = 0
def worker():
    global count
    while True:
        try:
            os.pread(fd, 5, 0)
        except OSError:
            pass
        bytes(m[:5])
        count += 1
        time.sleep(0.001)
threading.Thread(target=worker, daemon=True).start()
for i in range(240):
    kind = "chardev" if (os.fstat(fd).st_mode & 0o170000) == 0o020000 else "regular"
    log.write(f"{count} {kind} {bytes(m[:5]).hex()}\n")
    time.sleep(0.25)
'''

if os.geteuid() != 0:
    sys.exit('run as root')
failures = []


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)


def revoke(*args):
    r = subprocess.run([sys.executable, '-I', '-B', str(REVOKE), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


d = Path(tempfile.mkdtemp(prefix='revoke-test-'))
d.chmod(0o755)
data, log, holder = d / 'data', d / 'holder.log', d / 'holder.py'
data.write_bytes(os.urandom(65536))
log.write_text('')
holder.write_text(HOLDER)
data.chmod(0o666)
log.chmod(0o666)
holder.chmod(0o644)

rc, out = revoke(1, '/dev/null')
check(rc == 1 and 'refusing PID 1' in out, 'refuses PID 1')
rc, out = revoke(os.getpid(), data)
check(rc == 1 and 'owned by root' in out, 'refuses a root-owned process')

p = subprocess.Popen(['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
                      sys.executable, '-I', '-B', str(holder), str(data), str(log)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
try:
    time.sleep(1)
    rc, out = revoke(p.pid, data)
    print(out.rstrip())
    check(rc == 0 and 'scheduler locking on' in out, 'revokes with scheduler locking on')
    time.sleep(1)
    targets = [os.readlink(f'/proc/{p.pid}/fd/{fd}') for fd in os.listdir(f'/proc/{p.pid}/fd')]
    check(str(data) not in targets, 'every descriptor on the file is replaced')
    check(str(data) not in Path(f'/proc/{p.pid}/maps').read_text(), 'every mapping of the file is replaced')
    check(p.poll() is None, 'the process keeps running')
    lines = log.read_text().split('\n')[-3:-1]
    before, after = (int(l.split()[0]) for l in lines)
    check(after > before, 'its second thread keeps running')
    check(all(l.split()[1:] == ['chardev', '0000000000'] for l in lines),
          'it now sees /dev/null and zeroed memory')
finally:
    p.kill()
    p.wait()
if failures:
    print(f'{len(failures)} failures; the files are in {d}')
    sys.exit(1)
shutil.rmtree(d)
print('all checks passed')
