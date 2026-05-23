# StarCam

라즈베리파이 3B + IMX296 Global Shutter Camera + GNSS 모듈로 구성한 **천체 / 인공위성 촬영용 웹 앱**입니다.  
Flask 서버를 Pi에서 실행하고, 같은 네트워크(또는 핫스팟)로 연결된 스마트폰·PC 브라우저에서 원격으로 제어합니다.

---

## 하드웨어 구성

| 부품 | 상세 |
|------|------|
| 컴퓨터 | Raspberry Pi 3B |
| 카메라 | IMX296 Global Shutter — 1456 × 1088 px |
| GPS | GNSS 모듈 `/dev/serial0` (GPIO UART, ttyS0), 9600 baud |
| OS | Raspberry Pi OS (Debian Bookworm, 64-bit) |
| Python | 3.11+ |

> **Global Shutter 선택 이유**: 고속 이동 물체(인공위성, 유성)를 찍을 때 Rolling Shutter 왜곡(빗 모양 잔상)이 없음.

---

## 프로젝트 구조

```
starcam/
├── app.py           # Flask 라우트 / API 엔드포인트
├── camera.py        # 카메라 드라이버 (picamera2 + subprocess 폴백)
├── gps_reader.py    # GNSS NMEA 파서 (pyserial 직접 사용)
├── sky_calc.py      # 박명/일출 시각 계산 (외부 의존 없음)
├── static/
│   └── index.html   # 단일 페이지 Web UI (순수 HTML/CSS/JS)
├── requirements.txt
└── .gitignore
```

촬영된 사진은 Pi의 `~/photos/` 에 저장되며, 이 저장소에는 포함되지 않습니다.

---

## 실행 방법

```bash
# 의존성 설치
pip3 install flask pyserial

# 서버 시작
cd /home/insoo/starcam
python3 app.py
```

브라우저에서 `http://<Pi의 IP>:5000` 으로 접속합니다.

### 백그라운드 자동 실행 (재부팅 후에도 유지)

```bash
crontab -e
# 아래 줄 추가:
@reboot sleep 15 && cd /home/insoo/starcam && python3 app.py >> /tmp/starcam.log 2>&1 &
```

### 앱 재시작

```bash
pkill -f app.py; sleep 1
nohup python3 /home/insoo/starcam/app.py >> /tmp/starcam.log 2>&1 &
```

---

## 촬영 모드

StarCam은 다섯 가지 촬영 모드를 지원합니다.

### 1. 수동 촬영

UI의 노출·게인·AWB 등을 설정하고 셔터 버튼을 누르는 기본 모드.  
연속 촬영(`count` 최대 20장)과 촬영 간격(`interval`) 설정 가능.

### 2. 주기 촬영 (Periodic)

설정한 분 간격으로 자동 촬영을 반복합니다.

- 간격: 10초 ~ 수 시간 자유 설정
- 촬영 파라미터(노출·게인·장수)는 ON 시점 기준으로 저장되며, 페이지 새로고침 후에도 복원됩니다.
- `suffix = "_periodic"` 으로 파일명 구분

### 3. 연속 촬영 — Burst Mode

한 번 모드를 시작하면 **최대 설정 시간** 동안 프레임 간 간격 없이 연속 촬영합니다.

- Still 모드로 전환 후 `cam.capture_file()` 루프 반복
- 파일명에 `_burst` 접미사 + 5자리 시퀀스 번호 (`star_..._00001_burst.jpg`)
- 운용 예: 1시간 동안 450ms 노출로 인공위성 통과 자동 포착

### 4. 자동 감지 촬영 (Auto-Trigger)

프리뷰 스트림을 실시간 분석하여 **밝기 변화**가 임계값을 초과하면 자동 촬영합니다.

- 임계값(`threshold`): 0~100, 기본 30
- 감지 시 설정된 파라미터로 즉시 촬영
- `suffix = "_autodetect"` 로 파일명 구분

### 5. 동영상 녹화 (Video)

`rpicam-vid` 를 이용해 H.264 MP4 파일을 저장합니다.

