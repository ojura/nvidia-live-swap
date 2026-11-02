#!/usr/bin/python3
"""Compile production file readers with modeled kernfs and NVIDIA seq files."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = runpy.run_path(str(ROOT / 'tests/check_nvidia.py'))['function']
SOURCE = (ROOT / 'nvpcf_hook.c').read_text()

PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <stdlib.h>
#include "text.h"
#define GFP_KERNEL 0
#define kmalloc(size, flags) malloc(size)
#define kfree(ptr) free(ptr)
#define O_RDONLY 0
#define PROC_SUPER_MAGIC 0x9fa0
#define ITER_DEST 1
#define IS_ERR(file) ((file) == NULL)
#define PTR_ERR(file) (-ENOENT)
struct super_block { unsigned long s_magic; };
struct inode { struct super_block *i_sb; };
struct file { struct inode *inode; void *private_data; };
struct seq_file;
struct seq_operations { int (*show)(struct seq_file *, void *); };
struct seq_file { struct file *file; const struct seq_operations *op; };
struct kiocb { struct file *ki_filp; loff_t ki_pos; };
struct kvec { void *iov_base; size_t iov_len; };
struct iov_iter { struct kvec *vector; size_t count; };
static struct super_block super;
static struct inode inode;
static struct file file, other_file;
static struct seq_file seq;
static struct seq_operations operations;
static struct { bool nvidia_ref, build_id_verified; void *pci;
    int (*proc_power_show)(struct seq_file *, void *); } h;
static bool legacy_proc, open_error, sleep_barrier, stable_gpu;
static unsigned int opens, closes, kernel_reads, seq_reads, shows;
static ssize_t injected_error;
static const char *payload;
static size_t chunk;
static int show_power(struct seq_file *s, void *v)
{ (void)v; assert(s == &seq && sleep_barrier && stable_gpu); shows++; return 0; }
static int show_info(struct seq_file *s, void *v)
{ (void)s; (void)v; assert(false); return 0; }
static const char *pci_name(void *dev) { (void)dev; return "0000:01:00.0"; }
#define file_inode(file) ((file)->inode)
static struct file *filp_open(const char *name, int flags, int mode)
{ assert(name && flags == O_RDONLY && mode == 0); opens++; return open_error ? NULL : &file; }
static void __fput_sync(struct file *f)
{ assert(f == &file && closes == 0); closes++; }
static ssize_t read_chunk(void *buf, size_t count, loff_t *position)
{
    size_t available;
    assert(*position >= 0);
    if (injected_error && *position >= (loff_t)chunk) return injected_error;
    available = strlen(payload) - (size_t)*position;
    if (available > count) available = count;
    if (available > chunk) available = chunk;
    memcpy(buf, payload + *position, available);
    *position += (loff_t)available;
    return (ssize_t)available;
}
static ssize_t kernel_read(struct file *f, void *buf, size_t count, loff_t *position)
{
    assert(f == &file);
    kernel_reads++;
    if (legacy_proc) return -EINVAL;
    return read_chunk(buf, count, position);
}
static void init_sync_kiocb(struct kiocb *iocb, struct file *f)
{ iocb->ki_filp = f; iocb->ki_pos = 0; }
static void iov_iter_kvec(struct iov_iter *iter, int direction, struct kvec *vec,
                          unsigned long count, size_t size)
{ assert(direction == ITER_DEST && count == 1); iter->vector = vec; iter->count = size; }
static ssize_t seq_read_iter(struct kiocb *iocb, struct iov_iter *iter)
{
    assert(iocb->ki_filp == &file && legacy_proc && sleep_barrier && stable_gpu);
    seq_reads++;
    if (!iocb->ki_pos) assert(!seq.op->show(&seq, NULL));
    return read_chunk(iter->vector->iov_base, iter->count, &iocb->ki_pos);
}
'''

TESTS = r'''
static void reset(bool proc)
{
    super.s_magic = proc ? PROC_SUPER_MAGIC : 0x62656572;
    inode.i_sb = &super;
    file = (struct file){ &inode, NULL };
    seq = (struct seq_file){ &file, &operations };
    operations.show = show_power;
    h.proc_power_show = show_power;
    file.private_data = proc ? &seq : (void *)1;
    h.nvidia_ref = h.build_id_verified = sleep_barrier = stable_gpu = true;
    legacy_proc = proc;
    open_error = false;
    opens = closes = kernel_reads = seq_reads = shows = 0;
    injected_error = 0;
    payload = "Runtime D3 status: Enabled (fine-grained)\n";
    chunk = 7;
}
int main(void)
{
    char data[256];
    reset(false);
    assert(!read_text("/sys/module/nvidia/parameters/example", data, sizeof(data)));
    assert(!strcmp(data, payload) && kernel_reads > 1 && !seq_reads && !shows && closes == 1);
    reset(true);
    assert(read_text("/proc/driver/nvidia/gpus/example/power", data, sizeof(data)) == -EINVAL);
    assert(kernel_reads == 1 && !seq_reads && closes == 1); /* Old backend fails. */
    reset(true);
    assert(!read_gpu_text("/proc/driver/nvidia/gpus/example/power", show_power, data, sizeof(data)));
    assert(!strcmp(data, payload) && !kernel_reads && seq_reads > 1 && shows == 1 && closes == 1);
    reset(true);
    assert(!read_gpu_text("/proc/driver/nvidia/gpus/example/power", show_power, data, 9));
    assert(!strcmp(data, "Runtime ") && !kernel_reads && closes == 1);
    reset(true);
    assert(!effective_fine() && !kernel_reads && seq_reads > 1 && closes == 1);
    reset(true);
    payload = "Runtime D3 status: Disabled\nOther: Enabled (fine-grained)\n";
    assert(effective_fine() == -EOPNOTSUPP && closes == 1);
    reset(true);
    payload = "Runtime D3 status: ?\n";
    assert(effective_fine() == -EAGAIN && closes == 1);
    reset(false);
    injected_error = -EIO;
    assert(read_text("/sys/module/nvidia/notes/example", data, sizeof(data)) == -EIO && closes == 1);
    reset(true);
    injected_error = -EIO;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -EIO && closes == 1);
    reset(true);
    open_error = true;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -ENOENT && !closes);
    reset(true);
    open_error = true;
    assert(read_text("sysfs", data, sizeof(data)) == -ENOENT && !closes);
    reset(false);
    assert(read_gpu_text("not-procfs", show_power, data, sizeof(data)) == -EPROTO);
    assert(closes == 1 && !seq_reads); /* Does not treat arbitrary private_data as seq_file. */
    reset(true);
    file.private_data = NULL;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -EPROTO && closes == 1);
    reset(true);
    seq.file = &other_file;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -EPROTO && closes == 1);
    reset(true);
    seq.op = NULL;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -EPROTO && closes == 1);
    reset(true);
    assert(read_gpu_text("power", show_info, data, sizeof(data)) == -EPROTO && closes == 1);
    assert(!shows && !seq_reads);
    reset(true);
    h.build_id_verified = false;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -ESTALE && !opens);
    reset(true);
    h.nvidia_ref = false;
    assert(read_gpu_text("power", show_power, data, sizeof(data)) == -ESTALE && !opens);
    reset(true);
    assert(read_gpu_text("power", NULL, data, sizeof(data)) == -ESTALE && !opens);
    assert(read_gpu_text("power", show_power, data, 0) == -EINVAL && !opens);
    puts("PASS: production kernfs/legacy-proc backend selection, checked seq association, short/error reads and close cleanup");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='nvpcf-production-io-') as tmp:
    root = Path(tmp)
    source, binary = root / 'readers.c', root / 'readers'
    functions = '\n'.join(
        f'static {result} {name}({args})\n{FUNCTION(SOURCE, name, raw=True)[1]}\n'
        for result, name, args in (
            ('ssize_t', 'read_blob', 'const char *name, void *buf, size_t size'),
            ('int', 'read_text', 'const char *name, char *buf, size_t size'),
            ('int', 'read_gpu_text', 'const char *name, int (*show)(struct seq_file *, void *), char *buf, size_t size'),
            ('int', 'effective_fine', 'void'),
        )
    )
    source.write_text(PREFIX + functions + TESTS)
    subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT), str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
