# -*- coding: utf-8 -*-
from __future__ import annotations
import os, math, itertools, concurrent.futures as fut
from typing import Callable, Dict, Iterable, List, Optional, Tuple
import numpy as np

try:
    import cv2
except Exception as _e:
    cv2 = None  # GUIでメッセージを出す側に任せる

DEFAULT_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

# ---------- ファイル列挙 ----------
def list_image_files(root: str, exts: Iterable[str] = DEFAULT_EXTS) -> List[str]:
    out: List[str] = []
    exts_low = tuple(x.lower() for x in exts)
    for d, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(exts_low):
                out.append(os.path.join(d, fn))
    return out

# ---------- Laplacian Variance（ブレ指標） ----------
def _laplacian_var(p: str) -> Optional[float]:
    if cv2 is None: return None
    try:
        buf = np.fromfile(p, dtype=np.uint8)
        im = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if im is None: return None
        im = cv2.GaussianBlur(im, (3,3), 0)
        v = cv2.Laplacian(im, cv2.CV_64F).var()
        return float(v)
    except Exception:
        return None

def compute_blur_parallel(paths: List[str], pool=None,
                          progress_cb: Optional[Callable[[int,int],None]] = None,
                          cancel_ev=None) -> Dict[str, float]:
    out: Dict[str, float] = {}
    total = len(paths)
    done = 0
    with fut.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for p, v in ex.map(lambda pp: (pp, _laplacian_var(pp)), paths):
            done += 1
            if progress_cb: progress_cb(done, total)
            if cancel_ev and cancel_ev.is_set(): break
            if v is not None:
                out[p] = v
    return out

# ---------- pHash ----------
def _phash_int(p: str) -> Optional[int]:
    if cv2 is None: return None
    try:
        buf = np.fromfile(p, dtype=np.uint8)
        im = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if im is None: return None
        im = cv2.resize(im, (32, 32), interpolation=cv2.INTER_AREA)
        im = np.float32(im)
        dct = cv2.dct(im)
        dct8 = dct[:8, :8]
        dct8[0,0] = 0.0
        med = np.median(dct8)
        bits = (dct8 > med).astype(np.uint8).flatten()
        val = 0
        for i, b in enumerate(bits):
            if b: val |= (1 << i)
        return int(val) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        return None

def compute_phash_parallel(paths: List[str], pool=None,
                           progress_cb: Optional[Callable[[int,int],None]] = None,
                           cancel_ev=None) -> Dict[str, int]:
    out: Dict[str, int] = {}
    total, done = len(paths), 0
    with fut.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for p, v in ex.map(lambda pp: (pp, _phash_int(pp)), paths):
            done += 1
            if progress_cb: progress_cb(done, total)
            if cancel_ev and cancel_ev.is_set(): break
            if v is not None:
                out[p] = v
    return out

# ---------- dHash（追加） ----------
def _dhash_int(p: str) -> Optional[int]:
    if cv2 is None: return None
    try:
        buf = np.fromfile(p, dtype=np.uint8)
        im = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if im is None: return None
        im = cv2.resize(im, (9, 8), interpolation=cv2.INTER_AREA)
        diff = im[:, 1:] > im[:, :-1]
        bits = diff.flatten()
        val = 0
        for i, b in enumerate(bits):
            if b: val |= (1 << i)
        return int(val) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        return None

def compute_dhash_parallel(paths: List[str], pool=None,
                           progress_cb: Optional[Callable[[int,int],None]] = None,
                           cancel_ev=None) -> Dict[str, int]:
    out: Dict[str, int] = {}
    total, done = len(paths), 0
    with fut.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for p, v in ex.map(lambda pp: (pp, _dhash_int(pp)), paths):
            done += 1
            if progress_cb: progress_cb(done, total)
            if cancel_ev and cancel_ev.is_set(): break
            if v is not None:
                out[p] = v
    return out

# ---------- Hamming距離 ----------
def _hamming64(a: int, b: int) -> int:
    return int((a ^ b).bit_count())

