import json
import unittest
from http.client import RemoteDisconnected
from types import SimpleNamespace
from unittest.mock import patch, Mock

import uiautomator2 as u2
from main import ReliableDevice


class TransportTests(unittest.TestCase):
    def setUp(self):
        # No device connection or local ADB server is needed for these checks.
        self.device = object.__new__(ReliableDevice)

    @patch('main.time.sleep')
    def test_truncated_read_retried(self, sleep):
        error = json.JSONDecodeError('truncated', '', 0)
        with patch.object(u2.Device, 'jsonrpc_call', side_effect=[error, '<hierarchy/>']) as rpc:
            self.assertEqual(self.device.jsonrpc_call('dumpWindowHierarchy'), '<hierarchy/>')
            self.assertEqual(rpc.call_count, 2)

    @patch('main.time.sleep')
    def test_read_retries_are_bounded(self, sleep):
        with patch.object(u2.Device, 'jsonrpc_call', side_effect=RemoteDisconnected()) as rpc:
            with self.assertRaises(RemoteDisconnected):
                self.device.jsonrpc_call('takeScreenshot')
            self.assertEqual(rpc.call_count, 3)

    def test_non_idempotent_rpc_is_not_replayed(self):
        with patch.object(u2.Device, 'jsonrpc_call', side_effect=RemoteDisconnected()) as rpc:
            with self.assertRaises(RemoteDisconnected):
                self.device.jsonrpc_call('click', [100, 200])
            self.assertEqual(rpc.call_count, 1)

    def test_adb_gestures_use_screen_coordinates(self):
        self.device.window_size = Mock(return_value=(720, 1280))
        self.device.shell = Mock(return_value=SimpleNamespace(exit_code=0, output=''))
        self.device.click(.5, .5)
        self.device.shell.assert_called_with(['input', 'tap', '360', '640'])
        self.device.swipe(.7, .75, .7, .45, duration=.5)
        self.device.shell.assert_called_with(['input', 'swipe', '503', '960', '503', '576', '500'])
        self.device.press('back')
        self.device.shell.assert_called_with(['input', 'keyevent', 'KEYCODE_BACK'])


if __name__ == '__main__':
    unittest.main()
