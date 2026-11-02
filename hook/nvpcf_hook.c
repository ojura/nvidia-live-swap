// SPDX-License-Identifier: GPL-2.0
/* Coalesce NVPCF CHANGE callbacks without waking an idle NVIDIA GPU. */
#include <linux/acpi.h>
#include <linux/capability.h>
#include <linux/device.h>
#include <linux/file.h>
#include <linux/fs.h>
#include <linux/ftrace.h>
#include <linux/kprobes.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/pm_runtime.h>
#include <linux/rcupdate.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/suspend.h>
#include <linux/tracepoint.h>
#include <linux/uaccess.h>
#include <linux/version.h>
#include <linux/wait.h>
#include <linux/uuid.h>
#include <linux/workqueue.h>
#include <trace/events/rpm.h>
#include <asm/ibt.h>
#include "state.h"
#include "text.h"
#include "lifecycle.h"
#include "build_id.h"

#if !defined(CONFIG_X86_64) || !defined(CONFIG_DYNAMIC_FTRACE_WITH_REGS) || \
    !defined(CONFIG_KRETPROBES) || !defined(CONFIG_PM_SLEEP)
#error "nvpcf_hook requires x86-64, ftrace registers, kretprobes and system PM"
#endif

#if defined(CONFIG_X86_KERNEL_IBT) || defined(CONFIG_CFI_CLANG)
#error "nvpcf_hook private function-pointer calls require kernel IBT and CFI disabled"
#endif

#define NVPCF_CHANGE 0xc0
/* cl2080_notification.h:218 in all four source versions listed in README.md. */
#define NVPCF_NOTIFY_INDEX 177

typedef void (*nvpcf_callback)(acpi_handle, u32, void *);
typedef int (*nv_start_callback)(void *, void *);
typedef void (*nv_shutdown_callback)(void *, void *, void *);
static const char * const phase_names[] = {
	"starting", "waiting", "active", "quiesced", "suspended", "guard-only",
	"unavailable", "detached"
};

static char *expected_nvidia_build_id;
module_param(expected_nvidia_build_id, charp, 0400);
MODULE_PARM_DESC(expected_nvidia_build_id,
	"Required GNU build-ID descriptor of the checked NVIDIA module (hex)");

static struct {
	/* Lifecycle order: system sleep/device locks, then action, then delivery. */
	struct mutex action;
	struct mutex delivery;
	spinlock_t lock;
	struct nh_state state;
	enum nh_phase phase;
	int last_error;
	bool accepting;
	bool auto_resume;
	bool self_ref;
	bool nvidia_ref;
	bool power_hold;
	bool guards_registered;
	bool guard_accepting;
	bool block_initialization;
	bool would_initialize;
	bool legacy_safe;
	bool legacy_blocked;
	struct task_struct *open_task;
	nv_start_callback original_start;
	nv_start_callback original_open;
	nv_shutdown_callback original_shutdown;
	void (*acpi_cleanup)(void);
	unsigned long start_ip;
	unsigned long open_ip;
	unsigned long shutdown_ip;
	atomic_t starting_calls;
	atomic_t opening_calls;
	atomic_t shutdown_calls;
	wait_queue_head_t guard_wait;
	u64 prelock_cleanup_calls;
	u64 guard_failures;
	bool guard_poisoned;
	bool retirement_failed;
	struct ftrace_ops guard_ops;
	struct notifier_block module_nb;
	bool event_registered;
	bool resume_registered;
	bool return_registered;
	bool observer_registered;
	u64 observer_missed_total;
	bool exiting;
	bool target_unavailable;
	struct pci_dev *pci;
	struct nh_owner owner;
	bool uuid_valid;
	bool fine_verified;
	struct nh_replay_control replay;
	uuid_t gpu_uuid;
	struct nh_owner_ops owner_ops;
	struct nh_fine_ops fine_ops;
	bool fine_held;
	int fine_release_error;
	struct module *nvidia_module;
	struct nh_build_id expected_build_id;
	char actual_build_id[NH_BUILD_ID_HEX_SIZE];
	bool build_id_verified;
	char nvidia_version[64];
	nvpcf_callback original;
	acpi_handle handle;
	void *data;
	struct task_struct *dispatch_task;
	bool dispatch_seen;
	atomic_t redirected;
	wait_queue_head_t redirect_wait;
	struct ftrace_ops event_ops;
	struct kretprobe observer;
	struct notifier_block pm_nb;
	struct notifier_block bus_nb;
	struct workqueue_struct *replay_wq;
	struct work_struct replay_work;
	struct work_struct resume_work;
} h;

static void notrace capture_event(acpi_handle handle, u32 event, void *data);
static int hold_active(void);
static void release_active(bool driver_gone);
static void unpin_nvidia(void);
static int initial_rollback(void *unused);
static int verify_nvidia_build_id(void);

static void unlock_action(void)
{
	mutex_unlock(&h.action);
}

#ifndef ftrace_get_symaddr
/* Installed 6.8 headers lack the 7.0 helper. Their is_endbr takes the word,
 * rather than a pointer. Keep the same x86 ENDBR-to-entry adjustment.
 */
static __always_inline unsigned long event_symaddr(unsigned long ip)
{
	u32 prefix;

	if (within_module(ip - sizeof(prefix), h.nvidia_module)) {
		memcpy(&prefix, (void *)(ip - sizeof(prefix)), sizeof(prefix));
		if (is_endbr(prefix))
			return ip - sizeof(prefix);
	}
	return ip;
}
#else
#define event_symaddr(ip) ftrace_get_symaddr(ip)
#endif

static void set_error(int error)
{
	unsigned long flags;

	spin_lock_irqsave(&h.lock, flags);
	h.last_error = error;
	spin_unlock_irqrestore(&h.lock, flags);
}

static int power_failure(int error)
{
	unsigned long flags;

	spin_lock_irqsave(&h.lock, flags);
	h.state.power_failures++;
	h.last_error = error;
	spin_unlock_irqrestore(&h.lock, flags);
	return error;
}

static bool pending(void)
{
	unsigned long flags;
	bool result;

	spin_lock_irqsave(&h.lock, flags);
	result = nh_pending(&h.state) != 0;
	spin_unlock_irqrestore(&h.lock, flags);
	return result;
}

static acpi_status legacy_method(acpi_handle handle, u32 level, void *context, void **out)
{
	acpi_handle method;

	if (ACPI_SUCCESS(acpi_get_handle(handle, "NVIF", &method)) ||
	    ACPI_SUCCESS(acpi_get_handle(handle, "WMMX", &method))) {
		*(bool *)context = true;
		return AE_CTRL_TERMINATE;
	}
	return AE_OK;
}

static int check_legacy_methods(void)
{
	bool found = false;
	acpi_status status;

	status = acpi_walk_namespace(ACPI_TYPE_DEVICE, ACPI_ROOT_OBJECT,
		ACPI_UINT32_MAX, legacy_method, NULL, &found, NULL);
	return ACPI_FAILURE(status) || found ? -EOPNOTSUPP : 0;
}

static int resolve_nvidia(const char *name, unsigned long *address)
{
	struct kprobe probe = { .symbol_name = name };
	int ret = register_kprobe(&probe);

	if (ret)
		return ret;
	*address = (unsigned long)probe.addr;
	unregister_kprobe(&probe);
	return 0;
}

struct shutdown_args {
	void *sp;
	void *nv;
	void *nvl;
};

static void cleanup_handlers(void *unused)
{
	unsigned long flags;

	h.acpi_cleanup();
	spin_lock_irqsave(&h.lock, flags);
	h.prelock_cleanup_calls++;
	spin_unlock_irqrestore(&h.lock, flags);
}

static void destroy_resources(void *context)
{
	struct shutdown_args *args = context;

	h.original_shutdown(args->sp, args->nv, args->nvl);
}

static const struct nh_teardown_ops teardown_ops = {
	.cleanup_handlers = cleanup_handlers,
	.destroy_resources = destroy_resources,
};

