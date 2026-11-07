import io
import unittest
from unittest.mock import patch
from nori.__main__ import main


class WebEntryTests(unittest.TestCase):
    def test_no_arguments_start_browser_interface(self):
        with patch('nori.web.serve') as serve:
            self.assertEqual(main([]), 0)
            self.assertTrue(serve.call_args.kwargs['open_browser'])
            self.assertEqual(serve.call_args.kwargs['port'], 8765)

    def test_custom_web_entry(self):
        with patch('nori.web.serve') as serve:
            self.assertEqual(main(['web','--data-dir','/tmp/example','--port','8766','--no-browser']),0)
            serve.assert_called_once_with('/tmp/example',port=8766,open_browser=False)
