#!/usr/bin/python3
"""Test NVIDIA compatibility checks with synthetic source and assembled ELF."""
from pathlib import Path
import runpy
import subprocess
import tempfile

CHECK = runpy.run_path(str(Path(__file__).with_name('check_nvidia.py')))

SOURCE = Path(__file__).with_name('expected_dispatcher.c').read_text()
NOTIFICATION = '''
#define NV2080_NOTIFIERS_NVPCF_EVENTS (177)
#define NV2080_NOTIFIERS_MAXCOUNT (202)
'''


def must_reject(callback, description):
    try:
        callback()
    except AssertionError:
        return
    raise AssertionError('checker accepted incompatible fixture: ' + description)


def mutate_loop(prefix, suffix=''):
    clean = CHECK['c_text'](SOURCE)
    loop = clean.index('for (i = 0;')
    opening = clean.index('{', loop)
    closing = CHECK['closing_delimiter'](clean, opening, '{', '}')
    return SOURCE[:opening + 1] + prefix + SOURCE[opening + 1:closing] + suffix + SOURCE[closing:]


def test_source():
    check = CHECK['check_dispatch_source']
    assert check(SOURCE, NOTIFICATION) == (177, 202)
    assert check(SOURCE.replace('notifyIndex <', '/* index */ notifyIndex   <'),
                 NOTIFICATION) == (177, 202)
    assert check(SOURCE.replace('NV_ASSERT_OR_RETURN_VOID(',
                                'NV_ASSERT_OR_RETURN_VOID \n ('), NOTIFICATION) == (177, 202)
    fixtures = {
        'NVPCF-excluding assertion': SOURCE.replace(
            'notifyIndex < NV2080_NOTIFIERS_MAXCOUNT',
            'notifyIndex < NV2080_NOTIFIERS_MAXCOUNT && notifyIndex != 177'),
        'reversed ISR assertion': SOURCE.replace(
            '!(pCurThread->flags & THREAD_STATE_FLAGS_IS_ISR_LOCKLESS)',
            '(pCurThread->flags & THREAD_STATE_FLAGS_IS_ISR_LOCKLESS)'),
        'wrong thread-state success condition': SOURCE.replace(
            'NV_OK == threadStateGetCurrent', 'NV_OK != threadStateGetCurrent'),
        'conditional index assertion': SOURCE.replace(
            '    NV_ASSERT_OR_RETURN_VOID(notifyIndex',
            '    if (notifyIndex != 177) NV_ASSERT_OR_RETURN_VOID(notifyIndex'),
        'early return': SOURCE.replace('    for (', '    if (notifyIndex == 177) return;\n    for ('),
        'NVPCF-excluding loop': SOURCE.replace(
            'i < pGpu->numSubdeviceBackReferences;',
            'i < pGpu->numSubdeviceBackReferences && notifyIndex != 177;'),
        'extra returning macro': mutate_loop('\nNEW_RETURN_IF(notifyIndex == 177);\n'),
        'NVPCF break': mutate_loop('\nif (notifyIndex == 177) break;\n'),
        'NVPCF continue': mutate_loop('\nif (notifyIndex == 177) continue;\n'),
        'conditionally guarded notification work': mutate_loop('\nif (notifyIndex != 177) {\n', '\n}\n'),
        'post-loop control flow': SOURCE[:SOURCE.rfind('}')] + 'unexpectedOperation();\n' + SOURCE[SOURCE.rfind('}'):],
    }
    assert CHECK['c_tokens']('x + +y') != CHECK['c_tokens']('x++ + y')
    for name, source in fixtures.items():
        assert source != SOURCE, name
        must_reject(lambda: check(source, NOTIFICATION), name)
    print(f'PASS: source checker accepts equivalent whitespace/comments and rejects {len(fixtures)} incompatible control-flow fixtures')