static void notrace run_teardown(struct shutdown_args *args)
{
	unsigned long flags;
	bool local_ref = false, quiescent;
	int ret = 0;

	if (READ_ONCE(h.replay.paused)) {
		ret = -EHOSTDOWN;
		goto unavailable;
	}
	spin_lock_irqsave(&h.pci->dev.power.lock, flags);
	if (h.pci->dev.power.runtime_status == RPM_ACTIVE &&
	    !h.pci->dev.power.runtime_error) {
		/* PCI removal can disable runtime PM before persistent teardown.
		 * The ACTIVE state is already stable under this lock; noresume
		 * retains it without rejecting a disabled, active device.
		 */
		if (!READ_ONCE(h.power_hold)) {
			pm_runtime_get_noresume(&h.pci->dev);
			local_ref = true;
		}
	} else {
		ret = -EAGAIN;
	}
	spin_unlock_irqrestore(&h.pci->dev.power.lock, flags);
	if (ret) {
		ret = pm_runtime_resume_and_get(&h.pci->dev);
		local_ref = ret >= 0;
	}
	if (!pci_device_is_present(h.pci))
		ret = -ENODEV;
unavailable:
	if (ret < 0) {
		/* Normal detach establishes this reference before releasing its
		 * kernel reference. This branch is involuntary/unavailable-device teardown.
		 * Drain still precedes resource destruction, but delivery failed.
		 */
		spin_lock_irqsave(&h.lock, flags);
		h.guard_failures++;
		h.guard_poisoned = true;
		h.last_error = ret;
		spin_unlock_irqrestore(&h.lock, flags);
		pr_err("nvpcf_hook: committed teardown lacks active power (%d); delivery is unverified\n", ret);
	}
	quiescent = !READ_ONCE(h.event_registered) && !atomic_read(&h.redirected) &&
		    !READ_ONCE(h.dispatch_task);
	if (WARN_ON_ONCE(!quiescent)) {
		/* Supported paths stop deferral before committing teardown. */
		spin_lock_irqsave(&h.lock, flags);
		h.guard_failures++;
		h.guard_poisoned = true;
		h.last_error = -EBUSY;
		spin_unlock_irqrestore(&h.lock, flags);
	}
	if (READ_ONCE(h.legacy_safe) && quiescent) {
		/* nv_shutdown_adapter entry has not taken API/GPU locks or freed
		 * interrupts. NVIDIA clears its own pointers, so later uninit skips
		 * the handlers already removed and drained here.
		 */
		nh_teardown(args, &teardown_ops);
	} else {
		destroy_resources(args);
	}
	if (local_ref)
		pm_runtime_put_noidle(&h.pci->dev);
}

static void notrace guarded_shutdown(void *sp, void *nv, void *nvl)
{
	struct shutdown_args args = { .sp = sp, .nv = nv, .nvl = nvl };

	run_teardown(&args);
	if (atomic_dec_and_test(&h.shutdown_calls))
		wake_up_all(&h.guard_wait);
}

static int notrace guarded_start(void *nv, void *sp)
{
	bool own = READ_ONCE(h.open_task) == current;
	bool safe;
	int ret;

	if (!nh_start_allowed(own, READ_ONCE(h.block_initialization))) {
		if (own)
			WRITE_ONCE(h.would_initialize, true);
		ret = -EBUSY;
		goto out;
	}
	safe = check_legacy_methods() == 0;
	ret = h.original_start(nv, sp);
	if (!ret) {
		safe = safe && check_legacy_methods() == 0;
		WRITE_ONCE(h.legacy_safe, safe);
		if (READ_ONCE(h.phase) == WAITING || READ_ONCE(h.phase) == STARTING ||
		    READ_ONCE(h.phase) == SUSPENDED)
			schedule_work(&h.resume_work);
	}
out:
	if (atomic_dec_and_test(&h.starting_calls))
		wake_up_all(&h.guard_wait);
	return ret;
}

static int notrace guarded_open(void *nv, void *sp)
{
	bool own = READ_ONCE(h.open_task) == current;
	bool safe = READ_ONCE(h.legacy_safe);
	int ret;

	/* nv_open_device runs under ldata_lock. The preceding initializer's
	 * legacy-method result is stable before this open can increment usage.
	 */
	if (!nh_open_allowed(own, safe))
		ret = -EOPNOTSUPP;
	else
		ret = h.original_open(nv, sp);
	if (atomic_dec_and_test(&h.opening_calls))
		wake_up_all(&h.guard_wait);
	return ret;
}

static void notrace guard_hook(unsigned long ip, unsigned long parent_ip,
			       struct ftrace_ops *ops, struct ftrace_regs *regs)
{
	if (!READ_ONCE(h.guard_accepting) || !h.nvidia_module ||
	    !within_module(ip, h.nvidia_module) || within_module(parent_ip, THIS_MODULE))
		return;
	if (ip == h.open_ip) {
		atomic_inc(&h.opening_calls);
		ftrace_regs_set_instruction_pointer(regs, (unsigned long)guarded_open);
	} else if (ip == h.start_ip) {
		atomic_inc(&h.starting_calls);
		ftrace_regs_set_instruction_pointer(regs, (unsigned long)guarded_start);
	} else if (ip == h.shutdown_ip) {
		atomic_inc(&h.shutdown_calls);
		ftrace_regs_set_instruction_pointer(regs, (unsigned long)guarded_shutdown);
	}
}

static int start_guards(void)
{
	unsigned long cleanup_ip, get_ip, put_ip, alloc_ip, free_ip, block_ip, unblock_ip;
	int ret;

	if (!nh_instance_attach_allowed(h.retirement_failed, h.guards_registered,
		h.nvidia_module != NULL))
		return -EUCLEAN;
	if (!h.nvidia_ref || !h.nvidia_module)
		return -ENODEV;
	if (!h.build_id_verified)
		return -ESTALE;
	if (h.guards_registered)
		return 0;
	ret = check_legacy_methods();
	if (ret) {
		pr_err("nvpcf_hook: legacy NVIF/WMMX ACPI methods are unsupported\n");
		return ret;
	}
	WRITE_ONCE(h.legacy_safe, true);
	ret = resolve_nvidia("nvidia:nv_open_device", &h.open_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nv_start_device", &h.start_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nv_shutdown_adapter", &h.shutdown_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nv_acpi_methods_uninit", &cleanup_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_dev_get_uuid", &get_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_dev_put_uuid", &put_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_modeset_rm_ops_alloc_stack", &alloc_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_modeset_rm_ops_free_stack", &free_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_dev_block_gc6", &block_ip);
	if (ret)
		return ret;
	ret = resolve_nvidia("nvidia:nvidia_dev_unblock_gc6", &unblock_ip);
	if (ret)
		return ret;
	h.fine_ops.block = (int (*)(const u8 *, void *))event_symaddr(block_ip);
	h.fine_ops.unblock = (int (*)(const u8 *, void *))event_symaddr(unblock_ip);
	h.owner_ops.get = (int (*)(const u8 *, void *))event_symaddr(get_ip);
	h.owner_ops.put = (void (*)(const u8 *, void *))event_symaddr(put_ip);
	h.owner_ops.alloc = (int (*)(void **))event_symaddr(alloc_ip);
	h.owner_ops.free = (void (*)(void *))event_symaddr(free_ip);
	h.original_open = (nv_start_callback)event_symaddr(h.open_ip);
	h.original_start = (nv_start_callback)event_symaddr(h.start_ip);
	h.original_shutdown = (nv_shutdown_callback)event_symaddr(h.shutdown_ip);
	h.acpi_cleanup = (void (*)(void))event_symaddr(cleanup_ip);
	ret = ftrace_set_filter_ip(&h.guard_ops, h.open_ip, 0, 1);
	if (!ret)
		ret = ftrace_set_filter_ip(&h.guard_ops, h.start_ip, 0, 0);
	if (!ret)
		ret = ftrace_set_filter_ip(&h.guard_ops, h.shutdown_ip, 0, 0);
	if (!ret)
		ret = register_ftrace_function(&h.guard_ops);
	if (!ret) {
		h.guards_registered = true;
		WRITE_ONCE(h.guard_accepting, true);
	}
	return ret;
}

static int stop_guards(void)
{
	bool had_guards = h.guards_registered;
	int ret;

	WRITE_ONCE(h.guard_accepting, false);
	if (h.guards_registered) {
		ret = unregister_ftrace_function(&h.guard_ops);
		if (ret) {
			WRITE_ONCE(h.guard_accepting, !h.retirement_failed);
			return ret;
		}
		h.guards_registered = false;
	}
	wait_event(h.guard_wait, !atomic_read(&h.starting_calls) &&
		   !atomic_read(&h.opening_calls) && !atomic_read(&h.shutdown_calls));
	/* A counter is released before the C epilogue. All replacements run
	 * in process context; tasks RCU covers that remaining execution.
	 */
	if (had_guards)
		synchronize_rcu_tasks();
	return 0;
}

static void notrace event_hook(unsigned long ip, unsigned long parent_ip,
			       struct ftrace_ops *ops, struct ftrace_regs *regs)
{
	unsigned long flags;

	/* Calls from deliver_one retain an ordinary return into this module. */
	if (!READ_ONCE(h.accepting) || !READ_ONCE(h.nvidia_module))
		return;
	if (!within_module(ip, h.nvidia_module) || within_module(parent_ip, THIS_MODULE) ||
	    (u32)ftrace_regs_get_argument(regs, 1) != NVPCF_CHANGE)
		return;
	spin_lock_irqsave(&h.lock, flags);
	if (h.accepting) {
		h.original = (nvpcf_callback)event_symaddr(ip);
		/* Reserve before redirecting, including the gap before stub entry. */
		atomic_inc(&h.redirected);
		ftrace_regs_set_instruction_pointer(regs, (unsigned long)capture_event);
	}
	spin_unlock_irqrestore(&h.lock, flags);
}

