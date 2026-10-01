"""Optional live-network test: python smoke_network.py (requires curl + internet)."""
import shutil
import subprocess
import sys
import unittest

import bwrap


class LiveNetworkTest(unittest.TestCase):
    def test_curl_google_com(self):
        curl = shutil.which('curl')
        self.assertIsNotNone(curl, 'curl must be installed')
        result = subprocess.run(
            [sys.executable, bwrap.__file__, '--unshare-all', '--share-net',
             '--info-fd', '999999', '--', curl, '--silent', '--show-error',
             '--max-time', '15', 'google.com'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout, 'curl did not return an HTTP response')


if __name__ == '__main__':
    unittest.main(verbosity=2)
