# StarCam — PLAN.md

## 프로젝트 개요

라즈베리파이 3B + Global Shutter Camera (IMX296) + GPS 모듈을 이용한  
별 사진 촬영용 웹 UI.  
핫스팟으로 스마트폰과 직접 연결, 브라우저에서 실시간 미리보기·촬영·갤러리·GPS 조회.

---

## 기술 스택

| 레이어 | 선택 | 이유 |
|--------|------|------|
| 서버 | Python 3 + Flask | RPi에 기본 탑재, 가볍고 설치 쉬움 |
| 카메라 | rpicam-apps | RPi OS 최신 표준 (libcamera 대체) |
| 실시간 스트림 | MJPEG over HTTP | 브라우저 네이티브, 추가 JS 불필요 |
| GPS | pyserial + NMEA 파싱 | gpsd 불필요, 직접 시리얼 수신 |
| 프론트엔드 | 순수 HTML/CSS/JS | 의존성 없음, 모바일 최적화 |

---

## 디렉토리 구조

```
starcam/
├── app.py           # Flask 라우트
├── camera.py        # rpicam 래퍼 (스트림·촬영)
├── gps_reader.py    # GPS 시리얼 읽기 + NMEA 파싱
├── static/
│   └── index.html   # 단일 페이지 Web UI
├── photos/          # 촬영 사진 저장 (gitignore)
├── requirements.txt
├── .gitignore
└── PLAN.md
```

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET  | `/` | Web UI |
| GET  | `/stream` | MJPEG 실시간 스트림 |
| GET  | `/api/status` | 카메라·GPS·시스템 상태 |
| POST | `/api/capture` | 사진 촬영 |
| GET  | `/api/photos` | 사진 목록 (GPS 정보 포함) |
| GET  | `/api/photos/<f>` | 원본 이미지 |
| GET  | `/api/photos/<f>/thumb` | 썸네일 (현재 원본 제공) |
| POST | `/api/photos/<f>/delete` | 사진 삭제 |

---

## GPS 지원

- 자동 포트 탐색: `/dev/ttyAMA0`, `/dev/serial0`, `/dev/ttyUSB0`, `/dev/ttyACM0`
- NMEA 문장 파싱: `$GPRMC` / `$GPGGA` (위도·경도·고도·위성 수)
- 연결 상태 표시: GPS 없음 / 연결됨(신호 탐색중) / Fix 획득
- 사진 촬영 시 GPS fix 데이터를 JSON 사이드카 파일로 저장  
  예: `star_20241201_213045_001.jpg` → `star_20241201_213045_001.json`
- 갤러리에서 GPS 배지 표시, 사진 모달에서 위·경도·고도·위성 수 표시

---

## 실행 방법

```bash
pip3 install flask pyserial
python3 app.py
```

### 부팅 시 자동 실행 (crontab)
```bash
crontab -e
# 아래 줄 추가:
@reboot sleep 15 && cd /home/pi/starcam && python3 app.py >> /tmp/starcam.log 2>&1 &
```

### 핫스팟
```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid "StarCam" password "starnight"
```

접속: `http://192.168.4.1:5000`

---

## GitHub 설정

```bash
git remote add origin https://github.com/<USERNAME>/starcam.git
git push -u origin main
```