static void notrace capture_event(acpi_handle handle, u32 event, void *data)
{
	unsigned long flags;
	bool deferred;

	spin_lock_irqsave(&h.pci->dev.power.lock, flags);
	deferred = h.pci->dev.power.runtime_status != RPM_ACTIVE;
	spin_unlock_irqrestore(&h.pci->dev.power.lock, flags);
	spin_lock_irqsave(&h.lock, flags);
	h.handle = handle;
	h.data = data;
	nh_receive(&h.state, deferred || h.replay.paused || !h.replay.enabled);
	if (h.accepting && h.replay.enabled)
		queue_work(h.replay_wq, &h.replay_work);
	spin_unlock_irqrestore(&h.lock, flags);
	if (atomic_dec_and_test(&h.redirected))
		wake_up_all(&h.redirect_wait);
}

static void resume_probe(void *unused, struct device *dev, int flags)
{
	unsigned long irqflags;

	if (dev != &h.pci->dev)
		return;
	/* The PM core already holds dev->power.lock. */
	spin_lock_irqsave(&h.lock, irqflags);
	if (h.accepting && h.replay.enabled)
		nh_resume_begin(&h.state, dev->power.runtime_status == RPM_SUSPENDED,
				READ_ONCE(h.dispatch_task) == current);
	spin_unlock_irqrestore(&h.lock, irqflags);
}

static void return_probe(void *unused, struct device *dev, unsigned long ip, int ret)
{
	unsigned long flags;

	if (dev != &h.pci->dev)
		return;
	spin_lock_irqsave(&h.lock, flags);
	if (h.accepting && h.replay.enabled && nh_pm_end(&h.state,
			dev->power.runtime_status == RPM_ACTIVE,
			dev->power.runtime_status == RPM_SUSPENDED, ret,
			READ_ONCE(h.dispatch_task) == current))
		queue_work(h.replay_wq, &h.replay_work);
	spin_unlock_irqrestore(&h.lock, flags);
}

static int observer_entry(struct kretprobe_instance *instance, struct pt_regs *regs)
{
	/* Retain only our task's NVPCF dispatch; other callers do not need a return slot. */
	return READ_ONCE(h.dispatch_task) != current ||
	       (u32)regs_get_kernel_argument(regs, 1) != NVPCF_NOTIFY_INDEX;
}

static int observer_return(struct kretprobe_instance *instance, struct pt_regs *regs)
{
	if (READ_ONCE(h.dispatch_task) == current)
		WRITE_ONCE(h.dispatch_seen, true);
	return 0;
}

/* delivery held; the existing kernel reference/stack owns the UUID lookup. */
static int acquire_fine_reference(void)
{
	unsigned long flags;
	int ret;

	if (h.fine_held)
		return -EBUSY;
	ret = h.fine_ops.block(h.gpu_uuid.b, h.owner.stack);
	if (!ret) {
		spin_lock_irqsave(&h.lock, flags);
		h.fine_held = true;
		spin_unlock_irqrestore(&h.lock, flags);
	}
	return ret;
}

static int release_fine_reference(void)
{
	unsigned long flags;
	int ret;

	if (!h.fine_held)
		return 0;
	ret = h.fine_ops.unblock(h.gpu_uuid.b, h.owner.stack);
	spin_lock_irqsave(&h.lock, flags);
	h.fine_release_error = ret;
	if (!ret) {
		h.fine_held = false;
	} else {
		h.state.power_failures++;
		h.last_error = ret;
		h.replay.enabled = false;
		if (READ_ONCE(h.phase) == ACTIVE)
			h.phase = QUIESCED;
	}
	spin_unlock_irqrestore(&h.lock, flags);
	if (ret) {
		pr_err("nvpcf_hook: cannot release temporary NVIDIA power reference (%d); ownership retained\n", ret);
	}
	return ret;
}

static int cleanup_fine_reference_if_active(void)
{
	int ret;

	if (!h.fine_held)
		return 0;
	WRITE_ONCE(h.dispatch_task, current);
	ret = pm_runtime_get_if_active(&h.pci->dev);
	if (ret > 0) {
		ret = release_fine_reference();
		pm_runtime_put(&h.pci->dev);
	} else if (!ret) {
		ret = -EAGAIN;
	}
	WRITE_ONCE(h.dispatch_task, NULL);
	return ret;
}

/* Called in process context with delivery held, never under an NVIDIA lock. */
static int deliver_one(bool force)
{
	unsigned long flags;
	nh_u64 snapshot;
	nvpcf_callback callback;
	acpi_handle handle;
	void *data;
	bool seen, cleanup_only, acquired_fine = false;
	int ret, released;

	spin_lock_irqsave(&h.lock, flags);
	cleanup_only = !nh_pending(&h.state);
	if (cleanup_only && !h.fine_held) {
		spin_unlock_irqrestore(&h.lock, flags);
		return 0;
	}
	if (h.state.stale) {
		spin_unlock_irqrestore(&h.lock, flags);
		return -ENODEV;
	}
	snapshot = nh_snapshot(&h.state);
	callback = h.original;
	handle = h.handle;
	data = h.data;
	spin_unlock_irqrestore(&h.lock, flags);
	if (!h.owner.held || (!cleanup_only && (!callback || !data)))
		return power_failure(-ENODEV);

	WRITE_ONCE(h.dispatch_task, current);
	ret = force ? pm_runtime_resume_and_get(&h.pci->dev) :
		      pm_runtime_get_if_active(&h.pci->dev);
	if (ret < 0 || (!force && ret == 0)) {
		WRITE_ONCE(h.dispatch_task, NULL);
		return ret < 0 ? power_failure(ret) : -EAGAIN;
	}
	/* Obtain Linux power first: ordinary replay must not wake a suspended GPU.
	 * Its ACTIVE state alone does not change NVIDIA's IDLE_INDICATED state.
	 */
	spin_lock_irqsave(&h.pci->dev.power.lock, flags);
	ret = h.pci->dev.power.runtime_error;
	spin_unlock_irqrestore(&h.pci->dev.power.lock, flags);
	if (ret || !pci_device_is_present(h.pci)) {
		ret = power_failure(ret ? ret : -ENODEV);
		goto put_linux;
	}
	/* A failed earlier unblock owns a reference even with zero pending. Retry
	 * its cleanup once here before acquiring another NVIDIA reference.
	 */
	ret = release_fine_reference();
	if (ret || cleanup_only)
		goto put_linux;
	ret = acquire_fine_reference();
	if (ret) {
		ret = power_failure(ret);
		goto put_linux;
	}
	acquired_fine = true;
	/* Reference acquisition can itself run RM work. Observe only the original
	 * callback's dispatch, not an event emitted by that earlier power work.
	 */
	WRITE_ONCE(h.dispatch_seen, false);
	callback(handle, NVPCF_CHANGE, data);
	seen = READ_ONCE(h.dispatch_seen);
	ret = seen ? 0 : -EIO;
	if (!seen)
		pr_err_ratelimited("nvpcf_hook: RM dispatch completion was not observed; requests remain pending\n");
	spin_lock_irqsave(&h.lock, flags);
	nh_finish(&h.state, snapshot, seen);
	h.last_error = ret;
	spin_unlock_irqrestore(&h.lock, flags);
put_linux:
	if (acquired_fine) {
		released = release_fine_reference();
		if (released)
			ret = released;
	}
	pm_runtime_put(&h.pci->dev);
	WRITE_ONCE(h.dispatch_task, NULL);
	return ret;
}

static void replay_work(struct work_struct *work)
{
	int ret;
	unsigned long flags;

	mutex_lock(&h.delivery);
	if (!READ_ONCE(h.accepting) || !READ_ONCE(h.replay.enabled)) {
		mutex_unlock(&h.delivery);
		return;
	}
	ret = deliver_one(false);
	/* Only newly arrived requests after a successful snapshot justify rerun. */
	spin_lock_irqsave(&h.lock, flags);
	if (!ret && h.accepting && h.replay.enabled && nh_pending(&h.state))
		queue_work(h.replay_wq, &h.replay_work);
	spin_unlock_irqrestore(&h.lock, flags);
	mutex_unlock(&h.delivery);
}

static int start_capture(void)
{
	int ret;

	if (!h.owner.held || !h.observer_registered || !h.guards_registered ||
	    !READ_ONCE(h.legacy_safe))
		return -EINVAL;
	ret = register_trace_rpm_resume(resume_probe, NULL);
	if (ret)
		return ret;
	h.resume_registered = true;
	ret = register_trace_rpm_return_int(return_probe, NULL);
	if (ret)
		goto fail;
	h.return_registered = true;
	ret = register_ftrace_function(&h.event_ops);
	if (ret)
		goto fail;
	h.event_registered = true;
	return 0;
fail:
	if (h.return_registered) {
		unregister_trace_rpm_return_int(return_probe, NULL);
		h.return_registered = false;
	}
	unregister_trace_rpm_resume(resume_probe, NULL);
	h.resume_registered = false;
	tracepoint_synchronize_unregister();
	return ret;
}

