# NVPCF hook

`nvpcf_hook` records NVIDIA's ACPI NVPCF CHANGE notifications while the GPU is
idle. A worker calls NVIDIA's original callback only after obtaining a
conditional Linux runtime-PM reference, which it retains through the callback.
The worker does not wake an idle GPU. Other ACPI event types use NVIDIA's
original path.

CHANGE does not include a payload with new limits. One dispatch covers the
requests in its snapshot; arrivals during that dispatch remain pending. The
helper retains an NVIDIA kernel-client device reference so the callback context survives between
capture and delivery. Keeping NVIDIA's module text loaded alone would not keep
that context alive.

## Requirements

Attachment requires these conditions:

- Exactly one NVIDIA display-class PCI device, reported as fixed and not
  attached through Thunderbolt. A second NVIDIA GPU appearing stops capture
  before its driver probe can replace NVIDIA's global ACPI context.
- The selected device is bound to a fully loaded `nvidia` module, with a readable
  `GPU UUID` in its `/proc/driver/nvidia/gpus/<PCI address>/information` entry.
- Its power entry reports `Runtime D3 status: Enabled (fine-grained)`. The
  requested `DynamicPowerManagement` parameter is insufficient because
  initialization can fall back to disabled PM.
- NVIDIA reports `UseKernelSuspendNotifiers=1`, and
  `/proc/driver/nvidia/suspend` is absent. Procfs-driven suspend is unsupported.
- The live ACPI device namespace does not contain `NVIF` or `WMMX` methods.
  Early global-handler cleanup would also clear these cached legacy handles.
- x86-64, ftrace with register saving and instruction-pointer modification,
  kretprobes, system PM, tasks RCU and the required exported runtime-PM APIs and
  tracepoints. Kernel IBT and Clang CFI must be disabled. The opaque private
  function-pointer calls have not been ported to those configurations.
- NVIDIA retains the private function declarations and call boundaries
  described below. Symbol resolution does not verify a C prototype.

The helper refuses to initialize the GPU itself. If an ordinary client has not
initialized it, the helper stays `waiting`. A successful ordinary initializer
queues an attachment attempt. That success is only a hint: if the last ordinary
client closes first, the helper refuses startup at the actual kernel get and
continues waiting. It can temporarily wake the GPU while acquiring ownership.

The kernel reference uses `nvidia_dev_get_uuid` and `nvidia_dev_put_uuid`. These
use NVIDIA's normal open/close count under `ldata_lock`, without the VFS close
path's system-PM read lock or its later PCI-removal operation. The reference is
not a persistent Linux power reference in fine-grained mode. NVIDIA's UVM code
calls `nvidia_dev_block_gc6` separately after acquiring this reference; this
helper does not call that function.

NVIDIA WARN-checks an external-kernel-client count update but does not return its
failure. Successful acquisition establishes device ownership, not proof that
the count update succeeded. Prepare and detach before planned reset or drain.
Arbitrary privileged resets and physical hardware loss can invalidate the GPU.

## Control interface

The root-only device is `/dev/nvpcf-hook`. Initial setup registers ownership,
the observer and hooks with capture still disabled. It publishes the control
device last, then enables capture with a non-failing state update. Before that,
notifications use NVIDIA's ordinary path. Initial setup or publication errors
normally roll back and fail module loading without captured requests to flush.
If cleanup cannot unregister a hook or safely release ownership, the helper
instead remains loaded and logs the error. It attempts to publish diagnostics
when publication has not already failed; simultaneous publication and cleanup
failure can leave a retained helper without a control device.

A successful load can also mean `waiting`, and later asynchronous attachment can
become `unavailable`. Read status to establish active monitoring. Status queries
only read helper state; they do not attempt attachment, wake the GPU or retry a
failed batch. Asynchronous attachment failures log their state and error when
they occur. The client supports five commands:

```
sudo ./hook/nvpcf-hookctl status
sudo ./hook/nvpcf-hookctl prepare
sudo ./hook/nvpcf-hookctl detach
sudo ./hook/nvpcf-hookctl resume
sudo ./hook/nvpcf-hookctl acknowledge-loss
```

