/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>
#include <sched.h>
#include "../state.h"
#include "../text.h"
#include "../lifecycle.h"

static void conserved(const struct nh_state *s)
{
	assert(s->received == s->dispatched + s->acknowledged_loss + nh_pending(s));
	assert(s->completed == s->dispatched + s->acknowledged_loss);
}

static void test_batches(void)
{
	struct nh_state s = { 0 };
	nh_u64 snapshot;

	nh_receive(&s, true);
	nh_receive(&s, true);
	snapshot = nh_snapshot(&s);
	/* A notification arriving while the snapshot is being dispatched survives. */
	nh_receive(&s, false);
	nh_finish(&s, snapshot, true);
	assert(s.dispatched == 2 && s.batches == 1 && nh_pending(&s) == 1);
	nh_finish(&s, nh_snapshot(&s), true);
	assert(s.dispatched == 3 && s.batches == 2 && !nh_pending(&s));
	assert(s.deferred == 2);
	conserved(&s);
}

static void test_absent_dispatch_and_feedback(void)
{
	struct nh_state s = { 0 };
	unsigned int i;

	nh_receive(&s, true);
	nh_resume_begin(&s, true, false);
	/* The core's first ACTIVE return after real resume permits one retry. */
	assert(nh_pm_end(&s, true, false, 0, false));
	/* Covers API-lock TLS allocation failure and patched-core internal defer. */
	nh_finish(&s, nh_snapshot(&s), false);
	assert(nh_pending(&s) == 1 && s.failures == 1 && s.dispatched == 0);
	/* Reference release emits idle/put events in this task and later PM work.
	 * Neither is a new resume from SUSPENDED; failure must not self-retry.
	 */
	for (i = 0; i < 1000; ++i) {
		assert(!nh_pm_end(&s, true, false, 0, true));
		assert(!nh_pm_end(&s, true, false, 0, false));
		assert(!nh_pm_end(&s, false, true, 0, false));
		nh_resume_begin(&s, false, false); /* already ACTIVE get */
		assert(!nh_pm_end(&s, true, false, 1, false));
	}
	assert(s.attempts == 1);
	/* A later unrelated real resume permits another attempt. */
	nh_resume_begin(&s, true, false);
	assert(nh_pm_end(&s, true, false, 0, false));
	nh_finish(&s, nh_snapshot(&s), true);
	assert(!nh_pending(&s) && s.dispatched == 1 && s.attempts == 2);
	conserved(&s);
}

static void test_missed_observer(void)
{
	struct nh_state s = { 0 };
	unsigned int actual_dispatches = 0;

	nh_receive(&s, false);
	actual_dispatches++;
	nh_finish(&s, nh_snapshot(&s), false); /* real dispatch, missed return */
	assert(nh_pending(&s) == 1 && s.dispatched == 0);
	assert(!nh_pm_end(&s, true, false, 0, false));
	nh_resume_begin(&s, true, false);
	assert(nh_pm_end(&s, true, false, 0, false));
	actual_dispatches++;
	nh_finish(&s, nh_snapshot(&s), true);
	assert(actual_dispatches == 2 && s.dispatched == 1 && s.failures == 1);
	/* Retrying may duplicate RM dispatch; accounting reports observations. */
	conserved(&s);
}

static void test_transition_observations(void)
{
	struct nh_state s = { 0 };

	nh_receive(&s, true);
	nh_resume_begin(&s, true, false);
	assert(!nh_pm_end(&s, false, true, 0, false)); /* async queueing */
	assert(!nh_pm_end(&s, false, false, -1, false)); /* RESUMING/NOWAIT */
	assert(nh_pm_end(&s, true, false, 0, false));
	assert(!nh_pm_end(&s, true, false, 0, false)); /* final resume event */
	nh_resume_begin(&s, true, false);
	assert(!nh_pm_end(&s, false, true, -1, false)); /* actual failed resume */
	assert(!nh_pm_end(&s, true, false, 0, false));
	nh_resume_begin(&s, true, true); /* our deliberate detach wake */
	assert(!nh_pm_end(&s, true, false, 0, true));
	assert(!nh_pm_end(&s, true, false, 0, false));
	assert(nh_pending(&s) == 1);
}

