#!/usr/bin/env python3
"""
detect_faint.py — 시간 차분(temporal differencing) 기반 희미한 위성 탐지

알고리즘:
1. ±BUF_SIZE 이웃 프레임의 중앙값(배경) 계산
2. diff = current - background
3. diff에 row/col 중앙값 재적용 (FPN 대각선 잔여물 제거)
4. 양의 잔차에서 compact linear cluster 탐지 (n=5~60px)
5. PCA로 선형성/각도/밀도/집중도 검증

Usage:
    python3 detect_faint.py ~/obs/20260525_dawn/scout/
    python3 detect_faint.py ~/obs/20260525_dawn/scout/ --range 60 130   # 구간 테스트
    python3 detect_faint.py ~/obs/20260525_dawn/scout/ --buf 6 --sigma 3.5
    python3 detect_faint.py ~/obs/20260525_dawn/scout/ --verbose         # 상세 출력
"""

import sys, os, argparse, time
from collections import defaultdict

try:
    import numpy as np
    from PIL import Image
    import scipy.ndimage as ndi
except ImportError:
    print("pip install numpy pillow scipy")
    sys.exit(1)


# ─── 이미지 로드 ─────────────────────────────────────────────────────────────

def load_gray(path: str, max_dim: int = 600) -> np.ndarray:
    """JPEG → grayscale float32 (최장변 max_dim px로 다운스케일)

    기본 600px: Pi 3B RAM 처리 기준 ~142ms (500ms 프레임의 28%)
    800px: ~418ms (너무 빡빡), 500px: ~91ms (위성 streak 5px 미만 위험)
    """
    img = Image.open(path).convert('L')
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    return np.array(img, dtype=np.float32)


# ─── 단일 diff 분석 ───────────────────────────────────────────────────────────

def detect_in_diff(diff: np.ndarray,
                   sigma_thresh: float = 3.5,
                   min_n: int = 5,   max_n: int = 60,
                   min_lin: float = 0.88,
                   min_len: float = 6.0,
                   min_density: float = 0.40,
                   min_conc: float = 0.55) -> list[dict]:
    """
    diff 이미지에서 compact linear cluster를 찾아 위성 궤적 후보 반환.

    Returns list of dicts: cy, cx, n, lin, len, angle, density, conc
    """
    # 1) diff 자체에 FPN 잔여물 제거
    # row median: 수평 FPN 밴딩 제거 (가장 중요)
    # col median: 수직/대각 FPN 잔여물 제거
    d = diff - np.median(diff, axis=1, keepdims=True)
    d = d    - np.median(d,   axis=0, keepdims=True)
    # 600px 이하에서는 col median 생략해도 무방 (row만으로 충분히 빠름)

    # 2) 양의 잔차만
    pos = np.clip(d, 0, None)
    valid = pos[pos > 0]
    if len(valid) < 20:
        return []

    mu    = float(valid.mean())
    sigma = float(valid.std())
    if sigma < 1e-6:
        return []

    # 3) 임계값 마스크
    mask = (pos > mu + sigma_thresh * sigma).astype(np.int32)

    # 4) 연결 성분 라벨링 + 크기 필터 (핵심 최적화: 대부분 1~4px 노이즈 스킵)
    labels, n_labels = ndi.label(mask)
    if n_labels == 0:
        return []

    label_ids = np.arange(1, n_labels + 1, dtype=np.int32)
    sizes     = ndi.sum(mask, labels, label_ids).astype(int)
    valid_ids = label_ids[(sizes >= min_n) & (sizes <= max_n)]

    if len(valid_ids) == 0:
        return []

    # 5) 후보 클러스터마다 PCA
    results = []
    for lbl in valid_ids:
        pts = np.argwhere(labels == lbl).astype(np.float32)  # (row, col)
        n   = len(pts)
        ctr = pts.mean(axis=0)
        c   = pts - ctr

        cov        = (c.T @ c) / n
        ev, evec   = np.linalg.eigh(cov)
        ev0        = max(float(ev[-1]), 1e-9)
        ev1        = max(float(ev[0]),  1e-9)
        lin        = ev0 / (ev0 + ev1)

        pmaj = c @ evec[:, -1]
        pmin = c @ evec[:,  0]
        tlen = float(pmaj.max() - pmaj.min())
        twid = float(pmin.max() - pmin.min())
        band = max(3.0, twid * 0.25)
        conc = float(np.mean(np.abs(pmin) < band))

        dy, dx = float(evec[0, -1]), float(evec[1, -1])
        angle   = abs(float(np.degrees(np.arctan2(dy, dx)))) % 90
        density = n / max(tlen, 1.0)

        if (lin     >= min_lin     and
            tlen    >= min_len     and
            density >= min_density and
            conc    >= min_conc    and
            8 < angle < 82):
            results.append({
                'cy':      round(float(ctr[0]), 1),
                'cx':      round(float(ctr[1]), 1),
                'n':       n,
                'lin':     round(lin, 3),
                'len':     round(tlen, 1),
                'angle':   round(angle, 1),
                'density': round(density, 3),
                'conc':    round(conc, 3),
            })

    return results