/* All fallible registration is complete before capture accepts calls. */
static void activate_capture(void)
{
	unsigned long flags;
	bool active;

	spin_lock_irqsave(&h.lock, flags);
	h.state.resume_armed = false;
	h.accepting = nh_capture_ready(h.owner.held, h.observer_registered,
		h.event_registered && h.guards_registered, READ_ONCE(h.legacy_safe));
	active = nh_activate_replay(&h.replay, h.accepting,
		h.fine_verified && !h.fine_release_error && h.build_id_verified,
		&h.phase, &h.last_error);
	if (h.replay.enabled && nh_pending(&h.state))
		queue_work(h.replay_wq, &h.replay_work);
	spin_unlock_irqrestore(&h.lock, flags);
	if (active)
		pr_info("nvpcf_hook: attached to %s, NVIDIA %s\n", pci_name(h.pci), h.nvidia_version);
	else
		pr_err("nvpcf_hook: capture restored without replay readiness: state=%s error=%d\n",
			phase_names[READ_ONCE(h.phase)], READ_ONCE(h.last_error));
}

static int stop_capture(void)
{
	unsigned long flags;
	bool had_event = h.event_registered, was_accepting, was_enabled;
	int ret;

	spin_lock_irqsave(&h.lock, flags);
	was_accepting = h.accepting;
	was_enabled = h.replay.enabled;
	h.accepting = false;
	h.replay.enabled = false;
	spin_unlock_irqrestore(&h.lock, flags);
	if (h.event_registered) {
		ret = unregister_ftrace_function(&h.event_ops);
		if (ret) {
			spin_lock_irqsave(&h.lock, flags);
			h.accepting = was_accepting;
			h.replay.enabled = was_enabled;
			spin_unlock_irqrestore(&h.lock, flags);
			return ret;
		}
		h.event_registered = false;
	}
	if (h.return_registered) {
		unregister_trace_rpm_return_int(return_probe, NULL);
		h.return_registered = false;
	}
	if (h.resume_registered) {
		unregister_trace_rpm_resume(resume_probe, NULL);
		h.resume_registered = false;
	}
	tracepoint_synchronize_unregister();
	wait_event(h.redirect_wait, atomic_read(&h.redirected) == 0);
	cancel_work_sync(&h.replay_work);
	/* The reservation ends before capture_event's C epilogue. */
	if (had_event)
		synchronize_rcu_tasks();
	return 0;
}

static int put_gpu(void)
{
	if (h.fine_held)
		return power_failure(-EBUSY);
	/* action serializes this against pre-unbind. The UUID lookup must still
	 * precede NVIDIA's removal from its Linux-device list.
	 */
	h.original = NULL;
	h.data = NULL;
	h.handle = NULL;
	h.fine_verified = false;
	nh_owner_release(&h.owner, &h.owner_ops, h.gpu_uuid.b);
	return 0;
}

static void stop_observer(void)
{
	if (h.observer_registered) {
		unregister_kretprobe(&h.observer);
		h.observer_missed_total += h.observer.nmissed;
		h.observer.nmissed = 0;
		h.observer_registered = false;
	}
}

static int lifecycle_stop(void *unused)
{
	int ret = stop_capture();

	if (!ret)
		h.phase = QUIESCED;
	return ret;
}

static int lifecycle_hold(void *unused)
{
	return hold_active();
}

static int lifecycle_dispatch(void *unused)
{
	int ret;

	mutex_lock(&h.delivery);
	ret = deliver_one(true);
	mutex_unlock(&h.delivery);
	return ret;
}

static int lifecycle_restore(void *unused)
{
	int ret;

	if (!h.owner.held) {
		h.phase = WAITING;
		return 0;
	}
	if (READ_ONCE(h.target_unavailable)) {
		h.phase = QUIESCED;
		return 0;
	}
	ret = start_capture();

	if (!ret)
		activate_capture();
	return ret;
}

static void lifecycle_unhold(void *unused)
{
	release_active(false);
}

static int lifecycle_release(void *unused)
{
	return put_gpu();
}

static void lifecycle_observer_stop(void *unused)
{
	stop_observer();
}

static const struct nh_lifecycle_ops lifecycle_ops = {
	.stop_capture = lifecycle_stop,
	.hold_active = lifecycle_hold,
	.dispatch = lifecycle_dispatch,
	.restore_capture = lifecycle_restore,
	.release_active = lifecycle_unhold,
	.release_owner = lifecycle_release,
	.stop_observer = lifecycle_observer_stop,
};

/* action held. Fresh ACPICA calls use the original path after stop_capture.
 * The observer remains through the final batch, then stops before owner release.
 */
static int quiesce_flush(void)
{
	struct nh_quiesce_result result;

	if (h.legacy_blocked)
		return -EOPNOTSUPP;
	WRITE_ONCE(h.block_initialization, true);
	wait_event(h.guard_wait, !atomic_read(&h.starting_calls) &&
		   !atomic_read(&h.opening_calls));
	result = nh_quiesce(NULL, &lifecycle_ops);
	if (!result.error && READ_ONCE(h.guard_poisoned)) {
		/* Ownership was already released when hardware failed. The guard
		 * drained before freeing resources, but cannot certify delivery.
		 */
		h.phase = UNAVAILABLE;
		unpin_nvidia();
		return -EIO;
	}
	if (result.error) {
		set_error(result.error);
		WRITE_ONCE(h.block_initialization, false);
	} else {
		/* Guard-only must permit NVIDIA exit; the guard retains helper code. */
		unpin_nvidia();
	}
	if (result.restore_error)
		pr_err("nvpcf_hook: cannot restore interception: %d; retry detach\n",
		       result.restore_error);
	return result.error;
}

static ssize_t read_blob(const char *name, void *buf, size_t size)
{
	struct file *file;
	loff_t pos = 0;
	ssize_t n = 0;
	size_t total = 0;

	file = filp_open(name, O_RDONLY, 0);
	if (IS_ERR(file))
		return PTR_ERR(file);
	while (total < size) {
		n = kernel_read(file, (char *)buf + total, size - total, &pos);
		if (n <= 0)
			break;
		total += (size_t)n;
	}
	__fput_sync(file);
	return n < 0 ? n : (ssize_t)total;
}

static int read_text(const char *name, char *buf, size_t size)
{
	ssize_t n = read_blob(name, buf, size - 1);

	if (n < 0)
		return n;
	buf[n] = '\0';
	return 0;
}

/* The LIVE instance is pinned before this read, so unload/replacement cannot
 * change the module between its note check and private calls/probe registration.
 */
static int verify_nvidia_build_id(void)
{
	struct nh_build_id actual = { { 0 }, 0 };
	unsigned char *note;
	ssize_t size;
	int ret;

	h.build_id_verified = false;
	h.actual_build_id[0] = '\0';
	if (!h.nvidia_ref || !h.nvidia_module ||
	    READ_ONCE(h.nvidia_module->state) != MODULE_STATE_LIVE)
		return -ENODEV;
	note = kmalloc(4097, GFP_KERNEL);
	if (!note)
		return -ENOMEM;
	size = read_blob("/sys/module/nvidia/notes/.note.gnu.build-id", note, 4097);
	if (size < 0) {
		ret = (int)size;
	} else if (size > 4096) {
		ret = -E2BIG;
	} else {
		ret = nh_note_build_id(note, (size_t)size, &actual);
		if (!ret) {
			nh_build_id_hex(&actual, h.actual_build_id);
			ret = nh_compare_build_id(&actual, &h.expected_build_id);
		}
	}
	kfree(note);
	if (ret)
		pr_err("nvpcf_hook: NVIDIA build-ID validation failed (%d): expected=%s actual=%s\n",
			ret, expected_nvidia_build_id,
			h.actual_build_id[0] ? h.actual_build_id : "unavailable");
	else
		h.build_id_verified = true;
	return ret;
}

static int kernel_notifier_mode(void)
{
	char buf[32];
	struct file *file;
	unsigned int value;
	int ret;

	ret = read_text("/sys/module/nvidia/parameters/NVreg_UseKernelSuspendNotifiers",
			buf, sizeof(buf));
	if (ret || kstrtouint(buf, 0, &value) || value != 1)
		return ret ? ret : -EOPNOTSUPP;
	file = filp_open("/proc/driver/nvidia/suspend", O_RDONLY, 0);
	if (!IS_ERR(file)) {
		__fput_sync(file);
		return -EOPNOTSUPP;
	}
	return PTR_ERR(file) == -ENOENT ? 0 : PTR_ERR(file);
}

