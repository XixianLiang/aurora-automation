"""Calendar geometry from captured Russian Windy UI (1080px display)."""
import unittest
from xml.etree import ElementTree as ET

from main import windy_calendar_state, find_windy_calendar


def calendar(label='11 PM', today=15, tomorrow=16, following=17):
    return ET.fromstring(f'''<node resource-id="plugin-mobile-calendar">
      <node scrollable="true" bounds="[0,2194][1080,2424]">
        <node text="Четверг {today}" bounds="[133,2220][401,2281]"/>
        <node text="Пятница {tomorrow}" bounds="[553,2220][834,2281]"/>
        <node text="Суббота {following}" bounds="[973,2220][1080,2281]"/>
      </node>
      <node text="{label}" bounds="[433,2110][648,2199]"/>
    </node>''')


class CalendarTests(unittest.TestCase):
    def test_calendar_without_html_ids(self):
        node = calendar()
        node.attrib.pop('resource-id')
        outer = ET.Element('node')
        outer.append(node)
        found = find_windy_calendar(list(outer.iter('node')))
        self.assertIs(found, node)
        self.assertEqual(windy_calendar_state(found, 16)['hour'], 23)

    def test_tonight_in_both_clock_formats(self):
        for label in ('11 PM', '23:00'):
            state = windy_calendar_state(calendar(label), 16)
            self.assertEqual(state['hour'], 23)
            self.assertTrue(state['on_target_day'])
            self.assertAlmostEqual(state['target_x'], 544.25)

    def test_same_hour_on_wrong_date_is_rejected(self):
        self.assertFalse(windy_calendar_state(calendar(), 17)['on_target_day'])

    def test_midnight_and_noon(self):
        self.assertEqual(windy_calendar_state(calendar('12 AM'), 16)['hour'], 0)
        self.assertEqual(windy_calendar_state(calendar('12 PM'), 16)['hour'], 12)

    def test_month_boundary(self):
        self.assertTrue(windy_calendar_state(calendar(today=31, tomorrow=1, following=2), 1)['on_target_day'])

    def test_missing_target_day_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Tomorrow is not visible'):
            windy_calendar_state(calendar(), 18)


if __name__ == '__main__':
    unittest.main()
