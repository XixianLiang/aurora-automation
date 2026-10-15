"""Collect app UI evidence; no aurora prediction or map-value inference."""
import argparse
import json
import re
import sys
import math
import time
import traceback
from http.client import IncompleteRead, RemoteDisconnected
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import uiautomator2 as u2

WINDY = 'com.windyty.android'
AURORA = 'com.jrustonapps.myauroraforecast'


class ReliableDevice(u2.Device):
    # Only reads and assignments of the same value may be safely replayed.
    RETRYABLE_RPC = {'dumpWindowHierarchy', 'deviceInfo', 'exist', 'count', 'objInfo',
                     'getText', 'takeScreenshot', 'setText', 'clearTextField',
                     'getClipboard', 'setClipboard', 'wakeUp'}

    def jsonrpc_call(self, method, params=None, timeout=10):
        for attempt in range(3):
            try:
                return super().jsonrpc_call(method, params, timeout)
            except (json.JSONDecodeError, RemoteDisconnected, IncompleteRead, ConnectionError):
                if method not in self.RETRYABLE_RPC or attempt == 2:
                    raise
                print(f'::notice title=UI transport::{method}: retrying interrupted response', flush=True)
                time.sleep(.5)

    def _point(self, x, y):
        width, height = self.window_size()
        return (int(x * width) if isinstance(x, float) and 0 <= x <= 1 else int(x),
                int(y * height) if isinstance(y, float) and 0 <= y <= 1 else int(y))

    def _input(self, *arguments):
        response = self.shell(['input', *map(str, arguments)])
        if response.exit_code:
            raise RuntimeError(response.output)

    def click(self, x, y):
        self._input('tap', *self._point(x, y))

    def swipe(self, fx, fy, tx, ty, duration=.5, steps=None):
        self._input('swipe', *self._point(fx, fy), *self._point(tx, ty), int(duration * 1000))

    def drag(self, sx, sy, ex, ey, duration=.5):
        self._input('draganddrop', *self._point(sx, sy), *self._point(ex, ey), int(duration * 1000))

    def press(self, key, meta=None):
        codes = {'back': 'KEYCODE_BACK', 'home': 'KEYCODE_HOME', 'enter': 'KEYCODE_ENTER'}
        self._input('keyevent', codes.get(key, key))


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def layer_tiles(nodes):
    tiles = {}
    for node in nodes:
        description = node.get('content-desc', '')
        if node.get('clickable') != 'true':
            continue
        for label in ('Ветер', 'Wind', 'Облака', 'Clouds'):
            if description == label or description.endswith(' ' + label):
                tiles[label] = description
    return tiles


def aurora_network_fix(dump, latitude, longitude):
    section = re.search(r'\n    network provider(?: \[mock\])?:\n(.*?)(?=\n    \w+ provider(?: \[mock\])?:|\n  Historical|\Z)', dump, re.S)
    if not section:
        return None
    fix = re.search(r'last location=Location\[network (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)[^\n]*', section[1])
    if not fix or 'mock' not in fix[0]:
        return None
    lat, lon = float(fix[1]), float(fix[2])
    if abs(lat - latitude) < .0001 and abs(lon - longitude) < .0001:
        return {'provider': 'network', 'latitude': lat, 'longitude': lon, 'mock': True,
                'aurora_listener_present': AURORA in section[1]}
    return None


