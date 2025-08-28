# blur_cleaner/detectors.py
from __future__ import annotations
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import cv2, numpy as np

def _gaussian(gray: np.ndarray, ksize: int = 3, sigma: float = 0.0) -> np.ndarray:
    if not ksize or ksize <= 1: return gray
    if ksize % 2 == 0: ksize += 1
    return cv2.GaussianBlur(gray, (ksize, ksize), sigmaX=sigma if sigma > 0 else 0.0)

def _lap_var(gray: np.ndarray, ksize: int = 3) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F, ksize=ksize).var())

def _ms_lap_var(gray: np.ndarray, scales=(1.0, 0.5, 0.25), ksize: int = 3, agg: str = "median") -> float:
    vals = []
    for s in scales:
        g = gray if s == 1.0 else cv2.resize(gray, (int(gray.shape[1]*s), int(gray.shape[0]*s)), interpolation=cv2.INTER_AREA)
        vals.append(_lap_var(g, ksize))
    if agg == "median": return float(np.median(vals))
    if agg == "max":    return float(np.max(vals))
    if agg == "min":    return float(np.min(vals))
    return float(np.mean(vals))

def _tenengrad(gray: np.ndarray, ksize: int = 3) -> float:
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=ksize)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=ksize)
    return float(np.mean(gx*gx + gy*gy))

def _decide_threshold(scores: List[float], fixed: Optional[float], mode="percentile", param=20.0) -> float:
    if fixed is not None: return float(fixed)
    arr = np.asarray(scores, dtype=np.float64)
    if mode == "zscore":
        return float(arr.mean() - param * arr.std())
    # default: percentile
    return float(np.percentile(arr, param))

def detect_blur_paths(
    paths: List[Path],
    *,
    scales=(1.0, 0.5, 0.25),
    lap_ksize=3,
    agg="median",
    gauss_ksize=3,
    gauss_sigma=0.0,
    and_tenengrad=True,
    ten_ksize=3,
    threshold: Optional[float]=800.0,       # MS
    auto_th="percentile",
    auto_param=25.0,
    ten_threshold: Optional[float]=800.0,   # Tenengrad
    ten_auto_th="percentile",
    ten_auto_param=25.0,
    max_side: Optional[int]=2000,
    legacy: bool=False,                      # Trueなら旧ラプラシアン単独
) -> Tuple[List[Dict], Dict]:
    """画像パス一覧を評価して行ごとのdict配列とmeta(しきい値)を返す"""
    rows, ms_scores, ten_scores = [], [], []

    for p in paths:
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            rows.append({"path": str(p), "status": "read_error"}); continue

        # オプション縮小
        if max_side and max(img.shape[:2]) > max_side:
            s = max_side / float(max(img.shape[:2]))
            img = cv2.resize(img, (int(img.shape[1]*s), int(img.shape[0]*s)), interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gauss_ksize and gauss_ksize > 1:
            gray = _gaussian(gray, gauss_ksize, gauss_sigma)

        ms = _ms_lap_var(gray, scales=scales, ksize=lap_ksize, agg=agg)
        ten = _tenengrad(gray, ksize=ten_ksize)
        ms_scores.append(ms); ten_scores.append(ten)
        rows.append({"path": str(p), "score": ms, "ten_score": ten, "status": "ok"})

    if not ms_scores:  # 画像ゼロ or 全滅
        return rows, {"th_ms": 0.0, "th_ten": 0.0}

    # しきい値
    th_ms  = _decide_threshold(ms_scores,  threshold,  auto_th,      auto_param)
    th_ten = _decide_threshold(ten_scores, ten_threshold, ten_auto_th, ten_auto_param)

    # 最終判定
    for r in rows:
        if r["status"] != "ok":
            r["is_blur"] = None; r["rule"] = None; continue
        ms_ok = (r["score"] < th_ms)
        if legacy:
            r["is_blur"] = bool(ms_ok); r["rule"] = "MS_only"
        elif and_tenengrad:
            ten_ok = (r["ten_score"] < th_ten)
            r["is_blur"] = bool(ms_ok and ten_ok); r["rule"] = "AND(ms,ten)"
        else:
            r["is_blur"] = bool(ms_ok); r["rule"] = "MS_only"

    return rows, {"th_ms": th_ms, "th_ten": th_ten}
