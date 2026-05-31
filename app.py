import io
import os
import glob
import json
import time
import shutil
import zipfile

from flask import Flask, Response, jsonify, request, send_from_directory, send_file, abort
from camera import Camera
from gps_reader import GPSReader
from mount import MountController

PHOTOS_DIR = os.path.expanduser('~/photos')
THUMBS_DIR = os.path.expanduser('~/photos/.thumbs')
THUMB_SIZE  = (240, 180)
THUMB_QUAL  = 55

ALLOWED_BAUDS = [4800, 9600, 19200, 38400, 57600, 115200]

app = Flask(__name__, static_folder='static')
camera = Camera()
gps = GPSReader()
mount_ctrl = MountController()

MOUNT_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mount_config.json")
_MOUNT_CFG_DEFAULTS = {
    "az_offset": 0.0, "el_offset": 0.0,
    "el_min": None, "el_max": None,
    "pixel_scale": None,
    "ra_hint": None, "dec_hint": None,
    "cal_server": "", "cal_token": "",
}

def _load_mount_cfg():
    try:
        with open(MOUNT_CFG_PATH) as f:
            cfg = json.load(f)
        return {**_MOUNT_CFG_DEFAULTS, **{k: cfg[k] for k in _MOUNT_CFG_DEFAULTS if k in cfg}}
    except Exception:
        return dict(_MOUNT_CFG_DEFAULTS)

def _save_mount_cfg(updates):
    cfg = _load_mount_cfg()
    cfg.update(updates)
    with open(MOUNT_CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg

def _apply_saved_el_limit():
    cfg = _load_mount_cfg()
    if cfg["el_min"] is not None and cfg["el_max"] is not None:
        el_off = cfg.get("el_offset") or 0.0
        try:
            mount_ctrl.set_el_limit(cfg["el_min"] - el_off, cfg["el_max"] - el_off)
        except Exception:
            pass

_mount_cache = None  # last known az/el, updated on every status poll

def _get_mount_snap():
    return _mount_cache


def _valid_photo_name(filename):
    return (
        isinstance(filename, str)
        and filename.startswith('star_')
        and filename.endswith('.jpg')
        and '/' not in filename
        and '\\' not in filename
        and '..' not in filename
    )


def _load_photo_meta(filename):
    json_path = os.path.join(PHOTOS_DIR, filename.replace('.jpg', '.json'))
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path) as f:
            return json.load(f)
    except Exception:
        return {}


def _plate_solve_timestamp(meta, filename=None):
    from datetime import datetime, timedelta

    ts = meta.get('captured_at_utc') or ''
    if ts.endswith('Z'):
        return (ts.split('.')[0] if '.' in ts else ts[:-1]) + 'Z'

    gps_ts = ((meta.get('gps') or {}).get('timestamp') or '').strip()
    local_ts = meta.get('captured_at') or ''
    if gps_ts and local_ts:
        try:
            local_dt = datetime.fromisoformat(local_ts)
            gps_time = datetime.strptime(gps_ts.split('.')[0], '%H:%M:%S').time()
            gps_dt = datetime.combine(local_dt.date(), gps_time)
            local_utc_date = (local_dt - timedelta(hours=9)).date()
            if gps_dt.date() > local_utc_date:
                gps_dt -= timedelta(days=1)
            elif gps_dt.date() < local_utc_date:
                gps_dt += timedelta(days=1)
            return gps_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass

    ts = meta.get('captured_at') or ''
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            return (dt - timedelta(hours=9)).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass

    if filename:
        try:
            parts = filename.split('_')
            dt = datetime.strptime(parts[1] + parts[2], '%Y%m%d%H%M%S')
            return (dt - timedelta(hours=9)).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass
    return None