def find_windy_calendar(nodes):
    calendar = next((n for n in nodes if n.get('resource-id') == 'plugin-mobile-calendar'), None)
    if calendar is not None:
        return calendar
    # Hosted WebView builds can omit HTML IDs from the accessibility tree.
    candidates = []
    for node in nodes:
        descendants = list(node.iter('node'))
        dates = sum(bool(re.fullmatch(r'[А-Яа-яЁё]+\s+\d{1,2}', n.get('text', ''))) for n in descendants)
        has_time = any(re.fullmatch(r'\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*[AP]M', n.get('text', '')) for n in descendants)
        if dates >= 2 and has_time and any(n.get('scrollable') == 'true' for n in descendants):
            candidates.append((len(descendants), node))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def windy_calendar_state(calendar, tomorrow):
    def bounds(node):
        return list(map(int, re.findall(r'\d+', node.get('bounds', ''))))

    nodes = list(calendar.iter('node'))
    days = [(int(match[1]), bounds(n)[0]) for n in nodes
            if (match := re.fullmatch(r'[А-Яа-яЁё]+\s+(\d{1,2})', n.get('text', '')))]
    # A heading clipped at the left edge does not identify its midnight position.
    starts = [x for _, x in days if x > 0]
    if len(starts) < 2:
        raise RuntimeError('Not enough visible calendar days to measure the timeline')
    day_width = starts[1] - starts[0]
    midnight = next((x for day, x in days if day == tomorrow and x > 0), None)
    if midnight is None:
        raise RuntimeError('Tomorrow is not visible in the Windy timeline')
    time_node = next((n for n in nodes if re.fullmatch(r'\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*[AP]M', n.get('text', ''))), None)
    if time_node is None:
        raise RuntimeError('Windy selected forecast hour is missing')
    label = time_node.get('text')
    hour = int(re.match(r'\d+', label)[0])
    if label.endswith(('AM', 'PM')):
        hour = hour % 12 + (12 if label.endswith('PM') else 0)
    b = bounds(time_node)
    pointer_x = (b[0] + b[2]) / 2
    row = next(n for n in nodes if n.get('scrollable') == 'true')
    # Aim inside the 23:00 hour's selection area, away from its rounding boundary.
    return {'hour': hour, 'pointer_x': pointer_x, 'target_x': midnight - day_width / 48,
            'on_target_day': midnight - day_width <= pointer_x < midnight,
            'y': bounds(row)[1] + (bounds(row)[3] - bounds(row)[1]) * .2}