static int effective_fine(void)
{
	char name[160], *buf, *field;
	int ret;

	buf = kmalloc(4096, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;
	snprintf(name, sizeof(name), "/proc/driver/nvidia/gpus/%s/power", pci_name(h.pci));
	ret = read_text(name, buf, 4096);
	field = ret ? NULL : nh_field(buf, "Runtime D3 status:");
	if (!ret)
		ret = !field ? -EPROTO : !strcmp(field, "Enabled (fine-grained)") ? 0 :
		      !strcmp(field, "?") ? -EAGAIN : -EOPNOTSUPP;
	kfree(buf);
	return ret;
}

static int check_configuration(void)
{
	int ret = kernel_notifier_mode();

	if (ret) {
		pr_err("nvpcf_hook: requires UseKernelSuspendNotifiers=1 and absent procfs suspend control\n");
		return ret;
	}
	ret = effective_fine();
	if (ret)
		pr_err("nvpcf_hook: GPU must report effective fine-grained runtime PM\n");
	/* After owning an initialized device, unknown mode is a validation error,
	 * not the pre-acquisition WAITING condition.
	 */
	return ret == -EAGAIN ? -EOPNOTSUPP : ret;
}

static bool inventory_matches(void)
{
	struct pci_dev *pci = NULL;
	unsigned int count = 0;
	bool target = false;

	for_each_pci_dev(pci) {
		if (pci->vendor == PCI_VENDOR_ID_NVIDIA &&
		    (pci->class >> 16) == PCI_BASE_CLASS_DISPLAY) {
			count++;
			target |= pci == h.pci;
		}
	}
	return count == 1 && target;
}

/* Device lock and action held. */
static int get_gpu_locked(void)
{
	char name[160], *buf, *field;
	int ret;

	if (h.owner.held || pending() || READ_ONCE(h.target_unavailable))
		return -EBUSY;
	if (!inventory_matches() || !h.pci->dev.driver || !h.nvidia_ref ||
	    strcmp(h.pci->dev.driver->name, "nvidia") || !pci_device_is_present(h.pci))
		return -ENODEV;
	buf = kmalloc(4096, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;
	snprintf(name, sizeof(name), "/proc/driver/nvidia/gpus/%s/information", pci_name(h.pci));
	ret = read_text(name, buf, 4096);
	field = ret ? NULL : nh_field(buf, "GPU UUID:");
	if (!ret && (!field || strncmp(field, "GPU-", 4) ||
	    uuid_parse(field + 4, &h.gpu_uuid)))
		ret = -EINVAL;
	kfree(buf);
	if (ret)
		return ret;
	h.uuid_valid = true;
	ret = nh_owner_acquire(&h.owner, &h.owner_ops, h.gpu_uuid.b);
	return ret;
}

/* action held; these conditions also block automatic reattachment after PM. */
static bool attachment_blocked(void)
{
	return h.retirement_failed || h.guard_poisoned || h.legacy_blocked || h.state.stale;
}

/* Action and system sleep held; pre-unbind waits for action before RM teardown. */
static int attach_locked(bool activate)
{
	unsigned long event_ip;
	bool new_observer = false;
	int ret;

	if (attachment_blocked())
		return -EUCLEAN;
	ret = verify_nvidia_build_id();
	if (ret)
		return ret;
	if (!READ_ONCE(h.legacy_safe)) {
		h.legacy_blocked = true;
		pr_crit("nvpcf_hook: legacy ACPI methods changed during attachment; retaining ownership\n");
		return -EOPNOTSUPP;
	}
	mutex_lock(&h.delivery);
	ret = cleanup_fine_reference_if_active();
	mutex_unlock(&h.delivery);
	if (ret)
		return ret;
	ret = check_configuration();
	h.fine_verified = !ret;
	if (ret)
		return ret;
	if (!h.observer_registered) {
		/* unregister_kretprobe leaves the resolved address in kp.addr. */
		h.observer.kp.addr = NULL;
		ret = register_kretprobe(&h.observer);
		if (ret) {
			pr_err("nvpcf_hook: RM dispatch observer registration failed: %d\n", ret);
			return ret;
		}
		h.observer_registered = true;
		new_observer = true;
	}
	if (!h.event_registered) {
		/* :mod: needs a private trace_array in the exported regex setter.
		 * Resolve through kprobes, then filter the exact address instead.
		 */
		ret = resolve_nvidia("nvidia:nv_acpi_nvpcf_event", &event_ip);
		if (!ret)
			ret = ftrace_set_filter_ip(&h.event_ops, event_ip, 0, 1);
		if (!ret)
			ret = start_capture();
		if (ret) {
			if (new_observer)
				stop_observer();
			return ret;
		}
	}
	if (activate)
		activate_capture();
	return 0;
}

static int pin_nvidia_locked(void)
{
	struct module *owner;

	if (!nh_instance_attach_allowed(h.retirement_failed, h.guards_registered,
		h.nvidia_module != NULL))
		return -EUCLEAN;

	if (!h.pci->dev.driver || strcmp(h.pci->dev.driver->name, "nvidia"))
		return -ENODEV;
	owner = h.pci->dev.driver->owner;
	/* try_module_get also accepts COMING, whose failed init can free its
	 * code despite references. Only attach to a completely loaded driver.
	 */
	if (!owner || strcmp(module_name(owner), "nvidia") || !nh_may_pin_driver(READ_ONCE(owner->state) == MODULE_STATE_LIVE,
		!h.nvidia_module || h.nvidia_module == owner))
		return -ENODEV;
	if (!h.nvidia_ref) {
		if (!try_module_get(owner))
			return -ENODEV;
		h.nvidia_ref = true;
	}
	h.nvidia_module = owner;
	strscpy(h.nvidia_version, owner->version ? owner->version : "unknown",
		sizeof(h.nvidia_version));
	return verify_nvidia_build_id();
}

static void unpin_nvidia(void)
{
	if (h.nvidia_ref) {
		h.nvidia_ref = false;
		module_put(h.nvidia_module);
	}
}

static int hold_active(void)
{
	unsigned long flags;
	bool acquired = false;
	int ret = 0;

	if (READ_ONCE(h.replay.paused))
		return power_failure(-EHOSTDOWN);
	spin_lock_irqsave(&h.pci->dev.power.lock, flags);
	if (!h.power_hold && h.pci->dev.power.runtime_status == RPM_ACTIVE &&
	    !h.pci->dev.power.runtime_error) {
		pm_runtime_get_noresume(&h.pci->dev);
		acquired = true;
	}
	spin_unlock_irqrestore(&h.pci->dev.power.lock, flags);
	if (!h.power_hold && !acquired) {
		ret = pm_runtime_resume_and_get(&h.pci->dev);
		if (ret < 0)
			return power_failure(ret);
		acquired = true;
	}
	spin_lock_irqsave(&h.pci->dev.power.lock, flags);
	ret = h.pci->dev.power.runtime_error;
	if (h.pci->dev.power.runtime_status != RPM_ACTIVE)
		ret = -EIO;
	spin_unlock_irqrestore(&h.pci->dev.power.lock, flags);
	if (ret || !pci_device_is_present(h.pci)) {
		if (acquired)
			pm_runtime_put(&h.pci->dev);
		return power_failure(ret ? ret : -ENODEV);
	}
	WRITE_ONCE(h.power_hold, true);
	return 0;
}

static void release_active(bool driver_gone)
{
	if (h.power_hold) {
		WRITE_ONCE(h.power_hold, false);
		if (driver_gone)
			pm_runtime_put_noidle(&h.pci->dev);
		else
			pm_runtime_put(&h.pci->dev);
	}
}

/* Device lock and action held. Refuse initialization during our kernel get;
 * a successful earlier initializer is only a hint, not lifetime ownership.
 */
static int prepare_open_locked(void)
{
	int ret = pin_nvidia_locked();

	if (!ret)
		ret = kernel_notifier_mode();
	if (!ret)
		ret = start_guards();
	if (!ret)
		ret = check_legacy_methods();
	if (!ret)
		ret = effective_fine();
	if (!ret)
		ret = hold_active();
	if (!ret) {
		WRITE_ONCE(h.would_initialize, false);
		WRITE_ONCE(h.open_task, current);
		ret = get_gpu_locked();
		WRITE_ONCE(h.open_task, NULL);
		if (ret == -EBUSY && READ_ONCE(h.would_initialize))
			ret = -EAGAIN;
	}
	return ret;
}

static void resume_work(struct work_struct *work)
{
	unsigned int sleep_flags;
	unsigned long flags;
	bool already_owned;
	int ret;

	/* Waits for the whole PM sequence/robust rollback, not only our post. */
	sleep_flags = lock_system_sleep();
	device_lock(&h.pci->dev);
	mutex_lock(&h.action);
	/* Acquiring system-sleep serialization proves the entire PM sequence is
	 * over, including for guard-only state which does not reacquire a device.
	 */
	spin_lock_irqsave(&h.lock, flags);
	h.replay.paused = false;
	spin_unlock_irqrestore(&h.lock, flags);
	if (attachment_blocked()) {
		WRITE_ONCE(h.auto_resume, false);
		WRITE_ONCE(h.block_initialization, true);
		h.phase = UNAVAILABLE;
	}
	if (!READ_ONCE(h.exiting) && !attachment_blocked() &&
	    ((h.auto_resume && READ_ONCE(h.phase) == SUSPENDED) || READ_ONCE(h.phase) == WAITING)) {
		already_owned = h.owner.held;
		WRITE_ONCE(h.auto_resume, false);
		WRITE_ONCE(h.block_initialization, false);
		spin_lock_irqsave(&h.lock, flags);
		h.replay.enabled = false;
		spin_unlock_irqrestore(&h.lock, flags);
		ret = already_owned ? 0 : prepare_open_locked();
		device_unlock(&h.pci->dev);
		if (!ret)
			ret = attach_locked(true);
		if (ret) {
			/* Existing ownership also owns buffered callbacks through PM.
			 * A validation error must not destroy their context.
			 */
			if (!already_owned && !h.legacy_blocked) {
				int released = put_gpu();

				if (released)
					ret = released;
			}
			h.phase = h.owner.held ? QUIESCED :
				  ret == -EAGAIN ? WAITING : UNAVAILABLE;
			set_error(ret);
			if (ret != -EAGAIN || already_owned)
				pr_err("nvpcf_hook: attachment failed: state=%s error=%d\n",
				       phase_names[READ_ONCE(h.phase)], ret);
		}
		if (!h.legacy_blocked)
			release_active(false);
		if (!h.owner.held)
			unpin_nvidia();
	} else {
		device_unlock(&h.pci->dev);
	}
	unlock_action();
	unlock_system_sleep(sleep_flags);
}

/* The loader still owns NVIDIA text until this GOING callback returns. */
static void invalidate_nvidia_symbols(void)
{
	h.build_id_verified = false;
	h.nvidia_module = NULL;
	h.original = NULL;
	h.original_open = NULL;
	h.original_start = NULL;
	h.original_shutdown = NULL;
	h.acpi_cleanup = NULL;
	h.owner_ops = (struct nh_owner_ops){ 0 };
	h.fine_ops = (struct nh_fine_ops){ 0 };
	h.open_ip = 0;
	h.start_ip = 0;
	h.shutdown_ip = 0;
	h.data = NULL;
	h.handle = NULL;
}

/* After failed unregistration, the ops can remain registered. Disable their
 * redirects, wait entries that saw the old flags, then join reservations and
 * epilogues before forgetting NVIDIA addresses. Helper code stays pinned.
 */
static void drain_disabled_redirects(void)
{
	unsigned long flags;

	WRITE_ONCE(h.guard_accepting, false);
	spin_lock_irqsave(&h.lock, flags);
	h.accepting = false;
	h.replay.enabled = false;
	spin_unlock_irqrestore(&h.lock, flags);
	synchronize_rcu_tasks();
	wait_event(h.guard_wait, !atomic_read(&h.starting_calls) &&
		!atomic_read(&h.opening_calls) && !atomic_read(&h.shutdown_calls));
	wait_event(h.redirect_wait, !atomic_read(&h.redirected));
	cancel_work_sync(&h.replay_work);
	synchronize_rcu_tasks();
}

static int module_notify(struct notifier_block *nb, unsigned long action, void *data)
{
	bool put_self = false;
	int ret, guards_ret;
	unsigned long flags;

	if (action != MODULE_STATE_GOING || data != READ_ONCE(h.nvidia_module))
		return NOTIFY_DONE;
	/* NVIDIA exit has finished. Keep the guard until now, then drain its
	 * redirected calls before the loader frees NVIDIA text.
	 */
	mutex_lock(&h.action);
	WRITE_ONCE(h.auto_resume, false);
	WRITE_ONCE(h.block_initialization, true);
	h.retirement_failed = true;
	WRITE_ONCE(h.guard_accepting, false);
	spin_lock_irqsave(&h.lock, flags);
	h.accepting = false;
	h.replay.enabled = false;
	spin_unlock_irqrestore(&h.lock, flags);
	ret = stop_capture();
	guards_ret = stop_guards();
	if (!ret)
		ret = guards_ret;
	if (ret)
		drain_disabled_redirects();
	stop_observer();
	release_active(true);
	WARN_ON(h.owner.held || h.nvidia_ref || h.fine_held);
	invalidate_nvidia_symbols();
	h.retirement_failed = ret != 0;
	if (!nh_may_unpin_helper(!ret && !h.guards_registered && !h.event_registered,
				!h.owner.held && !h.owner.stack_ready && !h.nvidia_ref && !h.power_hold && !h.fine_held,
				pending(), h.guard_poisoned || h.retirement_failed)) {
		h.phase = UNAVAILABLE;
		if (ret)
			set_error(ret);
		pr_err("nvpcf_hook: NVIDIA exited; state=unavailable error=%d pending=%u fault=%u\n",
			READ_ONCE(h.last_error), pending(), READ_ONCE(h.guard_poisoned));
	} else {
		h.phase = DETACHED;
		if (h.self_ref) {
			h.self_ref = false;
			put_self = true;
		}
	}
	unlock_action();
	/* Module exit unregisters this notifier and joins queued work before
	 * freeing our text, even if another CPU starts removal immediately.
	 */
	if (put_self)
		module_put(THIS_MODULE);
	return NOTIFY_DONE;
}

static int pm_notify(struct notifier_block *nb, unsigned long action, void *unused)
{
	unsigned long flags;

	switch (action) {
	case PM_SUSPEND_PREPARE:
	case PM_HIBERNATION_PREPARE:
	case PM_RESTORE_PREPARE:
		mutex_lock(&h.action);
		if (h.guards_registered || h.owner.held) {
			spin_lock_irqsave(&h.lock, flags);
			nh_pause_replay(&h.replay);
			h.state.resume_armed = false;
			spin_unlock_irqrestore(&h.lock, flags);
			/* PM prepare only pauses and joins replay. The kernel reference
			 * retains context while capture counts notifications.
			 */
			cancel_work_sync(&h.replay_work);
			if (h.owner.held && !attachment_blocked()) {
				h.phase = SUSPENDED;
				WRITE_ONCE(h.auto_resume, true);
			} else if (attachment_blocked()) {
				WRITE_ONCE(h.auto_resume, false);
				h.phase = UNAVAILABLE;
			}
		}
		unlock_action();
		return NOTIFY_OK;
	case PM_POST_SUSPEND:
	case PM_POST_HIBERNATION:
	case PM_POST_RESTORE:
		mutex_lock(&h.action);
		if (!h.exiting && h.replay.paused &&
		    (h.guards_registered || h.owner.held))
			schedule_work(&h.resume_work);
		unlock_action();
		break;
	}
	return NOTIFY_DONE;
}

static int bus_notify(struct notifier_block *nb, unsigned long action, void *dev)
{
	struct device *changed = dev;
	struct pci_dev *pci = to_pci_dev(changed);
	unsigned long flags;
	bool target_remove = changed == &h.pci->dev &&
		(action == BUS_NOTIFY_UNBIND_DRIVER || action == BUS_NOTIFY_DEL_DEVICE);
	bool another_gpu = changed != &h.pci->dev && action == BUS_NOTIFY_ADD_DEVICE &&
		pci->vendor == PCI_VENDOR_ID_NVIDIA &&
		(pci->class >> 16) == PCI_BASE_CLASS_DISPLAY;
	int ret;

	if (!target_remove && !another_gpu)
		return NOTIFY_DONE;
	WRITE_ONCE(h.target_unavailable, true);
	WRITE_ONCE(h.auto_resume, false);
	/* UNBIND holds device_lock, before NVIDIA removes nvl from its list.
	 * A concurrent controller put must finish before this callback returns.
	 * Kernel put is also used by modeset_remove in this lock context; unlike
	 * VFS close it does not synchronously initiate PCI removal afterward.
	 * Neither notification can veto removal. Never join resume_work here.
	 */
	mutex_lock(&h.action);
	if (h.legacy_blocked) {
		WRITE_ONCE(h.block_initialization, true);
		h.phase = UNAVAILABLE;
		set_error(-EOPNOTSUPP);
		pr_crit("nvpcf_hook: unsupported legacy ACPI state retains ownership; PCI removal may wait\n");
		unlock_action();
		return NOTIFY_DONE;
	}
	if (h.owner.held) {
		ret = quiesce_flush();
		if (ret) {
			/* Stop any restoration from failed voluntary-style flushing. */
			ret = stop_capture();
			if (ret) {
				/* Unbind ignores notifier errors. Retain ownership if the
				 * kernel cannot remove our hook, even though removal waits.
				 */
				pr_crit("nvpcf_hook: cannot stop interception (%d); PCI removal may wait for detach\n", ret);
				set_error(ret);
				unlock_action();
				return NOTIFY_DONE;
			}
			spin_lock_irqsave(&h.lock, flags);
			nh_mark_stale(&h.state);
			spin_unlock_irqrestore(&h.lock, flags);
			if (h.fine_held) {
				h.retirement_failed = true;
				set_error(h.fine_release_error ? h.fine_release_error : -EBUSY);
				pr_crit("nvpcf_hook: outstanding NVIDIA power reference retains ownership during removal\n");
				unlock_action();
				return NOTIFY_DONE;
			}
			stop_observer();
			ret = put_gpu();
			if (ret) {
				h.retirement_failed = true;
				unlock_action();
				return NOTIFY_DONE;
			}
			pr_err("nvpcf_hook: ownership ended with undelivered requests; inspect status\n");
		}
	}
	WRITE_ONCE(h.block_initialization, true);
	unpin_nvidia();
	h.phase = UNAVAILABLE;
	set_error(another_gpu ? -EOPNOTSUPP : -ENODEV);
	pr_info("nvpcf_hook: state=unavailable: %s (error=%d)\n",
		another_gpu ? "another NVIDIA GPU appeared" : "selected GPU is being removed/unbound",
		another_gpu ? -EOPNOTSUPP : -ENODEV);
	unlock_action();
	return NOTIFY_DONE;
}

static int status_show(struct seq_file *seq, void *unused)
{
	struct nh_state s;
	unsigned long flags;
	int error, fine_error;
	bool fine_held;
	enum nh_phase phase;

	mutex_lock(&h.action);
	spin_lock_irqsave(&h.lock, flags);
	s = h.state;
	error = h.last_error;
	phase = h.phase;
	fine_held = h.fine_held;
	fine_error = h.fine_release_error;
	spin_unlock_irqrestore(&h.lock, flags);
	seq_printf(seq, "state: %s\npci: %s\nnvidia_version: %s\ndevice_reference: %u\n",
		phase_names[phase], pci_name(h.pci), h.nvidia_version, !!h.owner.held);
	if (h.uuid_valid)
		seq_printf(seq, "gpu_uuid: GPU-%pUb\n", &h.gpu_uuid);
	seq_printf(seq, "stack_ready: %u\nreplay_paused: %u\n", h.owner.stack_ready,
		READ_ONCE(h.replay.paused));
	seq_printf(seq, "nvidia_power_reference: %u\nnvidia_power_release_error: %d\n", fine_held, fine_error);
	seq_printf(seq, "expected_nvidia_build_id: %s\nactual_nvidia_build_id: %s\n",
		expected_nvidia_build_id, h.actual_build_id[0] ? h.actual_build_id : "unavailable");
	seq_printf(seq, "build_id_verified: %u\n", h.build_id_verified);
	seq_printf(seq, "retirement_failed: %u\n", h.retirement_failed);
	seq_printf(seq, "guards_registered: %u\ninitialization_blocked: %u\nnvidia_reference: %u\npower_reference: %u\n",
		h.guards_registered, READ_ONCE(h.block_initialization), h.nvidia_ref,
		READ_ONCE(h.power_hold));
	seq_printf(seq, "starting_calls: %d\nopening_calls: %d\nshutdown_calls: %d\nprelock_cleanup_calls: %llu\nguard_failures: %llu\nlegacy_namespace_blocked: %u\n",
		atomic_read(&h.starting_calls), atomic_read(&h.opening_calls),
		atomic_read(&h.shutdown_calls),
		(unsigned long long)READ_ONCE(h.prelock_cleanup_calls),
		(unsigned long long)READ_ONCE(h.guard_failures), h.legacy_blocked);
	seq_printf(seq, "pending_requests: %llu\nreceived_requests: %llu\ndeferred_requests: %llu\n",
		(unsigned long long)nh_pending(&s), (unsigned long long)s.received,
		(unsigned long long)s.deferred);
	seq_printf(seq, "requests_dispatched: %llu\ndispatch_batches: %llu\ncoalesced_requests: %llu\n",
		(unsigned long long)s.dispatched, (unsigned long long)s.batches,
		(unsigned long long)(s.dispatched - s.batches));
	seq_printf(seq, "dispatch_attempts: %llu\ndispatch_failures: %llu\nacknowledged_loss: %llu\n",
		(unsigned long long)s.attempts, (unsigned long long)s.failures,
		(unsigned long long)s.acknowledged_loss);
	seq_printf(seq, "power_failures: %llu\ndelivery_failures: %llu\n",
		(unsigned long long)s.power_failures,
		(unsigned long long)(s.power_failures + s.failures));
	seq_printf(seq, "stale_pending: %u\nlast_error: %d\nobserver_missed: %llu\nredirected: %d\n",
		s.stale, error,
		(unsigned long long)(h.observer_missed_total + READ_ONCE(h.observer.nmissed)),
		atomic_read(&h.redirected));
	unlock_action();
	return 0;
}

static int control_open(struct inode *inode, struct file *file)
{
	return single_open(file, status_show, NULL);
}

static int preflight(void)
{
	bool had_hold;
	int ret;

	if (h.legacy_blocked || h.guard_poisoned || h.state.stale || h.retirement_failed)
		return -EUCLEAN;
	if (READ_ONCE(h.phase) == GUARD_ONLY || READ_ONCE(h.phase) == WAITING)
		return 0;
	if (!h.owner.held)
		return h.last_error ? h.last_error : -ENODEV;
	mutex_lock(&h.delivery);
	had_hold = h.power_hold;
	WRITE_ONCE(h.dispatch_task, current);
	ret = hold_active();
	if (!ret)
		ret = deliver_one(true);
	if (!had_hold)
		release_active(false);
	WRITE_ONCE(h.dispatch_task, NULL);
	mutex_unlock(&h.delivery);
	return ret;
}

/* action/system sleep held. This path retires old registrations, never
 * attaches or calls NVIDIA addresses after its instance has exited.
 */
static int retry_retirement(void)
{
	int ret, guards_ret;

	WRITE_ONCE(h.auto_resume, false);
	if (h.nvidia_module) {
		if (!h.nvidia_ref) {
			if (READ_ONCE(h.nvidia_module->state) != MODULE_STATE_LIVE ||
			    !try_module_get(h.nvidia_module))
				return -ENODEV;
			h.nvidia_ref = true;
			ret = verify_nvidia_build_id();
			if (ret)
				return ret;
		}
		if (h.owner.held) {
			ret = hold_active();
			if (ret)
				return ret;
		}
		ret = initial_rollback(NULL);
	} else {
		ret = stop_capture();
		guards_ret = stop_guards();
		if (!ret)
			ret = guards_ret;
		if (ret)
			drain_disabled_redirects();
		stop_observer();
		if (!ret && (h.owner.held || h.owner.stack_ready || h.nvidia_ref || h.fine_held))
			ret = -EUCLEAN;
	}
	if (ret) {
		set_error(ret);
		return ret;
	}
	h.retirement_failed = false;
	h.phase = h.guard_poisoned || h.state.stale ? UNAVAILABLE : DETACHED;
	if (READ_ONCE(h.phase) == UNAVAILABLE)
		return -EUCLEAN;
	set_error(0);
	return 0;
}

static ssize_t control_write(struct file *file, const char __user *buf,
			     size_t count, loff_t *pos)
{
	char command[32];
	unsigned int sleep_flags;
	unsigned long flags;
	bool put_self = false, restore = false, cancel_resume = false;
	int ret = 0;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (!count || count >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buf, count))
		return -EFAULT;
	if (memchr(command, '\0', count))
		return -EINVAL;
	command[count] = '\0';
	strim(command);
	if (strcmp(command, "detach") && strcmp(command, "prepare") &&
	    strcmp(command, "resume") && strcmp(command, "acknowledge-loss"))
		return -EINVAL;

	sleep_flags = lock_system_sleep();
	mutex_lock(&h.action);
	/* The post worker may still be queued. This barrier also proves that
	 * system PM has finished before a manual flush or final reference put.
	 */
	spin_lock_irqsave(&h.lock, flags);
	h.replay.paused = false;
	spin_unlock_irqrestore(&h.lock, flags);
	if (!strcmp(command, "prepare")) {
		ret = preflight();
	} else if (!strcmp(command, "resume")) {
		if (h.state.stale || h.guard_poisoned || h.legacy_blocked || h.retirement_failed) {
			ret = -EUCLEAN;
		} else if (!nh_monitoring_active(READ_ONCE(h.phase), &h.replay,
			READ_ONCE(h.accepting), h.fine_verified && !READ_ONCE(h.fine_release_error) && h.build_id_verified)) {
			if (!h.self_ref) {
				__module_get(THIS_MODULE);
				h.self_ref = true;
			}
			WRITE_ONCE(h.target_unavailable, false);
			WRITE_ONCE(h.block_initialization, false);
			WRITE_ONCE(h.auto_resume, false);
			spin_lock_irqsave(&h.lock, flags);
			h.phase = WAITING;
			spin_unlock_irqrestore(&h.lock, flags);
			restore = true;
			schedule_work(&h.resume_work);
		}
	} else if (!strcmp(command, "acknowledge-loss")) {
		spin_lock_irqsave(&h.lock, flags);
		if (h.owner.held || h.nvidia_module || h.guards_registered ||
		    READ_ONCE(h.phase) != UNAVAILABLE || h.retirement_failed || (!h.state.stale && !h.guard_poisoned)) {
			ret = -EINVAL;
		} else {
			if (h.state.stale)
				nh_acknowledge_loss(&h.state);
			h.guard_poisoned = false;
			h.phase = DETACHED;
		}
		spin_unlock_irqrestore(&h.lock, flags);
		cancel_resume = !ret;
	} else if (h.retirement_failed) {
		ret = retry_retirement();
		cancel_resume = !h.retirement_failed;
	} else if (h.state.stale || h.guard_poisoned) {
		ret = -EUCLEAN;
	} else if (READ_ONCE(h.phase) != GUARD_ONLY) {
		WRITE_ONCE(h.auto_resume, false);
		if (!h.nvidia_module) {
			ret = stop_capture();
			if (!ret)
				ret = stop_guards();
			if (!ret)
				h.phase = DETACHED;
		} else {
			/* The module object cannot be freed until its GOING callback
			 * acquires action. A temporary ref excludes exit during prep.
			 */
			if (!h.nvidia_ref) {
				if (READ_ONCE(h.nvidia_module->state) == MODULE_STATE_LIVE &&
				    try_module_get(h.nvidia_module)) {
					h.nvidia_ref = true;
					ret = verify_nvidia_build_id();
				} else
					ret = -ENODEV;
			}
			if (!ret)
				ret = quiesce_flush();
			if (!ret)
				h.phase = h.guards_registered ? GUARD_ONLY : DETACHED;
			if (!h.owner.held)
				unpin_nvidia();
		}
		cancel_resume = !ret;
	}
	if (!ret && READ_ONCE(h.phase) == DETACHED && h.self_ref) {
		h.self_ref = false;
		put_self = true;
	}
	unlock_action();
	unlock_system_sleep(sleep_flags);
	/* Never join a worker waiting for system_transition_mutex while owning it. */
	if (cancel_resume)
		cancel_work_sync(&h.resume_work);
	if (restore) {
		flush_work(&h.resume_work);
		mutex_lock(&h.action);
		if (READ_ONCE(h.phase) != ACTIVE && READ_ONCE(h.phase) != WAITING)
			ret = h.last_error ? h.last_error : -ENODEV;
		unlock_action();
	}
	if (put_self)
		module_put(THIS_MODULE); /* control file still owns our code */
	return ret ? ret : count;
}

static const struct file_operations control_fops = {
	.owner = THIS_MODULE,
	.open = control_open,
	.read = seq_read,
	.write = control_write,
	.llseek = seq_lseek,
	.release = single_release,
};
static struct miscdevice control_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "nvpcf-hook",
	.fops = &control_fops,
	.mode = 0600,
};

