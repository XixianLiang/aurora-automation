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
        shell('wm', 'size', '720x1280')
        shell('wm', 'density', '320')
        shell('settings', 'put', 'global', 'auto_time_zone', '0')
        shell('cmd', 'alarm', 'set-timezone', 'Europe/Moscow')
        shell('settings', 'put', 'system', 'time_12_24', '24')
        shell('cmd', 'location', 'set-location-enabled', 'true')
        if not russian_first():
            shell('am', 'start', '-a', 'android.settings.LOCALE_SETTINGS')
            time.sleep(1)
            collector.capture('languages-before', 'com.android.settings')
            if not any('Русский' in n.get('text', '') for n in collector.nodes('com.android.settings')):
                collector.click(resourceId='com.android.settings:id/add_language')
                collector.click(resourceId='android:id/locale_search_menu')
                device(resourceId='android:id/search_src_text').set_text('Russian')
                collector.click(packageName='com.android.settings', text='Русский')
                collector.click(packageName='com.android.settings', text='Россия')
            time.sleep(1)
            collector.capture('languages-added', 'com.android.settings')
            # On a compact display, the Russian row/handle can start below the fold.
            for _ in range(3):
                nodes = collector.nodes('com.android.settings')
                handles = [n for n in nodes if n.get('resource-id') == 'com.android.settings:id/dragHandle'
                           and int(re.findall(r'\d+', n.get('bounds'))[3]) - int(re.findall(r'\d+', n.get('bounds'))[1]) > 20]
                if len(handles) >= 2:
                    break
                device.swipe(.5, .8, .5, .4, duration=.5)
            nodes = list(ET.fromstring(device.dump_hierarchy(root_in_active=True)).iter('node'))
            # API 35/36 locale picker presents draggable language rows.
            rows = [node for node in nodes if any(child.get('resource-id') == 'com.android.settings:id/label'
                    for child in node.iter('node')) and any(child.get('resource-id') == 'com.android.settings:id/dragHandle'
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
        print('::notice title=Emulator setup::Russian locale and Moscow timezone configured', flush=True)
        # The factory Play Store can be too old for this APK's background license service.
        # Opening it triggers its normal initialization/update; no account is entered.
        device.app_start('com.android.vending')
        time.sleep(2)
        # Play defers self-update installation while its store UI is foreground.
        device.press('home')
        service = ''
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            service = shell('cmd', 'package', 'query-services', '--brief', '--components',
                            '-a', 'com.android.vending.licensing.IBackgroundLicensingService',
                            '-p', 'com.android.vending')
            if 'BackgroundLicensingService' in service:
                break
            print('Waiting for Google Play background licensing service...', flush=True)
            time.sleep(5)
        else:
            raise RuntimeError('Google Play background licensing service did not become available')
        # Give first-boot package updates time to finish before native app startup.
        print('::notice title=Emulator setup::Google Play licensing service ready; settling package updates', flush=True)
        time.sleep(30)
        collector.capture('play-service-ready', 'com.android.vending')
        config = shell('cmd', 'activity', 'get-config')
        timezone = shell('getprop', 'persist.sys.timezone')
        offset = shell('date', '+%z')
        if timezone != 'Europe/Moscow' or offset != '+0300':
            raise RuntimeError(f'Unexpected timezone: {timezone} / {offset}')
        result.update(status='success', configuration=config, timezone=timezone, utc_offset=offset,
                      supported_abis=shell('getprop', 'ro.product.cpu.abilist'),
                      native_bridge=shell('getprop', 'ro.dalvik.vm.native.bridge'),
                      location_enabled=shell('cmd', 'location', 'is-location-enabled'),
                      play_license_service=service)
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