- `status` reads the state and counters.
- `prepare` deliberately resumes the GPU and attempts pending delivery while
  retaining ownership and monitoring. It is a fallible preflight before a swap
  changes registration files, services or client handles. Waiting and guard-only
  states pass preflight without claiming that monitoring is active.
- `detach` stops capture and resume triggers, waits for redirected calls, joins
  private work, takes an active Linux reference even with zero pending requests,
  and flushes the final batch. The observer remains through that flush. It then
  removes the observer, puts the kernel-client reference, frees its NVIDIA stack,
  balances Linux power and drops its NVIDIA text reference. The state becomes
  `guard-only` while NVIDIA is still loaded.
- `resume` permits ordinary initialization again and attempts attachment. It can
  return success in `waiting`; check status before claiming active monitoring.
  Existing owned context survives a failed post-sleep revalidation.
- `acknowledge-loss` explicitly accepts a recorded loss or teardown fault after
  NVIDIA has exited and the guards have stopped. Stale requests are transferred
  to `acknowledged_loss`. A swap script must not issue this automatically.

A failed final flush retains pending requests and device ownership and attempts
to restore interception. If restoration also fails, inspect `last_error`, the
state and the kernel log, then retry detach. Restoration keeps `quiesced` and
the error while effective fine-grained PM is unverified; `resume` then attempts
revalidation. It reports `active` only when capture and replay are ready. New
notifications use NVIDIA's ordinary path while interception is absent. A hardware failure during an
irreversible put can instead leave an error in `unavailable` after ownership
ended; it is not reported as successful detach.

The helper keeps itself pinned while it owns context or teardown guards. Normal
`rmmod nvpcf_hook` therefore refuses until explicit preparation has succeeded and
NVIDIA has exited. Module exit only removes the remaining registrations and
metadata; it does not attempt fallible delivery. Forced module removal is
unsupported.

### Swap ordering

1. Run `prepare` before modifying registration files, stopping services or
   revoking client handles.
2. Keep the helper's kernel reference while those other holders are released.
3. Run final `detach`. It can still fail after the earlier changes; stop and
   recover if it does.
4. Keep the helper loaded in `guard-only` while unloading NVIDIA's dependent
   modules and then `nvidia` itself.
5. After NVIDIA exit and guard removal, unload the helper.
6. Load and verify the replacement NVIDIA modules, then load the helper again.

On an aborted swap, `resume` must restore initialization admission before a
dependent module needs to initialize the GPU. An absent control device is an I/O
error, not successful detach. Integration must distinguish an unloaded helper
from a loaded helper whose operation failed.

### Unavailable devices

The pre-unbind notifier runs before NVIDIA removes the device from its UUID
lookup list. It serializes with controller operations so a concurrent UUID put
finishes before list deletion. It must not bypass that serialization merely
because a put is in progress.

PCI notifications cannot veto removal. If final delivery fails, the helper stops
capture, records undelivered requests as stale, removes the observer and releases
ownership. Saved callback arguments are invalidated before release. Errors or
stale requests retain the helper's diagnostic state until explicit
`acknowledge-loss` after NVIDIA exit. A failure to unregister interception or an
unsupported cached legacy-method state instead retains ownership and reports
that removal may wait.

The same procedure runs when another NVIDIA GPU appears. These notifications do
not make arbitrary hardware loss recoverable.

## System PM

The highest-priority prepare notifier pauses replay and joins private work
before NVIDIA takes its system-PM locks. Capture remains count-only. The helper
retains its kernel-client reference, opaque stack and NVIDIA text reference
through suspend, hibernation and rollback. Prepare does not allocate, acquire
power or release ownership, so it does not introduce a failure requiring its own
robust-notifier rollback.

A post or rollback notification queues work. That work takes
`lock_system_sleep()` before device and helper lifecycle locks, and therefore
waits for the complete PM sequence, including NVIDIA's post or rollback. It
revalidates effective fine-grained PM before enabling replay. A validation
failure retains the owned callback context and pending requests in `quiesced`.
Waiting state may attempt attachment; guard-only state only clears paused
metadata and does not acquire ownership. Manual prepare/detach also take the
system-sleep lock and can safely clear paused metadata before the queued worker
runs. Involuntary bus removal does not clear it or claim verified delivery while
PM is paused.