static int find_gpu(void)
{
	struct pci_dev *pci = NULL, *found = NULL;
	unsigned int count = 0;

	for_each_pci_dev(pci) {
		if (pci->vendor == PCI_VENDOR_ID_NVIDIA &&
		    (pci->class >> 16) == PCI_BASE_CLASS_DISPLAY) {
			count++;
			if (!found)
				found = pci_dev_get(pci);
		}
	}
	if (count != 1 || pci_is_thunderbolt_attached(found) ||
	    found->dev.removable != DEVICE_FIXED) {
		pci_dev_put(found);
		pr_err("nvpcf_hook: requires exactly one fixed internal NVIDIA PCI GPU\n");
		return -EOPNOTSUPP;
	}
	h.pci = found;
	return 0;
}

static int initial_stage(void *unused)
{
	int ret = prepare_open_locked();

	/* Init acquired device_lock before action. Drop it before any put. */
	device_unlock(&h.pci->dev);
	if (!ret)
		ret = attach_locked(false);
	if (ret == -EAGAIN) {
		h.phase = WAITING;
		set_error(ret);
		pr_info("nvpcf_hook: waiting for a successful ordinary GPU initializer\n");
		return 0;
	}
	return ret;
}

static int initial_publish(void *unused)
{
	return misc_register(&control_device);
}

