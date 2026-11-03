#!/usr/bin/python3
"""Exercise production control_open with misc_open's actual initial private data."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'nvpcf_hook.c').read_text()
FUNCTION = runpy.run_path(str(ROOT / 'tests/check_nvidia.py'))['function']
BODY = FUNCTION(SOURCE, 'control_open', raw=True)[1]

PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#ifdef NEGATIVE_HANDOFF_TEST
#undef assert
#define assert(condition) ((condition) ? (void)0 : _Exit(77))
#endif
struct inode { int unused; };
struct file { void *private_data; };
struct miscdevice { unsigned int marker; };
struct seq_file;
struct seq_operations { int (*show)(struct seq_file *, void *); };
struct seq_file { struct file *file; const struct seq_operations *op; void *private; };
static struct miscdevice control_device = { 12345 };
static unsigned int fail_allocation, allocations, frees, outstanding, single_calls, releases;
static int status_show(struct seq_file *seq, void *data)
{ (void)seq; (void)data; return 0; }
static void *allocate(size_t size)
{
    void *memory;
    allocations++;
    if (allocations == fail_allocation) return NULL;
    memory = calloc(1, size);
    assert(memory && memory != &control_device);
    outstanding++;
    return memory;
}
static void release(void *memory)
{
    assert(memory && memory != &control_device && outstanding > 0);
    frees++;
    outstanding--;
    free(memory);
}
/* Model exact seq_file.c:573-590: op allocation first, then seq_open's
 * allocation. Only op is freed if the latter fails, and release is not called
 * by the VFS for a failed open. This models allocation ownership, not a live VFS.
 */
static int single_open(struct file *file, int (*show)(struct seq_file *, void *), void *data)
{
    struct seq_operations *op;
    struct seq_file *seq;
    single_calls++;
    assert(file->private_data == NULL); /* seq_open warns if this is non-NULL. */
    assert(show == status_show && data == NULL);
    op = allocate(sizeof(*op));
    if (!op) return -ENOMEM;
    op->show = show;
    seq = allocate(sizeof(*seq));
    if (!seq) { release(op); return -ENOMEM; }
    seq->file = file;
    seq->op = op;
    seq->private = data;
    file->private_data = seq;
    return 0;
}
static int single_release(struct inode *inode, struct file *file)
{
    struct seq_file *seq = file->private_data;
    const struct seq_operations *op;
    (void)inode;
    releases++;
    assert(seq && (void *)seq != &control_device && seq->file == file);
    op = seq->op;
    release(seq);
    release((void *)op);
    file->private_data = NULL;
    return 0;
}
'''

TESTS = r'''
int main(void)
{
    struct inode inode = { 0 };
    unsigned int failure;
    for (failure = 0; failure < 3; ++failure) {
        struct file file = { .private_data = &control_device };
        int error;
        allocations = frees = outstanding = single_calls = releases = 0;
        fail_allocation = failure;
        error = control_open(&inode, &file);
        assert(single_calls == 1 && control_device.marker == 12345);
        if (failure) {
            assert(error == -ENOMEM && file.private_data == NULL);
            assert(allocations == failure && frees == failure - 1 && !outstanding);
            assert(!releases);
        } else {
            struct seq_file *seq = file.private_data;
            assert(!error && seq && (void *)seq != &control_device);
            assert(seq->file == &file && seq->op->show == status_show && seq->private == NULL);
            assert(allocations == 2 && !frees && outstanding == 2);
            assert(!single_release(&inode, &file));
            assert(releases == 1 && frees == 2 && !outstanding && !file.private_data);
        }
    }
    puts("PASS: production control_open replaces the unused misc pointer with seq_file, handles both allocation failures and releases success exactly once");
    return 0;
}
'''

module_version = re.search(r'MODULE_VERSION\("([^"]+)"\)', SOURCE)[1]
package_version = re.search(r'^PACKAGE_VERSION="([^"]+)"', (ROOT / 'dkms.conf').read_text(), re.M)[1]
assert module_version == package_version
assert tuple(map(int, module_version.split('.'))) > (0, 1)

with tempfile.TemporaryDirectory(prefix='nvpcf-control-open-') as tmp:
    root = Path(tmp)
    for negative in (False, True):
        body = BODY
        if negative:
            assert body.count('file->private_data = NULL;') == 1
            body = body.replace('file->private_data = NULL;', '(void)file;')
        source, binary = root / ('negative.c' if negative else 'control.c'), root / ('negative' if negative else 'control')
        source.write_text(PREFIX + 'static int control_open(struct inode *inode, struct file *file)\n' + body + '\n' + TESTS)
        command = ['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter']
        if negative:
            command.append('-DNEGATIVE_HANDOFF_TEST')
        subprocess.run(command + [str(source), '-o', str(binary)], check=True)
        result = subprocess.run([str(binary)], check=False)
        assert result.returncode == (77 if negative else 0), result.returncode
print('PASS: removing the misc private-data clear is rejected; module and DKMS versions agree and do not reuse 0.1')