On the inspected host, enabled NVIDIA sleep units invoke
`/usr/bin/nvidia-sleep.sh`. With the procfs suspend entry absent, that script
exits before changing VTs or writing procfs. Attachment checks this mode.

## Teardown and locking

The guard intercepts committed `nv_shutdown_adapter` entry, before NVIDIA frees
interrupts or adapter resources or takes the later API/GPU locks. It calls
NVIDIA's own `nv_acpi_methods_uninit` first. This removes and drains both global
PSR and NVPCF handlers, clearing NVIDIA's pointers. The later core cleanup sees
the cleared pointers and skips a second removal/free. C0-only draining would be
insufficient because the PSR callback also acquires the API lock.

Before owner release, helper capture, redirected calls, private work and the
observer have stopped. Fresh ACPI calls therefore run synchronously in NVIDIA,
rather than enqueueing helper work with soon-freed data. A short active Linux
reference covers handler cleanup and resource destruction. If our final put did
not destroy the adapter, a later guarded teardown acquires its own short active
reference. Guard-only state does not retain a Linux power reference across
NVIDIA's later runtime-PM reinitialization.

Normal final close holds `ldata_lock`; PCI removal's first stop can also hold
NVIDIA's Linux-device-list semaphore. The inspected ordinary PSR/C0 dispatch
paths use API/GPU locks and existing RM client handles, not those Linux
semaphores. This argument does not infer RM subscribers from open-file counts or
validate arbitrary third-party kernel callbacks.

Legacy-method admission is checked at setup and around a successful ordinary
initializer. Own open checks the result under `ldata_lock` before incrementing
usage. Owned context prevents ordinary reinitialization; guard-only blocks new
initialization. A table loaded later does not by itself repopulate the cached
legacy handles. An unsupported state retains ownership and diagnostics instead
of attempting unsafe cleanup.

The helper's lock order is system-sleep serialization, device lock where
needed, lifecycle mutex, then delivery mutex. Pre-unbind already holds the
device lock and waits for the lifecycle mutex. Committed shutdown does not take
that mutex, including when called inside the helper's put. The private spinlock
is not held while taking the device PM lock or calling RM. Runtime-PM probes
enter with the PM lock held and take only the private spinlock.

Both capture and guard handlers reserve counters before redirecting the
instruction pointer, including the gap before replacement entry. Stopping
interception joins those reservations and private work. Tasks-RCU grace periods
also cover the C epilogues after the counters reach zero.

Guard code remains pinned through actual NVIDIA exit. `MODULE_STATE_GOING`
occurs after exit and before NVIDIA text is freed; the notifier then unregisters
and drains the guards. This protects helper execution through teardown. It is
not the mechanism that owns ACPI callback data.

## Dispatch completion and replay

The original callback is `void(acpi_handle, u32, void *)`. Its data is a non-NULL
allocated `nv_acpi_t *`; that object's `notifier_data` member is NULL for NVPCF.
They are different pointers. Saved arguments are cleared before device release.

An outer callback return does not prove dispatch: API-lock TLS allocation can
fail first. A kretprobe observes `gpuNotifySubDeviceEvent_IMPL` returning for
index 177 on the helper's serialized delivery task. The checked dispatcher has
two early-return assertions, for lockless ISR context and an invalid index. The
ordinary callback initializes thread state with `THREAD_STATE_FLAGS_NONE`, and
177 is valid in all four inspected tags. Subscriber filtering and absent
listeners are normal completion. The counter records a normal dispatch-loop
return, not consumer receipt. A missed observation retains pending requests and
can cause duplicate dispatch on a later retry.

The replay trigger pairs `rpm_resume` entry from SUSPENDED with a later
`rpm_return_int` event showing ACTIVE under the PM lock. Idle/put events cannot
rearm it. Own delivery and reference-release events are suppressed. Failed
dispatch does not immediately retry; another notification, real external resume
or explicit control operation can attempt it again. A power-state observation
is not ownership: delivery still obtains its own conditional active reference.