# ---------- 類似（素直な O(n^2) 版） ----------
def build_similar_pairs_bktree(hash_map: Dict[str, int],
                               radius: int = 6,
                               progress_cb: Optional[Callable[[int,int],None]] = None,
                               cancel_ev=None) -> List[Tuple[str,str,int]]:
    # NOTE: 数百〜数千枚想定なら O(n^2) でも現実的。BK-tree未実装環境でも動くようにする。
    items = list(hash_map.items())
    n = len(items)
    pairs: List[Tuple[str,str,int]] = []
    total = n
    for i in range(n):
        if cancel_ev and cancel_ev.is_set(): break
        pa, ha = items[i]
        for j in range(i+1, n):
            pb, hb = items[j]
            d = _hamming64(ha, hb)
            if d <= radius:
                pairs.append((pa, pb, d))
        if progress_cb: progress_cb(i+1, total)
    return pairs

# ---------- ハイブリッド（pHash + dHash） ----------
def build_similar_pairs_bktree_hybrid(phash_map: Dict[str, int],
                                      dhash_map: Dict[str, int],
                                      radius_p: int = 6, radius_d: int = 8,
                                      progress_cb: Optional[Callable[[int,int],None]] = None,
                                      cancel_ev=None) -> List[Tuple[str,str,int]]:
    cand_p = build_similar_pairs_bktree(phash_map, radius=radius_p,
                                        progress_cb=progress_cb, cancel_ev=cancel_ev) if phash_map else []
    cand_d = build_similar_pairs_bktree(dhash_map, radius=radius_d,
                                        progress_cb=progress_cb, cancel_ev=cancel_ev) if dhash_map else []
    # 和集合＋再スコアリング
    tmp = {}
    for a, b, dp in cand_p:
        k = (a, b) if a < b else (b, a)
        tmp[k] = [dp, None]
    for a, b, dd in cand_d:
        k = (a, b) if a < b else (b, a)
        if k in tmp:
            tmp[k][1] = dd
        else:
            tmp[k] = [None, dd]
    pairs: List[Tuple[str,str,int]] = []
    for (a, b), (dp, dd) in tmp.items():
        dp = radius_p if dp is None else dp
        dd = radius_d if dd is None else dd
        score = 0.7 * (dp / max(1, radius_p)) + 0.3 * (dd / max(1, radius_d))
        pairs.append((a, b, int(round(100 * score))))  # 0〜100の擬似距離
    pairs.sort(key=lambda t: t[2])
    return pairs

# ---------- SSIM（任意の最終フィルタ） ----------
def _ssim_gray(img1: np.ndarray, img2: np.ndarray) -> float:
    # Wang+04 の簡易実装
    C1, C2 = (0.01*255)**2, (0.03*255)**2
    import cv2 as _cv2
    x, y = img1.astype(np.float32), img2.astype(np.float32)
    mu1 = _cv2.GaussianBlur(x, (11,11), 1.5); mu2 = _cv2.GaussianBlur(y, (11,11), 1.5)
    mu1_sq, mu2_sq, mu12 = mu1*mu1, mu2*mu2, mu1*mu2
    sigma1_sq = _cv2.GaussianBlur(x*x, (11,11), 1.5) - mu1_sq
    sigma2_sq = _cv2.GaussianBlur(y*y, (11,11), 1.5) - mu2_sq
    sigma12   = _cv2.GaussianBlur(x*y, (11,11), 1.5) - mu12
    ssim = ((2*mu12 + C1) * (2*sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2) + 1e-6)
    return float(ssim.mean())

def ssim_filter_pairs(pairs: List[Tuple[str,str,int]],
                      max_pairs: int = 100, thresh: float = 0.82) -> List[Tuple[str,str,int]]:
    if cv2 is None: return pairs
    out: List[Tuple[str,str,int]] = []
    for a, b, d in pairs[:max_pairs]:
        try:
            ia = cv2.imdecode(np.fromfile(a, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            ib = cv2.imdecode(np.fromfile(b, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if ia is None or ib is None: continue
            def _resize(g):
                h, w = g.shape
                s = 256.0 / min(h, w)
                s = min(s, 3.0)
                return cv2.resize(g, (int(w*s), int(h*s)), interpolation=cv2.INTER_AREA)
            ssim = _ssim_gray(_resize(ia), _resize(ib))
            if ssim >= thresh:
                out.append((a, b, d))
        except Exception:
            pass
    return out
