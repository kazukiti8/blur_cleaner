# blur_cleaner/cache_db.py
from __future__ import annotations
import os, sqlite3, time, threading
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  mtime      INTEGER NOT NULL,
  size       INTEGER NOT NULL,
  blur       REAL,
  phash      TEXT,            -- ★ INTEGER → TEXT（16桁hex）
  last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON files(last_seen);
"""

class CacheDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.session_ts = int(time.time())
        # 他スレッド使用も許可（ロックで直列化）
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self):
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # ---- セッション制御 ----
    def begin_session(self):
        self.session_ts = int(time.time())

    def finalize_session(self, seen_paths: Iterable[str], purge_deleted: bool = True):
        ts = self.session_ts
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE files SET last_seen=? WHERE path=?",
                ((ts, p) for p in seen_paths)
            )
            if purge_deleted:
                self._conn.execute("DELETE FROM files WHERE last_seen < ?", (ts,))

    # ---- 取得系 ----
    def get_cached_records(
        self, paths: Iterable[str]
    ) -> Dict[str, Tuple[int,int,Optional[float],Optional[int]]]:
        """
        return: {path: (mtime, size, blur, phash_int or None)}
        """
        ps = list(paths)
        out: Dict[str, Tuple[int,int,Optional[float],Optional[int]]] = {}
        if not ps:
            return out
        q = "SELECT path, mtime, size, blur, phash FROM files WHERE path IN (%s)" % \
            ",".join("?" for _ in ps)
        with self._lock:
            cur = self._conn.execute(q, ps)
            for row in cur:
                path, mtime, size, blur, ph = row
                # TEXT(hex) → int。NULL/空は None 扱い
                ph_int = None
                if ph:
                    try:
                        ph_int = int(str(ph), 16)
                    except Exception:
                        ph_int = None
                out[path] = (int(mtime), int(size), (blur if blur is not None else None), ph_int)
        return out

    def get_cached_phash(
        self, files: List[Tuple[str,int,int]]
    ) -> Tuple[List[str], Dict[str,int]]:
        """
        files: [(path, mtime, size)]
        returns: (need_compute_paths, cached_map)
        """
        need: List[str] = []
        cached: Dict[str,int] = {}
        if not files:
            return need, cached

        paths = [p for p,_,_ in files]
        cache = self.get_cached_records(paths)
        for p, m, s in files:
            rec = cache.get(p)
            if not rec:
                need.append(p); continue
            cm, cs, _, ph = rec
            if cm != m or cs != s or ph is None:
                need.append(p)
            else:
                cached[p] = int(ph)
        return need, cached

    # ---- 更新系 ----
    def upsert_blur(self, rows: List[Tuple[str,int,int,float]]):
        """
        rows: [(path, mtime, size, blur)]
        """
        if not rows:
            return
        ts = self.session_ts
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO files (path, mtime, size, blur, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     mtime=excluded.mtime,
                     size=excluded.size,
                     blur=excluded.blur,
                     last_seen=excluded.last_seen""",
                [(p, int(m), int(s), float(b), ts) for (p,m,s,b) in rows]
            )

    def upsert_phash(self, rows: List[Tuple[str,int,int,int]]):
        """
        rows: [(path, mtime, size, phash_int)]
        phash は 16桁HEXの TEXT で保存
        """
        if not rows:
            return
        ts = self.session_ts
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO files (path, mtime, size, phash, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     mtime=excluded.mtime,
                     size=excluded.size,
                     phash=excluded.phash,
                     last_seen=excluded.last_seen""",
                [(p, int(m), int(s), f"{int(h) & ((1<<64)-1):016x}", ts) for (p,m,s,h) in rows]
            )

    # デバッグ用
    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM files")
            return int(cur.fetchone()[0])


def open_cache_at(target_dir: str, db_name: str = ".blur_cleaner_cache.sqlite") -> CacheDB:
    os.makedirs(target_dir, exist_ok=True)
    db_path = os.path.join(target_dir, db_name)
    return CacheDB(db_path)
