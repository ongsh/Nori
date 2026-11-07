"""Public-repository bootstrap: immutable revision, verified bundle, no checkout."""
import base64
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BUNDLE_SHA256 = '57db852d2b43dabd766762f0729d67f8f1c3ba22c5fba8420f4b103302ef00d3'
MAX_DOWNLOAD = 2 * 1024 * 1024


def install(revision):
    if not sys.platform.startswith('linux') or sys.version_info < (3, 11):
        raise RuntimeError('Requires Linux/WSL2 and Python 3.11+. Native Windows/macOS are unsupported.')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('Supply an exact trusted 40-character commit SHA, not main or a moving branch.')
    url = f'https://raw.githubusercontent.com/ongsh/Nori/{revision}/distribution/nori.pyz.b64'
    # No token handling, insecure TLS mode, private-repository bypass or fallback.
    with urllib.request.urlopen(url, timeout=30) as response:
        if response.geturl() != url:
            raise RuntimeError('Unexpected download redirect; no package executed.')
        encoded = response.read(MAX_DOWNLOAD + 1)
    if len(encoded) > MAX_DOWNLOAD:
        raise RuntimeError('Package download exceeds size limit.')
    payload = base64.b64decode(encoded.strip(), validate=True)
    if hashlib.sha256(payload).hexdigest() != BUNDLE_SHA256:
        raise RuntimeError('Package checksum mismatch; no package executed.')
    with tempfile.TemporaryDirectory(prefix='nori-install-') as directory:
        package = Path(directory) / 'nori.pyz'
        package.write_bytes(payload)
        subprocess.run([sys.executable, str(package), '--install'], check=True)


def main():
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('Usage: python3 install_online.py EXACT_COMMIT_SHA')
        install(sys.argv[1])
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Nori installation failed: {exc}\nFor a private repository, use the authenticated GitHub Actions artifact instead. No access controls are bypassed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
