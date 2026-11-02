/* SPDX-License-Identifier: GPL-2.0 */
#ifndef NVPCF_HOOK_LIFECYCLE_H
#define NVPCF_HOOK_LIFECYCLE_H
#include "state.h"

static inline int nh_may_unpin_helper(int hooks_stopped, int ownership_released,
				     nh_u64 requests_pending, int fault)
{
	return hooks_stopped && ownership_released && !requests_pending && !fault;
}

/* The caller serializes lifecycle operations. stop_capture also waits for
 * redirected calls and the replay worker. The observer remains registered
 * through the final dispatch.
 */
static inline int nh_start_allowed(int own_open, int initialization_blocked)
{
	return !own_open && !initialization_blocked;
}

static inline int nh_open_allowed(int own_open, int legacy_safe)
{
	return !own_open || legacy_safe;
}

static inline bool nh_may_pin_driver(bool live, bool same_module)
{
	return live && same_module;
}

/* Registered guards can be reused only while their NVIDIA instance is known.
 * Incomplete cleanup blocks attachment even if a replacement reuses an address.
 */
static inline bool nh_instance_attach_allowed(bool retirement_failed,
		bool guards_registered, bool instance_known)
{
	return !retirement_failed && (!guards_registered || instance_known);
}

/* A successful stack allocation can return NULL. Callers serialize access and
 * retain the NVIDIA module reference until the device reference and stack have
 * been released. Acquisition requires an empty owner; release can be called
 * again after cleanup.
 */
struct nh_owner {
	bool held;
	bool stack_ready;
	void *stack;
};

struct nh_fine_ops {
	int (*block)(const unsigned char *, void *);
	int (*unblock)(const unsigned char *, void *);
};

struct nh_owner_ops {
	int (*alloc)(void **);
	void (*free)(void *);
	int (*get)(const unsigned char *, void *);
	void (*put)(const unsigned char *, void *);
};

static inline int nh_owner_acquire(struct nh_owner *owner,
		const struct nh_owner_ops *ops, const unsigned char *uuid)
{
	int error;

	owner->stack = NULL;
	error = ops->alloc(&owner->stack);
	if (error)
		return error;
	owner->stack_ready = true;
	error = ops->get(uuid, owner->stack);
	if (error) {
		ops->free(owner->stack);
		owner->stack = NULL;
		owner->stack_ready = false;
	} else {
		owner->held = true;
	}
	return error;
}

static inline void nh_owner_release(struct nh_owner *owner,
		const struct nh_owner_ops *ops, const unsigned char *uuid)
{
	if (owner->held) {
		ops->put(uuid, owner->stack);
		owner->held = false;
	}
	if (owner->stack_ready) {
		ops->free(owner->stack);
		owner->stack = NULL;
		owner->stack_ready = false;
	}
}

/* Stage leaves capture disabled. Once publication succeeds, activation cannot
 * fail. Failed staging/publication rolls back before init can release code.
 */
struct nh_publication_ops {
	int (*stage)(void *);
	int (*publish)(void *);
	void (*activate)(void *);
	int (*rollback)(void *);
};

struct nh_publication_result {
	int error;
	int rollback_error;
	bool publication_attempted;
};

static inline struct nh_publication_result
nh_publish(void *ctx, const struct nh_publication_ops *ops)
{
	struct nh_publication_result result = { 0, 0, false };

	result.error = ops->stage(ctx);
	if (!result.error) {
		result.publication_attempted = true;
		result.error = ops->publish(ctx);
	}
	if (result.error)
		result.rollback_error = ops->rollback(ctx);
	else
		ops->activate(ctx);
	return result;
}

struct nh_lifecycle_ops {
	int (*stop_capture)(void *);
	int (*hold_active)(void *);
	int (*dispatch)(void *);
	int (*restore_capture)(void *);
	void (*release_active)(void *);
	int (*release_owner)(void *);
	void (*stop_observer)(void *);
};

struct nh_quiesce_result {
	int error;
	int restore_error;
};

static inline struct nh_quiesce_result
nh_quiesce(void *ctx, const struct nh_lifecycle_ops *ops)
{
	struct nh_quiesce_result result = { 0, 0 };

	result.error = ops->stop_capture(ctx);
	if (result.error)
		return result;
	result.error = ops->hold_active(ctx);
	if (!result.error)
		result.error = ops->dispatch(ctx);
	if (result.error) {
		result.restore_error = ops->restore_capture(ctx);
		ops->release_active(ctx);
		return result;
	}
	ops->stop_observer(ctx);
	result.error = ops->release_owner(ctx);
	ops->release_active(ctx);
	return result;
}

struct nh_teardown_ops {
	void (*cleanup_handlers)(void *);
	void (*destroy_resources)(void *);
};

/* Use this ordering only after entry to adapter shutdown. Cleanup waits for
 * ordinary callbacks while resources still exist and before the RM API and
 * GPU locks are taken.
 */
static inline void nh_teardown(void *ctx, const struct nh_teardown_ops *ops)
{
	ops->cleanup_handlers(ctx);
	ops->destroy_resources(ctx);
}

#endif
