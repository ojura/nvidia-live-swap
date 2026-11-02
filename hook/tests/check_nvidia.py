#!/usr/bin/python3
"""Read NVIDIA source/ELF evidence without executing or loading driver code."""
import argparse
from pathlib import Path
import re
import struct
import subprocess
import tarfile

PATHS = {
    'acpi': 'kernel-open/nvidia/nv-acpi.c',
    'nv': 'kernel-open/nvidia/nv.c',
    'power': 'src/nvidia/arch/nvalloc/unix/src/dynamic-power.c',
    'osapi': 'src/nvidia/arch/nvalloc/unix/src/osapi.c',
    'rmapi': 'src/nvidia/src/kernel/rmapi/rmapi.c',
    'dispatch': 'src/nvidia/src/kernel/gpu/gpu_rmapi.c',
    'modeset': 'kernel-open/nvidia/nv-modeset-interface.c',
    'linux': 'kernel-open/common/inc/nv-linux.h',
    'uvm': 'kernel-open/nvidia/nv_uvm_interface.c',
    'uuid': 'src/nvidia/src/kernel/gpu/gpu_uuid.c',
    'string': 'src/nvidia/src/libraries/nvport/string/string_generic.c',
    'notification': 'src/common/sdk/nvidia/inc/class/cl2080_notification.h',
}


def c_text(text):
    pattern = r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]),
                  text, flags=re.S)


def function(text, name):
    clean = c_text(text)
    match = re.search(
        r'(?m)^(?:static\s+)?(?:void|int|NV_STATUS|NvLength)\s+(?:NV_API_CALL\s+)?'
        + re.escape(name) + r'\s*\([^;]*?\)\s*\{', clean)
    assert match, f'missing function definition: {name}'
    opening = clean.index('{', match.start())
    depth, end = 1, opening + 1
    while depth:
        depth += (clean[end] == '{') - (clean[end] == '}')
        end += 1
    signature = re.sub(r'\s+', '', clean[match.start():opening])
    return signature, clean[opening:end], text.count('\n', 0, match.start()) + 1


def check_kernel_sources(texts):
    expected = {
        'nv_open_device': 'staticintnv_open_device(nv_state_t*nv,nvidia_stack_t*sp)',
        'nv_start_device': 'intnv_start_device(nv_state_t*nv,nvidia_stack_t*sp)',
        'nv_shutdown_adapter': 'voidnv_shutdown_adapter(nvidia_stack_t*sp,nv_state_t*nv,nv_linux_state_t*nvl)',
        'nvidia_dev_get_uuid': 'intnvidia_dev_get_uuid(constNvU8*uuid,nvidia_stack_t*sp)',
        'nvidia_dev_put_uuid': 'voidnvidia_dev_put_uuid(constNvU8*uuid,nvidia_stack_t*sp)',
    }
    bodies = {}
    for name, expected_signature in expected.items():
        signature, bodies[name], _ = function(texts['nv'], name)
        assert signature == expected_signature, (name, signature)
    body = bodies['nv_open_device']
    assert body.index('nv_start_device(') < body.index('atomic64_inc(')
    body = bodies['nv_start_device']
    assert 'rm_unref_dynamic_power(sp, nv, NV_DYNAMIC_PM_FINE)' in body
    body = bodies['nv_shutdown_adapter']
    assert body.index('rm_disable_adapter(') < body.index('rm_shutdown_adapter(')
    body = bodies['nvidia_dev_get_uuid']
    for term in ('find_uuid_candidate(', 'nv_open_device(', 'nv_get_cached_uuid(',
                 'nv_close_device(', 'rm_set_external_kernel_client_count('):
        assert term in body, term
    body = bodies['nvidia_dev_put_uuid']
    assert body.index('find_uuid(') < body.index('nv_close_device(')
    assert 'pci_stop_and_remove' not in body and 'nv_system_pm_lock' not in body
    signature, body, _ = function(texts['modeset'], 'nvidia_modeset_rm_ops_alloc_stack')
    assert signature == 'staticintnvidia_modeset_rm_ops_alloc_stack(nvidia_stack_t**sp)'
    assert 'return nv_kmem_cache_alloc_stack(sp)' in body
    signature, body, _ = function(texts['modeset'], 'nvidia_modeset_rm_ops_free_stack')
    assert signature == 'staticvoidnvidia_modeset_rm_ops_free_stack(nvidia_stack_t*sp)'
    assert 'sp != NULL' in body and 'nv_kmem_cache_free_stack(sp)' in body
    signature, body, _ = function(texts['acpi'], 'nv_acpi_methods_uninit')
    assert signature == 'voidNV_API_CALLnv_acpi_methods_uninit(void)'
    assert body.index('nv_acpi_powersource_hotplug_event') < body.index('nv_acpi_nvpcf_event')
    assert 'psr_nv_acpi_object = NULL' in body and 'nvpcf_nv_acpi_object = NULL' in body