def _response_error_text(exc):
    resp = getattr(exc, 'response', None)
    if resp is None:
        return str(exc)
    reason = getattr(resp, 'reason', '') or ''
    text = getattr(resp, 'text', '') or ''
    if '<html' in text.lower() or '<!doctype' in text.lower():
        return reason or 'HTML error response'
    return text[:500] or reason or str(exc)


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/stream')
def stream():
    def generate():
        last_seq = -1
        while True:
            frame, seq = camera.get_frame_with_seq()
            if frame and seq != last_seq:
                last_seq = seq
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.033)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/status')
def status():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    disk = shutil.disk_usage(PHOTOS_DIR)
    photo_count = len(glob.glob(os.path.join(PHOTOS_DIR, 'star_*.jpg')))

    cpu_temp = None
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            cpu_temp = int(f.read()) / 1000
    except Exception:
        pass

    return jsonify({
        'camera_connected': camera.is_connected(),
        'camera_enabled':   camera.is_enabled(),
        'cpu_temp':         cpu_temp,
        'disk_free':        f'{disk.free / (1024**3):.1f} GB',
        'disk_percent':     disk.used * 100 // disk.total,
        'photo_count':      photo_count,
        'auto_trigger':     camera.get_auto_trigger_status(),
        'periodic':         camera.get_periodic_status(),
        'burst':            camera.get_burst_status(),
        'scout':            camera.get_scout_status(),
        'fpn_enabled':      camera.get_fpn_enabled(),
        **gps.status(),
    })


@app.route('/api/gps')
def gps_detail():
    return jsonify(gps.detail())


@app.route('/api/gps/baud', methods=['GET'])
def gps_baud_get():
    return jsonify({'baud': gps.get_baud(), 'allowed': ALLOWED_BAUDS})


@app.route('/api/gps/baud', methods=['POST'])
def gps_baud_set():
    params = request.get_json(silent=True) or {}
    baud = params.get('baud')
    if baud not in ALLOWED_BAUDS:
        return jsonify({'error': f'baud must be one of {ALLOWED_BAUDS}'}), 400
    gps.set_baud(baud)
    return jsonify({'baud': baud, 'ok': True})


@app.route('/api/capture', methods=['POST'])
def capture():
    params   = request.get_json(silent=True) or {}
    gps_data = gps.get_fix()
    result = camera.capture(params, gps_data, mount_data=_get_mount_snap())
    return jsonify(result)


@app.route('/api/calibrate', methods=['POST'])
def calibrate():
    params     = request.get_json(silent=True) or {}
    shutter_ms = float(params.get('shutter_ms', 3000))
    result     = camera.calibrate(shutter_ms=shutter_ms)
    return jsonify(result)


@app.route('/api/camera/reset', methods=['POST'])
def camera_reset():
    camera.reset_capture_state()
    return jsonify({'ok': True})


@app.route('/api/camera/toggle', methods=['POST'])
def camera_toggle():
    new_state = not camera.is_enabled()
    camera.set_enabled(new_state)
    return jsonify({'enabled': new_state})


@app.route('/api/camera/fpn', methods=['GET', 'POST'])
def camera_fpn():
    if request.method == 'POST':
        params = request.get_json(silent=True) or {}
        enabled = bool(params.get('enabled', True))
        camera.set_fpn_enabled(enabled)
    return jsonify({'enabled': camera.get_fpn_enabled()})


@app.route('/api/camera/preview', methods=['POST'])
def camera_preview():
    params = request.get_json(silent=True) or {}
    max_ms = int(params.get('max_ms', 2000))
    camera.set_preview_max_ms(max_ms)
    return jsonify({'max_ms': camera.get_preview_max_ms(), 'ok': True})


import heapq as _heapq

@app.route('/api/photos')
def list_photos():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    # scandir: 디렉토리 한 번만 읽고 mtime으로 상위 50개만 추출 (전체 sort 불필요)
    try:
        entries = [
            (e.stat().st_mtime, e.path, e.name)
            for e in os.scandir(PHOTOS_DIR)
            if e.name.startswith('star_') and e.name.endswith('.jpg')
        ]
    except Exception:
        entries = []
    top50 = _heapq.nlargest(50, entries, key=lambda x: x[0])
    photos = []
    for mtime, filepath, filename in top50:
        stat_res = os.stat(filepath)
        metadata = None
        sidecar  = filepath.replace('.jpg', '.json')
        if os.path.exists(sidecar):
            try:
                with open(sidecar) as f:
                    metadata = json.load(f)
            except Exception:
                pass
        gps_meta = metadata.get('gps') if metadata else None
        photos.append({
            'filename': filename,
            'size':     stat_res.st_size,
            'mtime':    mtime,
            'has_gps':  bool(gps_meta and gps_meta.get('fix')),
            'metadata': metadata,
        })
    return jsonify(photos)


