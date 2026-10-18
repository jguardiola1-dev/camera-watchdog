import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import cv2
from alarm_policy import AlarmLatches
import test_orb_detector as fixtures
from test_orb_detector import scene


class LatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'state.json'
        self.latches = AlarmLatches(self.path)
        self.lost = dict(comparison_mode='day_night_references', reliable=True,
                         status='scene_change', scene_change=True, observed_scene_loss=True)
        self.stable = dict(comparison_mode='day_night_references', reliable=True, status='stable')

    def test_confirmed_loss_survives_restart_and_unknown_but_stable_clears(self):
        self.assertEqual(self.latches.update('porche', self.lost),(True,True))
        restarted = AlarmLatches(self.path)
        self.assertEqual(restarted.update('porche', {'status':'offline'}),(True,True))
        self.assertEqual(restarted.update('porche', self.stable),(False,True))
        self.assertFalse(AlarmLatches(self.path).get('porche'))

    def test_temporal_pending_and_unknown_cannot_set_an_alarm(self):
        for slug, result in [('principal',{**self.lost,'comparison_mode':'temporal'}),
                             ('porche',{**self.lost,'comparison_mode':'temporal'}),
                             ('porche',{**self.lost,'observed_scene_loss':False}),
                             ('porche',{'status':'inconclusive','reliable':False})]:
            self.assertFalse(self.latches.update(slug,result)[0])

    def test_corrupt_persistence_is_not_silently_accepted(self):
        self.path.write_text('{')
        with self.assertRaises(ValueError): AlarmLatches(self.path)


class OperationalTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LegacyIsolationTests.setUpClass.__func__)

    def setUp(self):
        fixtures.LegacyIsolationTests.setUp(self)
        self.app.OPTIONS_FILE.write_text(json.dumps({'detailed_log':False,'census_alarms_enabled':True,
                                                    'census_block_distance':0.15}))
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.addCleanup(patch.stopall)
        patch.dict(self.app.os.environ, {'SUPERVISOR_TOKEN':'test-token'}).start()
        self.http = patch.object(self.app.urllib.request,'urlopen').start()
        self.http.return_value.__enter__.return_value.status = 201
        self.current = self.app.image_path('porche','current')
        for name, image in [('day',scene()),('night',scene(42))]:
            cv2.imwrite(str(self.app.image_path('porche',name)),image)

    def payloads(self):
        return [json.loads(call.args[0].data) for call in self.http.call_args_list]

    def test_two_captures_publish_census_on_and_return_publishes_off(self):
        previous=self.app.image_path('porche','previous')
        previous.write_bytes(b'original')
        with patch.object(self.app,'capture_frame'), patch.object(self.app,'canny_difference') as canny:
            self.app.process_camera(self.camera)
            cv2.imwrite(str(self.current),scene(99))
            self.app.process_camera(self.camera)
            self.assertFalse(self.app.get_rt('porche')['changed'])
            self.app.process_camera(self.camera)
            self.assertTrue(self.app.get_rt('porche')['changed'])
            cv2.imwrite(str(self.current),scene(42))
            self.app.process_camera(self.camera)
            self.assertFalse(self.app.get_rt('porche')['changed'])
            canny.assert_not_called()
        self.assertEqual([p['state'] for p in self.payloads()],['off','on','off'])
        self.assertEqual(self.payloads()[1]['attributes']['camaras_en_sabotaje'],['porche'])
        self.assertEqual(previous.read_bytes(),b'original')
        records=[json.loads(line) for path in (self.app.DATA_DIR/'observations').rglob('*.jsonl') for line in path.read_text().splitlines()]
        self.assertTrue(any(r.get('alarms_published') and r.get('alarm_state')=='on' for r in records))
        self.assertTrue(all(r['mode']=='operational' for r in records))

    def test_pausing_and_clearing_sample_images_does_not_disable_alarm_or_recovery(self):
        client = self.app.app.test_client()
        client.post('/sample-history/pause')
        client.post('/sample-history/pause-diagnostics')
        with patch.object(self.app, 'capture_frame'):
            self.app.process_camera(self.camera)
            cv2.imwrite(str(self.current), scene(99))
            self.app.process_camera(self.camera)
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'], 'on')
            client.post('/sample-history/clear')
            cv2.imwrite(str(self.current), scene())
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'], 'off')
        self.assertEqual(self.app.sample_history_status()['image_count'], 0)
        self.assertEqual(self.app.sample_history_status()['diagnostic_bytes'], 0)

    def test_principal_checkbox_controls_publication_like_other_cameras(self):
        camera={**self.camera,'slug':'principal','name':'principal','enabled':True}
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera,camera]))
        self.app.get_alarm_latches().states['principal']=True
        self.app.set_rt('porche',alarm_ready=True)
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[0]['state'],'on')
        self.assertEqual(self.payloads()[0]['attributes']['camaras_habilitadas'],['porche','principal'])
        self.assertEqual(self.payloads()[0]['attributes']['camaras_en_sabotaje'],['principal'])
        camera['enabled']=False
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera,camera]))
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'off')
        self.assertEqual(self.payloads()[-1]['attributes']['camaras_habilitadas'],['porche'])

    def test_disabling_checkbox_immediately_hides_existing_alert_without_erasing_history(self):
        camera={**self.camera,'enabled':False}
        self.app.CAMERAS_FILE.write_text(json.dumps([camera]))
        self.app.set_rt('porche',last_ok=self.app.time.time(),changed=True,
                        census={'status':'scene_change','observed_scene_loss':True})
        self.app.get_alarm_latches().states['porche']=True
        client=self.app.app.test_client()
        status=client.get('/api/status').get_json()['cameras'][0]
        self.assertFalse(status['alarms_enabled'])
        self.assertFalse(status['changed'])
        self.assertTrue(status['census']['observed_scene_loss'])
        self.assertTrue(self.app.get_rt('porche')['changed'])
        self.assertTrue(self.app.get_alarm_latches().get('porche'))
        page=client.get('/').get_data(as_text=True)
        self.assertIn('Avisos Census desactivados',page)
        self.assertNotIn('class="badge danger alarm"',page)
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'off')
        camera['enabled']=True
        self.app.CAMERAS_FILE.write_text(json.dumps([camera]))
        self.assertTrue(client.get('/api/status').get_json()['cameras'][0]['changed'])
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'on')

    def test_principal_uses_its_references_and_confirms_real_scene_loss(self):
        import shutil
        camera={**self.camera,'slug':'principal','name':'principal','enabled':True}
        self.app.CAMERAS_FILE.write_text(json.dumps([camera]))
        directory=self.app.camera_dir('principal')
        for kind in ('day','night'):
            shutil.copyfile(self.app.image_path('porche',kind),self.app.image_path('principal',kind))
        current=self.app.image_path('principal','current')
        cv2.imwrite(str(current),scene())
        with patch.object(self.app,'capture_frame'):
            self.app.process_camera(camera)
            result=self.app.get_rt('principal')
            self.assertEqual(result['census']['comparison_mode'],'day_night_references')
            self.assertTrue(result['alarm_ready'])
            cv2.imwrite(str(current),scene(99))
            self.app.process_camera(camera)
            self.app.process_camera(camera)
            self.assertTrue(self.app.get_rt('principal')['changed'])
            self.assertEqual(self.payloads()[-1]['state'],'on')
            self.assertEqual(self.payloads()[-1]['attributes']['camaras_en_sabotaje'],['principal'])
            cv2.imwrite(str(current),scene())
            self.app.process_camera(camera)
            self.assertEqual(self.payloads()[-1]['state'],'off')

    def test_startup_unknown_cannot_clear_an_existing_homeassistant_alarm(self):
        self.app.update_sabotaje_cam()
        self.http.assert_not_called()
        self.assertEqual(self.app.last_ha_publication['reason'],'awaiting_reference_validation')

    def test_unconfigured_cameras_do_not_block_on_to_off_after_return(self):
        unconfigured={**self.camera,'slug':'lateral','name':'lateral','enabled':True}
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera,unconfigured]))
        self.app.get_alarm_latches().update('porche', {
            'comparison_mode':'day_night_references','reliable':True,
            'scene_change':True,'observed_scene_loss':True})
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'on')
        self.assertEqual(self.payloads()[-1]['attributes']['camaras_pendientes_referencias'],['lateral'])
        self.app.get_alarm_latches().update('porche', {
            'comparison_mode':'day_night_references','reliable':True,'status':'stable'})
        self.app.set_rt('porche',alarm_ready=True)
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'off')
        self.assertEqual(self.payloads()[-1]['attributes']['camaras_en_sabotaje'],[])

    def test_two_nights_keep_both_light_states_off_and_clear_confirmed_alarm(self):
        cv2.imwrite(str(self.app.image_path('porche','night_alt')),scene(73))
        with patch.object(self.app,'capture_frame'):
            for image in (scene(42),scene(73),scene(42)):
                cv2.imwrite(str(self.current),image)
                self.app.process_camera(self.camera)
                self.assertFalse(self.app.get_rt('porche')['changed'])
            cv2.imwrite(str(self.current),scene(99))
            self.app.process_camera(self.camera)
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'],'on')
            cv2.imwrite(str(self.current),scene(73))
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'],'off')

    def test_one_reference_can_publish_alarm_and_return_clears_it(self):
        self.app.image_path('porche', 'day').unlink()
        with patch.object(self.app, 'capture_frame'):
            cv2.imwrite(str(self.current), scene(42))
            self.app.process_camera(self.camera)
            self.assertTrue(self.app.get_rt('porche')['alarm_ready'])
            self.assertEqual(self.payloads()[-1]['state'], 'off')
            cv2.imwrite(str(self.current), scene(99))
            self.app.process_camera(self.camera)
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'], 'on')
            cv2.imwrite(str(self.current), scene(42))
            self.app.process_camera(self.camera)
            self.assertEqual(self.payloads()[-1]['state'], 'off')

    def test_missing_references_do_not_hide_a_previously_confirmed_alarm(self):
        self.app.get_alarm_latches().states['porche']=True
        for kind in ('day','night'):
            self.app.image_path('porche',kind).unlink()
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'on')
        self.assertEqual(self.payloads()[-1]['attributes']['camaras_en_sabotaje'],['porche'])

    def test_other_configured_camera_must_be_validated_before_clearing(self):
        import shutil
        other={**self.camera,'slug':'principal','name':'principal','enabled':True}
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera,other]))
        for kind in ('day','night'):
            shutil.copyfile(self.app.image_path('porche',kind),self.app.image_path('principal',kind))
        self.app.set_rt('porche',alarm_ready=True)
        self.app.update_sabotaje_cam()
        self.http.assert_not_called()
        self.app.set_rt('principal',alarm_ready=True)
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[-1]['state'],'off')

    def test_persisted_loss_publishes_on_before_initial_capture(self):
        self.app.get_alarm_latches().states['porche']=True
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[0]['state'],'on')

    def test_failed_publication_is_retried_and_does_not_acknowledge_success(self):
        self.app.set_rt('porche',alarm_ready=True)
        self.http.side_effect=RuntimeError('private')
        self.assertFalse(self.app.update_sabotaje_cam()['published'])
        self.assertNotIn('private',json.dumps(self.app.last_ha_publication))
        self.http.side_effect=None
        self.assertTrue(self.app.update_sabotaje_cam()['published'])

    def test_disabling_publication_prevents_requests(self):
        self.app.OPTIONS_FILE.write_text('{}')
        self.app.set_rt('porche',alarm_ready=True)
        self.app.update_sabotaje_cam()
        self.http.assert_not_called()

    def test_disabled_camera_does_not_publish_its_latched_alarm(self):
        self.app.CAMERAS_FILE.write_text(json.dumps([{**self.camera,'enabled':False}]))
        self.app.get_alarm_latches().states['porche']=True
        self.app.update_sabotaje_cam()
        self.assertEqual(self.payloads()[0]['state'],'off')

    def test_corrupt_camera_configuration_cannot_clear_the_sensor(self):
        self.app.CAMERAS_FILE.write_text('{')
        self.assertFalse(self.app.update_sabotaje_cam()['published'])
        self.http.assert_not_called()