def check_sources(texts):
    check_kernel_sources(texts)
    signature, body, line = function(texts['acpi'], 'nv_acpi_nvpcf_event')
    assert signature == 'staticvoidnv_acpi_nvpcf_event(acpi_handlehandle,u32event_type,void*data)'
    assert 'ACPI_NVPCF_EVENT_CHANGE' in body and 'pNvAcpiObject->sp' in body
    assert re.search(r'#define\s+ACPI_NVPCF_EVENT_CHANGE\s+0xC0', texts['acpi'])
    signature, _, _ = function(texts['nv'], 'nv_pmops_runtime_resume')
    assert signature == 'intnv_pmops_runtime_resume(structdevice*dev)'
    signature, body, _ = function(texts['dispatch'], 'gpuNotifySubDeviceEvent_IMPL')
    assert signature == ('voidgpuNotifySubDeviceEvent_IMPL(OBJGPU*pGpu,NvU32notifyIndex,'
                         'void*pNotifyParams,NvU32notifyParamsSize,NvV32info32,NvV16info16)')
    assert body.count('NV_ASSERT_OR_RETURN_VOID') == 2
    assert 'THREAD_STATE_FLAGS_IS_ISR_LOCKLESS' in body
    assert 'notifyIndex < NV2080_NOTIFIERS_MAXCOUNT' in body
    assert not re.search(r'\breturn\b', body), 'new explicit dispatcher early return needs inspection'
    for call in ('_gpuFilterSubDeviceEventInfo', 'notifyFillNotifierMemory', 'osEventNotificationWithInfo'):
        assert call in body
    index = int(re.search(r'#define\s+NV2080_NOTIFIERS_NVPCF_EVENTS\s+\((\d+)\)', texts['notification'])[1])
    maximum = int(re.search(r'#define\s+NV2080_NOTIFIERS_MAXCOUNT\s+\((\d+)\)', texts['notification'])[1])
    assert index == 177 and index < maximum
    _, body, _ = function(texts['osapi'], 'rm_acpi_nvpcf_notify')
    for expected in ('THREAD_STATE_FLAGS_NONE', 'rmapiLockAcquire', 'gpumgrGetGpu(0)',
                     'os_ref_dynamic_power', 'gpuNotifySubDeviceEvent', 'os_unref_dynamic_power'):
        assert expected in body
    _, body, _ = function(texts['rmapi'], 'rmapiLockAcquire')
    assert 'tlsEntryAcquire' in body and 'NV_ERR_INSUFFICIENT_RESOURCES' in body
    _, body, _ = function(texts['power'], 'os_ref_dynamic_power')
    assert 'status = nv_indicate_not_idle(nv)' in body
    assert 'nvp->dynamic_power.refcount--' in body
    _, body, _ = function(texts['nv'], 'nv_indicate_not_idle')
    assert 'pm_runtime_get_noresume(dev)' in body and 'pci_bus_type.shutdown(dev)' in body
    assert 'return NV_OK' in body and 'return NV_ERR_NOT_SUPPORTED' in body
    _, body, _ = function(texts['uuid'], 'nvGetUuidString')
    assert '{ 8, 4, 4, 4, 12 }' in body and 'pUuid->uuid, NV_UUID_LEN' in body
    _, body, _ = function(texts['string'], 'portStringBufferToHexGroups')
    assert 'buf += groupSize' in body and 'portStringBufferToHex(' in body
    _, body, _ = function(texts['string'], 'portStringBufferToHex')
    assert '(buf[i/2] & 0xF)' in body and '(buf[i/2] >> 4)' in body
    assert 'nvidia_dev_block_gc6' in texts['uvm']
    assert 'rm_is_altstack_in_use' in texts['linux'] and '*sp = NULL' in texts['linux']
    return f'kernel-client ownership, teardown, UUID, callback/dispatcher declarations and control flow; event index {index} < {maximum}'


