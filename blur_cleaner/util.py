from pathlib import Path

DEFAULT_EXTS = {".jpg",".jpeg",".png",".gif",".bmp",".tif",".tiff",".webp",".heic"}

def want_file(p: Path, include_exts=None):
    if not p.is_file(): return False
    if p.name.lower() in ("thumbs.db",): return False
    exts = include_exts or DEFAULT_EXTS
    return p.suffix.lower() in exts
