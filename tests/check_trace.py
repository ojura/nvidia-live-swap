#!/usr/bin/python3
"""Check, as root, that trace.sh leaves the global tracer settings, the list
of tracing instances and the EC driver's debug lines as it found them, after
a normal run and after SIGTERM."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

TRACE = Path(__file__).resolve().parent.parent / 'trace.sh'
T = Path('/sys/kernel/tracing')
C = Path('/sys/kernel/debug/dynamic_debug/control')

if os.geteuid() != 0:
    sys.exit('run as root')
failures = []


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)


def snapshot():
    ec = sorted(l.split()[0] + ' ' + l.split()[2] for l in C.read_text().splitlines()
                if l.startswith('drivers/acpi/ec.c:'))
    return {'tracer': (T / 'current_tracer').read_text(), 'on': (T / 'tracing_on').read_text(),
            'instances': sorted(os.listdir(T / 'instances')), 'ec': ec}


before = snapshot()
r = subprocess.run(['/bin/bash', str(TRACE), '03'], capture_output=True, text=True)
check(r.returncode == 0 and '--- NVPCF notifications' in r.stdout and ' notifications' in r.stdout,
      'trace.sh 03 completes a 3-second run')
check(snapshot() == before, 'the settings are unchanged after a normal run')

p = subprocess.Popen(['/bin/bash', str(TRACE), '30'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(1.5)
during = snapshot()
check(during['instances'] != before['instances'], 'the run uses its own tracing instance')
p.send_signal(signal.SIGTERM)
p.wait(timeout=10)
check(p.returncode != 0, 'SIGTERM ends the run with a failure status')
check(snapshot() == before, 'the settings are unchanged after SIGTERM')
if failures:
    sys.exit(f'{len(failures)} failures')
print('all checks passed')