struct fixture {
	struct nh_state s;
	bool owner, observer, capture, rm_locks;
	bool capture_fail, power_fail, dispatch_fail, restore_fail, in_flight_arrival;
	bool held_power, power_unavailable, resources_alive, psr_object, nvpcf_object, pci_remove;
	bool linux_ldata, linux_devices, keep_instance;
	unsigned int queued_c0, queued_psr, context_frees, cleanup_calls, ordinary_failures;
	unsigned int active_gets, completed_release, original_callbacks;
	atomic_uint bus_callbacks;
	bool concurrent_unbind, lookup_present;
	pthread_mutex_t *action;
	atomic_bool bus_waiting;
	char log[128];
};

static void record(struct fixture *f, const char *text)
{
	assert(!f->rm_locks);
	assert(strlen(f->log) + strlen(text) < sizeof(f->log));
	strcat(f->log, text);
}

static int stop(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "stop ");
	assert(f->owner && f->observer);
	if (f->capture_fail)
		return -1;
	f->capture = false;
	/* A stub reserved before unregister can still record before drain ends. */
	if (f->in_flight_arrival)
		nh_receive(&f->s, true);
	record(f, "wait drain ");
	/* Fresh ACPICA notifications after unregister use the original path. */
	f->original_callbacks++;
	return 0;
}

static int hold(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "hold ");
	assert(f->owner && !f->capture);
	f->active_gets++;
	if (f->power_fail)
		return -4;
	f->held_power = true;
	return 0;
}

static int dispatch(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "dispatch ");
	assert(f->owner && f->observer && !f->capture && f->held_power);
	if (nh_pending(&f->s)) {
		nh_finish(&f->s, nh_snapshot(&f->s), !f->dispatch_fail);
	}
	return f->dispatch_fail ? -2 : 0;
}

static int restore(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "restore ");
	assert(f->owner && f->observer);
	if (f->restore_fail)
		return -3;
	f->capture = true;
	return 0;
}

static void ordinary_callbacks(struct fixture *f)
{
	/* Modeled PSR/NVPCF paths acquire API/GPU locks, not the Linux-device
	 * semaphores. They cannot enqueue helper work after capture stopped.
	 */
	assert(!f->rm_locks && f->resources_alive && !f->capture && !f->observer);
	while (f->queued_psr) {
		assert(f->psr_object);
		f->queued_psr--;
		if (f->power_unavailable)
			f->ordinary_failures++;
		else
			f->original_callbacks++;
	}
	while (f->queued_c0) {
		assert(f->nvpcf_object);
		f->queued_c0--;
		if (f->power_unavailable)
			f->ordinary_failures++;
		else
			f->original_callbacks++;
	}
}

static void unhold(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "unhold ");
	f->held_power = false;
}

static void owned_cleanup(void *ctx)
{
	struct fixture *f = ctx;

	f->cleanup_calls++;
	if (!f->psr_object && !f->nvpcf_object)
		return; /* Later NVIDIA-owned uninit is empty, including under locks. */
	assert(!f->rm_locks && f->resources_alive && (f->held_power || f->power_unavailable));
	/* Both Linux semaphores may be held at committed shutdown. The
	 * modeled ordinary handlers need API/GPU locks, not these semaphores.
	 */
	assert(f->linux_ldata);
	/* Late arrivals after helper interception stopped, before API teardown. */
	f->queued_psr++;
	f->queued_c0++;
	ordinary_callbacks(f); /* NVIDIA removes PSR, then drains while it is valid. */
	f->psr_object = false;
	f->context_frees++;
	/* NVPCF is still registered until the second removal. */
	f->queued_c0++;
	ordinary_callbacks(f);
	f->nvpcf_object = false;
	f->context_frees++;
}