- 해상도: 1920×1080 (기본), 설정 변경 가능
- 저장 위치: `~/photos/video_YYYYMMDD_HHMMSS.mp4`
- UI에서 STOP 누를 때까지 녹화 지속

---

## 촬영 파라미터

| 파라미터 | 키 이름 | 기본값 | 범위 | 설명 |
|----------|---------|-------|------|------|
| 노출 | `shutter_ms` | 5000 | 1 ~ 120000 ms | 셔터 시간. us 단위로 변환 후 picamera2에 적용 |
| 게인 | `gain` | 8 | 1 ~ 16 | 아날로그 게인 (ISO ≈ gain × 100) |
| AWB | `awb` | `auto` | auto / none / incandescent 등 | 화이트 밸런스 프리셋 |
| 장수 | `count` | 1 | 1 ~ 20 | 연속 촬영 매수 |
| 간격 | `interval` | 0 | 0 ~ 60 s | 연속 촬영 간 대기 시간 |
| 채도 | `saturation` | 1.0 | 0 ~ 2 | 0 = 흑백 |
| 선명도 | `sharpness` | 1.5 | 0 ~ 4 | — |
| 대비 | `contrast` | 1.0 | 0.5 ~ 2 | — |
| JPEG 품질 | `quality` | 95 | 75 ~ 99 | — |

---

## 파일 이름 형식

```
star_YYYYMMDD_HHMMSS_mmm_SEQ[_suffix].jpg
예) star_20260517_105030_123_001_periodic.jpg
         날짜      시분초  ms  장번호  촬영모드
```

- `mmm` : 촬영 시각의 밀리초 (000 ~ 999)
- `SEQ` : 연속 촬영 순서, 001 부터
- `suffix` : `_periodic` / `_autodetect` / `_burst` — 없으면 수동 촬영

### 사이드카 JSON

모든 촬영에 대해 `.jpg` 옆에 동일 이름의 `.json` 파일이 자동 생성됩니다.

```json
{
  "captured_at": "2026-05-17T10:50:30.123",
  "camera": {
    "shutter_ms": 5000,
    "shutter_us": 5000000,
    "gain": 8,
    "iso_equiv": 800,
    "awb": "none",
    "saturation": 0.0,
    "sharpness": 1.5,
    "contrast": 1.0,
    "quality": 95,
    "sequence": 1,
    "total": 1,
    "interval_s": 0
  },
  "gps": {
    "fix": true,
    "latitude": 37.5587,
    "longitude": 127.1467,
    "altitude": 128.0,
    "satellites": 9,
    "hdop": 2.2,
    "timestamp": "01:50:30.123Z"
  }
}
```

GPS fix가 없으면 `"gps"` 필드는 생략됩니다.

---

## API 엔드포인트

### 상태 / 시스템

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | Web UI (index.html) |
| GET | `/stream` | MJPEG 실시간 프리뷰 스트림 |
| GET | `/api/status` | 카메라·GPS·시스템·촬영 모드 통합 상태 |
| GET | `/api/gps` | GPS 상세 정보 (포트, HDOP, 속도, 마지막 NMEA 문장 등) |
| GET | `/api/gps/baud` | 현재 baud rate + 허용 목록 |
| POST | `/api/gps/baud` | baud rate 변경 `{"baud": 9600}` |
| GET | `/api/sky` | 오늘/내일 박명·일출 시각 및 LEO 위성 가시 시간대 |

