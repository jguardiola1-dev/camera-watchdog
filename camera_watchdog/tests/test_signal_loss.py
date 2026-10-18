import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alarm_policy import SignalLossMonitor
import test_alarm_policy as alarm_fixtures
import test_orb_detector as fixtures
import web_ui


class SignalMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'signal.json'
        self.monitor = SignalLossMonitor(self.path)

    def test_two_failures_and_entire_wait_are_both_required(self):
        self.assertFalse(self.monitor.status('camera', now=1000)['confirmed'])
        self.monitor.failure('camera', now=0)
        self.assertFalse(self.monitor.status('camera', now=121)['confirmed'])
        self.monitor.failure('camera', now=45)
        self.assertFalse(self.monitor.status('camera', now=119)['confirmed'])
        self.assertTrue(self.monitor.status('camera', now=120)['confirmed'])

    def test_success_resets_pending_and_a_new_cut_requires_full_wait(self):
        self.monitor.failure('camera', now=0)
        self.monitor.failure('camera', now=45)
        self.monitor.success('camera')
        self.assertFalse(self.monitor.status('camera', now=130)['pending'])
        self.monitor.failure('camera', now=140)
        self.monitor.failure('camera', now=180)
        self.assertFalse(self.monitor.status('camera', now=259)['confirmed'])
        self.assertTrue(self.monitor.status('camera', now=260)['confirmed'])

    def test_confirmed_alarm_survives_restart_until_a_valid_capture(self):
        self.monitor.failure('camera', now=0)
        self.monitor.failure('camera', now=40)
        self.assertTrue(self.monitor.status('camera', now=120)['confirmed'])
        restarted = SignalLossMonitor(self.path)
        self.assertTrue(restarted.status('camera')['confirmed'])
        self.assertFalse(restarted.status('camera')['capture_validated'])
        restarted.success('camera')
        self.assertFalse(SignalLossMonitor(self.path).status('camera')['confirmed'])

    def test_unconfirmed_startup_gets_a_new_grace_period_and_cameras_are_independent(self):
        self.monitor.failure('camera', now=0)
        restarted = SignalLossMonitor(self.path)
        self.assertFalse(restarted.status('camera', now=1000)['pending'])
        self.monitor.failure('camera', now=40)
        self.assertTrue(self.monitor.status('camera', now=120)['confirmed'])
        self.assertFalse(self.monitor.status('other', now=120)['confirmed'])


class SignalPublicationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LegacyIsolationTests.setUpClass.__func__)

    def setUp(self):
        alarm_fixtures.OperationalTests.setUp(self)
        self.camera = {**self.camera, 'enabled': False, 'signal_loss_enabled': True}
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))

    def payloads(self):
        return alarm_fixtures.OperationalTests.payloads(self)

    def fail_capture(self, now):
        with patch('alarm_policy.time.monotonic', return_value=now), \
             patch.object(self.app, 'capture_frame', side_effect=RuntimeError('no stream')):
            with self.assertRaises(RuntimeError):
                self.app.process_camera(self.camera)
            self.app.update_sabotaje_cam()

    def test_signal_is_independent_of_census_and_recovery_clears_sensor(self):
        for kind in ('day', 'night'):
            self.app.image_path('porche', kind).unlink()
        self.fail_capture(10)
        self.fail_capture(50)
        self.assertEqual(self.payloads()[-1]['state'], 'off')
        with patch('alarm_policy.time.monotonic', return_value=130):
            self.app.update_sabotaje_cam()
            self.assertEqual(self.payloads()[-1]['state'], 'on')
            self.assertEqual(self.payloads()[-1]['attributes']['camaras_sin_senal'], ['porche'])
            state = self.app.app.test_client().get('/api/status').get_json()['cameras'][0]
            self.assertTrue(state['changed'])
            self.assertFalse(state['alarms_enabled'])
            self.assertTrue(state['signal_loss_enabled'])
            self.assertIn('pérdida de señal', web_ui.presentation('porche', state)[0])
        with patch.object(self.app, 'capture_frame'):
            self.app.process_camera(self.camera)
        self.assertEqual(self.payloads()[-1]['state'], 'off')

    def test_both_alarm_types_are_combined_and_signal_recovery_keeps_scene_alarm(self):
        self.camera['enabled'] = True
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.app.get_alarm_latches().states['porche'] = True
        self.fail_capture(0)
        self.fail_capture(40)
        with patch('alarm_policy.time.monotonic', return_value=120):
            self.app.update_sabotaje_cam()
        attrs = self.payloads()[-1]['attributes']
        self.assertEqual(attrs['camaras_cambio_escena'], ['porche'])
        self.assertEqual(attrs['camaras_sin_senal'], ['porche'])
        self.assertEqual(attrs['numero_camaras'], 1)
        self.app.get_signal_loss_monitor().success('porche')
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'], 'on')
        self.assertEqual(self.payloads()[-1]['attributes']['camaras_sin_senal'], [])

    def test_global_pause_and_per_camera_checkbox_suppress_signal_alarm(self):
        self.fail_capture(0)
        self.fail_capture(40)
        with patch('alarm_policy.time.monotonic', return_value=120):
            self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'], 'on')
        self.camera['signal_loss_enabled'] = False
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'], 'off')
        state = self.app.app.test_client().get('/api/status').get_json()['cameras'][0]
        self.assertFalse(state['changed'])
        self.assertNotIn('alarm', web_ui.presentation('porche', state)[1])
        self.app.OPTIONS_FILE.write_text('{}')
        self.http.reset_mock()
        self.app.update_sabotaje_cam()
        self.http.assert_not_called()

    def test_successful_signal_return_clears_after_restart_even_if_census_is_inconclusive(self):
        self.camera['enabled'] = True
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.app.get_signal_loss_monitor().states['porche'] = True
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'], 'on')
        with patch.object(self.app, 'capture_frame'), \
             patch.object(self.app, 'observe_census', return_value={'status':'inconclusive'}):
            self.app.process_camera(self.camera)
        self.assertEqual(self.payloads()[-1]['state'], 'off')

    def test_analysis_failure_does_not_count_as_lost_signal(self):
        with patch.object(self.app, 'capture_frame'), \
             patch.object(self.app, 'observe_geometry', side_effect=RuntimeError('analysis failure')):
            with self.assertRaises(RuntimeError):
                self.app.process_camera(self.camera)
        self.assertFalse(self.app.get_signal_loss_monitor().status('porche')['pending'])

    def test_corrupt_frame_does_count_as_capture_failure(self):
        self.current.write_bytes(b'not jpeg')
        with patch.object(self.app, 'capture_frame'):
            with self.assertRaises(Exception):
                self.app.process_camera(self.camera)
        self.assertEqual(self.app.get_signal_loss_monitor().status('porche')['failures'], 1)

    def test_manual_capture_recovers_signal_and_checkbox_stops_alarm_immediately(self):
        self.app.set_rt('porche', offline=True, census={'status':'offline'})
        self.app.get_signal_loss_monitor().states['porche'] = True
        with patch.object(self.app, 'capture_frame'):
            self.assertEqual(self.app.app.test_client().post('/camera/porche/capture-now').status_code, 302)
        self.assertEqual(self.payloads()[-1]['state'], 'off')
        self.assertFalse(self.app.get_rt('porche')['offline'])
        self.app.get_signal_loss_monitor().states['porche'] = True
        response = self.app.app.test_client().post('/camera/save', data=dict(
            original_slug='porche', name='porche', rtsp_url='rtsp://camera.invalid/stream'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.app.get_signal_loss_monitor().get('porche'))
        self.assertEqual(self.payloads()[-1]['state'], 'off')

    def test_form_saves_independent_check_without_enabling_census(self):
        response = self.app.app.test_client().post('/camera/save', data=dict(
            original_slug='porche', name='porche', rtsp_url='rtsp://camera.invalid/stream',
            signal_loss_enabled='on'))
        self.assertEqual(response.status_code, 302)
        camera = self.app.load_cameras()[0]
        self.assertFalse(camera['enabled'])
        self.assertTrue(camera['signal_loss_enabled'])
        page = self.app.app.test_client().get('/camera/porche/edit').get_data(as_text=True)
        self.assertIn('Avisar por pérdida de señal', page)

    def test_disabling_signal_checkbox_clears_without_waiting_for_missing_census_capture(self):
        self.camera['enabled'] = True
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.app.get_signal_loss_monitor().states['porche'] = True
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'], 'on')
        response = self.app.app.test_client().post('/camera/save', data=dict(
            original_slug='porche', name='porche', enabled='on', rtsp_url='rtsp://camera.invalid/stream'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.payloads()[-1]['state'], 'off')
