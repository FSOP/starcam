import os
import io
import json
import time
import statistics
import threading
import subprocess
import collections
import queue
from datetime import datetime, timedelta, timezone

try:
    from picamera2 import Picamera2
    _HAS_PICAMERA2 = True
except ImportError:
    _HAS_PICAMERA2 = False

try:
    import numpy as np
    from PIL import Image, ImageFilter
    _HAS_IMGLIB = True
except ImportError:
    _HAS_IMGLIB = False

PHOTOS_DIR = os.path.expanduser('~/photos')

AWB_MODE = {
    'auto': 0, 'daylight': 5, 'cloudy': 6,
}

def gain_to_iso(gain):
    return int(round(gain * 100 / 50) * 50)


class Camera:
    def __init__(self):
        self._frame_lock    = threading.Lock()
        self._state_lock    = threading.Lock()
        self._cam_op_lock   = threading.Lock()
        self._proc_lock     = threading.Lock()
        self._capture_mutex = threading.Lock()

        self._latest_frame   = None
        self._frame_seq      = 0
        self._capturing      = False
        self._preview_max_ms = 1000
        self._connected      = False

        # auto-trigger
        self._trig_enabled   = False
        self._trig_threshold = 30
        self._trig_cooldown  = 15.0
        self._trig_last      = 0.0
        self._trig_callback  = None
        self._trig_hit_count = 0
        self._trig_thread    = None
        self._trig_capturing = False
        self._video_proc     = None
        self._video_filename = ''
        self._video_started  = 0.0
        self._burst_enabled      = False
        self._burst_count        = 0
        self._burst_started      = 0.0
        self._burst_duration     = 0
        self._burst_thread       = None
        self._burst_gps_callback = None

        # periodic capture
        self._per_enabled   = False
        self._per_interval  = 600      # seconds
        self._per_callback  = None
        self._per_shot_count = 0
        self._per_next      = 0.0
        self._per_thread    = None
        self._enabled        = True

        self._cam            = None
        self._preview_config = None
        self._preview_proc   = None

        # scout mode
        self._scout_enabled      = False
        self._scout_scheduled    = False
        self._scout_frame_count  = 0
        self._scout_detect_count = 0
        self._scout_params       = {}
        self._scout_callback     = None
        self._scout_thread       = None
        self._scout_last_result  = None
        self._scout_stop_at      = None  # datetime (UTC) to auto-stop
        self._scout_start_at     = None  # datetime (UTC) to auto-start
        self._scout_pending      = {}    # params stored while waiting for start_at
        self._scout_gps_callback = None  # GPS fix callback for ring frames
        # Static angle history: reject repeating angles (clouds / Milky Way)
        _RING_STATIC_WIN = 4
        self._ring_angle_hist = collections.deque(maxlen=_RING_STATIC_WIN)

        # FPN correction toggle (프리뷰 + 저장 사진 양쪽에 적용)
        self._fpn_enabled = True

    def get_fpn_enabled(self) -> bool:
        return self._fpn_enabled

    def set_fpn_enabled(self, enabled: bool):
        self._fpn_enabled = bool(enabled)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    # ── Main loop ─────────────────────────────────────────────────
    def _run(self):
        while True:
            if not self._enabled:
                self._stop_all()
                time.sleep(0.5)
                continue
            if _HAS_PICAMERA2:
                self._run_picamera2()
            else:
                self._run_subprocess_preview()
            if self._enabled:
                time.sleep(2)

    def _stop_all(self):
        with self._state_lock:
            cam = self._cam
        if cam is not None:
            self._close_cam(cam)
            with self._state_lock:
                self._cam = None
                self._preview_config = None
        with self._proc_lock:
            proc = self._preview_proc
        if proc is not None:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            with self._proc_lock:
                self._preview_proc = None
        self._connected = False

    # ── picamera2 preview loop ────────────────────────────────────
    def _run_picamera2(self):
        cam = None
        try:
            cam = Picamera2()
            preview_config = cam.create_preview_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                controls={
                    "FrameDurationLimits": (100000, self._preview_max_ms * 1000),
                    "AeFlickerMode":   1,     # 수동 플리커 보정
                    "AeFlickerPeriod": 8333,  # 60 Hz (한국 전원 주파수)
                },
            )
            cam.configure(preview_config)
            cam.start()
            time.sleep(0.5)

            with self._state_lock:
                self._cam = cam
                self._preview_config = preview_config
                self._connected = True

            while True:
                if not self._enabled:
                    break
                if self._capturing:
                    time.sleep(0.05)
                    continue
                with self._cam_op_lock:
                    if not self._enabled or self._capturing:
                        continue
                    try:
                        arr = cam.capture_array()
                        arr = self._fpn_correct_array(arr)
                        buf = io.BytesIO()
                        Image.fromarray(arr).save(buf, format='jpeg', quality=40)
                        with self._frame_lock:
                            self._latest_frame = buf.getvalue()
                            self._frame_seq += 1
                    except Exception:
                        break
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            with self._state_lock:
                self._cam = None
                self._preview_config = None
                self._connected = False
            if cam is not None:
                self._close_cam(cam)

    def _close_cam(self, cam):
        for fn in (cam.stop, cam.close):
            try:
                fn()
            except Exception:
                pass

    # ── subprocess preview loop ───────────────────────────────────
    def _run_subprocess_preview(self):
        while self._enabled:
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
                    if self._capturing or not self._enabled:
                        proc.kill()
                        break
                    time.sleep(0.05)
                stdout, _ = proc.communicate()
                with self._proc_lock:
                    self._preview_proc = None
                if not self._capturing and proc.returncode == 0 and stdout:
                    frame = self._fpn_correct_jpeg(stdout, quality=50)
                    with self._frame_lock:
                        self._latest_frame = frame
                        self._frame_seq += 1
                    self._connected = True
                elif not self._capturing:
                    self._connected = False
                    time.sleep(1)
            except Exception:
                self._connected = False
                with self._proc_lock:
                    self._preview_proc = None
                time.sleep(2)
            if self._enabled and not self._capturing:
                time.sleep(0.3)

        with self._proc_lock:
            proc = self._preview_proc
        if proc is not None:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            with self._proc_lock:
                self._preview_proc = None
        self._connected = False

    def _wait_preview_stop(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._proc_lock:
                if self._preview_proc is None:
                    break
            time.sleep(0.05)
        time.sleep(0.1)

    # ── Public ────────────────────────────────────────────────────
    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame

    def get_frame_with_seq(self):
        with self._frame_lock:
            return self._latest_frame, self._frame_seq

    def get_preview_max_ms(self):
        return self._preview_max_ms

    def set_preview_max_ms(self, ms):
        ms = max(200, min(10000, int(ms)))
        self._preview_max_ms = ms
        with self._state_lock:
            cam = self._cam
        if cam is not None:
            try:
                cam.set_controls({'FrameDurationLimits': (100000, ms * 1000)})
            except Exception:
                pass

    def set_auto_trigger(self, enabled, threshold=30, callback=None):
        self._trig_threshold = max(5, min(100, int(threshold)))
        self._trig_callback  = callback
        self._trig_enabled   = enabled
        if enabled and _HAS_IMGLIB:
            if self._trig_thread is None or not self._trig_thread.is_alive():
                self._trig_thread = threading.Thread(
                    target=self._trig_loop, daemon=True, name='autotrig')
                self._trig_thread.start()

    def get_auto_trigger_status(self):
        return {
            'enabled':    self._trig_enabled,
            'threshold':  self._trig_threshold,
            'hit_count':  self._trig_hit_count,
            'has_imglib': _HAS_IMGLIB,
            'capturing':  self._trig_capturing,
        }

    def _trig_loop(self):
        prev_arr = None
        prev_seq = -1
        while self._trig_enabled:
            if not _HAS_IMGLIB:
                time.sleep(1)
                continue
            frame, seq = self.get_frame_with_seq()
            if frame is None or seq == prev_seq:
                time.sleep(0.15)
                continue
            prev_seq = seq
            try:
                img = Image.open(io.BytesIO(frame)).convert('L').resize((320, 240))
                arr = np.array(img, dtype=np.int16)
            except Exception:
                time.sleep(0.15)
                continue
            if prev_arr is not None and not self._capturing:
                diff = np.abs(arr - prev_arr).clip(0, 255).astype(np.uint8)
                # GaussianBlur(1): 단일픽셀 노이즈 제거, 선형 궤적은 보존
                blurred = np.array(
                    Image.fromarray(diff).filter(ImageFilter.GaussianBlur(1)))
                peak = int(blurred.max())
                # 구름/카메라 흔들림 거부: 밝은 픽셀이 너무 많으면 위성 아님
                # 320×240=76800px 기준 3000px ≈ 4% 초과면 대형 변화(구름/진동)
                thr_low = max(self._trig_threshold // 2, 15)
                bright_px = int((blurred >= thr_low).sum())
                if peak >= self._trig_threshold and bright_px < 3000:
                    now = time.time()
                    if now - self._trig_last >= self._trig_cooldown:
                        self._trig_last = now
                        self._trig_hit_count += 1
                        if self._trig_callback:
                            threading.Thread(target=self._trig_callback,
                                             daemon=True).start()
            prev_arr = arr
            time.sleep(0.15)

    def set_periodic_capture(self, enabled, interval_s=600, callback=None):
        self._per_interval = max(10, int(interval_s))
        self._per_callback = callback
        self._per_enabled  = enabled
        if enabled:
            self._per_next = time.time() + self._per_interval
            if self._per_thread is None or not self._per_thread.is_alive():
                self._per_thread = threading.Thread(
                    target=self._per_loop, daemon=True, name='periodic')
                self._per_thread.start()

    def get_periodic_status(self):
        remaining = max(0, int(self._per_next - time.time())) if self._per_enabled else 0
        return {
            'enabled':    self._per_enabled,
            'interval_s': self._per_interval,
            'shot_count': self._per_shot_count,
            'next_in_s':  remaining,
        }

    def _per_loop(self):
        while self._per_enabled:
            now = time.time()
            if now >= self._per_next and not self._capturing:
                self._per_next = now + self._per_interval
                self._per_shot_count += 1
                if self._per_callback:
                    def _run_cb(cb):
                        try:
                            cb()
                        except Exception as e:
                            print(f"[periodic] capture error: {e}", flush=True)
                    threading.Thread(target=_run_cb,
                                     args=(self._per_callback,),
                                     daemon=True).start()
            time.sleep(5)

    def start_burst(self, params, duration_s=3600, gps_callback=None):
        if self._burst_enabled:
            return {"ok": False, "error": "이미 연속촬영 중입니다"}
        self._burst_enabled      = True
        self._burst_count        = 0
        self._burst_started      = time.time()
        self._burst_duration     = max(10, int(duration_s))
        self._burst_gps_callback = gps_callback
        self._burst_thread       = threading.Thread(
            target=self._burst_loop, args=(dict(params),),
            daemon=True, name="burst")
        self._burst_thread.start()
        return {"ok": True, **self.get_burst_status()}

    def stop_burst(self):
        self._burst_enabled = False
        return {"ok": True, **self.get_burst_status()}

    def get_burst_status(self):
        elapsed = int(time.time() - self._burst_started) if self._burst_started > 0 else 0
        remaining = max(0, self._burst_duration - elapsed) if self._burst_enabled else 0
        return {
            "running":   self._burst_enabled,
            "count":     self._burst_count,
            "elapsed":   elapsed,
            "remaining": remaining,
            "duration":  self._burst_duration,
        }

    def _burst_loop(self, params):
        shutter_ms = float(params.get("shutter", 5000))
        shutter_us = int(shutter_ms * 1000)
        gain       = float(params.get("gain", 8))
        awb        = params.get("awb", "auto")
        saturation = float(params.get("saturation", 1.0))
        sharpness  = float(params.get("sharpness", 1.5))
        contrast   = float(params.get("contrast", 1.0))
        quality    = int(params.get("quality", 95))
        if not self._capture_mutex.acquire(blocking=True, timeout=10):
            self._burst_enabled = False
            return
        try:
            deadline = self._burst_started + self._burst_duration
            if _HAS_PICAMERA2:
                self._burst_loop_picamera2(
                    shutter_ms, shutter_us, gain, awb,
                    saturation, sharpness, contrast, quality, deadline)
            else:
                self._burst_loop_subprocess(
                    shutter_ms, shutter_us, gain, awb,
                    saturation, sharpness, contrast, quality, deadline)
        finally:
            self._burst_enabled = False
            self._capture_mutex.release()

    def _burst_loop_picamera2(self, shutter_ms, shutter_us, gain, awb,
                               saturation, sharpness, contrast, quality, deadline):
        with self._state_lock:
            cam      = self._cam
            prev_cfg = self._preview_config
        if cam is None:
            print("[burst] camera not ready", flush=True)
            return
        controls = {
            "ExposureTime": shutter_us, "AnalogueGain": float(gain),
            "Saturation": float(saturation), "Sharpness": float(sharpness),
            "Contrast": float(contrast), "AeEnable": False,
            "AwbEnable": awb != "none",
            **( {"AwbMode": AWB_MODE.get(awb, 0)} if awb != "none" else {} ),
            "NoiseReductionMode": 0,
            "FrameDurationLimits": (shutter_us, shutter_us + 1000000),
        }
        still_cfg = cam.create_still_configuration(
            main={"size": (1456, 1088), "format": "RGB888"}, controls=controls)
        self._capturing = True
        try:
            with self._cam_op_lock:
                try:
                    cam.stop()
                    cam.configure(still_cfg)
                    cam.start()
                    time.sleep(0.5)
                except Exception as e:
                    print(f"[burst] mode switch failed: {e}", flush=True)
                    return
            os.makedirs(PHOTOS_DIR, exist_ok=True)
            cam.options["quality"] = quality
            while self._burst_enabled and time.time() < deadline:
                buf = io.BytesIO()
                try:
                    cam.capture_file(buf, format="jpeg")
                    captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                    data = buf.getvalue()
                    if data:
                        self._burst_count += 1
                        seq = self._burst_count
                        ms  = captured_at.microsecond // 1000
                        ts  = captured_at.strftime("%Y%m%d_%H%M%S") + f"_{ms:03d}"
                        filename = f"star_{ts}_{seq:05d}_burst.jpg"
                        filepath = os.path.join(PHOTOS_DIR, filename)
                        with open(filepath, "wb") as fh:
                            fh.write(data)
                        gps_data = self._burst_gps_callback() if self._burst_gps_callback else None
                        self._save_sidecar(
                            filepath, captured_at,
                            shutter_ms, shutter_us,
                            gain, awb, 1.0, 1.5, 1.0, quality,
                            seq, 0, 0, gps_data,
                        )
                    else:
                        print("[burst] empty frame", flush=True)
                except Exception as e:
                    print(f"[burst] frame error: {e}", flush=True)
                    break
        finally:
            self._capturing = False
            with self._cam_op_lock:
                try:
                    cam.stop()
                    cam.configure(prev_cfg)
                    cam.start()
                    time.sleep(0.3)
                except Exception:
                    pass

    def _burst_loop_subprocess(self, shutter_ms, shutter_us, gain, awb,
                                saturation, sharpness, contrast, quality, deadline):
        self._capturing = True
        self._wait_preview_stop()
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        try:
            while self._burst_enabled and time.time() < deadline:
                tmp_path = "/tmp/starcam_burst.jpg"
                cmd = [
                    "rpicam-still", "-n", "-o", tmp_path,
                    "--shutter", str(shutter_us), "--gain", str(gain),
                    *(("--awbgains", "2.0", "1.5") if awb == "none" else ("--awb", awb)),
                    "--denoise", "off",
                    "--saturation", str(saturation), "--sharpness", str(sharpness),
                    "--contrast", str(contrast), "--quality", str(quality),
                    "-t", "200",
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True,
                                            timeout=shutter_ms / 1000 + 30)
                    if result.returncode == 0 and os.path.exists(tmp_path):
                        captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                        self._burst_count += 1
                        seq      = self._burst_count
                        ms       = captured_at.microsecond // 1000
                        ts       = captured_at.strftime("%Y%m%d_%H%M%S") + f"_{ms:03d}"
                        filename = f"star_{ts}_{seq:05d}_burst.jpg"
                        filepath = os.path.join(PHOTOS_DIR, filename)
                        import shutil
                        shutil.move(tmp_path, filepath)
                        gps_data = self._burst_gps_callback() if self._burst_gps_callback else None
                        self._save_sidecar(
                            filepath, captured_at,
                            shutter_ms, shutter_us,
                            gain, awb, 1.0, 1.5, 1.0, quality,
                            seq, 0, 0, gps_data,
                        )
                    else:
                        print("[burst] rpicam-still failed", flush=True)
                except subprocess.TimeoutExpired:
                    print("[burst] timeout", flush=True)
                except Exception as e:
                    print(f"[burst] error: {e}", flush=True)
                    break
        finally:
            self._capturing = False

    def is_connected(self):
        return self._connected


    # ── Video recording ────────────────────────────────────────────
    def is_enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        self._enabled = enabled

    # ── Metadata ──────────────────────────────────────────────────
    def _save_sidecar(self, filepath, captured_at, shutter_ms, shutter_us,
                      gain, awb, saturation, sharpness, contrast, quality,
                      seq, total, interval, gps_data):
        metadata = {
            'captured_at': captured_at.isoformat(timespec='milliseconds'),
            'camera': {
                'shutter_ms': shutter_ms, 'shutter_us': shutter_us,
                'gain': gain, 'iso_equiv': gain_to_iso(gain),
                'awb': awb, 'saturation': saturation,
                'sharpness': sharpness, 'contrast': contrast,
                'quality': quality, 'sequence': seq,
                'total': total, 'interval_s': interval,
            },
        }
        if gps_data:
            gps_section = dict(gps_data)
            try:
                ms = captured_at.microsecond // 1000
                ts = gps_section.get('timestamp', '')
                if ts and len(ts) >= 19:
                    gps_section['timestamp'] = ts[:19] + f'.{ms:03d}Z'
            except Exception:
                pass
            metadata['gps'] = gps_section
        with open(filepath.replace('.jpg', '.json'), 'w') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        return bool(gps_data)

    # ── Capture ───────────────────────────────────────────────────
    def capture(self, params, gps_data=None, suffix=''):
        if not self._capture_mutex.acquire(blocking=False):
            return {'saved': [], 'count': 0, 'gps_saved': False,
                    'error': '촬영 중입니다. 잠시 후 다시 시도하세요'}
        try:
            return self._do_capture(params, gps_data, suffix)
        finally:
            self._capture_mutex.release()

    def _do_capture(self, params, gps_data=None, suffix=""):
        shutter_ms  = float(params.get('shutter_ms') or params.get('shutter', 5000))
        gain        = float(params.get('gain',       8))
        awb         =       params.get('awb',        'auto')
        count       = min(int(params.get('count',    1)), 20)
        interval    = max(0.0, float(params.get('interval', 1)))
        saturation  = float(params.get('saturation', 1.0))
        sharpness   = float(params.get('sharpness',  1.5))
        contrast    = float(params.get('contrast',   1.0))
        quality     =   int(params.get('quality',    95))

        if not self._enabled:
            return {'saved': [], 'count': 0, 'gps_saved': False,
                    'error': '카메라가 꺼져 있습니다'}

        os.makedirs(PHOTOS_DIR, exist_ok=True)
        shutter_us = int(shutter_ms * 1000)
        args = (shutter_ms, shutter_us, gain, awb, count, interval,
                saturation, sharpness, contrast, quality, gps_data)

        if _HAS_PICAMERA2:
            with self._state_lock:
                cam = self._cam
                prev_cfg = self._preview_config
            if cam is None:
                return {'saved': [], 'count': 0, 'gps_saved': False,
                        'error': '카메라 연결 중입니다. 잠시 후 다시 시도하세요'}
            return self._capture_picamera2(cam, prev_cfg, *args, suffix=suffix)
        else:
            return self._capture_subprocess(*args, suffix=suffix)

    def _capture_picamera2(self, cam, prev_cfg, shutter_ms, shutter_us, gain, awb,
                           count, interval, saturation, sharpness, contrast, quality, gps_data, suffix=""):
        controls = {
            "ExposureTime": shutter_us, "AnalogueGain": float(gain),
            "Saturation": float(saturation), "Sharpness": float(sharpness),
            "Contrast": float(contrast), "AeEnable": False,
            "AwbEnable": awb != 'none',
            **( {"AwbMode": AWB_MODE.get(awb, 0)} if awb != 'none' else {} ),
            "NoiseReductionMode": 0,
            "FrameDurationLimits": (shutter_us, shutter_us + 1000000),
        }
        still_cfg = cam.create_still_configuration(
            main={"size": (1456, 1088), "format": "RGB888"}, controls=controls,
        )

        # Phase 1: capture all frames to RAM (avoids SD card latency between shots)
        frames = []  # (jpeg_bytes, captured_at, seq)
        last_error = None
        self._capturing = True
        try:
            with self._cam_op_lock:
                cam.options['quality'] = quality
                for i in range(count):
                    buf = io.BytesIO()
                    try:
                        cam.switch_mode_and_capture_file(still_cfg, buf, format='jpeg')
                        # 노출 종료 직후 시각에서 노출시간의 절반을 빼 mid-exposure 시각 계산
                        captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                        data = buf.getvalue()
                        if data:
                            frames.append((data, captured_at, i + 1))
                        else:
                            last_error = 'capture failed'
                    except Exception as e:
                        last_error = str(e)
                    if i < count - 1 and interval > 0:
                        time.sleep(max(0.0, interval - shutter_ms / 1000))
                # 프리뷰 모드로 명시적 복귀 (switch_mode보다 안정적)
                try:
                    cam.stop()
                except Exception:
                    pass
                try:
                    cam.configure(prev_cfg)
                    cam.start()
                    time.sleep(0.3)
                except Exception:
                    pass  # 실패하면 _run 루프가 카메라 재시작
        finally:
            # Release camera so preview resumes while we write to disk
            self._capturing = False

        # Phase 2: write all frames to disk (preview is already live again)
        saved, gps_saved = [], False
        for jpeg_bytes, captured_at, seq in frames:
            jpeg_bytes = self._fpn_correct_jpeg(jpeg_bytes, quality)
            ms = captured_at.microsecond // 1000
            ts = captured_at.strftime('%Y%m%d_%H%M%S') + f'_{ms:03d}'
            filename = f'star_{ts}_{seq:03d}{suffix}.jpg'
            filepath = os.path.join(PHOTOS_DIR, filename)
            try:
                with open(filepath, 'wb') as f:
                    f.write(jpeg_bytes)
                saved.append(filename)
                gps_saved |= self._save_sidecar(
                    filepath, captured_at, shutter_ms, shutter_us,
                    gain, awb, saturation, sharpness, contrast, quality,
                    seq, count, interval, gps_data,
                )
            except Exception as e:
                last_error = str(e)

        return {'saved': saved, 'count': len(saved), 'gps_saved': gps_saved,
                'error': last_error if not saved else None}

    def _capture_subprocess(self, shutter_ms, shutter_us, gain, awb,
                            count, interval, saturation, sharpness, contrast, quality, gps_data, suffix=""):
        saved, gps_saved, last_error = [], False, None
        self._capturing = True
        self._wait_preview_stop()

        # Write to /tmp (tmpfs = RAM) during burst, move to PHOTOS_DIR at end
        tmp_frames = []
        try:
            for i in range(count):
                tmp_path = f'/tmp/starcam_{i:03d}.jpg'
                cmd = [
                    'rpicam-still', '-n', '-o', tmp_path,
                    '--shutter', str(shutter_us), '--gain', str(gain),
                    *(('--awbgains', '2.0', '1.5') if awb == 'none' else ('--awb', awb)),
                    '--denoise', 'off',
                    '--saturation', str(saturation), '--sharpness', str(sharpness),
                    '--contrast', str(contrast), '--quality', str(quality),
                    '-t', '200',
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True,
                                            timeout=shutter_ms / 1000 + 30)
                    if result.returncode == 0 and os.path.exists(tmp_path):
                        # rpicam-still: 200ms 준비 + shutter_us 노출 후 subprocess 반환
                        # datetime.now() ≈ 노출종료, mid-exposure = now - shutter_us/2
                        captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                        tmp_frames.append((tmp_path, captured_at, i + 1))
                    else:
                        last_error = result.stderr.decode('utf-8', errors='ignore').strip() or 'capture failed'
                except subprocess.TimeoutExpired:
                    last_error = 'timeout'
                if i < count - 1 and interval > 0:
                    time.sleep(max(0.0, interval - shutter_ms / 1000))
        finally:
            self._capturing = False

        for tmp_path, captured_at, seq in tmp_frames:
            ms = captured_at.microsecond // 1000
            ts = captured_at.strftime('%Y%m%d_%H%M%S') + f'_{ms:03d}'
            filename = f'star_{ts}_{seq:03d}{suffix}.jpg'
            filepath = os.path.join(PHOTOS_DIR, filename)
            try:
                with open(tmp_path, 'rb') as src:
                    raw = src.read()
                os.remove(tmp_path)
                jpeg_bytes = self._fpn_correct_jpeg(raw, quality)
                with open(filepath, 'wb') as dst:
                    dst.write(jpeg_bytes)
                saved.append(filename)
                gps_saved |= self._save_sidecar(
                    filepath, captured_at, shutter_ms, shutter_us,
                    gain, awb, saturation, sharpness, contrast, quality,
                    seq, count, interval, gps_data,
                )
            except Exception as e:
                last_error = str(e)

        return {'saved': saved, 'count': len(saved), 'gps_saved': gps_saved,
                'error': last_error if not saved else None}

    # ── FPN (Fixed-Pattern Noise) correction ────────────────────────
    # FPN 기준선 백분위수.
    # 50 = 중앙값 (맑은 밤 최적).
    # 구름이 행의 절반 이상을 자주 덮는다면 20~30으로 낮추면 안전.
    _FPN_PERCENTILE = 20

    def _fpn_correct_array(self, arr: 'np.ndarray') -> 'np.ndarray':
        """Row-then-column percentile FPN correction (2-stage).

        Stage 1: 각 행의 하위 _FPN_PERCENTILE% 값을 빼서 행 FPN 제거
        Stage 2: 잔차 배열에서 각 열의 하위 _FPN_PERCENTILE% 값을 빼서 열 FPN 제거
        마지막에 원래 평균 밝기를 한 번 복원.

        2단계로 분리하면 행·열 기준선을 각각 독립적으로 추정하므로
        단순히 row_pct + col_pct 를 한꺼번에 빼는 방식의
        '이중 차감(double-subtraction)' 문제가 없음.
        노이즈 수준의 약한 별도 배경 대비가 유지됨.
        """
        if not _HAS_IMGLIB or not self._fpn_enabled:
            return arr
        p = self._FPN_PERCENTILE
        a = arr.astype(np.float32)

        def _correct_channel(ch):
            mu = float(ch.mean())
            # Stage 1: row FPN
            stage1 = ch - np.percentile(ch, p, axis=1, keepdims=True)
            # Stage 2: column FPN (잔차 기준)
            stage2 = stage1 - np.percentile(stage1, p, axis=0, keepdims=True)
            # 원래 밝기 복원
            return stage2 + mu

        if a.ndim == 3:
            for c in range(a.shape[2]):
                a[:, :, c] = _correct_channel(a[:, :, c])
        else:
            a = _correct_channel(a)
        return np.clip(a, 0, 255).astype(np.uint8)

    def _fpn_correct_jpeg(self, jpeg_bytes: bytes, quality: int = 95) -> bytes:
        """Decode a JPEG, apply FPN correction, and re-encode at *quality*."""
        if not _HAS_IMGLIB or not jpeg_bytes or not self._fpn_enabled:
            return jpeg_bytes
        try:
            arr = np.array(Image.open(io.BytesIO(jpeg_bytes)))
            arr = self._fpn_correct_array(arr)
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, format='JPEG', quality=quality)
            return buf.getvalue()
        except Exception:
            return jpeg_bytes

    # ── Auto-calibration ──────────────────────────────────────────
    def analyze_jpeg(self, data_or_path):
        try:
            from PIL import Image
            if isinstance(data_or_path, (bytes, bytearray)):
                img = Image.open(io.BytesIO(data_or_path)).convert('L')
                size = len(data_or_path)
            else:
                img = Image.open(data_or_path).convert('L')
                size = os.path.getsize(data_or_path) if os.path.exists(data_or_path) else 0
            w, h = img.size
            scale = min(1.0, 640 / max(w, h))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
            pixels = list(img.getdata())
            n = len(pixels)
            avg = sum(pixels) / n
            dark = sorted(pixels)[:n // 2]
            noise = statistics.stdev(dark) if len(dark) > 2 else 1.0
            threshold = min(avg + 3 * noise, 245)
            star_count = sum(1 for p in pixels if p > threshold)
            return {'avg': round(avg, 1), 'noise': round(noise, 1),
                    'stars': star_count, 'score': round(star_count / (noise + 1), 2)}
        except Exception:
            return {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}

    def calibrate(self, shutter_ms=3000):
        gains = [2, 4, 8, 16]
        shutter_us = int(shutter_ms * 1000)

        if not self._enabled:
            return {'error': '카메라가 꺼져 있습니다', 'results': []}

        if _HAS_PICAMERA2:
            with self._state_lock:
                cam = self._cam
                prev_cfg = self._preview_config
            if cam is None:
                return {'error': '카메라 연결 중입니다', 'results': []}
            return self._cal_picamera2(cam, prev_cfg, gains, shutter_ms, shutter_us)
        else:
            return self._cal_subprocess(gains, shutter_ms, shutter_us)

    def _cal_picamera2(self, cam, prev_cfg, gains, shutter_ms, shutter_us):
        results = []
        self._capturing = True
        try:
            with self._cam_op_lock:
                for gain in gains:
                    controls = {
                        "ExposureTime": shutter_us, "AnalogueGain": float(gain),
                        "AeEnable": False, "AwbEnable": False,
                        "NoiseReductionMode": 0,
                        "FrameDurationLimits": (shutter_us, shutter_us + 1000000),
                    }
                    still_cfg = cam.create_still_configuration(
                        main={"size": (1456, 1088), "format": "RGB888"}, controls=controls,
                    )
                    buf = io.BytesIO()
                    try:
                        cam.switch_mode_and_capture_file(still_cfg, buf, format='jpeg')
                        data = buf.getvalue()
                        stats = self.analyze_jpeg(data) if data else {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                    except Exception:
                        stats = {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                    results.append({'gain': gain, 'iso': gain_to_iso(gain), **stats})
                try:
                    cam.switch_mode(prev_cfg)
                except Exception:
                    pass
        finally:
            self._capturing = False
        return self._cal_result(results, shutter_ms)

    def _cal_subprocess(self, gains, shutter_ms, shutter_us):
        results = []
        self._capturing = True
        self._wait_preview_stop()
        try:
            for gain in gains:
                fp = f'/tmp/starcam_cal_g{gain}.jpg'
                cmd = [
                    'rpicam-still', '-n', '-o', fp,
                    '--shutter', str(shutter_us), '--gain', str(gain),
                    '--awb', 'auto', '--denoise', 'off', '-t', '200',
                ]
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=shutter_ms / 1000 + 15)
                    stats = self.analyze_jpeg(fp) if (r.returncode == 0 and os.path.exists(fp)) else {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    stats = {'avg': 0, 'noise': 0, 'stars': 0, 'score': 0}
                results.append({'gain': gain, 'iso': gain_to_iso(gain), **stats})
        finally:
            self._capturing = False
        return self._cal_result(results, shutter_ms)

    def _cal_result(self, results, shutter_ms):
        if not results:
            return {'error': 'calibration failed', 'results': []}
        best = max(results, key=lambda r: r['score'])
        return {
            'results': results,
            'recommended': {
                'gain': best['gain'], 'shutter_ms': shutter_ms,
                'note': '별이 보이면 노출을 5~30초로 늘려보세요',
            },
        }

    # ── Satellite trail detection ─────────────────────────────
    def detect_trail(self, image_data, npy_path=None):
        """
        Detect a linear satellite trail in a JPEG image.

        Two-stage pipeline:
          Stage 1 — Rough Hough: find the line with the most inlier votes among
                    high-sigma bright pixels, robust against scattered stars.
          Stage 2 — Regional PCA: run PCA only on the inlier pixels identified by
                    Hough, giving precise linearity / length / width metrics without
                    star contamination.
        Falls back to global PCA (v3 style) when the pixel count is small enough
        that Hough is unnecessary.

        npy_path : str or None
            If given, save the noise-cleaned float32 array to this path as a
            compressed .npz (``np.savez_compressed``).  Loading it back with
            ``np.load(path)['cleaned']`` gives the exact array the detection
            algorithm operated on — sufficient for bit-exact reproduction.
        """
        if not _HAS_IMGLIB:
            return {'detected': False, 'reason': 'numpy/PIL 없음'}
        try:
            img = Image.open(io.BytesIO(image_data)).convert('L')
            w, h = img.size
            scale = min(1.0, 800 / w)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
            arr = np.array(img, dtype=np.float32)

            # 2-D fixed-pattern noise removal (row + column medians)
            row_med = np.median(arr, axis=1, keepdims=True)
            col_med = np.median(arr, axis=0, keepdims=True)
            cleaned = arr - row_med - col_med + float(arr.mean())

            # Raw intermediate data — save before any threshold/decision logic
            if npy_path is not None:
                try:
                    np.savez_compressed(npy_path, cleaned=cleaned)
                except Exception as _npy_e:
                    print(f'[detect] npy save error: {_npy_e}', flush=True)

            mu    = float(cleaned.mean())
            sigma = float(cleaned.std())
            if sigma < 0.5:
                return {'detected': False, 'reason': '이미지 균일 (노출 부족?)'}

            # ── Stage 1: Rough Hough ──────────────────────────────────────
            # Use a high threshold to limit candidates and keep Hough fast.
            HOUGH_SIG   = 7.0
            HOUGH_TOL   = 3.0   # inlier distance tolerance (px)
            HOUGH_MDIST = 30    # minimum pair distance to define a line (px)
            HOUGH_MIN_N = 5     # minimum inliers to accept a line

            hough_coords = np.argwhere(cleaned > mu + HOUGH_SIG * sigma)
            n_hough = len(hough_coords)

            hough_inliers = None
            if 4 < n_hough <= 500:
                pts = hough_coords.astype(np.float32)
                best_count = 0
                best_mask  = None
                # All pairs — vectorised inner loop
                for i in range(len(pts)):
                    diff = pts - pts[i]                    # (N,2)
                    dists = np.hypot(diff[:, 0], diff[:, 1])
                    for j in np.where(dists > HOUGH_MDIST)[0]:
                        if j <= i:
                            continue
                        dy, dx = diff[j]
                        inv = 1.0 / dists[j]
                        ny, nx = -dx * inv, dy * inv      # unit normal
                        d = np.abs(diff[:, 0] * ny + diff[:, 1] * nx)
                        mask = d < HOUGH_TOL
                        cnt  = int(mask.sum())
                        if cnt > best_count:
                            best_count = cnt
                            best_mask  = mask

                if best_count >= HOUGH_MIN_N and best_mask is not None:
                    inlier_pts = pts[best_mask]
                    # Continuity check: fill_rate = inliers / actual trail extent
                    c = inlier_pts - inlier_pts.mean(axis=0)
                    cov_h = (c.T @ c) / len(c)
                    _, evec_h = np.linalg.eigh(cov_h)
                    proj_h = c @ evec_h[:, -1]
                    extent = float(proj_h.max() - proj_h.min())
                    fill = best_count / max(extent, 1.0)
                    # Real trail: dense pixels; star coincidence: sparse gaps
                    if fill > 0.05:
                        hough_inliers = inlier_pts

            # ── Stage 2: Regional PCA ─────────────────────────────────────
            def _pca_metrics(pts_2d):
                n   = len(pts_2d)
                ctr = pts_2d.mean(axis=0)
                c   = pts_2d - ctr
                cov = (c.T @ c) / n
                ev, evec  = np.linalg.eigh(cov)
                ev0 = max(float(ev[-1]), 1e-9)
                ev1 = max(float(ev[0]),  1e-9)
                lin = ev0 / (ev0 + ev1)
                pmaj = c @ evec[:, -1]
                pmin = c @ evec[:,  0]
                tlen = float(pmaj.max() - pmaj.min())
                twid = float(pmin.max() - pmin.min())
                band = max(4.0, twid * 0.25)
                conc = float(np.mean(np.abs(pmin) < band))
                dy, dx = float(evec[0, -1]), float(evec[1, -1])
                angle = abs(float(np.degrees(np.arctan2(dy, dx)))) % 90
                return lin, tlen, twid, conc, n, angle

            MIN_ANGLE = 8.0  # reject within 8° of horizontal or vertical

            # Prefer Hough inliers; fall back to multi-sigma global sweep
            candidates = []
            if hough_inliers is not None and len(hough_inliers) >= HOUGH_MIN_N:
                lin, tlen, twid, conc, n, angle = _pca_metrics(hough_inliers)
                score = lin * conc * min(1.0, tlen / 50.0)
                candidates.append({'method': 'hough+pca', 'lin': lin, 'n': n,
                                   'trail_len': tlen, 'trail_w': twid,
                                   'conc': conc, 'score': score, 'angle': angle})

            for sig_mult in (5.0, 5.5, 6.0, 7.0):
                coords = np.argwhere(cleaned > mu + sig_mult * sigma)
                n = int(len(coords))
                if n < 4 or n > 300:
                    continue
                lin, tlen, twid, conc, _, angle = _pca_metrics(coords.astype(np.float32))
                score = lin * conc * min(1.0, tlen / 50.0)
                candidates.append({'method': f'pca@{sig_mult}σ', 'lin': lin, 'n': n,
                                   'trail_len': tlen, 'trail_w': twid,
                                   'conc': conc, 'score': score, 'angle': angle})

            if not candidates:
                return {'detected': False, 'reason': '후보 없음'}

            best = max(candidates, key=lambda x: x['score'])
            density = best['n'] / max(best['trail_len'], 1.0)
            MIN_DENSITY = 0.25  # continuous streak ≥0.25 px⁻¹; aligned stars ≈0.05

            detected = (
                best['lin']       > 0.88
                and best['trail_len'] > 20
                and best['trail_w']   < best['trail_len'] * 0.40
                and best['conc']      > 0.55
                and MIN_ANGLE < best['angle'] < 90 - MIN_ANGLE
                and density >= MIN_DENSITY
            )
            if detected:
                return {
                    'detected':       True,
                    'method':         best['method'],
                    'linearity':      round(best['lin'], 3),
                    'trail_len_px':   round(best['trail_len'], 1),
                    'trail_width_px': round(best['trail_w'], 1),
                    'concentration':  round(best['conc'], 3),
                    'angle_deg':      round(best['angle'], 1),
                    'n_bright':       best['n'],
                    'density':        round(density, 3),
                }
            reason = f'직선성 부족 (lin={best["lin"]:.2f}, len={best["trail_len"]:.0f}px)'
            if not (MIN_ANGLE < best['angle'] < 90 - MIN_ANGLE):
                reason = f'수평/수직 아티팩트 제외 (angle={best["angle"]:.1f}°)'
            elif density < MIN_DENSITY:
                reason = f'별 직선 배열 (density={density:.3f}, n={best["n"]}, len={best["trail_len"]:.0f}px)'
            return {
                'detected':  False,
                'reason':    reason,
                'linearity': round(best['lin'], 3),
                'angle_deg': round(best['angle'], 1),
                'n_bright':  best['n'],
                'density':   round(density, 3),
            }
        except Exception as e:
            return {'detected': False, 'reason': str(e)}

    # ── Scout mode ────────────────────────────────────────────
    def _parse_utc_hhmm(self, time_str):
        """Parse "HH:MM" string into the next absolute UTC datetime."""
        if not time_str:
            return None
        try:
            h, m = int(time_str[:2]), int(time_str[3:5])
            now = datetime.utcnow()
            dt  = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
        except Exception:
            return None

    # Ring buffer constants
    _RING_PRE    = 8    # pre-trigger frames to retain
    _RING_POST   = 15   # post-trigger frames to capture after event
    _RING_STATIC = 4    # suppress if same angle (±5°) repeats this many times

    def start_scout(self, params, on_detect=None, stop_at_utc=None,
                    start_at_utc=None, gps_callback=None):
        if self._scout_enabled or self._scout_scheduled:
            return {'ok': False, 'error': '이미 감시/예약 중'}
        self._scout_gps_callback = gps_callback
        start_dt = self._parse_utc_hhmm(start_at_utc)
        if start_dt:
            self._scout_scheduled = True
            self._scout_start_at  = start_dt
            self._scout_pending   = {'params': params, 'on_detect': on_detect,
                                     'stop_at_utc': stop_at_utc}
            threading.Thread(target=self._scout_schedule_wait,
                             daemon=True, name='scout-sched').start()
            return {'ok': True, **self.get_scout_status()}
        return self._start_scout_now(params, on_detect, stop_at_utc)

    def _start_scout_now(self, params, on_detect, stop_at_utc):
        self._scout_stop_at      = self._parse_utc_hhmm(stop_at_utc)
        self._scout_enabled      = True
        self._scout_frame_count  = 0
        self._scout_detect_count = 0
        self._scout_last_result  = None
        self._scout_params       = dict(params)
        self._scout_callback     = on_detect
        self._ring_angle_hist.clear()
        self._scout_thread = threading.Thread(
            target=self._scout_loop, daemon=True, name='scout')
        self._scout_thread.start()
        return {'ok': True, **self.get_scout_status()}

    def _scout_schedule_wait(self):
        while self._scout_scheduled:
            if datetime.utcnow() >= self._scout_start_at:
                print(f'[scout] 예약 시작 ({self._scout_start_at.strftime("%H:%M UTC")})',
                      flush=True)
                self._scout_scheduled = False
                p = dict(self._scout_pending)
                self._scout_start_at = None
                self._scout_pending  = {}
                self._start_scout_now(p['params'], p['on_detect'], p.get('stop_at_utc'))
                return
            time.sleep(10)

    def stop_scout(self):
        self._scout_enabled   = False
        self._scout_scheduled = False
        self._scout_start_at  = None
        self._scout_stop_at   = None
        self._scout_pending   = {}
        return {'ok': True, **self.get_scout_status()}

    def get_scout_status(self):
        stop_at_str  = self._scout_stop_at.strftime('%H:%M UTC')  if self._scout_stop_at  else None
        start_at_str = self._scout_start_at.strftime('%H:%M UTC') if self._scout_start_at else None
        return {
            'enabled':      self._scout_enabled,
            'scheduled':    self._scout_scheduled,
            'start_at':     start_at_str,
            'frame_count':  self._scout_frame_count,
            'detect_count': self._scout_detect_count,
            'last_result':  self._scout_last_result,
            'stop_at':      stop_at_str,
        }

    def _is_static_angle(self, angle_deg):
        """Return True if the same angle (±5°) has repeated _RING_STATIC times in a row."""
        self._ring_angle_hist.append(angle_deg)
        if len(self._ring_angle_hist) < self._RING_STATIC:
            return False
        angles = list(self._ring_angle_hist)
        return (max(angles) - min(angles)) < 10.0

    def _save_ring_frame(self, jpeg_bytes, captured_at, event_tag, seq,
                         shutter_ms, shutter_us, gain, awb,
                         saturation, sharpness, contrast, quality,
                         gps_data=None, trail=None):
        """Write one ring-buffer frame to disk with FPN correction + sidecar."""
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        if self._fpn_enabled and _HAS_IMGLIB:
            jpeg_bytes = self._fpn_correct_jpeg(jpeg_bytes, quality)
        ms  = captured_at.microsecond // 1000
        ts  = captured_at.strftime('%Y%m%d_%H%M%S') + f'_{ms:03d}'
        filename = f'star_{ts}_{seq:04d}_ring.jpg'
        filepath = os.path.join(PHOTOS_DIR, filename)
        with open(filepath, 'wb') as fh:
            fh.write(jpeg_bytes)
        metadata = {
            'captured_at': captured_at.isoformat(timespec='milliseconds'),
            'camera': {
                'shutter_ms': shutter_ms, 'shutter_us': shutter_us,
                'gain': gain, 'iso_equiv': gain_to_iso(gain),
                'awb': awb, 'saturation': saturation,
                'sharpness': sharpness, 'contrast': contrast, 'quality': quality,
            },
            'ring_event': event_tag,
            'ring_seq':   seq,
        }
        if trail:
            metadata['detection'] = trail
        if gps_data:
            metadata['gps'] = gps_data
        with open(filepath.replace('.jpg', '.json'), 'w') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        return filename

    def _scout_loop(self):
        """Ring-buffer scout: continuous short-exposure capture with pre/post trigger saving."""
        shutter_ms = float(self._scout_params.get('shutter_ms', 1000))
        shutter_us = int(shutter_ms * 1000)
        gain       = float(self._scout_params.get('gain', 8))
        awb        = self._scout_params.get('awb', 'none')
        quality    = int(self._scout_params.get('quality', 95))
        saturation = float(self._scout_params.get('saturation', 0.0))
        sharpness  = float(self._scout_params.get('sharpness', 1.5))
        contrast   = float(self._scout_params.get('contrast', 1.0))
        pre_n      = int(self._scout_params.get('pre_frames',  self._RING_PRE))
        post_n     = int(self._scout_params.get('post_frames', self._RING_POST))

        if _HAS_PICAMERA2:
            self._scout_ring_picamera2(shutter_ms, shutter_us, gain, awb,
                                       saturation, sharpness, contrast, quality,
                                       pre_n, post_n)
        else:
            self._scout_ring_subprocess(shutter_ms, shutter_us, gain, awb,
                                        saturation, sharpness, contrast, quality,
                                        pre_n, post_n)

    def _make_scout_worker(self, shutter_ms, shutter_us, gain, awb,
                            saturation, sharpness, contrast, quality,
                            pre_n, post_n, ring, ring_lock, post_ctx, post_lock,
                            stop_evt):
        """Return a detection worker function for parallel scout processing."""

        def _worker():
            det_q = post_ctx['_det_q']
            while not stop_evt.is_set():
                try:
                    captured_at, raw, ring_snap = det_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                try:
                    trail = self.detect_trail(raw)
                    self._scout_last_result = trail

                    if trail.get('detected') and self._scout_enabled:
                        angle = trail.get('angle_deg', 45.0)
                        if self._is_static_angle(angle):
                            continue

                        with post_lock:
                            if post_ctx['remaining'] > 0:
                                continue  # already in post-trigger
                            self._scout_detect_count += 1
                            event_tag = captured_at.strftime('%Y%m%d_%H%M%S')
                            post_ctx['event_tag'] = event_tag
                            post_ctx['seq']       = 0
                            post_ctx['remaining'] = post_n

                        gps_data = (self._scout_gps_callback()
                                    if self._scout_gps_callback else None)

                        # Save pre-trigger frames (ring snapshot)
                        for i, (ts, data) in enumerate(ring_snap):
                            with post_lock:
                                post_ctx['seq'] += 1
                                seq = post_ctx['seq']
                            is_trigger = (i == len(ring_snap) - 1)
                            self._save_ring_frame(
                                data, ts, event_tag, seq,
                                shutter_ms, shutter_us, gain, awb,
                                saturation, sharpness, contrast, quality,
                                gps_data=gps_data,
                                trail=trail if is_trigger else None)

                        print(f'[scout] 이벤트 탐지 {event_tag}  '
                              f'lin={trail.get("linearity")}  '
                              f'ang={trail.get("angle_deg")}°  '
                              f'len={trail.get("trail_len_px")}px', flush=True)

                        if self._scout_callback:
                            threading.Thread(target=self._scout_callback,
                                             daemon=True).start()
                finally:
                    det_q.task_done()

        return _worker

    def _scout_ring_picamera2(self, shutter_ms, shutter_us, gain, awb,
                               saturation, sharpness, contrast, quality,
                               pre_n, post_n):
        with self._state_lock:
            cam      = self._cam
            prev_cfg = self._preview_config
        if cam is None:
            print('[scout] camera not ready', flush=True)
            return

        controls = {
            'ExposureTime': shutter_us, 'AnalogueGain': float(gain),
            'Saturation': float(saturation), 'Sharpness': float(sharpness),
            'Contrast': float(contrast), 'AeEnable': False,
            'AwbEnable': awb != 'none',
            **({'AwbMode': AWB_MODE.get(awb, 0)} if awb != 'none' else {}),
            'NoiseReductionMode': 0,
            'FrameDurationLimits': (shutter_us, shutter_us + 1_000_000),
        }
        still_cfg = cam.create_still_configuration(
            main={'size': (1456, 1088), 'format': 'RGB888'}, controls=controls)

        ring      = collections.deque(maxlen=pre_n)
        ring_lock = threading.Lock()
        post_lock = threading.Lock()
        stop_evt  = threading.Event()
        det_q     = queue.Queue(maxsize=1)
        post_ctx  = {'remaining': 0, 'event_tag': None, 'seq': 0, '_det_q': det_q}

        worker_fn = self._make_scout_worker(
            shutter_ms, shutter_us, gain, awb,
            saturation, sharpness, contrast, quality,
            pre_n, post_n, ring, ring_lock, post_ctx, post_lock, stop_evt)
        worker = threading.Thread(target=worker_fn, daemon=True, name='scout-detect')
        worker.start()

        self._capturing = True
        try:
            with self._cam_op_lock:
                cam.stop()
                cam.configure(still_cfg)
                cam.start()
                time.sleep(0.5)
            cam.options['quality'] = quality

            while self._scout_enabled:
                if self._scout_stop_at and datetime.utcnow() >= self._scout_stop_at:
                    print(f'[scout] 자동 종료 ({self._scout_stop_at.strftime("%H:%M UTC")})',
                          flush=True)
                    self._scout_enabled = False
                    break

                buf = io.BytesIO()
                try:
                    cam.capture_file(buf, format='jpeg')
                except Exception as e:
                    print(f'[scout] capture error: {e}', flush=True)
                    time.sleep(0.5)
                    continue

                captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                raw = buf.getvalue()
                if not raw:
                    continue

                self._scout_frame_count += 1

                with post_lock:
                    pr        = post_ctx['remaining']
                    event_tag = post_ctx['event_tag']

                if pr > 0:
                    # Post-trigger: save this frame directly in capture thread
                    with post_lock:
                        post_ctx['seq'] += 1
                        seq = post_ctx['seq']
                        post_ctx['remaining'] -= 1
                        remaining_after = post_ctx['remaining']

                    gps_data = (self._scout_gps_callback()
                                if self._scout_gps_callback else None)
                    self._save_ring_frame(raw, captured_at, event_tag, seq,
                                         shutter_ms, shutter_us, gain, awb,
                                         saturation, sharpness, contrast, quality,
                                         gps_data=gps_data)
                    if remaining_after == 0:
                        total = pre_n + 1 + post_n
                        print(f'[scout] 이벤트 {event_tag} 저장 완료 ({total}장)',
                              flush=True)
                        with ring_lock:
                            ring.clear()
                else:
                    # Pre-trigger: add to ring, hand off to detection worker
                    with ring_lock:
                        ring.append((captured_at, raw))
                        ring_snap = list(ring)
                    try:
                        det_q.put_nowait((captured_at, raw, ring_snap))
                    except queue.Full:
                        pass  # worker still busy; skip detection for this frame
        finally:
            stop_evt.set()
            worker.join(timeout=2)
            self._capturing = False
            with self._cam_op_lock:
                try:
                    cam.stop()
                    cam.configure(prev_cfg)
                    cam.start()
                    time.sleep(0.3)
                except Exception:
                    pass

    def _scout_ring_subprocess(self, shutter_ms, shutter_us, gain, awb,
                                saturation, sharpness, contrast, quality,
                                pre_n, post_n):
        """Subprocess fallback with parallel detection worker."""
        ring      = collections.deque(maxlen=pre_n)
        ring_lock = threading.Lock()
        post_lock = threading.Lock()
        stop_evt  = threading.Event()
        det_q     = queue.Queue(maxsize=1)
        post_ctx  = {'remaining': 0, 'event_tag': None, 'seq': 0, '_det_q': det_q}

        worker_fn = self._make_scout_worker(
            shutter_ms, shutter_us, gain, awb,
            saturation, sharpness, contrast, quality,
            pre_n, post_n, ring, ring_lock, post_ctx, post_lock, stop_evt)
        worker = threading.Thread(target=worker_fn, daemon=True, name='scout-detect')
        worker.start()

        self._capturing = True
        self._wait_preview_stop()
        try:
            while self._scout_enabled:
                if self._scout_stop_at and datetime.utcnow() >= self._scout_stop_at:
                    self._scout_enabled = False
                    break

                tmp = '/tmp/starcam_ring.jpg'
                cmd = [
                    'rpicam-still', '-n', '-o', tmp,
                    '--shutter', str(shutter_us), '--gain', str(gain),
                    *(('--awbgains', '2.0', '1.5') if awb == 'none'
                      else ('--awb', awb)),
                    '--denoise', 'off',
                    '--saturation', str(saturation), '--sharpness', str(sharpness),
                    '--contrast', str(contrast), '--quality', str(quality),
                    '-t', '200',
                ]
                try:
                    r = subprocess.run(cmd, capture_output=True,
                                       timeout=shutter_ms / 1000 + 15)
                    if r.returncode != 0 or not os.path.exists(tmp):
                        time.sleep(0.5)
                        continue
                    captured_at = datetime.now() - timedelta(microseconds=shutter_us // 2)
                    with open(tmp, 'rb') as fh:
                        raw = fh.read()
                    os.unlink(tmp)
                except Exception as e:
                    print(f'[scout-sub] error: {e}', flush=True)
                    time.sleep(1)
                    continue

                self._scout_frame_count += 1

                with post_lock:
                    pr        = post_ctx['remaining']
                    event_tag = post_ctx['event_tag']

                if pr > 0:
                    with post_lock:
                        post_ctx['seq'] += 1
                        seq = post_ctx['seq']
                        post_ctx['remaining'] -= 1
                        remaining_after = post_ctx['remaining']
                    gps_data = (self._scout_gps_callback()
                                if self._scout_gps_callback else None)
                    self._save_ring_frame(raw, captured_at, event_tag, seq,
                                         shutter_ms, shutter_us, gain, awb,
                                         saturation, sharpness, contrast, quality,
                                         gps_data=gps_data)
                    if remaining_after == 0:
                        with ring_lock:
                            ring.clear()
                else:
                    with ring_lock:
                        ring.append((captured_at, raw))
                        ring_snap = list(ring)
                    try:
                        det_q.put_nowait((captured_at, raw, ring_snap))
                    except queue.Full:
                        pass
        finally:
            stop_evt.set()
            worker.join(timeout=2)
            self._capturing = False
