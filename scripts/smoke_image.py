"""Run inside a disposable image; never connects to cameras or Home Assistant."""
import platform
import subprocess

import cv2
import numpy as np
from PIL import Image

import app
from census_detector import compare_scene

assert subprocess.run(['ffmpeg', '-version'], capture_output=True).returncode == 0
image = np.random.default_rng(23).integers(0, 256, (360, 640), dtype=np.uint8)
comparison = compare_scene(image, image)
assert comparison['reliable'] and comparison['status'] == 'stable', comparison
client = app.app.test_client()
assert client.get('/').status_code == 200
assert client.get('/sample-history').status_code == 200
assert client.get('/api/status').get_json()['cameras'] == []
assert client.post('/sample-history/pause').status_code == 302
assert client.post('/sample-history/pause-diagnostics').status_code == 302
print('Runtime OK:', platform.machine(), 'OpenCV', cv2.__version__, 'Pillow', Image.__version__)
