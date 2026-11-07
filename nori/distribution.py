"""Offline user installation of the self-contained zipapp. Never touches project data."""
import os
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import zipfile

MARKER = '# Nori managed launcher v1'


# A standalone launcher avoids importing a zipapp before deciding which immutable
# release to execute. The inherited shared lock covers the full process lifetime.
LAUNCHER = r'''import fcntl, hashlib, json, os, pathlib, re, sys, tempfile
root = pathlib.Path.home() / '.local/lib/nori'
def safe(p):
    for q in (p, *p.parents):
        if q.is_symlink(): raise RuntimeError('Symlinked installation path refused')
    return p
def lock(name):
    return os.open(safe(root/name), os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
def validate(entry):
    digest = entry['sha256']
    if not re.fullmatch('[0-9a-f]{64}', digest): raise RuntimeError('Invalid release identity')
    path = safe(root/'releases'/(digest+'.pyz'))
    if path.stat().st_size > 8*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise RuntimeError('Release integrity check failed')
    return path
try:
    safe(root)
    run = lock('run.lock')
    exclusive = True
    try: fcntl.flock(run, fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        exclusive = False
        fcntl.flock(run, fcntl.LOCK_SH)
    update = lock('update.lock')
    fcntl.flock(update, fcntl.LOCK_EX)
    statefile = safe(root/'update-state.json')
    if statefile.stat().st_size > 16384: raise RuntimeError('Invalid installation state')
    state = json.loads(statefile.read_text())
    rollback_only = sys.argv[1:] == ['--rollback']
    if rollback_only:
        if state.get('pending'):
            state['pending'] = None
        elif state.get('previous'):
            validate(state['previous'])
            state['pending'] = state['previous']
        else:
            raise RuntimeError('No previous release is available')
        fd, tmp = tempfile.mkstemp(dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(state, out); out.flush(); os.fsync(out.fileno())
            os.replace(tmp, statefile)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        print('Rollback selection saved. Close all Nori processes and restart the launcher.')
        raise SystemExit(0)
    if exclusive and state.get('pending'):
        try:
            validate(state['pending'])
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            print('Pending update not activated: '+str(exc), file=sys.stderr)
            state['pending'] = None
    if exclusive and state.get('pending'):
        state['previous'], state['current'], state['pending'] = state['current'], state['pending'], None
        fd, tmp = tempfile.mkstemp(dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(state, out); out.flush(); os.fsync(out.fileno())
            os.replace(tmp, statefile)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    path = validate(state['current'])
    os.close(update)
    fcntl.flock(run, fcntl.LOCK_SH)
    os.set_inheritable(run, True)
    os.environ['NORI_RUN_LOCK_FD'] = str(run)
    os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])
except (OSError, ValueError, KeyError, RuntimeError) as exc:
    print('Nori launcher: '+str(exc), file=sys.stderr)
    raise SystemExit(2)
'''

def paths():
    home = Path.home()
    targets = home / '.local/lib/nori/nori.pyz', home / '.local/bin/nori'
    for target in targets:
        for path in (target, *target.parents):
            if path.is_symlink():
                raise RuntimeError(f'Refusing symlinked installation path: {path}')
    return targets


def supported():
    if sys.version_info < (3, 11) or not sys.platform.startswith('linux'):
        raise RuntimeError('Nori requires Linux/WSL2 and Python 3.11+. Native Windows/macOS are unsupported.')


def owned_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open('NORI_DISTRIBUTION') as marker:
                return marker.read(64) == b'nori-user-zipapp-v1\n'
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def install(source):
    supported()
    if os.geteuid() == 0:
        print('Warning: installing for root, not your regular WSL/Linux user. Prefer running without sudo as your normal user.')
    source = Path(source).resolve()
    if not owned_archive(source):
        raise RuntimeError('Install from the built nori.pyz distribution, not a source checkout.')
    target, launcher = paths()
    if target.is_symlink() or (target.exists() and not owned_archive(target)):
        raise RuntimeError(f'Refusing to replace unrelated file: {target}')
    if launcher.is_symlink() or (launcher.exists() and MARKER not in launcher.read_text().splitlines()):
        raise RuntimeError(f'Refusing to replace unrelated command: {launcher}')
    for directory in (target.parent, launcher.parent):
        directory.mkdir(parents=True, exist_ok=True)
    from . import __version__
    from .updater import safe, atomic, locked, save, state
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    releases = safe(target.parent / 'releases')
    releases.mkdir(mode=0o700, exist_ok=True)
    with locked():
        release = safe(releases / (digest + '.pyz'))
        if not release.exists():
            atomic(release, payload)
        elif release.stat().st_size != len(payload) or hashlib.sha256(release.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Existing immutable release failed integrity check; installation unchanged.')
        statepath = safe(target.parent / 'update-state.json')
        if statepath.exists():
            data = state()
            if data.get('pending') and data['pending']['sha256'] != digest:
                raise RuntimeError('A different release is pending. Cancel it before reinstalling.')
            if data['current']['sha256'] != digest:
                data['pending'] = {'sha256': digest, 'version': __version__}
                save(data)
        else:
            previous = None
            if target.exists():
                legacy = target.read_bytes()
                legacy_hash = hashlib.sha256(legacy).hexdigest()
                if legacy_hash != digest:
                    legacy_path = safe(releases / (legacy_hash + '.pyz'))
                    if not legacy_path.exists():
                        atomic(legacy_path, legacy)
                    previous = {'sha256': legacy_hash, 'version': 'legacy'}
            save({'current': {'sha256': digest, 'version': __version__}, 'pending': None, 'previous': previous})
        text = '#!' + sys.executable + '\n' + MARKER + '\n' + LAUNCHER
        # Same-filesystem replacement prevents partially written executable files.
        destinations = [(launcher, text.encode(), 0o700)]
        if not target.exists():
            destinations.insert(0, (target, payload, 0o600))
        for destination, data, mode in destinations:
            fd, temporary = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data); handle.flush(); os.fsync(handle.fileno())
                os.chmod(temporary, mode)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    print('Close all Nori processes before restarting to activate any staged release.')
    print(f'Installed: {launcher}\nOpen the browser interface: {launcher}\nKeep its terminal open while using Nori. In WSL, paste the printed URL into your browser if needed.')
    if str(launcher.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('Optional, for this shell: export PATH="$HOME/.local/bin:$PATH"')
    print('No shell startup files changed. Uninstall: ' + str(launcher) + ' --uninstall')


def uninstall():
    supported()
    target, launcher = paths()
    if launcher.is_symlink() or target.is_symlink() or not owned_archive(target):
        raise RuntimeError('Installation ownership check failed; no files removed.')
    if not launcher.is_file() or MARKER not in launcher.read_text().splitlines():
        raise RuntimeError('Launcher ownership check failed; no files removed.')
    from .updater import safe, locked, state, exclusive_lifecycle
    with exclusive_lifecycle(), locked():
        data = state()
        # Remove only validated managed release files; never recurse into data.
        releases = safe(target.parent / 'releases')
        for release in releases.glob('*.pyz'):
            safe(release)
            if release.stem == hashlib.sha256(release.read_bytes()).hexdigest() and owned_archive(release):
                release.unlink()
        safe(target.parent / 'update-state.json').unlink()
        launcher.unlink()
        target.unlink()
    print('Removed Nori program and launcher. All project/control data retained.')


def entry():
    try:
        if sys.argv[1:] == ['--install']:
            install(sys.argv[0]); return 0
        if sys.argv[1:] == ['--uninstall']:
            uninstall(); return 0
        supported()
        from .__main__ import main
        return main()
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