### 촬영

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/capture` | 수동 촬영 |
| POST | `/api/calibrate` | 자동 게인 보정 |
| POST | `/api/camera/toggle` | 카메라 센서 ON/OFF 토글 |
| POST | `/api/camera/preview` | 프리뷰 최대 노출 설정 `{"max_ms": 1000}` |
| GET/POST | `/api/camera/periodic` | 주기 촬영 상태 조회 / 시작·중지 |
| GET/POST | `/api/camera/burst` | 연속 촬영 상태 조회 / 시작·중지 |
| GET/POST | `/api/camera/autotrigger` | 자동 감지 촬영 상태 조회 / 시작·중지 |
| GET/POST | `/api/camera/video` | 동영상 녹화 상태 조회 / 시작·중지 |

### 갤러리

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/photos` | 최신 50장 목록 (GPS 메타 포함) |
| GET | `/api/photos/<filename>` | 원본 이미지 |
| GET | `/api/photos/<filename>/thumb` | 썸네일 (현재 원본과 동일) |
| POST | `/api/photos/<filename>/delete` | 사진 + JSON 사이드카 삭제 |
| POST | `/api/photos/download` | 선택 사진 ZIP 다운로드 |
| GET | `/api/videos/<filename>` | 동영상 파일 다운로드 |

#### `/api/status` 응답 예시

```json
{
  "camera_connected": true,
  "camera_enabled": true,
  "cpu_temp": 44.0,
  "disk_free": "8.0 GB",
  "disk_percent": 36,
  "photo_count": 142,
  "gps_fix": true,
  "latitude": 37.5587,
  "longitude": 127.1467,
  "altitude": 128.0,
  "satellites": 9,
  "utc_time": "01:50:30",
  "periodic": {
    "enabled": false,
    "interval_s": 600,
    "shot_count": 0,
    "next_in_s": 0
  },
  "burst": {
    "enabled": false,
    "count": 0,
    "elapsed_s": 0
  },
  "auto_trigger": {
    "enabled": false,
    "threshold": 30,
    "hit_count": 0
  }
}
```

---

## 모듈 설명

### `app.py` — Flask 서버

- `camera`, `gps` 객체를 모듈 수준에서 생성, 앱 시작 시 `camera.start()` / `gps.start()` 호출
- 모든 API 응답은 JSON
- 사진 파일명 유효성 검사: `star_`로 시작, `.jpg`로 끝, 경로 이동 문자(`/`, `..`) 차단
- `_do_auto_capture()` 와 `_do_periodic()` 은 별도 스레드에서 실행되는 콜백 함수로, 촬영 시 매번 `gps.get_fix()` 를 호출해 최신 위치를 가져옴

### `camera.py` — 카메라 드라이버

#### 두 가지 동작 모드

| 모드 | 조건 | 설명 |
|------|------|------|
| **picamera2** | `picamera2` 라이브러리 설치됨 (기본) | libcamera 직접 제어, 고성능 |
| **subprocess** | picamera2 없음 | `rpicam-still` / `rpicam-vid` 셸 명령 호출 |

#### 내부 스레드 구조

```
_run() 스레드  —  항상 실행, picamera2 세션 유지 + 프리뷰 프레임 생성
_per_loop() 스레드  —  주기 촬영 타이머
_burst_loop() 스레드  —  연속 촬영 루프
```

#### 프리뷰 최적화

- 프리뷰 프레임 캡처 시 JPEG 품질 40으로 압축해 대역폭 절약
- 기본 최대 노출 1000ms (더 길면 프리뷰가 굼뜨므로)
- 촬영 후 `stop() → configure(prev_cfg) → start()` 로 프리뷰 모드 명시 복귀 (`switch_mode` 불안정 이슈 우회)

#### 연속 촬영 (burst) 동작 원리

picamera2 모드:
1. `_capture_mutex` 획득
2. 카메라를 still 모드로 한 번만 전환
3. `cam.capture_file()` 루프 — duration_s 초과 또는 stop 신호까지 반복
4. 종료 후 프리뷰 모드 복귀

subprocess 모드:
- `rpicam-still` 을 루프에서 반복 호출

#### 자동 게인 보정 (`calibrate`)

`rpicam-still` 로 짧은 테스트 촬영 → 중앙 16×16 픽셀 평균 밝기 → 목표 밝기(128)에 맞는 게인 값을 이진 탐색으로 계산해 반환.

### `gps_reader.py` — GNSS 파서

#### 설계 결정: gpsd 대신 pyserial 직접 사용

