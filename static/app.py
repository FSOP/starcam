import os
import glob
import json
import time
import shutil

from flask import Flask, Response, jsonify, request, send_from_directory
from camera import Camera
from gps_reader import GPSReader

PHOTOS_DIR = os.path.expanduser('~/photos')

app = Flask(__name__, static_folder='static')
camera = Camera()
gps = GPSReader()


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/stream')
def stream():
    def generate():
        while True:
            frame = camera.get_frame()
            if frame:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.3)
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
        'cpu_temp': cpu_temp,
        'disk_free': f'{disk.free / (1024**3):.1f} GB',
        'disk_percent': disk.used * 100 // disk.total,
        'photo_count': photo_count,
        **gps.status(),
    })


@app.route('/api/capture', methods=['POST'])
def capture():
    params = request.get_json(silent=True) or {}
    gps_data = gps.get_fix()
    result = camera.capture(params, gps_data)
    return jsonify(result)


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
        gps = metadata.get('gps') if metadata else None
        photos.append({
            'filename': filename,
            'size': stat.st_size,
            'mtime': stat.st_mtime,
            'has_gps': bool(gps and gps.get('fix')),
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


if __name__ == '__main__':
    gps.start()
    camera.start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
