from __future__ import annotations
import os, gc, threading, concurrent.futures as futures
from typing import Callable, Dict, Iterable, List, Tuple, Optional
from PIL import Image, ImageOps
import numpy as np

# --- 画像拡張子 ---
DEFAULT_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif")

# HEIF対応（入っていれば自動で有効）
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass

# ---------------------------
#    画像列挙
# ---------------------------
def list_image_files(root: str, exts: Iterable[str] = DEFAULT_EXTS) -> List[str]:
    exts_l = tuple(e.lower() for e in exts)
    out: List[str] = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(exts_l):
                out.append(os.path.join(dirpath, fn))
    return out

# ---------------------------
#    ブレ（ラプラシアン分散）
# ---------------------------
def _safe_open_gray_thumb(path: str, max_side: int = 640) -> Optional[Image.Image]:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("L")  # グレースケール
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            return im.copy()
    except Exception:
        return None

def _laplacian_variance(gray_img: Image.Image) -> float:
    # Laplacian（3x3近似）→ 分散
    a = np.asarray(gray_img, dtype=np.float32)
    # 0  1  0
    # 1 -4  1
    # 0  1  0
    k = np.array([[0,1,0],[1,-4,1],[0,1,0]], dtype=np.float32)
    # パディングして畳み込み（簡易実装）
    pad = np.pad(a, ((1,1),(1,1)), mode="reflect")
    # 畳み込み
    conv = (
        k[0,0]*pad[:-2, :-2] + k[0,1]*pad[:-2,1:-1] + k[0,2]*pad[:-2,2:] +
        k[1,0]*pad[1:-1, :-2] + k[1,1]*pad[1:-1,1:-1]+ k[1,2]*pad[1:-1,2:] +
        k[2,0]*pad[2:,  :-2] + k[2,1]*pad[2:, 1:-1] + k[2,2]*pad[2:,  2:]
    )
    v = float(np.var(conv))
    return v

def compute_blur_parallel(
    paths: List[str],
    max_workers: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_ev: Optional[threading.Event] = None,
    max_inflight: int = 64,
    batch_size: int = 512,
) -> Dict[str, float]:
    """
    画像パス一覧 → {path: laplacian_variance} を返す。
    - 未対応/壊れた画像は除外（戻り値に含めない）
    - 同時デコード数は max_inflight で制限
    - バッチごとにGC＆進捗更新
    """
    total = len(paths)
    if total == 0:
        return {}

    if max_workers is None:
        cw = os.cpu_count() or 4
        max_workers = max(1, min(8, cw // 2))  # 控えめ

    def _job(p: str) -> Tuple[str, Optional[float]]:
        if cancel_ev and cancel_ev.is_set():
            return (p, None)
        im = _safe_open_gray_thumb(p)
        if im is None:
            return (p, None)
        try:
            v = _laplacian_variance(im)
            return (p, v)
        except Exception:
            return (p, None)
        finally:
            del im

    out: Dict[str, float] = {}
    done = 0
    sem = threading.Semaphore(max_inflight)

    def _wrap(p: str):
        with sem:
            return _job(p)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i in range(0, total, batch_size):
            if cancel_ev and cancel_ev.is_set():
                break
            chunk = paths[i:i+batch_size]
            fs = [ex.submit(_wrap, p) for p in chunk]
            for f in futures.as_completed(fs):
                p, v = f.result()
                if v is not None:
                    out[p] = v
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            gc.collect()

    return out

# ---------------------------
#    pHash（既存：少し整理）
# ---------------------------
def _safe_open_thumb_for_hash(path: str, max_side: int = 256) -> Optional[Image.Image]:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("L")
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            return im.copy()
    except Exception:
        return None

def _phash64(gray_img: Image.Image) -> int:
    im_small = gray_img.resize((32, 32), Image.Resampling.LANCZOS)
    arr = np.asarray(im_small, dtype=np.float32)
    dct = np.abs(np.fft.fft2(arr))[:8, :8]
    med = np.median(dct)
    bits = (dct > med).astype(np.uint8).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return int(v & ((1 << 64) - 1))

def _hamming(a: int, b: int) -> int:
    return int((a ^ b).bit_count())

def compute_phash_parallel(
    paths: List[str],
    max_workers: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_ev: Optional[threading.Event] = None,
    max_inflight: int = 64,
    batch_size: int = 512,
) -> Dict[str, int]:
    total = len(paths)
    if total == 0:
        return {}

    if max_workers is None:
        cw = os.cpu_count() or 4
        max_workers = max(1, min(8, cw // 2))

    def _job(p: str) -> Tuple[str, Optional[int]]:
        if cancel_ev and cancel_ev.is_set():
            return (p, None)
        im = _safe_open_thumb_for_hash(p)
        if im is None:
            return (p, None)
        try:
            h = _phash64(im)
            return (p, h)
        except Exception:
            return (p, None)
        finally:
            del im

    out: Dict[str, int] = {}
    done = 0
    sem = threading.Semaphore(max_inflight)

    def _wrap(p: str):
        with sem:
            return _job(p)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i in range(0, total, batch_size):
            if cancel_ev and cancel_ev.is_set():
                break
            chunk = paths[i:i+batch_size]
            fs = [ex.submit(_wrap, p) for p in chunk]
            for f in futures.as_completed(fs):
                p, h = f.result()
                if h is not None:
                    out[p] = h
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            gc.collect()
    return out

# ---------------------------
#    類似探索（バケツ分割）
# ---------------------------
def build_similar_pairs_bktree(
    path_to_hash: Dict[str, int],
    radius: int = 6,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_ev: Optional[threading.Event] = None,
    bucket_bits: int = 12,
) -> List[Tuple[str, str, int]]:
    items = list(path_to_hash.items())
    total = len(items)
    if total == 0:
        return []

    buckets: Dict[int, List[Tuple[str, int]]] = {}
    mask = (1 << bucket_bits) - 1
    for p, h in items:
        key = (h >> (64 - bucket_bits)) & mask
        buckets.setdefault(key, []).append((p, h))

    pairs: List[Tuple[str, str, int]] = []
    processed = 0
    for _, arr in buckets.items():
        n = len(arr)
        for i in range(n):
            if cancel_ev and cancel_ev.is_set():
                return pairs
            p1, h1 = arr[i]
            for j in range(i+1, n):
                p2, h2 = arr[j]
                d = _hamming(h1, h2)
                if d <= radius:
                    pairs.append((p1, p2, d))
            processed += 1
            if progress_cb:
                progress_cb(processed, total)
        gc.collect()
    return pairs