gpsd는 재부팅 후 `DEVICES=""` 상태로 시작해 GPS 장치를 자동으로 인식하지 못하는 문제가 반복됨.  
대신 `serial.Serial('/dev/serial0', 9600)` 을 직접 열어 NMEA 문장을 파싱합니다.

#### 파싱 대상 NMEA 문장

| 문장 | 획득 데이터 |
|------|------------|
| `$GNGGA` / `$GPGGA` | 위도·경도·고도·위성 수·HDOP·UTC 시각·Fix quality |
| `$GNRMC` / `$GPRMC` | 위도·경도·속도(knot→km/h)·진방위각·UTC 날짜 |
| `$GNZDA` / `$GPZDA` | UTC 날짜 및 시각 |

#### 자동 포트 탐색

시작 시 후보 포트(`/dev/serial0`, `/dev/ttyAMA0`, `/dev/ttyUSB0`, ...)를 순서대로 열어 NMEA 데이터가 오는 첫 번째 포트를 사용합니다. 연결이 끊기면 3초 후 재탐색합니다.

#### `get_fix()` vs `status()`

| 메서드 | 반환 | 용도 |
|--------|------|------|
| `get_fix()` | GPS fix 있을 때만 `{fix, latitude, longitude, ...}` dict, 없으면 `None` | **사진 사이드카 저장용** |
| `status()` | 연결 상태 포함 전체 딕셔너리 (항상 반환) | **UI 상태 표시용** |

### `sky_calc.py` — 박명 시각 계산기

외부 라이브러리 없이 순수 Python으로 태양 고도각을 계산합니다.

- Julian Date 기반 태양 적위·시각 계산
- 대기 굴절 보정 포함
- 이진 탐색(`_bisect`)으로 각 박명 고도각 통과 시각을 분 단위로 정밀 계산

| 이벤트 | 태양 고도각 |
|--------|------------|
| 일출/일몰 | −0.833° (대기 굴절 포함) |
| 시민박명 | −6° |
| 항해박명 | −12° |
| 천문박명 | −18° |

LEO(고도 약 500km) 위성 가시 시간대는 **시민박명 ~ 천문박명** 구간으로 계산됩니다 (지상은 어두워 위성에 햇빛이 반사되는 시간대).

---

## Web UI

### 화면 구성

단일 HTML 파일(`static/index.html`)로 구성되며, 다음 세 탭으로 나뉩니다.

| 탭 | 내용 |
|----|------|
| **촬영** | 실시간 프리뷰 + 파라미터 컨트롤 + 모든 촬영 모드 |
| **갤러리** | 사진 목록·다운로드·삭제 |
| **GPS** | 상세 GPS 상태, baud 변경 |

### 촬영 탭 주요 기능

- **MJPEG 실시간 프리뷰** (`<img src="/stream">`)
- **반응형 레이아웃**: PC(≥900px) 좌우 분할, 모바일 상하 분할 + 드래그 스플리터
- **비율 프리셋**: 프리뷰 우상단 [4:3] [16:9] [꽉 찬] 버튼
- **파라미터 슬라이더**: 노출·게인·채도·선명도·대비·품질 — ÷2/×2·−/+ 스텝 버튼 포함
- **프리셋 버튼 1~5**: localStorage에 설정 저장/불러오기
  - 데이터 있는 슬롯: 파란색 강조, 클릭 → 설정 불러오기
  - 빈 슬롯: 클릭 → 현재 설정 저장
  - 우클릭: 슬롯 선택 → 💾 덮어쓰기 / ✕ 삭제 버튼 활성화
  - 페이지 로드 시 프리셋 '1'이 있으면 자동 적용
- **GPS 예약 촬영**: `HH:MM:SS` UTC 입력 → 정확한 시각에 자동 촬영
  - GPS UTC와 기기 클락을 주기적으로 동기화해 서브세컨드 정확도 유지
  - 탭 상단에 카운트다운 배너 표시
- **자동 감지 촬영 섹션**: 임계값·파라미터 설정 후 ON/OFF
- **주기 촬영 섹션**: 간격·파라미터 설정 후 ON/OFF, 다음 촬영까지 카운트다운
- **연속 촬영 섹션**: 노출·게인·촬영 시간 설정 후 시작/중지
- **동영상 녹화 섹션**: 해상도·노출·게인 설정, 녹화 중 경과 시간 표시