# ELF checks intentionally verify call boundaries separately from C declarations.
def check_module(name):
    symbols = subprocess.check_output(['nm', '--defined-only', str(name)], text=True)
    targets = ('nv_open_device', 'nv_start_device', 'nv_shutdown_adapter',
               'nv_acpi_methods_uninit', 'nv_acpi_nvpcf_event', 'nvidia_dev_get_uuid',
               'nvidia_dev_put_uuid', 'nvidia_modeset_rm_ops_alloc_stack',
               'nvidia_modeset_rm_ops_free_stack')
    for target in targets:
        assert re.search(r'(?m)^[0-9a-fA-F]+ [tT] ' + target + r'$', symbols), target
    def disassemble(target):
        return subprocess.check_output(
            ['objdump', '-dr', '--disassemble=' + target, str(name)], text=True)
    for target in ('nv_open_device', 'nv_start_device', 'nv_shutdown_adapter', 'nv_acpi_nvpcf_event'):
        assert '__fentry__' in disassemble(target), target
    for caller, callee in [('nvidia_dev_get_uuid', 'nv_open_device'),
                           ('nvidia_dev_put_uuid', 'nv_close_device'),
                           ('nv_open_device', 'nv_start_device'),
                           ('nv_stop_device', 'nv_shutdown_adapter')]:
        code = disassemble(caller)
        direct = re.search(r'\bcall\b[^\n]*<' + callee + r'[^>]*>', code)
        relocated = re.search(r'\bcall\b[^\n]*\n\s*[0-9a-f]+: R_X86_64_PLT32\s+'
                              + callee + r'-0x4', code)
        assert direct or relocated, (caller, callee)
    print('PASS: linked NVIDIA module has ownership/allocator/guard symbols, fentry and required call boundaries:', name)



def source_tar(name):
    texts = {}
    with tarfile.open(name, 'r:*') as archive:
        for key, relative in PATHS.items():
            matches = [member for member in archive.getmembers()
                       if member.isfile() and member.name.endswith('/' + relative)]
            assert len(matches) == 1, (relative, len(matches))
            texts[key] = archive.extractfile(matches[0]).read().decode()
    return texts


def check_core(name):
    with open(name, 'rb') as stream:
        header = stream.read(20)
    assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', header, 18)[0] == 62
    symbols = subprocess.check_output(['nm', '--defined-only', str(name)], text=True)
    for symbol in ('gpuNotifySubDeviceEvent_IMPL', 'rm_acpi_nvpcf_notify', 'rmapiLockAcquire'):
        assert re.search(r'(?m)^[0-9a-fA-F]+ T ' + symbol + r'$', symbols), symbol
    callback = subprocess.check_output(
        ['objdump', '-dr', '--disassemble=rm_acpi_nvpcf_notify', str(name)], text=True)
    for symbol in ('threadStateInit', 'rmapiLockAcquire', 'gpuNotifySubDeviceEvent_IMPL', 'os_ref_dynamic_power'):
        assert symbol in callback, symbol
    dispatcher = subprocess.check_output(
        ['objdump', '-dr', '--disassemble=gpuNotifySubDeviceEvent_IMPL', str(name)], text=True)
    assert re.search(r'\bret\b|__x86_return_thunk', dispatcher)
    assert '__fentry__' not in dispatcher, 'inspect changed core instrumentation'
    print('PASS: x86-64 core has global text targets, expected callback calls and dispatcher returns')
    print('ELF symbols/call sites do not encode or validate the C prototypes; source checks are separate.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-tar', type=Path)
    parser.add_argument('--core-object', type=Path)
    parser.add_argument('--module-object', type=Path, action='append', default=[])
    parser.add_argument('--kernel-source', type=Path, action='append', default=[])
    args = parser.parse_args()
    if not (args.source_tar or args.core_object or args.module_object or args.kernel_source):
        parser.error('provide source or ELF input')
    if args.source_tar:
        print('PASS:', check_sources(source_tar(args.source_tar)))
    if args.core_object:
        check_core(args.core_object)
    for name in args.module_object:
        check_module(name)
    for directory in args.kernel_source:
        texts = {key: (directory / PATHS[key].removeprefix('kernel-open/')).read_text()
                 for key in ('nv', 'acpi', 'modeset')}
        check_kernel_sources(texts)
        print('PASS: kernel-client and guard source declarations/control flow:', directory)


if __name__ == '__main__':
    main()
