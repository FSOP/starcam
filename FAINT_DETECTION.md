# 희미한 위성 탐지 — 알고리즘 문서

> `detect_faint.py` 설계 근거, 검증 결과, Pi 통합 가이드.

---

## 왜 기존 알고리즘이 희미한 위성을 놓치는가

기존 `detect_trail.py`는 **단일 프레임 전체 이미지에 Hough + PCA**를 적용한다.  
IMX296 센서의 **FPN(Fixed Pattern Noise, 고정 패턴 노이즈)** 이 이 방식을 망친다.

### FPN이란?

센서 하드웨어 특성상 **매 프레임 같은 위치에 같은 패턴**으로 나타나는 노이즈.  
IMX296에서 관찰되는 종류:

| 종류 | 특성 | 영향 |
|------|------|------|
| 수평 밴딩 | 특정 행(row) 전체가 밝거나 어두움 | row median 제거로 해결 |
| 대각선 패턴 | ~35°, ~42°, ~19° 방향 반복 줄무늬 | **위성 궤적처럼 보여서 위험** |
| 수직 FPN | 특정 열(col) 밝기 편차 | col median 제거로 해결 |

```
FPN 대각선이 단일 프레임에서:
  n = 100~300 픽셀 (위성 streak의 10~25배)
  linearity = 0.57~0.74
  angle ≈ 35°, 42° 등 고정

희미한 위성 streak:
  n = 10~12 픽셀
  linearity ≥ 0.95
  → FPN에 완전히 묻혀서 PCA가 FPN을 주성분으로 인식
```

---

## 해결책: 시간 차분 (Temporal Differencing)

FPN은 **정적**이다. 매 프레임 같은 위치에 있다.  
위성은 **이동**한다. 매 프레임 20px 이상 움직인다.

```
background = mean(앞뒤 ±2 프레임)

프레임 N의 FPN 위치 = 프레임 N±1의 FPN 위치  →  diff에서 0
프레임 N의 위성 위치 ≠ 프레임 N±1의 위성 위치  →  diff에서 살아남음
```

LEO 위성은 프레임당 20~55px 이동하므로, 단 2개의 배경 프레임만으로도  
위성 신호가 배경에 오염되지 않는다.

---

## 알고리즘 단계별 설명

```
┌─────────────────────────────────────────────────────────┐
│ 1. 배경 계산                                             │
│    bg = mean(frame[N-2], frame[N-1], frame[N+1], frame[N+2])
│    (배치용 — 실시간은 과거 4프레임만 사용)               │
├─────────────────────────────────────────────────────────┤
│ 2. 차분                                                  │
│    diff = frame[N] - bg                                 │
│    FPN(정적) → 0,  위성(이동) → 양수 신호               │
├─────────────────────────────────────────────────────────┤
│ 3. 잔여 FPN 제거                                         │
│    diff -= median(diff, axis=row)   # 수평 밴딩 제거     │
│    diff -= median(diff, axis=col)   # 수직/대각 제거     │
├─────────────────────────────────────────────────────────┤
│ 4. 임계값 마스크                                         │
│    pos = clip(diff, 0, ∞)                               │
│    mask = pos > μ + 3.5σ                                │
├─────────────────────────────────────────────────────────┤
│ 5. 연결 성분 추출 (scipy.ndimage.label)                  │
│    크기 필터: 5 ≤ n ≤ 60 픽셀만 PCA 진행                │
│    (나머지 ~3000개 1~4px 노이즈 클러스터는 스킵)         │
├─────────────────────────────────────────────────────────┤
│ 6. PCA 검증 (클러스터당)                                 │
│    linearity ≥ 0.88  (선형성)                           │
│    trail_len ≥ 6 px  (최소 길이)                        │
│    density   ≥ 0.40  (픽셀 밀도)                        │
│    conc      ≥ 0.55  (집중도)                           │
│    8° < angle < 82°  (수평/수직 FPN 제외)               │
├─────────────────────────────────────────────────────────┤
│ 7. 궤적 클러스터링 (배치 전용)                           │
│    3+ 프레임 연속, 같은 방향 이동 → 위성 확정            │
│    max_px_per_frame = 55px (LEO 최대 이동 속도 기준)     │
└─────────────────────────────────────────────────────────┘
```

