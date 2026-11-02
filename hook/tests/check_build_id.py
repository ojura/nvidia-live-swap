#!/usr/bin/python3
"""Check the shared note parser against GNU ld's actual build-ID sections."""
from pathlib import Path
import subprocess
import tempfile

BINARY = Path(__file__).with_name('build_id_test')

with tempfile.TemporaryDirectory(prefix='nvpcf-build-id-notes-') as tmp:
    root = Path(tmp)
    source, obj = root / 'entry.s', root / 'entry.o'
    source.write_text('.globl _start\n.text\n_start: ret\n')
    subprocess.run(['as', '--64', str(source), '-o', str(obj)], check=True)
    for length in (1, 3, 8, 16, 20, 32, 64):
        descriptor = bytes(range(length)).hex()
        elf, note = root / f'elf{length}', root / f'note{length}'
        subprocess.run(['ld', '--build-id=0x' + descriptor, '-o', str(elf), str(obj)], check=True)
        subprocess.run(['objcopy', '--dump-section', '.note.gnu.build-id=' + str(note), str(elf)], check=True)
        subprocess.run([str(BINARY), descriptor, str(note)], check=True)
        subprocess.run([str(BINARY), descriptor.upper(), str(note)], check=True)
print('PASS: actual GNU build-ID notes of 1, 3, 8, 16, 20, 32 and 64 descriptor bytes, including unpadded final descriptors')
