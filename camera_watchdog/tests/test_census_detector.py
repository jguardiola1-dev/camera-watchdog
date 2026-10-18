from dataclasses import replace
import unittest

import numpy as np

from census_detector import compare_scene, SceneObserver, ReferenceSceneObserver, CensusSettings
from test_orb_detector import scene, shift


class CensusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = scene()

    def test_identical_scene(self):
        r = compare_scene(self.image, self.image)
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "stable")

    def test_initial_dark_image_does_not_poison_the_anchor(self):
        observer = SceneObserver()
        r = observer.observe(np.zeros_like(self.image), "dark", 1)
        self.assertEqual(r["status"], "inconclusive")
        self.assertIsNone(observer.anchor)
        r = observer.observe(self.image, "valid", 61)
        self.assertEqual(r["status"], "initialized")

    def test_options_require_separate_stable_and_change_thresholds(self):
        with self.assertRaises(ValueError):
            CensusSettings.from_options({"census_stable_block_ratio": 0.7})

    def test_modest_exposure_changes(self):
        for gain, offset in [(0.7, 20), (1.1, 10), (0.85, -10)]:
            changed = np.clip(self.image.astype(float) * gain + offset, 0, 255).astype(np.uint8)
            r = compare_scene(self.image, changed)
            self.assertTrue(r["reliable"], r)
            self.assertEqual(r["status"], "stable", r)

    def test_small_foreground_change(self):
        changed = self.image.copy()
        changed[120:230, 250:390] = scene(42)[120:230, 250:390]
        r = compare_scene(self.image, changed)
        self.assertEqual(r["status"], "stable", r)

    def test_black_or_uniform_image_is_not_a_normal_scene(self):
        for level in [0, 120, 255]:
            r = compare_scene(self.image, np.full_like(self.image, level))
            self.assertFalse(r["reliable"])
            self.assertEqual(r["status"], "inconclusive")

    def test_small_capture_jitter(self):
        r = compare_scene(self.image, shift(self.image, 1))
        self.assertEqual(r["status"], "stable", r)

    def test_anchor_is_frozen_until_return(self):
        # A stricter experimental threshold makes synthetic unrelated noise a
        # candidate. This verifies temporal behavior, not production calibration.
        settings = replace(CensusSettings(), block_distance=0.15)
        observer = SceneObserver(settings)
        observer.observe(self.image, "before", 1)
        moved = scene(99)
        r = observer.observe(moved, "changed_first", 61)
        self.assertTrue(r["scene_change"], r)
        self.assertEqual(r["anchor_id"], "before")
        r = observer.observe(moved, "changed_second", 121)
        self.assertTrue(r["observed_scene_loss"], r)
        self.assertTrue(np.array_equal(observer.anchor, self.image))
        r = observer.observe(self.image, "returned", 181)
        self.assertFalse(r["observed_scene_loss"])
        self.assertEqual(r["status"], "stable")

    def test_bad_quality_does_not_adopt_new_anchor(self):
        observer = SceneObserver()
        observer.observe(self.image, "before", 1)
        observer.observe(np.zeros_like(self.image), "dark", 61)
        self.assertTrue(np.array_equal(observer.anchor, self.image))
        r = observer.observe(self.image, "returned", 121)
        self.assertEqual(r["status"], "stable")

    def test_replaying_a_capture_cannot_confirm_an_event(self):
        observer = SceneObserver()
        observer.observe(self.image, "before", 1)
        with self.assertRaises(ValueError):
            observer.observe(self.image, "before", 1)

    def test_partial_scene_change_does_not_become_a_new_normal_anchor(self):
        settings = replace(CensusSettings(), block_distance=0.15)
        changed = self.image.copy()
        changed[:, :250] = scene(99)[:, :250]
        observer = SceneObserver(settings)
        observer.observe(self.image, "before", 1)
        r = observer.observe(changed, "partial_change", 61)
        self.assertEqual(r["status"], "inconclusive", r)
        self.assertEqual(r["reason"], "partial_structure_change")
        self.assertTrue(np.array_equal(observer.anchor, self.image))


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.day = scene(2026)
        self.night = scene(42)
        self.references = {'day': ('day-1', self.day), 'night': ('night-1', self.night)}
        self.observer = ReferenceSceneObserver(replace(CensusSettings(), block_distance=0.15))

    def test_switch_to_night_clears_without_replacing_references(self):
        self.assertEqual(self.observer.observe(self.day, 'day', 1, self.references)['status'], 'stable')
        self.assertEqual(self.observer.observe(self.night, 'night', 61, self.references)['status'], 'stable')
        self.assertTrue(np.array_equal(self.references['day'][1], self.day))

    def test_both_references_must_change_and_return_clears(self):
        moved = scene(99)
        first = self.observer.observe(moved, 'moved-1', 1, self.references)
        self.assertEqual(first['status'], 'scene_change')
        self.assertFalse(first['observed_scene_loss'])
        second = self.observer.observe(moved, 'moved-2', 61, self.references)
        self.assertTrue(second['observed_scene_loss'])
        returned = self.observer.observe(self.night, 'returned', 121, self.references)
        self.assertFalse(returned['observed_scene_loss'])
        self.assertFalse(returned['alarms_published'])

    def test_missing_reference_or_dark_frame_cannot_confirm_loss(self):
        for refs, image in [({}, scene(99)),
                            (self.references, np.zeros_like(self.day))]:
            observer = ReferenceSceneObserver(self.observer.settings)
            for t in (1, 61):
                result = observer.observe(image, str(t), t, refs)
                self.assertEqual(result['status'], 'inconclusive')
                self.assertFalse(result['observed_scene_loss'])

    def test_single_saved_reference_confirms_and_recovers(self):
        for kind in ('day', 'night_5'):
            observer = ReferenceSceneObserver(self.observer.settings)
            refs = {kind: ('only', self.day)}
            self.assertEqual(observer.observe(self.day, 'normal', 1, refs)['status'], 'stable')
            self.assertFalse(observer.observe(scene(99), 'first', 61, refs)['observed_scene_loss'])
            self.assertTrue(observer.observe(scene(99), 'second', 121, refs)['observed_scene_loss'])
            self.assertFalse(observer.observe(self.day, 'returned', 181, refs)['observed_scene_loss'])

    def test_all_ten_are_compared_and_any_one_match_clears(self):
        from reference_catalog import REFERENCE_KINDS
        refs = {kind: (kind, scene(200 + index)) for index, kind in enumerate(REFERENCE_KINDS)}
        for index, (kind, (_, image)) in enumerate(refs.items()):
            result = self.observer.observe(image, kind, 1 + index * 60, refs)
            self.assertEqual(result['status'], 'stable')
            self.assertEqual(result['reference'], kind)
            self.assertEqual(set(result['reference_comparisons']), set(REFERENCE_KINDS))
        self.assertFalse(self.observer.observe(scene(99), 'first', 601, refs)['observed_scene_loss'])
        self.assertTrue(self.observer.observe(scene(99), 'second', 661, refs)['observed_scene_loss'])
        self.assertFalse(self.observer.observe(refs['night_5'][1], 'return', 721, refs)['observed_scene_loss'])

    def test_replay_gap_and_reference_change_require_new_confirmations(self):
        moved = scene(99)
        self.observer.observe(moved, 'first', 1, self.references)
        with self.assertRaises(ValueError):
            self.observer.observe(moved, 'replay', 1, self.references)
        self.assertFalse(self.observer.observe(moved, 'gap', 301, self.references)['observed_scene_loss'])
        refs = {**self.references, 'day': ('day-2', self.day)}
        self.assertFalse(self.observer.observe(moved, 'new-ref', 361, refs)['observed_scene_loss'])

    def test_two_night_variants_are_normal_and_loss_requires_all_three(self):
        alternate=scene(73)
        refs={**self.references,'night_alt':('night-alt-1',alternate)}
        for t,image in [(1,self.day),(61,self.night),(121,alternate),(181,self.night)]:
            result=self.observer.observe(image,str(t),t,refs)
            self.assertEqual(result['status'],'stable')
            self.assertFalse(result['observed_scene_loss'])
        moved=scene(99)
        first=self.observer.observe(moved,'moved1',241,refs)
        second=self.observer.observe(moved,'moved2',301,refs)
        self.assertFalse(first['observed_scene_loss'])
        self.assertTrue(second['observed_scene_loss'])
        self.assertEqual(second['reason'],'all_references_changed')
        self.assertEqual(set(second['reference_comparisons']),{'day','night','night_alt'})
        recovered=self.observer.observe(alternate,'returned',361,refs)
        self.assertEqual(recovered['reference'],'night_alt')
        self.assertFalse(recovered['observed_scene_loss'])

    def test_invalid_extra_night_blocks_new_confirmation(self):
        refs={**self.references,'night_alt':('invalid',None)}
        for t in (1,61):
            result=self.observer.observe(scene(99),str(t),t,refs)
            self.assertFalse(result['observed_scene_loss'])
            self.assertEqual(result['status'],'inconclusive')

    def test_adding_extra_night_resets_pending_confirmation(self):
        moved=scene(99)
        self.observer.observe(moved,'old-set',1,self.references)
        refs={**self.references,'night_alt':('alternate',scene(73))}
        result=self.observer.observe(moved,'new-set',61,refs)
        self.assertFalse(result['observed_scene_loss'])
        self.assertEqual(result['pending'],1)


if __name__ == "__main__":
    unittest.main()
