import os
import json
import time
import threading
import subprocess
from datetime import datetime

PHOTOS_DIR = os.path.expanduser('~/photos')


class Camera:
    def __init__(self):
        self._frame_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._latest_frame = None
        self._capturing = False
        self._connected = False

    def start(self):
        t = threading.Thread(target=self._preview_loop, daemon=True)
        t.start()

    def _preview_loop(self):
        while True:
            if self._capturing:
                time.sleep(0.2)
                continue
            try:
                result = subprocess.run(
                    ['rpicam-jpeg', '-n', '-o', '-',
                     '--width', '640', '--height', '480',
                     '-t', '500', '--quality', '50'],
                    capture_output=True, timeout=15,
                )
                if result.returncode == 0 and result.stdout:
                    with self._frame_lock:
                        self._latest_frame = result.stdout
                    self._connected = True
                else:
                    self._connected = False
                    time.sleep(2)
            except Exception:
                self._connected = False
                time.sleep(2)
            time.sleep(0.5)

    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame

    def is_connected(self):
        return self._connected

    def capture(self, params, gps_data=None):
        shutter_ms = float(params.get('shutter', 2000))   # milliseconds
        gain = float(params.get('gain', 1))
        awb = params.get('awb', 'auto')
        count = min(int(params.get('count', 1)), 20)
        interval = float(params.get('interval', 1))

        os.makedirs(PHOTOS_DIR, exist_ok=True)
        saved = []
        gps_saved = False
        last_error = None

        self._capturing = True
        time.sleep(0.6)  # let preview thread exit its current subprocess

        try:
            with self._capture_lock:
                for i in range(count):
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    filename = f'star_{ts}_{i+1:03d}.jpg'
                    filepath = os.path.join(PHOTOS_DIR, filename)

                    shutter_us = int(shutter_ms * 1000)
                    cmd = [
                        'rpicam-still', '-n',
                        '-o', filepath,
                        '--shutter', str(shutter_us),
                        '--gain', str(gain),
                        '--awb', awb,
                        '-t', '500',
                    ]
                    timeout_sec = shutter_ms / 1000 + 30
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, timeout=timeout_sec
                        )
                        if result.returncode == 0 and os.path.exists(filepath):
                            saved.append(filename)
                            if gps_data:
                                sidecar = filepath.replace('.jpg', '.json')
                                with open(sidecar, 'w') as f:
                                    json.dump(gps_data, f, indent=2)
                                gps_saved = True
                        else:
                            last_error = result.stderr.decode('utf-8', errors='ignore').strip()
                    except subprocess.TimeoutExpired:
                        last_error = 'timeout'

                    if i < count - 1:
                        time.sleep(interval)
        finally:
            self._capturing = False

        return {
            'saved': saved,
            'count': len(saved),
            'gps_saved': gps_saved,
            'error': last_error if not saved else None,
        }