TARGETS = (
    'nv_open_device', 'nv_start_device', 'nv_shutdown_adapter',
    'nv_acpi_methods_uninit', 'nv_acpi_nvpcf_event', 'nvidia_dev_get_uuid',
    'nvidia_dev_put_uuid', 'nvidia_modeset_rm_ops_alloc_stack',
    'nvidia_modeset_rm_ops_free_stack', 'nv_stop_device', 'nv_close_device',
    'nv_start_device_wrong', '__fentry__',
)
INSTRUMENTED = ('nv_open_device', 'nv_start_device', 'nv_shutdown_adapter', 'nv_acpi_nvpcf_event')


def assembly(destination, prefix=''):
    calls = {
        'nvidia_dev_get_uuid': 'nv_open_device',
        'nvidia_dev_put_uuid': 'nv_close_device',
        'nv_open_device': destination,
        'nv_stop_device': 'nv_shutdown_adapter',
    }
    lines = ['.text']
    for name in TARGETS:
        lines += ['.globl ' + name, '.type ' + name + ', @function', name + ':']
        if name in INSTRUMENTED:
            if prefix:
                lines.append(prefix)
            lines.append('call __fentry__')
        if name in calls:
            lines.append('call ' + calls[name])
        lines += ['ret', '.size ' + name + ', .-' + name]
    lines.append('.section .note.GNU-stack,"",@progbits')
    return '\n'.join(lines) + '\n'


def build(root, name, destination, prefix=''):
    source, obj, linked = (root / (name + suffix) for suffix in ('.s', '.o', '.elf'))
    source.write_text(assembly(destination, prefix))
    subprocess.run(['as', '--64', str(source), '-o', str(obj)], check=True)
    subprocess.run(['ld', '-o', str(linked), '--entry=nvidia_dev_get_uuid', str(obj)], check=True)
    return obj, linked


def test_elf():
    check = CHECK['check_module']
    with tempfile.TemporaryDirectory(prefix='nvpcf-checker-elf-') as tmp:
        root = Path(tmp)
        for name, prefix in (('valid', ''), ('valid-endbr', 'endbr64')):
            for binary in build(root, name, 'nv_start_device', prefix):
                check(binary)
        for name, target, prefix in (
            ('wrong-symbol', 'nv_start_device_wrong', ''),
            ('skip-fentry', 'nv_start_device+5', ''),
            ('non-entry-offset', 'nv_start_device+1', ''),
            ('backward-offset', 'nv_start_device-1', ''),
            ('instrumentation-not-at-entry', 'nv_start_device', 'nop'),
        ):
            for binary in build(root, name, target, prefix):
                must_reject(lambda: check(binary), name + binary.suffix)
    # These textual relocation fixtures also check the relocation's location,
    # which an unrelated adjacent record must not establish.
    match = CHECK['direct_call_to_entry']
    symbols = {'caller': 0, 'target': 16}
    valid = '  0: e8 00 00 00 00 call 5 <caller+0x5>\n  1: R_X86_64_PLT32 target-0x4\n'
    assert match(valid, 'caller', 'target', symbols)
    assert match(valid.replace('PLT32', 'PC32'), 'caller', 'target', symbols)
    assert not match(valid.replace('  1:', '  2:'), 'caller', 'target', symbols)
    assert not match(valid.replace('target-0x4', 'target+0x1'), 'caller', 'target', symbols)
    assert not match(valid.replace('target-0x4', 'target_wrong-0x4'), 'caller', 'target', symbols)
    assert not match('  0: e8 00 00 00 00 call 10 <target>\n'
                     '  1: R_X86_64_64 target-0x4\n', 'caller', 'target', symbols)
    assert not match('  0: e8 00 00 00 00 call 11 <target+0x1>\n', 'caller', 'target', symbols)
    assert not match('  0: e8 00 00 00 00 call 11 <target>\n', 'caller', 'target', symbols)
    print('PASS: ELF checker accepts exact entries/ENDBR and rejects wrong symbols, offsets, misplaced fentry and invalid relocations')


test_source()
test_elf()