static void original_destroy(void *ctx)
{
	struct fixture *f = ctx;

	assert(!f->psr_object && !f->nvpcf_object && !f->queued_c0 && !f->queued_psr);

	f->rm_locks = true;
	owned_cleanup(f); /* The later core call skips already-cleared objects. */
	f->resources_alive = false;
	f->rm_locks = false;
	f->linux_devices = false;
	f->linux_ldata = false;
}

static const struct nh_teardown_ops teardown_ops = {
	.cleanup_handlers = owned_cleanup,
	.destroy_resources = original_destroy,
};

static void *release_bus_callback(void *ctx)
{
	struct fixture *f = ctx;

	atomic_store_explicit(&f->bus_waiting, true, memory_order_release);
	assert(!pthread_mutex_lock(f->action));
	/* Pre-unbind must wait for the UUID put before NVIDIA removes the device
	 * from its lookup list. It cannot skip the lock during another task's put.
	 */
	assert(!f->owner && f->completed_release == 1 && f->lookup_present);
	f->lookup_present = false;
	atomic_fetch_add_explicit(&f->bus_callbacks, 1, memory_order_relaxed);
	assert(!pthread_mutex_unlock(f->action));
	return NULL;
}

static void release_owner(void *ctx)
{
	struct fixture *f = ctx;
	pthread_t thread;
	pthread_mutex_t action = PTHREAD_MUTEX_INITIALIZER;

	record(f, "release ");
	assert((!nh_pending(&f->s) || f->s.stale) && f->owner && !f->observer &&
	       (f->held_power || f->power_unavailable));
	f->action = &action;
	assert(!pthread_mutex_lock(f->action));
	if (f->concurrent_unbind) {
		assert(!pthread_create(&thread, NULL, release_bus_callback, f));
		while (!atomic_load_explicit(&f->bus_waiting, memory_order_acquire))
			sched_yield();
	}
	assert(f->lookup_present); /* UUID lookup is still valid during put. */
	if (!f->keep_instance) {
		f->linux_ldata = true;
		f->linux_devices = f->pci_remove;
		/* Committed shutdown does not acquire the helper action mutex. */
		nh_teardown(f, &teardown_ops);
	}
	f->owner = false;
	f->completed_release++;
	assert(!pthread_mutex_unlock(f->action));
	if (f->concurrent_unbind)
		assert(!pthread_join(thread, NULL));
	assert(!pthread_mutex_destroy(f->action));
	f->action = NULL;
}

static void observer_stop(void *ctx)
{
	struct fixture *f = ctx;

	record(f, "observer-stop ");
	assert(f->owner && f->observer && !f->capture);
	f->observer = false;
}

static const struct nh_lifecycle_ops ops = {
	.stop_capture = stop,
	.hold_active = hold,
	.dispatch = dispatch,
	.restore_capture = restore,
	.release_active = unhold,
	.release_owner = release_owner,
	.stop_observer = observer_stop,
};

static struct fixture fixture(void)
{
	struct fixture f = { .owner = true, .observer = true, .capture = true,
		.resources_alive = true, .psr_object = true, .nvpcf_object = true,
		.lookup_present = true };

	nh_receive(&f.s, true);
	return f;
}

