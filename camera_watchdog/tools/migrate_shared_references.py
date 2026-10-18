"""One-time migration, run while Camera Watchdog is stopped. No image learning."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def migrate(images, backup):
    images, backup = Path(images).resolve(), Path(backup).resolve()
    if not images.is_dir() or backup == images or images in backup.parents:
        raise ValueError('Backup must be outside the images directory')
    plan = []
    for camera in sorted(images.iterdir()):
        if not camera.is_dir():
            continue
        if camera.is_symlink() or camera.resolve().parent != images:
            raise ValueError('Unexpected camera path')
        old = camera / 'census_references'
        if not old.exists():
            continue
        if old.is_symlink() or old.resolve().parent != camera.resolve():
            raise ValueError('Unexpected reference path')
        files = [p for p in old.rglob('*') if p.is_file()]
        if any(p.is_symlink() or old.resolve() not in p.resolve().parents for p in files):
            raise ValueError('Unexpected reference file')
        selected = {kind: (old / (kind + '.jpg')).read_bytes()
                    for kind in ('day', 'night') if (old / (kind + '.jpg')).is_file()}
        metadata = (old / 'selection.json').read_bytes() if (old / 'selection.json').is_file() else None
        if metadata is not None:
            json.loads(metadata)
        plan.append((camera, old, files, selected, metadata))
    backup.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(backup / 'references-before.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for camera, old, files, selected, metadata in plan:
            for p in files + [camera / ('reference_' + k + '.jpg') for k in ('day', 'night')] + [camera / 'reference_selection.json']:
                if p.is_file():
                    archive.write(p, str(p.relative_to(images)))
    report = []
    for camera, old, files, selected, metadata in plan:
        entry = {'camera': camera.name, 'references': {}}
        for kind, content in selected.items():
            destination = camera / ('reference_' + kind + '.jpg')
            temporary = destination.with_suffix('.migration.tmp')
            temporary.write_bytes(content)
            temporary.replace(destination)
            assert destination.read_bytes() == content
            entry['references'][kind] = hashlib.sha256(content).hexdigest()
        if metadata is not None:
            (camera / 'reference_selection.json').write_bytes(metadata)
        # The resolved directory was checked to be a direct child of this camera.
        shutil.rmtree(old)
        report.append(entry)
    (backup / 'migration.json').write_text(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', required=True)
    parser.add_argument('--backup', required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.images, args.backup)))
