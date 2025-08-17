from __future__ import annotations
from pathlib import Path
from collections import defaultdict, deque
import csv
import os
import sqlite3
from typing import Dict, List, Tuple, Set

from PIL import Image
from .db import ensure_db, stat_tuple
from .hashers import file_hash, perceptual_hash
from .blur import blur_score
from .util import want_file

# CSV 仕様（visual=グループ判定、blur_single=単独ブレ判定）
HEADER = ["type","domain","group","keep","candidate","relation"]

# HEIC対応（入っていれば有効化）
try:
    from pillow_heif import register_heif
    register_heif()
except Exception:
    pass


def image_size(path: str) -> Tuple[int,int]:
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0,0)


def _get_blur(con: sqlite3.Connection, cache: Dict[str, float], p: str) -> float | None:
    if p in cache:
        return cache[p]
    row = con.execute("SELECT blur FROM cache WHERE path=?", (p,)).fetchone()
    blr = row[0] if row else None
    if blr is None:
        blr = blur_score(p)
        con.execute("UPDATE cache SET blur=? WHERE path=?", (blr, p))
    cache[p] = blr if blr is not None else -1.0
    return blr


def keeper_score(con: sqlite3.Connection, blur_cache: Dict[str,float], p: str):
    """大きいほど優先: シャープさ → 画素数 → ファイルサイズ → 更新日時"""
    w,h = image_size(p)
    mtime, size = stat_tuple(p)
    vol = _get_blur(con, blur_cache, p)  # VoL（大きいほどシャープ）
    # None対策で -1
    return (vol if vol is not None else -1.0, w*h, size, mtime, p)


def pick_keeper(con: sqlite3.Connection, blur_cache: Dict[str,float], paths: List[str]) -> str:
    return sorted(paths, key=lambda p: keeper_score(con, blur_cache, p), reverse=True)[0]


def scan(target_dir: str, report_csv="report.csv", dbpath=".imgclean.db",
         blur_threshold: float = 120.0, do_similar: bool=False, phash_distance:int=6,
         include_exts=None, exclude_substr: List[str] | None = None) -> int:
    """
    - visual（duplicate+similarを統合）: グループのkeeper以外を候補に出力
    - blur_single: VoLしきい値未満を単独で出力（visualと独立）
    """
    con = ensure_db(dbpath)
    cur = con.cursor()
    cur.execute("PRAGMA synchronous=NORMAL;")

    root = Path(target_dir)
    files: List[Path] = []
    for p in root.rglob("*"):
        if isinstance(exclude_substr, list) and any(s.lower() in str(p).lower() for s in exclude_substr):
            continue
        if want_file(p, include_exts):
            files.append(p)

    # 1) キャッシュ更新（shaだけ先に埋める）
    for p in files:
        mtime, size = stat_tuple(str(p))
        row = cur.execute("SELECT mtime,size,sha FROM cache WHERE path=?", (str(p),)).fetchone()
        if not (row and row[0]==mtime and row[1]==size and row[2]):
            sha = file_hash(str(p))
            cur.execute("REPLACE INTO cache(path,mtime,size,sha,phash,blur) VALUES(?,?,?,?,?,?)",
                        (str(p), mtime, size, sha, None, None))

    # 2) duplicate（完全一致）で visual グループ化
    dup_groups: Dict[str, List[str]] = defaultdict(list)
    for p in files:
        sha = cur.execute("SELECT sha FROM cache WHERE path=?", (str(p),)).fetchone()[0]
        if sha:
            dup_groups[sha].append(str(p))

    rows: List[List[str]] = []
    blur_cache: Dict[str,float] = {}

    # 候補ごとのベストvisual判定（duplicate優先>similar）
    # cand -> (priority, group, keep, relation)
    # priority: duplicate=1, similar=2
    best_visual: Dict[str, Tuple[int, str, str, str]] = {}

    for sha, group in dup_groups.items():
        if len(group) < 2:
            continue
        keep = pick_keeper(con, blur_cache, group)
        for g in group:
            if g == keep:
                continue
            # duplicate は priority 1
            if (g not in best_visual) or (best_visual[g][0] > 1):
                best_visual[g] = (1, sha, keep, "same_sha")

    # 3) similar（pHash距離閾値で連結成分クラスタリング）
    if do_similar:
        # 3-1) pHash を埋める
        ph_vals: Dict[str,str] = {}
        for p in files:
            phs = cur.execute("SELECT phash FROM cache WHERE path=?", (str(p),)).fetchone()[0]
            if phs is None:
                phs = perceptual_hash(str(p))
                cur.execute("UPDATE cache SET phash=? WHERE path=?", (phs, str(p)))
            if phs:
                ph_vals[str(p)] = phs

        # 3-2) 近傍グラフを作る（O(N^2) 注意：大量枚数では重い→将来最適化）
        # keys はファイルパス、辺には距離d<=phash_distance
        import imagehash
        nodes = list(ph_vals.keys())
        graph: Dict[str, List[str]] = defaultdict(list)
        for i in range(len(nodes)):
            h1 = imagehash.hex_to_hash(ph_vals[nodes[i]])
            for j in range(i+1, len(nodes)):
                h2 = imagehash.hex_to_hash(ph_vals[nodes[j]])
                d = h1 - h2
                if d <= phash_distance:
                    a, b = nodes[i], nodes[j]
                    graph[a].append(b); graph[b].append(a)

        # 3-3) 連結成分ごとに keeper を選び、非keeperを similar として記録
        visited: Set[str] = set()
        gid = 0
        for n in nodes:
            if n in visited:
                continue
            # BFS
            comp = []
            q = deque([n]); visited.add(n)
            while q:
                u = q.popleft(); comp.append(u)
                for v in graph.get(u, []):
                    if v not in visited:
                        visited.add(v); q.append(v)
            if len(comp) < 2:
                continue
            gid += 1
            keep = pick_keeper(con, blur_cache, comp)
            group_id = f"phash#{gid}"
            for f in comp:
                if f == keep:
                    continue
                # duplicate判定が既にある候補は duplicate を優先（priority 1）
                if (f not in best_visual):
                    best_visual[f] = (2, group_id, keep, f"phash_d<={phash_distance}")

    # 4) best_visual を CSV 行へ（visual / group）
    for cand, (_prio, group_id, keep, reason) in best_visual.items():
        rows.append(["visual", "group", group_id, keep, cand, reason])

    # 5) blur 単独（visualと独立に常に出す）
    for p in files:
        blr = _get_blur(con, blur_cache, str(p))
        if blr is not None and blr < blur_threshold:
            rows.append(["blur_single", "single", "", "", str(p), f"lap_var={blr:.1f}"])

    with open(report_csv, "w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(HEADER)
        w.writerows(rows)

    con.commit()
    con.close()
    return len(rows)
