#!/usr/bin/env python3
"""
detect_trail.py — Standalone satellite trail detector

Run the StarCam Hough + Regional PCA detection algorithm on JPEG photos
captured by a Raspberry Pi IMX296 camera (or any sky photo).

Usage:
    python3 detect_trail.py <file.jpg>
    python3 detect_trail.py <folder/>
    python3 detect_trail.py <folder/> --copy-to <output/>
    python3 detect_trail.py <folder/> --csv results.csv
    python3 detect_trail.py <folder/> --quiet

Requirements:
    pip install numpy pillow
"""

import sys
import os
import io
import glob
import json
import time
import shutil
import argparse
import csv

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("Missing dependencies.  Run:  pip install numpy pillow")
    sys.exit(1)


# ── Detection algorithm ────────────────────────────────────────────────────────

def detect_trail(image_data: bytes) -> dict:
    """
    Two-stage Hough + Regional PCA satellite trail detector.

    Stage 1 — Rough Hough: find the line with the most inlier votes among
              high-sigma bright pixels.  Robust against scattered stars.
    Stage 2 — Regional PCA: run PCA only on the Hough inliers, giving precise
              linearity / length / width metrics without star contamination.
    Falls back to global PCA when the pixel count is small (twilight conditions).

    Parameters
    ----------
    image_data : bytes
        Raw JPEG data.

    Returns
    -------
    dict with keys:
        detected       (bool)
        method         (str)   'hough+pca' | 'pca@Nσ'
        linearity      (float) 0–1, higher = more linear
        trail_len_px   (float) trail length in pixels (at 800px width)
        trail_width_px (float) trail width in pixels
        concentration  (float) 0–1, fraction of pixels within band
        n_bright       (int)   number of above-threshold pixels
        reason         (str)   failure reason when detected=False
    """
    try:
        img = Image.open(io.BytesIO(image_data)).convert('L')
        w, h = img.size
        scale = min(1.0, 800 / w)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
        arr = np.array(img, dtype=np.float32)

        # 2-D fixed-pattern noise removal (row + column medians)
        row_med = np.median(arr, axis=1, keepdims=True)
        col_med = np.median(arr, axis=0, keepdims=True)
        cleaned = arr - row_med - col_med + float(arr.mean())

        mu    = float(cleaned.mean())
        sigma = float(cleaned.std())
        if sigma < 0.5:
            return {'detected': False, 'reason': 'uniform image (underexposed?)'}

        # ── Stage 1: Rough Hough ──────────────────────────────────────────────
        HOUGH_SIG   = 7.0
        HOUGH_TOL   = 3.0    # inlier tolerance in px
        HOUGH_MDIST = 30     # min pair separation to define a line
        HOUGH_MIN_N = 5      # min inliers to accept a line

        hough_coords = np.argwhere(cleaned > mu + HOUGH_SIG * sigma)
        n_hough = len(hough_coords)

        hough_inliers = None
        if 4 < n_hough <= 500:
            pts = hough_coords.astype(np.float32)
            best_count, best_mask = 0, None
            for i in range(len(pts)):
                diff  = pts - pts[i]
                dists = np.hypot(diff[:, 0], diff[:, 1])
                for j in np.where(dists > HOUGH_MDIST)[0]:
                    if j <= i:
                        continue
                    dy, dx = diff[j]
                    inv = 1.0 / dists[j]
                    ny, nx = -dx * inv, dy * inv
                    d    = np.abs(diff[:, 0] * ny + diff[:, 1] * nx)
                    mask = d < HOUGH_TOL
                    cnt  = int(mask.sum())
                    if cnt > best_count:
                        best_count = cnt
                        best_mask  = mask

            if best_count >= HOUGH_MIN_N and best_mask is not None:
                inlier_pts = pts[best_mask]
                c = inlier_pts - inlier_pts.mean(axis=0)
                _, evec_h = np.linalg.eigh((c.T @ c) / len(c))
                proj_h = c @ evec_h[:, -1]
                extent = float(proj_h.max() - proj_h.min())
                if best_count / max(extent, 1.0) > 0.05:
                    hough_inliers = inlier_pts

        # ── Stage 2: Regional PCA ─────────────────────────────────────────────
        def _pca(pts_2d):
            n   = len(pts_2d)
            ctr = pts_2d.mean(axis=0)
            c   = pts_2d - ctr
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
            return lin, tlen, twid, conc, n, angle

        MIN_ANGLE = 8.0  # reject within 8° of horizontal or vertical

        candidates = []
        if hough_inliers is not None and len(hough_inliers) >= HOUGH_MIN_N:
            lin, tlen, twid, conc, n, angle = _pca(hough_inliers)
            score = lin * conc * min(1.0, tlen / 50.0)
            candidates.append({'method': 'hough+pca', 'lin': lin, 'n': n,
                                'trail_len': tlen, 'trail_w': twid,
                                'conc': conc, 'score': score, 'angle': angle})

        for sig_mult in (5.0, 5.5, 6.0, 7.0):
            coords = np.argwhere(cleaned > mu + sig_mult * sigma)
            n = int(len(coords))
            if n < 4 or n > 300:
                continue
            lin, tlen, twid, conc, _, angle = _pca(coords.astype(np.float32))
            score = lin * conc * min(1.0, tlen / 50.0)
            candidates.append({'method': f'pca@{sig_mult}σ', 'lin': lin, 'n': n,
                                'trail_len': tlen, 'trail_w': twid,
                                'conc': conc, 'score': score, 'angle': angle})

        if not candidates:
            return {'detected': False, 'reason': 'no candidates'}

        best = max(candidates, key=lambda x: x['score'])
        ok = (best['lin'] > 0.88
              and best['trail_len'] > 20
              and best['trail_w']   < best['trail_len'] * 0.40
              and best['conc']      > 0.55
              and MIN_ANGLE < best['angle'] < 90 - MIN_ANGLE)

        if ok:
            return {
                'detected':       True,
                'method':         best['method'],
                'linearity':      round(best['lin'], 3),
                'trail_len_px':   round(best['trail_len'], 1),
                'trail_width_px': round(best['trail_w'], 1),
                'concentration':  round(best['conc'], 3),
                'angle_deg':      round(best['angle'], 1),
                'n_bright':       best['n'],
            }
        reason = f'low linearity (lin={best["lin"]:.2f}, len={best["trail_len"]:.0f}px)'
        if not (MIN_ANGLE < best['angle'] < 90 - MIN_ANGLE):
            reason = f'horizontal/vertical artifact (angle={best["angle"]:.1f}°)'
        return {
            'detected':  False,
            'reason':    reason,
            'linearity': round(best['lin'], 3),
            'angle_deg': round(best['angle'], 1),
            'n_bright':  best['n'],
        }

    except Exception as e:
        return {'detected': False, 'reason': str(e)}


