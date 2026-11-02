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


SPLICE = re.compile(r'\\(?:\r\n|\n|\r)')


def splice_lines(text):
    # C translation phase 2 precedes comments and token recognition. One pass
    # matters: a removed pair must not make another physical line splice again.
    return SPLICE.sub('', text)


def stripped_c_text(text):
    pattern = r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]),
                  text, flags=re.S)


def c_text(text):
    return stripped_c_text(splice_lines(text))


def function(text, name, *, raw=False):
    original = text
    spans = list(SPLICE.finditer(original))
    text = splice_lines(text)
    clean = stripped_c_text(text)
    match = re.search(
        r'(?m)^(?:static\s+)?(?:void|int|bool|NV_STATUS|NvLength)\s+(?:(?:NV_API_CALL|notrace)\s+)?'
        + re.escape(name) + r'\s*\([^;]*?\)\s*\{', clean)
    assert match, f'missing function definition: {name}'
    opening = clean.index('{', match.start())
    depth, end = 1, opening + 1
    while depth:
        depth += (clean[end] == '{') - (clean[end] == '}')
        end += 1
    signature = re.sub(r'\s+', '', clean[match.start():opening])
    removed = 0
    for span in spans:
        if span.start() - removed > match.start():
            break
        removed += span.end() - span.start()
    line = original.count('\n', 0, match.start() + removed) + 1
    return signature, (text if raw else clean)[opening:end], line


def check_kernel_sources(texts):
    expected = {
        'nv_open_device': 'staticintnv_open_device(nv_state_t*nv,nvidia_stack_t*sp)',
        'nv_start_device': 'intnv_start_device(nv_state_t*nv,nvidia_stack_t*sp)',
        'nv_shutdown_adapter': 'voidnv_shutdown_adapter(nvidia_stack_t*sp,nv_state_t*nv,nv_linux_state_t*nvl)',
        'nvidia_dev_get_uuid': 'intnvidia_dev_get_uuid(constNvU8*uuid,nvidia_stack_t*sp)',
        'nvidia_dev_put_uuid': 'voidnvidia_dev_put_uuid(constNvU8*uuid,nvidia_stack_t*sp)',
        'nvidia_dev_block_gc6': 'intnvidia_dev_block_gc6(constNvU8*uuid,nvidia_stack_t*sp)',
        'nvidia_dev_unblock_gc6': 'intnvidia_dev_unblock_gc6(constNvU8*uuid,nvidia_stack_t*sp)',
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
    for name, operation in [('nvidia_dev_block_gc6', 'rm_ref_dynamic_power'),
                            ('nvidia_dev_unblock_gc6', 'rm_unref_dynamic_power')]:
        body = bodies[name]
        assert body.index('find_uuid(') < body.index(operation + '(')
        assert 'NV_DYNAMIC_PM_FINE' in body and 'return -ENODEV' in body
        assert 'up(&nvl->ldata_lock)' in body
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


def closing_delimiter(text, opening, left, right):
    assert text[opening] == left
    depth = 1
    for end in range(opening + 1, len(text)):
        depth += (text[end] == left) - (text[end] == right)
        if depth == 0:
            return end
    raise AssertionError('unbalanced source delimiter')


def compact(text):
    return re.sub(r'\s+', '', text)


def c_tokens(text, *, spliced=False):
    if not spliced:
        text = splice_lines(text)
    token = re.compile(
        r'(?P<skip>\s+|/\*.*?\*/|//[^\n]*)|'
        r'(?:u8|u|U|L)?"(?:\\.|[^"\\])*"|(?:u|U|L)?\'(?:\\.|[^\'\\])*\'|'
        r'[A-Za-z_][A-Za-z_0-9]*|0[xX][0-9a-fA-F]+[uUlL]*|'
        r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eEpP][+-]?\d+)?[uUlLfF]*|'
        r'>>=|<<=|\.\.\.|->|\+\+|--|&&|\|\||<=|>=|==|!=|<<|>>|'
        r'\+=|-=|\*=|/=|%=|&=|\|=|\^=|##|[{}()\[\];,.:?~!%^&*+\-/|<>=#]',
        flags=re.S,
    )
    result, position = [], 0
    while position < len(text):
        match = token.match(text, position)
        assert match, f'unrecognized C token at byte {position}; source review required'
        if match.lastgroup != 'skip':
            result.append(match[0])
        position = match.end()
    return tuple(result)


def check_dispatch_source(text, notification):
    signature, body, _ = function(text, 'gpuNotifySubDeviceEvent_IMPL', raw=True)
    reference = Path(__file__).with_name('expected_dispatcher.c').read_text()
    expected_signature, expected_body, _ = function(reference, 'gpuNotifySubDeviceEvent_IMPL', raw=True)
    assert signature == expected_signature, 'dispatcher declaration changed; source review required'
    assert c_tokens(body, spliced=True) == c_tokens(expected_body, spliced=True), 'unrecognized dispatcher body; source review required'
    index = int(re.search(r'#define\s+NV2080_NOTIFIERS_NVPCF_EVENTS\s+\((\d+)\)', notification)[1])
    maximum = int(re.search(r'#define\s+NV2080_NOTIFIERS_MAXCOUNT\s+\((\d+)\)', notification)[1])
    assert index == 177 and index < maximum
    return index, maximum


