# StarCam — CLAUDE.md

> 라즈베리파이 3B + IMX296 Global Shutter Camera + GPS로 인공위성·별 궤적 촬영하는 웹 앱.
> 세션 간 컨텍스트 유지를 위한 레퍼런스 문서.

---

## 환경

| 항목 | 값 |
|------|-----|
| 하드웨어 | Raspberry Pi 3B |
| 카메라 | IMX296 Global Shutter (1456×1088) |
| GPS | GNSS 모듈 — `/dev/serial0` (mini UART, ttyS0), 9600 baud |
| OS | Raspberry Pi OS (Debian Bookworm) |
| Python | 3.13 |
| 사용자 | `insoo` (홈: `/home/insoo`) |
| 앱 경로 | `/home/insoo/starcam/` |
| 사진 저장 | `/home/insoo/photos/` |
| 로그 | `/tmp/starcam.log` |
| 웹 포트 | 5000 |
| SSH 프로파일 | `pi` (ssh 설정에 등록됨) |

---

## 앱 시작 / 재시작

```bash
# 기존 프로세스 종료
ssh pi 'pkill -f app.py'

# 백그라운드로 재시작
ssh pi 'cd /home/insoo/starcam && nohup python3 app.py >> /tmp/starcam.log 2>&1 &'

# 로그 확인
ssh pi 'tail -20 /tmp/starcam.log'

# 앱 동작 확인
ssh pi 'curl -s http://localhost:5000/api/status | python3 -m json.tool'
ssh pi 'curl -s http://localhost:5000/api/gps | python3 -m json.tool'
```

> **주의**: `pkill -f app.py`는 프로세스가 없으면 exit 1을 반환하므로, 재시작 스크립트에서는 두 명령을 분리해서 실행해야 한다. `pkill ... ; restart_cmd` (세미콜론) 또는 `pkill ... || true && restart_cmd` 방식 사용.

---

## 디렉토리 구조

```
starcam/
├── app.py           # Flask 라우트 + API 엔드포인트
├── camera.py        # IMX296 카메라 래퍼 (picamera2 / subprocess 폴백)
├── gps_reader.py    # pyserial 기반 NMEA 파서 (gpsd 불필요)
├── static/
│   └── index.html   # 단일 페이지 Web UI (촬영 / 갤러리 / GPS 탭)
├── CLAUDE.md        # 이 파일 (세션 간 레퍼런스)
├── requirements.txt # flask, pyserial
└── .gitignore
```

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET  | `/` | Web UI |
| GET  | `/stream` | MJPEG 실시간 스트림 (0.3s 간격) |
| GET  | `/api/status` | 카메라·GPS·시스템 통합 상태 |
| GET  | `/api/gps` | GPS 상세 상태 (포트, baud, HDOP, 속도 등) |
| GET  | `/api/gps/baud` | 현재 baud rate + 허용 목록 |
| POST | `/api/gps/baud` | baud rate 변경 `{"baud": 9600}` |
| POST | `/api/capture` | 사진 촬영 |
| POST | `/api/calibrate` | 자동 게인 보정 |
| POST | `/api/camera/toggle` | 카메라 on/off 토글 (실제 센서 해제) |
| GET  | `/api/photos` | 사진 목록 (최신 50장, GPS 메타 포함) |
| GET  | `/api/photos/<f>` | 원본 이미지 |
| GET  | `/api/photos/<f>/thumb` | 썸네일 (현재 원본과 동일) |
| POST | `/api/photos/<f>/delete` | 사진 삭제 (jpg + json 함께) |
| POST | `/api/photos/download` | 선택 사진 ZIP 다운로드 `{"filenames": [...]}` |

### `/api/status` 응답 필드

```json
{
  "camera_connected": true,
  "camera_enabled": true,
  "cpu_temp": 44.0,
  "disk_free": "8.0 GB",
  "disk_percent": 36,
  "photo_count": 77,
  "gps_connected": true,
  "gps_fix": true,
  "gps_fix_quality": 1,
  "gps_port": "/dev/serial0",
  "gps_baud": 9600,
  "latitude": 37.5587,
  "longitude": 127.1467,
  "altitude": 128.0,
  "satellites": 9,
  "hdop": 2.2,
  "speed_kmh": 0.0,
  "course": 66.5,
  "utc_time": "09:56:31",
  "utc_date": "2026-05-17"
}
```

### `/api/gps` 추가 필드

```json
{
  "fix_quality": 1,
  "last_sentence": "$GNGGA,...",
  "candidate_ports": ["/dev/serial0", "/dev/ttyAMA0", "..."]
}
```

### `/api/photos/download` 요청/응답

```bash
# 요청
curl -X POST http://localhost:5000/api/photos/download \
  -H 'Content-Type: application/json' \
  -d '{"filenames": ["star_20260517_105030_123_001.jpg"]}' \
  --output photos.zip

# 응답: application/zip (선택한 .jpg + 각 .json 사이드카 포함)
```

---

## gps_reader.py 아키텍처

