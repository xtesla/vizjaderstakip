"""Offline tests for SIS tab detection and conservative fallback."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_monitor as m

class DummyPage:
    def __init__(self, url):
        self.url = url
        self.wait_called = False
    def wait_for_load_state(self, *args, **kwargs):
        self.wait_called = True

class DummyContext:
    def __init__(self, pages):
        self.pages = pages

class DetectionTests(unittest.TestCase):
    def test_recognizes_start_page_not_only_schedule_page(self):
        p = DummyPage('https://mojeaeh.vizja.pl/vizja-stud-app/ledge/view/stud.StartPage?action=security.authentication.ImapLogin')
        with patch.object(m,'chrome_page_hosts', return_value=[]):
            self.assertIs(m.wait_for_sis(DummyContext([p]), timeout_sec=1), p)
        self.assertTrue(p.wait_called)

    def test_does_not_accept_wrong_domain(self):
        p = DummyPage('https://example.org/vizja-stud-app/ledge/view/stud.StartPage')
        with patch.object(m,'chrome_page_hosts', return_value=[]), patch.object(m.time,'sleep',return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'erişemedi'):
                m.wait_for_sis(DummyContext([p]), timeout_sec=0.01)

if __name__ == '__main__':
    unittest.main()
