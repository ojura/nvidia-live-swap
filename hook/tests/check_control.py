#!/usr/bin/python3
"""Exercise the control client against ordinary temporary files only."""
from pathlib import Path
import subprocess
import sys
import tempfile

CLIENT = Path(__file__).resolve().parents[1] / 'nvpcf-hookctl'


def run(device, command):
    return subprocess.run(
        [sys.executable, '-I', '-B', str(CLIENT), '--device', str(device), command],
        text=True, capture_output=True, check=False,
    )


with tempfile.TemporaryDirectory(prefix='nvpcf-control-test-') as tmp:
    root = Path(tmp)
    status = root / 'status'
    text = 'state: active\npending_requests: 3\n'
    status.write_text(text)
    result = run(status, 'status')
    assert result.returncode == 0 and result.stdout == text, result
    assert status.read_text() == text
    for command in ('prepare', 'detach', 'resume', 'acknowledge-loss'):
        target = root / command
        target.touch()
        result = run(target, command)
        assert result.returncode == 0, result
        assert target.read_text() == command + '\n'
    for command in ('status', 'prepare', 'detach', 'resume', 'acknowledge-loss'):
        result = run(root / 'missing', command)
        assert result.returncode == 1 and 'failed:' in result.stderr, result
    result = run(root, 'detach')
    assert result.returncode == 1, result
    for command in ('discard', 'detach\nacknowledge-loss', ''):
        result = run(status, command)
        assert result.returncode == 2, result
        assert status.read_text() == text
print('PASS: control status, explicit commands, invalid arguments and I/O failures using temporary files')
