#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-scale Laplacian Variance + Tenengrad (AND Ensemble) Blur Detector

機能:
- 多尺度ラプラシアン分散 (MS: Multi-Scale) でブレ度スコアを算出
- Tenengrad (Sobel勾配) のスコアも算出
- AND判定: MSが低い かつ Tenengradも低い場合のみ「ブレ」と確定（誤検出を減らす）
- 自動しきい値（percentile / zscore） or 固定しきい値
- CSV出力（path, score, ten_score, is_blur, rule, status）
- ブレ画像の移動/コピー、最大辺リサイズ、前処理(GaussianBlur)

依存:
- opencv-python, numpy
- pandas（任意。なければ標準csvで出力）
"""

import argparse
import sys
import math
from pathlib import Path
from typing import List, Tuple, Optional
import shutil

import cv2
import numpy as np

try:
    import pandas as pd
except ImportError:
    pd = None  # pandasが無ければ標準csvでフォールバック


# ----------------------------
# 画像I/O & 前処理
# ----------------------------
def imread_rgb(path: Path) -> Optional[np.ndarray]:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def resize_max_side(img_rgb: np.ndarray, max_side: Optional[int]) -> np.ndarray:
    if not max_side:
        return img_rgb
    h, w = img_rgb.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return img_rgb
    scale = max_side / float(side)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(img_rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)


def gaussian_preblur(gray: np.ndarray, ksize: int, sigma: float) -> np.ndarray:
    k = int(ksize)
    if k < 1:
        return gray
    if k % 2 == 0:
        k += 1
    if k <= 1:
        return gray
    return cv2.GaussianBlur(gray, (k, k), sigmaX=float(sigma) if sigma > 0 else 0.0)


# ----------------------------
# ブレスコア算出
# ----------------------------
def laplacian_variance(gray: np.ndarray, ksize: int = 3) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_64F, ksize=ksize)
    return float(lap.var())


def multiscale_laplacian_variance(
    gray: np.ndarray,
    scales: List[float] = (1.0, 0.5, 0.25),
    ksize: int = 3,
    agg: str = "mean",  # mean|median|max|min
) -> float:
    vals = []
    for s in scales:
        if s <= 0:
            continue
        if s == 1.0:
            g = gray
        else:
            new_h = max(1, int(round(gray.shape[0] * s)))
            new_w = max(1, int(round(gray.shape[1] * s)))
            g = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        vals.append(laplacian_variance(g, ksize=ksize))

    if not vals:
        return 0.0

    agg = (agg or "mean").lower()
    if agg == "median":
        return float(np.median(vals))
    if agg == "max":
        return float(np.max(vals))
    if agg == "min":
        return float(np.min(vals))
    return float(np.mean(vals))


def tenengrad_focus_measure(gray: np.ndarray, ksize: int = 3) -> float:
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=ksize)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=ksize)
    fm = gx * gx + gy * gy  # 勾配強度の二乗和（高いほどシャープ）
    return float(np.mean(fm))


# ----------------------------
# しきい値決定
# ----------------------------
def decide_threshold(
    scores: List[float],
    fixed_threshold: Optional[float],
    auto_mode: Optional[str],
    auto_param: float,
) -> float:
    """
    - fixed_threshold が与えられればそれを使う
    - auto_mode:
        * 'percentile' : 下位パーセンタイル値（例: 20 -> 下位20%をブレ候補）
        * 'zscore'     : mean - α*std
        * None/'none'  : デフォルトで20%タイル
    """
    if fixed_threshold is not None:
        return float(fixed_threshold)

    if not scores:
        return 0.0

    arr = np.asarray(scores, dtype=np.float64)
    mode = (auto_mode or "percentile").lower()

    if mode == "percentile":
        q = float(auto_param)
        q = min(max(q, 0.0), 100.0)
        return float(np.percentile(arr, q))

    if mode == "zscore":
        mean = float(np.mean(arr))
        std = float(np.std(arr))
        return float(mean - auto_param * std)

    # デフォルト: 20パーセンタイル
    return float(np.percentile(arr, 20.0))


# ----------------------------
# ユーティリティ
# ----------------------------
def collect_images(input_dir: Path, patterns: List[str]) -> List[Path]:
    files = []
    for pat in patterns:
        files.extend(sorted(input_dir.rglob(pat)))
    seen = set()
    unique = []
    for p in files:
        if p.is_file():
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"):
                if p not in seen:
                    unique.append(p)
                    seen.add(p)
    return unique


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def move_or_copy(src: Path, dst_dir: Path, copy: bool = False):
    ensure_dir(dst_dir)
    dst = dst_dir / src.name
    if copy:
        shutil.copy2(str(src), str(dst))
    else:
        shutil.move(str(src), str(dst))


def to_csv(rows: List[dict], out_csv: Path):
    if not rows:
        return
    if pd is None:
        import csv
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
    else:
        df = pd.DataFrame(rows)
        df.to_csv(out_csv, index=False, encoding="utf-8")


# ----------------------------
# メイン処理
# ----------------------------
def process_one(
    path: Path,
    max_side: Optional[int],
    gauss_ksize: int,
    gauss_sigma: float,
    scales: List[float],
    ksize: int,
    agg: str,
    ten_ksize: int,
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    img = imread_rgb(path)
    if img is None:
        return None, None, "read_error"

    img = resize_max_side(img, max_side)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    if gauss_ksize > 0:
        gray = gaussian_preblur(gray, gauss_ksize, gauss_sigma)

    ms_score = multiscale_laplacian_variance(gray, scales=scales, ksize=ksize, agg=agg)
    ten_score = tenengrad_focus_measure(gray, ksize=ten_ksize)
    return ms_score, ten_score, None


def main():
    ap = argparse.ArgumentParser(description="Multi-scale + Tenengrad Blur Detector (AND Ensemble)")
    ap.add_argument("input", type=str, help="入力ディレクトリ")
    ap.add_argument("--patterns", nargs="*", default=["*.jpg", "*.jpeg", "*.png", "*.bmp", "*.tif", "*.tiff", "*.webp"],
                    help="探索パターン（スペース区切り）")
    ap.add_argument("--max-side", type=int, default=2000, help="最大辺ピクセル（縮小）。0で無効")
    ap.add_argument("--gauss-ksize", type=int, default=0, help="前処理GaussianBlurのksize（奇数）。0で無効")
    ap.add_argument("--gauss-sigma", type=float, default=0.0, help="GaussianBlurのsigmaX")

    ap.add_argument("--scales", type=str, default="1.0,0.5,0.25", help="多尺度の倍率（例: 1.0,0.5,0.25）")
    ap.add_argument("--lap-ksize", type=int, default=3, help="Laplacianのksize（奇数推奨）")
    ap.add_argument("--agg", type=str, default="mean", choices=["mean", "median", "max", "min"], help="スコア集約方法")

    # MS側しきい値
    ap.add_argument("--threshold", type=float, default=None, help="MSの固定しきい値（この値未満をブレ候補）")
    ap.add_argument("--auto-th", type=str, default="percentile", choices=["percentile", "zscore", "none"],
                    help="MS自動しきい値の方法（threshold未指定時）")
    ap.add_argument("--auto-param", type=float, default=20.0,
                    help="MS: percentileなら%（例20）、zscoreならα（mean-α*std）")

    # Tenengrad（Sobel）側
    ap.add_argument("--and-tenengrad", dest="and_tenengrad", action="store_true",
                    help="Tenengrad（Sobel）とMSのANDで最終判定（誤検出減）")
    ap.add_argument("--ten-ksize", type=int, default=3, help="TenengradのSobel ksize")
    ap.add_argument("--ten-threshold", type=float, default=None, help="Tenengrad固定しきい値（この値未満をブレ候補）")
    ap.add_argument("--ten-auto-th", type=str, default="percentile", choices=["percentile", "zscore", "none"],
                    help="Tenengrad自動しきい値の方法")
    ap.add_argument("--ten-auto-param", type=float, default=25.0,
                    help="Tenengrad: percentileなら%（例25）、zscoreならα")

    # 出力/運用
    ap.add_argument("--move-blur", type=str, default=None, help="ブレ判定の画像を指定フォルダに移動")
    ap.add_argument("--copy-blur", action="store_true", help="移動ではなくコピーにする")
    ap.add_argument("--csv", type=str, default="blur_scores.csv", help="CSV出力パス")
    ap.add_argument("--preview", type=int, default=0, help="スコア下位N件を表示（ブレ候補の確認）")

    args = ap.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"[ERROR] 入力ディレクトリが見つかりません: {input_dir}", file=sys.stderr)
        sys.exit(1)

    # scalesの解析
    try:
        scales = [float(s.strip()) for s in args.scales.split(",") if s.strip()]
    except Exception:
        print("[ERROR] --scales の指定が不正です。例: 1.0,0.5,0.25", file=sys.stderr)
        sys.exit(1)

    images = collect_images(input_dir, args.patterns)
    if not images:
        print("[WARN] 対象画像が見つかりませんでした。", file=sys.stderr)
        sys.exit(0)

    rows = []
    ms_scores: List[float] = []
    ten_scores: List[float] = []
    errors = 0

    for p in images:
        ms, ten, err = process_one(
            p,
            max_side=args.max_side if args.max_side > 0 else None,
            gauss_ksize=args.gauss_ksize,
            gauss_sigma=args.gauss_sigma,
            scales=scales,
            ksize=args.lap_ksize,
            agg=args.agg,
            ten_ksize=args.ten_ksize,
        )
        if err or ms is None or math.isnan(ms) or ten is None or math.isnan(ten):
            errors += 1
            rows.append({"path": str(p), "score": None, "ten_score": None, "is_blur": None, "rule": None, "status": err or "error"})
        else:
            ms_scores.append(ms)
            ten_scores.append(ten)
            rows.append({"path": str(p), "score": ms, "ten_score": ten, "is_blur": None, "rule": None, "status": "ok"})

    if not ms_scores or not ten_scores:
        print("[ERROR] スコアが計算できませんでした（読み込み失敗や非対応形式の可能性）", file=sys.stderr)
        sys.exit(2)

    # しきい値決定（MS / Tenengrad）
    th_ms = decide_threshold(
        scores=ms_scores,
        fixed_threshold=args.threshold,
        auto_mode=(None if args.auto_th == "none" else args.auto_th),
        auto_param=args.auto_param,
    )
    th_ten = decide_threshold(
        scores=ten_scores,
        fixed_threshold=args.ten_threshold,
        auto_mode=(None if args.ten_auto_th == "none" else args.ten_auto_th),
        auto_param=args.ten_auto_param,
    )

    # 判定 & オプション処理
    blur_dir = Path(args.move_blur) if args.move_blur else None
    for r in rows:
        if r["status"] != "ok":
            continue
        ms_ok = (r["score"] < th_ms)
        if args.and_tenengrad:
            ten_ok = (r["ten_score"] < th_ten)
            is_blur = bool(ms_ok and ten_ok)
            rule = "AND(ms,ten)"
        else:
            is_blur = bool(ms_ok)
            rule = "MS_only"

        r["is_blur"] = is_blur
        r["rule"] = rule

        if blur_dir and is_blur:
            try:
                move_or_copy(Path(r["path"]), blur_dir, copy=args.copy_blur)
            except Exception as e:
                r["status"] = f"move_copy_error:{e}"

    # CSV出力
    out_csv = Path(args.csv)
    to_csv(rows, out_csv)

    # プレビュー（低スコア順）
    if args.preview > 0:
        ok_rows = [r for r in rows if r["status"] == "ok" and r["score"] is not None]
        ok_rows.sort(key=lambda x: x["score"])
        print("\n=== ブレ候補（MSスコア低い順） ===")
        for r in ok_rows[: args.preview]:
            mark = "BLUR" if r["is_blur"] else "SHARP"
            print(f'{r["score"]:10.3f} | Ten {r["ten_score"]:10.3f} | {mark:5s} | {r["rule"]:11s} | {r["path"]}')

    # レポート
    total = len(images)
    blur_cnt = sum(1 for r in rows if r.get("is_blur") is True)
    print("\n[RESULT]")
    print(f"  画像数: {total}")
    print(f"  失敗数: {errors}")
    print(f"  しきい値 MS:  {th_ms:.3f}  (mode={args.auto_th if args.threshold is None else 'fixed'})")
    print(f"  しきい値 TEN: {th_ten:.3f}  (mode={args.ten_auto_th if args.ten_threshold is None else 'fixed'})")
    print(f"  AND判定: {'ON' if args.and_tenengrad else 'OFF'}")
    print(f"  ブレ判定: {blur_cnt} / {total}")
    print(f"  CSV: {out_csv.resolve()}")
    if blur_dir:
        print(f"  ブレ画像 {'コピー' if args.copy_blur else '移動'} 先: {blur_dir.resolve()}")


if __name__ == "__main__":
    main()
