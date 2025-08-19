from __future__ import annotations
import os
from typing import Dict, Tuple, Optional
from PIL import Image, ImageTk, ImageOps  # pip install pillow

__all__ = ["ThumbnailCache"]

class ThumbnailCache:
    """path -> (mtime, PhotoImage, (w,h)) をキャッシュ"""
    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, ImageTk.PhotoImage, Tuple[int,int]]] = {}

    def clear(self) -> None:
        self._cache.clear()

    def get_thumb(self, path: str, max_w: int, max_h: int) -> Optional[ImageTk.PhotoImage]:
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return None
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with Image.open(path) as im:
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                im.thumbnail((max_w, max_h), Image.LANCZOS)
                tkimg = ImageTk.PhotoImage(im)
        except Exception:
            return None
        self._cache[path] = (mtime, tkimg, im.size if 'im' in locals() else (0,0))
        return tkimg
