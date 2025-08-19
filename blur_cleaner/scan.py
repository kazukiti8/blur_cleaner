# scan.py - 画像走査（CSVなし運用・統計オプション対応）
from __future__ import annotations
import os, csv, sqlite3, hashlib, math
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from PIL import Image
import numpy as np

# -------- 設定 --------
DEFAULT_EXTS = [".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic", ".heif"]

# -------- ユーティリティ --------
def _norm_exts(exts: Optional[List[str]]) -> List[str]:
    if not exts:
        return DEFAULT_EXTS
    out = []
    for e in exts:
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.append(e)
    return out

def _match_filters(path: str, include_exts: Optional[List[str]], exclude_substr: Optional[List[str]]) -> bool:
    p = path.lower()
    if include_exts:
        if not any(p.endswith(e) for e in include_exts):
            return False
    if exclude_substr:
        for s in exclude_substr:
            if s and (s.lower() in p):
                return False
    return True

def _file_sig(path: str) -> Tuple[int, float]:
    st = os.stat(path)
    return (int(st.st_size), float(st.st_mtime))

# -------- 画像特徴 --------
def _load_gray_small(path: str, size: int = 256) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("L")
        im.thumbnail((size, size))
        arr = np.asarray(im, dtype=np.float32)
    return arr

def _lap_var(gray: np.ndarray) -> float:
    """
    Laplacian分散（OpenCV無し版）。値が小さいほどブレが強い。
    カーネル [[0,1,0],[1,-4,1],[0,1,0]]
    """
    g = np.pad(gray, 1, mode="edge")
    c = (g[0:-2,1:-1] + g[2:,1:-1] + g[1:-1,0:-2] + g[1:-1,2:] - 4.0*g[1:-1,1:-1])
    return float(np.var(c))

def _dct_matrix(n: int) -> np.ndarray:
    x, y = np.meshgrid(np.arange(n), np.arange(n))
    mat = np.cos((np.pi * (2*x + 1) * y) / (2 * n)).astype(np.float64)
    mat[0, :] = mat[0, :] / math.sqrt(2)
    mat *= math.sqrt(2 / n)
    return mat

_DCT32 = _dct_matrix(32)

def _phash64(path: str) -> int:
    """
    pHash(64bit): 32x32→DCT→左上8x8の中央値で符号化（DC除外）。
    """
    with Image.open(path) as im:
        im = im.convert("L").resize((32, 32), Image.BILINEAR)
        a = np.asarray(im, dtype=np.float64)
    T = _DCT32
    dct = T @ a @ T.T
    cut = dct[:8, :8].copy()
    sub = cut.flatten()[1:]  # DC除外
    med = np.median(sub)
    bits = (cut > med).astype(np.uint8)
    val = 0
    for b in bits.flatten():
        val = (val << 1) | int(b)
    return int(val)

