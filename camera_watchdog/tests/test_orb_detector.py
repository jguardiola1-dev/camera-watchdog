import ast
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

import orb_detector as detector
from orb_detector import OrbObserver, Settings, estimate


ROOT = Path(__file__).resolve().parents[1]


def scene(seed=2026):
    rng = np.random.default_rng(seed)
    image = cv2.GaussianBlur(rng.integers(35, 190, (360, 640), dtype=np.uint8), (3, 3), 0)
    for _ in range(250):
        x, y = rng.integers(20, 620), rng.integers(20, 340)
        cv2.circle(image, (int(x), int(y)), int(rng.integers(2, 10)),
                   int(rng.integers(25, 220)), -1)
    return image


def shift(image, x, y=0):
    return cv2.warpAffine(image, np.float32([[1, 0, x], [0, 1, y]]),
                          (image.shape[1], image.shape[0]))


class GeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = scene()

    def test_stationary_scene(self):
        r = estimate(self.image, self.image)
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "stable")
        self.assertAlmostEqual(r["dx"], 0, delta=0.1)

    def test_exposure_and_contrast_do_not_indicate_motion(self):
        for gain, offset in [(0.7, 20), (1.1, 10), (0.85, -10)]:
            with self.subTest(gain=gain, offset=offset):
                changed = np.clip(self.image.astype(float) * gain + offset, 0, 255).astype(np.uint8)
                r = estimate(self.image, changed)
                self.assertTrue(r["reliable"], r)
                self.assertEqual(r["status"], "stable", r)

    def test_uneven_lighting(self):
        gradient = np.linspace(0.7, 1.2, 640)[None, :]
        changed = np.clip(self.image * gradient, 0, 255).astype(np.uint8)
        r = estimate(self.image, changed)
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "stable", r)

    def test_translation_with_changed_exposure(self):
        changed = np.clip(shift(self.image, 18, -10).astype(float) * 0.8 + 15, 0, 255).astype(np.uint8)
        r = estimate(self.image, changed)
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "movement")
        self.assertAlmostEqual(r["dx"], 18, delta=1)
        self.assertAlmostEqual(r["dy"], -10, delta=1)

    def test_rotation(self):
        matrix = cv2.getRotationMatrix2D((320, 180), 2.0, 1)
        r = estimate(self.image, cv2.warpAffine(self.image, matrix, (640, 360)))
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "movement")
        self.assertAlmostEqual(abs(r["rotation"]), 2, delta=0.3)

    def test_scale(self):
        matrix = cv2.getRotationMatrix2D((320, 180), 0, 1.04)
        r = estimate(self.image, cv2.warpAffine(self.image, matrix, (640, 360)))
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "movement")
        self.assertAlmostEqual(r["scale"], 1.04, delta=0.01)

    def test_local_foreground_motion_leaves_background_stable(self):
        changed = self.image.copy()
        changed[120:230, 250:390] = scene(42)[120:230, 250:390]
        r = estimate(self.image, changed)
        self.assertTrue(r["reliable"], r)
        self.assertEqual(r["status"], "stable", r)

    def test_uniform_obstruction_is_inconclusive_not_a_fake_transform(self):
        for level in [0, 120, 255]:
            r = estimate(self.image, np.full_like(self.image, level))
            self.assertFalse(r["reliable"])
            self.assertEqual(r["status"], "inconclusive")
            self.assertIsNone(r["dx"])
            self.assertIsNone(r["rotation"])

    def test_localized_features_are_rejected(self):
        image = np.full_like(self.image, 120)
        image[130:230, 250:390] = self.image[130:230, 250:390]
        r = estimate(image, shift(image, 12))
        self.assertFalse(r["reliable"], r)
        self.assertIsNone(r["dx"])

    def test_aspect_ratio_change_is_not_silently_distorted(self):
        r = estimate(self.image, cv2.resize(self.image, (640, 480)))
        self.assertEqual(r["reason"], "aspect_ratio_changed")
        self.assertFalse(r["reliable"])

    def test_complete_loss_of_scene_is_inconclusive(self):
        r = estimate(self.image, scene(78))
        self.assertFalse(r["reliable"], r)
        self.assertIsNone(r["dx"])

    def test_missing_opencv(self):
        with patch.object(detector, "cv2", None):
            r = estimate(self.image, self.image)
        self.assertEqual(r["status"], "unavailable")
        self.assertFalse(r["observed_movement"])


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.image = scene()
        self.anchor = self.folder / "reference_day.png"
        self.current = self.folder / "current.png"
        cv2.imwrite(str(self.anchor), self.image)
        self.observer = OrbObserver()
        self.references = {"day": self.anchor}

    def observe(self, image, slug="porche", settings=None):
        cv2.imwrite(str(self.current), image)
        return self.observer.observe(slug, self.current, self.references, settings)

    def test_movement_requires_three_coherent_captures(self):
        moved = shift(self.image, 20)
        for i in range(1, 4):
            r = self.observe(moved)
            self.assertEqual(r["pending"], i)
            self.assertEqual(r["observed_movement"], i == 3)
        r = self.observe(self.image)
        self.assertEqual(r["pending"], 0)
        self.assertFalse(r["observed_movement"])

    def test_interrupted_confirmation_resets(self):
        self.observe(shift(self.image, 20))
        self.observe(np.zeros_like(self.image))
        r = self.observe(shift(self.image, 20))
        self.assertEqual(r["pending"], 1)

    def test_opposite_transforms_do_not_confirm(self):
        self.observe(shift(self.image, 20))
        r = self.observe(shift(self.image, -20))
        self.assertEqual(r["pending"], 1)
        self.assertFalse(r["observed_movement"])

    def test_drift_is_measured_against_unchanged_anchor(self):
        digest = hashlib.sha256(self.anchor.read_bytes()).hexdigest()
        results = [self.observe(shift(self.image, x)) for x in [2, 4, 6, 8, 10, 12]]
        self.assertEqual(results[0]["status"], "stable")
        self.assertEqual(results[-1]["status"], "movement")
        self.assertAlmostEqual(results[-1]["dx"], 12, delta=1)
        self.assertEqual(hashlib.sha256(self.anchor.read_bytes()).hexdigest(), digest)

    def test_camera_counters_are_independent(self):
        moved = shift(self.image, 20)
        self.observe(moved, "porche")
        self.observe(moved, "porche")
        r = self.observe(moved, "principal")
        self.assertEqual(r["pending"], 1)

    def test_manual_reference_change_invalidates_pending(self):
        self.observe(shift(self.image, 20))
        cv2.imwrite(str(self.anchor), shift(self.image, 3))
        r = self.observe(shift(self.image, 20))
        self.assertEqual(r["pending"], 1)

    def test_threshold_change_invalidates_pending(self):
        self.observe(shift(self.image, 20))
        r = self.observe(shift(self.image, 20), settings=replace(Settings(), translation_percent=1.1))
        self.assertEqual(r["pending"], 1)

    def test_no_reference_never_learns_current_image(self):
        self.references = {}
        r = self.observe(self.image)
        self.assertEqual(r["reason"], "no_saved_reference")
        self.assertFalse(r["observed_movement"])
        self.assertEqual(set(p.name for p in self.folder.iterdir()), {"current.png", "reference_day.png"})

    def test_disagreeing_references_do_not_authorize_new_orientation(self):
        fixed = self.folder / "fixed.png"
        cv2.imwrite(str(fixed), shift(self.image, 20))
        self.references["fixed"] = fixed
        r = self.observe(self.image)
        self.assertEqual(r["reason"], "references_disagree")
        self.assertFalse(r["reliable"])
        self.assertIsNone(r["dx"])

    def test_invalid_configuration(self):
        for value in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                Settings.from_options({"orb_translation_percent": value})
        with self.assertRaises(ValueError):
            Settings.from_options({"orb_confirmations": 1})


class LegacyIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("watchdog_under_test", ROOT / "app.py")
        cls.app = importlib.util.module_from_spec(spec)
        # Importing the production app creates /data/images. Suppress only that
        # initialization; every test then points all persistence into its temp dir.
        with patch.object(Path, "mkdir"):
            spec.loader.exec_module(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        for name, value in [("DATA_DIR", folder), ("IMAGES_DIR", folder / "images"),
                            ("CAMERAS_FILE", folder / "cameras.json"),
                            ("OPTIONS_FILE", folder / "options.json"),
                            ("LOG_FILE", folder / "watchdog.log"),
                            ("runtime", {}), ("orb_observer", OrbObserver()),
                            ("census_observers", {}), ("census_pending_evidence", {}),
                            ("observation_store", None)]:
            p = patch.object(self.app, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.camera = dict(self.app.DEFAULT_CAMERA, slug="porche", name="porche")
        self.app.OPTIONS_FILE.write_text(json.dumps({"detailed_log": False}))
        self.app.alarm_latches = None
        self.app.last_ha_signature = None
        self.app.last_ha_publication = {}
        current = self.app.image_path("porche", "current")
        cv2.imwrite(str(current), scene())
        self.app.set_rt("porche", changed=True, offline=True, pending_count=2)

    def test_observation_cannot_change_or_publish_alarm_state(self):
        current = self.app.image_path("porche", "current")
        anchor = self.app.image_path("porche", "day")
        cv2.imwrite(str(anchor), scene())
        cv2.imwrite(str(current), shift(scene(), 20))
        before = self.app.get_rt("porche")
        with patch.object(self.app, "update_sabotaje_cam") as publish:
            for _ in range(3):
                self.app.observe_geometry(self.camera, current)
            publish.assert_not_called()
        after = self.app.get_rt("porche")
        self.assertTrue(after["orb"]["observed_movement"])
        for key in ("changed", "offline", "pending_count"):
            self.assertEqual(after[key], before[key])

    def test_observer_failure_does_not_escape_into_offline_logic(self):
        with patch.object(self.app.orb_observer, "observe", side_effect=RuntimeError("secret")):
            self.app.observe_geometry(self.camera, self.app.image_path("porche", "current"))
        state = self.app.get_rt("porche")
        self.assertEqual(state["orb"]["reason"], "diagnostic_error")
        self.assertNotIn("secret", json.dumps(state["orb"]))
        self.assertTrue(state["changed"])

    def test_disabled_observation_does_not_touch_legacy_state(self):
        self.app.OPTIONS_FILE.write_text(json.dumps({"orb_observation": False, "detailed_log": False}))
        self.app.observe_geometry(self.camera, self.app.image_path("porche", "current"))
        state = self.app.get_rt("porche")
        self.assertEqual(state["orb"]["status"], "disabled")
        self.assertTrue(state["changed"])
        self.assertEqual(state["pending_count"], 2)

    def test_operational_capture_never_uses_legacy_canny_decision(self):
        cv2.imwrite(str(self.app.image_path("porche", "previous")), scene())
        self.app.set_rt("porche", pending_count=0)
        with patch.object(self.app, "capture_frame"), \
             patch.object(self.app, "collection_only", return_value=False), \
             patch.object(self.app, "canny_difference", return_value=5.0), \
             patch.object(self.app, "update_sabotaje_cam") as publish:
            publish.return_value = {"published": False}
            self.app.process_camera(self.camera)
            self.assertFalse(self.app.get_rt("porche")["changed"])
            self.app.process_camera(self.camera)
            self.assertFalse(self.app.get_rt("porche")["changed"])
            self.app.canny_difference.assert_not_called()
            self.assertEqual(publish.call_count, 2)

    def test_dashboard_and_api_show_observation_without_credentials(self):
        self.app.CAMERAS_FILE.write_text(json.dumps([dict(self.camera, password="secret", rtsp_url="rtsp://secret@host")]))
        self.app.observe_geometry(self.camera, self.app.image_path("porche", "current"))
        client = self.app.app.test_client()
        data = client.get("/api/status").get_json()
        self.assertEqual(data["cameras"][0]["orb"]["mode"], "observation")
        self.assertNotIn("secret", json.dumps(data))
        self.assertIn("solo observación", client.get("/").get_data(as_text=True))
        self.assertIn("no activan el sensor", client.get("/camera/porche").get_data(as_text=True))

    def test_existing_alarm_rtsp_and_reference_functions_are_unchanged(self):
        old_functions = json.loads((ROOT / "tests" / "fixtures" / "legacy_function_hashes.json").read_text())
        new = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        new_functions = {node.name: ast.dump(node) for node in new.body if isinstance(node, ast.FunctionDef)}
        for name in ("build_rtsp", "clear_alert",
                     "canny_difference", "covered_from_stats",
                     "camera_save", "accept_current"):
            self.assertEqual(old_functions[name], hashlib.sha256(new_functions[name].encode()).hexdigest(), name)

    def test_collection_guard_blocks_every_sensor_publish(self):
        with patch.object(self.app.urllib.request, "urlopen") as api:
            self.app.update_sabotaje_cam()
            api.assert_not_called()
        self.assertTrue(self.app.collection_only())

    def test_collection_never_calls_canny_or_overwrites_previous(self):
        previous = self.app.image_path("porche", "previous")
        previous.write_bytes(b"original")
        with patch.object(self.app, "capture_frame"), \
             patch.object(self.app, "canny_difference") as canny, \
             patch.object(self.app, "record_observation") as save, \
             patch.object(self.app, "update_sabotaje_cam") as api:
            self.app.process_camera(self.camera)
            canny.assert_not_called()
            api.assert_not_called()
            save.assert_called_once()
        self.assertEqual(previous.read_bytes(), b"original")
        self.assertFalse(self.app.get_rt("porche")["changed"])

    def test_credentials_are_redacted_from_capture_errors(self):
        error = RuntimeError("ffmpeg rtsp://user:secret@192.0.2.10:554/stream failed")
        self.assertNotIn("secret", self.app.safe_capture_error(error))

    def test_live_census_is_recorded_without_any_alarm_call(self):
        current = self.app.image_path("porche", "current")
        with patch.object(self.app.urllib.request, "urlopen") as api:
            self.app.observe_census(self.camera, current)
            self.app.record_observation(self.camera, current, census=self.app.get_rt("porche")["census"])
            api.assert_not_called()
        state = self.app.get_rt("porche")
        self.assertEqual(state["census"]["mode"], "observation")
        self.assertEqual(state["census"]["status"], "initialized")
        self.assertTrue(state["observation_storage"]["recorded"])
        records = list((self.app.DATA_DIR / "observations").rglob("*.jsonl"))
        record = json.loads(records[0].read_text())
        self.assertIn("census", record)
        self.assertFalse(record["alarms_published"])
        self.assertFalse(record["census"]["alarms_published"])
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        client = self.app.app.test_client()
        self.assertIn("Census:", client.get("/").get_data(as_text=True))
        self.assertIn("Diagnóstico Census", client.get("/camera/porche").get_data(as_text=True))
        self.assertEqual(client.get("/api/status").get_json()["cameras"][0]["census"]["mode"], "observation")

    def test_census_errors_do_not_discard_the_existing_anchor(self):
        current = self.app.image_path("porche", "current")
        self.app.observe_census(self.camera, current)
        observer = self.app.census_observers["porche"]
        anchor = observer.anchor.copy()
        with patch.object(observer, "observe", side_effect=RuntimeError("secret")):
            self.app.observe_census(self.camera, current)
        self.assertTrue(np.array_equal(observer.anchor, anchor))
        self.assertEqual(self.app.get_rt("porche")["census"]["reason"], "diagnostic_error")
        self.assertNotIn("secret", json.dumps(self.app.get_rt("porche")["census"]))

    def test_fixed_day_night_references_are_used_recorded_and_never_overwritten(self):
        folder = self.app.camera_dir('porche')
        for kind, image in [('day', scene()), ('night', scene(42))]:
            cv2.imwrite(str(self.app.image_path('porche',kind)), image)
        before = {k: self.app.image_path('porche',k).read_bytes() for k in ('day','night')}
        current = self.app.image_path('porche', 'current')
        cv2.imwrite(str(current), scene(42))
        with patch.object(self.app.urllib.request, 'urlopen') as api:
            result = self.app.observe_census(self.camera, current)
            self.app.record_observation(self.camera, current, census=result)
            api.assert_not_called()
        self.assertEqual(result['comparison_mode'], 'day_night_references')
        self.assertEqual(result['reference'], 'night')
        self.assertEqual(result['status'], 'stable')
        self.assertFalse(result['observed_scene_loss'])
        self.assertEqual(before, {k: self.app.image_path('porche',k).read_bytes() for k in ('day','night')})
        self.assertIn('referencias de día y noche', self.app.census_details(result))
        record = json.loads(next((self.app.DATA_DIR / 'observations').rglob('*.jsonl')).read_text())
        self.assertEqual(set(record['census']['reference_comparisons']), {'day', 'night'})
        self.assertFalse(record['alarms_published'])


if __name__ == "__main__":
    unittest.main()
