from __future__ import annotations
import os, threading, queue, time
from typing import Optional, Tuple, Dict, Any
import tkinter as tk
from PIL import Image, ImageTk, ImageOps

# HEIF対応
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass


class _LRU:
    def __init__(self, max_items: int = 512):
        self.max = max_items
        self._dict: Dict[str, Tuple[float, Image.Image]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Image.Image]:
        with self._lock:
            v = self._dict.get(key)
            if not v: return None
            self._dict[key] = (time.time(), v[1])
            return v[1]

    def put(self, key: str, img: Image.Image):
        with self._lock:
            self._dict[key] = (time.time(), img)
            if len(self._dict) > self.max:
                oldest = min(self._dict.items(), key=lambda kv: kv[1][0])[0]
                self._dict.pop(oldest, None)


class PreviewPanel(tk.LabelFrame):
    """
    ・中央描画（anchor=center）
    ・遅延サムネ生成（メモリLRUのみ）
    """
    def __init__(self, master, title: str = "プレビュー",
                 w_single=840, h_single=630, w_pair=720, h_pair=540):
        super().__init__(master, text=title, padx=8, pady=8, bg="#ffffff")
        self._default_size = (w_single, h_single, w_pair, h_pair)

        self.cv_single = tk.Canvas(self, width=w_single, height=h_single, bg="#222", highlightthickness=0)
        self.frm_pair  = tk.Frame(self, bg="#ffffff")
        self.lbl_keep = tk.Label(self.frm_pair, text="保持", bg="#ffffff")
        self.lbl_cand = tk.Label(self.frm_pair, text="候補", bg="#ffffff")
        self.cv_left  = tk.Canvas(self.frm_pair, width=w_pair, height=h_pair, bg="#222", highlightthickness=0)
        self.cv_right = tk.Canvas(self.frm_pair, width=w_pair, height=h_pair, bg="#222", highlightthickness=0)
        self.frm_pair.grid_columnconfigure(0, weight=1)
        self.frm_pair.grid_columnconfigure(1, weight=1)
        self.lbl_keep.grid(row=0, column=0, sticky="w", padx=6, pady=(0, 6))
        self.lbl_cand.grid(row=0, column=1, sticky="w", padx=6, pady=(0, 6))
        self.cv_left.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.cv_right.grid(row=1, column=1, sticky="nsew", padx=(6, 0))

        self.cv_single.pack_forget()
        self.frm_pair.pack_forget()

        self._last_mode: str = "none"
        self._last_paths: Dict[str, str] = {"single": "", "left": "", "right": ""}

        self._rq: "queue.Queue[Tuple[str, Tuple[Any, ...]]]" = queue.Queue(maxsize=256)
        self._dq: "queue.Queue[Tuple[str, Tuple[Any, ...]]]" = queue.Queue(maxsize=256)
        self._cache = _LRU(max_items=256)

        self._alive = True
        self._after_id: Optional[str] = None
        threading.Thread(target=self._worker, daemon=True).start()
        self._schedule_drain()

        for cv in (self.cv_single, self.cv_left, self.cv_right):
            cv.bind("<Configure>", self._on_canvas_resize)

    # ---- API ----
    def clear(self):
        self._last_mode = "none"
        self.cv_single.pack_forget()
        self.frm_pair.pack_forget()

    def show_single(self, path: str):
        if not path or not os.path.isfile(path):
            self.clear(); return
        self.frm_pair.pack_forget()
        if not self.cv_single.winfo_ismapped():
            self.cv_single.pack(fill="both", expand=True)
        self._last_mode = "single"
        self._last_paths["single"] = path
        w = max(50, self.cv_single.winfo_width() or self._default_size[0])
        h = max(50, self.cv_single.winfo_height() or self._default_size[1])
        self._enqueue("single", (path, w, h))

    def show_pair(self, left: str, right: str):
        if left and not os.path.isfile(left): left = ""
        if right and not os.path.isfile(right): right = ""
        if left and right:
            self.cv_single.pack_forget()
            if not self.frm_pair.winfo_ismapped():
                self.frm_pair.pack(fill="both", expand=True)
            self._last_mode = "pair"
            self._last_paths["left"] = left
            self._last_paths["right"] = right
            wl = max(50, self.cv_left.winfo_width() or self._default_size[2])
            hl = max(50, self.cv_left.winfo_height() or self._default_size[3])
            wr = max(50, self.cv_right.winfo_width() or self._default_size[2])
            hr = max(50, self.cv_right.winfo_height() or self._default_size[3])
            self._enqueue("left",  (left,  wl, hl))
            self._enqueue("right", (right, wr, hr))
        elif left:
            self.show_single(left)
        elif right:
            self.show_single(right)
        else:
            self.clear()

    def shutdown(self):
        self._alive = False
        if self._after_id is not None:
            try: self.after_cancel(self._after_id)
            except Exception: pass
            self._after_id = None
        try: self._rq.put_nowait(("__quit__", tuple()))
        except Exception: pass

    # ---- 内部：非同期処理 ----
    def _enqueue(self, kind: str, args):
        try:
            self._rq.put_nowait((kind, args))
        except queue.Full:
            try: self._rq.get_nowait()
            except Exception: pass
            try: self._rq.put_nowait((kind, args))
            except Exception: pass

    def _worker(self):
        while True:
            try:
                kind, args = self._rq.get()
                if kind == "__quit__": break
                path, w, h = args

                key = f"{path}|{w}x{h}|{os.path.getmtime(path) if os.path.exists(path) else 0}"
                pil_img = self._cache.get(key)

                if pil_img is None:
                    pil_img = self._load_pil(path, w, h)
                    if pil_img:
                        self._cache.put(key, pil_img)

                if not self._alive: break
                if pil_img is not None:
                    try: self._dq.put_nowait((kind, (path, pil_img)))
                    except queue.Full:
                        try: self._dq.get_nowait()
                        except Exception: pass
                        try: self._dq.put_nowait((kind, (path, pil_img)))
                        except Exception: pass
            except Exception:
                pass

    def _schedule_drain(self):
        if not self._alive: return
        self._after_id = self.after(30, self._drain_draw_queue)

    def _drain_draw_queue(self):
        if not self._alive: return
        try:
            while True:
                kind, args = self._dq.get_nowait()
                path, pil_img = args
                try:
                    tk_img = ImageTk.PhotoImage(pil_img)
                except Exception:
                    continue
                if not self._alive: return
                if kind == "single":
                    self._draw_center(self.cv_single, tk_img)
                elif kind == "left":
                    self._draw_center(self.cv_left, tk_img)
                elif kind == "right":
                    self._draw_center(self.cv_right, tk_img)
        except queue.Empty:
            pass
        self._schedule_drain()

    @staticmethod
    def _draw_center(canvas: tk.Canvas, img: ImageTk.PhotoImage):
        try:
            canvas.delete("all")
            w = max(1, int(canvas.winfo_width()))
            h = max(1, int(canvas.winfo_height()))
            canvas.create_image(w//2, h//2, image=img, anchor="center")
            canvas.image = img
        except Exception:
            pass

    @staticmethod
    def _load_pil(path: str, w: int, h: int) -> Optional[Image.Image]:
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
                im.thumbnail((w, h), Image.Resampling.LANCZOS)
                return im.copy()
        except Exception:
            return None

    def _on_canvas_resize(self, _e: tk.Event):
        if not self._alive: return
        if self._last_mode == "single" and self.cv_single.winfo_ismapped():
            p = self._last_paths.get("single") or ""
            if p:
                w = max(50, self.cv_single.winfo_width())
                h = max(50, self.cv_single.winfo_height())
                self._enqueue("single", (p, w, h))
        elif self._last_mode == "pair" and self.frm_pair.winfo_ismapped():
            lp = self._last_paths.get("left") or ""
            rp = self._last_paths.get("right") or ""
            if lp:
                w = max(50, self.cv_left.winfo_width()); h = max(50, self.cv_left.winfo_height())
                self._enqueue("left", (lp, w, h))
            if rp:
                w = max(50, self.cv_right.winfo_width()); h = max(50, self.cv_right.winfo_height())
                self._enqueue("right", (rp, w, h))
