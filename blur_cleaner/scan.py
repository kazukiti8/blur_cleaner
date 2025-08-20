# -*- coding: utf-8 -*-
"""
blur_cleaner.scan
画像のブレ（ボケ）判定＋（任意）類似画像チェックを行うスキャナ。

・progress_cb(phase, cur, tot) に対応（GUIの進捗バー更新用）
・高速化：
  - ラプラシアン分散をベクトル化（Python二重forループ排除）
  - 長辺を最大1024pxへリサイズしてから指標計算
  - 進捗コールを間引き（UI更新の負荷軽減）
・外部依存：Pillow, NumPy（OpenCVがあれば自動利用して更に高速）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from collections import defaultdict
import csv
import math

import numpy as np
from PIL import Image

# OpenCVがあれば使う（なくてもOK）
try:
    import cv2  # type: ignore
    _HAVE_OPENCV = True
except Exception:
    _HAVE_OPENCV = False


# ----------------------------
# 型・定数
# ----------------------------

ProgressCb = Callable[[str, int, int], None]
# phase 例: "precount" | "scan" | "similar" | "done"

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp"}


@dataclass
class ScanRow:
    path: str
    blur_value: float
    is_blur: bool
    phash: Optional[int] = None


# ----------------------------
# ユーティリティ
# ----------------------------

def _to_gray_np(img: Image.Image) -> np.ndarray:
    """PIL Image -> グレースケールnumpy.float32"""
    if img.mode != "L":
        img = img.convert("L")
    return np.asarray(img, dtype=np.float32)


def _prep_for_blur(im: Image.Image, max_side: int = 1024) -> np.ndarray:
    """
    ブレ判定前処理：長辺max_sideへ縮小（速度↑、精度は概ね維持）
    """
    w, h = im.size
    s = max(w, h)
    if s > max_side:
        scale = max_side / float(s)
        im = im.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    return _to_gray_np(im)


def _laplacian_var(gray: np.ndarray) -> float:
    """
    ラプラシアン分散（ボケ度指標）。OpenCVがあれば使用、なければNumPyでベクトル化。
    値が小さいほどボケ。
    """
    if gray.size == 0 or gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0

    if _HAVE_OPENCV:
        # OpenCVの方が速い
        lap = cv2.Laplacian(gray, ddepth=cv2.CV_32F, ksize=3)
        return float(lap.var())

    # NumPy版（上下左右の和 - 4*中心） → 分散
    c = gray[1:-1, 1:-1]
    up = gray[:-2, 1:-1]
    down = gray[2:, 1:-1]
    left = gray[1:-1, :-2]
    right = gray[1:-1, 2:]
    lap = (up + down + left + right) - 4.0 * c
    return float(np.var(lap))


def _dct2(a: np.ndarray) -> np.ndarray:
    """
    簡易2D-DCT（依存を増やさないための小実装）
    入力は32x32程度を想定。十分速いが、必要ならscipyへ置換可。
    """
    N, M = a.shape
    result = np.zeros((N, M), dtype=np.float32)
    # 事前にcos値をテーブル化して少し高速化
    cos_u = np.array([[math.cos((2*x + 1) * u * math.pi / (2 * N)) for x in range(N)] for u in range(N)], dtype=np.float32)
    cos_v = np.array([[math.cos((2*y + 1) * v * math.pi / (2 * M)) for y in range(M)] for v in range(M)], dtype=np.float32)

    for u in range(N):
        cu = math.sqrt(1.0 / N) if u == 0 else math.sqrt(2.0 / N)
        for v in range(M):
            cv = math.sqrt(1.0 / M) if v == 0 else math.sqrt(2.0 / M)
            # (u, v)成分 = cu*cv * Σ_x Σ_y a[x,y]*cos_u[u,x]*cos_v[v,y]
            s = (a * cos_u[u][:, None] * cos_v[v][None, :]).sum()
            result[u, v] = cu * cv * s
    return result


def _phash(img: Image.Image, hash_size: int = 8) -> int:
    """
    pHash: 32x32→DCT→低周波8x8→中央値で二値→64bit整数化
    """
    im = img.convert("L").resize((32, 32), Image.BILINEAR)
    a = np.asarray(im, dtype=np.float32)
    d = _dct2(a)
    d_low = d[:hash_size, :hash_size]
    med = np.median(d_low[1:, 1:])  # DC除く
    bits = (d_low >= med).astype(np.uint8)
    val = 0
    for b in bits.flatten():
        val = (val << 1) | int(b)
    return val


def _hamming(a: int, b: int) -> int:
    return int(bin(a ^ b).count("1"))


def _iter_files(root: Path,
                include_exts: Optional[Sequence[str]],
                exclude_substr: Optional[Sequence[str]]) -> Iterable[Path]:
    exts = {e.lower() for e in (include_exts or SUPPORTED_EXTS)}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        if exclude_substr and any(s in p.as_posix() for s in exclude_substr):
            continue
        yield p


# ----------------------------
# メイン
# ----------------------------

def scan(
    target_dir: str,
    report_csv: Optional[str] = None,
    dbpath: Optional[str] = None,            # 互換のため残置（未使用）
    blur_threshold: float = 80.0,
    do_similar: bool = False,
    phash_distance: int = 10,
    include_exts: Optional[Sequence[str]] = None,
    exclude_substr: Optional[Sequence[str]] = None,
    collect_stats: bool = False,
    *,
    progress_cb: Optional[ProgressCb] = None,  # ★追加：進捗コールバック
    progress_stride: int = 5,                  # 何件ごとに進捗通知するか（UI負荷軽減）
    resize_max_side: int = 1024,               # ブレ判定前の最大長辺
) -> Tuple[List[ScanRow], Dict[str, float]]:
    """
    画像群を走査し、ブレ指標（ラプラシアン分散）と、任意でpHash類似も計算。

    Returns:
        rows: 画像ごとの結果リスト
        stats: ざっくり統計（collect_stats=True のとき）
    """
    root = Path(target_dir)
    files = list(_iter_files(root, include_exts, exclude_substr))
    total = len(files)

    if progress_cb:
        progress_cb("precount", 0, total)

    rows: List[ScanRow] = []

    # --- ブレ判定 ---
    for idx, f in enumerate(files, start=1):
        try:
            with Image.open(f) as im:
                gray = _prep_for_blur(im, max_side=resize_max_side)
                blur_val = _laplacian_var(gray)
                is_blur = blur_val < float(blur_threshold)
                rows.append(ScanRow(path=str(f), blur_value=blur_val, is_blur=is_blur))
        except Exception:
            # 壊れた画像等はスキップ（必要に応じてログ追加）
            continue

        if progress_cb and (idx % max(1, progress_stride) == 0 or idx == total):
            progress_cb("scan", idx, total)

    # --- 類似判定（任意） ---
    if do_similar and rows:
        # pHash計算
        for i, row in enumerate(rows, start=1):
            try:
                with Image.open(row.path) as im:
                    row.phash = _phash(im)
            except Exception:
                row.phash = None
            if progress_cb and (i % max(1, progress_stride) == 0 or i == len(rows)):
                progress_cb("similar", i, len(rows))

        # 簡易グルーピング（返り値には含めないが、距離計算の負荷は軽い）
        n = len(rows)
        phs = [r.phash for r in rows]
        for i in range(n):
            pi = phs[i]
            if pi is None:
                continue
            for j in range(i + 1, n):
                pj = phs[j]
                if pj is None:
                    continue
                # 閾値以下なら同グループ候補（必要なら拡張して返却）
                if _hamming(pi, pj) <= phash_distance:
                    pass  # ここで何かしたければ実装

    # --- 統計 ---
    stats: Dict[str, float] = {}
    if collect_stats:
        if rows:
            vals = np.array([r.blur_value for r in rows], dtype=np.float32)
            stats = {
                "count": float(len(rows)),
                "mean_blur": float(np.mean(vals)),
                "min_blur": float(np.min(vals)),
                "max_blur": float(np.max(vals)),
            }
        else:
            stats = {"count": 0.0, "mean_blur": 0.0, "min_blur": 0.0, "max_blur": 0.0}

    # --- CSV出力（必要なとき） ---
    if report_csv:
        with open(report_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path", "blur_value", "is_blur", "phash"])
            for r in rows:
                w.writerow([r.path, f"{r.blur_value:.6f}", int(r.is_blur),
                            r.phash if r.phash is not None else ""])

    if progress_cb:
        progress_cb("done", total, total)

    return rows, stats
