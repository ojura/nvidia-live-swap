#!/usr/bin/python3 -IB
"""Check reconstruction of Mutter's current monitor configuration.

These tests exercise the conversion without connecting to a desktop. Run
reapply-layout.py --check in the session to validate the result with Mutter.
"""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / 'reapply-layout.py'
spec = importlib.util.spec_from_file_location('reapply_layout', SCRIPT)
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)
PANEL = ('eDP-1', 'Test', 'Panel', '123')
EXTERNAL = ('DP-1', 'Test', 'Monitor', '456')


def mode(name, current=False, preferred=False):
    return (name, 2560, 1600, 240.0, 1.0, [1.0, 2.0],
            {'is-current': current, 'is-preferred': preferred})


def state():
    return [7, [(PANEL, [mode('preferred', preferred=True),
                        mode('current', current=True)], {'is-builtin': True})],
            [(0, 0, 1.0, 0, True, [PANEL], {})],
            {'layout-mode': 2}]


class CurrentLayoutTests(unittest.TestCase):
    def test_current_mode_is_used_instead_of_preferred_mode(self):
        serial, configuration, properties = layout.current_configuration(state())
        self.assertEqual(serial, 7)
        self.assertEqual(configuration, [(0, 0, 1.0, 0, True,
                                          [('eDP-1', 'current', {})])])
        self.assertEqual(properties, {})

    def test_position_scale_transform_and_primary_are_preserved(self):
        value = state()
        value[2] = [(-1920, 120, 2.0, 6, False, [PANEL], {})]
        self.assertEqual(layout.current_configuration(value)[1][0][:5],
                         (-1920, 120, 2.0, 6, False))

    def test_layout_mode_requires_the_capability(self):
        value = state()
        self.assertEqual(layout.current_configuration(value)[2], {})
        value[3]['supports-changing-layout-mode'] = True
        self.assertEqual(layout.current_configuration(value)[2], {'layout-mode': 2})

    def test_underscanning_is_preserved(self):
        value = state()
        value[1][0][2]['is-underscanning'] = True
        physical = layout.current_configuration(value)[1][0][5]
        self.assertEqual(physical[0][2], {'underscanning': True})
        value[1][0][2]['is-underscanning'] = False
        physical = layout.current_configuration(value)[1][0][5]
        self.assertEqual(physical[0][2], {'underscanning': False})

    def test_mirrored_monitors_keep_their_individual_modes(self):
        value = state()
        value[1].append((EXTERNAL, [mode('external-current', current=True)], {}))
        value[2] = [(0, 0, 1.0, 0, True, [PANEL, EXTERNAL], {})]
        physical = layout.current_configuration(value)[1][0][5]
        self.assertEqual(physical, [('DP-1', 'external-current', {}),
                                    ('eDP-1', 'current', {})])

    def test_missing_active_layout_is_rejected(self):
        value = state()
        value[2] = []
        with self.assertRaisesRegex(ValueError, 'active monitor'):
            layout.current_configuration(value)

    def test_missing_monitor_is_rejected(self):
        value = state()
        value[1] = []
        with self.assertRaisesRegex(ValueError, 'missing from the monitor list'):
            layout.current_configuration(value)

    def test_missing_current_mode_is_rejected(self):
        value = state()
        value[1][0][1][1][-1]['is-current'] = False
        with self.assertRaisesRegex(ValueError, 'expected one current mode'):
            layout.current_configuration(value)

    def test_ambiguous_current_mode_is_rejected(self):
        value = state()
        value[1][0][1][0][-1]['is-current'] = True
        with self.assertRaisesRegex(ValueError, 'expected one current mode'):
            layout.current_configuration(value)

    def test_signature_ignores_serial_and_unused_modes(self):
        before = state()
        after = copy.deepcopy(before)
        after[0] += 1
        after[1][0][1].append(mode('another-unused-mode'))
        self.assertEqual(layout.layout_signature(before), layout.layout_signature(after))

    def test_signature_detects_layout_changes(self):
        before = state()
        after = copy.deepcopy(before)
        after[2] = [(0, 0, 2.0, 0, True, [PANEL], {})]
        self.assertNotEqual(layout.layout_signature(before), layout.layout_signature(after))
        after = copy.deepcopy(before)
        after[3]['layout-mode'] = 1
        self.assertNotEqual(layout.layout_signature(before), layout.layout_signature(after))

    def test_variant_uses_the_runtime_api_types(self):
        from gi.repository import GLib
        value = state()
        value[3]['supports-changing-layout-mode'] = True
        value[1][0][2]['is-underscanning'] = False
        serial, configuration, properties = layout.current_configuration(value)
        packed = layout.configuration_variant(GLib, serial, 0, configuration, properties)
        self.assertEqual(packed.get_type_string(), '(uua(iiduba(ssa{sv}))a{sv})')
        self.assertEqual(packed.unpack(), (serial, 0, configuration, properties))

    def test_unknown_arguments_fail_before_connecting(self):
        for args in (['--chek'], ['--check', 'extra']):
            result = subprocess.run([sys.executable, '-I', str(SCRIPT), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
