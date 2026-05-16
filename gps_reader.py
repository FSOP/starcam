import time
import threading

GPS_PORTS = ['/dev/ttyAMA0', '/dev/serial0', '/dev/ttyS0', '/dev/ttyUSB0', '/dev/ttyACM0']
GPS_BAUD = 9600


class GPSReader:
    def __init__(self):
        self._lock = threading.Lock()
        self._connected = False
        self._fix = False
        self._lat = None
        self._lon = None
        self._alt = None
        self._satellites = 0
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            port = self._find_port()
            if port:
                self._read_port(port)
            else:
                time.sleep(10)

    def _find_port(self):
        try:
            import serial
        except ImportError:
            return None
        for port in GPS_PORTS:
            try:
                with serial.Serial(port, GPS_BAUD, timeout=3) as s:
                    line = s.readline().decode('ascii', errors='ignore')
                if line.startswith('$GP') or line.startswith('$GN'):
                    return port
            except Exception:
                continue
        return None

    def _read_port(self, port):
        try:
            import serial
            with serial.Serial(port, GPS_BAUD, timeout=2) as ser:
                with self._lock:
                    self._connected = True
                while self._running:
                    try:
                        line = ser.readline().decode('ascii', errors='ignore').strip()
                        self._parse(line)
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            with self._lock:
                self._connected = False
                self._fix = False

    def _parse(self, sentence):
        if not sentence.startswith('$'):
            return
        parts = sentence.split(',')
        try:
            if parts[0] in ('$GPRMC', '$GNRMC') and len(parts) >= 7:
                if parts[2] == 'A':
                    lat = _nmea_to_deg(parts[3], parts[4])
                    lon = _nmea_to_deg(parts[5], parts[6])
                    with self._lock:
                        self._fix = True
                        self._lat = lat
                        self._lon = lon
                else:
                    with self._lock:
                        self._fix = False

            elif parts[0] in ('$GPGGA', '$GNGGA') and len(parts) >= 10:
                if parts[6] != '0':
                    lat = _nmea_to_deg(parts[2], parts[3])
                    lon = _nmea_to_deg(parts[4], parts[5])
                    alt = float(parts[9]) if parts[9] else None
                    sats = int(parts[7]) if parts[7] else 0
                    with self._lock:
                        self._fix = True
                        self._lat = lat
                        self._lon = lon
                        self._alt = alt
                        self._satellites = sats
        except (ValueError, IndexError):
            pass

    def status(self):
        with self._lock:
            return {
                'gps_connected': self._connected,
                'gps_fix': self._fix,
                'latitude': self._lat,
                'longitude': self._lon,
                'altitude': self._alt,
                'satellites': self._satellites,
            }

    def get_fix(self):
        with self._lock:
            if not self._fix:
                return None
            return {
                'fix': True,
                'latitude': self._lat,
                'longitude': self._lon,
                'altitude': self._alt,
                'satellites': self._satellites,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }


def _nmea_to_deg(value, direction):
    if not value:
        return None
    dot = value.index('.')
    deg = float(value[:dot - 2])
    minutes = float(value[dot - 2:])
    result = deg + minutes / 60
    if direction in ('S', 'W'):
        result = -result
    return round(result, 6)