# ── CLI ────────────────────────────────────────────────────────────────────────

def collect_files(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, '*.jpg')))
        files += sorted(glob.glob(os.path.join(path, '*.JPG')))
        return sorted(set(files))
    # glob pattern
    return sorted(glob.glob(path))


def main():
    parser = argparse.ArgumentParser(
        description='Batch satellite trail detection on StarCam JPEG photos.')
    parser.add_argument('input', help='JPEG file, folder, or glob pattern')
    parser.add_argument('--copy-to', metavar='DIR',
                        help='Copy detected photos (+ sidecar JSON) to this folder')
    parser.add_argument('--csv', metavar='FILE',
                        help='Write per-file results to a CSV file')
    parser.add_argument('--quiet', action='store_true',
                        help='Only print detected files')
    args = parser.parse_args()

    files = collect_files(args.input)
    if not files:
        print(f'No files found: {args.input}')
        sys.exit(1)

    if args.copy_to:
        os.makedirs(args.copy_to, exist_ok=True)

    csv_rows = []
    n_detected = 0
    t0 = time.time()
    w = 80  # display width

    for filepath in files:
        try:
            with open(filepath, 'rb') as fh:
                data = fh.read()
        except OSError as e:
            print(f'[ERROR] {os.path.basename(filepath)}: {e}')
            continue

        result = detect_trail(data)
        fname  = os.path.basename(filepath)

        if result['detected']:
            n_detected += 1
            tag = '\033[92m[DETECTED]\033[0m'
            detail = (f"method={result['method']}  "
                      f"lin={result['linearity']}  "
                      f"len={result['trail_len_px']}px  "
                      f"w={result['trail_width_px']}px  "
                      f"conc={result['concentration']}  "
                      f"n={result['n_bright']}")
            if not args.quiet:
                print(f'{tag} {fname}')
                print(f'           {detail}')
            if args.copy_to:
                shutil.copy2(filepath, os.path.join(args.copy_to, fname))
                sidecar = filepath.replace('.jpg', '.json').replace('.JPG', '.json')
                if os.path.exists(sidecar):
                    shutil.copy2(sidecar, os.path.join(args.copy_to,
                                                        os.path.basename(sidecar)))
        else:
            if not args.quiet:
                lin = result.get('linearity', '—')
                n   = result.get('n_bright', '—')
                reason = result.get('reason', '')
                print(f'\033[90m[  miss  ]\033[0m {fname}')
                print(f'           lin={lin}  n={n}  {reason}')

        if args.csv:
            row = {'file': fname, **result}
            csv_rows.append(row)

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    pct  = 100 * n_detected / len(files) if files else 0

    print()
    print('─' * w)
    print(f'총 {len(files):,}장 처리 | '
          f'감지 {n_detected:,}장 ({pct:.1f}%) | '
          f'소요 {mins}m {secs:02d}s')
    if args.copy_to:
        print(f'→ 감지 파일 복사: {args.copy_to}')

    if args.csv and csv_rows:
        fieldnames = ['file', 'detected', 'method', 'linearity',
                      'trail_len_px', 'trail_width_px', 'concentration',
                      'angle_deg', 'n_bright', 'reason']
        with open(args.csv, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f'→ CSV 저장: {args.csv}')


if __name__ == '__main__':
    main()