### 핵심 설계 결정

**gpsd 대신 pyserial 직접 사용** — 재부팅 후 gpsd `DEVICES=""` 문제로 연결 안 됨.
`serial.Serial('/dev/serial0', 9600)` 직접 열어서 NMEA 파싱.

### 동작 방식

1. `start()` → 백그라운드 스레드 시작
2. `_find_port()` → `CANDIDATE_PORTS` 순서로 포트 탐색, 데이터 오는 첫 포트 사용
3. `_read_loop()` → 64바이트씩 읽어서 `\n` 기준으로 NMEA 문장 분리
4. `_parse()` → `$GNGGA` / `$GNRMC` / `$GNZDA` 파싱
5. 연결 끊기면 3초 후 재탐색

### 파싱하는 NMEA 문장

| 문장 | 파싱 데이터 |
|------|------------|
| `$GNGGA` / `$GPGGA` | 위도, 경도, fix quality, 위성 수, HDOP, 고도, UTC 시각 |
| `$GNRMC` / `$GPRMC` | 위도, 경도, 속도(knot→km/h), 방위각, UTC 날짜 |
| `$GNZDA` / `$GPZDA` | UTC 날짜 및 시각 |

### Baud Rate 변경

```python
gps.set_baud(9600)   # 런타임 변경 — 다음 read에서 자동 적용
gps.get_baud()       # 현재 baud
```

---

## GPS 시리얼 포트 구성

### 라즈베리파이 3B UART 매핑

- `/dev/serial0` → `ttyS0` (mini UART, GPIO 14/15) — **GPS 연결**
- `ttyAMA1` (PL011 full UART) → Bluetooth 내부 사용
- `enable_uart=1` in `/boot/firmware/config.txt`

### raw NMEA 확인

```bash
stty -F /dev/serial0 9600 && timeout 5 cat /dev/serial0
```

### gpsd 관련 (현재 미사용)

gpsd는 설치되어 있으나 `DEVICES=""` 상태로 미사용. 앱은 pyserial 직접 사용.

---

## camera.py 아키텍처

### 두 가지 모드

1. **picamera2 모드** (기본): libcamera 직접 제어, `Picamera2` 객체 사용
2. **subprocess 모드** (fallback): `rpicam-still` 명령 호출

### IMX296 해상도

- 프리뷰: 640×480
- 촬영: 1456×1088 (full resolution)

### 촬영 파라미터

| 파라미터 | 기본값 | 범위 | 설명 |
|----------|-------|------|------|
| shutter | 5000ms | 1~120000ms | 노출 시간 |
| gain | 8 | 1~16 | 아날로그 게인 (ISO = gain×100/50×50) |
| awb | auto | auto/incandescent/... | 화이트 밸런스 |
| count | 1 | 1~20 | 연속 촬영 장수 |
| interval | 0~60s | 0 가능 | 연속 촬영 간격 (0 = 딜레이 없음) |
| saturation | 1.0 | 0~2 | 채도 |
| sharpness | 1.5 | 0~4 | 선명도 |
| contrast | 1.0 | 0.5~2 | 대비 |
| quality | 95 | 75~99 | JPEG 품질 |

### RAM 버퍼 연속 촬영 (2-phase capture)

연속 촬영(count > 1) 시 SD카드 쓰기 지연을 피하기 위해 2단계로 동작:

1. **Phase 1** — 모든 프레임을 `io.BytesIO()`(RAM)에 캡처, `self._capturing = True` 유지
2. **Phase 2** — `self._capturing = False`로 프리뷰 즉시 재개 → 이후 RAM의 데이터를 디스크에 순서대로 기록

subprocess 모드는 `/tmp/`(tmpfs = RAM)에 임시 저장 후 `~/photos/`로 이동.

### 파일명 포맷

```
star_YYYYMMDD_HHMMSS_mmm_SEQ.jpg
예: star_20260517_105030_123_001.jpg
         날짜      시분초  ms  장번호
```

- `mmm` = 촬영 시각의 밀리초 (000~999)
- `SEQ` = 연속 촬영 순서 (001부터)
- 2026-05-17 이전에 찍은 사진은 ms 없는 구 포맷: `star_YYYYMMDD_HHMMSS_SEQ.jpg`

### 사이드카 JSON

촬영 시 `.jpg` 옆에 동일 기본명의 `.json` 저장:

```json
{
  "captured_at": "2026-05-17T10:05:30.123",
  "camera": {
    "shutter_ms": 5000, "shutter_us": 5000000,
    "gain": 8, "iso_equiv": 800,
    "awb": "auto", "saturation": 1.0,
    "sharpness": 1.5, "contrast": 1.0,
    "quality": 95, "sequence": 1, "total": 3, "interval_s": 0
  },
  "gps": {
    "fix": true, "latitude": 37.5587, "longitude": 127.1467,
    "altitude": 128.0, "satellites": 9, "hdop": 2.2, "timestamp": "09:56:31"
  }
}
```

### 카메라 toggle

