import json
import unittest
from unittest.mock import patch

import test_orb_detector as fixtures


class HistoryControlsTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LegacyIsolationTests.setUpClass.__func__)

    def setUp(self):
        fixtures.LegacyIsolationTests.setUp(self)
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.client = self.app.app.test_client()

    def test_pause_persists_and_clear_preserves_current_and_configuration(self):
        current = self.app.image_path('porche', 'current')
        reference = self.app.image_path('porche', 'night')
        reference.write_bytes(current.read_bytes())
        protected = {p: p.read_bytes() for p in (current, reference, self.app.CAMERAS_FILE, self.app.OPTIONS_FILE)}
        self.app.record_observation(self.camera, current)
        self.assertEqual(self.app.sample_history_status()['image_count'], 1)
        response = self.client.post('/sample-history/pause', headers={'X-Ingress-Path': '/ingress-test'})
        self.assertEqual(response.location, '/ingress-test/sample-history')
        self.app.observation_store = None
        self.assertFalse(self.app.sample_history_enabled())
        self.app.record_observation(self.camera, current)
        self.assertEqual(self.app.sample_history_status()['image_count'], 1)
        self.assertEqual(self.client.post('/sample-history/clear').status_code, 302)
        self.app.record_observation(self.camera, current)
        self.assertEqual(self.app.sample_history_status()['image_count'], 0)
        self.assertTrue(list((self.app.DATA_DIR/'observations').rglob('*.jsonl')))
        self.assertEqual(self.client.post('/sample-history/resume').status_code, 302)
        self.app.record_observation(self.camera, current)
        self.assertEqual(self.app.sample_history_status()['image_count'], 1)
        for p, content in protected.items():
            self.assertEqual(p.read_bytes(), content)
        self.assertEqual(self.client.post('/sample-history/unknown').status_code, 400)
        self.assertEqual(self.client.get('/sample-history/clear').status_code, 405)

    def test_clear_error_is_reported_and_capture_state_is_preserved(self):
        before = self.app.get_rt('porche')
        with patch.object(self.app.get_observation_store(), 'clear_images', side_effect=OSError('private')):
            response = self.client.post('/sample-history/clear')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private', response.get_data(as_text=True))
        self.assertEqual(self.app.get_rt('porche'), before)

    def test_dashboard_and_api_show_history_controls_and_current_size(self):
        self.app.record_observation(self.camera, self.app.image_path('porche', 'current'))
        home = self.client.get('/', headers={'X-Ingress-Path': '/ingress-test'}).get_data(as_text=True)
        self.assertIn('href="/ingress-test/sample-history"', home)
        self.assertNotIn('id="sample-history"', home)
        page = self.client.get('/sample-history', headers={'X-Ingress-Path': '/ingress-test'}).get_data(as_text=True)
        self.assertIn('/ingress-test/sample-history/pause', page)
        self.assertIn('/ingress-test/sample-history/clear', page)
        self.assertIn('data-confirm=', page)
        self.assertEqual(self.client.get('/api/status').get_json()['sample_history']['image_count'], 1)
        self.client.post('/sample-history/pause')
        self.assertIn('/sample-history/resume', self.client.get('/sample-history').get_data(as_text=True))

    def test_diagnostic_controls_preserve_image_preference_and_survive_restart(self):
        current = self.app.image_path('porche', 'current')
        self.client.post('/sample-history/pause')
        self.assertEqual(self.client.post('/sample-history/pause-diagnostics').status_code, 302)
        self.app.observation_store = None
        self.app.record_observation(self.camera, current)
        self.assertFalse(list((self.app.DATA_DIR/'observations').rglob('*.jsonl')))
        self.assertFalse(self.app.sample_history_enabled())
        self.assertFalse(self.app.diagnostic_history_enabled())
        self.client.post('/sample-history/resume')
        self.assertFalse(self.app.diagnostic_history_enabled())
        self.app.record_observation(self.camera, current)
        self.assertEqual(self.app.sample_history_status()['image_count'], 1)
        self.assertEqual(self.app.sample_history_status()['diagnostic_bytes'], 0)
        self.client.post('/sample-history/resume-diagnostics')
        self.app.record_observation(self.camera, current)
        self.assertGreater(self.app.sample_history_status()['diagnostic_bytes'], 0)
        self.assertEqual(self.client.post('/sample-history/clear-diagnostics').status_code, 302)
        self.assertEqual(self.app.sample_history_status()['diagnostic_bytes'], 0)
        self.assertEqual(self.app.sample_history_status()['image_count'], 1)
        self.assertTrue(self.app.sample_history_enabled())
        self.assertTrue(self.app.diagnostic_history_enabled())

    def test_paused_diagnostic_analysis_does_not_write_verbose_logs(self):
        self.client.post('/sample-history/pause-diagnostics')
        self.app.OPTIONS_FILE.write_text(json.dumps({'detailed_log': True}))
        current = self.app.image_path('porche', 'current')
        with patch.object(self.app, 'write_watchdog_log') as log:
            self.app.observe_census(self.camera, current)
            self.app.observe_geometry(self.camera, current)
            log.assert_not_called()
        self.assertIsNotNone(self.app.get_rt('porche').get('census'))
