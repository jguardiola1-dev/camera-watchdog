import concurrent.futures
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_orb_detector as fixtures


class CaptureAndLoggingTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LegacyIsolationTests.setUpClass.__func__)

    def setUp(self):
        fixtures.LegacyIsolationTests.setUp(self)
        self.camera['rtsp_url']='rtsp://camera.invalid/stream'
        self.app.CAMERAS_FILE.write_text(json.dumps([self.camera]))
        self.current=self.app.image_path('porche','current')
        self.jpeg=self.current.read_bytes()

    def ffmpeg(self, command, **kwargs):
        Path(command[-1]).write_bytes(self.jpeg)
        return SimpleNamespace(returncode=0)

    def test_manual_capture_waits_for_same_camera_analysis_but_other_camera_can_capture(self):
        entered, release, manual_queued = threading.Event(), threading.Event(), threading.Event()
        manual_ffmpeg=threading.Event()
        errors=[]
        original_lock=self.app.camera_operation_lock
        def lock(slug):
            if threading.current_thread().name=='manual': manual_queued.set()
            return original_lock(slug)
        def geometry(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test timeout')
        def ffmpeg(command, **kwargs):
            if threading.current_thread().name=='manual': manual_ffmpeg.set()
            return self.ffmpeg(command, **kwargs)
        def run(function):
            try: function()
            except BaseException as error: errors.append(error)
        self.addCleanup(release.set)
        with patch.object(self.app,'camera_operation_lock',side_effect=lock), \
             patch.object(self.app.subprocess,'run',side_effect=ffmpeg), \
             patch.object(self.app,'observe_geometry',side_effect=geometry), \
             patch.object(self.app,'observe_census',return_value={'status':'stable'}), \
             patch.object(self.app,'record_observation'), \
             patch.object(self.app,'image_stats',return_value=(100,40,30)):
            periodic=threading.Thread(target=lambda:run(lambda:self.app.process_camera(self.camera)),name='periodic')
            manual=threading.Thread(target=lambda:run(lambda:self.app.app.test_client().post('/camera/porche/capture-now')),name='manual')
            try:
                periodic.start()
                self.assertTrue(entered.wait(3))
                manual.start()
                self.assertTrue(manual_queued.wait(3))
                self.assertFalse(manual_ffmpeg.is_set())
                other={**self.camera,'slug':'lateral'}
                self.app.capture_frame(other,self.app.image_path('lateral','current'))
                self.assertTrue(self.app.get_rt('lateral')['last_capture_at'])
            finally:
                release.set()
                periodic.join(5)
                if manual.ident is not None: manual.join(5)
            self.assertFalse(periodic.is_alive())
            self.assertFalse(manual.is_alive())
            self.assertTrue(manual_ffmpeg.is_set())
            self.assertEqual(errors,[])

    def test_manual_capture_updates_image_time_without_claiming_new_analysis(self):
        self.app.set_rt('porche',last_ok=time.time()-400)
        previous=self.app.get_rt('porche')['last_ok']
        with patch.object(self.app.subprocess,'run',side_effect=self.ffmpeg):
            response=self.app.app.test_client().post('/camera/porche/capture-now')
        self.assertEqual(response.status_code,302)
        state=self.app.get_rt('porche')
        self.assertGreater(state['last_capture_at'],previous)
        self.assertEqual(state['last_ok'],previous)
        status=self.app.app.test_client().get('/api/status').get_json()['cameras'][0]
        self.assertEqual(status['last_capture'],time.strftime('%H:%M:%S',time.localtime(state['last_capture_at'])))
        self.assertEqual(self.app.app.test_client().post('/camera/porche/reference/day').status_code,302)

    def test_failed_capture_preserves_existing_image_and_timestamp(self):
        self.app.set_rt('porche',last_capture_at=100)
        def fail(command, **kwargs):
            Path(command[-1]).write_bytes(b'partial')
            return SimpleNamespace(returncode=1,stderr=b'capture failed')
        with patch.object(self.app.subprocess,'run',side_effect=fail):
            with self.assertRaises(RuntimeError): self.app.capture_frame(self.camera,self.current)
        self.assertEqual(self.current.read_bytes(),self.jpeg)
        self.assertEqual(self.app.get_rt('porche')['last_capture_at'],100)
        self.assertFalse(self.current.with_suffix('.tmp.jpg').exists())

    def test_concurrent_logging_preserves_whole_lines(self):
        messages=['camera-%d event-%d'%(camera,event) for camera in range(6) for event in range(40)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(self.app.write_watchdog_log,messages))
        self.assertCountEqual(self.app.LOG_FILE.read_text().splitlines(),messages)

    def test_logging_rotates_with_bounded_size(self):
        with patch.object(self.app,'WATCHDOG_LOG_MAX_BYTES',256):
            for i in range(100): self.app.write_watchdog_log('event-%03d '%i+'x'*30)
        files=list(self.app.DATA_DIR.glob('watchdog.log*'))
        self.assertEqual(len(files),4)
        self.assertTrue(all(p.stat().st_size<=256 for p in files))
        self.assertIn('event-099',self.app.LOG_FILE.read_text())
