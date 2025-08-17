import sqlite3, os
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache(
  path TEXT PRIMARY KEY,
  mtime REAL, size INTEGER,
  sha TEXT, phash TEXT, blur REAL
);
"""

def ensure_db(dbpath: str):
    Path(dbpath).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dbpath)
    con.execute(SCHEMA)
    con.commit()
    return con

def stat_tuple(p: str):
    st = os.stat(p)
    return st.st_mtime, st.st_size