static void test_detach(void)
{
	struct fixture f = fixture();
	struct nh_quiesce_result r;

	f.in_flight_arrival = true;
	f.concurrent_unbind = true;
	r = nh_quiesce(&f, &ops);
	assert(!r.error && !r.restore_error);
	assert(!strcmp(f.log, "stop wait drain hold dispatch observer-stop release unhold "));
	assert(f.s.dispatched == 2 && !nh_pending(&f.s));
	assert(f.completed_release == 1 && f.active_gets == 1);
	assert(f.original_callbacks == 4 && atomic_load(&f.bus_callbacks) == 1);
	assert(f.context_frees == 2 && f.cleanup_calls == 2 && !f.resources_alive);
	assert(f.s.received == 2); /* Late ordinary callbacks did not enqueue private work. */
	conserved(&f.s);

	f = fixture();
	f.capture_fail = true;
	r = nh_quiesce(&f, &ops);
	assert(r.error == -1 && !r.restore_error);
	assert(f.owner && f.capture && f.observer && nh_pending(&f.s) == 1);

	f = fixture();
	f.dispatch_fail = true;
	r = nh_quiesce(&f, &ops);
	assert(r.error == -2 && !r.restore_error);
	assert(!strcmp(f.log, "stop wait drain hold dispatch restore unhold "));
	assert(f.owner && f.capture && f.observer && nh_pending(&f.s) == 1);
	f.dispatch_fail = false;
	f.log[0] = '\0';
	r = nh_quiesce(&f, &ops);
	assert(!r.error && !f.owner && !nh_pending(&f.s));

	f = fixture();
	f.dispatch_fail = true;
	f.restore_fail = true;
	r = nh_quiesce(&f, &ops);
	assert(r.error == -2 && r.restore_error == -3);
	assert(f.owner && f.observer && !f.capture && nh_pending(&f.s) == 1);
	/* Even failed restoration leaves an owned, retryable pending batch. */
	f.dispatch_fail = false;
	f.restore_fail = false;
	r = nh_quiesce(&f, &ops);
	assert(!r.error && !nh_pending(&f.s));
}

static void test_power_and_guard_lifetime(void)
{
	struct fixture f = fixture();
	struct nh_quiesce_result r;

	f.power_fail = true;
	r = nh_quiesce(&f, &ops);
	assert(r.error == -4 && !r.restore_error);
	assert(!strcmp(f.log, "stop wait drain hold restore unhold "));
	assert(f.owner && f.capture && f.observer && !f.held_power);
	assert(f.completed_release == 0 && f.s.attempts == 0 && nh_pending(&f.s) == 1);

	/* Even a zero-pending final release needs active ownership. */
	f = fixture();
	nh_finish(&f.s, nh_snapshot(&f.s), true);
	f.power_fail = true;
	r = nh_quiesce(&f, &ops);
	assert(r.error == -4 && f.completed_release == 0 && f.owner);

	/* Another user or init-on-probe can keep the instance after our kernel
	 * reference is released. Guard code must remain for its later teardown.
	 */
	f = fixture();
	f.keep_instance = true;
	r = nh_quiesce(&f, &ops);
	assert(!r.error && !f.owner && !f.capture && !f.observer && !f.held_power);
	assert(f.resources_alive && f.psr_object && f.nvpcf_object);
	assert(!nh_may_unpin_helper(0, 0, 0, 0));
	/* Drop our NVIDIA text reference; the hook does not import NVIDIA symbols.
	 * Other users then leave; the guard still drains ordinary late events.
	 */
	f.linux_ldata = true;
	f.held_power = true; /* Later guard takes its own short active reference. */
	nh_teardown(&f, &teardown_ops);
	f.held_power = false;
	f.linux_ldata = false;
	assert(f.context_frees == 2 && !f.resources_alive);
	assert(!nh_may_unpin_helper(0, 0, 0, 0));
	/* GOING follows exit. Unregister and join replacements before releasing
	 * the helper reference. Linux power was balanced after the device put.
	 */
	assert(!nh_may_unpin_helper(0, 1, 0, 0)); /* guard not yet drained */
	f.held_power = false;
	assert(nh_may_unpin_helper(1, 1, 0, 0));
	assert(!nh_may_unpin_helper(1, 1, 1, 0));
	assert(!nh_may_unpin_helper(1, 1, 0, 1));

	/* PCI shutdown may hold both NVIDIA Linux semaphores. Cleanup precedes
	 * API/GPU locks, and the later uninit skips already-freed global objects.
	 */
	f = fixture();
	f.pci_remove = true;
	r = nh_quiesce(&f, &ops);
	assert(!r.error && f.context_frees == 2 && f.original_callbacks == 4);
	assert(!f.psr_object && !f.nvpcf_object && !f.resources_alive);

	/* Involuntary unavailable-device teardown is an error, not publication.
	 * Ordinary handlers are still drained before resources disappear, and
	 * stale pending plus the error prevent helper removal until explicit ack.
	 */
	f = fixture();
	f.capture = false;
	f.observer = false;
	f.power_unavailable = true;
	nh_mark_stale(&f.s);
	release_owner(&f);
	assert(f.ordinary_failures == 3 && !f.s.dispatched && nh_pending(&f.s) == 1);
	assert(!nh_may_unpin_helper(1, 1, nh_pending(&f.s), 1));
	assert(nh_acknowledge_loss(&f.s));
	assert(f.s.acknowledged_loss == 1);
	assert(nh_may_unpin_helper(1, 1, 0, 0));
	conserved(&f.s);
}