@app.route('/api/photos/<filename>')
def serve_photo(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    return send_from_directory(PHOTOS_DIR, filename)


@app.route('/api/photos/<filename>/meta')
def serve_meta(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    json_name = filename.replace('.jpg', '.json')
    json_path = os.path.join(PHOTOS_DIR, json_name)
    if not os.path.exists(json_path):
        return jsonify({'error': 'no metadata'}), 404
    return send_from_directory(
        PHOTOS_DIR, json_name,
        as_attachment=True,
        download_name=json_name,
        mimetype='application/json',
    )


@app.route('/api/photos/<filename>/thumb')
def serve_thumb(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    os.makedirs(THUMBS_DIR, exist_ok=True)
    thumb_path = os.path.join(THUMBS_DIR, filename)
    if not os.path.exists(thumb_path):
        orig = os.path.join(PHOTOS_DIR, filename)
        if not os.path.exists(orig):
            return jsonify({'error': 'not found'}), 404
        try:
            from PIL import Image
            img = Image.open(orig)
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            img.save(thumb_path, 'JPEG', quality=THUMB_QUAL, optimize=True)
        except Exception:
            return send_from_directory(PHOTOS_DIR, filename)
    resp = send_from_directory(THUMBS_DIR, filename)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/photos/download', methods=['POST'])
def download_photos():
    params = request.get_json(silent=True) or {}
    filenames = params.get('filenames', [])
    valid = [
        fn for fn in filenames
        if isinstance(fn, str) and fn.startswith('star_') and fn.endswith('.jpg')
        and '/' not in fn and '\\' not in fn and '..' not in fn
    ]
    if not valid:
        return jsonify({'error': 'no valid files'}), 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for fn in valid:
            path = os.path.join(PHOTOS_DIR, fn)
            if os.path.exists(path):
                zf.write(path, fn)
            sidecar = path.replace('.jpg', '.json')
            if os.path.exists(sidecar):
                zf.write(sidecar, fn.replace('.jpg', '.json'))
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='starcam_photos.zip')



@app.route('/api/photos/<filename>/delete', methods=['POST'])
def delete_photo(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    filepath = os.path.join(PHOTOS_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    sidecar = filepath.replace('.jpg', '.json')
    if os.path.exists(sidecar):
        os.remove(sidecar)
    thumb = os.path.join(THUMBS_DIR, filename)
    if os.path.exists(thumb):
        os.remove(thumb)
    return jsonify({'ok': True})


_sky_cache = {}  # {date_str: response dict}
_trig_params = {}   # capture params for auto-trigger
_per_params  = {}   # capture params for periodic

def _do_auto_capture():
    p = dict(_trig_params) or {
        'shutter_ms': 5000, 'gain': 8.0, 'awb': 'none',
        'count': 3, 'interval': 1.0,
        'saturation': 0.0, 'sharpness': 1.5, 'contrast': 1.0, 'quality': 95,
    }
    camera._trig_capturing = True
    try:
        camera.capture(p, gps.get_fix(), suffix="_autodetect")
    finally:
        camera._trig_capturing = False





@app.route('/api/camera/periodic', methods=['GET', 'POST'])
def periodic_api():
    global _per_params
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        enabled    = bool(data.get('enabled', False))
        interval_s = max(10, int(data.get('interval_s', 600)))
        if 'params' in data:
            _per_params = data['params']
        def _do_periodic():
            camera.capture(dict(_per_params), gps.get_fix(), suffix="_periodic")
        camera.set_periodic_capture(
            enabled, interval_s=interval_s,
            callback=_do_periodic if enabled else None)
        return jsonify({**camera.get_periodic_status(), 'ok': True})
    return jsonify(camera.get_periodic_status())




@app.route('/api/camera/burst', methods=['GET', 'POST'])
def burst_api():
    if request.method == 'POST':
        data   = request.get_json(silent=True) or {}
        action = data.get('action', 'start')
        if action == 'start':
            params     = data.get('params', {})
            duration_s = max(10, int(data.get('duration_s', 3600)))
            return jsonify(camera.start_burst(params, duration_s, gps_callback=gps.get_fix, mount_data=_get_mount_snap()))
        if action == 'stop':
            return jsonify(camera.stop_burst())
        return jsonify({'error': 'unknown action'}), 400
    return jsonify(camera.get_burst_status())


@app.route('/api/camera/autotrigger', methods=['GET', 'POST'])
def auto_trigger_api():
    global _trig_params
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        enabled   = bool(data.get('enabled', False))
        threshold = max(5, min(100, int(data.get('threshold', 30))))
        if 'params' in data:
            _trig_params = data['params']
        camera.set_auto_trigger(
            enabled, threshold=threshold,
            callback=_do_auto_capture if enabled else None)
        return jsonify({**camera.get_auto_trigger_status(), 'ok': True})
    return jsonify(camera.get_auto_trigger_status())


@app.route('/api/camera/scout', methods=['GET', 'POST'])
def scout_api():
    if request.method == 'POST':
        data   = request.get_json(silent=True) or {}
        action = data.get('action', 'start')
        if action == 'start':
            _default_scout = {
                'shutter_ms': 500, 'gain': 8, 'awb': 'none',
                'saturation': 0, 'sharpness': 1.5, 'contrast': 1.0, 'quality': 95,
                'pre_frames': 8, 'post_frames': 15,
            }
            scout_params = {**_default_scout, **data.get('scout_params', {})}
            stop_at  = data.get('stop_at_utc')  or None
            start_at = data.get('start_at_utc') or None
            return jsonify(camera.start_scout(scout_params,
                                              stop_at_utc=stop_at,
                                              start_at_utc=start_at,
                                              gps_callback=gps.get_fix,
                                              mount_data=_get_mount_snap()))
        if action == 'stop':
            return jsonify(camera.stop_scout())
        return jsonify({'error': 'unknown action'}), 400
    return jsonify(camera.get_scout_status())


@app.route('/api/sky')
def sky_events():
    from sky_calc import calc_sky_events
    from datetime import datetime, timedelta, timezone, date
    kst = timezone(timedelta(hours=9))
    st = gps.status()
    lat = st.get('latitude') or 37.6
    lon = st.get('longitude') or 127.1
    today = datetime.now(kst).date()
    tomorrow = today + timedelta(days=1)
    cache_key = str(today)
    if cache_key not in _sky_cache:
        _sky_cache.clear()
        ev_today    = calc_sky_events(lat, lon, today)
        ev_tomorrow = calc_sky_events(lat, lon, tomorrow)
        _sky_cache[cache_key] = {'today': ev_today, 'tomorrow': ev_tomorrow,
                                 'lat': lat, 'lon': lon, 'cached': True}
    return jsonify(_sky_cache[cache_key])


_LED_DEFAULTS = {'ACT': 'mmc0', 'PWR': 'default-on'}

def _led_write(led, value):
    try:
        trig   = f'/sys/class/leds/{led}/trigger'
        bright = f'/sys/class/leds/{led}/brightness'
        if value == 0:
            open(trig,   'w').write('none')
            open(bright, 'w').write('0')
        else:
            open(trig,   'w').write(_LED_DEFAULTS.get(led, 'default-on'))
            open(bright, 'w').write('1')
        return True
    except PermissionError:
        return False


@app.route('/api/system/leds', methods=['GET', 'POST'])
def leds_api():
    def _brightness(led):
        try:
            return int(open(f'/sys/class/leds/{led}/brightness').read().strip())
        except Exception:
            return None

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        for led, key in [('ACT', 'act'), ('PWR', 'pwr')]:
            if key in data:
                if not _led_write(led, int(data[key])):
                    return jsonify({'ok': False, 'error': 'permission',
                                    'hint': 'ssh pi "sudo bash ~/starcam/setup-leds.sh"'}), 403
        return jsonify({'ok': True})

    act = _brightness('ACT')
    pwr = _brightness('PWR')
    writeable = os.access('/sys/class/leds/ACT/brightness', os.W_OK)
    return jsonify({'act': act, 'pwr': pwr, 'controllable': writeable})




_mount_was_connected = False

@app.route('/api/mount/status')
def mount_status():
    global _mount_cache, _mount_was_connected
    st = mount_ctrl.status()
    now_conn = bool(st.get('connected'))
    if now_conn and not _mount_was_connected:
        _apply_saved_el_limit()
    _mount_was_connected = now_conn
    if now_conn and st.get('az') is not None:
        _mount_cache = {'az': st['az'], 'el': st.get('el')}
    return jsonify(st)


@app.route('/api/mount/move', methods=['POST'])
def mount_move():
    data = request.get_json(silent=True) or {}
    axis = data.get('axis', 'AZ')
    steps = data.get('steps', 0)
    if axis not in ('AZ', 'EL'):
        return jsonify({'ok': False, 'error': 'axis must be AZ or EL'}), 400
    if not isinstance(steps, (int, float)) or steps == 0:
        return jsonify({'ok': False, 'error': 'invalid steps'}), 400
    return jsonify(mount_ctrl.move(axis, int(steps)))


@app.route('/api/mount/goto', methods=['POST'])
def mount_goto():
    data = request.get_json(silent=True) or {}
    axis = data.get('axis', 'AZ')
    deg  = data.get('deg')
    if axis not in ('AZ', 'EL'):
        return jsonify({'ok': False, 'error': 'axis must be AZ or EL'}), 400
    if deg is None:
        return jsonify({'ok': False, 'error': 'deg required'}), 400
    return jsonify(mount_ctrl.goto(axis, float(deg)))


@app.route('/api/mount/config', methods=['GET', 'POST'])
def mount_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if 'steps_per_deg' in data:
            return jsonify(mount_ctrl.set_steps_per_deg(data['steps_per_deg']))
    return jsonify({'connected': mount_ctrl.is_connected()})


@app.route('/api/mount/invert', methods=['POST'])
def mount_invert():
    data = request.get_json(silent=True) or {}
    axis   = data.get('axis', 'AZ')
    invert = int(bool(data.get('invert', False)))
    if axis not in ('AZ', 'EL'):
        return jsonify({'ok': False, 'error': 'axis must be AZ or EL'}), 400
    return jsonify(mount_ctrl._send_command(f'INVERT_{axis} {invert}'))

@app.route('/api/mount/goto_both', methods=['POST'])
def mount_goto_both():
    data   = request.get_json(silent=True) or {}
    az_deg = data.get('az_deg')
    el_deg = data.get('el_deg')
    if az_deg is None or el_deg is None:
        return jsonify({'ok': False, 'error': 'az_deg and el_deg required'}), 400
    return jsonify(mount_ctrl.goto_both(float(az_deg), float(el_deg)))


@app.route('/api/mount/calibrate', methods=['POST'])
def mount_calibrate():
    data = request.get_json(silent=True) or {}
    axis = data.get('axis', 'AZ').upper()
    if axis not in ('AZ', 'EL'):
        return jsonify({'ok': False, 'error': 'axis must be AZ or EL'}), 400
    return jsonify(mount_ctrl.calibrate(axis))


@app.route('/api/mount/el_limit', methods=['GET', 'POST', 'DELETE'])
def mount_el_limit():
    if request.method == 'DELETE':
        _save_mount_cfg({'el_min': None, 'el_max': None})
        return jsonify(mount_ctrl.clear_el_limit())
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        sky_min = data.get('min_deg')
        sky_max = data.get('max_deg')
        if sky_min is None or sky_max is None:
            return jsonify({'ok': False, 'error': 'min_deg and max_deg required'}), 400
        sky_min, sky_max = float(sky_min), float(sky_max)
        if sky_min >= sky_max:
            return jsonify({'ok': False, 'error': 'min_deg must be less than max_deg'}), 400
        cfg = _save_mount_cfg({'el_min': sky_min, 'el_max': sky_max})
        el_off = cfg.get('el_offset') or 0.0
        return jsonify(mount_ctrl.set_el_limit(sky_min - el_off, sky_max - el_off))
    return jsonify(mount_ctrl.status())



@app.route('/api/mount/settings', methods=['GET', 'POST'])
def mount_settings():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        allowed = {'az_offset', 'el_offset', 'el_min', 'el_max', 'pixel_scale', 'ra_hint', 'dec_hint', 'cal_server', 'cal_token'}
        cfg = _save_mount_cfg({k: v for k, v in data.items() if k in allowed})
        return jsonify({**cfg, 'ok': True})
    return jsonify(_load_mount_cfg())


@app.route('/api/mount/auto_calibrate', methods=['POST'])
def mount_auto_calibrate():
    import requests as _req
    from datetime import datetime, timezone as _tz

    data       = request.get_json(silent=True) or {}
    cfg        = _load_mount_cfg()
    cal_server = (data.get('cal_server') or cfg.get('cal_server') or '').rstrip('/')
    cal_token  = data.get('cal_token')  or cfg.get('cal_token') or ''
    if not cal_server or not cal_token:
        return jsonify({'ok': False, 'error': 'cal_server/cal_token 미설정'}), 400

    import time as _tm; _t0 = _tm.time()
    filename = (data.get('filename') or '').strip()
    meta = {}
    source = 'capture'
    warnings = []

    if filename:
        if not _valid_photo_name(filename):
            return jsonify({'ok': False, 'error': 'invalid filename'}), 400
        image_path = os.path.join(PHOTOS_DIR, filename)
        if not os.path.exists(image_path):
            return jsonify({'ok': False, 'error': '사진 파일 없음'}), 404
        meta = _load_photo_meta(filename)
        gps_data = meta.get('gps')
        source = 'existing'
        result = {'saved': [filename]}
    else:
        gps_data = gps.get_fix()
        if not gps_data:
            return jsonify({'ok': False, 'error': 'GPS fix 없음'}), 400

        shutter_ms = float(data.get('shutter_ms', 2000))
        gain       = float(data.get('gain', 8.0))
        result = camera.capture(
            {'shutter_ms': shutter_ms, 'gain': gain, 'awb': 'none',
             'count': 1, 'saturation': 0, 'sharpness': 1.5, 'contrast': 1.0, 'quality': 95},
            gps_data, suffix='_astrocal')
        print(f'[autocal] capture {_tm.time()-_t0:.1f}s', flush=True)
        if not result.get('saved'):
            return jsonify({'ok': False, 'error': '촬영 실패: ' + (result.get('error') or '?')}), 500
        filename = result['saved'][0]
        image_path = os.path.join(PHOTOS_DIR, filename)
        meta = _load_photo_meta(filename)

    if not (gps_data and gps_data.get('fix') and
            gps_data.get('latitude') is not None and gps_data.get('longitude') is not None):
        return jsonify({'ok': False, 'error': '사진/GPS fix 없음'}), 400

    enc_az = enc_el = None
    mount_meta = meta.get('mount') if meta else None
    if mount_meta and mount_meta.get('az') is not None:
        enc_az, enc_el = mount_meta.get('az'), mount_meta.get('el')
    elif source == 'existing':
        warnings.append('사진 사이드카에 마운트 AZ/EL이 없어 오프셋은 업데이트하지 않음')
    else:
        try:
            st = mount_ctrl.status()
            if st.get('connected') and st.get('az') is not None:
                enc_az, enc_el = st['az'], st.get('el')
        except Exception:
            pass

    pixel_scale = cfg.get('pixel_scale')
    timestamp = _plate_solve_timestamp(meta, filename)
    if not timestamp:
        timestamp = datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        warnings.append('촬영 UTC 시각이 없어 현재 UTC를 사용함')
    form = {
        'timestamp': timestamp,
        'lat': str(gps_data['latitude']),
        'lon': str(gps_data['longitude']),
    }
    if pixel_scale:
        # Tighter range when scale is known — speeds solve from 60-180s to ~10s
        form['scale_low']  = str(round(pixel_scale * 0.9, 4))
        form['scale_high'] = str(round(pixel_scale * 1.1, 4))
    if cfg.get('ra_hint') is not None and cfg.get('dec_hint') is not None:
        warnings.append('이동식 관측소 운용을 위해 RA/Dec 힌트는 전송하지 않음')

    try:
        with open(image_path, 'rb') as img_f:
            resp = _req.post(
                f'{cal_server}/api/calibrate',
                headers={'Authorization': f'Bearer {cal_token}'},
                files={'image': img_f},
                data=form,
                timeout=200,
            )
        resp.raise_for_status()
        solved = resp.json()
        print(f'[autocal] total {_tm.time()-_t0:.1f}s  server elapsed={solved.get("elapsed")}s', flush=True)
    except _req.exceptions.Timeout:
        return jsonify({
            'ok': False, 'error': 'Plate solve 타임아웃 (180s)',
            'image': filename, 'source': source,
            'timestamp': timestamp, 'warnings': warnings,
        }), 408
    except Exception as e:
        code = getattr(getattr(e, 'response', None), 'status_code', 500)
        msg  = _response_error_text(e)
        return jsonify({
            'ok': False, 'error': f'서버 오류 {code}: {msg}',
            'image': filename, 'source': source,
            'timestamp': timestamp, 'warnings': warnings,
        }), 502

    updates = {}
    if solved.get('pixel_scale'):
        updates['pixel_scale'] = solved['pixel_scale']   # always update with latest
    if solved.get('ra') is not None:
        updates['ra_hint']  = solved['ra']
        updates['dec_hint'] = solved['dec']
    az_offset = el_offset = None
    if enc_az is not None and solved.get('az') is not None:
        az_offset = round(solved['az'] - enc_az, 4)
        el_offset = round((solved.get('el') or 0) - (enc_el or 0), 4)
        updates['az_offset'] = az_offset
        updates['el_offset'] = el_offset
    if updates:
        _save_mount_cfg(updates)

    return jsonify({
        'ok': True, 'solved': solved,
        'encoder': {'az': enc_az, 'el': enc_el},
        'az_offset': az_offset, 'el_offset': el_offset,
        'image': filename,
        'source': source,
        'timestamp': timestamp,
        'warnings': warnings,
    })



# ════════════════════════════════════════════════════════════
#  Scheduled Capture
# ════════════════════════════════════════════════════════════
import uuid as _uuid
import threading as _threading
from datetime import datetime, timezone as _tz, timedelta as _td

_scheduled_jobs = {}   # id → job dict
_sched_lock     = _threading.Lock()

def _run_scheduled_job(job):
    try:
        target = job['utc_dt']

        # 1. Camera warmup
        job['status'] = 'warming'
        job['status_msg'] = '카메라 워밍업 중…'
        if not camera.is_enabled():
            job['_camera_was_off'] = True
            camera.set_enabled(True)
            import time as _time; _time.sleep(3)

        # 2. Goto (if AZ/EL specified)
        if job.get('az') is not None and job.get('el') is not None:
            job['status'] = 'goto'
            job['status_msg'] = f"GOTO AZ {job['az']:.1f}° EL {job['el']:.1f}°"
            cfg    = _load_mount_cfg()
            az_off = cfg.get('az_offset') or 0.0
            el_off = cfg.get('el_offset') or 0.0
            az_enc = ((job['az'] - az_off) % 360 + 360) % 360
            el_enc = job['el'] - el_off
            res = mount_ctrl.goto_both(az_enc, el_enc)
            if not res.get('ok'):
                job['status']     = 'error'
                job['status_msg'] = 'goto 실패: ' + (res.get('error') or '?')
                return

        # 3. Wait until target time
        import time as _time
        now  = datetime.now(_tz.utc)
        wait = (target - now).total_seconds()
        if wait > 0:
            job['status']     = 'waiting'
            job['status_msg'] = f'{wait:.0f}초 대기 중…'
            _time.sleep(wait)

        # 4. Capture
        if job['status'] == 'cancelled':
            return
        job['status']     = 'capturing'
        job['status_msg'] = '촬영 중…'
        gps_data = gps.get_fix()
        cap_res  = camera.capture(job['params'], gps_data, mount_data=_get_mount_snap())
        saved_n  = len(cap_res.get('saved') or [])
        job['status']     = 'done'
        job['result']     = cap_res
        job['status_msg'] = f'{saved_n}장 저장됨' + (' · 오류: ' + cap_res['error'] if cap_res.get('error') and not saved_n else '')

        # 5. Camera off
        if job.get('camera_off_after', True):
            _time.sleep(1)
            camera.set_enabled(False)
            job['status_msg'] += ' · 카메라 꺼짐'

    except Exception as e:
        job['status']     = 'error'
        job['status_msg'] = '오류: ' + str(e)


def _scheduler_loop():
    import time as _time
    while True:
        _time.sleep(5)
        now = datetime.now(_tz.utc)
        with _sched_lock:
            pending = [j for j in _scheduled_jobs.values() if j['status'] == 'pending']
        for job in pending:
            lead = _td(seconds=job.get('lead_secs', 90))
            if now >= job['utc_dt'] - lead:
                job['status'] = 'warming'   # claim before thread starts
                _threading.Thread(target=_run_scheduled_job, args=(job,), daemon=True).start()

_threading.Thread(target=_scheduler_loop, daemon=True, name='scheduler').start()


@app.route('/api/schedule', methods=['GET'])
def schedule_list():
    with _sched_lock:
        jobs = sorted(_scheduled_jobs.values(), key=lambda x: x['utc_iso'])
    return jsonify([
        {k: v for k, v in j.items() if not k.startswith('_') and k != 'utc_dt'}
        for j in jobs
    ])


@app.route('/api/schedule', methods=['POST'])
def schedule_add():
    data    = request.get_json(silent=True) or {}
    utc_iso = data.get('utc_iso', '').strip()
    if not utc_iso:
        return jsonify({'ok': False, 'error': 'utc_iso required'}), 400
    try:
        utc_dt = datetime.fromisoformat(utc_iso.replace('Z', '+00:00'))
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=_tz.utc)
    except Exception:
        return jsonify({'ok': False, 'error': 'utc_iso 형식 오류 (예: 2026-05-31T22:30:00Z)'}), 400

    now = datetime.now(_tz.utc)
    lead = int(data.get('lead_secs', 90))
    if utc_dt <= now + _td(seconds=lead):
        return jsonify({'ok': False, 'error': f'이동 준비 시간({lead}s) 포함 {lead}초 이상 남은 시간만 예약 가능'}), 400

    job_id = _uuid.uuid4().hex[:8]
    job = {
        'id':              job_id,
        'utc_iso':         utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'utc_dt':          utc_dt,
        'az':              data.get('az'),
        'el':              data.get('el'),
        'params':          data.get('params', {}),
        'lead_secs':       lead,
        'camera_off_after': bool(data.get('camera_off_after', True)),
        'status':          'pending',
        'status_msg':      '대기 중',
        'result':          None,
        'error':           None,
        'created_at':      now.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    with _sched_lock:
        _scheduled_jobs[job_id] = job
    return jsonify({'ok': True, 'id': job_id, 'utc_iso': job['utc_iso']})


@app.route('/api/schedule/<job_id>', methods=['DELETE'])
def schedule_cancel(job_id):
    with _sched_lock:
        job = _scheduled_jobs.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    if job['status'] not in ('pending', 'warming', 'waiting'):
        return jsonify({'ok': False, 'error': f'취소 불가 (상태: {job["status"]})'}), 400
    job['status']     = 'cancelled'
    job['status_msg'] = '취소됨'
    return jsonify({'ok': True})


if __name__ == '__main__':
    gps.start()
    camera.start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