# ─── 롤링 버퍼 처리 ──────────────────────────────────────────────────────────

def process_archive(all_paths: list[str],
                    buf_size: int    = 2,
                    sigma_thresh: float = 3.5,
                    min_n: int = 5,   max_n: int = 60,
                    min_lin: float = 0.88,
                    min_len: float = 6.0,
                    min_density: float = 0.40,
                    min_conc: float = 0.55,
                    max_dim: int = 600,
                    verbose: bool = False) -> list[dict]:
    """
    롤링 윈도우로 아카이브 전체 처리.

    배경 = median(이웃 ±buf_size 프레임) — 타깃 프레임 제외.
    메모리: 한 번에 2*buf_size+1 프레임만 유지.
    """
    n_total = len(all_paths)
    detections = []

    # 롤링 캐시: {idx: array}
    cache: dict[int, np.ndarray] = {}

    def ensure(idx):
        """캐시에 없으면 로드"""
        if idx not in cache:
            cache[idx] = load_gray(all_paths[idx], max_dim=max_dim)
        return cache[idx]

    def evict_before(idx):
        """idx보다 오래된 항목 제거"""
        for k in list(cache.keys()):
            if k < idx:
                del cache[k]

    t0 = time.time()
    for i in range(n_total):
        # 필요한 인덱스 결정
        bg_indices = [j for j in range(max(0, i - buf_size),
                                        min(n_total, i + buf_size + 1))
                      if j != i]

        # 로드
        cur = ensure(i)
        bg_arrays = [ensure(j) for j in bg_indices]
        evict_before(max(0, i - buf_size))

        if len(bg_arrays) < max(2, buf_size // 2):
            continue  # 버퍼 불충분 (시작/끝 경계)

        # mean이 median보다 ~32배 빠름 (LEO 위성은 매 프레임 20px+ 이동하므로
        # 배경 오염 없음 — 위성이 배경 프레임들에서도 다른 픽셀에 있음)
        bg   = np.mean(np.stack(bg_arrays, axis=0), axis=0)
        diff = cur - bg

        hits = detect_in_diff(diff,
                               sigma_thresh=sigma_thresh,
                               min_n=min_n, max_n=max_n,
                               min_lin=min_lin, min_len=min_len,
                               min_density=min_density, min_conc=min_conc)

        fname = os.path.basename(all_paths[i])
        if hits:
            for h in hits:
                detections.append({'frame': i, 'file': fname, **h})
            if verbose:
                for h in hits:
                    print(f"  [{i:05d}] {fname}  "
                          f"cy={h['cy']:6.1f} cx={h['cx']:6.1f}  "
                          f"n={h['n']:3d}  lin={h['lin']:.3f}  "
                          f"len={h['len']:5.1f}  ang={h['angle']:5.1f}°  "
                          f"den={h['density']:.3f}  conc={h['conc']:.3f}")

        # 진행 표시 (verbose 아닐 때)
        if not verbose and (i % 100 == 0 or i == n_total - 1):
            elapsed = time.time() - t0
            rate    = (i + 1) / elapsed if elapsed > 0 else 0
            eta     = (n_total - i - 1) / rate if rate > 0 else 0
            print(f"  {i+1:5d}/{n_total}  탐지={len(detections)}건  "
                  f"{rate:.1f}fps  남은시간={eta:.0f}s",
                  end='\r', flush=True)

    if not verbose:
        print()  # 줄바꿈

    return detections


# ─── 궤적 클러스터링 (여러 프레임의 탐지를 하나의 위성으로 묶기) ────────────

def cluster_trajectory(detections: list[dict],
                       max_frame_gap: int = 4,
                       max_px_per_frame: float = 55.0,
                       min_frames: int = 3) -> list[list[dict]]:
    """
    연속 프레임의 탐지를 하나의 위성 궤적으로 묶음.

    - 프레임 간 이동 속도 제한: max_px_per_frame × frame_gap
    - 방향 일관성 체크 (세 번째 점부터)
    - min_frames 미만이면 제외 (단일 프레임 거짓 탐지 걸러냄)
    """
    if not detections:
        return []

    # 같은 프레임의 여러 탐지 → 대표값으로 병합 (가장 lin 높은 것 우선)
    from collections import defaultdict
    by_frame: dict[int, list[dict]] = defaultdict(list)
    for d in detections:
        by_frame[d['frame']].append(d)

    merged: list[dict] = []
    for fr in sorted(by_frame):
        cluster = by_frame[fr]
        if len(cluster) == 1:
            merged.append(cluster[0])
        else:
            # 같은 각도 그룹 내에서 묶기 (±10°)
            best = max(cluster, key=lambda x: x['lin'])
            merged.append(best)

    merged.sort(key=lambda x: x['frame'])

    trajectories = []
    used = set()

    for i, d in enumerate(merged):
        if i in used:
            continue
        traj = [d]
        used.add(i)

        for j in range(i + 1, len(merged)):
            if j in used:
                continue
            d2 = merged[j]
            frame_gap = d2['frame'] - traj[-1]['frame']
            if frame_gap > max_frame_gap:
                break

            dcy = d2['cy'] - traj[-1]['cy']
            dcx = d2['cx'] - traj[-1]['cx']
            dist = (dcy**2 + dcx**2) ** 0.5

            # 프레임 간 거리 허용 (gap에 비례)
            if dist > max_px_per_frame * frame_gap:
                continue

            # 방향 일관성 체크 (세 번째 점부터)
            if len(traj) >= 2:
                prev_dcy = traj[-1]['cy'] - traj[-2]['cy']
                prev_dcx = traj[-1]['cx'] - traj[-2]['cx']
                # 방향 벡터 내적 (음수 = 반대 방향)
                dot = dcy * prev_dcy + dcx * prev_dcx
                if dot < 0:
                    continue

            traj.append(d2)
            used.add(j)

        if len(traj) >= min_frames:
            trajectories.append(traj)

    return trajectories


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='시간 차분으로 희미한 위성 탐지 (scout 아카이브용)')
    parser.add_argument('scout_dir', help='scout 디렉터리 경로')
    parser.add_argument('--range', nargs=2, type=int, metavar=('FROM', 'TO'),
                        help='처리할 프레임 인덱스 범위 (기본: 전체)')
    parser.add_argument('--buf', type=int, default=2,
                        help='시간 차분 버퍼 크기 ±N (기본: 2, mean 사용)')
    parser.add_argument('--sigma', type=float, default=3.5,
                        help='탐지 sigma 임계값 (기본: 3.5)')
    parser.add_argument('--min-n',       type=int,   default=5)
    parser.add_argument('--max-n',       type=int,   default=60)
    parser.add_argument('--min-lin',     type=float, default=0.88)
    parser.add_argument('--min-len',     type=float, default=6.0)
    parser.add_argument('--min-density', type=float, default=0.40)
    parser.add_argument('--min-conc',    type=float, default=0.55)
    parser.add_argument('--max-dim',     type=int,   default=600,
                        help='이미지 최장변 px (기본 600: Pi 3B ~142ms/프레임)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='탐지마다 즉시 출력')
    args = parser.parse_args()

    # arch 파일 목록 (번호순)
    all_files = sorted([
        os.path.join(args.scout_dir, f)
        for f in os.listdir(args.scout_dir)
        if f.endswith('_arch.jpg')
    ])
    if not all_files:
        print(f"ERROR: {args.scout_dir} 에서 *_arch.jpg 파일을 찾을 수 없음")
        sys.exit(1)

    if args.range:
        fr, to = args.range
        all_files = all_files[fr:to]
        print(f"프레임 {fr}~{to} ({len(all_files)}개)")
    else:
        print(f"전체 {len(all_files)}개 프레임")

    print(f"설정: buf=±{args.buf}  σ={args.sigma}  max_dim={args.max_dim}px  "
          f"n={args.min_n}~{args.max_n}  "
          f"lin≥{args.min_lin}  len≥{args.min_len}  "
          f"den≥{args.min_density}  conc≥{args.min_conc}\n")

    t_start = time.time()
    detections = process_archive(
        all_files,
        buf_size    = args.buf,
        sigma_thresh= args.sigma,
        min_n       = args.min_n,   max_n    = args.max_n,
        min_lin     = args.min_lin, min_len  = args.min_len,
        min_density = args.min_density, min_conc = args.min_conc,
        max_dim     = args.max_dim,
        verbose     = args.verbose,
    )
    elapsed = time.time() - t_start

    print(f"\n처리 완료: {elapsed:.1f}초  ({elapsed/len(all_files)*1000:.1f}ms/프레임)")
    print(f"원시 탐지: {len(detections)}건\n")

    # ── 궤적 묶기
    trajectories = cluster_trajectory(detections)

    # ── 요약 출력
    if not detections:
        print("탐지 없음. --sigma를 낮추거나 --min-len / --min-n을 줄여보세요.")
        return

    # 프레임별 탐지 목록
    print("── 원시 탐지 목록 ──────────────────────────────────────────────────")
    print(f"{'idx':>6}  {'file':<46}  {'cy':>6}  {'cx':>6}  "
          f"{'n':>3}  {'lin':>5}  {'len':>5}  {'ang':>5}  {'den':>5}  {'conc':>5}")
    for d in detections:
        print(f"{d['frame']:6d}  {d['file']:<46}  "
              f"{d['cy']:6.1f}  {d['cx']:6.1f}  "
              f"{d['n']:3d}  {d['lin']:5.3f}  {d['len']:5.1f}  "
              f"{d['angle']:5.1f}  {d['density']:5.3f}  {d['conc']:5.3f}")

    if trajectories:
        print(f"\n── 궤적 ({len(trajectories)}건) ──────────────────────────────────────────")
        for ti, traj in enumerate(trajectories):
            first, last = traj[0], traj[-1]
            n_frames = last['frame'] - first['frame'] + 1
            dcy = last['cy'] - first['cy']
            dcx = last['cx'] - first['cx']
            speed_px = (dcy**2 + dcx**2)**0.5 / max(1, len(traj)-1)
            print(f"  궤적#{ti+1}  프레임={first['frame']}~{last['frame']} "
                  f"({len(traj)}개 탐지/{n_frames}프레임)  "
                  f"Δcy={dcy:+.0f}px  Δcx={dcx:+.0f}px  "
                  f"속도≈{speed_px:.1f}px/frame")
            for d in traj:
                print(f"    [{d['frame']:5d}] cy={d['cy']:6.1f} cx={d['cx']:6.1f}  "
                      f"ang={d['angle']:5.1f}°  n={d['n']:3d}  len={d['len']:5.1f}")


if __name__ == '__main__':
    main()