static void test_waiting_and_admission(void)
{
	bool initialized = false, legacy_safe = true, active = false;
	unsigned int users = 0, helper_starts = 0;

	assert(nh_start_allowed(0, 0));
	assert(!nh_start_allowed(1, 0)); /* helper never initializes */
	assert(!nh_start_allowed(0, 1)); /* prepared blocks new initialization */
	assert(nh_open_allowed(0, 0)); /* existing external clients keep baseline ABI */
	assert(!nh_open_allowed(1, 0)); /* own open checks legacy result under ldata */
	assert(nh_open_allowed(1, 1));

	/* Initializer succeeded, but closes before deferred attachment takes ldata_lock. */
	initialized = true;
	users = 1;
	initialized = false;
	users = 0;
	assert(nh_open_allowed(1, legacy_safe));
	if (!initialized) {
		if (nh_start_allowed(1, 0))
			helper_starts++;
		assert(!nh_start_allowed(1, 0));
	}
	assert(!active && users == 0 && helper_starts == 0);
	/* A failed ordinary initializer is not a live context or an attach hint. */
	initialized = false;
	assert(!active && !initialized);

	/* A still-live ordinary user allows a real owned open. Closing it after
	 * our increment cannot free context: our reference remains.
	 */
	initialized = true;
	users = 1;
	assert(nh_open_allowed(1, legacy_safe));
	if (initialized) {
		users++;
		active = true; /* effective FINE is separately checked after open */
	}
	users--;
	assert(active && users == 1 && helper_starts == 0);
	/* Legacy names loaded later do not populate cached handles without init.
	 * A later initializer that did populate them changes legacy_safe first;
	 * the own-open check excludes usage increment even after an earlier hint.
	 */
	legacy_safe = false;
	assert(!nh_open_allowed(1, legacy_safe));
	assert(!nh_start_allowed(0, 1));
	/* A name/symbol existing is not a prototype check; lookup/observer failure
	 * is before acceptance, so a failed setup cannot collect private requests.
	 */
	{
		struct nh_state s = { 0 };
		bool observer_registered = false, hook_registered = false;
		bool accepting = observer_registered && hook_registered;
		assert(!accepting && !nh_pending(&s));
		observer_registered = true;
		accepting = observer_registered && hook_registered;
		assert(!accepting && !nh_pending(&s));
	}
}

/* Robust rollback visits successful earlier notifiers. The high-priority
 * helper prepare cannot fail: it pauses work and retains every ownership ref.
 */
