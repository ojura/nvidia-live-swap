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
#include "lifecycle.h"
typedef unsigned char u8;
typedef void (*nvpcf_callback)(void *, unsigned int, void *);
typedef int (*nv_start_callback)(void *, void *);
typedef void (*nv_shutdown_callback)(void *, void *, void *);
struct module { int state; const char *version; };
struct notifier_block { int unused; };
struct driver { const char *name; struct module *owner; };
struct device { struct driver *driver; };
struct pci_dev { struct device dev; };
#define MODULE_STATE_LIVE 1
#define MODULE_STATE_GOING 2
#define NOTIFY_DONE 0
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags))
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define WARN_ON(x) assert(!(x))
#define THIS_MODULE (&helper)
#define queue_work(...) (++queued)
static struct module helper, old_module, new_module;
static struct driver driver;
static struct pci_dev pci;
static int queued, capture_error, guard_error, rollback_error, drains, self_puts;
static int old_calls, new_calls, resolutions;
static int token;
static struct {
    int lock, action, guard_ops;
    struct nh_state state;
    struct nh_replay_control replay;
    enum nh_phase phase;
    struct nh_owner owner;
    struct nh_owner_ops owner_ops;
    struct module *nvidia_module;
    struct pci_dev *pci;
    bool accepting, fine_verified, observer_registered, event_registered;
    bool guards_registered, guard_accepting, legacy_safe, target_unavailable;
    bool nvidia_ref, self_ref, power_hold, auto_resume, block_initialization;
    bool retirement_failed, guard_poisoned;
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
static bool pending(void) { return nh_pending(&h.state) != 0; }
static void set_error(int error) { h.last_error = error; }
static void mutex_lock(int *lock) { (void)lock; }
static void unlock_action(void) { }
static int start_capture(void)
{ if (!capture_error) h.event_registered = true; return capture_error; }
static int stop_capture(void)
{ if (!capture_error) h.event_registered = false; return capture_error; }
static int stop_guards(void)
{
    h.guard_accepting = false;
    if (!guard_error) h.guards_registered = false;
    return guard_error;
}
static void stop_observer(void) { h.observer_registered = false; }
static void release_active(bool gone) { (void)gone; h.power_hold = false; }
static void drain_disabled_redirects(void)
{ drains++; h.guard_accepting = h.accepting = h.replay.enabled = false; }
static int hold_active(void) { h.power_hold = true; return 0; }
static int initial_rollback(void *ctx) { (void)ctx; return rollback_error; }
static bool try_module_get(struct module *m) { return m->state == MODULE_STATE_LIVE; }
static void module_put(struct module *m) { assert(m == &helper); self_puts++; }
static void strscpy(char *dest, const char *src, size_t size)
{ assert(strlen(src) < size); memcpy(dest, src, strlen(src) + 1); }
static int check_legacy_methods(void) { return 0; }
static unsigned long event_symaddr(unsigned long address) { return address; }
static int resolve_nvidia(const char *name, unsigned long *address)
{
    resolutions++;
    *address = (unsigned long)dummy;
    if (!strcmp(name, "nvidia:nvidia_dev_get_uuid"))
        *address = (unsigned long)new_get;
    return 0;
}
static int ftrace_set_filter_ip(void *ops, unsigned long ip, int remove, int reset)
{ (void)ops; (void)ip; (void)remove; (void)reset; return 0; }
static int register_ftrace_function(void *ops) { (void)ops; return 0; }
'''

FUNCTIONS = (
    ('void', 'activate_capture', 'void'),
    ('int', 'lifecycle_restore', 'void *unused'),
    ('void', 'invalidate_nvidia_symbols', 'void'),
    ('int', 'pin_nvidia_locked', 'void'),
    ('int', 'start_guards', 'void'),
    ('int', 'module_notify', 'struct notifier_block *nb, unsigned long action, void *data'),
    ('int', 'retry_retirement', 'void'),
)

TESTS = r'''
static void reset(void)
{
    memset(&h, 0, sizeof(h));
    queued = capture_error = guard_error = rollback_error = drains = self_puts = 0;
    old_calls = new_calls = resolutions = 0;
    old_module = (struct module){ MODULE_STATE_GOING, "old" };
    new_module = (struct module){ MODULE_STATE_LIVE, "replacement" };
    driver = (struct driver){ "nvidia", &new_module };
    pci.dev.driver = &driver;
    h.pci = &pci;
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
        assert(module_notify(NULL, MODULE_STATE_GOING, &old_module) == NOTIFY_DONE);
        assert(h.retirement_failed && !h.guard_accepting);
        assert(h.guards_registered == ((failure & 1u) != 0));
        assert(h.event_registered == ((failure & 2u) != 0));
        assert(h.phase == UNAVAILABLE && h.last_error == -ENODEV && drains == 1);
        assert(h.self_ref && !self_puts && !h.nvidia_module && !h.owner_ops.get);
        assert(!h.original && !h.original_open && !h.original_start && !h.original_shutdown);
        assert(!h.acpi_cleanup && !h.open_ip && !h.start_ip && !h.shutdown_ip);
        assert(!h.data && !h.handle);
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
int main(void)
{
    test_actual_restore();
    test_actual_retirement();
    puts("PASS: compiled production restoration/recovery and failed retirement/replacement/address-reuse paths");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='nvpcf-production-transitions-') as tmp:
    root = Path(tmp)
    source, binary = root / 'transitions.c', root / 'transitions'
    declarations = '\n'.join(f'static {result} {name}({args})\n{body(name)}\n'
                             for result, name, args in FUNCTIONS)
    source.write_text(PREFIX + declarations + TESTS)
    subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-Wno-cast-function-type', '-I', str(ROOT),
                    str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
