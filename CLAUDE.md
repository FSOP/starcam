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
├── gps_reader.py    # gpsd 소켓 클라이언트 (localhost:2947)
├── static/
│   └── index.html   # 단일 페이지 Web UI (촬영 / 갤러리 / GPS 탭)
├── CLAUDE.md        # 이 파일 (세션 간 레퍼런스)
├── requirements.txt # flask, pyserial (gpsd는 시스템 패키지)
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
| POST | `/api/mount/auto_calibrate` | plate solve 자동 보정. 새 촬영 또는 기존 사진 `{"filename": "star_...jpg"}` 재사용 |

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

**gpsd 소켓 클라이언트 사용** — `localhost:2947` JSON 프로토콜로 gpsd에 연결.  
(과거에는 pyserial 직접 사용했으나, 현재는 gpsd 방식으로 전환됨)

### 동작 방식

1. `start()` → 백그라운드 스레드 시작
2. `localhost:2947`에 소켓 연결 → `?WATCH={"enable":true,"json":true}` 전송
3. JSON 응답 파싱 (`TPV`: 위치/속도, `SKY`: 위성 정보)
4. 연결 끊기면 재연결 시도

### 파싱하는 gpsd JSON 클래스

| 클래스 | 파싱 데이터 |
|--------|------------|
| `TPV` | 위도, 경도, 고도, 속도, 방위각, UTC 시각, fix mode |
| `SKY` | 위성 수, HDOP |

---

## GPS 시리얼 포트 구성

### 라즈베리파이 3B UART 매핑

- `/dev/serial0` → `ttyS0` (mini UART, GPIO 14/15) — **GPS 연결 (gpsd가 관리)**
- `ttyAMA1` (PL011 full UART) → Bluetooth 내부 사용
- `enable_uart=1` in `/boot/firmware/config.txt`

### gpsd 상태 확인

```bash
# gpsd 동작 확인
ssh pi 'gpspipe -w -n 5'

# raw NMEA 직접 확인 (gpsd 중지 후)
stty -F /dev/serial0 9600 && timeout 5 cat /dev/serial0
```

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

`mount.az` / `mount.el`은 원시 인코더값으로 유지한다. `_get_mount_snap()`은 설정된
`az_offset` / `el_offset`을 함께 넣고 `az_corrected = (az + az_offset) % 360`,
`el_corrected = el + el_offset`도 저장한다.

### 카메라 toggle

`/api/camera/toggle` → `camera.set_enabled(False)` → `_run()` 루프에서 `_stop_all()` 호출 → libcamera 세션 실제 종료 (센서 전원 절감). UI 프리뷰 스트림도 멈춤.

### Scout / faint 탐지

- 실시간 scout worker는 먼저 밝은 궤적용 `detect_trail()`을 실행한다.
- 밝은 궤적이 없으면 `faint-diff`를 실행한다. 현재 프레임에서 과거 4프레임 median 배경을 빼고, compact linear cluster를 찾는다.
- 빠른 위성을 놓치지 않기 위해 실시간 모드는 2~3프레임 confirmation을 기다리지 않는다. 1프레임 고신뢰 후보로 즉시 ring trigger를 걸고, 기존 pre/hit/post 저장 구조가 앞뒤 프레임을 보존한다. 대각선 1픽셀 streak는 8방향 연결로 묶고, 실시간 후보 크기 상한은 `faint_max_n=180`으로 둔다.
- `scipy`가 없으면 `has_faint=false`로 표시되고 기존 밝은 궤적 탐지만 동작한다.

---

## Web UI (index.html) 기능

### 탭 구성

1. **촬영** — 카메라 프리뷰 + 파라미터 컨트롤 + 촬영/보정 버튼
2. **갤러리** — 사진 목록, 메타데이터 모달, 다운로드/삭제
3. **GPS** — 상세 GPS 상태, baud rate 변경
4. **마운트 제어** — AZ/EL 이동, plate solve 자동 보정, 마운트 로그

### 촬영 탭 기능