def check_sources(texts):
    check_kernel_sources(texts)
    signature, body, line = function(texts['acpi'], 'nv_acpi_nvpcf_event')
    assert signature == 'staticvoidnv_acpi_nvpcf_event(acpi_handlehandle,u32event_type,void*data)'
    assert 'ACPI_NVPCF_EVENT_CHANGE' in body and 'pNvAcpiObject->sp' in body
    assert re.search(r'#define\s+ACPI_NVPCF_EVENT_CHANGE\s+0xC0', texts['acpi'])
    signature, _, _ = function(texts['nv'], 'nv_pmops_runtime_resume')
    assert signature == 'intnv_pmops_runtime_resume(structdevice*dev)'
    index, maximum = check_dispatch_source(texts['dispatch'], texts['notification'])
    _, body, _ = function(texts['osapi'], 'rm_acpi_nvpcf_notify')
    assert 'threadStateInit(&threadState,THREAD_STATE_FLAGS_NONE);' in compact(body)
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


# ELF checks verify exact call entries separately from source declarations.
def direct_call_to_entry(code, caller, target, symbols, entry_only=False):
    lines = code.splitlines()
    entry = symbols[caller]
    # ENDBR is an optional entry prefix, not permission to skip instrumentation.
    endbr = re.search(r'(?m)^\s*' + format(entry, 'x') +
                      r':\s+f3\s+0f\s+1e\s+fa\s+endbr64\s*$', code)
    instrumented_entry = entry + (4 if endbr else 0)
    instruction = re.compile(
        r'^\s*([0-9a-fA-F]+):\s+e8(?:\s+[0-9a-fA-F]{2}){4}\s+'
        r'callq?\s+([0-9a-fA-F]+)\s+<([^>]+)>\s*$')
    relocation = re.compile(
        r'^\s*([0-9a-fA-F]+):\s+(R_\S+)\s+(\S+)\s*$')
    for position, line in enumerate(lines):
        call = instruction.match(line)
        if not call:
            continue
        address, destination = int(call[1], 16), int(call[2], 16)
        if entry_only and address != instrumented_entry:
            continue
        reloc = relocation.match(lines[position + 1]) if position + 1 < len(lines) else None
        if reloc:
            # For E8 disp32, P is call+1 and RIP is call+5. S-4-P+RIP=S.
            # Another addend or a relocation on another byte does not prove S.
            if (reloc[2] in ('R_X86_64_PLT32', 'R_X86_64_PC32') and
                    int(reloc[1], 16) == address + 1 and reloc[3] == target + '-0x4'):
                return True
            continue
        if target in symbols and call[3] == target and destination == symbols[target]:
            return True
    return False


def check_module(name):
    table = subprocess.check_output(['nm', '--defined-only', str(name)], text=True)
    symbols = {match[2]: int(match[1], 16) for match in re.finditer(
        r'(?m)^([0-9a-fA-F]+) [tT] (\S+)$', table)}
    targets = ('nv_open_device', 'nv_start_device', 'nv_shutdown_adapter',
               'nv_acpi_methods_uninit', 'nv_acpi_nvpcf_event', 'nvidia_dev_get_uuid',
               'nvidia_dev_put_uuid', 'nvidia_modeset_rm_ops_alloc_stack',
               'nvidia_modeset_rm_ops_free_stack', 'nv_stop_device', 'nv_close_device',
               'nvidia_dev_block_gc6', 'nvidia_dev_unblock_gc6',
               'rm_ref_dynamic_power', 'rm_unref_dynamic_power')
    for target in targets:
        assert target in symbols, target

    def disassemble(target):
        return subprocess.check_output(
            ['objdump', '-dr', '--disassemble=' + target, str(name)], text=True)

    for target in ('nv_open_device', 'nv_start_device', 'nv_shutdown_adapter', 'nv_acpi_nvpcf_event'):
        assert direct_call_to_entry(disassemble(target), target, '__fentry__', symbols,
                                    entry_only=True), ('missing entry instrumentation', target)
    for caller, callee in [('nvidia_dev_get_uuid', 'nv_open_device'),
                           ('nvidia_dev_put_uuid', 'nv_close_device'),
                           ('nv_open_device', 'nv_start_device'),
                           ('nv_stop_device', 'nv_shutdown_adapter'),
                           ('nvidia_dev_block_gc6', 'rm_ref_dynamic_power'),
                           ('nvidia_dev_unblock_gc6', 'rm_unref_dynamic_power')]:
        assert direct_call_to_entry(disassemble(caller), caller, callee, symbols), (caller, callee)
    print('PASS: linked NVIDIA module has ownership/allocator/guard symbols, entry fentry and exact entry calls:', name)



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
