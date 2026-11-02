#!/usr/bin/python3
"""Compile production lifecycle bodies with mocked kernel operations."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = runpy.run_path(str(ROOT / 'tests/check_nvidia.py'))['function']
SOURCE = (ROOT / 'nvpcf_hook.c').read_text()


def body(name):
    return FUNCTION(SOURCE, name, raw=True)[1]


PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <sys/types.h>
#ifdef NEGATIVE_ORDER_TEST
#undef assert
#define assert(e) ((e) ? (void)0 : _Exit(77))
#endif
#include "lifecycle.h"
#include "build_id.h"
#include "text.h"
typedef unsigned char u8;
typedef void (*nvpcf_callback)(void *, unsigned int, void *);
typedef int (*nv_start_callback)(void *, void *);
typedef void (*nv_shutdown_callback)(void *, void *, void *);
struct module { int state; const char *version, *name; };
#define module_name(m) ((m)->name)
struct notifier_block { int unused; };
struct work_struct { int unused; };
struct ftrace_ops { int unused; };
struct seq_file;
struct ftrace_regs { unsigned long args[3], ip; };
static void guard_hook(unsigned long, unsigned long, struct ftrace_ops *, struct ftrace_regs *);
static void event_hook(unsigned long, unsigned long, struct ftrace_ops *, struct ftrace_regs *);
typedef unsigned int u32;
struct driver { const char *name; struct module *owner; };
struct device { struct driver *driver; struct { int lock, runtime_error; } power; };
struct pci_dev { struct device dev; };
#define MODULE_STATE_LIVE 1
#define MODULE_STATE_GOING 2
#define NOTIFY_DONE 0
#define NOTIFY_OK 1
#define PM_SUSPEND_PREPARE 10
#define PM_HIBERNATION_PREPARE 11
#define PM_RESTORE_PREPARE 12
#define PM_POST_SUSPEND 13
#define PM_POST_HIBERNATION 14
#define PM_POST_RESTORE 15
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags))
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define pr_crit(...) ((void)0)
#define pr_err_ratelimited(...) ((void)0)
#define NVPCF_CHANGE 0xc0
#define current ((void *)&token)
typedef void *acpi_handle;
#define WARN_ON(x) assert(!(x))
#define THIS_MODULE (&helper)
#define queue_work(...) (++queued)
#define GFP_KERNEL 0
#define kmalloc(size, flags) malloc(size)
#define kfree(ptr) free(ptr)
static struct module helper, old_module, new_module;
static struct driver driver;
static struct pci_dev pci;
static int queued, capture_error, guard_error, rollback_error, drains, self_puts;
static int old_calls, new_calls, resolutions;
static int config_error, config_checks, system_barriers, resume_queued, work_joins;
static char *expected_nvidia_build_id = "000102";
static unsigned char note_bytes[128];
static size_t note_size;
static int note_error, note_reads;
static const char *gpu_info_text;
static int token;
static bool linux_active, internal_in_use, patched, tls_failure, present, power_emits_event;
static int linux_refs, native_refs, block_error, unblock_error, block_calls, unblock_calls;
static int actual_dispatches, owner_puts;
static unsigned int graces, wait_mask, invalidations, within_calls, redirects;
static bool joined_work, drain_complete, expect_old_symbols;
static struct {
    int lock, action, delivery, guard_ops, event_ops;
    int starting_calls, opening_calls, shutdown_calls, redirected;
    struct { struct { void *addr; } kp; } observer;
    struct work_struct replay_work, resume_work;
    struct nh_state state;
    struct nh_replay_control replay;
    enum nh_phase phase;
    struct nh_owner owner;
    struct { unsigned char b[16]; } gpu_uuid;
    struct nh_owner_ops owner_ops;
    struct nh_fine_ops fine_ops;
    int (*proc_power_show)(struct seq_file *, void *);
    int (*proc_info_show)(struct seq_file *, void *);
    bool fine_held;
    int fine_release_error;
    void *dispatch_task;
    bool dispatch_seen;
    struct module *nvidia_module;
    struct nh_build_id expected_build_id;
    char actual_build_id[NH_BUILD_ID_HEX_SIZE];
    bool build_id_verified;
    struct pci_dev *pci;
    bool accepting, fine_verified, observer_registered, event_registered, uuid_valid;
    bool guards_registered, guard_accepting, legacy_safe, target_unavailable;
    bool nvidia_ref, self_ref, power_hold, auto_resume, block_initialization;
    bool retirement_failed, guard_poisoned, legacy_blocked, exiting;
    int last_error;
    char nvidia_version[64];
    nvpcf_callback original;
    nv_start_callback original_open, original_start;
    nv_shutdown_callback original_shutdown;
    void (*acpi_cleanup)(void);
    unsigned long open_ip, start_ip, shutdown_ip;
    void *data, *handle;
} h;
static int old_get(const unsigned char *uuid, void *stack)
{ (void)uuid; (void)stack; old_calls++; return 0; }
static int new_get(const unsigned char *uuid, void *stack)
{ (void)uuid; (void)stack; new_calls++; return 0; }
static void dummy(void) { }
static bool within_module(unsigned long ip, struct module *module)
{ (void)ip; within_calls++; assert(module); return true; }
static void ftrace_regs_set_instruction_pointer(struct ftrace_regs *regs, unsigned long ip)
{ assert(regs); redirects++; regs->ip = ip; }
static unsigned long ftrace_regs_get_argument(struct ftrace_regs *regs, unsigned int arg)
{ assert(regs && arg < 3); return regs->args[arg]; }
static void atomic_inc(int *value) { (*value)++; }
static int atomic_read(const int *value) { return *value; }
static int guarded_open(void *a, void *b) { (void)a; (void)b; assert(false); return 0; }
static int guarded_start(void *a, void *b) { (void)a; (void)b; assert(false); return 0; }
static void guarded_shutdown(void *a, void *b, void *c)
{ (void)a; (void)b; (void)c; assert(false); }
static void capture_event(void *a, unsigned int b, void *c)
{ (void)a; (void)b; (void)c; assert(false); }
static void assert_old_symbols(void)
{
    if (!expect_old_symbols) return;
    assert(h.nvidia_module == &old_module && h.owner_ops.get == old_get);
    assert(h.owner_ops.put && h.owner_ops.free && h.fine_ops.block && h.fine_ops.unblock);
    assert(h.original == (nvpcf_callback)dummy &&
           h.original_open == (nv_start_callback)dummy &&
           h.original_start == (nv_start_callback)dummy &&
           h.original_shutdown == (nv_shutdown_callback)dummy);
    assert(h.acpi_cleanup == dummy && h.open_ip == 123 && h.start_ip == 123 && h.shutdown_ip == 123);
    assert(h.data == &token && h.handle == &token);
    assert(h.proc_power_show && h.proc_info_show);
}
static void record_grace(void)
{
    assert_old_symbols();
    assert(!h.guard_accepting && !h.accepting && !h.replay.enabled);
    guard_hook(1, 2, NULL, NULL);
    event_hook(1, 2, NULL, NULL);
    assert(!within_calls && !redirects);
    if (!(graces & 1u)) {
        wait_mask = 0;
        joined_work = drain_complete = false;
    } else {
        assert(wait_mask == 3 && joined_work);
        drain_complete = true;
    }
    graces++;
}
static void record_wait(const char *queue)
{
    assert_old_symbols();
    assert((graces & 1u) && !drain_complete);
    if (strstr(queue, "guard_wait")) {
        assert(!(wait_mask & 1u));
        h.starting_calls = h.opening_calls = h.shutdown_calls = 0;
        wait_mask |= 1u;
    } else {
        assert(strstr(queue, "redirect_wait") && !(wait_mask & 2u));
        h.redirected = 0;
        wait_mask |= 2u;
    }
}
#define synchronize_rcu_tasks() record_grace()
#define wait_event(queue, condition) do { record_wait(#queue); assert(condition); } while (0)

static bool pending(void) { return nh_pending(&h.state) != 0; }
static void set_error(int error) { h.last_error = error; }
static void mutex_lock(int *lock) { (void)lock; }
static void mutex_unlock(int *lock) { (void)lock; }
static void unlock_action(void) { }
static int start_capture(void)
{ if (!capture_error) h.event_registered = true; return capture_error; }
static int stop_capture(void)
{ if (!capture_error) { h.event_registered = h.accepting = h.replay.enabled = false; } return capture_error; }
static int stop_guards(void)
{
    h.guard_accepting = false;
    if (!guard_error) h.guards_registered = false;
    return guard_error;
}
static void stop_observer(void) { h.observer_registered = false; }
static void release_active(bool gone)
{ (void)gone; if (h.power_hold) { assert(linux_refs > 0); linux_refs--; h.power_hold = false; } }
static int hold_active(void)
{ if (!h.power_hold) { linux_active = true; linux_refs++; h.power_hold = true; } return 0; }
static int initial_rollback(void *ctx) { (void)ctx; return rollback_error; }
static bool try_module_get(struct module *m) { return m->state == MODULE_STATE_LIVE; }
static void module_put(struct module *m) { assert(m == &helper); self_puts++; }
static void strscpy(char *dest, const char *src, size_t size)
{ assert(strlen(src) < size); memcpy(dest, src, strlen(src) + 1); }
static int power_failure(int error)
{ h.state.power_failures++; h.last_error = error; return error; }
static int pm_runtime_get_if_active(struct device *dev)
{ (void)dev; if (!linux_active) return 0; linux_refs++; return 1; }
static int pm_runtime_resume_and_get(struct device *dev)
{ (void)dev; linux_active = true; linux_refs++; return 0; }
static void pm_runtime_put(struct device *dev)
{ (void)dev; assert(linux_refs > 0); linux_refs--; }
static bool pci_device_is_present(struct pci_dev *dev) { (void)dev; return present; }
static int block_fine(const unsigned char *uuid, void *stack)
{
    (void)uuid; (void)stack;
    assert(linux_refs > 0 && linux_active);
    block_calls++;
    if (block_error) return block_error;
    native_refs++;
    internal_in_use = true;
    if (power_emits_event) h.dispatch_seen = true;
    return 0;
}
static int unblock_fine(const unsigned char *uuid, void *stack)
{
    (void)uuid; (void)stack;
    assert(linux_refs > 0 && linux_active && native_refs > 0);
    unblock_calls++;
    if (unblock_error) return unblock_error;
    native_refs--;
    internal_in_use = native_refs > 0;
    return 0;
}
static void original_callback(void *handle, unsigned int event, void *data)
{
    (void)handle; (void)data; assert(event == NVPCF_CHANGE && linux_active);
    if ((patched && !internal_in_use) || tls_failure) return;
    actual_dispatches++;
    h.dispatch_seen = true;
}
static void owner_put(const unsigned char *uuid, void *stack)
{ (void)uuid; (void)stack; assert(!native_refs && !h.fine_held && linux_refs > 0); owner_puts++; }
static void stack_free(void *stack) { (void)stack; assert(!native_refs && !h.fine_held); }
static unsigned int lock_system_sleep(void) { system_barriers++; return 0; }
static void unlock_system_sleep(unsigned int flags) { (void)flags; }
static void device_lock(struct device *dev) { (void)dev; }
static void device_unlock(struct device *dev) { (void)dev; }
static void cancel_work_sync(struct work_struct *work)
{ (void)work; assert_old_symbols(); work_joins++; if (graces & 1u) { assert(wait_mask == 3); joined_work = true; drains++; } }
static void schedule_work(struct work_struct *work) { (void)work; resume_queued++; }
static int check_configuration(void) { config_checks++; return config_error; }
static int register_kretprobe(void *probe) { (void)probe; return 0; }
static int prepare_open_locked(void) { return 0; }
static void unpin_nvidia(void) { h.nvidia_ref = false; }
static ssize_t read_blob(const char *name, void *data, size_t size)
{
    assert(h.nvidia_ref && h.nvidia_module && h.nvidia_module->state == MODULE_STATE_LIVE);
    assert(!strcmp(name, "/sys/module/nvidia/notes/.note.gnu.build-id"));
    note_reads++;
    if (note_error) return note_error;
    assert(note_size <= size);
    memcpy(data, note_bytes, note_size);
    return (ssize_t)note_size;
}
static bool inventory_matches(void) { return true; }
static const char *pci_name(struct pci_dev *dev) { (void)dev; return "0000:01:00.0"; }
static int read_gpu_text(const char *name, int (*show)(struct seq_file *, void *), char *buf, size_t size)
{ assert(name && show && strlen(gpu_info_text) < size); memcpy(buf, gpu_info_text, strlen(gpu_info_text) + 1); return 0; }
static int uuid_parse(const char *text, void *uuid)
{ if (strcmp(text, "00112233-4455-6677-8899-aabbccddeeff")) return -EINVAL; memset(uuid, 0x11, 16); return 0; }
static int stack_alloc(void **stack) { *stack = NULL; return 0; }
static int check_legacy_methods(void) { return 0; }
static unsigned long event_symaddr(unsigned long address) { return address; }
static int resolve_nvidia(const char *name, unsigned long *address)
{
    assert(h.build_id_verified && h.nvidia_ref && note_reads > 0);
    resolutions++;
    *address = (unsigned long)dummy;
    if (!strcmp(name, "nvidia:nvidia_dev_get_uuid"))
        *address = (unsigned long)new_get;
    if (!strcmp(name, "nvidia:nvidia_dev_block_gc6"))
        *address = (unsigned long)block_fine;
    if (!strcmp(name, "nvidia:nvidia_dev_unblock_gc6"))
        *address = (unsigned long)unblock_fine;
    return 0;
}
static int ftrace_set_filter_ip(void *ops, unsigned long ip, int remove, int reset)
{ (void)ops; (void)ip; (void)remove; (void)reset; return 0; }
static int register_ftrace_function(void *ops) { (void)ops; return 0; }
'''

FUNCTIONS = (
    ('int', 'verify_nvidia_build_id', 'void'),
    ('int', 'acquire_fine_reference', 'void'),
    ('int', 'release_fine_reference', 'void'),
    ('int', 'cleanup_fine_reference_if_active', 'void'),
    ('int', 'deliver_one', 'bool force'),
    ('int', 'put_gpu', 'void'),
    ('int', 'get_gpu_locked', 'void'),
    ('void', 'activate_capture', 'void'),
    ('int', 'lifecycle_restore', 'void *unused'),
    ('void', 'guard_hook', 'unsigned long ip, unsigned long parent_ip, struct ftrace_ops *ops, struct ftrace_regs *regs'),
    ('void', 'event_hook', 'unsigned long ip, unsigned long parent_ip, struct ftrace_ops *ops, struct ftrace_regs *regs'),
    ('void', 'drain_disabled_redirects', 'void'),
    ('void', 'invalidate_nvidia_symbols', 'void'),
    ('int', 'pin_nvidia_locked', 'void'),
    ('int', 'start_guards', 'void'),
    ('bool', 'attachment_blocked', 'void'),
    ('int', 'attach_locked', 'bool activate'),
    ('void', 'resume_work', 'struct work_struct *work'),
    ('int', 'pm_notify', 'struct notifier_block *nb, unsigned long action, void *unused'),
    ('int', 'module_notify', 'struct notifier_block *nb, unsigned long action, void *data'),
    ('int', 'retry_retirement', 'void'),
    ('int', 'preflight', 'void'),
    ('int', 'lifecycle_stop', 'void *unused'),
    ('int', 'lifecycle_hold', 'void *unused'),
    ('int', 'lifecycle_dispatch', 'void *unused'),
    ('void', 'lifecycle_unhold', 'void *unused'),
    ('int', 'lifecycle_release', 'void *unused'),
    ('void', 'lifecycle_observer_stop', 'void *unused'),
)

TESTS = r'''
static void reset(void)
{
    memset(&h, 0, sizeof(h));
    queued = capture_error = guard_error = rollback_error = drains = self_puts = 0;
    old_calls = new_calls = resolutions = 0;
    graces = wait_mask = invalidations = within_calls = redirects = 0;
    joined_work = drain_complete = expect_old_symbols = false;
    old_module = (struct module){ MODULE_STATE_GOING, "old", "nvidia" };
    new_module = (struct module){ MODULE_STATE_LIVE, "replacement", "nvidia" };
    driver = (struct driver){ "nvidia", &new_module };
    pci.dev.driver = &driver;
    h.pci = &pci;
    memset(note_bytes, 0, sizeof(note_bytes));
    note_bytes[0] = 4; note_bytes[4] = 3; note_bytes[8] = 3;
    memcpy(note_bytes + 12, "GNU", 4);
    note_bytes[17] = 1; note_bytes[18] = 2; note_size = 19;
    note_error = note_reads = 0;
    assert(!nh_parse_build_id(expected_nvidia_build_id, &h.expected_build_id));
    h.build_id_verified = true;
    h.proc_power_show = h.proc_info_show = (int (*)(struct seq_file *, void *))dummy;
    config_error = config_checks = system_barriers = resume_queued = work_joins = 0;
    linux_active = internal_in_use = patched = tls_failure = power_emits_event = false;
    present = true;
    linux_refs = native_refs = block_error = unblock_error = 0;
    block_calls = unblock_calls = actual_dispatches = owner_puts = 0;
    h.fine_ops = (struct nh_fine_ops){ block_fine, unblock_fine };
    h.owner_ops.put = owner_put;
    h.owner_ops.free = stack_free;
    h.owner_ops.alloc = stack_alloc;
    gpu_info_text = "Model: GPU without a cached UUID\n";
}
static void test_actual_drain(void)
{
    reset();
    h.nvidia_module = &old_module;
    h.guard_accepting = h.accepting = h.replay.enabled = true;
    h.starting_calls = h.opening_calls = h.shutdown_calls = h.redirected = 1;
    drain_disabled_redirects();
    assert(graces == 2 && wait_mask == 3 && joined_work && drain_complete);
    assert(!h.guard_accepting && !h.accepting && !h.replay.enabled);
    assert(!h.starting_calls && !h.opening_calls && !h.shutdown_calls && !h.redirected);
    assert(!invalidations && !within_calls && !redirects && work_joins == 1);
}
static void test_disabled_handlers_without_module(void)
{
    unsigned int flags;
    for (flags = 0; flags < 2; ++flags) {
        reset();
        h.accepting = h.guard_accepting = flags != 0;
        guard_hook(1, 2, NULL, NULL);
        event_hook(1, 2, NULL, NULL);
        assert(!within_calls && !redirects);
    }
}
static void test_actual_restore(void)
{
    reset();
    h.owner.held = h.observer_registered = h.guards_registered = h.legacy_safe = true;
    h.accepting = true;
    h.phase = QUIESCED;
    h.fine_verified = false;
    h.last_error = -EOPNOTSUPP;
    nh_receive(&h.state, true);
    assert(!lifecycle_restore(NULL));
    assert(h.phase == QUIESCED && h.accepting && !h.replay.enabled);
    assert(h.last_error == -EOPNOTSUPP && queued == 0 && pending());
    assert(!nh_monitoring_active(h.phase, &h.replay, h.accepting, h.fine_verified));
    h.phase = WAITING;
    h.fine_verified = true;
    activate_capture();
    assert(h.phase == ACTIVE && h.replay.enabled && h.last_error == 0);
    assert(queued == 1 && pending());
}
static void test_actual_retirement(void)
{
    unsigned int fault, reuse, failure;
    for (failure = 1; failure < 4; ++failure)
    for (fault = 0; fault < 2; ++fault)
    for (reuse = 0; reuse < 2; ++reuse) {
        reset();
        h.nvidia_module = &old_module;
        h.guards_registered = h.guard_accepting = h.self_ref = true;
        h.event_registered = true;
        h.owner_ops.get = old_get;
        h.data = h.handle = &token;
        h.original_open = (nv_start_callback)dummy;
        h.original_start = (nv_start_callback)dummy;
        h.original_shutdown = (nv_shutdown_callback)dummy;
        h.original = (nvpcf_callback)dummy;
        h.acpi_cleanup = dummy;
        h.open_ip = h.start_ip = h.shutdown_ip = 123;
        guard_error = (failure & 1u) ? -ENODEV : 0;
        capture_error = (failure & 2u) ? -ENODEV : 0;
        expect_old_symbols = true;
        h.starting_calls = h.opening_calls = h.shutdown_calls = h.redirected = 1;
        assert(module_notify(NULL, MODULE_STATE_GOING, &old_module) == NOTIFY_DONE);
        assert(h.retirement_failed && !h.guard_accepting);
        assert(h.guards_registered == ((failure & 1u) != 0));
        assert(h.event_registered == ((failure & 2u) != 0));
        assert(h.phase == UNAVAILABLE && h.last_error == -ENODEV && drains == 1);
        assert(h.self_ref && !self_puts && !h.nvidia_module && !h.owner_ops.get);
        assert(!h.original && !h.original_open && !h.original_start && !h.original_shutdown);
        assert(!h.acpi_cleanup && !h.open_ip && !h.start_ip && !h.shutdown_ip);
        assert(!h.data && !h.handle && !h.fine_ops.block && !h.fine_ops.unblock);
        assert(!h.proc_power_show && !h.proc_info_show);
        /* A replacement can even occupy the old module's address. Neither
         * actual admission body may resolve or reuse the retired addresses.
         */
        if (reuse) {
            old_module.state = MODULE_STATE_LIVE;
            driver.owner = &old_module;
        }
        assert(pin_nvidia_locked() == -EUCLEAN);
        assert(start_guards() == -EUCLEAN);
        assert(!resolutions && !old_calls && !new_calls);
        assert(retry_retirement() == -ENODEV && h.retirement_failed);
        guard_error = capture_error = 0;
        h.guard_poisoned = fault != 0;
        assert(retry_retirement() == (fault ? -EUCLEAN : 0));
        assert(!h.retirement_failed && !h.guards_registered);
        assert(h.guard_poisoned == (fault != 0));
        assert(h.phase == (fault ? UNAVAILABLE : DETACHED));
        if (fault) {
            assert(!nh_may_unpin_helper(1, 1, 0, h.guard_poisoned));
            continue;
        }
        assert(!pin_nvidia_locked());
        assert(!start_guards() && resolutions > 0 && h.owner_ops.get == new_get);
        assert(!h.owner_ops.get(NULL, NULL));
        assert(new_calls == 1 && old_calls == 0);
    }
}
static const struct nh_lifecycle_ops delivery_ops = {
    .stop_capture = lifecycle_stop, .hold_active = lifecycle_hold,
    .dispatch = lifecycle_dispatch, .restore_capture = lifecycle_restore,
    .release_active = lifecycle_unhold, .release_owner = lifecycle_release,
    .stop_observer = lifecycle_observer_stop,
};
static void prepare_delivery(bool use_patch)
{
    reset();
    patched = use_patch;
    h.owner.held = h.owner.stack_ready = h.nvidia_ref = true;
    h.observer_registered = h.guards_registered = h.legacy_safe = true;
    h.fine_verified = h.accepting = h.event_registered = h.replay.enabled = true;
    h.phase = ACTIVE;
    h.original = original_callback;
    h.data = &token;
    nh_receive(&h.state, true);
}
static void test_actual_two_state_delivery(void)
{
    unsigned int patch;
    for (patch = 0; patch < 2; ++patch) {
        struct nh_quiesce_result result;
        prepare_delivery(patch != 0);
        assert(deliver_one(false) == -EAGAIN);
        assert(!block_calls && !linux_refs && !linux_active && pending());
        if (patched) {
            /* Linux resume alone repeatedly leaves NVIDIA IDLE_INDICATED. */
            assert(!pm_runtime_resume_and_get(&pci.dev));
            original_callback(NULL, NVPCF_CHANGE, &token);
            assert(!h.dispatch_seen && !internal_in_use && pending());
            pm_runtime_put(&pci.dev);
        }
        assert(!preflight());
        assert(!pending() && actual_dispatches == 1 && block_calls == 1 && unblock_calls == 1);
        assert(!linux_refs && !native_refs && !h.fine_held && h.owner.held);
        /* Final detach must also finish from ACTIVE Linux / idle NVIDIA. */
        nh_receive(&h.state, true);
        assert(!internal_in_use && linux_active);
        result = nh_quiesce(NULL, &delivery_ops);
        assert(!result.error && !result.restore_error && !h.owner.held);
        assert(actual_dispatches == 2 && block_calls == 2 && unblock_calls == 2);
        assert(!linux_refs && !native_refs && !h.fine_held && owner_puts == 1);
    }
    prepare_delivery(true);
    block_error = -EINVAL;
    assert(deliver_one(true) == -EINVAL && pending());
    assert(!linux_refs && !native_refs && !unblock_calls && !h.fine_held);
    prepare_delivery(true);
    tls_failure = power_emits_event = true;
    assert(deliver_one(true) == -EIO && pending());
    assert(block_calls == 1 && unblock_calls == 1 && !linux_refs && !native_refs);
    prepare_delivery(true);
    unblock_error = -ENODEV;
    {
        struct nh_quiesce_result result = nh_quiesce(NULL, &delivery_ops);
        assert(result.error == -ENODEV && !result.restore_error);
    }
    assert(!pending() && actual_dispatches == 1); /* Observed dispatch is reported. */
    assert(h.fine_held && native_refs == 1 && !linux_refs);
    assert(h.owner.held && h.owner.stack_ready && h.nvidia_ref && !owner_puts);
    assert(h.phase == QUIESCED && h.accepting && !h.replay.enabled);
    assert(put_gpu() == -EBUSY && h.owner.held && h.owner.stack_ready);
    unblock_error = 0;
    assert(!preflight()); /* Zero pending still cleans the outstanding reference. */
    assert(!h.fine_held && !native_refs && !linux_refs && !h.fine_release_error);
    assert(actual_dispatches == 1 && block_calls == 1 && unblock_calls == 2);
    activate_capture();
    assert(h.phase == ACTIVE && h.replay.enabled);
    assert(!nh_quiesce(NULL, &delivery_ops).error);
    assert(owner_puts == 1 && !h.owner.held && !native_refs && !linux_refs);
}
static void test_actual_pm_admission(void)
{
    unsigned int late, failure;
    for (late = 0; late < 2; ++late)
    for (failure = 0; failure < 4; ++failure) {
        prepare_delivery(true);
        h.nvidia_module = &new_module;
        h.block_initialization = true;
        h.last_error = -EIO;
        if (!late) {
            if (failure == 0) h.retirement_failed = true;
            if (failure == 1) h.guard_poisoned = true;
            if (failure == 2) h.legacy_blocked = true;
            if (failure == 3) h.state.stale = true;
            h.phase = UNAVAILABLE;
        }
        assert(pm_notify(NULL, PM_SUSPEND_PREPARE, NULL) == NOTIFY_OK);
        if (late) {
            assert(h.phase == SUSPENDED && h.auto_resume);
            if (failure == 0) h.retirement_failed = true;
            if (failure == 1) h.guard_poisoned = true;
            if (failure == 2) h.legacy_blocked = true;
            if (failure == 3) h.state.stale = true;
        }
        assert(pm_notify(NULL, PM_POST_SUSPEND, NULL) == NOTIFY_DONE);
        assert(resume_queued == 1 && work_joins == 1 && !system_barriers);
        resume_work(NULL);
        assert(h.phase == UNAVAILABLE && !h.replay.enabled && !h.replay.paused);
        assert(h.last_error == -EIO && h.block_initialization && !h.auto_resume);
        assert(h.owner.held && h.nvidia_ref && !config_checks && !resolutions);
        assert(attach_locked(true) == -EUCLEAN && !config_checks);
        assert(system_barriers == 1);
    }
    prepare_delivery(true);
    h.nvidia_module = &new_module;
    assert(pm_notify(NULL, PM_SUSPEND_PREPARE, NULL) == NOTIFY_OK);
    assert(pm_notify(NULL, PM_POST_SUSPEND, NULL) == NOTIFY_DONE);
    resume_work(NULL);
    assert(h.phase == ACTIVE && h.replay.enabled && config_checks == 1);
    assert(system_barriers == 1 && h.owner.held && h.nvidia_ref);
}
static void test_actual_build_id_admission(void)
{
    reset();
    h.build_id_verified = false;
    new_module.state = 0; /* COMING is not a completely loaded instance. */
    assert(pin_nvidia_locked() == -ENODEV && !note_reads && !resolutions);
    new_module.state = MODULE_STATE_LIVE;
    new_module.name = "different_module";
    assert(pin_nvidia_locked() == -ENODEV && !note_reads && !resolutions);
    new_module.name = "nvidia";
    note_bytes[16] = 0xaa;
    assert(pin_nvidia_locked() == -ESTALE);
    assert(h.nvidia_ref && !h.build_id_verified && note_reads == 1 && !resolutions);
    assert(!strcmp(h.actual_build_id, "aa0102"));
    assert(start_guards() == -ESTALE && !resolutions);
    note_bytes[16] = 0;
    assert(!pin_nvidia_locked() && h.build_id_verified && note_reads == 2);
    assert(!start_guards() && resolutions > 0);
    /* Revalidation of retained ownership must read the note again, before
     * configuration/private calls, not trust the earlier successful check.
     */
    h.owner.held = h.owner.stack_ready = true;
    h.phase = WAITING;
    h.accepting = h.event_registered = h.observer_registered = h.legacy_safe = true;
    h.fine_verified = true;
    note_bytes[16] = 0xbb;
    {
        int previous = note_reads;
        resume_work(NULL);
        assert(note_reads == previous + 1 && !h.build_id_verified);
        assert(h.phase == QUIESCED && h.last_error == -ESTALE && !config_checks && h.fine_verified);
    }
    activate_capture();
    assert(h.phase == QUIESCED && !h.replay.enabled && h.last_error == -ESTALE);
    note_bytes[16] = 0;
    h.phase = WAITING;
    resume_work(NULL);
    assert(h.phase == ACTIVE && h.replay.enabled && h.build_id_verified);
    note_error = -ENOENT;
    assert(verify_nvidia_build_id() == -ENOENT && !h.build_id_verified);
    note_error = 0; note_size = 11;
    assert(verify_nvidia_build_id() == -EBADMSG && !h.build_id_verified);
    /* Retire one instance, then reject a different replacement descriptor
     * even though its LIVE module and required symbol names are available.
     */
    reset();
    h.nvidia_module = &old_module;
    h.self_ref = h.guards_registered = true;
    assert(module_notify(NULL, MODULE_STATE_GOING, &old_module) == NOTIFY_DONE);
    assert(!h.nvidia_module && !h.build_id_verified && h.phase == DETACHED);
    note_bytes[16] = 0xcc;
    assert(pin_nvidia_locked() == -ESTALE && note_reads == 1 && !resolutions);
    assert(start_guards() == -ESTALE && !resolutions);
}
static void test_actual_waiting_for_uuid(void)
{
    reset();
    h.nvidia_ref = true;
    h.owner_ops.get = new_get;
    assert(!hold_active());
    assert(get_gpu_locked() == -EAGAIN && !h.owner.held && !new_calls);
    release_active(false);
    gpu_info_text = "GPU UUID: unknown\n";
    assert(!hold_active());
    assert(get_gpu_locked() == -EINVAL && !h.owner.held && !new_calls);
    release_active(false);
    gpu_info_text = "GPU UUID: GPU-malformed\n";
    assert(!hold_active());
    assert(get_gpu_locked() == -EINVAL && !h.owner.held && !new_calls);
    release_active(false);
    gpu_info_text = "GPU UUID: GPU-00112233-4455-6677-8899-aabbccddeeff\n";
    assert(!hold_active());
    assert(!get_gpu_locked() && h.owner.held && h.uuid_valid && new_calls == 1);
    assert(!put_gpu());
    release_active(false);
    assert(!linux_refs);
}
int main(void)
{
    test_actual_drain();
    test_disabled_handlers_without_module();
    test_actual_restore();
    test_actual_retirement();
    test_actual_two_state_delivery();
    test_actual_pm_admission();
    test_actual_build_id_admission();
    test_actual_waiting_for_uuid();
    puts("PASS: compiled production restoration/recovery and failed retirement/replacement/address-reuse and two-state stock/patched delivery and PM/build-ID admission paths");
    return 0;
}
'''

def declarations(negative=False):
    result = []
    for result_type, name, args in FUNCTIONS:
        original = body(name)
        if negative and name == 'module_notify':
            assert original.count('drain_disabled_redirects();') == 1
            original = original.replace('drain_disabled_redirects();',
                                        '{ invalidate_nvidia_symbols(); drain_disabled_redirects(); }')
        compiled_name = 'invalidate_nvidia_symbols_impl' if name == 'invalidate_nvidia_symbols' else name
        result.append(f'static {result_type} {compiled_name}({args})\n{original}\n')
        if name == 'invalidate_nvidia_symbols':
            result.append(r"""