`/api/camera/toggle` → `camera.set_enabled(False)` → `_run()` 루프에서 `_stop_all()` 호출 → libcamera 세션 실제 종료 (센서 전원 절감). UI 프리뷰 스트림도 멈춤.

---

## Web UI (index.html) 기능

### 탭 구성

1. **촬영** — 카메라 프리뷰 + 파라미터 컨트롤 + 촬영/보정 버튼
2. **갤러리** — 사진 목록, 메타데이터 모달, 다운로드/삭제
3. **GPS** — 상세 GPS 상태, baud rate 변경

### 촬영 탭 기능

- **반응형 레이아웃**: PC(≥900px) 좌측 프리뷰 / 우측 컨트롤, 모바일 상단 프리뷰 / 하단 컨트롤
- **드래그 스플리터**: 프리뷰 영역과 컨트롤 영역 경계를 드래그로 조절 (PC: 가로, 모바일: 세로)
- **비율 프리셋**: 프리뷰 우상단에 [4:3] [16:9] [꽉 찬] 버튼
- **노출 스텝 버튼**: 슬라이더 양옆에 ÷2 / ×2 버튼
- **게인 스텝 버튼**: 슬라이더 양옆에 − / + (±0.5) 버튼
- **카메라 꺼짐 오버레이**: 카메라 off 상태에서 프리뷰 중앙에 대형 "카메라 켜기" 버튼
- **GPS 예약 촬영**: `HH:MM:SS` 입력 → GPS UTC 해당 시각에 자동 촬영, 탭 위에 카운트다운 배너 표시
  - GPS UTC 오프셋을 status 폴링마다 갱신해 기기 클락으로 보간 (sub-second 정확도)
- **진행 표시**: 연속 촬영 중 프로그레스 바 + 프리뷰 오버레이에 "N/M (RAM 버퍼링)" 표시

### 갤러리 탭 기능

- **그리드 / 목록 전환**: ⊞ / ≡ 토글
- **멀티 선택**: 체크박스로 복수 선택 → 하단 선택바 표시
- **일괄 삭제**: 선택한 사진 + json 사이드카 동시 삭제
- **일괄 ZIP 다운로드**: "⬇ ZIP" 버튼 → 선택한 jpg + json을 서버에서 ZIP으로 묶어 전송

### 헤더 상태 칩

| 아이콘 | 내용 | 색상 기준 |
|--------|------|----------|
| 📷 | 카메라 상태 | ok/warn/err |
| ⊕ | GPS 상태 (클릭 시 GPS 탭 이동) | fix=ok, 탐색중=warn, 없음=dim |
| 🌡 | CPU 온도 | <40°C ok / 40~60°C 주황 / ≥60°C 빨강 |
| 💾 | 디스크 여유 | >75% 사용 warn, >90% err |

---

## 부팅 시 자동 시작 설정

```bash
# crontab 확인
ssh pi 'crontab -l'

# 자동 시작 등록 (없으면)
# @reboot sleep 15 && cd /home/insoo/starcam && python3 app.py >> /tmp/starcam.log 2>&1 &
```

---

## 자주 쓰는 디버그 명령

```bash
# GPS 실시간 상태 (5초마다 갱신)
ssh pi "watch -n5 'curl -s http://localhost:5000/api/gps | python3 -m json.tool'"

# GPS raw 시리얼 수신 확인
ssh pi 'timeout 5 cat /dev/serial0'

# 카메라 연결 확인
ssh pi 'libcamera-hello --list-cameras'

# 앱 로그 실시간
ssh pi 'tail -f /tmp/starcam.log'

# 앱 재시작
ssh pi 'pkill -f app.py'; ssh pi 'cd /home/insoo/starcam && nohup python3 app.py >> /tmp/starcam.log 2>&1 &'

# import 오류 확인
ssh pi 'cd /home/insoo/starcam && python3 -c "import app; print(\"ok\")"'

# 의존성 설치
ssh pi 'pip3 install flask pyserial'

# 사진 목록 확인
ssh pi 'ls -lh ~/photos/ | tail -10'

# 특정 사진 메타데이터 확인
ssh pi 'cat ~/photos/star_20260517_105030_123_001.json | python3 -m json.tool'
```

---

## 알려진 이슈 및 해결

| 이슈 | 원인 | 해결 |
|------|------|------|
| 재부팅 후 GPS 연결 안됨 | gpsd `DEVICES=""` | pyserial 직접 사용으로 전환 완료 |
| GPS baud rate 변경 | 앱에서 실시간 변경 불가 | `/api/gps/baud` POST로 변경 가능 |
| sudo 필요한 작업 | `/etc/default/gpsd` 수정 등 | 앱 레벨에서 우회 (pyserial) |
| 연속 촬영 간 SD카드 지연 | SD 쓰기 속도 느림 | RAM 버퍼 2-phase capture로 해결 |
| `pkill` + 재시작 스크립트 exit 255 | pkill이 프로세스 없으면 exit 1 반환 | 두 명령을 별도 ssh 호출로 분리 |