Targets are resolved by module-qualified temporary kprobes, followed by exact
`ftrace_set_filter_ip`. The exported regex setter requires a private trace-array
pointer for `:mod:`. The 6.8 entry-address adjustment matches its ENDBR helper;
7.0 uses `ftrace_get_symaddr`.

## Status

- `state`: `starting`, `waiting`, `active`, `quiesced`, `suspended`, `guard-only`,
  `unavailable` or `detached`.
- `pci`, `gpu_uuid`, `nvidia_version`: selected device and actual loaded version.
- `device_reference`, `stack_ready`, `nvidia_reference`, `power_reference`:
  separate ownership records. A successfully allocated NVIDIA stack can be NULL.
- `guards_registered`, `initialization_blocked`, `replay_paused`: lifecycle state.
- `received_requests`: captured CHANGE calls. Ordinary calls after interception
  stops are not counted.
- `pending_requests`: captured requests not covered by observed dispatch or an
  explicit loss acknowledgement.
- `deferred_requests`: captured calls sampled inactive or received while replay
  was paused/disabled.
- `requests_dispatched`, `dispatch_batches`, `coalesced_requests`: covered
  requests, observed completed batches, and their difference.
- `dispatch_attempts`, `dispatch_failures`: attempts that reached the original
  callback, and attempts without an observed dispatcher return.
- `power_failures`, `delivery_failures`: power/device errors, and their sum with
  dispatch failures. An inactive conditional get is ordinary deferral.
- `observer_missed`: kretprobe misses, including unrelated calls that could not
  obtain an instance before filtering.
- `redirected`, `starting_calls`, `opening_calls`, `shutdown_calls`: reserved
  replacement calls that have not reached their completion counter.
- `prelock_cleanup_calls`, `guard_failures`, `legacy_namespace_blocked`: early
  cleanup calls, teardown faults and unsupported cached legacy state.
- `stale_pending`, `acknowledged_loss`, `last_error`: unavailable pending state,
  explicitly accepted losses and the latest errno.

## Private interfaces and evidence

There is not a runtime version-number allowlist. Attachment rejects concrete
configuration, symbol-lookup and registration failures. The extra observer
requires a private precompiled-core symbol as well as kernel-open functions.
Future prototype, dispatcher or compiler changes can require a hook update even
when symbol names still exist. The source/ELF checker detects the specified
declarations and call boundaries offline; it is not a runtime prototype check.

Ownership/allocator/guard declarations and source roles were checked in
595.99.02, 610.57.04, 615.71.09 and 615.78.08. The dispatcher body is identical in
those tags; NVPCF is 177 and MAXCOUNT is 199, 200, 202 and 202. Stock-core ELF
inspection used Ubuntu's 615.71.09 `nv-kernel.o_binary.ubuntu`. Linked NVIDIA
modules for all three installed kernels retained the required symbols, fentry
and guard call boundaries.

Review references below use NVIDIA 615.71.09 unless stated otherwise:

- `kernel-open/nvidia/nv.c:1790-1868,2215-2305,5464-5594`: startup admission,
  FINE references, stop/close and UUID get/put. UUID get can initialize candidate
  devices, which is why the helper's own startup refusal is required.
- `kernel-open/nvidia/nv-modeset-interface.c:33-44,146-177` and
  `kernel-open/common/inc/nv-linux.h:847-872`: real linked opaque stack callbacks,
  including successful NULL allocation. Inline cache allocators are not symbols.
- `kernel-open/nvidia/nv_uvm_interface.c:142-196`: UUID ownership and separate
  GC6 blocking. The helper does not copy the versioned RM-ops structure.
- `src/nvidia/src/kernel/gpu/gpu_uuid.c:43-75,325-368` and
  `src/nvidia/src/libraries/nvport/string/string_generic.c:269-339`: raw UUID bytes
  are formatted sequentially, high nibble first, in 8/4/4/4/12 hex groups.
  Kernel `uuid_parse`, not GUID byte reordering, reconstructs the raw UUID.
- `kernel-open/nvidia/nv-acpi.c:129-245,317-391` and
  `src/nvidia/arch/nvalloc/unix/src/osapi.c:4590-4653,6179-6211`: PSR/C0
  callbacks, handler ownership, cached methods, cleanup and core API/GPU locks.
