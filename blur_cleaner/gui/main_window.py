from __future__ import annotations
import os, sys, json, threading, queue, subprocess
from typing import Dict, List, Optional, Tuple, Any

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from ..scan import scan, ScanRow
from ..apply import apply_from_rows
from .table_views import BlurTable, VisualTable
from .preview_panel import PreviewPanel

from ..fast_scan import compute_phash_parallel, build_similar_pairs_bktree

__all__ = ["main"]

PHASH_DIST  = 6
DO_SIMILAR  = False   # 既存scanの類似は使わず、自前で行う
AUTO_PCT    = 10      # ブレ下位10%を閾値に固定


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

        self.var_target   = tk.StringVar(value="")

        self._task_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._all_rows: List[Dict[str, str]] = []
        self._cancel_ev: Optional[threading.Event] = None
        self._alive = True  # pollループ生存フラグ

        self._build_ui()
        self._poll_queue()

    # ---- UI ----
    def _build_ui(self):
        paned_main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned_main.pack(fill=tk.BOTH, expand=True)

        # サイドバー
        sidebar = tk.Frame(paned_main, bg="#ffffff", width=360)
        paned_main.add(sidebar, weight=0)

        def _label(parent, text, **kw):
            return tk.Label(parent, text=text, bg="#ffffff", fg="#222", **kw)

        _label(sidebar, "操作", font=("", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 6))

        row = tk.Frame(sidebar, bg="#ffffff")
        row.pack(fill=tk.X, padx=12, pady=(2, 2))
        _label(row, "対象フォルダ").pack(anchor="w")
        ttk.Entry(row, textvariable=self.var_target).pack(fill=tk.X, pady=3)
        ttk.Button(row, text="参照…", command=self._browse_target).pack(anchor="e")

        # ボタン群
        btns = tk.Frame(sidebar, bg="#ffffff")
        btns.pack(fill=tk.X, padx=12, pady=(12, 12))
        self.btn_scan = ttk.Button(btns, text="▶ スキャン開始", command=self._scan_clicked)
        self.btn_cancel = ttk.Button(btns, text="⏹ 中止", command=self._cancel_clicked, state=tk.DISABLED)
        self.btn_apply = ttk.Button(btns, text="🗑 ごみ箱へ送る（選択）", command=self._apply_clicked)

        self.btn_scan.pack(fill=tk.X)
        self.btn_cancel.pack(fill=tk.X, pady=(8, 0))
        self.btn_apply.pack(fill=tk.X, pady=(8, 0))

        # 右側
        right_container = tk.Frame(paned_main, bg="#ffffff")
        paned_main.add(right_container, weight=1)

        split = ttk.Panedwindow(right_container, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        center = tk.Frame(split, bg="#ffffff"); split.add(center, weight=3)
        self.nb = ttk.Notebook(center); self.nb.pack(fill=tk.BOTH, expand=True)

        self.tab_blur = tk.Frame(self.nb, bg="#ffffff"); self.nb.add(self.tab_blur, text="ブレ結果（昇順）")
        self.blur_table = BlurTable(self.tab_blur, on_select=self._on_select_blur,
                                    on_open=self._open_in_explorer, page_size=1000)
        self.blur_table.pack(fill=tk.BOTH, expand=True)

        self.tab_vis = tk.Frame(self.nb, bg="#ffffff"); self.nb.add(self.tab_vis, text="類似結果（一致度付き）")
        self.visual_table = VisualTable(self.tab_vis, on_select=self._on_select_visual,
                                        on_open_keep=self._open_in_explorer, on_open_cand=self._open_in_explorer,
                                        page_size=1000)
        self.visual_table.pack(fill=tk.BOTH, expand=True)

        right = tk.Frame(split, bg="#ffffff"); split.add(right, weight=5)
        self.status_card = tk.LabelFrame(right, text="ステータス", padx=10, pady=10, bg="#ffffff")
        self.lbl_info = tk.Label(self.status_card, text="準備OK", bg="#ffffff", fg="#222")
        self.pb = ttk.Progressbar(self.status_card, orient="horizontal", mode="determinate", maximum=100)
        self.status_card.pack(fill=tk.X, pady=(0, 8))
        self.lbl_info.pack(anchor="w")
        self.pb.pack(fill=tk.X, pady=(6, 0))

        # プレビュー
        self.preview = PreviewPanel(right, w_single=880, h_single=660, w_pair=760, h_pair=560)
        self.preview.pack(fill=tk.BOTH, expand=True)

        def _set_initial_sash():
            try:
                paned_main.sashpos(0, 340)
            except Exception:
                pass
            try:
                total = right_container.winfo_width() or self.winfo_width()
                split.sashpos(0, int(total * 0.40))
            except Exception:
                pass
        self.after(80, _set_initial_sash)

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
        try:
            target = self.var_target.get().strip()

            def _prog(phase: str, cur: int, tot: int):
                self._safe_put(("progress", {"phase": phase, "current": cur, "total": tot}))

            # Phase A: ブレのみ（全件）
            rows_passA, stats = scan(
                target_dir=target, report_csv=None, dbpath=None,
                blur_threshold=1e9, do_similar=DO_SIMILAR, phash_distance=PHASH_DIST,
                include_exts=None, exclude_substr=None, collect_stats=True,
                progress_cb=_prog,
            )
            if self._cancel_ev and self._cancel_ev.is_set():
                self._safe_put(("msg", "［中止］ブレ計測を中断しました")); return

            # 自動しきい値（下位10%固定）
            vals = [r.blur_value for r in rows_passA]
            thr = self._percentile(vals, AUTO_PCT) if vals else 0.0
            self._safe_put(("msg", f"［自動］ブレしきい値 = {thr:.1f}（下位{AUTO_PCT}%）"))

            rows_blur = [{
                "type": "blur_single", "domain": "single",
                "candidate": r.path, "relation": f"lap_var={r.blur_value:.6f};",
            } for r in rows_passA]

            # Phase B: pHashはシャープのみ
            sharp_paths = [r.path for r in rows_passA if r.blur_value >= thr]
            self._safe_put(("msg", f"［情報］pHash対象 {len(sharp_paths)} 件（全{len(rows_passA)}件中）"))
            if self._cancel_ev and self._cancel_ev.is_set():
                self._all_rows = rows_blur
                self._safe_put(("table_blur", rows_blur))
                self._safe_put(("msg", "［中止］pHash計算を開始せず終了"))
                return

            def _cb_hash(done, total):
                self._safe_put(("progress", {"phase": "hash", "current": done, "total": total}))
            path_to_hash = compute_phash_parallel(
                sharp_paths, max_workers=None, progress_cb=_cb_hash, cancel_ev=self._cancel_ev
            )
            if self._cancel_ev and self._cancel_ev.is_set():
                self._all_rows = rows_blur
                self._safe_put(("table_blur", rows_blur))
                self._safe_put(("msg", "［中止］pHash計算を中断しました"))
                return

            def _cb_bk(done, total):
                self._safe_put(("progress", {"phase": "類似判定", "current": done, "total": total}))
            pairs = build_similar_pairs_bktree(
                path_to_hash, radius=PHASH_DIST, progress_cb=_cb_bk, cancel_ev=self._cancel_ev
            )
            if self._cancel_ev and self._cancel_ev.is_set():
                self._all_rows = rows_blur
                self._safe_put(("table_blur", rows_blur))
                self._safe_put(("msg", "［中止］類似探索を中断しました"))
                return

            blur_map = {r.path: r.blur_value for r in rows_passA}
            rows_vis: List[Dict[str, str]] = []
            for a, b, dist in pairs:
                keep, cand = (a, b) if blur_map.get(a, 0.0) >= blur_map.get(b, 0.0) else (b, a)
                rows_vis.append({
                    "type": "visual", "domain": "group",
                    "keep": keep, "candidate": cand,
                    "relation": f"dist={dist}; lap_keep={blur_map.get(keep, 0.0):.6f}; lap_cand={blur_map.get(cand, 0.0):.6f};"
                })

            self._all_rows = rows_blur + rows_vis
            self._safe_put(("table_blur", rows_blur))
            self._safe_put(("table_vis", rows_vis))
            self._safe_put(("msg", f"［完了］スキャン: ブレ {len(rows_blur)} 件 / 類似 {len(rows_vis)} 件"))
        except Exception as e:
            self._safe_put(("error", f"スキャン失敗: {e}"))
        finally:
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

    # ---- Queue反映 ----
    def _poll_queue(self):
        if not self._alive:
            return
        PHASE_JA = {"scan": "スキャン", "similar": "類似判定", "hash": "ハッシュ計算", "load": "読込", "save": "保存", "類似判定": "類似判定"}
        try:
            while True:
                kind, payload = self._task_q.get_nowait()
                if kind == "msg":
                    self.lbl_info.config(text=str(payload))
                elif kind == "error":
                    self.lbl_info.config(text=str(payload)); messagebox.showerror("エラー", str(payload))
                elif kind == "progress":
                    cur = int(payload.get("current", 0))
                    tot = max(1, int(payload.get("total", 1)))
                    pct = int(cur * 100 / tot)
                    phase = payload.get("phase", "scan")
                    self.pb["maximum"] = 100
                    self.pb["value"] = pct
                    self.lbl_info.config(text=f"［{PHASE_JA.get(str(phase).lower(), '処理')}］ {cur}/{tot}（{pct}%）")
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
        # 空振りガード（テーブルリロード直後など）
        if not path or not os.path.isfile(path):
            self.preview.clear(); return
        self.preview.show_single(path)

    def _on_select_visual(self, keep: Optional[str], cand: Optional[str]):
        # 実在チェック
        k = keep if (keep and os.path.isfile(keep)) else None
        c = cand if (cand and os.path.isfile(cand)) else None
        if k and c: self.preview.show_pair(k, c)
        elif k: self.preview.show_single(k)
        elif c: self.preview.show_single(c)
        else: self.preview.clear()

    # ---- Util ----
    @staticmethod
    def _percentile(values: List[float], p: int) -> float:
        if not values: return 0.0
        v = sorted(values); p = max(0, min(100, int(p)))
        idx = int(round((p / 100.0) * (len(v) - 1)))
        return float(v[idx])

    def _open_in_explorer(self, path: str):
        if not path or not os.path.exists(path): 
            return
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
        # キューあふれ時は最古を捨てる
        try:
            self._task_q.put_nowait(item)
        except queue.Full:
            try:
                self._task_q.get_nowait()
            except Exception:
                pass
            try:
                self._task_q.put_nowait(item)
            except Exception:
                pass

    def _on_close(self):
        # 処理中なら中止要求
        if self._cancel_ev and not self._cancel_ev.is_set():
            self._cancel_ev.set()
        # プレビュー停止
        try:
            self.preview.shutdown()
        except Exception:
            pass
        # poll停止
        self._alive = False
        self.destroy()


def main():
    app = BlurCleanerApp()
    app.mainloop()
