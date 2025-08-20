# blur_cleaner/fast_scan.py  — 安定化版（バッチ処理＋同時実行制限＋軽量バケツ探索）
from __future__ import annotations
import os, math, gc, threading, concurrent.futures as futures
from typing import Callable, Dict, Iterable, List, Tuple, Optional
from PIL import Image, ImageOps
import numpy as np

try:
    import pillow_heif  # HEIFが入っていれば有効化
    pillow_heif.register_heif_opener()
except Exception:
    pass

# ---------- 基本ユーティリ ----------
def _safe_open_thumb(path: str, max_side: int = 256) -> Optional[Image.Image]:
    """Pillowで安全に開いて縮小。戻り値はクローズ不要（copy済み）。失敗は None。"""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("L")  # グレースケール化（pHash用途）
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            return im.copy()
    except Exception:
        return None

def _phash(im: Image.Image) -> int:
    """pHash（64bit）。画像は既にグレースケール＆縮小前提。"""
    # 32x32にリサイズ→DCT→上位左8x8の低周波成分→中央値でビット化
    im_small = im.resize((32, 32), Image.Resampling.LANCZOS)
    arr = np.asarray(im_small, dtype=np.float32)
    # 雑なDCT: FFT経由の近似でも十分
    dct = np.fft.fft2(arr)
    dct = np.abs(dct[:8, :8])
    med = np.median(dct)
    bits = (dct > med).astype(np.uint8).flatten()
    # 64bitへ
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return int(v & ((1 << 64) - 1))

def _hamming(a: int, b: int) -> int:
    return int((a ^ b).bit_count())

# ---------- pHash計算（安定化） ----------
def compute_phash_parallel(
    paths: List[str],
    max_workers: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_ev: Optional[threading.Event] = None,
    max_inflight: int = 64,
    batch_size: int = 512,
) -> Dict[str, int]:
    """
    画像パス一覧 → {path: phash(64bit)} を返す。
    - 同時デコードは max_inflight で制限
    - バッチごとにGC＆進捗報告
    """
    total = len(paths)
    if total == 0:
        return {}
    if max_workers is None:
        cw = os.cpu_count() or 4
        max_workers = max(1, min(8, cw // 2))  # 控えめ

    # 実際に投げる仕事
    def _job(p: str) -> Tuple[str, Optional[int]]:
        if cancel_ev and cancel_ev.is_set():
            return (p, None)
        im = _safe_open_thumb(p)
        if im is None:
            return (p, None)
        try:
            h = _phash(im)
            return (p, h)
        except Exception:
            return (p, None)
        finally:
            # 明示解放
            del im

    out: Dict[str, int] = {}
    done = 0

    # セマフォで同時フライ数を制限
    sem = threading.Semaphore(max_inflight)

    def _wrap(p: str):
        with sem:
            return _job(p)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        # バッチに分割して投入
        for i in range(0, total, batch_size):
            if cancel_ev and cancel_ev.is_set():
                break
            chunk = paths[i:i + batch_size]
            fs = [ex.submit(_wrap, p) for p in chunk]
            for f in futures.as_completed(fs):
                p, h = f.result()
                if h is not None:
                    out[p] = h
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            # バッチ終わりでクリーンアップ
            gc.collect()

    return out

# ---------- 類似探索（バケツ分割） ----------
def build_similar_pairs_bktree(
    path_to_hash: Dict[str, int],
    radius: int = 6,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_ev: Optional[threading.Event] = None,
    bucket_bits: int = 12,
) -> List[Tuple[str, str, int]]:
    """
    上位 bucket_bits でバケツに分け、同バケツ内だけハミング距離を計算。
    radius 以下のペアを返す。
    """
    items = list(path_to_hash.items())
    total = len(items)
    if total == 0:
        return []

    # バケツ化
    buckets: Dict[int, List[Tuple[str, int]]] = {}
    mask = (1 << bucket_bits) - 1
    for p, h in items:
        key = (h >> (64 - bucket_bits)) & mask
        buckets.setdefault(key, []).append((p, h))

    # 各バケツ内で距離計算（O(n^2)だがnは小さくなる想定）
    pairs: List[Tuple[str, str, int]] = []
    processed = 0
    for key, arr in buckets.items():
        n = len(arr)
        for i in range(n):
            if cancel_ev and cancel_ev.is_set():
                return pairs
            p1, h1 = arr[i]
            for j in range(i + 1, n):
                p2, h2 = arr[j]
                d = _hamming(h1, h2)
                if d <= radius:
                    pairs.append((p1, p2, d))
            processed += 1
            if progress_cb:
                progress_cb(processed, total)
        # バケツごとにGC
        gc.collect()

    return pairs