class Collector:
    def __init__(self, device, output, timeout):
        self.d, self.output, self.timeout = device, output, timeout
        self.error_dialog_count = 0
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
            nodes = list(ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node'))
            # First boot or a display change can leave a system ANR dialog on top.
            close = next((n for n in nodes if n.get('resource-id') == 'android:id/aerr_close'), None)
            if close is not None and selector.get('resourceId') != 'android:id/aerr_close':
                self.error_dialog_count += 1
                self.capture(f'system-app-error-{self.error_dialog_count}', 'android')
                bounds = list(map(int, re.findall(r'\d+', close.get('bounds', ''))))
                self.d.click((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
                time.sleep(.5)
                return None
            for node in nodes:
                if all(node.get(mapping[key]) == value for key, value in selector.items()):
                    bounds = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
                    if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                        return bounds
            return None
        x1, y1, x2, y2 = self.wait(find, f'UI element {selector}')
        self.d.click((x1 + x2) // 2, (y1 + y2) // 2)
        time.sleep(.5)

    def click_if_present(self, **selector):
        mapping = {'packageName': 'package', 'resourceId': 'resource-id',
                   'text': 'text', 'description': 'content-desc', 'className': 'class'}
        for node in ET.fromstring(self.d.dump_hierarchy(root_in_active=True)).iter('node'):
            if all(node.get(mapping[key]) == value for key, value in selector.items()):
                bounds = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
                if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    self.d.click((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
                    time.sleep(.5)
                    return True
        return False

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
                self.click_if_present(packageName=WINDY, text=next(iter(texts & {'Настроить позже', 'Set up later'})))
            elif any(n.get('package') == WINDY and n.get('class') == 'android.widget.EditText' for n in nodes):
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
        environment = self.device_environment()
        if environment['timezone'] != 'Europe/Moscow' or 'ru-rRU' not in environment['configuration'].split(',')[0]:
            raise RuntimeError('Windy requires Russian as the first language and Europe/Moscow timezone; run scripts/prepare_emulator.py first')
        self.start(WINDY)
        self.prepare_windy()
        self.wait(lambda: self.d(packageName=WINDY, className='android.widget.EditText').exists, 'Windy loaded')
        # Controls appear after the search field; Back during startup can exit Windy.
        self.wait(lambda: any(n.get('text') == 'd' for n in self.nodes(WINDY)),
                  'Windy map controls loaded')
        self.click(packageName=WINDY, text='d')
        # Establish a known baseline so repeat runs verify a real layer change.
        def layer_menu():
            nodes = self.nodes(WINDY)
            denial = next((n.get('text') for n in nodes if n.get('text') in (
                'Не разрешать', "Don't allow", 'Do not allow')), None)
            if denial:
                self.capture('windy-location-permission', WINDY)
                self.click(packageName=WINDY, text=denial)
                self.click(packageName=WINDY, text='d')
                return None
            tiles = layer_tiles(nodes)
            return tiles if tiles.keys() & {'Ветер', 'Wind'} and tiles.keys() & {'Облака', 'Clouds'} else None
        menu_descriptions = self.wait(layer_menu, 'Windy layer menu loaded')
        wind_label = 'Ветер' if 'Ветер' in menu_descriptions else 'Wind'
        cloud_label = 'Облака' if 'Облака' in menu_descriptions else 'Clouds'
        self.click(packageName=WINDY, description=menu_descriptions[wind_label])
        self.wait(lambda: any(n.get('text', '').startswith('m/s') and len(n.get('text', '')) > 5 for n in self.nodes(WINDY)),
                  'Wind baseline legend')
        cloud_description = self.wait(layer_menu, 'Windy cloud tile')[cloud_label]
        cloud_node = next(n for n in self.nodes(WINDY) if n.get('content-desc') == cloud_description)
        cloud_bounds = list(map(int, re.findall(r'\d+', cloud_node.get('bounds'))))
        if (cloud_bounds[1] + cloud_bounds[3]) / 2 > self.d.window_size()[1] * .8:
            # The compact menu fades its bottom row behind the calendar; expose it first.
            self.d.swipe(.7, .75, .7, .45, duration=.5)
            time.sleep(.5)
        self.click(packageName=WINDY, description=cloud_description)
        self.wait(lambda: not any(n.get('text', '').startswith('m/s') and len(n.get('text', '')) > 5 for n in self.nodes(WINDY)),
                  'Clouds layer legend changed')
        layer_evidence = self.capture('windy-layer', WINDY)
        self.d.press('back')
        self.wait(lambda: not layer_tiles(self.nodes(WINDY)), 'menu closed')
        time.sleep(1)  # Allow menu close animation before tapping search.
        self.click(packageName=WINDY, text='\ue02c')
        time.sleep(.5)
        self.d(packageName=WINDY, className='android.widget.EditText').set_text(query)

        def results():
            # HTML IDs are absent in some cloud WebView builds; use result-card semantics.
            return [node.get('content-desc') for node in self.nodes(WINDY)
                    if node.get('content-desc') and node.get('clickable') == 'true'
                    and node.get('class') == 'android.view.View'
                    and node.get('content-desc')[0].isalpha()
                    and not node.get('content-desc').startswith(('l ', 'Q ', 'O '))
                    and len([child for child in node.iter('node') if child.get('text')]) >= 2]

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
        def detail_present():
            nodes = self.nodes(WINDY)
            return any(n.get('resource-id') == 'plugin-detail' or n.get('text') in ('Source:', 'Источник:') for n in nodes)
        self.wait(detail_present, 'target forecast panel')
        self.wait(lambda: not results(), 'search completed')
        detail_evidence = self.capture('windy-location', WINDY)
        # A forecast-model introduction can consume the first Back press.
        for _ in range(3):
            if not detail_present():
                break
            self.d.press('back')
            time.sleep(.5)
        self.wait(lambda: not detail_present(), 'target map visible')
        forecast_time = self.windy_tonight()
        time.sleep(2)
        map_evidence = self.capture('windy', WINDY)
        times = [t for t in map_evidence['visible_text'] if re.fullmatch(r'\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*[AP]M', t)]
        return {'package': WINDY, 'query': query, 'selected_location': selected,
                'search_candidates': candidates, 'layer': 'Clouds', 'layer_ui_label': cloud_label,
                'layer_confirmation': 'Clouds tile clicked; legend changed from Wind baseline',
                'forecast_time_text': times, 'forecast_time_iso': forecast_time,
                'forecast_timezone': 'Europe/Moscow', 'cloud_cover_percent': None,
                'evidence': {'layer': layer_evidence, 'search': search_evidence,
                             'location': detail_evidence, 'map': map_evidence}}

    def windy_tonight(self):
        """Drag the UI calendar to today's 23:00 MSK and verify both day and hour."""
        device_now = datetime.fromisoformat(self.d.shell('date +%Y-%m-%dT%H:%M:%S%z').output.strip())
        target = device_now.astimezone(ZoneInfo('Europe/Moscow')).replace(hour=23, minute=0, second=0, microsecond=0)
        tomorrow = (target + timedelta(days=1)).day
        self.capture('windy-time-before', WINDY)
        for _ in range(8):
            calendar = find_windy_calendar(self.nodes(WINDY))
            if calendar is None:
                raise RuntimeError('Windy timeline is missing')
            state = windy_calendar_state(calendar, tomorrow)
            if state['hour'] == 23 and state['on_target_day']:
                return target.isoformat(timespec='seconds')
            delta = state['target_x'] - state['pointer_x']
            width = self.d.window_size()[0]
            # Android consumes touch slop before the calendar starts scrolling.
            distance = min(abs(delta) + width / 45, width * .65)
            start = width * (.8 if delta > 0 else .2)
            end = start - math.copysign(distance, delta)
            self.d.swipe(start, state['y'], end, state['y'], duration=1)
            time.sleep(1)
        raise RuntimeError('Windy timeline did not reach today 23:00 MSK')

    def aurora(self, query, exact_result=None, gps_coordinates=None):
        try:
            return self._aurora_once(query, exact_result, gps_coordinates)
        except Exception:
            # The ARM translation layer can crash the app once on its first launch.
            if self.d.app_current().get('package') == AURORA:
                raise
            self.capture('aurora-crash-before-retry', self.d.app_current().get('package'))
            print('::notice title=Collection::Aurora left foreground; restarting once', flush=True)
            return self._aurora_once(query, exact_result, gps_coordinates)

    def _aurora_once(self, query, exact_result=None, gps_coordinates=None):
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
            raise RuntimeError('Location injection requires an Android emulator')
        latitude, longitude = coordinates
        enabled = self.d.shell(['cmd', 'location', 'set-location-enabled', 'true'])
        if enabled.exit_code:
            raise RuntimeError(enabled.output)
        for permission in ('ACCESS_COARSE_LOCATION', 'ACCESS_FINE_LOCATION'):
            response = self.d.shell(['pm', 'grant', AURORA, 'android.permission.' + permission])
            if response.exit_code:
                raise RuntimeError(response.output)
        def shell_checked(command):
            response = self.d.shell(command)
            if response.exit_code:
                raise RuntimeError(response.output)

        # Aurora can request only NETWORK_PROVIDER, leaving emulator GPS inactive.
        # Feed both providers explicitly; these are marked mock by Android.
        shell_checked(['appops', 'set', 'com.android.shell', 'android:mock_location', 'allow'])
        for provider in ('gps', 'network'):
            shell_checked(['cmd', 'location', 'providers', 'add-test-provider', provider])
            shell_checked(['cmd', 'location', 'providers', 'set-test-provider-enabled', provider, 'true'])

        def inject_location():
            for provider in ('gps', 'network'):
                shell_checked(['cmd', 'location', 'providers', 'set-test-provider-location', provider,
                               '--location', f'{latitude},{longitude}', '--accuracy', '5'])

        inject_location()
        prefix = AURORA + ':id/'
        # Explicitly select current location, rather than reusing a saved custom city.
        self.click(resourceId=prefix + 'subtitle')
        self.click(resourceId='android:id/button1')
        time.sleep(1)
        if any('You will now receive notifications' in n.get('text', '') for n in self.nodes(AURORA)):
            self.capture('aurora-current-location-confirmation', AURORA)
            self.click_if_present(resourceId='android:id/button1')

        def gps_fix():
            if self.d.app_current().get('package') != AURORA:
                raise RuntimeError('Aurora left foreground while setting current location')
            inject_location()
            time.sleep(.5)
            response = self.d.shell('dumpsys location', timeout=10)
            if response.exit_code:
                raise RuntimeError(response.output)
            dump = response.output
            (self.output / 'aurora-location-service.txt').write_text(dump, encoding='utf-8')
            return aurora_network_fix(dump, latitude, longitude)

        location_proof = self.wait(gps_fix, 'target mock network location installed in emulator')
        subtitle = self.wait(lambda: self._aurora_subtitle(prefix, 'current location'),
                             'Aurora probability updated for current injected location')
        title = self.d(resourceId=prefix + 'title').get_text()
        kp = re.search(r'KP Index\s+(\d+(?:\.\d+)?)', title)
        chance = re.search(r'(\d+(?:\.\d+)?)%', subtitle)
        evidence = self.capture('aurora', AURORA)
        return {'package': AURORA, 'query': query, 'location_mode': 'emulator_test_providers',
                'selected_location': None, 'requested_coordinates': {
                    'latitude': latitude, 'longitude': longitude},
                'emulator_location_fix': location_proof,
                'location_confirmation': 'System network mock coordinates verified; Aurora current location selected. App does not display city or coordinates.',
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
    parser.add_argument('--location', default='Мурманск')
    parser.add_argument('--app', choices=['both', 'windy', 'aurora'], default='both')
    parser.add_argument('--serial', help='ADB serial; required when multiple devices are connected')
    parser.add_argument('--output', type=Path, help='Output directory (default: timestamped artifacts folder)')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--windy-result', help='Exact full search-result description to select')
    parser.add_argument('--aurora-gps', nargs=2, type=float, metavar=('LAT', 'LON'),
                        help='Use emulator GPS/network test providers for Aurora current location; explicit latitude and longitude')
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
        device = ReliableDevice(args.serial or u2.connect().serial)
        collector = Collector(device, output, args.timeout)
        result['device_serial'] = device.serial
        result['device_environment'] = collector.device_environment()
        # Collect Windy first so Aurora failure cannot prevent cloud-map evidence.
        jobs = []
        if args.app in ('both', 'windy'):
            jobs.append(('windy', collector.windy, args.windy_result))
        if args.app in ('both', 'aurora'):
            jobs.append(('aurora', lambda query, exact: collector.aurora(query, exact, args.aurora_gps), args.aurora_result))
        errors = {}
        for name, collect, exact_result in jobs:
            print(f'::notice title=Collection::{name}: collecting location UI', flush=True)
            try:
                source = collect(args.location, exact_result)
                source['status'] = 'success'
                result['sources'][name] = source
                print(f'::notice title=Collection::{name}: success', flush=True)
            except Exception as error:
                (output / f'{name}-traceback.txt').write_text(traceback.format_exc(), encoding='utf-8')
                errors[name] = str(error)
                source = {'status': 'error', 'error': str(error)}
                try:
                    source['failure_evidence'] = collector.capture(
                        f'{name}-failure', collector.d.app_current().get('package'))
                except Exception as capture_error:
                    source['failure_capture_error'] = str(capture_error)
                result['sources'][name] = source
                print(f'::notice title=Collection::{name}: failed; inspect result.json', flush=True)
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
