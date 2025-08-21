from __future__ import annotations
import os, sys, threading, queue, subprocess
from typing import Dict, List, Optional, Tuple, Any

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from ..apply import apply_from_rows
from .table_views import BlurTable, VisualTable
from .preview_panel import PreviewPanel

from ..fast_scan import (
    list_image_files,
    compute_blur_parallel,
    compute_phash_parallel,
    build_similar_pairs_bktree,
    DEFAULT_EXTS,
)
from ..cache_db import open_cache_at, CacheDB

__all__ = ["main"]

PHASH_DIST = 6
AUTO_PCT   = 10


class BlurCleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            s = ttk.Style(self)
            s.theme_use("vista" if "vista" in s.theme_names() else "clam")
        except Exception:
            pass

        self.title("画像整理（ブレ/類似）")
        self.geometry("1500x920")
        self.minsize(1240, 720)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.var_target = tk.StringVar(value="")

        self._task_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._all_rows: List[Dict[str, str]] = []
        self._cancel_ev: Optional[threading.Event] = None
        self._alive = True

        # テーブル幅固定（中央ペインの合計幅）
        self._table_width_px = 820

        self._build_ui()
        self._poll_queue()
        self.bind("<Configure>", self._on_main_resize)

    # ---- UI ----
    def _build_ui(self):
        self.paned_main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.paned_main.pack(fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(self.paned_main, bg="#ffffff", width=340)
        self.paned_main.add(sidebar, weight=0)

        def _label(parent, text, **kw):
            return tk.Label(parent, text=text, bg="#ffffff", fg="#222", **kw)

        _label(sidebar, "操作", font=("", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 6))

        row = tk.Frame(sidebar, bg="#ffffff")
        row.pack(fill=tk.X, padx=12, pady=(2, 2))
        _label(row, "対象フォルダ").pack(anchor="w")
        ttk.Entry(row, textvariable=self.var_target).pack(fill=tk.X, pady=3)
        ttk.Button(row, text="参照…", command=self._browse_target).pack(anchor="e")

        btns = tk.Frame(sidebar, bg="#ffffff")
        btns.pack(fill=tk.X, padx=12, pady=(12, 12))
        self.btn_scan   = ttk.Button(btns, text="▶ スキャン開始", command=self._scan_clicked)
        self.btn_cancel = ttk.Button(btns, text="⏹ 中止", command=self._cancel_clicked, state=tk.DISABLED)
        self.btn_apply  = ttk.Button(btns, text="🗑 ごみ箱へ送る（選択）", command=self._apply_clicked)
        self.btn_scan.pack(fill=tk.X)
        self.btn_cancel.pack(fill=tk.X, pady=(8, 0))
        self.btn_apply.pack(fill=tk.X, pady=(8, 0))

        self.right_container = tk.Frame(self.paned_main, bg="#ffffff")
        self.paned_main.add(self.right_container, weight=1)

        self.split = ttk.Panedwindow(self.right_container, orient=tk.HORIZONTAL)
        self.split.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 中央：情報テーブル（固定幅）
        self.center = tk.Frame(self.split, bg="#ffffff", width=self._table_width_px)
        self.center.pack_propagate(False)
        self.split.add(self.center, weight=0)

        self.nb = ttk.Notebook(self.center); self.nb.pack(fill=tk.BOTH, expand=True)
        self.tab_blur = tk.Frame(self.nb, bg="#ffffff")
        self.nb.add(self.tab_blur, text="ブレ結果（昇順）")
        self.blur_table = BlurTable(self.tab_blur, on_select=self._on_select_blur,
                                    on_open=self._open_in_explorer, page_size=1000)
        self.blur_table.pack(fill=tk.BOTH, expand=True)

        self.tab_vis = tk.Frame(self.nb, bg="#ffffff")
        self.nb.add(self.tab_vis, text="類似結果（一致度付き）")
        self.visual_table = VisualTable(self.tab_vis, on_select=self._on_select_visual,
                                        on_open_keep=self._open_in_explorer, on_open_cand=self._open_in_explorer,
                                        page_size=1000)
        self.visual_table.pack(fill=tk.BOTH, expand=True)

        # 右：ステータス＋プレビュー
        right = tk.Frame(self.split, bg="#ffffff")
        self.split.add(right, weight=1)
        self.status_card = tk.LabelFrame(right, text="ステータス", padx=10, pady=10, bg="#ffffff")
        self.lbl_info = tk.Label(self.status_card, text="準備OK", bg="#ffffff", fg="#222")
        self.pb = ttk.Progressbar(self.status_card, orient="horizontal", mode="determinate", maximum=100)
        self.status_card.pack(fill=tk.X, pady=(0, 8))
        self.lbl_info.pack(anchor="w")
        self.pb.pack(fill=tk.X, pady=(6, 0))
        self.preview = PreviewPanel(right, w_single=980, h_single=700, w_pair=860, h_pair=620)
        self.preview.pack(fill=tk.BOTH, expand=True)

        def _set_initial_sash():
            try: self.paned_main.sashpos(0, 340)
            except Exception: pass
            try: self.split.sashpos(0, self._table_width_px)
            except Exception: pass
        self.after(80, _set_initial_sash)

    def _on_main_resize(self, _evt=None):
        try:
            self.center.configure(width=self._table_width_px)
            self.split.sashpos(0, self._table_width_px)
        except Exception:
            pass

    # ---- sidebar ----
    def _browse_target(self):
        d = filedialog.askdirectory(title="対象フォルダを選択")
        if d:
            self.var_target.set(d)

    # ---- Scan / Apply / Cancel ----
    def _scan_clicked(self):
        target = self.var_target.get().strip()
        if not target or not os.path.isdir(target):
            messagebox.showerror("エラー", "対象フォルダが正しくありません。")
            return
        self._cancel_ev = threading.Event()
        self._set_busy_state(True, "スキャン中...")
        threading.Thread(target=self._scan_job, daemon=True).start()

    def _cancel_clicked(self):
        if self._cancel_ev and not self._cancel_ev.is_set():
            self._cancel_ev.set()
            self.lbl_info.config(text="［中止要求］停止中…")

    def _apply_clicked(self):
        mode = "blur" if self.nb.index(self.nb.select()) == 0 else "visual"
        selected_paths = (self.blur_table.selected_candidates()
                          if mode == "blur" else self.visual_table.selected_paths())
        if not selected_paths:
            messagebox.showwarning("注意", "選択された項目がありません。")
            return
        if not messagebox.askyesno("確認", f"選択された {len(selected_paths)} 件をごみ箱へ送ります。よろしいですか？"):
            return
        self._cancel_ev = threading.Event()
        self._set_busy_state(True, "適用中...")
        threading.Thread(target=self._apply_job, args=(mode, set(selected_paths)), daemon=True).start()

    # ---- Jobs ----
    def _scan_job(self):
        cache: Optional[CacheDB] = None
        try:
            target = self.var_target.get().strip()
            # DB（ワーカースレッド内で開く）
            try:
                cache = open_cache_at(target); cache.begin_session()
            except Exception as e:
                self._safe_put(("msg", f"［警告］キャッシュ無効: {e}"))
                cache = None

            # 1) ファイル列挙
            all_paths = list_image_files(target, DEFAULT_EXTS)
            if self._cancel_ev and self._cancel_ev.is_set():
                return

            # mtime/size
            metas: List[Tuple[str,int,int]] = []
            for p in all_paths:
                try:
                    st = os.stat(p); metas.append((p, int(st.st_mtime), int(st.st_size)))
                except Exception:
                    pass

            # 2) blur 差分
            need_blur: List[str] = []
            blur_map: Dict[str, float] = {}
            if cache:
                recs = cache.get_cached_records([p for p,_,_ in metas])
                for p, m, s in metas:
                    r = recs.get(p)
                    if (not r) or (r[0] != m or r[1] != s) or (r[2] is None):
                        need_blur.append(p)
                    else:
                        blur_map[p] = float(r[2])
            else:
                need_blur = [p for p,_,_ in metas]

            if need_blur:
                def _cb_blur(done, total):
                    self._safe_put(("progress", {"phase": "スキャン", "current": done, "total": total}))
                comp = compute_blur_parallel(need_blur, None, _cb_blur, self._cancel_ev)
                if self._cancel_ev and self._cancel_ev.is_set():
                    return
                blur_map.update(comp)
                if cache and comp:
                    mlookup = {p:(m,s) for p,m,s in metas}
                    rows = []
                    for p, v in comp.items():
                        if p in mlookup:
                            m,s = mlookup[p]; rows.append((p, m, s, v))
                    cache.upsert_blur(rows)

            # 3) ブレ表（昇順）
            rows_blur: List[Dict[str, str]] = []
            for p in sorted(blur_map.keys(), key=lambda x: blur_map.get(x, 1e18)):
                v = blur_map[p]
                rows_blur.append({"type": "blur_single", "domain": "single",
                                  "candidate": p, "relation": f"lap_var={v:.6f};"})
            self._safe_put(("table_blur", rows_blur))

            # 4) しきい値→ pHash 対象
            vals = list(blur_map.values())
            thr = self._percentile(vals, AUTO_PCT) if vals else 0.0
            self._safe_put(("msg", f"［自動］ブレしきい値 = {thr:.1f}（下位{AUTO_PCT}%）"))
            sharp_paths = [p for p,_,_ in metas if blur_map.get(p, 0.0) >= thr]
            self._safe_put(("msg", f"［情報］pHash対象 {len(sharp_paths)} 件（全{len(metas)}件中）"))
            if self._cancel_ev and self._cancel_ev.is_set():
                if cache: cache.finalize_session([p for p,_,_ in metas])
                return

            # 5) pHash 差分
            if cache:
                need_hash, cached_hash = cache.get_cached_phash([t for t in metas if t[0] in sharp_paths])
            else:
                need_hash, cached_hash = sharp_paths, {}
            def _cb_hash(done, total):
                self._safe_put(("progress", {"phase": "ハッシュ計算", "current": done, "total": total}))
            path_to_hash: Dict[str,int] = dict(cached_hash)
            if need_hash:
                computed = compute_phash_parallel(need_hash, None, _cb_hash, self._cancel_ev)
                if self._cancel_ev and self._cancel_ev.is_set():
                    if cache: cache.finalize_session([p for p,_,_ in metas])
                    return
                path_to_hash.update(computed)
                if cache and computed:
                    mlookup = {p:(m,s) for p,m,s in metas}
                    rows = []
                    for p, h in computed.items():
                        if p in mlookup:
                            m,s = mlookup[p]; rows.append((p, m, s, h))
                    cache.upsert_phash(rows)

            # 6) 類似探索
            def _cb_bk(done, total):
                self._safe_put(("progress", {"phase": "類似判定", "current": done, "total": total}))
            pairs = build_similar_pairs_bktree(path_to_hash, radius=PHASH_DIST,
                                               progress_cb=_cb_bk, cancel_ev=self._cancel_ev)
            if self._cancel_ev and self._cancel_ev.is_set():
                if cache: cache.finalize_session([p for p,_,_ in metas])
                return

            rows_vis: List[Dict[str, str]] = []
            for a, b, dist in pairs:
                keep, cand = (a, b) if blur_map.get(a, 0.0) >= blur_map.get(b, 0.0) else (b, a)
                rows_vis.append({"type": "visual", "domain": "group",
                                 "keep": keep, "candidate": cand,
                                 "relation": f"dist={dist}; lap_keep={blur_map.get(keep,0.0):.6f}; lap_cand={blur_map.get(cand,0.0):.6f};"})

            self._all_rows = rows_blur + rows_vis
            self._safe_put(("table_vis", rows_vis))
            self._safe_put(("msg", f"［完了］スキャン: ブレ {len(rows_blur)} 件 / 類似 {len(rows_vis)} 件"))

            if cache:
                cache.finalize_session([p for p,_,_ in metas], purge_deleted=True)

        except Exception as e:
            self._safe_put(("error", f"スキャン失敗: {e}"))
        finally:
            try:
                if cache: cache.close()
            except Exception:
                pass
            self._safe_put(("idle", None))

    def _apply_job(self, mode: str, selected_paths: set[str]):
        try:
            filtered: List[Dict[str, str]] = []
            if mode == "blur":
                for r in self._all_rows:
                    if r.get("type")=="blur_single" and r.get("domain")=="single":
                        if (r.get("candidate") or "") in selected_paths:
                            filtered.append(r)
            else:
                for r in self._all_rows:
                    if r.get("type")=="visual" and r.get("domain")=="group":
                        if (r.get("candidate") or "") in selected_paths or (r.get("keep") or "") in selected_paths:
                            filtered.append(r)

            res = apply_from_rows(filtered, only=("blur" if mode=="blur" else "visual"),
                                  protect=None, max_move=None, dry_run=False, log_dir=None)
            self._safe_put(("msg", f"［適用］移動:{res['moved']} / 不明:{res['missing']} / エラー:{res['errors']}"))

            if mode == "blur":
                self._safe_put(("remove_blur", selected_paths))
            else:
                self._safe_put(("remove_visual", selected_paths))
        except Exception as e:
            self._safe_put(("error", f"適用失敗: {e}"))
        finally:
            self._safe_put(("idle", None))

    # ---- Queue ----
    def _poll_queue(self):
        if not self._alive:
            return
        PHASE_JA = {"スキャン":"スキャン","hash":"ハッシュ計算","ハッシュ計算":"ハッシュ計算","類似判定":"類似判定"}
        try:
            while True:
                kind, payload = self._task_q.get_nowait()
                if kind == "msg":
                    self.lbl_info.config(text=str(payload))
                elif kind == "error":
                    self.lbl_info.config(text=str(payload)); messagebox.showerror("エラー", str(payload))
                elif kind == "progress":
                    cur = int(payload.get("current", 0)); tot = max(1, int(payload.get("total", 1)))
                    pct = int(cur * 100 / tot)
                    phase = payload.get("phase", "スキャン")
                    self.pb["maximum"] = 100; self.pb["value"] = pct
                    self.lbl_info.config(text=f"［{PHASE_JA.get(str(phase), str(phase))}］ {cur}/{tot}（{pct}%）")
                elif kind == "table_blur":
                    self.blur_table.load(payload)
                elif kind == "table_vis":
                    self.visual_table.load(payload)
                    kp, cp = self.visual_table.current_pair_paths()
                    if kp or cp: self._on_select_visual(kp, cp)
                elif kind == "remove_blur":
                    self.blur_table.remove_by_paths(payload)
                elif kind == "remove_visual":
                    self.visual_table.remove_by_paths(payload)
                elif kind == "idle":
                    self._set_busy_state(False)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    # ---- 選択→プレビュー ----
    def _on_select_blur(self, path: Optional[str]):
        if not path or not os.path.isfile(path):
            self.preview.clear(); return
        self.preview.show_single(path)

    def _on_select_visual(self, keep: Optional[str], cand: Optional[str]):
        k = keep if (keep and os.path.isfile(keep)) else None
        c = cand if (cand and os.path.isfile(cand)) else None
        if k and c: self.preview.show_pair(k, c)
        elif k: self.preview.show_single(k)
        elif c: self.preview.show_single(c)
        else: self.preview.clear()

    # ---- Utils ----
    @staticmethod
    def _percentile(values: List[float], p: int) -> float:
        if not values: return 0.0
        v = sorted(values); p = max(0, min(100, int(p)))
        idx = int(round((p / 100.0) * (len(v) - 1)))
        return float(v[idx])

    def _open_in_explorer(self, path: str):
        if not path or not os.path.exists(path): return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def _set_busy_state(self, busy: bool, msg: Optional[str] = None):
        if busy:
            if msg: self.lbl_info.config(text=msg)
            self.pb["maximum"] = 100; self.pb["value"] = 0
            self.btn_scan.config(state=tk.DISABLED)
            self.btn_apply.config(state=tk.DISABLED)
            self.btn_cancel.config(state=tk.NORMAL)
            self.config(cursor="watch")
        else:
            self.pb["value"] = 0
            self.btn_scan.config(state=tk.NORMAL)
            self.btn_apply.config(state=tk.NORMAL)
            self.btn_cancel.config(state=tk.DISABLED)
            self.config(cursor="")
        self.update_idletasks()

    def _safe_put(self, item: Tuple[str, Any]):
        try:
            self._task_q.put_nowait(item)
        except queue.Full:
            try: self._task_q.get_nowait()
            except Exception: pass
            try: self._task_q.put_nowait(item)
            except Exception: pass

    def _on_close(self):
        if self._cancel_ev and not self._cancel_ev.is_set():
            self._cancel_ev.set()
        try: self.preview.shutdown()
        except Exception: pass
        self._alive = False
        self.destroy()


def main():
    app = BlurCleanerApp()
    app.mainloop()
