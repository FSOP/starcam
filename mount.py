import serial
import glob
import threading
import time
import re


class MountController:
    def __init__(self):
        self._ser  = None
        self._lock = threading.Lock()
        self._port = None

    def _find_port(self):
        for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
            ports = glob.glob(pattern)
            if ports:
                return sorted(ports)[0]
        return None

    def _connect(self):
        port = self._find_port()
        if not port:
            return False
        try:
            self._ser  = serial.Serial(port, 115200, timeout=10)
            self._port = port
            time.sleep(2)
            return True
        except Exception:
            self._ser = None
            return False

    def _parse_response(self, resp):
        result = {'raw_resp': resp}
        if not resp:
            return result
        if resp.startswith('READY'):
            result['status'] = 'ready'
        elif resp.startswith('DONE'):
            result['status'] = 'done'
        # 공백 허용: "AZ: 132.36" 형태도 파싱
        for m in re.finditer(r'([A-Z_]+):\s*([0-9.+-]+)', resp):
            key = m.group(1).lower()
            val = m.group(2)
            try:
                result[key] = float(val) if '.' in val else int(val)
            except ValueError:
                pass
        return result

    def _send_command(self, cmd, timeout=None):
        with self._lock:
            if not self.is_connected():
                if not self._connect():
                    return {'ok': False, 'error': 'Arduino not connected'}
            try:
                old_timeout = self._ser.timeout
                if timeout is not None:
                    self._ser.timeout = timeout
                self._ser.reset_input_buffer()
                self._ser.write((cmd + '\n').encode())
                resp = self._ser.readline().decode().strip()
                self._ser.timeout = old_timeout
                parsed = self._parse_response(resp)
                parsed['ok']      = True
                parsed['command'] = cmd
                return parsed
            except Exception as e:
                self._ser = None
                return {'ok': False, 'error': str(e), 'command': cmd}

    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    def move(self, axis, steps):
        return self._send_command(f'MOVE_{axis.upper()} {int(steps)}')

    def goto(self, axis, target_deg):
        return self._send_command(
            f'GOTO_{axis.upper()} {float(target_deg):.2f}', timeout=60)

    def goto_both(self, az_deg, el_deg):
        """AZ + EL 동시 이동"""
        return self._send_command(
            f'GOTO {float(az_deg):.2f} {float(el_deg):.2f}', timeout=90)

    def calibrate(self, axis='AZ'):
        cmd = 'CAL' if axis.upper() == 'AZ' else 'CAL_EL'
        return self._send_command(cmd, timeout=30)

    def set_el_limit(self, min_deg, max_deg):
        return self._send_command(
            f'SET_EL_LIMIT {float(min_deg):.1f} {float(max_deg):.1f}')

    def clear_el_limit(self):
        return self._send_command('CLR_EL_LIMIT')

    def set_steps_per_deg(self, value):
        return self._send_command(f'SET_SPD {float(value):.4f}')

    def status(self):
        response = self._send_command('STATUS')
        return {'connected': self.is_connected(), 'port': self._port, **response}
