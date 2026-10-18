import json
import time
import unittest
from unittest.mock import patch

import cv2
import test_orb_detector as fixtures
from test_orb_detector import scene


class ReferenceControlsTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LegacyIsolationTests.setUpClass.__func__)

    def setUp(self):
        fixtures.LegacyIsolationTests.setUp(self)
        self.camera = {**self.camera, 'slug': 'principal', 'name': 'principal'}
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.current = self.app.image_path('principal', 'current')
        cv2.imwrite(str(self.current), scene())
        self.app.set_rt('principal', last_ok=time.time(), alarm_ready=True)
        self.client = self.app.app.test_client()
        self.directory = self.app.camera_dir('principal')

    def test_save_updates_one_shared_file_without_clearing_confirmed_alarm(self):
        legacy = self.app.image_path('principal', 'day')
        cv2.imwrite(str(legacy), scene(42))
        self.app.get_alarm_latches().states['principal'] = True
        with patch.object(self.app, 'update_sabotaje_cam') as publish:
            response = self.client.post('/camera/principal/reference/day')
            self.assertEqual(response.status_code, 302)
            publish.assert_not_called()
        self.assertEqual(legacy.read_bytes(), self.current.read_bytes())
        self.assertFalse((self.directory/'census_references').exists())
        self.assertFalse(self.app.image_path('principal','night').exists())
        self.assertFalse(self.app.get_rt('principal')['alarm_ready'])
        self.assertTrue(self.app.get_alarm_latches().get('principal'))

    def test_saved_reference_is_immediately_shared_without_import(self):
        legacy = self.app.image_path('principal', 'night')
        cv2.imwrite(str(legacy), scene(42))
        page = self.client.get('/camera/principal').get_data(as_text=True)
        self.assertIn('Referencias de la cámara', page)
        self.assertNotIn('/import', page)
        self.assertNotIn('Guardar para el detector anterior</button>', page.split('Controles del diagnóstico anterior')[0])
        self.assertEqual(self.client.get('/image/principal/night').data,
                         self.client.get('/image/principal/census-night').data)
        self.assertFalse((self.directory/'census_references').exists())
        cv2.imwrite(str(self.current),scene(42))
        result=self.app.observe_census(self.camera,self.current)
        self.assertEqual(result['comparison_mode'],'day_night_references')
        self.assertEqual(result['status'],'stable')
        self.assertEqual(result['reference'],'night')

    def test_invalid_stale_and_unknown_references_are_rejected(self):
        for url in ('/camera/unknown/census-reference/day',
                    '/camera/principal/census-reference/fixed',
                    '/camera/principal/census-reference/day/import'):
            self.assertIn(self.client.post(url).status_code, (400, 404))
        self.app.set_rt('principal', last_ok=time.time()-200)
        self.assertEqual(self.client.post('/camera/principal/census-reference/day').status_code, 400)
        self.app.set_rt('principal', last_ok=time.time())
        self.current.write_bytes(b'not an image')
        self.assertEqual(self.client.post('/camera/principal/census-reference/day').status_code, 400)
        self.assertFalse(self.app.image_path('principal','day').exists())

    def test_delete_only_removes_requested_census_reference(self):
        for kind in ('day', 'night'):
            self.client.post('/camera/principal/reference/'+kind)
        response = self.client.post('/camera/principal/reference/night/delete')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.app.image_path('principal','day').exists())
        self.assertFalse(self.app.image_path('principal','night').exists())
        self.assertEqual(self.client.get('/image/principal/night').status_code,404)
        self.assertEqual(self.client.get('/image/principal/census-night').status_code,404)

    def test_both_observers_receive_the_same_day_and_night_files(self):
        for kind in ('day','night','night_alt'):
            self.client.post('/camera/principal/reference/'+kind)
        with patch.object(self.app.orb_observer,'observe',return_value={}) as observe:
            self.app.observe_geometry(self.camera,self.current)
        references=observe.call_args.args[2]
        result=self.app.observe_census(self.camera,self.current)
        import hashlib
        for kind in ('day','night','night_alt'):
            self.assertEqual(references[kind],self.app.image_path('principal',kind))
            expected=kind+'-'+hashlib.sha256(references[kind].read_bytes()).hexdigest()
            self.assertEqual(result['reference_comparisons'][kind]['reference_id'],expected)

    def test_optional_night_has_shared_controls_and_can_be_deleted_independently(self):
        for kind in ('day','night','night_alt'):
            self.assertEqual(self.client.post('/camera/principal/reference/'+kind).status_code,302)
        page=self.client.get('/camera/principal').get_data(as_text=True)
        self.assertIn('Noche 1',page)
        self.assertIn('Noche 2',page)
        self.assertEqual(self.client.get('/image/principal/night_alt').data,self.current.read_bytes())
        self.client.post('/camera/principal/reference/night_alt/delete')
        self.assertFalse(self.app.image_path('principal','night_alt').exists())
        self.assertTrue(self.app.image_path('principal','night').exists())

    def test_capacity_holes_and_all_ten_shared_images(self):
        from reference_catalog import REFERENCE_GROUPS, REFERENCE_KINDS
        import hashlib
        for group, kinds in REFERENCE_GROUPS.items():
            for kind in kinds:
                response = self.client.post('/camera/principal/reference-group/'+group+'/add')
                self.assertEqual(response.status_code, 302)
                self.assertTrue(self.app.image_path('principal', kind).is_file())
            before = {kind: self.app.image_path('principal', kind).read_bytes() for kind in kinds}
            self.assertEqual(self.client.post('/camera/principal/reference-group/'+group+'/add').status_code, 409)
            self.assertEqual(before, {kind: self.app.image_path('principal', kind).read_bytes() for kind in kinds})
        with patch.object(self.app.orb_observer, 'observe', return_value={}) as observe:
            self.app.observe_geometry(self.camera, self.current)
        references = observe.call_args.args[2]
        result = self.app.observe_census(self.camera, self.current)
        for kind in REFERENCE_KINDS:
            self.assertEqual(self.client.get('/image/principal/'+kind).data, self.current.read_bytes())
            self.assertEqual(result['reference_comparisons'][kind]['reference_id'],
                             kind+'-'+hashlib.sha256(references[kind].read_bytes()).hexdigest())
        self.client.post('/camera/principal/reference/day_3/delete')
        cv2.imwrite(str(self.current), scene(77))
        self.assertEqual(self.client.post('/camera/principal/reference-group/day/add').status_code, 302)
        self.assertEqual(self.app.image_path('principal', 'day_3').read_bytes(), self.current.read_bytes())
        self.assertNotEqual(self.app.image_path('principal', 'day_4').read_bytes(), self.current.read_bytes())
        page = self.client.get('/camera/principal').get_data(as_text=True)
        self.assertEqual(page.count('5 / 5 imágenes'), 2)
        self.assertNotIn('/reference-group/day/add', page)
        self.assertEqual(self.client.post('/camera/principal/reference/day_6').status_code, 400)
        self.assertEqual(self.client.post('/camera/principal/reference-group/unknown/add').status_code, 400)

    def test_concurrent_adds_cannot_overfill_or_overwrite(self):
        from concurrent.futures import ThreadPoolExecutor
        from reference_catalog import saved_reference_paths
        def add(_):
            with self.app.app.test_client() as client:
                return client.post('/camera/principal/reference-group/night/add').status_code
        with ThreadPoolExecutor(max_workers=8) as executor:
            codes = list(executor.map(add, range(10)))
        self.assertEqual(codes.count(302), 5)
        self.assertEqual(codes.count(409), 5)
        self.assertEqual(len(saved_reference_paths(self.directory)), 5)
