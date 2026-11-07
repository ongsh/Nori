"""Explicit user-level updates from one curated public source.

Checksums detect corruption, not a compromised upstream repository. No fetched
installer or shell script is run. Releases are immutable and activated on restart.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import io
import zipfile
import os
from pathlib import Path
import re
import tempfile
import urllib.request

from . import __version__

REPOSITORY = 'ongsh/Nori'
BRANCH = 'codex/nori-p1-rebuild-20261107'
MAX_BUNDLE = 8 * 1024 * 1024


def safe(path):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError('Refusing symlinked installation path: ' + str(item))
    return path


def root():
    return safe(Path.home() / '.local/lib/nori')


@contextmanager
def locked(name='update.lock'):
    directory = root()
    if not directory.is_dir():
        raise RuntimeError('Install the Nori zipapp first; source checkouts cannot self-update.')
    path = safe(directory / name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def inherited_run_fd():
    try:
        descriptor = int(os.environ.get('NORI_RUN_LOCK_FD', '-1'))
        if descriptor < 3:
            return None
        path = safe(root() / 'run.lock')
        expected, actual = path.stat(), os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
            return descriptor
    except (OSError, ValueError):
        pass
    return None


@contextmanager
def exclusive_lifecycle():
    path = safe(root() / 'run.lock')
    descriptor = inherited_run_fd()
    opened = descriptor is None
    if opened:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Close all other Nori processes before uninstalling.') from exc
        yield
    finally:
        if opened:
            os.close(descriptor)


def atomic(path, data):
    safe(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state():
    path = safe(root() / 'update-state.json')
    if not path.is_file():
        raise RuntimeError('Reinstall the latest Nori zipapp to enable managed updates.')
    if path.stat().st_size > 16384:
        raise RuntimeError('Invalid installation state.')
    data = json.loads(path.read_text())
    for key in ('current', 'pending', 'previous'):
        if data.get(key) is not None:
            entry = data[key]
            if not isinstance(entry, dict) or not re.fullmatch('[0-9a-f]{64}', entry.get('sha256', '')):
                raise RuntimeError('Invalid installation state.')
            safe(root() / 'releases' / (entry['sha256'] + '.pyz'))
    if not data.get('current'):
        raise RuntimeError('Missing installed release.')
    return data


def state_token(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def save(data):
    atomic(root() / 'update-state.json', json.dumps(data, sort_keys=True).encode())


def running_managed():
    location = str(Path(__file__).absolute())
    base = str(root())
    return inherited_run_fd() is not None and (location.startswith(base + '/releases/') or location.startswith(base + '/nori.pyz/'))


def status():
    result = dict(installed=False, current_version=__version__, update_available=False,
                  pending=False, rollback_available=False)
    if not running_managed() or not safe(root() / 'update-state.json').exists():
        return result
    with locked():
        data = state()
    result.update(installed=True, pending=bool(data.get('pending')),
                  rollback_available=bool(data.get('previous') or data.get('pending')),
                  rollback_token=state_token(data),
                  previous_version=(data.get('previous') or {}).get('version'),
                  pending_version=(data.get('pending') or {}).get('version'),
                  rollback_action='cancel_pending' if data.get('pending') else 'rollback',
                  rollback_target_version=(data['current'] if data.get('pending') else (data.get('previous') or {})).get('version'),
                  active_version=data['current']['version'],
                  next_version=(data.get('pending') or data['current'])['version'])
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Update download redirect rejected.')


def fetch(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'Nori-user-updater', 'Accept': 'application/vnd.github+json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        if response.geturl() != url:
            raise RuntimeError('Update download redirect rejected.')
        content = response.read(limit + 1)
    if len(content) > limit:
        raise RuntimeError('Update download exceeds size limit.')
    return content


def manifest():
    head = json.loads(fetch(f'https://api.github.com/repos/{REPOSITORY}/commits/{BRANCH}', 256 * 1024))['sha']
    if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head):
        raise RuntimeError('Invalid upstream revision.')
    raw = fetch(f'https://raw.githubusercontent.com/{REPOSITORY}/{head}/distribution/update.json', 16384)
    item = json.loads(raw)
    if (item.get('schema') != 1 or not re.fullmatch('[0-9a-f]{40}', str(item.get('commit', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(item.get('bundle_sha256', '')))
            or type(item.get('bundle_size')) is not int or not 0 < item['bundle_size'] <= MAX_BUNDLE
            or not isinstance(item.get('version'), str) or not re.fullmatch(r'[0-9A-Za-z.+_-]{1,64}', item['version'])):
        raise RuntimeError('Invalid curated update manifest.')
    item['manifest_token'] = hashlib.sha256(raw).hexdigest()
    return item


def check():
    result = status()
    item = manifest()
    result.update(latest_version=item['version'], commit=item['commit'], manifest_token=item['manifest_token'], manifest_digest=item['manifest_token'])
    if result['installed']:
        with locked():
            data = state()
        result['update_available'] = item['bundle_sha256'] != (data.get('pending') or data['current'])['sha256']
    return result


def stage_update(expected_commit=None, expected_manifest=None):
    if not expected_commit or not expected_manifest:
        raise RuntimeError('Check for updates first and confirm the displayed release.')
    # No network activity or directory creation for an unmanaged source checkout.
    if not status()['installed']:
        raise RuntimeError('Install the Nori zipapp first.')
    with locked():
        initial_state_token = state_token(state())
    item = manifest()
    if item['commit'] != expected_commit or item['manifest_token'] != expected_manifest:
        raise RuntimeError('The offered release changed. Check again before updating.')
    encoded = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{item['commit']}/distribution/nori.pyz.b64", (MAX_BUNDLE * 4 // 3) + 4096)
    try:
        payload = base64.b64decode(encoded.strip(), validate=True)
    except ValueError as exc:
        raise RuntimeError('Invalid release encoding.') from exc
    if len(payload) != item['bundle_size'] or hashlib.sha256(payload).hexdigest() != item['bundle_sha256']:
        raise RuntimeError('Release integrity check failed; current installation is unchanged.')
    from .distribution import owned_archive
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            with archive.open('NORI_VERSION') as version_file:
                version = version_file.read(128).decode().strip()
            if version != item['version']:
                raise RuntimeError('Manifest version does not match release metadata.')
            with archive.open('NORI_DISTRIBUTION') as marker:
                if marker.read(64) != b'nori-user-zipapp-v1\n':
                    raise RuntimeError('Not a Nori distribution.')
    except (KeyError, zipfile.BadZipFile, UnicodeError) as exc:
        raise RuntimeError('Invalid Nori release metadata.') from exc
    with locked():
        data = state()
        if state_token(data) != initial_state_token:
            raise RuntimeError('Installation state changed during download. Review and confirm again.')
        if data.get('pending') and (data['pending']['sha256'] != item['bundle_sha256'] or data['pending'].get('commit') != item['commit']):
            raise RuntimeError('A different release is already pending. Cancel it before staging another update.')
        if item['bundle_sha256'] == data['current']['sha256'] and not data.get('pending'):
            raise RuntimeError('This release is already active.')
        release = safe(root() / 'releases' / (item['bundle_sha256'] + '.pyz'))
        if not release.exists():
            atomic(release, payload)
        elif hashlib.sha256(release.read_bytes()).hexdigest() != item['bundle_sha256']:
            raise RuntimeError('Existing immutable release failed integrity check.')
        if not owned_archive(release):
            raise RuntimeError('Not a Nori distribution; current installation unchanged.')
        data['pending'] = dict(sha256=item['bundle_sha256'], version=item['version'], commit=item['commit'])
        save(data)
    return dict(status(), staged=True, restart_required=True)


def stage_rollback(expected_state=None):
    if not running_managed():
        raise RuntimeError('Run the installed Nori launcher to roll back.')
    with locked():
        data = state()
        if not expected_state or expected_state != state_token(data):
            raise RuntimeError('Installation state changed. Review the rollback target and confirm again.')
        previous = data.get('previous')
        if data.get('pending'):
            data['pending'] = None
        elif previous:
            release = safe(root() / 'releases' / (previous['sha256'] + '.pyz'))
            if hashlib.sha256(release.read_bytes()).hexdigest() != previous['sha256']:
                raise RuntimeError('Rollback release failed integrity check.')
            data['pending'] = previous
        else:
            raise RuntimeError('No previous release is available.')
        save(data)
    return dict(status(), staged=True, restart_required=True)
