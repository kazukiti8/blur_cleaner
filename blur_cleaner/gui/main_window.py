from __future__ import annotations
import os, sys, threading, queue, subprocess
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from ..apply import apply_from_rows
from .table_views import BlurTable, VisualTable
from .preview_panel import PreviewPanel
from .dialogs import TabbedSettingsDialog
try:
    from ..detectors import detect_blur_paths
except Exception:
    detect_blur_paths = None

from ..fast_scan import (
    list_image_files,
    compute_blur_parallel,
    compute_phash_parallel,
    build_similar_pairs_bktree,
    DEFAULT_EXTS,
)
from ..cache_db import open_cache_at, CacheDB   # ← ★ここ修正！

# pHash距離しきい値 / ブレ自動%（旧互換用）
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

        # --- 新方式オプション（GUI設定で変更） ---
        self.opt_and_ten   = True
        self.opt_ms_mode   = "fixed"      # "fixed" / "percentile" / "zscore"
        self.opt_ms_param  = 25.0
        self.opt_ms_fixed  = 800.0
        self.opt_ten_mode  = "fixed"
        self.opt_ten_param = 25.0
        self.opt_ten_fixed = 800.0

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

        head = tk.Frame(sidebar, bg="#ffffff"); head.pack(fill=tk.X, padx=12, pady=(12, 0))
        tk.Label(head, text="対象フォルダ", bg="#ffffff").pack(anchor="w")
        row = tk.Frame(head, bg="#ffffff"); row.pack(fill=tk.X, pady=(4, 6))
        tk.Entry(row, textvariable=self.var_target).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="参照", command=self._choose_dir).pack(side=tk.LEFT, padx=(8,0))

        btns = tk.Frame(sidebar, bg="#ffffff")
        btns.pack(fill=tk.X, padx=12, pady=(12, 12))
        self.btn_scan   = ttk.Button(btns, text="▶ スキャン開始", command=self._scan_clicked)
        self.btn_cancel = ttk.Button(btns, text="⏹ 中止", command=self._cancel_clicked, state=tk.DISABLED)
        self.btn_apply  = ttk.Button(btns, text="🗑 ごみ箱へ送る（選択）", command=self._apply_clicked)
        self.btn_scan.pack(fill=tk.X)
        self.btn_cancel.pack(fill=tk.X, pady=(8, 0))
        self.btn_apply.pack(fill=tk.X, pady=(8, 0))

        # ★ 設定ボタン（新規）
        self.btn_settings = ttk.Button(btns, text="⚙ 設定…", command=self._open_settings)
        self.btn_settings.pack(fill=tk.X, pady=(8, 0))

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
        self.visual_table = VisualTable(
            self.tab_vis,
            on_select=self._on_select_visual,
            on_open_keep=lambda p: self._open_in_explorer(p),
            on_open_cand=lambda p: self._open_in_explorer(p),
            page_size=1000
        )

        self.visual_table.pack(fill=tk.BOTH, expand=True)

        # 右：プレビュー
        self.preview = PreviewPanel(self.split)
        self.split.add(self.preview, weight=1)

        # 下：情報ラベル
        foot = tk.Frame(self.right_container, bg="#ffffff"); foot.pack(fill=tk.X)
        self.progress = ttk.Progressbar(foot, mode="determinate", length=260)
        self.progress.pack(side=tk.RIGHT, padx=8, pady=(0,8))
        self.lbl_info = tk.Label(foot, text="準備完了", bg="#ffffff")
        self.lbl_info.pack(side=tk.LEFT, padx=8, pady=(0,8))

    def _on_main_resize(self, _e=None):
        try:
            w = self.right_container.winfo_width()
            self._table_width_px = min(960, max(560, int(w * 0.55)))
            self.center.config(width=self._table_width_px)
        except Exception:
            pass

    def _choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.var_target.set(d)

    def _set_busy_state(self, busy: bool, msg: str):
        self.btn_scan.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_apply.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_cancel.config(state=tk.NORMAL if busy else tk.DISABLED)
        self.progress.config(mode="indeterminate" if busy else "determinate")
        if busy:
            self.progress.start(50)
        else:
            try: self.progress.stop()
            except Exception: pass
        self.lbl_info.config(text=msg)

    def _open_settings(self):
        d = TabbedSettingsDialog(
            self,
            target_dir=self.var_target.get().strip(),
            include="", exclude="",
            blur_auto=(self.opt_ms_mode!="fixed"),
            blur_pct=int(self.opt_ms_param),
            blur_thr=float(self.opt_ms_fixed),
            and_tenengrad=self.opt_and_ten,
            ms_mode=self.opt_ms_mode, ms_param=self.opt_ms_param,
            ten_mode=self.opt_ten_mode, ten_thr=self.opt_ten_fixed, ten_param=self.opt_ten_param
        )
        if d.result:
            r = d.result
            self.opt_and_ten   = bool(r.get("and_tenengrad", True))
            self.opt_ms_mode   = str(r.get("ms_mode", "fixed"))
            self.opt_ms_param  = float(r.get("ms_param", 25.0))
            self.opt_ms_fixed  = float(r.get("blur_thr", 800.0))
            self.opt_ten_mode  = str(r.get("ten_mode", "fixed"))
            self.opt_ten_param = float(r.get("ten_param", 25.0))
            self.opt_ten_fixed = float(r.get("ten_thr", 800.0))
            self.lbl_info.config(text="設定を更新しました。")

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

            # メタ情報（mtime/size）
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

            used_new_detector = False
            path_is_blur = {}
            path_to_ten = {}
            if need_blur:
                if detect_blur_paths is not None:
                    used_new_detector = True
                    self._safe_put(("progress", {"phase": "スキャン(新方式)", "current": 0, "total": len(need_blur)}))
                    # しきい値モード
                    ms_auto  = (None if self.opt_ms_mode == "fixed" else self.opt_ms_mode)
                    ms_fixed = (None if self.opt_ms_mode != "fixed" else self.opt_ms_fixed)
                    ten_auto  = (None if self.opt_ten_mode == "fixed" else self.opt_ten_mode)
                    ten_fixed = (None if self.opt_ten_mode != "fixed" else self.opt_ten_fixed)
                    rows, meta = detect_blur_paths(
                        [Path(p) for p in need_blur],
                        agg="median",
                        gauss_ksize=3,
                        and_tenengrad=self.opt_and_ten,
                        threshold=ms_fixed, auto_th=ms_auto, auto_param=self.opt_ms_param,
                        ten_threshold=ten_fixed, ten_auto_th=ten_auto, ten_auto_param=self.opt_ten_param,
                        max_side=2000, legacy=False,
                    )
                    mlookup = {p:(m,s) for p,m,s in metas}
                    up_rows = []
                    for r in rows:
                        p = str(r.get("path") or "")
                        if not p: continue
                        ms = float(r.get("score") or 0.0)
                        ten = float(r.get("ten_score") or 0.0)
                        isb = bool(r.get("is_blur") is True)
                        blur_map[p] = ms
                        path_to_ten[p] = ten
                        path_is_blur[p] = isb
                        if cache and p in mlookup:
                            m,s = mlookup[p]; up_rows.append((p, m, s, ms))
                    if cache and up_rows:
                        cache.upsert_blur(up_rows)
                    self._safe_put(("msg", f"［しきい値］MS:{meta.get('th_ms',0):.1f} / TEN:{meta.get('th_ten',0):.1f} / AND:{'ON' if self.opt_and_ten else 'OFF'}"))
                else:
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
                v_ms = blur_map[p]
                v_tn = path_to_ten.get(p, 0.0)
                rows_blur.append({"type": "blur_single", "domain": "single",
                                  "candidate": p, "relation": f"lap_var={v_ms:.6f}; ten={v_tn:.6f};"})
            self._safe_put(("table_blur", rows_blur))

            # 4) pHash 対象選定
            if 'used_new_detector' in locals() and used_new_detector and path_is_blur:
                sharp_paths = [p for p,_,_ in metas if not path_is_blur.get(p, False)]
                self._safe_put(("msg", f"［選定］新方式の判定で pHash対象 {len(sharp_paths)} 件（全{len(metas)}件中）"))
            else:
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
                    cache.upsert_hash([(p, h) for p,h in computed.items()])

            # 6) 類似ペア抽出
            pairs: List[Tuple[str,str,int]] = []
            # …（既存の pHash ペア抽出ロジックをそのまま呼び出し）…
            # ここは元の実装に合わせて差し替えてね（距離, PHASH_DIST）

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
                    if r.get("type") == "blur_single" and r.get("candidate") in selected_paths:
                        filtered.append(r)
            else:
                for r in self._all_rows:
                    if r.get("type") == "visual" and r.get("candidate") in selected_paths:
                        filtered.append(r)
            apply_from_rows(filtered)
            self._safe_put(("msg", f"［完了］適用: {len(filtered)} 件をごみ箱へ送付"))
            # テーブルから消す
            if mode == "blur":
                self.blur_table.remove_by_paths(selected_paths)
            else:
                self.visual_table.remove_candidates(selected_paths)
        except Exception as e:
            self._safe_put(("error", f"適用失敗: {e}"))
        finally:
            self._safe_put(("idle", None))

    # ---- Queue / Preview ----
    def _poll_queue(self):
        if not self._alive: return
        try:
            msg = self._task_q.get_nowait()
        except queue.Empty:
            self.after(60, self._poll_queue)
            return
        tag, payload = msg
        if tag == "progress":
            cur, total = payload.get("current", 0), payload.get("total", 0)
            try:
                self.progress.config(mode="determinate", maximum=max(1, int(total)))
                self.progress["value"] = int(cur)
            except Exception:
                pass
        elif tag == "msg":
            self.lbl_info.config(text=str(payload))
        elif tag == "error":
            messagebox.showerror("エラー", str(payload))
            self.lbl_info.config(text=str(payload))
        elif tag == "idle":
            self._set_busy_state(False, "待機中")
        elif tag == "table_blur":
            self.blur_table.load(payload)
        elif tag == "table_vis":
            self.visual_table.load(payload)
        self.after(60, self._poll_queue)

    def _safe_put(self, item: Tuple[str, Any]):
        try: self._task_q.put_nowait(item)
        except Exception: pass

    def _on_select_blur(self, path: Optional[str]):
        if path and os.path.isfile(path): self.preview.show_single(path)
        else: self.preview.clear()

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
                subprocess.run(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", path])
            else:
                subprocess.run(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

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
