import time
import threading


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
            try:
                import gps
                session = gps.gps(host='localhost', port='2947')
                session.stream(gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE)
                with self._lock:
                    self._connected = True

                for report in session:
                    if not self._running:
                        break
                    cls = report.get('class', '')

                    if cls == 'TPV':
                        mode = report.get('mode', 0)
                        if mode >= 2:
                            with self._lock:
                                self._fix = True
                                self._lat = report.get('lat', self._lat)
                                self._lon = report.get('lon', self._lon)
                                self._alt = report.get('altHAE') or report.get('alt')
                        else:
                            with self._lock:
                                self._fix = False

                    elif cls == 'SKY':
                        used = report.get('uSat', 0)
                        if not used:
                            sats = report.get('satellites', [])
                            used = sum(1 for s in sats if s.get('used', False))
                        with self._lock:
                            self._satellites = used

            except Exception:
                pass
            finally:
                with self._lock:
                    self._connected = False
                    self._fix = False
            time.sleep(5)

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
