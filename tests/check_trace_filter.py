#!/usr/bin/python3
"""Run trace.sh as an ordinary user with ordinary files in place of the
tracing and debug control files, and a function-filter write that fails the
way tracefs rejects an unknown function name. trace.sh must stop before it
selects function_graph and remove the instance. Adapted from a fixture by
the crazy-binary-patching-mofo session."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

TRACE = Path(__file__).resolve().parent.parent / 'trace.sh'
root = Path(tempfile.mkdtemp(prefix='trace-filter-'))
(root / 'instances').mkdir()
(root / 'bin').mkdir()
(root / 'control').write_text('drivers/acpi/ec.c:281 [acpi]acpi_ec_read_status =_ "EC_SC(R)"\n')
(root / 'runtime_status').write_text('suspended\n')
(root / 'lib.sh').write_text('nvidia_gpu() { echo fixture; }\n')
mock = root / 'mock'
mock.write_text('''#!/usr/bin/python3 -I
import os, shutil, sys
from pathlib import Path
root = Path(os.environ['TRACE_TEST_ROOT'])
p = Path(sys.argv[1])
assert p.parent == root / 'instances', p
if Path(sys.argv[0]).name == 'mkdir':
    p.mkdir()
    for name in ('tracing_on', 'current_tracer', 'trace'):
        (p / name).write_text('')
    (p / 'set_ftrace_filter').symlink_to('/dev/full')
else:
    shutil.rmtree(p)
''')
mock.chmod(0o755)
for name in ('mkdir', 'rmdir'):
    (root / 'bin' / name).symlink_to(mock)
text = TRACE.read_text()
text = text.replace('/sys/kernel/debug/dynamic_debug/control', str(root / 'control'))
text = text.replace('/sys/kernel/tracing/instances', str(root / 'instances'))
text = text.replace('/sys/bus/pci/devices/$gpu/power/runtime_status', str(root / 'runtime_status'))
# Record every echo while keeping its output and status.
text = text.replace('set -u\n', 'set -u\necho() { printf "%s\\n" "$*" >> "$TRACE_TEST_ROOT/echo.log"; builtin echo "$@"; }\n', 1)
(root / 'trace.sh').write_text(text)
(root / 'echo.log').write_text('')
env = dict(os.environ, TRACE_TEST_ROOT=str(root), PATH=f'{root}/bin:/usr/bin:/bin')
r = subprocess.run(['/bin/bash', str(root / 'trace.sh'), '0'], env=env, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, text=True, timeout=20)
echoed = (root / 'echo.log').read_text().splitlines()
failures = []
for cond, what in [
    (r.returncode != 0, 'exits with a failure status'),
    ('could not set the function filter' in r.stdout, 'reports the filter failure'),
    ('function_graph' not in echoed, 'never selects function_graph'),
    (not any(l.endswith(' +p') for l in echoed), 'never turns EC debug lines on'),
    (os.listdir(root / 'instances') == [], 'removes the instance'),
]:
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)
if failures:
    print(r.stdout)
    sys.exit(f'{len(failures)} failures; the files are in {root}')
shutil.rmtree(root)
print('all checks passed')