### 헤더 상태 칩

| 칩 | 의미 | 색상 기준 |
|----|------|----------|
| 📷 | 카메라 상태 | 초록(정상) / 주황(경고) / 빨강(오류) |
| ⊕ | GPS 상태 | 초록(Fix) / 주황(탐색 중) / 회색(미연결) |
| 🌡 | CPU 온도 | < 40°C 초록 / 40~60°C 주황 / ≥ 60°C 빨강 |
| 💾 | 디스크 여유 | > 75% 사용 시 주황 / > 90% 빨강 |

4초마다 `/api/status` 폴링으로 자동 갱신됩니다.

### 갤러리 탭

- 그리드 ⊞ / 목록 ≡ 전환
- 멀티 선택 + 일괄 삭제 / ZIP 다운로드
- 사진 클릭 → 메타데이터 모달 (노출·게인·GPS 위치 표시)
- GPS 태그된 사진은 배지 표시

---

## GPS 시리얼 포트 설정 (Raspberry Pi 3B)

```bash
# /boot/firmware/config.txt 에 추가
enable_uart=1

# /boot/firmware/cmdline.txt 에서 제거
# console=serial0,115200
```

UART 확인:
```bash
stty -F /dev/serial0 9600 && timeout 5 cat /dev/serial0
# $GNGGA, $GNRMC 등 NMEA 문장이 출력되면 정상
```

---

## 주요 디버그 명령

```bash
# 앱 로그 실시간
ssh pi 'tail -f /tmp/starcam.log'

# 상태 확인
ssh pi 'curl -s http://localhost:5000/api/status | python3 -m json.tool'

# 카메라 연결 확인
ssh pi 'libcamera-hello --list-cameras'

# GPS raw 수신 확인
ssh pi 'timeout 5 cat /dev/serial0'

# 사진 파일 목록 (최근 10장)
ssh pi 'ls -lh ~/photos/star_*.jpg | tail -10'

# 특정 사진 메타데이터
ssh pi 'cat ~/photos/star_20260517_105030_123_001.json | python3 -m json.tool'
```

---

## 알려진 이슈 및 해결책

| 증상 | 원인 | 해결 |
|------|------|------|
| 재부팅 후 GPS 연결 안 됨 | gpsd `DEVICES=""` 초기화 | pyserial 직접 사용으로 전환 (현재 적용) |
| 주기 촬영 노출이 항상 5초 | `_do_capture` 가 `shutter_ms` 키 대신 `shutter` 키를 읽음 | `shutter_ms` 우선 읽기로 수정 (현재 적용) |
| 주기·자동 촬영 GPS 미저장 | `gps.status()` 전달 (UI용 키 형식) → `_save_sidecar` 키 불일치 | `gps.get_fix()` 로 변경 (현재 적용) |
| 프리뷰 굼뜸 / 롤링 아티팩트 | 프리뷰 노출 2000ms + `switch_mode` 불안정 | 최대 노출 1000ms, JPEG 품질 40, stop/configure/start 방식 변경 |
| 촬영 후 카메라 죽음 | `switch_mode()` 실패 | 명시적 `stop → configure → start` 시퀀스로 교체 |
| 연속 촬영 간 SD 지연 | SD 쓰기 속도 한계 | picamera2 burst: still 모드 유지 + 직접 파일 쓰기로 딜레이 최소화 |

---

## 백업 / 동기화

이 저장소(`~/raspberryObsCode`)는 Pi의 `/home/insoo/starcam`을 rsync로 가져온 백업입니다.

```bash
# Pi → 로컬 동기화
rsync -av --exclude='__pycache__' --exclude='*.pyc' \
  pi:/home/insoo/starcam/ ~/raspberryObsCode/

# 사진만 별도 백업
rsync -av pi:/home/insoo/photos/ ~/raspberryObsCode_photos/
```
