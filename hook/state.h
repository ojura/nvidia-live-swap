/* SPDX-License-Identifier: GPL-2.0 */
#ifndef NVPCF_HOOK_STATE_H
#define NVPCF_HOOK_STATE_H

#ifdef __KERNEL__
#include <linux/types.h>
typedef u64 nh_u64;
#else
#include <stdbool.h>
#include <stdint.h>
typedef uint64_t nh_u64;
#endif

struct nh_replay_control {
	bool enabled;
	bool paused;
};

static inline bool nh_capture_ready(bool owned, bool observer, bool hook, bool legacy_safe)
{
	return owned && observer && hook && legacy_safe;
}

static inline void nh_pause_replay(struct nh_replay_control *replay)
{
	replay->enabled = false;
	replay->paused = true;
}

static inline void nh_resume_replay(struct nh_replay_control *replay, bool ready)
{
	replay->paused = false;
	replay->enabled = ready;
}

/* The caller serializes access. A batch covers the requests in its snapshot. */
struct nh_state {
	nh_u64 received;
	nh_u64 completed;
	nh_u64 deferred;
	nh_u64 dispatched;
	nh_u64 batches;
	nh_u64 attempts;
	nh_u64 failures;
	nh_u64 power_failures;
	nh_u64 acknowledged_loss;
	bool stale;
	bool resume_armed;
};

static inline nh_u64 nh_pending(const struct nh_state *s)
{
	return s->received - s->completed;
}

static inline void nh_receive(struct nh_state *s, bool deferred)
{
	s->received++;
	if (deferred)
		s->deferred++;
}

static inline nh_u64 nh_snapshot(const struct nh_state *s)
{
	return s->received;
}

static inline void nh_finish(struct nh_state *s, nh_u64 snapshot, bool seen)
{
	s->attempts++;
	if (!seen) {
		s->failures++;
		return;
	}
	s->dispatched += snapshot - s->completed;
	s->completed = snapshot;
	s->batches++;
}

/* Idle/put tracepoints alone must not retry a failed batch. */
static inline void nh_resume_begin(struct nh_state *s, bool suspended, bool own)
{
	if (own)
		s->resume_armed = false;
	else if (suspended)
		s->resume_armed = true;
}

static inline bool nh_pm_end(struct nh_state *s, bool active, bool suspended,
			    int result, bool own)
{
	if (own || (suspended && result < 0)) {
		s->resume_armed = false;
		return false;
	}
	if (!active || !s->resume_armed)
		return false;
	s->resume_armed = false;
	return nh_pending(s) != 0;
}

/* An unavailable device cannot receive replay. Retain the count until ack. */
static inline void nh_mark_stale(struct nh_state *s)
{
	s->stale = nh_pending(s) != 0;
}

static inline bool nh_acknowledge_loss(struct nh_state *s)
{
	if (!s->stale)
		return false;
	s->acknowledged_loss += nh_pending(s);
	s->completed = s->received;
	s->stale = false;
	return true;
}

#endif
