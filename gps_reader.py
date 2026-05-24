"""
GPS reader: gpsd socket client (JSON protocol).
Connects to gpsd at localhost:2947 - no direct serial access, no port conflict.
"""

import json
import socket
import threading
import time

GPSD_HOST = '127.0.0.1'
GPSD_PORT = 2947


class GPSReader:
    def __init__(self):
        self._lock = threading.Lock()
        self._connected = False
        self._fix = False
        self._fix_quality = 0
        self._lat = None
        self._lon = None
        self._alt = None
        self._satellites = 0
        self._hdop = None
        self._speed_kmh = None
        self._track = None      # 진방위각 COG (true north)
        self._magtrack = None   # 자방위각 (magnetic north)
        self._magvar = None     # 자기 편차
        self._utc_time = None
        self._utc_date = None
        self._mode = 0
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self._running:
            try:
                self._connect_and_read()
            except Exception:
                pass
            with self._lock:
                self._connected = False
                self._fix = False
            time.sleep(3)

    def _connect_and_read(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect((GPSD_HOST, GPSD_PORT))
            with self._lock:
                self._connected = True
            sock.sendall(b'?WATCH={"enable":true,"json":true,"nmea":true}\n')
            buf = ''
            while self._running:
                try:
                    chunk = sock.recv(4096).decode('utf-8', errors='ignore')
                    if not chunk:
                        break
                    buf += chunk
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if line:
                            self._handle(line)
                except socket.timeout:
                    continue

    def _handle(self, line):
        if line.startswith('$'):
            self._parse_nmea(line)
            return
        try:
            msg = json.loads(line)
        except Exception:
            return
        cls = msg.get('class')
        if cls == 'TPV':
            self._parse_tpv(msg)
        elif cls == 'SKY':
            self._parse_sky(msg)

    def _parse_tpv(self, msg):
        mode = msg.get('mode', 0)
        with self._lock:
            self._mode = mode
            self._fix = mode >= 2
            self._fix_quality = mode
            if 'lat' in msg:
                self._lat = round(msg['lat'], 7)
            if 'lon' in msg:
                self._lon = round(msg['lon'], 7)
            if 'altMSL' in msg:
                self._alt = round(msg['altMSL'], 2)
            elif 'alt' in msg:
                self._alt = round(msg['alt'], 2)
            if 'speed' in msg:
                self._speed_kmh = round(msg['speed'] * 3.6, 1)
            if 'track' in msg:
                self._track = round(msg['track'], 2)
            if 'magtrack' in msg:
                self._magtrack = round(msg['magtrack'], 2)
            if 'magvar' in msg:
                self._magvar = round(msg['magvar'], 2)
            if 'time' in msg:
                raw = msg['time']  # e.g. "2026-05-17T09:56:31.000Z"
                if 'T' in raw:
                    date_part, time_part = raw.replace('Z', '').split('T', 1)
                    self._utc_date = date_part
                    self._utc_time = time_part[:8]
                else:
                    self._utc_time = raw[:8]

    def _parse_sky(self, msg):
        sats = msg.get('satellites', [])
        with self._lock:
            if sats:
                self._satellites = sum(1 for s in sats if s.get('used'))
            elif 'uSat' in msg:
                self._satellites = msg['uSat']
            # satellites 없으면 NMEA GGA에서 채움 (아래 _parse_nmea)
            if 'hdop' in msg:
                self._hdop = msg['hdop']

    def _parse_nmea(self, line):
        """/에서 위성 수 파싱 (native UBX 모드 보완)"""
        try:
            if not line.startswith('$'):
                return
            parts = line.split(',')
            if len(parts) > 7 and parts[0].endswith('GGA'):
                num_sv = int(parts[7]) if parts[7].isdigit() else 0
                if num_sv > 0:
                    with self._lock:
                        self._satellites = num_sv
        except Exception:
            pass

    # ── Public API (backward compatible) ──────────────────────────

    def status(self):
        with self._lock:
            return {
                'gps_connected':   self._connected,
                'gps_fix':         self._fix,
                'gps_fix_quality': self._fix_quality,
                'latitude':        self._lat,
                'longitude':       self._lon,
                'altitude':        self._alt,
                'satellites':      self._satellites,
                'hdop':            self._hdop,
                'speed_kmh':       self._speed_kmh,
                'course':          self._track,    # 기존 호환
                'track':           self._track,    # 진방위각
                'magtrack':        self._magtrack, # 자방위각
                'magvar':          self._magvar,
                'utc_time':        self._utc_time,
                'utc_date':        self._utc_date,
                'gps_port':        'gpsd',
                'gps_baud':        None,
            }

    def get_fix(self):
        with self._lock:
            if not self._fix:
                return None
            if self._utc_date and self._utc_time:
                utc_str = f'{self._utc_date}T{self._utc_time}Z'
            else:
                utc_str = self._utc_time
            return {
                'fix':        True,
                'latitude':   self._lat,
                'longitude':  self._lon,
                'altitude':   self._alt,
                'satellites': self._satellites,
                'hdop':       self._hdop,
                'track':      self._track,
                'magtrack':   self._magtrack,
                'magvar':     self._magvar,
                'utc':        utc_str,
            }

    def detail(self):
        with self._lock:
            return {
                'connected':   self._connected,
                'fix':         self._fix,
                'fix_quality': self._fix_quality,
                'latitude':    self._lat,
                'longitude':   self._lon,
                'altitude':    self._alt,
                'satellites':  self._satellites,
                'hdop':        self._hdop,
                'speed_kmh':   self._speed_kmh,
                'course':      self._track,
                'track':       self._track,
                'magtrack':    self._magtrack,
                'magvar':      self._magvar,
                'utc_time':    self._utc_time,
                'utc_date':    self._utc_date,
                'port':        'gpsd://127.0.0.1:2947',
                'baud':        None,
            }

    def get_baud(self):
        return None

    def set_baud(self, baud):
        pass
