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

PHOTOS_DIR = os.path.expanduser('~/photos')

ALLOWED_BAUDS = [4800, 9600, 19200, 38400, 57600, 115200]

app = Flask(__name__, static_folder='static')
camera = Camera()
gps = GPSReader()


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
    result   = camera.capture(params, gps_data)
    return jsonify(result)


@app.route('/api/calibrate', methods=['POST'])
def calibrate():
    params     = request.get_json(silent=True) or {}
    shutter_ms = float(params.get('shutter_ms', 3000))
    result     = camera.calibrate(shutter_ms=shutter_ms)
    return jsonify(result)


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


@app.route('/api/photos')
def list_photos():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    photos = []
    for filepath in sorted(
        glob.glob(os.path.join(PHOTOS_DIR, 'star_*.jpg')), reverse=True
    )[:50]:
        filename = os.path.basename(filepath)
        stat = os.stat(filepath)
        metadata = None
        sidecar = filepath.replace('.jpg', '.json')
        if os.path.exists(sidecar):
            try:
                with open(sidecar) as f:
                    metadata = json.load(f)
            except Exception:
                pass
        gps_meta = metadata.get('gps') if metadata else None
        photos.append({
            'filename': filename,
            'size':     stat.st_size,
            'mtime':    stat.st_mtime,
            'has_gps':  bool(gps_meta and gps_meta.get('fix')),
            'metadata': metadata,
        })
    return jsonify(photos)


@app.route('/api/photos/<filename>')
def serve_photo(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    return send_from_directory(PHOTOS_DIR, filename)


@app.route('/api/photos/<filename>/thumb')
def serve_thumb(filename):
    if not (filename.endswith('.jpg') and filename.startswith('star_')):
        return jsonify({'error': 'invalid filename'}), 400
    return send_from_directory(PHOTOS_DIR, filename)


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
            return jsonify(camera.start_burst(params, duration_s, gps_callback=gps.get_fix))
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
                'shutter_ms': 1000, 'gain': 8, 'awb': 'none',
                'saturation': 0, 'sharpness': 1.5, 'contrast': 1.0, 'quality': 95,
                'pre_frames': 8, 'post_frames': 15,
            }
            scout_params = {**_default_scout, **data.get('scout_params', {})}
            stop_at  = data.get('stop_at_utc')  or None
            start_at = data.get('start_at_utc') or None
            return jsonify(camera.start_scout(scout_params,
                                              stop_at_utc=stop_at,
                                              start_at_utc=start_at,
                                              gps_callback=gps.get_fix))
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


if __name__ == '__main__':
    gps.start()
    camera.start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