static void test_pm_rollback(void)
{
	int fail;

	for (fail = -1; fail < 4; ++fail) {
		struct fixture f = fixture();
		struct nh_replay_control replay = { .enabled = true };
		bool prepared = false, resume_queued = false, nvidia_locked = false;
		bool transition_locked = true;
		int i, completed = 0;

		for (i = 0; i < 4; ++i) {
			if (i == 1) {
				nh_pause_replay(&replay);
				prepared = true;
				/* ACPI still counts while replay is disabled. */
				nh_receive(&f.s, replay.paused);
				assert(f.owner && f.capture && f.observer && !replay.enabled);
				assert(!f.active_gets && !f.completed_release);
			}
			/* The helper itself has neither a failure point nor a PM put. */
			if (i == fail && i != 1)
				break;
			if (i == 2)
				nvidia_locked = true;
			completed++;
		}
		for (i = 0; i < completed; ++i) {
			if (i == 1 && prepared) {
				resume_queued = true;
				assert(transition_locked && f.owner && replay.paused);
			}
			if (i == 2)
				nvidia_locked = false;
		}
		assert(!nvidia_locked);
		transition_locked = false;
		if (resume_queued) {
			assert(!transition_locked && !nvidia_locked && f.owner);
			nh_resume_replay(&replay, nh_capture_ready(f.owner, f.observer,
				f.capture, true));
		}
		assert(replay.enabled && f.owner && f.capture && f.observer);
		assert(resume_queued == (fail != 0));
		assert(nh_pending(&f.s) == (prepared ? 2u : 1u));
		assert(!f.context_frees && !f.completed_release && !f.s.dispatched);
		conserved(&f.s);
	}
	/* Revalidation failure after PM keeps owned data and pending requests. */
	{
		struct nh_replay_control replay = { .enabled = true };
		struct fixture f = fixture();
		nh_pause_replay(&replay);
		nh_resume_replay(&replay, false);
		assert(!replay.enabled && !replay.paused && f.owner && nh_pending(&f.s));
		/* Guard-only clears paused metadata without attaching or replaying. */
		nh_pause_replay(&replay);
		nh_resume_replay(&replay, nh_capture_ready(false, false, false, true));
		assert(!replay.enabled && !replay.paused);
	}
}

struct owner_backend {
	bool text_pinned, null_stack, alloc_fail, get_fail;
	unsigned int allocations, gets, puts, frees;
	char log[64];
};
static struct owner_backend backend;
static int stack_token;
static const unsigned char sample_uuid[16] = { 0, 1, 2, 3, 4, 5, 6, 7,
	8, 9, 10, 11, 12, 13, 14, 15 };

static int backend_alloc(void **stack)
{
	assert(backend.text_pinned);
	backend.allocations++;
	strcat(backend.log, "alloc ");
	if (backend.alloc_fail)
		return -1;
	*stack = backend.null_stack ? NULL : &stack_token;
	return 0;
}
static void backend_free(void *stack)
{
	assert(backend.text_pinned);
	assert(stack == (backend.null_stack ? NULL : &stack_token));
	backend.frees++;
	strcat(backend.log, "free ");
}
static int backend_get(const unsigned char *uuid, void *stack)
{
	assert(backend.text_pinned && !memcmp(uuid, sample_uuid, sizeof(sample_uuid)));
	assert(stack == (backend.null_stack ? NULL : &stack_token));
	backend.gets++;
	strcat(backend.log, "get ");
	return backend.get_fail ? -2 : 0;
}
static void backend_put(const unsigned char *uuid, void *stack)
{
	assert(backend.text_pinned && !memcmp(uuid, sample_uuid, sizeof(sample_uuid)));
	assert(stack == (backend.null_stack ? NULL : &stack_token));
	backend.puts++;
	strcat(backend.log, "put ");
}
static const struct nh_owner_ops owner_ops = {
	.alloc = backend_alloc, .free = backend_free,
	.get = backend_get, .put = backend_put,
};

static void test_kernel_owner(void)
{
	unsigned int null_stack, failure;

	for (null_stack = 0; null_stack < 2; ++null_stack) {
		for (failure = 0; failure < 3; ++failure) {
			struct nh_owner owner = { 0 };
			int error;

			backend = (struct owner_backend){ .text_pinned = true,
				.null_stack = null_stack != 0, .alloc_fail = failure == 1,
				.get_fail = failure == 2 };
			error = nh_owner_acquire(&owner, &owner_ops, sample_uuid);
			assert(error == (failure == 1 ? -1 : failure == 2 ? -2 : 0));
			assert(owner.held == !failure && owner.stack_ready == !failure);
			/* Includes attach failure after a successful get, and a normal put.
			 * Both use exactly the production acquire/release implementation.
			 */
			nh_owner_release(&owner, &owner_ops, sample_uuid);
			nh_owner_release(&owner, &owner_ops, sample_uuid);
			assert(!owner.held && !owner.stack_ready && !owner.stack);
			assert(backend.allocations == 1);
			assert(backend.gets == (failure == 1 ? 0u : 1u));
			assert(backend.puts == (!failure ? 1u : 0u));
			assert(backend.frees == (failure == 1 ? 0u : 1u));
			if (!failure)
				assert(!strcmp(backend.log, "alloc get put free "));
			backend.text_pinned = false; /* Only after put and stack free. */
		}
	}
	/* Every prerequisite is required before capture can accept a callback. */
	for (failure = 0; failure < 16; ++failure)
		assert(nh_capture_ready((failure & 1u) != 0, (failure & 2u) != 0,
			(failure & 4u) != 0, (failure & 8u) != 0) == (failure == 15));
}

