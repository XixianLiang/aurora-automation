import unittest
from xml.etree.ElementTree import Element
from main import windy_startup_action


def nodes(*texts):
    return [Element('node', text=t) for t in texts]


class StartupTests(unittest.TestCase):
    def test_real_location_explanation_labels(self):
        self.assertEqual(windy_startup_action(nodes('Windy: Карта ветров и прогноз погоды',
            'Для этой функции нужен доступ к местоположению', 'Не разрешать', '\ue91d')), 'deny_location')

    def test_remaining_panel_after_denial(self):
        self.assertEqual(windy_startup_action(nodes('Map', 'm/s03510152030', '\ue91d')), 'close_panel')

    def test_loading_screen_does_not_trigger_back(self):
        self.assertEqual(windy_startup_action(nodes('Map')), 'wait')

    def test_visible_map_menu_is_ready(self):
        self.assertEqual(windy_startup_action(nodes('Map', 'd', '\ue91d')), 'ready')


if __name__ == '__main__':
    unittest.main()
