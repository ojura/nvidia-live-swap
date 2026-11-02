# NVPCF hook

`nvpcf_hook` catches NVIDIA's ACPI NVPCF CHANGE notifications. While the GPU is
runtime-suspended, it records them instead of calling NVIDIA's handler. When the
GPU wakes, a worker obtains a Linux runtime-PM reference and calls the original
handler. It obtains that reference only while the GPU is already active, so
ordinary delivery does not wake the GPU. Other ACPI event types use NVIDIA's
original path.

CHANGE notifications do not include new power limits as a payload. One call can
therefore handle every request that was pending when delivery began. Requests
that arrive during the call remain pending for the next delivery.

The hook retains an NVIDIA kernel-client device reference so that the handler's
data remains allocated until delivery. A module reference alone would keep
NVIDIA's code loaded, but would not keep that data allocated.

## Requirements

The hook attaches only when these conditions hold:

- The read-only `expected_nvidia_build_id` parameter contains the GNU build ID
  checked by the loader, as 2 through 128 unseparated hex characters (1 through
  64 bytes). Both hex cases are accepted. Missing or malformed input fails
  module initialization.
- Exactly one NVIDIA display-class PCI device is present, reported as fixed and
  not attached through Thunderbolt. A second NVIDIA GPU appearing stops capture
  before its driver probe can replace NVIDIA's global ACPI context.
- The selected device is bound to a fully loaded `nvidia` module, with a readable
  `GPU UUID` in `/proc/driver/nvidia/gpus/<PCI address>/information`.
- Its power entry reports `Runtime D3 status: Enabled (fine-grained)`. The
  requested `DynamicPowerManagement` parameter is insufficient because
  initialization can fall back to disabled runtime PM.
- NVIDIA reports `UseKernelSuspendNotifiers=1`, and
  `/proc/driver/nvidia/suspend` is absent. Procfs-driven suspend is unsupported.
- The live ACPI device namespace does not contain `NVIF` or `WMMX` methods.
  Removing the global handlers early would also clear these cached legacy
  handles.
- The kernel supports x86-64, ftrace with register saving and instruction-pointer
  modification, kretprobes, system PM, tasks RCU and the required exported
  runtime-PM APIs and tracepoints. Kernel IBT and Clang CFI must be disabled; the
  private function-pointer calls have not been ported to those configurations.
