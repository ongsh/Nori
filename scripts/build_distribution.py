"""Build reproducible standard-library-only zipapp. Run from any directory."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {'__main__.py': b'from nori.distribution import entry\nraise SystemExit(entry())\n',
             'NORI_DISTRIBUTION': b'nori-user-zipapp-v1\n'}
    for source in sorted((ROOT / 'nori').glob('*.py')):
        files['nori/' + source.name] = source.read_bytes()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name('SHA256SUMS').write_text(f'{checksum}  {output.name}\n')
    print(output)
    return output


if __name__ == '__main__':
    build(ROOT / 'dist/nori.pyz')
