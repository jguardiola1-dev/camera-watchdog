from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.migrate_shared_references import migrate


class MigrationTests(unittest.TestCase):
    def test_preserves_active_census_images_and_archives_conflicting_legacy_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root/'images'
            old = images/'porche'/'census_references'
            old.mkdir(parents=True)
            for kind in ('day','night'):
                (old/(kind+'.jpg')).write_bytes(('calibrated-'+kind).encode())
                (old.parent/('reference_'+kind+'.jpg')).write_bytes(('old-'+kind).encode())
            (old/'selection.json').write_text('{"origin":"selected"}')
            other = images/'lateral'
            other.mkdir()
            (other/'reference_day.jpg').write_bytes(b'lateral-day')
            report=migrate(images,root/'backup')
            self.assertEqual(len(report),1)
            self.assertFalse(old.exists())
            for kind in ('day','night'):
                self.assertEqual((old.parent/('reference_'+kind+'.jpg')).read_bytes(),('calibrated-'+kind).encode())
            self.assertEqual((other/'reference_day.jpg').read_bytes(),b'lateral-day')
            with zipfile.ZipFile(root/'backup/references-before.zip') as archive:
                self.assertEqual(archive.read('porche/reference_day.jpg'),b'old-day')
                self.assertEqual(archive.read('porche/census_references/day.jpg'),b'calibrated-day')
            self.assertEqual(migrate(images,root/'second-backup'),[])