---

## 성능

### 처리 속도 (600px 기준, col median 포함)

| 환경 | 시간 | 프레임 주기(500ms) 대비 |
|------|------|----------------------|
| 로컬 PC (SSD 포함) | 143ms | 29% |
| 로컬 PC (RAM 입력) | 41ms | 8% |
| **Pi 3B (RAM 입력 추정, ×3.5)** | **~142ms** | **28%** ✅ |

### 해상도별 비교 (RAM 입력)

| 해상도 | PC | Pi 3B 추정 |
|--------|----|-----------|
| 800px | 87ms | ~305ms (61%) |
| **600px** | **41ms** | **~142ms (28%)** ← 기본값 |
| 500px | 26ms | ~91ms (18%) |

600px에서 위성 streak = 5~9px (탐지 기준 min_len=6px 충족).

### 단계별 병목 (PC 기준)

| 단계 | 시간 |
|------|------|
| JPEG 로드 + 디코딩 | ~61ms (배치만 해당, 실시간 0ms) |
| 배경 mean (4프레임) | ~5ms |
| row/col median 제거 | ~18ms |
| ndimage.label + 크기 필터 | ~21ms |
| PCA (5~10개 후보) | ~3ms |

---

## 2026-05-25 새벽 검증 결과

**세션**: 03:26~04:45 KST, 4504 프레임, 600px 스케일, σ=3.5

### 탐지된 위성 10건

| 궤적 | 시간 (KST) | 프레임 수 | 각도 | 방향 | 밝기(n px) |
|------|-----------|---------|------|------|-----------|
| #1 | 03:26:08~03:26:20 | 13/15f | 35° | 우상향 | 12~34 |
| #2 | 03:27:25~03:27:40 | 17/27f | 53° | 좌하향 | 11~27 |
| #3 | 03:43:01~03:43:14 | 16/16f | 35° | 우상향 | **33~49** |
| #4 | 03:45:42~03:45:58 | 16/20f | 17° | 우상향 | 8~18 |
| #5 | 03:50:32~03:50:34 | 3/3f | 35° | 우상향 | 24~50 |
| #6 | 04:00:10~04:00:23 | 18/18f | 16° | 우상향 | 22~34 |
| #7 | 04:07:37~04:08:13 | **37/37f** | 23° | 좌하향 | 12~24 |
| #8 | 04:13:55~04:14:07 | 11/14f | — | 좌하향 | — |
| #9 | 04:37:45~04:37:56 | 12/12f | — | 좌하향 | — |
| #10 | 04:40:13~04:40:25 | 10/13f | — | 우상향 | — |

- 기존 `detect_trail.py`: 동일 세션에서 3건만 탐지 (밝은 위성)
- 궤적#3 (n=33~49): 비교적 밝은데도 기존 알고리즘이 놓침 → FPN 오염 때문
- 궤적#7 (37프레임): 37초 동안 천천히 이동 — 가장 긴 단일 패스
- 각도 범위: 11°~55° 탐지 성공 (거의 수평 궤적 포함)

### 거짓 탐지율

- 원시 탐지 166건 → 궤적 10건 + orphan 9건
- Orphan 중 single_frame: 3건 (75분에 3건 = 2.4건/시간)
- 실시간 감시라면 최대 3회 불필요한 ring 발동 (약 36MB 낭비)
- `_RING_STATIC` (같은 각도 4회 반복 억제) 로 FPN 잔여물 자동 차단

---

## Pi 3B 실시간 통합 계획

### 현재 구조 (`camera.py` `_make_scout_worker`)

```python
# 현재: 단일 프레임 분석
trail = self.detect_trail(raw)          # detect_trail.py (Hough+PCA)
if trail.get('detected'):
    fire_ring_trigger(ring_snap)        # pre8 + hit + post15 저장
```

