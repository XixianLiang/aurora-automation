"""Collect app UI evidence; no aurora prediction or map-value inference."""
import argparse
import json
import re
import sys
import subprocess
import math
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import uiautomator2 as u2

WINDY = 'com.windyty.android'
AURORA = 'com.jrustonapps.myauroraforecast'


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


class Collector:
    def __init__(self, device, output, timeout):
        self.d, self.output, self.timeout = device, output, timeout
        self.output.mkdir(parents=True, exist_ok=True)

    def wait(self, predicate, description):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.5)
        raise RuntimeError(f'Timed out: {description}')

    def click(self, **selector):
        mapping = {'packageName': 'package', 'resourceId': 'resource-id',
                   'text': 'text', 'description': 'content-desc', 'className': 'class'}
        def find():
            for node in ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node'):
                if all(node.get(mapping[key]) == value for key, value in selector.items()):
                    bounds = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
                    if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                        return bounds
            return None
        x1, y1, x2, y2 = self.wait(find, f'UI element {selector}')
        self.d.click((x1 + x2) // 2, (y1 + y2) // 2)
        time.sleep(.5)

    def nodes(self, package):
        return [n for n in ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node')
                if n.get('package') == package]

    def capture(self, name, package):
        xml = self.d.dump_hierarchy(root_in_active=True)
        self.d.screenshot(str(self.output / f'{name}.png'))
        (self.output / f'{name}.xml').write_text(xml, encoding='utf-8')
        return {'captured_at': now(), 'screenshot': f'{name}.png',
                'hierarchy': f'{name}.xml', 'visible_text': [n.get('text')
                for n in ET.fromstring(xml).iter('node')
                if n.get('package') == package and n.get('text')]}

    def start(self, package):
        self.d.screen_on()
        # A simple swipe can dismiss an unsecured lock screen; never enter credentials.
        if self.d.app_current().get('package') in ('com.hihonor.aod', 'com.android.systemui'):
            self.d.swipe(.5, .85, .5, .2)
        self.d.app_start(package, stop=True)
        time.sleep(1)
        if package == AURORA and self.d.app_current().get('package') == 'com.android.vending':
            raise RuntimeError('Aurora redirected to Google Play installer/license check. Inspect installer metadata; the tested install command is documented in README.md.')
        def foreground():
            current = self.d.app_current().get('package')
            if package == AURORA and current == 'com.android.vending':
                raise RuntimeError('Aurora redirected to Google Play installer/license check. Inspect installer metadata; the tested install command is documented in README.md.')
            if current in (package, 'com.google.android.permissioncontroller', 'com.android.permissioncontroller'):
                return True
            # Some emulator builds report the previous task while a permission dialog is active.
            nodes = ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node')
            return any(n.get('package') in (package, 'com.google.android.permissioncontroller',
                       'com.android.permissioncontroller') for n in nodes)
        self.wait(foreground, f'{package} foreground (unlock device if necessary)')

    def prepare_windy(self):
        deadline = time.monotonic() + self.timeout
        step = 0
        while time.monotonic() < deadline:
            nodes = list(ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node'))
            texts = {n.get('text') for n in nodes}
            permission = next((n for n in nodes if n.get('resource-id') in (
                'com.android.permissioncontroller:id/permission_deny_button',
                'com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button')), None)
            if permission is not None:
                self.capture(f'windy-onboarding-{step}', self.d.app_current().get('package'))
                self.click(resourceId=permission.get('resource-id'))
            elif texts & {'Установить единицы', 'Set units'}:
                self.capture(f'windy-onboarding-{step}', WINDY)
                self.click(packageName=WINDY, text='m/s')
                self.click(packageName=WINDY, text='°C')
                self.click(packageName=WINDY, text=next(iter(texts & {'Установить единицы', 'Set units'})))
            elif texts & {'Настроить позже', 'Set up later'}:
                self.capture(f'windy-onboarding-{step}', WINDY)
                self.click(packageName=WINDY, text=next(iter(texts & {'Настроить позже', 'Set up later'})))
            elif any(n.get('resource-id') == 'q' for n in nodes):
                return
            else:
                time.sleep(.5)
                continue
            step += 1
            time.sleep(.5)
        raise RuntimeError('Windy onboarding did not complete; inspect failure screenshot')

    def prepare_aurora(self):
        prefix = AURORA + ':id/'
        deadline = time.monotonic() + self.timeout
        step = 0
        while time.monotonic() < deadline:
            nodes = list(ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node'))
            if any(n.get('package') == 'com.android.vending' for n in nodes):
                raise RuntimeError('Aurora redirected to Google Play. Check installer metadata and startup evidence.')
            ids = {n.get('resource-id') for n in nodes}
            permission_id = next((rid for rid in (
                'com.android.permissioncontroller:id/permission_deny_button',
                'com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button') if rid in ids), None)
            if permission_id:
                self.capture(f'aurora-onboarding-{step}', self.d.app_current().get('package'))
                self.click(resourceId=permission_id)
            elif prefix + 'permission_button' in ids:
                self.capture(f'aurora-onboarding-{step}', AURORA)
                self.click(resourceId=prefix + 'permission_button')
            elif prefix + 'subtitle' in ids:
                return
            else:
                time.sleep(.5)
                continue
            step += 1
            time.sleep(1)
        raise RuntimeError('Aurora onboarding did not complete; inspect failure screenshot')

    def device_environment(self):
        return {
            'configuration': self.d.shell('cmd activity get-config').output.strip(),
            'timezone': self.d.shell('getprop persist.sys.timezone').output.strip(),
            'device_time': self.d.shell('date +%Y-%m-%dT%H:%M:%S%z').output.strip(),
            'aurora_installer': self.d.shell(['pm', 'list', 'packages', '-i', AURORA]).output.strip(),
        }

    def windy(self, query, exact_result=None):
        self.start(WINDY)
        self.prepare_windy()
        self.wait(lambda: self.d(resourceId='q').exists, 'Windy loaded')
        # Close startup/detail/search panels to expose the menu.
        for _ in range(4):
            if self.d(packageName=WINDY, text='d').exists:
                break
            self.d.press('back')
            time.sleep(.5)
        self.click(packageName=WINDY, text='d')
        # Establish a known baseline so repeat runs verify a real layer change.
        def layer_menu():
            descriptions = {n.get('content-desc') for n in self.nodes(WINDY)}
            return descriptions if descriptions & {'Ветер', 'Wind'} else None
        menu_descriptions = self.wait(layer_menu, 'Windy layer menu loaded')
        wind_label = 'Ветер' if 'Ветер' in menu_descriptions else 'Wind'
        cloud_label = 'Облака' if 'Облака' in menu_descriptions else 'Clouds'
        self.click(packageName=WINDY, description=wind_label)
        self.wait(lambda: self.d(resourceId='legend-mobile').get_text().startswith('m/s'),
                  'Wind baseline legend')
        self.click(packageName=WINDY, description=cloud_label)
        self.wait(lambda: not self.d(resourceId='legend-mobile').get_text().startswith('m/s'),
                  'Clouds layer legend changed')
        layer_evidence = self.capture('windy-layer', WINDY)
        self.d.press('back')
        self.wait(lambda: not self.d(resourceId='plugin-menu').exists, 'menu closed')
        time.sleep(1)  # Allow menu close animation before tapping search.
        self.click(packageName=WINDY, text='\ue02c')
        time.sleep(.5)
        self.d(resourceId='q').set_text(query)

        def results():
            for node in self.nodes(WINDY):
                if node.get('resource-id') == 'plugin-search':
                    return [n.get('content-desc') for n in node.iter('node')
                            if n.get('content-desc')
                            and n.get('content-desc')[0].isalpha()
                            and not n.get('content-desc').startswith(('l ', 'Q ', 'O '))
                            and n.get('content-desc') not in (
                                'O All', 'Q Airports', '\ue008 Wx Stations',
                                'l Webcams', '\ue002 Kite, Surf, WS',
                                'O Все', 'Q Аэропорты', '\ue008 Метеостанции',
                                'l Веб-камеры', '\ue002 Кайт, Серф, WS')
                            and not any(
                            child.get('content-desc') for child in n.iter('node') if child is not n)]
            return []

        candidates = self.wait(results, 'Windy search results')
        # Select from location results after excluding search-category tabs.
        selected = exact_result or candidates[0]
        if selected not in candidates:
            raise RuntimeError(f'Windy result not found: {selected}; available: {candidates}')
        search_evidence = self.capture('windy-search', WINDY)
        time.sleep(1)
        # WebView result nodes can be stale in UiObject2; use fresh XML bounds.
        target = next(n for n in self.nodes(WINDY) if n.get('content-desc') == selected)
        x1, y1, x2, y2 = map(int, re.findall(r'\d+', target.get('bounds')))
        self.d.click((x1 + x2) // 2, (y1 + y2) // 2)
        self.wait(lambda: self.d(resourceId='plugin-detail').exists, 'target forecast panel')
        self.wait(lambda: not self.d(resourceId='plugin-search').exists, 'search completed')
        detail_evidence = self.capture('windy-location', WINDY)
        self.d.press('back')
        self.wait(lambda: not self.d(resourceId='plugin-detail').exists, 'target map visible')
        time.sleep(2)
        map_evidence = self.capture('windy', WINDY)
        times = [t for t in map_evidence['visible_text'] if re.fullmatch(r'\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*[AP]M', t)]
        return {'package': WINDY, 'query': query, 'selected_location': selected,
                'search_candidates': candidates, 'layer': 'Clouds', 'layer_ui_label': cloud_label,
                'layer_confirmation': 'Clouds tile clicked; legend changed from Wind baseline',
                'forecast_time_text': times, 'forecast_time_iso': None,
                'forecast_timezone': None, 'cloud_cover_percent': None,
                'evidence': {'layer': layer_evidence, 'search': search_evidence,
                             'location': detail_evidence, 'map': map_evidence}}

    def aurora(self, query, exact_result=None, gps_coordinates=None):
        self.start(AURORA)
        self.prepare_aurora()
        prefix = AURORA + ':id/'
        if gps_coordinates is not None:
            return self.aurora_gps(query, gps_coordinates)
        self.click(resourceId=prefix + 'subtitle')
        self.click(resourceId='android:id/button3')
        field = self.d(resourceId=prefix + 'places_autocomplete')
        if not field.wait(timeout=self.timeout):
            raise RuntimeError('Aurora custom-location field missing')
        self.click(resourceId=prefix + 'places_autocomplete')
        field.set_text(query)

        def results():
            return [n.get('text') for n in self.nodes(AURORA)
                    if n.get('class') == 'android.widget.TextView'
                    and not n.get('resource-id') and n.get('text')
                    and n.get('text') != 'Manually Set Location']

        candidates = self.wait(results, 'Aurora location suggestions')
        selected = exact_result or candidates[0]
        if selected not in candidates:
            raise RuntimeError(f'Aurora result not found: {selected}; available: {candidates}')
        search_evidence = self.capture('aurora-search', AURORA)
        self.click(packageName=AURORA, text=selected, className='android.widget.TextView')
        self.wait(lambda: field.get_text() == selected, 'Aurora suggestion applied')
        self.click(resourceId=prefix + 'action_done')
        self.wait(lambda: self.d(resourceId=prefix + 'subtitle').exists,
                  'Aurora home page')
        city = selected.split(',')[0].strip()
        subtitle = self.wait(lambda: self._aurora_subtitle(prefix, city),
                             'Aurora probability updated for selected city')
        title = self.d(resourceId=prefix + 'title').get_text()
        kp = re.search(r'KP Index\s+(\d+(?:\.\d+)?)', title)
        chance = re.search(r'(\d+(?:\.\d+)?)%', subtitle)
        evidence = self.capture('aurora', AURORA)
        return {'package': AURORA, 'query': query, 'selected_location': selected,
                'search_candidates': candidates, 'kp_index': float(kp[1]) if kp else None,
                'probability_percent': float(chance[1]) if chance else None,
                'forecast_period': 'now' if 'now' in subtitle else None,
                'forecast_time_iso': None, 'raw_title': title, 'raw_subtitle': subtitle,
                'evidence': {'search': search_evidence, 'home': evidence}}

    def aurora_gps(self, query, coordinates):
        if not self.d.serial.startswith('emulator-'):
            raise RuntimeError('GPS injection requires an Android emulator')
        latitude, longitude = coordinates
        for permission in ('ACCESS_COARSE_LOCATION', 'ACCESS_FINE_LOCATION'):
            response = self.d.shell(['pm', 'grant', AURORA, 'android.permission.' + permission])
            if response.exit_code:
                raise RuntimeError(response.output)
        command = ['adb', '-s', self.d.serial, 'emu', 'geo', 'fix', str(longitude), str(latitude)]
        response = subprocess.run(command, check=True, capture_output=True, text=True)
        if 'OK' not in response.stdout or 'KO:' in response.stdout:
            raise RuntimeError('GPS injection failed: ' + response.stdout)
        prefix = AURORA + ':id/'
        # Explicitly select current location, rather than reusing a saved custom city.
        self.click(resourceId=prefix + 'subtitle')
        self.click(resourceId='android:id/button1')
        time.sleep(1)
        if any('You will now receive notifications' in n.get('text', '') for n in self.nodes(AURORA)):
            self.capture('aurora-current-location-confirmation', AURORA)
            self.click(resourceId='android:id/button1')

        def gps_fix():
            dump = self.d.shell('dumpsys location').output
            for provider in ('fused', 'gps'):
                matches = re.findall(r'last location=Location\[' + provider + r' (-?\d+\.\d+),(-?\d+\.\d+)', dump)
                for lat_text, lon_text in matches:
                    lat, lon = float(lat_text), float(lon_text)
                    if abs(lat - latitude) < .0001 and abs(lon - longitude) < .0001:
                        return {'provider': provider, 'latitude': lat, 'longitude': lon}
            return None

        location_proof = self.wait(gps_fix, 'target GPS fix delivered to emulator')
        subtitle = self.wait(lambda: self._aurora_subtitle(prefix, 'current location'),
                             'Aurora probability updated for current GPS location')
        title = self.d(resourceId=prefix + 'title').get_text()
        kp = re.search(r'KP Index\s+(\d+(?:\.\d+)?)', title)
        chance = re.search(r'(\d+(?:\.\d+)?)%', subtitle)
        evidence = self.capture('aurora', AURORA)
        return {'package': AURORA, 'query': query, 'location_mode': 'emulator_gps',
                'selected_location': None, 'requested_coordinates': {
                    'latitude': latitude, 'longitude': longitude},
                'emulator_location_fix': location_proof,
                'location_label': 'current location',
                'kp_index': float(kp[1]) if kp else None,
                'probability_percent': float(chance[1]) if chance else None,
                'forecast_period': 'now', 'forecast_time_iso': None,
                'raw_title': title, 'raw_subtitle': subtitle,
                'evidence': {'home': evidence}}

    def _aurora_subtitle(self, prefix, city):
        value = next((n.get('text', '') for n in self.nodes(AURORA)
                      if n.get('resource-id') == prefix + 'subtitle'), '')
        return value if city.casefold() in value.casefold() and '%' in value else None


def main():
    parser = argparse.ArgumentParser(description='Search Windy/Aurora and save UI evidence.')
    parser.add_argument('--location', default='Murmansk')
    parser.add_argument('--app', choices=['both', 'windy', 'aurora'], default='both')
    parser.add_argument('--serial', help='ADB serial; required when multiple devices are connected')
    parser.add_argument('--output', type=Path, help='Output directory (default: timestamped artifacts folder)')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--windy-result', help='Exact full search-result description to select')
    parser.add_argument('--aurora-gps', nargs=2, type=float, metavar=('LAT', 'LON'),
                        help='Use emulator GPS/current location for Aurora; explicit latitude and longitude')
    parser.add_argument('--aurora-result', help='Exact full suggestion text to select')
    args = parser.parse_args()
    if not args.location.strip() or args.timeout <= 0:
        parser.error('location must be nonempty and timeout must be positive')
    if args.aurora_gps:
        lat, lon = args.aurora_gps
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            parser.error('GPS coordinates must be finite latitude [-90,90] and longitude [-180,180]')
        if args.aurora_result:
            parser.error('--aurora-gps and --aurora-result cannot be combined')
    output = args.output or Path('artifacts') / datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-%f')
    result = {'schema_version': 1, 'started_at': now(), 'requested_location': args.location,
              'status': 'running', 'sources': {}}
    collector = None
    try:
        device = u2.connect(args.serial) if args.serial else u2.connect()
        collector = Collector(device, output, args.timeout)
        result['device_serial'] = device.serial
        result['device_environment'] = collector.device_environment()
        # Collect Aurora first so the final screen stays on Windy's target cloud map.
        jobs = []
        if args.app in ('both', 'aurora'):
            jobs.append(('aurora', lambda query, exact: collector.aurora(query, exact, args.aurora_gps), args.aurora_result))
        if args.app in ('both', 'windy'):
            jobs.append(('windy', collector.windy, args.windy_result))
        errors = {}
        for name, collect, exact_result in jobs:
            print(f'{name}: collecting location UI...', flush=True)
            try:
                source = collect(args.location, exact_result)
                source['status'] = 'success'
                result['sources'][name] = source
            except Exception as error:
                errors[name] = str(error)
                source = {'status': 'error', 'error': str(error)}
                try:
                    source['failure_evidence'] = collector.capture(
                        f'{name}-failure', collector.d.app_current().get('package'))
                except Exception as capture_error:
                    source['failure_capture_error'] = str(capture_error)
                result['sources'][name] = source
        if errors:
            result['errors'] = errors
            result['error'] = '; '.join(f'{name}: {error}' for name, error in errors.items())
            result['status'] = 'partial' if len(errors) < len(jobs) else 'error'
        else:
            result['status'] = 'success'
    except Exception as error:
        result['status'] = 'error'
        result['error'] = str(error)
        if collector:
            try:
                package = collector.d.app_current().get('package')
                result['failure_evidence'] = collector.capture('failure', package)
            except Exception as capture_error:
                result['failure_capture_error'] = str(capture_error)
    finally:
        output.mkdir(parents=True, exist_ok=True)
        result['finished_at'] = now()
        path = output / 'result.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{result['status']}: {path.resolve()}", flush=True)
    if result['status'] != 'success':
        print(result['error'], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
