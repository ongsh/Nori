"""Offline user installation of the self-contained zipapp. Never touches project data."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
import zipfile

MARKER = '# Nori managed launcher v1'


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
            return archive.read('NORI_DISTRIBUTION') == b'nori-user-zipapp-v1\n'
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def install(source):
    supported()
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
    payload = source.read_bytes()
    text = '#!/bin/sh\n' + MARKER + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(target)) + ' "$@"\n'
    # Same-filesystem replacement prevents partially written executable files.
    for destination, data, mode in ((target, payload, 0o600), (launcher, text.encode(), 0o700)):
        fd, temporary = tempfile.mkstemp(dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f'Installed: {launcher}\nRun: {launcher} --help')
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
