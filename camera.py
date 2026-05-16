import os
import io
import json
import time
import statistics
import threading
import subprocess
from datetime import datetime

PHOTOS_DIR = os.path.expanduser('~/photos')

# Gain → ISO (approximate for IMX296, base ISO ≈ 100)
def gain_to_iso(gain):
    return int(round(gain * 100 / 50) * 50)   # round to nearest 50


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

    def _wait_preview_stop(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._proc_lock:
                if self._preview_proc is None:
                    break
            time.sleep(0.05)
        time.sleep(0.1)

    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame

    def is_connected(self):
        return self._connected

    def _build_cmd(self, filepath, shutter_us, gain, awb,
                   saturation=1.0, sharpness=1.5, contrast=1.0, quality=95):
        cmd = [
            'rpicam-still', '-n', '-o', filepath,
            '--shutter', str(shutter_us),
            '--gain', str(gain),
            '--awb', awb,
            '--denoise', 'off',
            '--saturation', str(saturation),
            '--sharpness', str(sharpness),
            '--contrast', str(contrast),
            '--quality', str(quality),
            '-t', '200',
        ]
        return cmd

    def capture(self, params, gps_data=None):
        shutter_ms   = float(params.get('shutter',    5000))
        gain         = float(params.get('gain',       8))
        awb          = params.get('awb',              'auto')
        count        = min(int(params.get('count',    1)), 20)
        interval     = float(params.get('interval',   1))
        saturation   = float(params.get('saturation', 1.0))
        sharpness    = float(params.get('sharpness',  1.5))
        contrast     = float(params.get('contrast',   1.0))
        quality      = int(params.get('quality',      95))

        os.makedirs(PHOTOS_DIR, exist_ok=True)
        saved, gps_saved, last_error = [], False, None

        self._capturing = True
        self._wait_preview_stop()

        try:
            with self._capture_lock:
                for i in range(count):
                    captured_at = datetime.now()
                    ts = captured_at.strftime('%Y%m%d_%H%M%S')
                    filename = f'star_{ts}_{i+1:03d}.jpg'
                    filepath = os.path.join(PHOTOS_DIR, filename)

                    shutter_us = int(shutter_ms * 1000)
                    cmd = self._build_cmd(filepath, shutter_us, gain, awb,
                                         saturation, sharpness, contrast, quality)
                    try:
                        result = subprocess.run(cmd, capture_output=True,
                                                timeout=shutter_ms / 1000 + 30)
                        if result.returncode == 0 and os.path.exists(filepath):
                            saved.append(filename)
                            metadata = {
                                'captured_at': captured_at.isoformat(timespec='seconds'),
                                'camera': {
                                    'shutter_ms': shutter_ms,
                                    'shutter_us': shutter_us,
                                    'gain': gain,
                                    'iso_equiv': gain_to_iso(gain),
                                    'awb': awb,
                                    'saturation': saturation,
                                    'sharpness': sharpness,
                                    'contrast': contrast,
                                    'quality': quality,
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
                            last_error = result.stderr.decode('utf-8', errors='ignore').strip() or 'capture failed'
                    except subprocess.TimeoutExpired:
                        last_error = 'timeout'

                    if i < count - 1:
                        time.sleep(interval)
        finally:
            self._capturing = False

        return {
            'saved': saved, 'count': len(saved),
            'gps_saved': gps_saved,
            'error': last_error if not saved else None,
        }

    # ── Auto-calibration ─────────────────────────────────────────
    def analyze_jpeg(self, filepath):
        """Score an image: more stars & less noise = higher score."""
        try:
            from PIL import Image
            img = Image.open(filepath).convert('L')
            # Downsample for speed on RPi 3
            w, h = img.size
            scale = min(1.0, 640 / max(w, h))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
            pixels = list(img.getdata())
            n = len(pixels)
            avg = sum(pixels) / n
            # Background noise = stdev of darker half
            dark = sorted(pixels)[:n // 2]
            noise = statistics.stdev(dark) if len(dark) > 2 else 1.0
            # Stars = pixels clearly above background
            threshold = min(avg + 3 * noise, 245)
            star_count = sum(1 for p in pixels if p > threshold)
            return {
                'avg': round(avg, 1),
                'noise': round(noise, 1),
                'stars': star_count,
                'score': round(star_count / (noise + 1), 2),
            }
        except Exception:
            # Fallback: larger file = more detail = more stars (crude)
            size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            return {'avg': 0, 'noise': 0, 'stars': 0, 'score': size / 1000}

    def calibrate(self, shutter_ms=3000):
        """
        Test gain 2/4/8/16 at fixed shutter.
        Returns per-gain scores and the recommended gain.
        """
        gains = [2, 4, 8, 16]
        results = []
        tmpdir = '/tmp/starcam_cal'
        os.makedirs(tmpdir, exist_ok=True)

        self._capturing = True
        self._wait_preview_stop()

        try:
            with self._capture_lock:
                for gain in gains:
                    filepath = os.path.join(tmpdir, f'cal_g{gain}.jpg')
                    shutter_us = int(shutter_ms * 1000)
                    cmd = self._build_cmd(filepath, shutter_us, gain, 'auto')
                    try:
                        r = subprocess.run(cmd, capture_output=True,
                                           timeout=shutter_ms / 1000 + 15)
                        if r.returncode == 0 and os.path.exists(filepath):
                            stats = self.analyze_jpeg(filepath)
                            os.remove(filepath)
                        else:
                            stats = {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                    except Exception:
                        stats = {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                    results.append({'gain': gain, 'iso': gain_to_iso(gain), **stats})
        finally:
            self._capturing = False

        if not results:
            return {'error': 'calibration failed', 'results': []}

        best = max(results, key=lambda r: r['score'])
        return {
            'results': results,
            'recommended': {
                'gain': best['gain'],
                'shutter_ms': shutter_ms,
                'note': '별이 보이면 노출을 5~30초로 늘려보세요',
            },
        }
