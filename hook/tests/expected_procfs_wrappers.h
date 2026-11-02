/*
 * SPDX-FileCopyrightText: Copyright (c) 2021 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: MIT
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
 * DEALINGS IN THE SOFTWARE.
 */

/* Expected single-open/read/release wrappers for compatibility checks. */
#define NV_DEFINE_SINGLE_PROCFS_FILE_HELPER(name, lock)                     \
    static ssize_t nv_procfs_read_lock_##name(                              \
        struct file *file,                                                  \
        char __user *buf,                                                   \
        size_t size,                                                        \
        loff_t *ppos                                                        \
    )                                                                       \
    {                                                                       \
        int ret;                                                            \
        ret = nv_down_read_interruptible(&lock);                            \
        if (ret < 0)                                                        \
        {                                                                   \
            return ret;                                                     \
        }                                                                   \
        size = seq_read(file, buf, size, ppos);                             \
        up_read(&lock);                                                     \
        return size;                                                        \
    }                                                                       \
                                                                            \
    static int nv_procfs_open_##name(                                       \
        struct inode *inode,                                                \
        struct file *filep                                                  \
    )                                                                       \
    {                                                                       \
        int ret;                                                            \
        ret = single_open(filep, nv_procfs_read_##name,                     \
                          NV_PDE_DATA(inode));                              \
        if (ret < 0)                                                        \
        {                                                                   \
            return ret;                                                     \
        }                                                                   \
        return ret;                                                         \
    }                                                                       \
                                                                            \
    static int nv_procfs_release_##name(                                    \
        struct inode *inode,                                                \
        struct file *filep                                                  \
    )                                                                       \
    {                                                                       \
        return single_release(inode, filep);                                \
    }

#define NV_DEFINE_SINGLE_PROCFS_FILE_READ_ONLY(name, lock)                  \
    NV_DEFINE_SINGLE_PROCFS_FILE_HELPER(name, lock)                         \
                                                                            \
    static const nv_proc_ops_t nv_procfs_##name##_fops = {                  \
        NV_PROC_OPS_SET_OWNER()                                             \
        .NV_PROC_OPS_OPEN    = nv_procfs_open_##name,                       \
        .NV_PROC_OPS_READ    = nv_procfs_read_lock_##name,                  \
        .NV_PROC_OPS_LSEEK   = seq_lseek,                                   \
        .NV_PROC_OPS_RELEASE = nv_procfs_release_##name,                    \
    };

#define NV_DEFINE_SINGLE_NVRM_PROCFS_FILE(name) \
    NV_DEFINE_SINGLE_PROCFS_FILE_READ_ONLY(name, nv_system_pm_lock)
