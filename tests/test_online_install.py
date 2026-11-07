import base64
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import install_online
from scripts.build_distribution import build


class Response(io.BytesIO):
    def geturl(self):
        return 'https://raw.githubusercontent.com/ongsh/Nori/' + 'a'*40 + '/distribution/nori.pyz.b64'


class OnlineInstallTests(unittest.TestCase):
    def test_published_bundle_matches_source(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = build(Path(directory)/'nori.pyz').read_bytes()
            actual = base64.b64decode((Path(__file__).resolve().parents[1]/'distribution/nori.pyz.b64').read_bytes())
            self.assertEqual(expected, actual)
            self.assertEqual(hashlib.sha256(actual).hexdigest(), install_online.BUNDLE_SHA256)

    def test_mock_download_installs_without_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = build(Path(directory)/'download/nori.pyz').read_bytes()
            with patch.dict(os.environ, HOME=directory), patch.object(install_online.urllib.request, 'urlopen', return_value=Response(base64.b64encode(payload))):
                install_online.install('a'*40)
            self.assertTrue((Path(directory)/'.local/bin/nori').exists())

    def test_corruption_does_not_execute(self):
        with patch.object(install_online.urllib.request, 'urlopen', return_value=Response(base64.b64encode(b'bad'))), patch.object(install_online.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                install_online.install('a'*40)
            run.assert_not_called()

    def test_moving_branch_refused_without_network(self):
        with patch.object(install_online.urllib.request, 'urlopen') as fetch:
            with self.assertRaises(RuntimeError):
                install_online.install('main')
            fetch.assert_not_called()