static void invalidate_nvidia_symbols(void)
{
    if (h.guards_registered || h.event_registered)
        assert(drain_complete && joined_work && graces >= 2 && wait_mask == 3);
    assert(!h.starting_calls && !h.opening_calls && !h.shutdown_calls && !h.redirected);
    assert_old_symbols();
    expect_old_symbols = false;
    invalidate_nvidia_symbols_impl();
    invalidations++;
    guard_hook(1, 2, NULL, NULL);
    event_hook(1, 2, NULL, NULL);
    assert(!within_calls && !redirects);
}
""")
    return '\n'.join(result)


with tempfile.TemporaryDirectory(prefix='nvpcf-production-transitions-') as tmp:
    root = Path(tmp)
    for negative in (False, True):
        source = root / ('negative.c' if negative else 'transitions.c')
        binary = root / ('negative' if negative else 'transitions')
        source.write_text(PREFIX + declarations(negative) + TESTS)
        command = ['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter', '-Wno-cast-function-type', '-I', str(ROOT)]
        if negative:
            command.append('-DNEGATIVE_ORDER_TEST')
        subprocess.run(command + [str(source), '-o', str(binary)], check=True)
        execution = subprocess.run([str(binary)], check=False)
        assert execution.returncode == (77 if negative else 0), execution.returncode
    print('PASS: production disabled handlers/drain order; invalidation-before-drain mutation is rejected')
