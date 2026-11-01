#!/usr/bin/python3 -I
"""Reapply the current GNOME monitor layout to release unused KMS devices.

Run as the desktop user. --check asks Mutter to validate the reconstructed
configuration without applying it. A normal run applies it temporarily and
checks that the active modes, positions, scales and transforms stayed the same.
"""
import argparse
import json
import os
import sys


INTERFACE = 'org.gnome.Mutter.DisplayConfig'
OBJECT = '/org/gnome/Mutter/DisplayConfig'


def current_configuration(state):
    serial, monitors, logical_monitors, properties = state
    if not logical_monitors:
        raise ValueError('The current configuration does not have an active monitor')
    available = {tuple(spec): (modes, props) for spec, modes, props in monitors}
    configuration = []
    for x, y, scale, transform, primary, specs, _ in logical_monitors:
        physical = []
        for spec in specs:
            if tuple(spec) not in available:
                raise ValueError(f'{spec[0]} is missing from the monitor list')
            modes, props = available[tuple(spec)]
            current = [mode[0] for mode in modes if mode[-1].get('is-current', False)]
            if len(current) != 1:
                raise ValueError(f'{spec[0]}: expected one current mode, got {current}')
            monitor_properties = {}
            if 'is-underscanning' in props:
                # Mutter 46's implementation accepts "underscanning" here.
                monitor_properties['underscanning'] = props['is-underscanning']
            physical.append((spec[0], current[0], monitor_properties))
        configuration.append((x, y, scale, transform, primary, sorted(physical)))
    global_properties = {}
    # Mutter rejects this property when the capability is absent, even if
    # its value is identical to the current layout mode.
    if properties.get('supports-changing-layout-mode', False):
        global_properties['layout-mode'] = properties['layout-mode']
    return serial, configuration, global_properties


def layout_signature(state):
    _, configuration, _ = current_configuration(state)
    specs = sorted(tuple(spec) for logical in state[2] for spec in logical[5])
    return state[3].get('layout-mode'), sorted(configuration), specs


def configuration_variant(GLib, serial, method, configuration, properties):
    logical = []
    for x, y, scale, transform, primary, monitors in configuration:
        physical = [(connector, mode, {key: GLib.Variant('b', value)
                                      for key, value in props.items()})
                    for connector, mode, props in monitors]
        logical.append((x, y, scale, transform, primary, physical))
    global_properties = {key: GLib.Variant('u', value) for key, value in properties.items()}
    return GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',
                        (serial, method, logical, global_properties))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='validate the current layout without applying it')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run this as the desktop user, without sudo')

    from gi.repository import Gio, GLib

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def request(method, parameters=None):
            return bus.call_sync(INTERFACE, OBJECT, INTERFACE, method, parameters,
                                 None, Gio.DBusCallFlags.NONE, 15000, None)

        before = request('GetCurrentState').unpack()
        serial, configuration, properties = current_configuration(before)
        print('Current layout: ' + json.dumps({
            'layout-mode': before[3].get('layout-mode'),
            'logical-monitors': configuration}), flush=True)
        request('ApplyMonitorsConfig', configuration_variant(
            GLib, serial, 0, configuration, properties))
        print('Mutter validated the unchanged layout.', flush=True)
        if args.check:
            return 0

        # Keep the original serial so Mutter rejects stale resource information.
        request('ApplyMonitorsConfig', configuration_variant(
            GLib, serial, 1, configuration, properties))
        after = request('GetCurrentState').unpack()
        if layout_signature(before) != layout_signature(after):
            raise ValueError('The display configuration differs after reapplication')
        print('Reapplied the layout temporarily; the display configuration is unchanged.')
        return 0
    except (GLib.Error, ValueError) as error:
        print(f'Could not reapply the layout: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
