#!/usr/bin/env python3
"""
analyze_misses.py — CSV에서 miss된 사진 중 "아슬아슬한" 케이스를 찾아 상세 분석.

Usage:
    python3 analyze_misses.py /tmp/scout_results.csv
    python3 analyze_misses.py /tmp/scout_results.csv --top 30
    python3 analyze_misses.py /tmp/scout_results.csv --rerun-top 20  # 느슨한 임계값으로 재시도
"""

import sys
import csv
import os
import argparse
import io

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("pip install numpy pillow")
    sys.exit(1)

SCOUT_DIR = os.path.expanduser('~/obs/20260525_dawn/scout/')


def score_miss(row):
    """miss 행에서 '아슬아슬함' 점수 계산 — 높을수록 위성일 가능성↑"""
    try:
        lin = float(row.get('linearity') or 0)
        n   = int(row.get('n_bright') or 0)
        den = float(row.get('density') or 0)
        ang = float(row.get('angle_deg') or 0)
        # 수평/수직 아티팩트는 제외
        angle_ok = 8 < ang < 82
        return lin * min(1.0, n / 10.0) * min(1.0, den / 0.1) * (1 if angle_ok else 0)
    except Exception:
        return 0.0


def detect_detail(image_data: bytes):
    """detect_trail 중간 결과를 모두 반환하는 진단판."""
    img = Image.open(io.BytesIO(image_data)).convert('L')
    w, h = img.size
    scale = min(1.0, 800 / w)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    arr = np.array(img, dtype=np.float32)

    row_med = np.median(arr, axis=1, keepdims=True)
    col_med = np.median(arr, axis=0, keepdims=True)
    cleaned = arr - row_med - col_med + float(arr.mean())
    mu, sigma = float(cleaned.mean()), float(cleaned.std())

    results = {}
    for sig_mult in (4.0, 4.5, 5.0, 5.5, 6.0, 7.0):
        coords = np.argwhere(cleaned > mu + sig_mult * sigma)
        n = len(coords)
        if n < 4 or n > 500:
            results[f'{sig_mult}σ'] = {'n': n, 'skip': True}
            continue
        pts = coords.astype(np.float32)
        ctr = pts.mean(axis=0)
        c   = pts - ctr
        cov = (c.T @ c) / n
        ev, evec = np.linalg.eigh(cov)
        ev0 = max(float(ev[-1]), 1e-9)
        ev1 = max(float(ev[0]),  1e-9)
        lin  = ev0 / (ev0 + ev1)
        pmaj = c @ evec[:, -1]
        pmin = c @ evec[:,  0]
        tlen = float(pmaj.max() - pmaj.min())
        twid = float(pmin.max() - pmin.min())
        band = max(4.0, twid * 0.25)
        conc = float(np.mean(np.abs(pmin) < band))
        dy, dx = float(evec[0, -1]), float(evec[1, -1])
        angle = abs(float(np.degrees(np.arctan2(dy, dx)))) % 90
        density = n / max(tlen, 1.0)
        results[f'{sig_mult}σ'] = {
            'n': n, 'lin': round(lin,3), 'len': round(tlen,1),
            'wid': round(twid,1), 'conc': round(conc,3),
            'angle': round(angle,1), 'density': round(density,3),
            'skip': False,
        }
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv_file')
    parser.add_argument('--top', type=int, default=20, help='상위 N개 출력')
    parser.add_argument('--rerun-top', type=int, default=0,
                        help='상위 N개 사진에 완화된 임계값으로 재분석')
    args = parser.parse_args()

    with open(args.csv_file) as f:
        rows = list(csv.DictReader(f))

    misses = [r for r in rows if r['detected'] == 'False']
    print(f"전체: {len(rows)}장  miss: {len(misses)}장  탐지: {len(rows)-len(misses)}장\n")

    # reason 분류
    from collections import Counter
    reasons = Counter(r['reason'] for r in misses)
    print("── Miss 사유 분포 ──────────────────────")
    for reason, cnt in reasons.most_common():
        print(f"  {cnt:4d}장  {reason}")
    print()

    # 아슬아슬한 miss 상위 N개
    misses.sort(key=score_miss, reverse=True)
    top = misses[:args.top]
    print(f"── 위성 가능성 높은 miss 상위 {args.top}개 ──────────────────────")
    print(f"{'파일':<50} {'lin':>6} {'n':>5} {'den':>6} {'ang':>6}  reason")
    for r in top:
        fname = r['file']
        lin = r.get('linearity','?')
        n   = r.get('n_bright','?')
        den = r.get('density','?')
        ang = r.get('angle_deg','?')
        reason = r.get('reason','')[:40]
        print(f"{fname:<50} {lin:>6} {n:>5} {den:>6} {ang:>6}  {reason}")

    if args.rerun_top > 0:
        print(f"\n── 상위 {args.rerun_top}개 다단계 sigma 상세 분석 ──────────────────────")
        for r in top[:args.rerun_top]:
            fname = r['file']
            path = os.path.join(SCOUT_DIR, fname)
            if not os.path.exists(path):
                print(f"  {fname}: 파일 없음")
                continue
            with open(path, 'rb') as fh:
                data = fh.read()
            detail = detect_detail(data)
            print(f"\n  {fname}")
            print(f"  {'σ':>5}  {'n':>5}  {'lin':>6}  {'len':>6}  {'wid':>5}  {'conc':>6}  {'ang':>6}  {'density':>7}")
            for sig_key, d in detail.items():
                if d.get('skip'):
                    print(f"  {sig_key:>5}  {d['n']:>5}  {'(skip)':>6}")
                    continue
                # 조건 체크
                flags = []
                if d['lin'] < 0.88: flags.append('lin↓')
                if d['len'] < 20:   flags.append('len↓')
                if d['density'] < 0.25: flags.append('den↓')
                if d['conc'] < 0.55: flags.append('conc↓')
                if not (8 < d['angle'] < 82): flags.append('ang!')
                flag_str = ' '.join(flags)
                print(f"  {sig_key:>5}  {d['n']:>5}  {d['lin']:>6}  {d['len']:>6}  {d['wid']:>5}  {d['conc']:>6}  {d['angle']:>6}  {d['density']:>7}  {flag_str}")


if __name__ == '__main__':
    main()