- NVIDIA retains the private function declarations and call boundaries described
  under [NVIDIA interfaces and compatibility checks](#nvidia-interfaces-and-compatibility-checks).
  Symbol resolution does not verify a C prototype.

The hook does not initialize the GPU itself. It needs an initialized GPU and a
readable cached UUID; otherwise it stays `waiting`. When another client
successfully initializes the GPU, the hook queues an attachment attempt. If that
client closes the GPU before the hook obtains its kernel-client device
reference, the hook continues waiting. Acquiring the reference can temporarily
wake the GPU.

## Loading and control commands

Use `load-hook.sh` to check the loaded NVIDIA driver build and pass its GNU build
ID as `expected_nvidia_build_id`. The loader must check the linked module,
kernel-open source and upstream dispatcher source as described below. A plain
module load without the checked build ID is rejected.

The control device, `/dev/nvpcf-hook`, is accessible only to root. A successful
load can leave the hook `waiting`, and attachment attempted later can fail with
`unavailable`. Use `status` to check whether the hook is `active`. Status only
reads the hook's state: it does not attach, wake the GPU or retry failed delivery.
An asynchronous attachment failure reports its state and error in the kernel
log.

From the repository root:

```
sudo ./hook/nvpcf-hookctl status
sudo ./hook/nvpcf-hookctl prepare
sudo ./hook/nvpcf-hookctl detach
sudo ./hook/nvpcf-hookctl resume
sudo ./hook/nvpcf-hookctl acknowledge-loss
```

- `status` reads the state and counters.
- `prepare` deliberately wakes the GPU and attempts to deliver pending
  notifications while retaining the device reference and notification
  monitoring. Run it before a swap changes registration files, stops services
  or revokes client handles. It can fail. In `waiting` and `guard-only`, it can
  succeed while notification monitoring remains inactive.
- `detach` stops capture and resume triggers, waits for redirected calls and the
  hook's worker, and obtains an active Linux runtime-PM reference even if the
  pending count is zero. It flushes the final batch before removing the dispatch
  observer. It then releases the kernel-client device reference, frees the
  NVIDIA stack, releases the Linux runtime-PM reference and drops its NVIDIA
  module reference. While NVIDIA remains loaded, the hook stays `guard-only`.
- `resume` requests a check when attachment or revalidation is needed. After
  detach or device loss, initialization stays blocked until the worker confirms
  that the selected target and its driver are eligible. It then allows ordinary
  initialization and attempts attachment. The command can succeed in `waiting`;
  use `status` to check whether monitoring became active. Failed revalidation
  after system sleep retains the existing device reference and callback data.
- `acknowledge-loss` accepts a recorded loss or shutdown fault after NVIDIA has
  exited and the guards have stopped. It transfers stale requests to
  `acknowledged_loss`. A swap script must not run it automatically.

If the final flush fails, the hook retains pending requests and its device
reference, then tries to restore interception. If that also fails, inspect
`last_error`, the state and the kernel log, then retry `detach`. While
fine-grained runtime PM remains unverified, restored capture stays `quiesced`
and retains the error. `resume` retries validation. The hook reports `active`
only when capture and replay are ready. When interception is absent, new
notifications use NVIDIA's ordinary path.

If hardware fails while the hook releases its device reference, the reference
can already be gone when the failure is detected. The hook then reports
`unavailable` with an error rather than reporting a successful detach.

The hook holds a reference to its own module while it retains device context or
shutdown guards. `rmmod nvpcf_hook` therefore refuses until preparation has
succeeded and NVIDIA has exited. Module exit removes the remaining registrations
and metadata; it does not attempt notification delivery, which could fail.
Forced removal is unsupported.

## Swap ordering and recovery

1. Run `prepare` before modifying registration files, stopping services or
   revoking client handles.
2. Keep the hook's kernel-client device reference while releasing those other
   holders.
3. Run `detach`. It can still fail after the earlier changes; stop and recover
   if it does.
4. Keep the hook loaded in `guard-only` while unloading NVIDIA's dependent
   modules and then `nvidia` itself.
5. After NVIDIA exits and the guards are removed, unload the hook.
6. Load the replacement `nvidia` module. Run `load-hook.sh` to check it and load
   nvpcf_hook, then load `nvidia_modeset`, `nvidia_drm` and `nvidia_uvm` as needed.

If a swap is aborted, `resume` must check eligibility and allow initialization
before a dependent module initializes the GPU. Treat a missing control device as
an I/O error, not a successful detach. Recovery must distinguish an unloaded hook
from a loaded hook whose command failed.

## Device and power references

The kernel-client device reference uses `nvidia_dev_get_uuid` and
`nvidia_dev_put_uuid`. They use NVIDIA's normal open and close count under
`ldata_lock`, without the VFS close path's system-PM read lock or its later
PCI-removal operation. In fine-grained mode, this reference keeps callback data
allocated without keeping the GPU powered. The hook also allocates an opaque
NVIDIA stack for these API calls. Successful stack allocation can return NULL;
`stack_ready` records that allocation separately from the pointer.

NVIDIA warns if its external-kernel-client count update fails, but does not
return that failure to the hook. A successful `nvidia_dev_get_uuid` therefore
confirms the device reference, but not the count update. Run `prepare` and
`detach` before a planned reset or drain. A privileged reset or physical hardware
failure can invalidate the GPU despite the reference.

Delivery uses two separate power references:

1. The hook obtains a Linux runtime-PM reference while the GPU is active, or
   wakes it deliberately for a forced flush.
2. It calls `nvidia_dev_block_gc6` with the UUID held by its kernel-client device
   reference. This acquires NVIDIA's FINE reference and changes NVIDIA's internal
   state from `IDLE_INDICATED` to `IN_USE`. Linux's `ACTIVE` state alone does not
   make that change. The original callback can then dispatch with either the
   stock core or PR #1299's internal deferral.

NVIDIA's UVM code also acquires the device reference and blocks GC6 separately.
The hook blocks GC6 temporarily for delivery, not throughout attachment. It calls
`nvidia_dev_unblock_gc6` before releasing the Linux power reference and suppresses
replay triggers from both releases, so its own power operations do not trigger
another delivery. Ordinary replay does not call `nvidia_dev_block_gc6` while
Linux reports the GPU suspended.

If blocking GC6 fails, requests remain pending and the hook releases the Linux
reference. Every successful block gets an unblock attempt, even when dispatch
was not observed. If UUID lookup fails during unblock, the hook records the
outstanding NVIDIA FINE reference and stops replay in `quiesced`. It retains the
kernel-client device reference, stack and module reference because cleanup is
incomplete. A later explicit flush or revalidation can retry that cleanup once,
even with a pending count of zero. The device reference and pre-unbind
serialization normally keep the UUID lookup valid. If it nevertheless fails,
the hook reports the error and retains the outstanding-reference record. A
completed dispatch remains counted even if later power cleanup fails.

## Initialization and device removal

During setup, the hook obtains the required references and registers its
observer and interception functions while notification capture remains disabled.
It publishes the control device last, then enables capture. Until capture is
enabled, notifications use NVIDIA's ordinary path.

If setup or control-device creation fails, the hook normally undoes setup and
fails to load. It has not captured requests at that point, so it does not need
to flush them. If it cannot unregister a function or safely release a reference,
it remains loaded and logs the error. It tries to publish the control device if
publication has not already failed. If both publication and cleanup fail, the
hook can remain loaded without a control device.

The pre-unbind notifier runs before NVIDIA removes the device from its UUID
lookup list. It serializes with controller operations so that a concurrent
`nvidia_dev_put_uuid` finishes before list deletion. Skipping that wait during a
reference release would let NVIDIA remove the lookup entry before the release
uses it.

PCI notifications cannot prevent removal. If final delivery fails, the hook
stops capture, marks undelivered requests stale, removes the observer and releases
its kernel-client device reference. It clears saved callback arguments before
releasing the reference. Errors and stale requests keep the diagnostic state
until a person runs `acknowledge-loss` after NVIDIA exits. If interception cannot
be unregistered, or cached legacy methods make cleanup unsafe, the hook instead
retains its device reference and reports that removal may wait.

After the selected GPU is removed or a second NVIDIA GPU appears, `resume`
requests another check but keeps GPU initialization blocked. Under the device
and lifecycle locks, the worker checks the selected device, verifies that it is
the only NVIDIA GPU, and checks its fully loaded NVIDIA module and setup
requirements. It allows initialization only after those checks pass. A removed
target or unsupported GPU inventory remains `unavailable`. `waiting` means that
the target is eligible but another client must initialize it; `starting` means
that the check is in progress.

PCI notifications record a loss counter under the private lock. That counter
prevents an older successful check from clearing a newer loss. Replay readiness
also checks the loss marker. Capture can continue counting notifications while
replay is disabled.

Incomplete cleanup sets `retirement_failed` and blocks `resume` and new
attachment. This includes failed unregistration of interception functions and
initialization rollback that cannot safely release resources. Failed old-hook
unregistration also blocks automatic attachment after system PM and a replacement
NVIDIA module that uses the old module's address. Before clearing old NVIDIA
function pointers, the hook disables redirection and waits for redirected calls
to finish. `detach` retries cleanup; `retirement_failed` stays set until cleanup
succeeds. The retry does not clear a separate delivery or shutdown fault or
acknowledge lost requests. Keeping nvpcf_hook loaded does not keep an exited
NVIDIA module's code loaded.

## System suspend and rollback

The highest-priority prepare notifier pauses replay and waits for the hook's
worker before NVIDIA takes its system-PM locks. The hook continues counting
notifications and retains its kernel-client device reference, NVIDIA stack and
NVIDIA module reference through suspend, hibernation and rollback. Its prepare
operation does not allocate memory, acquire power or release these references,
so it cannot introduce a failure that needs notifier rollback.

A post or rollback notification queues work. The worker takes
`lock_system_sleep()` before the device and lifecycle locks, so it waits until
the complete PM sequence has finished, including NVIDIA's post or rollback work.
It checks effective fine-grained runtime PM before enabling replay. If validation
fails, it retains the device reference, callback data and pending requests in
`quiesced`.

A `waiting` hook can attempt attachment. A `guard-only` hook clears its paused
flag without acquiring a device reference. Manual `prepare` and `detach` also
take the system-sleep lock and can clear that flag before the queued worker
runs. Bus removal does not clear the flag or report verified delivery while PM
remains paused.

On the test machine, NVIDIA's enabled sleep services run
`/usr/bin/nvidia-sleep.sh`. With the procfs suspend entry absent, that script
exits before changing virtual terminals or writing procfs. The hook requires
this suspend mode.

## Shutdown and locking

The shutdown guard intercepts entry to `nv_shutdown_adapter`, before NVIDIA
frees interrupts or adapter resources or takes the later RM API and GPU locks.
It first calls NVIDIA's `nv_acpi_methods_uninit`, which removes both global PSR
and NVPCF handlers and waits for running handlers to finish. NVIDIA clears its
handler pointers, so later core cleanup skips the handlers already removed.
Removing only the NVPCF handler would leave the PSR callback, which also takes
the RM API lock.

Before releasing the kernel-client device reference, the hook stops capture,
redirected calls, its worker and the observer. New ACPI calls then run
synchronously through NVIDIA rather than queueing hook work with data that is
about to be freed. A temporary active Linux runtime-PM reference keeps the GPU
active during handler cleanup and resource destruction. If releasing the device
reference does not shut the GPU down, the guard obtains another temporary Linux
runtime-PM reference when a later client triggers shutdown. `guard-only` does
not retain that power reference across NVIDIA's later runtime-PM initialization.

Normal final close holds `ldata_lock`; the first stop during PCI removal can
also hold NVIDIA's Linux-device-list semaphore. NVIDIA's ordinary PSR and NVPCF
dispatch paths use the RM API and GPU locks and existing RM client handles, but
do not acquire those Linux semaphores. This lock analysis covers those paths.
Third-party kernel callbacks need separate checks.

The hook checks for legacy methods during setup and before and after a
successful initialization by another client. When it opens the device for its
kernel-client reference, it checks that result under `ldata_lock` before
incrementing usage. The retained device reference prevents ordinary
reinitialization. In `guard-only`, the hook blocks new initialization. Loading
another ACPI table does not itself populate NVIDIA's cached legacy handles. If
those handles enter an unsupported state, the hook retains its device reference
and diagnostics rather than attempting unsafe cleanup.

The hook's lock order is system-sleep serialization, device lock where needed,
lifecycle mutex, then delivery mutex. Pre-unbind already holds the device lock
and waits for the lifecycle mutex. `nv_shutdown_adapter` does not take that
mutex, including when called while the hook releases its device reference. The
private spinlock is not held while taking the device PM lock or calling RM.
Runtime-PM probes enter with the PM lock held and take only the private spinlock.

Both capture and guard handlers reserve counters before redirecting the
instruction pointer, including the gap before replacement entry. Stopping
interception waits for those reserved calls and the hook's work. Tasks-RCU grace
periods also cover the C epilogues after the counters reach zero.

The guards keep nvpcf_hook loaded until NVIDIA's exit function has finished.
The `MODULE_STATE_GOING` notification runs after exit and before NVIDIA's module
code is freed. The notifier then unregisters the guards and waits for their
redirected calls to finish. The kernel sets NVIDIA's module state to
`MODULE_STATE_GOING` before exit; the hook must wait for the later notification
before removing its guards.

## Reading NVIDIA configuration

Sysfs notes and parameters use `kernel_read`. NVIDIA's GPU `power` and
`information` files register legacy `proc_read` wrappers, so `kernel_read`
rejects them with `EINVAL` on the supported kernels. To read those two GPU files,
the hook uses exported `seq_read_iter` after checking that the file is NVIDIA's
expected `single_open` file. It requires procfs, checks `seq->file` and matches
`seq->op->show` against the checked module's power or information show function.
It does not use a generic `private_data` fallback or pass kernel memory as a
user buffer. Every path closes an opened file, including after a context
mismatch, short read or error.

NVIDIA's checked wrapper opens with `single_open`, reads under
`nv_system_pm_lock` and releases with `single_release`. Every GPU-text read by
the hook holds `lock_system_sleep` and the lifecycle mutex. Before acquiring the
device reference, it also holds the PCI device lock. Afterwards, the reference
keeps the context allocated and pre-unbind waits for the lifecycle mutex. These
conditions prevent removal of the proc entry during a read.

Because the hook requires NVIDIA's kernel suspend notifiers and an absent
procfs suspend control, the system-sleep lock prevents NVIDIA's PM writer from
changing the data during the iterator read. The hook can therefore read through
the iterator without calling the legacy read wrapper. Closing the file still
runs NVIDIA's checked `single_release` wrapper.

`tests/check_nvidia.py` compares the complete open, read and release macro tokens
with `tests/expected_procfs_wrappers.h`. It checks the show-function declarations
and requires the linked show and wrapper symbols and calls to `single_open`,
`seq_read` and `single_release`. The three kernels listed in the build commands
below export `seq_read_iter` with its matching iterator API. Ubuntu kernel
commit `102c85de1d6726aa44823e8f9740eea74216c7a5`, `fs/read_write.c:508-550` and
`fs/proc/inode.c:554-568,658-667`, defines the legacy/iterator distinction.

## Dispatch completion and replay

The original callback is `void(acpi_handle, u32, void *)`. Its data is a non-NULL
allocated `nv_acpi_t *`; that object's `notifier_data` member is NULL for NVPCF.
They are different pointers. Saved arguments are cleared before device release.

NVIDIA's callback can return before dispatch if allocation of API-lock
thread-local state fails. The hook therefore uses a kretprobe to observe
`gpuNotifySubDeviceEvent_IMPL` returning for notification index 177 in the
worker's serialized delivery task.

The checked dispatcher has two early-return assertions: one rejects lockless
ISR context, and the other rejects an invalid index. The ordinary callback
initializes thread state with `THREAD_STATE_FLAGS_NONE`, and index 177 is valid
in all four source versions listed below. Dispatch can complete even if
filtering excludes subscribers or listeners are absent. The counter records
completion of the dispatch loop; it does not confirm receipt by a subscriber.
If the probe misses the return, requests remain pending and a retry can deliver
them twice.

A replay trigger requires `rpm_resume` entry from `SUSPENDED`, followed by an
`rpm_return_int` event that reports `ACTIVE` under the PM lock. Idle and put
events do not enable another retry. Events caused by the hook's delivery and
reference releases are ignored. After a failed dispatch, another notification,
an external resume or an explicit control operation can retry delivery.
Delivery still obtains its own conditional Linux runtime-PM reference before
calling NVIDIA.

The hook resolves target addresses with temporary kprobes qualified by module
name, then passes the exact addresses to `ftrace_set_filter_ip`. It does not use
the exported regular-expression setter because `:mod:` requires a private
trace-array pointer. On 6.8 it adjusts the entry address using the same ENDBR
rule as that kernel's helper; on 7.0 it uses `ftrace_get_symaddr`.

## NVIDIA interfaces and compatibility checks

Attachment checks the device configuration and required symbols and
registrations. The hook uses private kernel-open functions and a symbol in
NVIDIA's precompiled core. Changes to their prototypes, dispatcher
implementation or compiled call boundaries can require a hook update even when
the symbol names remain. The source and ELF checker tests the declarations and
call boundaries described here rather than accepting a list of version numbers.

The device-reference, stack-allocation and guard declarations and their source
implementations were checked in 595.99.02, 610.57.04, 615.71.09 and 615.78.08.
The dispatcher body is identical in those tags; NVPCF is 177 and MAXCOUNT is
199, 200, 202 and 202. Stock-core ELF inspection used Ubuntu's 615.71.09
`nv-kernel.o_binary.ubuntu`. Linked NVIDIA modules for the three kernels listed
below retained the required symbols, entry instrumentation and guard calls.

The source locations below refer to NVIDIA 615.71.09 unless stated otherwise:

- `kernel-open/nvidia/nv.c:1790-1868,2215-2305,5464-5594`: GPU initialization,
  FINE references, device stop and close, and UUID reference acquisition and
  release. UUID acquisition can initialize candidate devices, which is why the
  hook must refuse to initialize one itself.
- `kernel-open/nvidia/nv-modeset-interface.c:33-44,146-177` and
  `kernel-open/common/inc/nv-linux.h:847-872`: linked callbacks that allocate and
  free the opaque NVIDIA stack, including successful allocation that returns
  NULL. Inline cache allocators are not symbols.
- `kernel-open/nvidia/nv_uvm_interface.c:142-196`: UUID device references and
  separate GC6 blocking. The hook does not copy the versioned RM-ops structure.
- `src/nvidia/src/kernel/gpu/gpu_uuid.c:43-75,325-368` and
  `src/nvidia/src/libraries/nvport/string/string_generic.c:269-339`: raw UUID
  bytes are formatted sequentially, high nibble first, in 8/4/4/4/12 hex groups.
  Kernel `uuid_parse`, not GUID byte reordering, reconstructs the raw UUID.
- `kernel-open/nvidia/nv-acpi.c:129-245,317-391` and
  `src/nvidia/arch/nvalloc/unix/src/osapi.c:4590-4653,6179-6211`: PSR and NVPCF
  callbacks, handler ownership, cached methods, cleanup and core API/GPU locks.
- `kernel-open/nvidia/nv.c:2142-2213`: `nv_shutdown_adapter` disables the adapter
  and releases resources, so global handler cleanup must happen first.
- `src/nvidia/src/kernel/rmapi/rmapi.c:682-699` and
  `src/nvidia/src/kernel/gpu/gpu_rmapi.c:624-732`: TLS allocation failure and
  dispatch completion.
- Ubuntu kernel commit `102c85de1d6726aa44823e8f9740eea74216c7a5`,
  `drivers/base/power/runtime.c:934-965`: final Linux runtime status follows the
  NVIDIA resume callback. Return events are not limited to resume completion.
- The same kernel's `drivers/base/dd.c:1338-1363` and
  `kernel/module/main.c:828-847`: pre-unbind runs before driver removal. The
  `MODULE_STATE_GOING` notification runs after exit and before module code is
  freed.
- The same kernel's `kernel/notifier.c:114-125`,
  `kernel/power/suspend.c:591-625` and `kernel/power/main.c:67-82`: robust
  notifier rollback and exported system-sleep serialization.

### Driver build ID

The hook takes a reference to a fully loaded `nvidia` module in
`MODULE_STATE_LIVE`, then reads `/sys/module/nvidia/notes/.note.gnu.build-id`
before calling private functions or registering probes. It checks the note's
header, bounds and four-byte alignment, accepts GNU ld's unpadded final
descriptor, and compares the build-ID bytes with the parameter. It does not use
the optional `struct module` build-ID field.

It repeats the comparison whenever it obtains a device reference, attaches or
revalidates, including a manual `resume` after module replacement. If the note
cannot be read, it returns the read's errno. Malformed or truncated notes return
`EBADMSG`; unsupported sizes return `E2BIG`; a missing GNU descriptor returns
`ENODATA`; and a different ID returns `ESTALE`. The kernel log reports the
expected and actual IDs.

Status reports the cached IDs and `build_id_verified`; reading it does not read
the note again or retry validation. Failed revalidation retains the device
reference and callback data, but does not report active monitoring. To use
another NVIDIA build, reload the hook with that build's checked ID. The
parameter cannot be changed through sysfs. The build ID ties the hook to the
driver build that the loader checked; the source and module checks establish
whether that build has the required interfaces.

## Status fields

- `state`: `starting`, `waiting`, `active`, `quiesced`, `suspended`, `guard-only`,
  `unavailable` or `detached`.
- `pci`, `gpu_uuid`, `nvidia_version`: the selected device and loaded version.
- `expected_nvidia_build_id`, `actual_nvidia_build_id`, `build_id_verified`:
  the required ID, latest successfully parsed note and validation state for the
  current attachment attempt.
- `device_reference`: the NVIDIA kernel-client device reference.
- `stack_ready`: successful NVIDIA stack allocation, which can return NULL.
- `nvidia_reference`: the NVIDIA module reference.
- `power_reference`: the Linux runtime-PM reference.
- `guards_registered`, `initialization_blocked`, `replay_paused`: whether guards
  are registered, GPU initialization is blocked and replay is paused.
- `retirement_failed`: incomplete cleanup that blocks attachment until `detach`
  successfully retries it. This can mean failed unregistration or retained
  resources after initialization rollback.
- `nvidia_power_reference`, `nvidia_power_release_error`: the temporary NVIDIA
  FINE reference and any failed release that still needs cleanup.
- `received_requests`: captured CHANGE calls. Ordinary calls after interception
  stops are not counted.
- `pending_requests`: captured requests not covered by observed dispatch or
  explicit loss acknowledgement.
- `deferred_requests`: captured calls while the GPU was inactive or replay was
  paused or disabled.
- `requests_dispatched`, `dispatch_batches`, `coalesced_requests`: requests
  covered by observed dispatch, completed batches, and the difference between
  the two counts.
- `dispatch_attempts`, `dispatch_failures`: attempts that reached the original
  callback, and attempts without an observed dispatcher return.
- `power_failures`, `delivery_failures`: power or device errors, and their sum
  with dispatch failures. Failing to get a conditional Linux reference because
  the GPU is inactive is ordinary deferral.
- `observer_missed`: kretprobe misses, including unrelated calls that could not
  obtain an instance before filtering.
- `redirected`, `starting_calls`, `opening_calls`, `shutdown_calls`: reserved
  replacement calls that have not yet released their counters.
- `prelock_cleanup_calls`: calls that remove NVIDIA's ACPI handlers before the
  shutdown locks.
- `guard_failures`: shutdown-guard errors.
- `legacy_namespace_blocked`: cached legacy methods that prevent safe cleanup.
- `stale_pending`: pending requests became undeliverable after device loss.
- `acknowledged_loss`: requests explicitly accepted as lost.
- `last_error`: the latest errno.

## Building and tests

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

`check` compiles the hook's shared state and reference-management code and its
lifecycle functions into userspace tests. The control-client tests use ordinary
temporary files. The suite also passes AddressSanitizer and
UndefinedBehaviorSanitizer. Coverage includes:

- Request snapshots, arrivals during dispatch, absent or missed dispatch,
  duplicate retries, PM feedback and loss acknowledgement. State exploration
  tries every sequence drawn from 14 operations through depth five, including
  choices that do not apply and sequences that reach the same state. It checks
  coverage of every operation and seven lifecycle phases, failed restoration
  and recovery after revalidation. A separate matrix checks 64 combinations of
  readiness and errors.
- Device references and stack allocation, including NULL and non-NULL stacks,
  failed acquisition, attachment cleanup, one-time release and UUID reference
  release before concurrent list deletion.
- Control-device publication after setup, publication and rollback failures,
  fully loaded NVIDIA module checks, startup refusal, late ordinary PSR and
  NVPCF callbacks during handler removal, and later cleanup that finds the
  handlers already removed.
- Field parsing and build-ID parsing at every supported descriptor length,
  mixed-case hex input, malformed or truncated headers and notes, name and
  descriptor alignment, duplicate GNU notes and replacement mismatches. GNU ld
  produces real notes at seven lengths, including unpadded one- and three-byte
  descriptors. Tests of the actual hook functions check the live module and its
  note before private symbol resolution, and repeat the check during
  revalidation of a retained device reference.
- Kernel file reads, including the legacy proc-read refusal, iterator reads,
  file and show-function association, short reads, errors and cleanup. Source
  and ELF checks cover the complete NVIDIA wrapper functions used for these
  reads.

The tests also compile the hook's actual activation, restoration, module-exit,
reference, guard, PM, attachment and cleanup-retry functions with mocked kernel
operations. They check failed unregistration followed by module replacement and
manual recovery, including replacement at the same address and retention of
independent faults. Tests of the bus notifier, resume request, preparation and
initialization functions check that target loss or a second GPU blocks startup,
that an eligible but uninitialized GPU remains `waiting`, and that a concurrent
loss invalidates an earlier successful check.

The drain model records when redirection is disabled, when both grace periods
finish, when reserved calls and work finish, and when NVIDIA pointers and
callback arguments are cleared. A version of the function that clears them too
early must fail the test. These tests check the order of the C operations; they
do not simulate progress of RCU in a running kernel. The same test program models
Linux `ACTIVE` and NVIDIA `IN_USE` separately. It checks preparation and detach
with stock and patched cores, deferral while the GPU is inactive, and failure
and recovery of the temporary NVIDIA FINE reference. PM tests retain the device
reference through suspend and notifier rollback, and check restoration after a
failed detach.

The source checker reads archives without extracting or executing their code.
It compares the dispatcher's complete C token sequence with
`tests/expected_dispatcher.c`. Backslash-newline splicing runs before removal of
comments and tokenization. Comments and whitespace are ignored; an unrecognized
code change requires source review. ELF checks require calls to exact symbol
entries, either directly or through PC32/PLT32 relocations on the call
displacement with addend -4. They reject similarly named functions and offsets
that could skip instrumentation. The tests assemble accepted and rejected ELF
inputs and change source conditions, including an assertion that rejects index
177.

The module compiles against the three header targets above. Compiler-name
notices and missing pahole or vmlinux do not prevent module linking; BTF
generation is skipped without vmlinux. `dkms.conf` describes how DKMS builds the
package. These commands do not install DKMS configuration or load modules.

## Tests required before deployment

The userspace tests run shared logic and mocked kernel operations. They do not
load the hook or exercise ACPI, runtime PM or NVIDIA on a GPU.

Before deployment, test both Ubuntu's unpatched driver core and the patched core
on the GPU. A patched core can hide an interception defect. Verify idle
behavior, delivery during an external wake, system suspend and rollback, final
detach and shutdown, and recovery after a failed swap.