static void initial_activate(void *unused)
{
	if (h.owner.held)
		activate_capture();
}

static int initial_rollback(void *unused)
{
	int ret;

	/* Acceptance is still disabled, so rollback has not captured requests. */
	if (h.legacy_blocked)
		return -EOPNOTSUPP;
	ret = stop_capture();
	if (ret)
		return ret;
	ret = release_fine_reference();
	if (ret)
		return ret;
	stop_observer();
	ret = put_gpu();
	if (ret)
		return ret;
	release_active(false);
	ret = stop_guards();
	if (!ret)
		unpin_nvidia();
	return ret;
}

static const struct nh_publication_ops publication_ops = {
	.stage = initial_stage,
	.publish = initial_publish,
	.activate = initial_activate,
	.rollback = initial_rollback,
};

static int __init hook_init(void)
{
	struct nh_publication_result publication;
	unsigned int sleep_flags;
	int ret;

	ret = nh_parse_build_id(expected_nvidia_build_id, &h.expected_build_id);
	if (ret) {
		pr_err("nvpcf_hook: expected_nvidia_build_id requires 2..128 unseparated hex characters (%d)\n", ret);
		return ret;
	}
	mutex_init(&h.action);
	mutex_init(&h.delivery);
	spin_lock_init(&h.lock);
	init_waitqueue_head(&h.redirect_wait);
	init_waitqueue_head(&h.guard_wait);
	atomic_set(&h.starting_calls, 0);
	atomic_set(&h.opening_calls, 0);
	atomic_set(&h.shutdown_calls, 0);
	atomic_set(&h.redirected, 0);
	INIT_WORK(&h.replay_work, replay_work);
	INIT_WORK(&h.resume_work, resume_work);
	h.phase = STARTING;
	h.event_ops.func = event_hook;
	h.event_ops.flags = FTRACE_OPS_FL_SAVE_REGS | FTRACE_OPS_FL_IPMODIFY |
		FTRACE_OPS_FL_RECURSION | FTRACE_OPS_FL_PERMANENT;
	h.guard_ops.func = guard_hook;
	h.guard_ops.flags = h.event_ops.flags;
	h.module_nb.notifier_call = module_notify;
	h.observer.kp.symbol_name = "nvidia:gpuNotifySubDeviceEvent_IMPL";
	h.observer.entry_handler = observer_entry;
	h.observer.handler = observer_return;
	h.observer.maxactive = 64;
	h.pm_nb.notifier_call = pm_notify;
	h.pm_nb.priority = INT_MAX;
	h.bus_nb.notifier_call = bus_notify;
	h.replay_wq = alloc_ordered_workqueue("nvpcf-replay", WQ_MEM_RECLAIM);
	if (!h.replay_wq) {
		ret = -ENOMEM;
		goto queues;
	}
	ret = find_gpu();
	if (ret)
		goto queues;
	sleep_flags = lock_system_sleep();
	ret = register_pm_notifier(&h.pm_nb);
	if (ret)
		goto sleep;
	ret = bus_register_notifier(&pci_bus_type, &h.bus_nb);
	if (ret)
		goto pm;
	ret = register_module_notifier(&h.module_nb);
	if (ret)
		goto bus;
	device_lock(&h.pci->dev);
	mutex_lock(&h.action);
	__module_get(THIS_MODULE);
	h.self_ref = true;
	publication = nh_publish(NULL, &publication_ops);
	ret = publication.error;
	if (ret && publication.rollback_error) {
		h.retirement_failed = true;
		WRITE_ONCE(h.block_initialization, true);
		/* Failed cleanup cannot permit freeing registered replacement code.
		 * Publish diagnostics if publication was not already attempted.
		 */
		h.phase = UNAVAILABLE;
		set_error(publication.rollback_error);
		if (!publication.publication_attempted) {
			int published = initial_publish(NULL);

			if (published)
				pr_crit("nvpcf_hook: diagnostic control publication failed: %d\n", published);
		}
		pr_crit("nvpcf_hook: init cleanup failed (%d after %d); retaining helper and ownership state\n",
			publication.rollback_error, ret);
		ret = 0;
	} else if (ret) {
		h.self_ref = false;
		module_put(THIS_MODULE);
		WRITE_ONCE(h.exiting, true);
		set_error(ret);
		pr_err("nvpcf_hook: initial setup/publication failed: %d\n", ret);
	}
	if (!h.legacy_blocked)
		release_active(false);
	if (!h.owner.held && !publication.rollback_error)
		unpin_nvidia();
	unlock_action();
	if (!ret) {
		unlock_system_sleep(sleep_flags);
		return 0;
	}
	unregister_module_notifier(&h.module_nb);
bus:
	bus_unregister_notifier(&pci_bus_type, &h.bus_nb);
pm:
	unregister_pm_notifier(&h.pm_nb);
sleep:
	unlock_system_sleep(sleep_flags);
	cancel_work_sync(&h.resume_work);
	ftrace_free_filter(&h.event_ops);
	ftrace_free_filter(&h.guard_ops);
	pci_dev_put(h.pci);
queues:
	if (h.replay_wq)
		destroy_workqueue(h.replay_wq);
	return ret;
}

static void __exit hook_exit(void)
{
	WRITE_ONCE(h.exiting, true);
	misc_deregister(&control_device);
	unregister_pm_notifier(&h.pm_nb);
	bus_unregister_notifier(&pci_bus_type, &h.bus_nb);
	unregister_module_notifier(&h.module_nb);
	cancel_work_sync(&h.resume_work);
	/* A retained self-reference excludes module exit before preparation. */
	WARN_ON(h.owner.held || h.owner.stack_ready || h.fine_held || h.self_ref || pending() || h.event_registered ||
		h.observer_registered || atomic_read(&h.redirected) ||
		h.guards_registered || h.nvidia_ref || h.power_hold ||
		atomic_read(&h.starting_calls) || atomic_read(&h.opening_calls) ||
		atomic_read(&h.shutdown_calls));
	ftrace_free_filter(&h.event_ops);
	ftrace_free_filter(&h.guard_ops);
	destroy_workqueue(h.replay_wq);
	pci_dev_put(h.pci);
}

module_init(hook_init);
module_exit(hook_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION("0.1");
MODULE_DESCRIPTION("Defer NVPCF changes with a kernel-client reference and explicit detach");
MODULE_SOFTDEP("pre: nvidia");
