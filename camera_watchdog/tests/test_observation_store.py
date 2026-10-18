import json
from pathlib import Path
import tempfile
import time
import unittest

from observation_store import ObservationStore

ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "reference_day.jpg"
        self.image.write_bytes(b"x" * 2000)
        self.now = time.time()

    def store(self, **kw):
        return ObservationStore(self.root / "observations", min_free_bytes=0, **kw)

    def test_metrics_every_capture_and_images_every_minute(self):
        store = self.store()
        a = store.record("porche", {"connection": "online"}, self.image, self.now)
        b = store.record("porche", {"connection": "online"}, self.image, self.now + 30)
        c = store.record("porche", {"connection": "online"}, self.image, self.now + 61)
        self.assertTrue(a["image_saved"])
        self.assertFalse(b["image_saved"])
        self.assertTrue(c["image_saved"])
        records = [json.loads(line) for p in store.root.rglob("*.jsonl") for line in p.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(not r["alarms_published"] for r in records))

    def test_storage_budget_and_references_preserved(self):
        store = self.store(max_bytes=4500, snapshot_seconds=1)
        for i in range(20):
            store.record("porche", {"orb": {"status": "stable"}}, self.image, self.now + i * 2)
        used = sum(p.stat().st_size for p in store.root.rglob("*") if p.is_file())
        self.assertLessEqual(used, 4500)
        self.assertEqual(self.image.read_bytes(), b"x" * 2000)

    def test_retention_removes_only_generated_files(self):
        store = self.store(retention_days=1)
        store.record("porche", {}, self.image, self.now)
        keep = store.root / "notes.txt"
        keep.write_text("keep")
        store.record("porche", {}, self.image, self.now + 90000)
        self.assertEqual(len(list(store.root.rglob("*.jpg"))), 1)
        self.assertEqual(keep.read_text(), "keep")

    def test_offline_observation_has_no_image(self):
        store = self.store()
        r = store.record("principal", {"connection": "offline"}, now=self.now)
        self.assertFalse(r["image_saved"])
        self.assertEqual(len(list(store.root.rglob("*.jsonl"))), 1)

    def test_event_evidence_shares_the_storage_budget(self):
        store = self.store(max_bytes=6000)
        result = store.record("porche", {"census": {"status": "scene_change"}},
                              now=self.now, extra_images={"census_before": b"a" * 2000,
                                                         "census_first_change": b"b" * 2000})
        self.assertEqual(result["evidence_saved"], 2)
        record = json.loads(next(store.root.rglob("*.jsonl")).read_text())
        self.assertEqual(len(record["evidence"]), 2)
        self.assertLessEqual(sum(p.stat().st_size for p in store.root.rglob('*') if p.is_file()), 6000)

    def test_paused_history_keeps_metrics_but_writes_no_jpegs(self):
        store = self.store()
        r = store.record('porche', {}, self.image, self.now,
                         extra_images={'census_before': b'evidence'}, images_enabled=False)
        self.assertFalse(r['image_saved'])
        self.assertEqual(r['evidence_saved'], 0)
        self.assertFalse(list(store.root.rglob('*.jpg')))
        record = json.loads(next(store.root.rglob('*.jsonl')).read_text())
        self.assertEqual(record['image_skipped'], 'history_paused')
        self.assertIsNone(record['image'])
        self.assertEqual(record['evidence'], {})
        resumed = store.record('porche', {}, self.image, self.now + 1)
        self.assertTrue(resumed['image_saved'])

    def test_clear_removes_only_samples_preserves_metrics_and_resets_accounting(self):
        store = self.store()
        store.record('porche', {}, self.image, self.now, extra_images={'before': b'evidence'})
        journal = next(store.root.rglob('*.jsonl'))
        journal_bytes = journal.read_bytes()
        keep = journal.parent / 'reference_night.jpg'
        keep.write_bytes(b'keep')
        self.assertEqual(store.stats()['image_count'], 2)
        result = store.clear_images()
        self.assertEqual(result['images_removed'], 2)
        self.assertEqual(result['bytes_freed'], 2008)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual(keep.read_bytes(), b'keep')
        self.assertEqual(self.image.read_bytes(), b'x' * 2000)
        self.assertEqual(store.stats()['image_bytes'], 0)
        self.assertEqual(store.stats()['diagnostic_bytes'], len(journal_bytes))
        self.assertEqual(store.clear_images()['images_removed'], 0)
        self.assertTrue(store.record('porche', {}, self.image, self.now + 1)['image_saved'])

    def test_clear_cannot_follow_an_hour_directory_symlink(self):
        store = self.store()
        outside = self.root / 'outside'
        outside.mkdir()
        protected = outside / 'sample-secret.jpg'
        protected.write_bytes(b'keep')
        try:
            (store.root / 'hour-external').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        self.assertEqual(store.clear_images()['images_removed'], 0)
        self.assertEqual(protected.read_bytes(), b'keep')

    def test_clear_serializes_with_concurrent_recorders(self):
        from concurrent.futures import ThreadPoolExecutor
        store = self.store(snapshot_seconds=0)
        def operation(index):
            if index % 3 == 0:
                return store.clear_images()
            return store.record('porche', {}, self.image, self.now + index)
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(operation, range(30)))
        actual = list(store.root.glob('hour-*/sample-*.jpg'))
        self.assertEqual(store.stats()['image_count'], len(actual))
        self.assertEqual(store.stats()['image_bytes'], sum(p.stat().st_size for p in actual))
        store.clear_images()
        self.assertEqual(store.stats()['image_bytes'], 0)

    def test_diagnostics_pause_can_keep_images_and_resume(self):
        store = self.store()
        r = store.record('porche', {}, self.image, self.now, diagnostics_enabled=False)
        self.assertTrue(r['image_saved'])
        self.assertFalse(r['diagnostics_saved'])
        self.assertIsNone(r['journal'])
        self.assertFalse(list(store.root.rglob('*.jsonl')))
        self.assertEqual(store.stats()['diagnostic_bytes'], 0)
        store.record('porche', {}, self.image, self.now + 1)
        self.assertTrue(list(store.root.rglob('*.jsonl')))

    def test_both_histories_paused_write_nothing(self):
        store = self.store()
        store.record('porche', {}, self.image, self.now, extra_images={'before': b'evidence'},
                     images_enabled=False, diagnostics_enabled=False)
        self.assertFalse(list(store.root.iterdir()))

    def test_clear_diagnostics_preserves_images_and_sampling_interval(self):
        store = self.store()
        store.record('porche', {}, self.image, self.now)
        images = {p: p.read_bytes() for p in store.root.rglob('*.jpg')}
        journal_bytes = store.stats()['diagnostic_bytes']
        result = store.clear_diagnostics()
        self.assertEqual(result['files_removed'], 1)
        self.assertEqual(result['bytes_freed'], journal_bytes)
        self.assertEqual(store.stats()['diagnostic_bytes'], 0)
        self.assertFalse(list(store.root.rglob('*.jsonl')))
        for p, content in images.items():
            self.assertEqual(p.read_bytes(), content)
        self.assertFalse(store.record('porche', {}, self.image, self.now + 1)['image_saved'])


if __name__ == "__main__":
    unittest.main()