- `kernel-open/nvidia/nv.c:2142-2213`: committed adapter shutdown disables the
  adapter and releases resources, so global handler cleanup must precede it.
- `src/nvidia/src/kernel/rmapi/rmapi.c:682-699` and
  `src/nvidia/src/kernel/gpu/gpu_rmapi.c:624-732`: TLS allocation failure and
  dispatch completion.
- Ubuntu kernel commit `102c85de1d6726aa44823e8f9740eea74216c7a5`,
  `drivers/base/power/runtime.c:934-965`: final Linux runtime status follows the
  NVIDIA resume callback; return events are not exclusively resume completion.
- The same kernel's `drivers/base/dd.c:1338-1363` and
  `kernel/module/main.c:828-847`: pre-unbind before driver removal; exit before
  GOING before text free.
- The same kernel's `kernel/notifier.c:114-125`,
  `kernel/power/suspend.c:591-625` and `kernel/power/main.c:67-82`: robust rollback
  and exported system-sleep serialization.

## Build and non-live checks

From the repository root:

```
make -C hook check
make -C hook modules KDIR=/usr/src/linux-headers-6.8.0-147-generic
make -C hook modules KDIR=/usr/src/linux-headers-7.0.0-34-generic
make -C hook modules KDIR=/usr/src/linux-headers-7.0.0-38-generic
python3 -I -B hook/tests/check_nvidia.py --source-tar /path/to/NVIDIA-tag.tar.gz
python3 -I -B hook/tests/check_nvidia.py --kernel-source /usr/src/nvidia-615.71.09
python3 -I -B hook/tests/check_nvidia.py --core-object /usr/src/nvidia-615.71.09/nvidia/nv-kernel.o_binary.ubuntu
python3 -I -B hook/tests/check_nvidia.py --module-object /path/to/decompressed/nvidia.ko
```

`check` compiles shared production state, ownership and lifecycle code into a
userspace test. Cases cover snapshots, arrivals during dispatch, absent/missed
dispatch, duplicate retry, PM feedback, detach restoration, retained ownership
through PM rollback, kernel put before concurrent list deletion, NULL/non-NULL
stacks, failed acquisition/attachment cleanup, one-time release, publication
after setup, publication/rollback failures, NVIDIA LIVE admission, startup
admission, late ordinary PSR/C0 drain, later empty cleanup, loss acknowledgement,
field parsing and exhaustive operation sequences through depth five. CLI tests
use ordinary temporary files. The state exploration checks all 537824 sequences of length five from
14 operations, visiting 579195 sequence-prefix nodes and applying 160796 valid
transitions. It asserts coverage of every operation, seven lifecycle phases,
arrivals during dispatch, missed/successful batches, invalid restoration,
revalidation recovery and loss acknowledgement. Duplicate states and
inapplicable choices are included; these are operation sequences, not counts of
distinct states. A separate matrix checks 64 readiness/error combinations. The
portable suite also passes AddressSanitizer and UndefinedBehaviorSanitizer.

These tests execute shared logic and model kernel ordering. They do not execute
kernel attachment, ACPI, runtime PM or NVIDIA. The source checker reads archives
without extracting or executing their code. It validates both dispatcher
assertion conditions, their placement before the normal dispatch loop and that
loop's bounds. ELF checks require calls to exact symbol entries or corresponding
PC32/PLT32 relocations on the call displacement with addend -4. They reject
similarly named functions and nonzero offsets that could skip instrumentation.
The tests assemble positive and negative ELF fixtures and mutate source
conditions, including an assertion that excludes index 177. These checks remain
separate from source C declarations; symbol tables do not validate C types.

The module compiles against the three installed header targets above. Compiler
name-string and missing pahole/vmlinux notices do not prevent module linking;
BTF generation is skipped without vmlinux. `dkms.conf` describes building the
package but these commands do not install DKMS configuration or load modules.

Live stock-core acceptance is still required. The source-patched core can mask
an interception defect and can defer internally without an observed dispatcher
return. Non-live checks and builds are not that acceptance test. Live checks
must establish idle behavior, replay during a real external wake, system PM and
rollback, final detach/teardown, and failed-swap recovery before deployment.