### 변경 계획

```python
# 추가: 롤링 배경 버퍼 (scout loop 초기화 시)
from collections import deque
detect_buf = deque(maxlen=4)           # 과거 4프레임 numpy 배열 보관

# 매 프레임:
detect_buf.append(frame_array)         # 원본 해상도 numpy (JPEG 저장 전)

# 탐지 (기존 detect_trail 과 병렬):
if len(detect_buf) >= 3:
    scaled = downscale(frame_array, max_dim=600)
    bg     = mean([downscale(f, 600) for f in detect_buf])
    hits   = detect_in_diff(scaled - bg)
    if hits and not self._is_static_angle(hits[0]['angle_deg']):
        fire_ring_trigger(ring_snap)
```

### 왜 과거 4프레임으로도 충분한가

LEO 위성은 프레임당 20~55px 이동한다.  
과거 4프레임에서 위성은 항상 **다른 픽셀**에 있으므로 배경 평균이 오염되지 않는다.

```
배경: frame[N-4], [N-3], [N-2], [N-1]
        위성 위치:   X-80px  X-60px  X-40px  X-20px

현재: frame[N]  위성 위치: X
→ mean(배경)[X] ≈ 하늘 밝기 (위성 없음)
→ diff[X] = 위성 신호 ✅
```

### 타이밍 (Pi 3B 추정)

```
프레임 캡처 (500ms 노출)
  ↓ 캡처 완료
detect_in_diff 실행 (~142ms, 병렬 스레드)
  ↓
다음 프레임 캡처 시작 (500ms 뒤)
```

현재 scout worker가 이미 별도 스레드에서 실행되므로 추가 지연 없음.

---

## 사용법

```bash
# 기본 실행 (전체 scout 아카이브)
python3 detect_faint.py ~/obs/20260525_dawn/scout/

# 빠른 구간 테스트
python3 detect_faint.py ~/obs/.../scout/ --range 60 130 --verbose

# 민감도 높이기 (σ 낮추면 더 희미한 것도 탐지, 거짓 탐지 증가)
python3 detect_faint.py ~/obs/.../scout/ --sigma 3.0

# 정밀도 높이기 (800px, 속도 느려짐)
python3 detect_faint.py ~/obs/.../scout/ --max-dim 800

# 주요 파라미터
#   --buf N        배경 프레임 수 ±N (기본 2)
#   --sigma F      탐지 임계값 (기본 3.5)
#   --max-dim N    처리 해상도 (기본 600)
#   --min-n N      클러스터 최소 픽셀 수 (기본 5)
#   --min-len F    streak 최소 길이 px (기본 6.0)
```

### 탐지 결과 JSON 필드

탐지된 프레임의 `.json` 사이드카에 `faint_detection` 블록 추가:

```json
{
  "captured_at": "2026-05-25T03:27:25.637",
  "captured_at_utc": "2026-05-24T18:27:25.637Z",
  "faint_detection": {
    "trajectory_id": 2,
    "trajectory_total_detections": 17,
    "trajectory_span_frames": 27,
    "trajectory_delta_cy_px": 552.0,
    "trajectory_delta_cx_px": -405.0,
    "streak_cy_px": 5.3,
    "streak_cx_px": 591.1,
    "streak_angle_deg": 53.9,
    "streak_n_pixels": 22,
    "streak_len_px": 11.4,
    "note": "captured_at/captured_at_utc = streak center time"
  }
}
```

> `captured_at`은 `camera.py`에서 `datetime.now() - shutter_us/2`로 계산하므로  
> **이미 노출 중간(streak 중심) 시각**이다. 별도 보정 불필요.

---

## 관련 파일

| 파일 | 역할 |
|------|------|
| `detect_faint.py` | 배치 탐지기 (이 문서의 대상) |
| `detect_trail.py` | 실시간 ring 트리거용 탐지기 (기존) |
| `analyze_misses.py` | `detect_trail.py` CSV 결과 miss 분석 |
| `camera.py` | 감시 루프, ring 트리거 (`_make_scout_worker`) |
