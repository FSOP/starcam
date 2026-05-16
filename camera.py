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
        self._proc_lock = threading.Lock()
        self._latest_frame = None
        self._capturing = False
        self._connected = False
        self._preview_proc = None

    def start(self):
        t = threading.Thread(target=self._preview_loop, daemon=True)
        t.start()

    def _preview_loop(self):
        while True:
            if self._capturing:
                time.sleep(0.1)
                continue
            try:
                proc = subprocess.Popen(
                    ['rpicam-jpeg', '-n', '-o', '-',
                     '--width', '640', '--height', '480',
                     '-t', '500', '--quality', '50'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                with self._proc_lock:
                    self._preview_proc = proc

                # Poll so we can kill it immediately when capture starts
                while proc.poll() is None:
                    if self._capturing:
                        proc.kill()
                        break
                    time.sleep(0.05)

                stdout, _ = proc.communicate()
                with self._proc_lock:
                    self._preview_proc = None

                if not self._capturing and proc.returncode == 0 and stdout:
                    with self._frame_lock:
                        self._latest_frame = stdout
                    self._connected = True
                elif not self._capturing:
                    self._connected = False
                    time.sleep(1)
            except Exception:
                self._connected = False
                with self._proc_lock:
                    self._preview_proc = None
                time.sleep(2)

            if not self._capturing:
                time.sleep(0.3)

    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame

    def is_connected(self):
        return self._connected

    def capture(self, params, gps_data=None):
        shutter_ms = float(params.get('shutter', 5000))
        gain = float(params.get('gain', 8))
        awb = params.get('awb', 'auto')
        count = min(int(params.get('count', 1)), 20)
        interval = float(params.get('interval', 1))

        os.makedirs(PHOTOS_DIR, exist_ok=True)
        saved = []
        gps_saved = False
        last_error = None

        self._capturing = True

        # Wait for preview process to actually die (up to 3s)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._proc_lock:
                if self._preview_proc is None:
                    break
            time.sleep(0.05)
        time.sleep(0.1)

        try:
            with self._capture_lock:
                for i in range(count):
                    captured_at = datetime.now()
                    ts = captured_at.strftime('%Y%m%d_%H%M%S')
                    filename = f'star_{ts}_{i+1:03d}.jpg'
                    filepath = os.path.join(PHOTOS_DIR, filename)

                    shutter_us = int(shutter_ms * 1000)
                    cmd = [
                        'rpicam-still', '-n',
                        '-o', filepath,
                        '--shutter', str(shutter_us),
                        '--gain', str(gain),
                        '--awb', awb,
                        '--denoise', 'off',   # 별이 뭉개지지 않게 노이즈 제거 끔
                        '-t', '200',
                    ]
                    timeout_sec = shutter_ms / 1000 + 30
                    try:
                        result = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
                        if result.returncode == 0 and os.path.exists(filepath):
                            saved.append(filename)
                            metadata = {
                                'captured_at': captured_at.isoformat(timespec='seconds'),
                                'camera': {
                                    'shutter_ms': shutter_ms,
                                    'shutter_us': shutter_us,
                                    'gain': gain,
                                    'awb': awb,
                                    'sequence': i + 1,
                                    'total': count,
                                    'interval_s': interval,
                                },
                            }
                            if gps_data:
                                metadata['gps'] = gps_data
                                gps_saved = True
                            with open(filepath.replace('.jpg', '.json'), 'w') as f:
                                json.dump(metadata, f, indent=2, ensure_ascii=False)
                        else:
                            stderr = result.stderr.decode('utf-8', errors='ignore').strip()
                            last_error = stderr or 'capture failed'
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
