"""Do not accept the emulator's default or an unrelated provider's location."""
import unittest
from main import aurora_network_fix, AURORA


def location_dump(lat=68.9707, lon=33.0827, listener=AURORA, mock='mock'):
    return f'''Location Manager State:
  Location Providers:
    network provider:
      listeners:
        10209/{listener} Request[BALANCED]
      last location=Location[network {lat},{lon} hAcc=5.0 et=+10m {mock}]
    gps provider:
      last location=Location[gps 68.9707,33.0827 hAcc=5.0 mock]
  Historical Aggregate Location Provider Data:
'''


class LocationTests(unittest.TestCase):
    def test_target_network_mock_with_aurora_listener(self):
        fix = aurora_network_fix(location_dump(), 68.9707, 33.0827)
        self.assertEqual(fix['provider'], 'network')
        self.assertTrue(fix['mock'])
        self.assertTrue(fix['aurora_listener_present'])

    def test_default_us_network_is_rejected_even_if_gps_matches(self):
        self.assertIsNone(aurora_network_fix(location_dump(39.237255, -123.150032), 68.9707, 33.0827))

    def test_unrelated_listener_is_rejected(self):
        self.assertIsNone(aurora_network_fix(location_dump(listener='com.google.android.gms'), 68.9707, 33.0827))

    def test_unmarked_location_is_rejected(self):
        self.assertIsNone(aurora_network_fix(location_dump(mock=''), 68.9707, 33.0827))


if __name__ == '__main__':
    unittest.main()
