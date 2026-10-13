"""Configure a freshly booted emulator through Android settings and verify it."""
import argparse
import json
from pathlib import Path
import re
import sys
import time
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Collector
import uiautomator2 as u2


def center(node):
    x1, y1, x2, y2 = map(int, re.findall(r'\d+', node.get('bounds')))
    return (x1 + x2) // 2, (y1 + y2) // 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/emulator-setup'))
    args = parser.parse_args()
    if not args.serial.startswith('emulator-'):
        parser.error('Only emulator serials are accepted')
    device = u2.connect(args.serial)
    collector = Collector(device, args.output, 60)
    result = {'serial': args.serial}

    def shell(*command):
        response = device.shell(list(command))
        if response.exit_code:
            raise RuntimeError(response.output)
        return response.output.strip()

    def russian_first():
        config = shell('cmd', 'activity', 'get-config').splitlines()[0]
        return bool(re.search(r'config:.*?(?:^|-)ru-rRU(?:,|-)', config)) and not bool(re.search(r'config:.*?-en-rUS,ru-rRU', config))

    try:
        shell('settings', 'put', 'global', 'auto_time_zone', '0')
        shell('cmd', 'alarm', 'set-timezone', 'Europe/Minsk')
        shell('settings', 'put', 'system', 'time_12_24', '24')
        if not russian_first():
            shell('am', 'start', '-a', 'android.settings.LOCALE_SETTINGS')
            time.sleep(1)
            collector.capture('languages-before', 'com.android.settings')
            collector.click(resourceId='com.android.settings:id/add_language')
            collector.click(resourceId='android:id/locale_search_menu')
            device(resourceId='android:id/search_src_text').set_text('Russian')
            collector.click(packageName='com.android.settings', text='Русский')
            collector.click(packageName='com.android.settings', text='Россия')
            time.sleep(1)
            collector.capture('languages-added', 'com.android.settings')
            nodes = list(ET.fromstring(device.dump_hierarchy(root_in_active=True)).iter('node'))
            # API 35/36 locale picker presents draggable language rows.
            rows = [node for node in nodes if any(child.get('resource-id') == 'com.android.settings:id/label'
                    for child in list(node)) and any(child.get('resource-id') == 'com.android.settings:id/dragHandle'
                    for child in list(node))]
            russian = next((row for row in rows if any('Русский' in child.get('text', '') for child in row.iter('node'))), None)
            if russian is not None and rows and russian is not rows[0]:
                handle = next(node for node in russian.iter('node') if node.get('resource-id') == 'com.android.settings:id/dragHandle')
                first = next(node for node in rows[0].iter('node') if node.get('resource-id') == 'com.android.settings:id/dragHandle')
                x, y = center(handle)
                target_x, target_y = center(first)
                device.drag(x, y, target_x, target_y - 40, duration=1)
                time.sleep(1)
            # Newer Settings versions ask to confirm a system language change.
            nodes = list(ET.fromstring(device.dump_hierarchy(root_in_active=True)).iter('node'))
            if any(node.get('text') in ('Change', 'Изменить') for node in nodes):
                label = next(node.get('text') for node in nodes if node.get('text') in ('Change', 'Изменить'))
                collector.click(packageName='com.android.settings', text=label)
            collector.wait(russian_first, 'ru-RU first system locale')
            collector.capture('languages-after', 'com.android.settings')
        config = shell('cmd', 'activity', 'get-config')
        timezone = shell('getprop', 'persist.sys.timezone')
        offset = shell('date', '+%z')
        if timezone != 'Europe/Minsk' or offset != '+0300':
            raise RuntimeError(f'Unexpected timezone: {timezone} / {offset}')
        result.update(status='success', configuration=config, timezone=timezone, utc_offset=offset,
                      supported_abis=shell('getprop', 'ro.product.cpu.abilist'),
                      native_bridge=shell('getprop', 'ro.dalvik.vm.native.bridge'))
        device.press('home')
    except Exception as error:
        result.update(status='error', error=str(error))
        try:
            result['failure_evidence'] = collector.capture('failure', device.app_current().get('package'))
        except Exception as capture_error:
            result['capture_error'] = str(capture_error)
    finally:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'environment.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