- **반응형 레이아웃**: PC(≥900px) 좌측 프리뷰 / 우측 컨트롤, 모바일 상단 프리뷰 / 하단 컨트롤
- **드래그 스플리터**: 프리뷰 영역과 컨트롤 영역 경계를 드래그로 조절 (PC: 가로, 모바일: 세로)
- **비율 프리셋**: 프리뷰 우상단에 [4:3] [16:9] [꽉 찬] 버튼
- **노출 스텝 버튼**: 슬라이더 양옆에 ÷2 / ×2 버튼
- **게인 스텝 버튼**: 슬라이더 양옆에 − / + (±0.5) 버튼
- **카메라 꺼짐 오버레이**: 카메라 off 상태에서 프리뷰 중앙에 대형 "카메라 켜기" 버튼
- **GPS 예약 촬영**: `HH:MM:SS` 입력 → GPS UTC 해당 시각에 자동 촬영, 탭 위에 카운트다운 배너 표시
  - GPS UTC 오프셋을 status 폴링마다 갱신해 기기 클락으로 보간 (sub-second 정확도)
- **위성 촬영 예약**: `/api/schedule` 예약 큐는 메모리 기반이라 재시작 시 비워진다. 실행 이력은 `schedule_history.jsonl` append 로그와 예약 사진 사이드카의 `schedule` 블록에 남긴다. `/api/schedule/history?limit=100`으로 조회 가능.
- **진행 표시**: 연속 촬영 중 프로그레스 바 + 프리뷰 오버레이에 "N/M (RAM 버퍼링)" 표시

### 갤러리 탭 기능

- **그리드 / 목록 전환**: ⊞ / ≡ 토글
- **멀티 선택**: 체크박스로 복수 선택 → 하단 선택바 표시
- **일괄 삭제**: 선택한 사진 + json 사이드카 동시 삭제
- **일괄 ZIP 다운로드**: "⬇ ZIP" 버튼 → 선택한 jpg + json을 서버에서 ZIP으로 묶어 전송
- **기존 사진 보정**: GPS fix가 저장된 사진 모달에서 `이 사진으로 보정` 버튼으로 같은 사진을 보정 서버에 재전송

### 마운트 제어 탭

- **새 사진으로 캘리브레이션**: `새 사진 촬영 후 보정` 버튼은 새 `_astrocal` 사진을 촬영한 뒤 plate-solve 서버에 전송
- **서버 전송 사진 표시**: 보정 완료 결과에 실제 전송한 사진 썸네일과 파일명을 표시
- **과거 사진 재사용**: `/api/mount/auto_calibrate`에 `filename`을 보내면 새 촬영 없이 `~/photos/<filename>`과 사이드카 JSON을 사용
- 기존 사진 보정은 사이드카의 `gps`, `captured_at_utc`, `mount` 값을 사용한다. timestamp는 `captured_at_utc` → `gps.timestamp` → `captured_at - 9h` 순서로 복원한다. `mount.az/el`이 없으면 solve 결과만 보여주고 오프셋은 업데이트하지 않는다.
- plate-solve 서버 v2는 업로드 후 `job_id`를 반환하므로 Pi는 `/api/calibrate/status/{job_id}`를 3초 간격으로 최대 5분 폴링한다.
- 이동식 관측소 운용이므로 모든 plate solve 요청에서 RA/Dec 힌트는 보내지 않고, 카메라/렌즈가 유지된다는 가정하에 `scale_low/high`와 `scale_units=arcsecperpix`만 보낸다. 서버 오류/타임아웃 시에도 UI에 전송한 사진을 표시한다.

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
| 재부팅 후 GPS 연결 안됨 | gpsd `DEVICES=""` | gpsd 소켓 방식으로 전환 완료 |
| GPS baud rate 변경 | 앱에서 실시간 변경 불가 | `/api/gps/baud` POST로 변경 가능 |
| sudo 필요한 작업 | `/etc/default/gpsd` 수정 등 | gpsd 소켓으로 우회 |
| 연속 촬영 간 SD카드 지연 | SD 쓰기 속도 느림 | RAM 버퍼 2-phase capture로 해결 |
| `pkill` + 재시작 스크립트 exit 255 | pkill이 프로세스 없으면 exit 1 반환 | 두 명령을 별도 ssh 호출로 분리 |