def _hamdist64(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# -------- キャッシュ（sqlite） --------
def _db_init(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cache(
        path TEXT PRIMARY KEY,
        size INTEGER,
        mtime REAL,
        phash INTEGER,
        lap REAL,
        sha1 TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sig ON cache(size, mtime)")
    conn.commit()

def _db_get(conn: sqlite3.Connection, path: str, size: int, mtime: float):
    cur = conn.cursor()
    cur.execute("SELECT phash, lap, sha1 FROM cache WHERE path=? AND size=? AND mtime=?", (path, size, mtime))
    return cur.fetchone()

def _db_put(conn: sqlite3.Connection, path: str, size: int, mtime: float, phash: Optional[int], lap: Optional[float], sha1: Optional[str]):
    cur = conn.cursor()
    cur.execute("REPLACE INTO cache(path,size,mtime,phash,lap,sha1) VALUES(?,?,?,?,?,?)",
                (path, size, mtime, phash, lap, sha1))
    conn.commit()

# -------- 本体 --------
def scan(
    target_dir: str,
    report_csv: Optional[str] = None,   # None ならCSV書き出ししない
    dbpath: str = ".imgclean.db",
    blur_threshold: float = 120.0,
    do_similar: bool = False,
    phash_distance: int = 6,
    include_exts: Optional[List[str]] = None,
    exclude_substr: Optional[List[str]] = None,
    collect_stats: bool = False,        # Trueで (rows, stats) を返す
) -> Union[List[Dict[str,str]], Tuple[List[Dict[str,str]], Dict[str,object]]]:
    """
    画像を走査し、判定行（dict）を返す。
    行の形式：
      - ブレ単独: {"type":"blur_single","domain":"single","group":"","keep":"","candidate":<path>,"relation":"lap_var=xx.x"}
      - 類似(重複): {"type":"visual","domain":"group","group":<gid>,"keep":<keep>,"candidate":<cand>,"relation":"dist=d; lap_keep=...; lap_cand=..."}
    """
    target_dir = os.path.abspath(target_dir)
    inc = _norm_exts(include_exts)
    exc = exclude_substr or []

    # ファイル列挙
    files: List[str] = []
    for root, _dirs, fnames in os.walk(target_dir):
        for fn in fnames:
            p = os.path.join(root, fn)
            if _match_filters(p, inc, exc):
                files.append(os.path.abspath(p))
    files.sort()

    # DB
    conn = sqlite3.connect(dbpath)
    _db_init(conn)

    # 特徴量
    metas: Dict[str, Dict] = {}
    for path in files:
        try:
            size, mtime = _file_sig(path)
            row = _db_get(conn, path, size, mtime)
            if row is not None:
                ph, lv, sh = row
                metas[path] = {"size": size, "mtime": mtime, "phash": ph, "lap": lv, "sha1": sh}
                continue

            gray = _load_gray_small(path, size=256)
            lap = _lap_var(gray)
            ph = _phash64(path) if do_similar else None
            sh = _sha1(path)  # 完全重複用
            metas[path] = {"size": size, "mtime": mtime, "phash": ph, "lap": lap, "sha1": sh}
            _db_put(conn, path, size, mtime, ph, lap, sh)
        except Exception:
            # 読めない/壊れた画像はスキップ
            continue

    # ---- ブレ単独 ----
    rows: List[Dict[str, str]] = []
    lap_samples: List[Tuple[str, float]] = []  # 統計用
    for path, m in metas.items():
        lap = m.get("lap") or 0.0
        lap_samples.append((path, lap))
        if lap < blur_threshold:
            rows.append({
                "type": "blur_single",
                "domain": "single",
                "group": "",
                "keep": "",
                "candidate": path,
                "relation": f"lap_var={lap:.1f}"
            })

    # ---- 類似（重複） ----
    if do_similar:
        # 完全重複（sha1一致）
        dup_groups: Dict[str, List[str]] = {}
        for path, m in metas.items():
            sh = m.get("sha1")
            if not sh:
                continue
            dup_groups.setdefault(sh, []).append(path)

        # Union-Find
        parent = {p: p for p in metas.keys()}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # 完全重複は強制結合
        for members in dup_groups.values():
            if len(members) > 1:
                base = members[0]
                for q in members[1:]:
                    union(base, q)

        # pHash距離で結合（上位16bitバケットで粗い候補抽出）
        items = [(p, int(m["phash"])) for p, m in metas.items() if m.get("phash") is not None]
        buckets: Dict[int, List[Tuple[str, int]]] = {}
        for p, h in items:
            key = (h >> 48) & 0xFFFF
            buckets.setdefault(key, []).append((p, h))
        for bucket in buckets.values():
            n = len(bucket)
            for i in range(n):
                p_i, h_i = bucket[i]
                for j in range(i+1, n):
                    p_j, h_j = bucket[j]
                    if _hamdist64(h_i, h_j) <= phash_distance:
                        union(p_i, p_j)

        # グループ収集
        groups: Dict[str, List[str]] = {}
        for p in metas.keys():
            r = find(p)
            groups.setdefault(r, []).append(p)

        # 各グループ：lap最大をkeep、そのほかをcandidateとして行を追加
        gid_counter = 1
        for members in groups.values():
            if len(members) <= 1:
                continue
            members.sort(key=lambda x: metas[x].get("lap") or 0.0, reverse=True)
            keep = members[0]
            keep_lap = metas[keep].get("lap") or 0.0
            keep_h = metas[keep].get("phash")
            gid = f"grp{gid_counter:06d}"
            gid_counter += 1
            for cand in members[1:]:
                cand_lap = metas[cand].get("lap") or 0.0
                dist = ""
                h = metas[cand].get("phash")
                if keep_h is not None and h is not None:
                    dist = str(_hamdist64(int(keep_h), int(h)))
                if dist:
                    rel = f"dist={dist}; lap_keep={keep_lap:.1f}; lap_cand={cand_lap:.1f}"
                else:
                    rel = f"lap_keep={keep_lap:.1f}; lap_cand={cand_lap:.1f}"
                rows.append({
                    "type": "visual",
                    "domain": "group",
                    "group": gid,
                    "keep": keep,
                    "candidate": cand,
                    "relation": rel
                })

    # ---- CSV書き出し（任意）----
    if report_csv:
        with open(report_csv, "w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=["type","domain","group","keep","candidate","relation"])
            w.writeheader()
            w.writerows(rows)

    if not collect_stats:
        return rows

    # ---- 統計 ----
    if lap_samples:
        laps = sorted(lv for _p, lv in lap_samples)
        n = len(laps)
        def pct(p: float):
            i = min(max(int(round(p*(n-1))), 0), n-1)
            return laps[i]
        stats: Dict[str, object] = {
            "files_total": len(lap_samples),
            "lap_min": laps[0],
            "lap_median": pct(0.5),
            "lap_p95": pct(0.95),
            "lap_max": laps[-1],
            # 追加のパーセンタイル
            "lap_p05": pct(0.05),
            "lap_p10": pct(0.10),
            "lap_p15": pct(0.15),
            "lap_p20": pct(0.20),
            "lap_p25": pct(0.25),
            "lap_p30": pct(0.30),
            "lap_p40": pct(0.40),
            "lowest": sorted(lap_samples, key=lambda t: t[1])[:20],
        }
    else:
        stats = {"files_total": 0, "lowest": []}

    return rows, stats