struct publication_fixture {
	struct nh_owner owner;
	bool waiting, published, accepting, hooks, text_reference, rollback_fail;
	bool publish_fail;
	unsigned int stage_fail, ordinary_calls, captured_calls;
	char log[96];
};

static void publication_receive(struct publication_fixture *f)
{
	if (f->accepting)
		f->captured_calls++;
	else
		f->ordinary_calls++;
}

static int publication_stage(void *ctx)
{
	struct publication_fixture *f = ctx;

	strcat(f->log, "stage ");
	assert(!f->published && !f->accepting);
	f->text_reference = true;
	if (f->stage_fail == 1)
		return -1;
	if (!f->waiting)
		assert(!nh_owner_acquire(&f->owner, &owner_ops, sample_uuid));
	if (f->stage_fail == 2)
		return -2;
	/* Observer/hook registration is still before publication and acceptance. */
	f->hooks = true;
	publication_receive(f);
	return f->stage_fail == 3 ? -3 : 0;
}

static int publication_publish(void *ctx)
{
	struct publication_fixture *f = ctx;

	strcat(f->log, "publish ");
	assert(f->hooks && !f->accepting);
	publication_receive(f);
	if (f->publish_fail)
		return -4;
	f->published = true;
	return 0;
}

static void publication_activate(void *ctx)
{
	struct publication_fixture *f = ctx;

	strcat(f->log, "activate ");
	assert(f->published && !f->captured_calls);
	f->accepting = !f->waiting;
}

static int publication_rollback(void *ctx)
{
	struct publication_fixture *f = ctx;

	strcat(f->log, "rollback ");
	assert(!f->published && !f->accepting && !f->captured_calls);
	if (f->rollback_fail)
		return -5;
	f->hooks = false;
	nh_owner_release(&f->owner, &owner_ops, sample_uuid);
	f->text_reference = false;
	return 0;
}

static const struct nh_publication_ops publication_ops = {
	.stage = publication_stage, .publish = publication_publish,
	.activate = publication_activate, .rollback = publication_rollback,
};

static void test_publication(void)
{
	unsigned int failure, publish_fail, rollback_fail, waiting;

	for (waiting = 0; waiting < 2; ++waiting)
	for (failure = 0; failure < 4; ++failure)
	for (publish_fail = 0; publish_fail < 2; ++publish_fail)
	for (rollback_fail = 0; rollback_fail < 2; ++rollback_fail) {
		struct publication_fixture f = { .stage_fail = failure,
			.publish_fail = publish_fail != 0, .rollback_fail = rollback_fail != 0,
			.waiting = waiting != 0 };
		struct nh_publication_result result;
		bool failed = failure || publish_fail;

		backend = (struct owner_backend){ .text_pinned = true };
		result = nh_publish(&f, &publication_ops);
		assert((result.error != 0) == failed);
		assert(result.publication_attempted == !failure);
		assert(result.rollback_error == (failed && rollback_fail ? -5 : 0));
		assert(!f.captured_calls);
		if (failed) {
			assert(!f.published && !f.accepting && strstr(f.log, "rollback "));
			assert(strstr(f.log, "activate ") == NULL);
			if (rollback_fail) {
				/* Init must retain code/diagnostics if cleanup cannot finish. */
				assert(f.text_reference && !nh_may_unpin_helper(0, 0, 0, 1));
				f.rollback_fail = false;
				assert(!publication_rollback(&f));
			}
			assert(!f.owner.held && !f.owner.stack_ready && !f.hooks && !f.text_reference);
		} else {
			assert(!strcmp(f.log, "stage publish activate "));
			assert(f.published && f.accepting == !waiting);
			assert(f.owner.held == !waiting);
			/* Cleanup after the publication-order assertion. */
			nh_owner_release(&f.owner, &owner_ops, sample_uuid);
		}
		assert(backend.puts <= 1 && backend.frees <= 1);
	}
	assert(nh_may_pin_driver(true, true));
	assert(!nh_may_pin_driver(false, true)); /* COMING/GOING are not LIVE. */
	assert(!nh_may_pin_driver(true, false));
	assert(!nh_may_pin_driver(false, false));
}

