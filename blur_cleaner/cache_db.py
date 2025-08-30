# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sqlite3, threading, time
from typing import Dict, List, Optional, Tuple

_DB_NAME = ".blur_cleaner_cache.sqlite"

def open_cache_at(target_dir: str) -> "CacheDB":
    os.makedirs(target_dir, exist_ok=True)
    return CacheDB(os.path.join(target_dir, _DB_NAME))

class CacheDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_tables()
        self._ensure_dhash_column()  # 新列を自動追加
        self._session_ts: Optional[int] = None

    def _ensure_tables(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                  path TEXT PRIMARY KEY,
                  mtime INTEGER NOT NULL DEFAULT 0,
                  size  INTEGER NOT NULL DEFAULT 0,
                  blur  REAL,
                  phash TEXT,
                  last_seen INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                )
                """
            )

    def _ensure_dhash_column(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(files)")
        cols = [r[1] for r in cur.fetchall()]
        if "dhash" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE files ADD COLUMN dhash TEXT")

    # ---- session ----
    def begin_session(self):
        self._session_ts = int(time.time())

    def finalize_session(self, seen_paths: List[str], purge_deleted: bool = False):
        if self._session_ts is None:
            self._session_ts = int(time.time())
        now = self._session_ts
        with self.conn:
            if seen_paths:
                q = "UPDATE files SET last_seen=? WHERE path=?"
                for p in seen_paths:
                    self.conn.execute(q, (now, p))
            if purge_deleted:
                self.conn.execute("DELETE FROM files WHERE last_seen < ?", (now,))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---- blur ----
    def get_cached_records(self, paths: List[str]) -> Dict[str, Tuple[int, int, Optional[float]]]:
        if not paths: return {}
        cur = self.conn.cursor()
        out: Dict[str, Tuple[int,int,Optional[float]]] = {}
        q = "SELECT path, mtime, size, blur FROM files WHERE path=?"
        for p in paths:
            cur.execute(q, (p,))
            row = cur.fetchone()
            if row:
                out[row[0]] = (int(row[1]), int(row[2]), row[3] if row[3] is not None else None)
        return out

    def upsert_blur(self, rows: List[Tuple[str, int, int, float]]):
        if not rows: return
        with self.conn:
            for p, m, s, v in rows:
                self.conn.execute(
                    "INSERT INTO files(path, mtime, size, blur, last_seen) VALUES(?,?,?,?,strftime('%s','now')) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,blur=excluded.blur,last_seen=excluded.last_seen",
                    (p, int(m), int(s), float(v)),
                )

    # ---- pHash ----
    def get_cached_phash(self, metas: List[Tuple[str,int,int]]) -> Tuple[List[str], Dict[str,int]]:
        need: List[str] = []
        cached: Dict[str,int] = {}
        if not metas: return need, cached
        cur = self.conn.cursor()
        q = "SELECT mtime,size,phash FROM files WHERE path=?"
        for p, m, s in metas:
            cur.execute(q, (p,))
            row = cur.fetchone()
            if (not row) or (int(row[0]) != int(m) or int(row[1]) != int(s)) or (row[2] is None):
                need.append(p)
            else:
                try:
                    cached[p] = int(str(row[2]), 16) & 0xFFFFFFFFFFFFFFFF
                except Exception:
                    need.append(p)
        return need, cached

    def upsert_phash(self, rows: List[Tuple[str,int]]):
        if not rows: return
        with self.conn:
            for p, h in rows:
                hx = f"{int(h) & 0xFFFFFFFFFFFFFFFF:016x}"
                self.conn.execute(
                    "UPDATE files SET phash=?, last_seen=strftime('%s','now') WHERE path=?",
                    (hx, p),
                )
                if self.conn.total_changes == 0:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO files(path, mtime, size, blur, phash, last_seen) VALUES(?,?,?,?,?,strftime('%s','now'))",
                        (p, 0, 0, None, hx),
                    )

    # ---- dHash（追加）----
    def get_cached_dhash(self, metas: List[Tuple[str,int,int]]) -> Tuple[List[str], Dict[str,int]]:
        need: List[str] = []
        cached: Dict[str,int] = {}
        if not metas: return need, cached
        cur = self.conn.cursor()
        q = "SELECT mtime,size,dhash FROM files WHERE path=?"
        for p, m, s in metas:
            cur.execute(q, (p,))
            row = cur.fetchone()
            if (not row) or (int(row[0]) != int(m) or int(row[1]) != int(s)) or (row[2] is None):
                need.append(p)
            else:
                try:
                    cached[p] = int(str(row[2]), 16) & 0xFFFFFFFFFFFFFFFF
                except Exception:
                    need.append(p)
        return need, cached

    def upsert_dhash(self, rows: List[Tuple[str,int]]):
        if not rows: return
        with self.conn:
            for p, h in rows:
                hx = f"{int(h) & 0xFFFFFFFFFFFFFFFF:016x}"
                self.conn.execute(
                    "UPDATE files SET dhash=?, last_seen=strftime('%s','now') WHERE path=?",
                    (hx, p),
                )
                if self.conn.total_changes == 0:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO files(path, mtime, size, blur, phash, dhash, last_seen) VALUES(?,?,?,?,?,?,strftime('%s','now'))",
                        (p, 0, 0, None, None, hx),
                    )

    # ---- 旧名互換（必要なら使われる）----
    def upsert_hash(self, rows: List[Tuple[str,int]]):
        # 後方互換：旧呼び出し名を受けて pHash とみなす
        self.upsert_phash(rows)