static void test_loss(void)
{
	struct nh_state s = { 0 };

	nh_receive(&s, true);
	assert(!nh_acknowledge_loss(&s)); /* ordinary detach cannot discard */
	nh_mark_stale(&s);
	assert(nh_pending(&s) == 1 && !s.acknowledged_loss);
	assert(nh_acknowledge_loss(&s));
	assert(!nh_pending(&s) && s.acknowledged_loss == 1 && !s.dispatched);
	assert(!nh_acknowledge_loss(&s));
	conserved(&s);
}

static void test_text(void)
{
	char good[] = "Name: GPU\nRuntime D3 status:  Enabled (fine-grained) \r\nOther: value\n";
	char fallback[] = "Runtime D3 status: Disabled\nExplanation: Enabled (fine-grained)\n";
	char embedded[] = "Other: Runtime D3 status: Enabled (fine-grained)\n";
	char uuid[] = "GPU UUID: GPU-00112233-4455-6677-8899-aabbccddeeff\n";

	assert(!strcmp(nh_field(good, "Runtime D3 status:"), "Enabled (fine-grained)"));
	assert(!strcmp(nh_field(fallback, "Runtime D3 status:"), "Disabled"));
	assert(nh_field(embedded, "Runtime D3 status:") == NULL);
	assert(!strcmp(nh_field(uuid, "GPU UUID:"), "GPU-00112233-4455-6677-8899-aabbccddeeff"));
}

static void test_many_interleavings(void)
{
	struct nh_state s = { 0 };
	uint32_t random = 1;
	unsigned int i;

	for (i = 0; i < 100000; ++i) {
		random = random * UINT32_C(1664525) + UINT32_C(1013904223);
		switch (random % 4) {
		case 0:
		case 1:
			nh_receive(&s, (random & 16) != 0);
			break;
		case 2:
			if (nh_pending(&s)) {
				nh_u64 snapshot = nh_snapshot(&s);
				if (random & 32)
					nh_receive(&s, false);
				nh_finish(&s, snapshot, (random & 64) != 0);
			}
			break;
		default:
			if (nh_pending(&s) && (random & 128)) {
				nh_mark_stale(&s);
				assert(nh_acknowledge_loss(&s));
			}
			break;
		}
		conserved(&s);
	}
	/* Sequence subtraction also handles wrap without ordering comparisons. */
	s = (struct nh_state){ .received = UINT64_MAX, .completed = UINT64_MAX };
	nh_receive(&s, true);
	assert(nh_pending(&s) == 1);
	nh_finish(&s, nh_snapshot(&s), true);
	assert(!nh_pending(&s) && s.dispatched == 1);
}

int main(void)
{
	test_batches();
	test_absent_dispatch_and_feedback();
	test_missed_observer();
	test_transition_observations();
	test_detach();
	test_power_and_guard_lifetime();
	test_waiting_and_admission();
	test_pm_rollback();
	test_kernel_owner();
	test_publication();
	test_loss();
	test_text();
	test_many_interleavings();
	puts("PASS: batches, failure retention, PM feedback, observer misses, detach, kernel ownership, publication, rollback, loss, parsing and 100000 interleavings");
	return 0;
}
