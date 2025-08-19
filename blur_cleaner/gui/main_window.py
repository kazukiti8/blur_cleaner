from __future__ import annotations
import os
import json
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, List, Optional, Tuple, Any

from ..scan import scan
from ..apply import apply_from_rows
from .dialogs import TabbedSettingsDialog
from .thumbs import ThumbnailCache

__all__ = ["main"]

CONFIG_NAME = "scan_cshe_cfg.json"   # 設定ファイル名（対象フォルダ直下）
CACHE_DB    = "scan_cshe"            # DBファイル名（対象フォルダ直下）
PHASH_DIST  = 6                      # 類似距離は固定
DO_SIMILAR  = True                   # 類似判定は常時ON

# ------------ ユーティリティ ------------
def _cache_path_for(target_dir: str) -> str:
    return os.path.join(target_dir, CACHE_DB)

def _cfg_path_for(target_dir: str) -> str:
    return os.path.join(target_dir, CONFIG_NAME)

def _fmt_size(bytes_: int) -> str:
    try:
        b = int(bytes_)
    except Exception:
        return "-"
    if b < 1024: return f"{b} B"
    kb = b / 1024.0
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024.0
    if mb < 1024: return f"{mb:.2f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"

def _fmt_f(x: Optional[float]) -> str:
    try:
        return f"{float(x):.1f}"
    except Exception:
        return "-"

def _pick_auto_threshold_from_stats(stats: Dict[str, float], percent: int) -> Optional[float]:
    p = max(1, min(50, int(percent)))
    rounded = max(5, min(40, int(round(p / 5) * 5)))
    key = f"lap_p{rounded:02d}"
    val = stats.get(key)
    return float(val) if val is not None else None

# ------------ メインGUI ------------
class BlurCleanerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("blur_cleaner - 画像整理（ブレ/類似）")
        self.geometry("1280x720")
        self.minsize(1120, 600)

        # 状態（拡張子は固定なので GUI では扱わない）
        self.var_target   = tk.StringVar(value="")
        self.var_exclude  = tk.StringVar(value="")

        # ブレ（既定: 自動p10）
        self.var_blur_auto = tk.BooleanVar(value=True)
        self.var_blur_pct  = tk.IntVar(value=10)
        self.var_blur_thr  = tk.DoubleVar(value=400.0)

        # 内部
        self._rows_all: List[Dict[str, str]] = []
        self._task_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._last_loaded_target: Optional[str] = None

        # ブレ表
        self._iid_path_blur: Dict[str, str] = {}
        self._iid_checked_blur: Dict[str, bool] = {}
        # 類似表
        self._iid_path_vis: Dict[str, str] = {}
        self._iid_keep_vis: Dict[str, str] = {}
        self._iid_checked_vis: Dict[str, bool] = {}

        # サムネ
        self._thumbs = ThumbnailCache()
        self._preview_w_single = 480
        self._preview_h_single = 360
        self._preview_w_pair   = 440
        self._preview_h_pair   = 320

        # 進捗バー
        self.pb = None

        self._build_ui()
        self._poll_queue()

    # ---------- UI -----------
    def _build_ui(self):
        root = ttk.Frame(self); root.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 上段（右にステータス＋進捗バー、左に操作）
        ops1 = ttk.Frame(root); ops1.pack(fill=tk.X)

        right = ttk.Frame(ops1); right.pack(side=tk.RIGHT)
        self.lbl_info = ttk.Label(right, text="準備OK")
        self.lbl_info.pack(side=tk.TOP, anchor="e")
        self.pb = ttk.Progressbar(right, orient="horizontal", mode="determinate", length=240, maximum=100)
        self.pb.pack(side=tk.TOP, pady=(2,0), anchor="e")

        ttk.Label(ops1, text="対象フォルダ:").pack(side=tk.LEFT)
        ttk.Entry(ops1, textvariable=self.var_target, width=70).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(ops1, text="参照...", command=self._browse_target).pack(side=tk.LEFT, padx=4)
        ttk.Button(ops1, text="オプション…", command=self._open_options_dialog).pack(side=tk.LEFT, padx=8)
        ttk.Button(ops1, text="スキャン開始", command=self._scan_clicked).pack(side=tk.LEFT, padx=8)

        # 中段：左右に分割
        split = ttk.Panedwindow(root, orient=tk.HORIZONTAL); split.pack(fill=tk.BOTH, expand=True, pady=(8,0))

        # 左ペイン：ボタンバー + Notebook(2タブ)
        left = ttk.Frame(split); split.add(left, weight=3)
        btnbar = ttk.Frame(left); btnbar.pack(fill=tk.X, pady=(0,4))
        ttk.Label(btnbar, text="結果タブ:").pack(side=tk.LEFT)

        self.nb = ttk.Notebook(left)
        ttk.Button(btnbar, text="ブレ結果", command=lambda: self.nb.select(self.tab_blur)).pack(side=tk.LEFT, padx=(6,2))
        ttk.Button(btnbar, text="類似結果", command=lambda: self.nb.select(self.tab_vis)).pack(side=tk.LEFT, padx=2)

        self.nb.pack(fill=tk.BOTH, expand=True)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # タブ：ブレ
        self.tab_blur = ttk.Frame(self.nb); self.nb.add(self.tab_blur, text="ブレ結果")
        cols_b = ("sel", "name", "size", "score")
        self.tree_blur = ttk.Treeview(self.tab_blur, columns=cols_b, show="headings", height=18)
        self.tree_blur.heading("sel", text="✓")
        self.tree_blur.heading("name", text="ファイル名")
        self.tree_blur.heading("size", text="サイズ")
        self.tree_blur.heading("score", text="スコア（ブレ値）")
        self.tree_blur.column("sel", width=40, anchor="center", stretch=False)
        self.tree_blur.column("name", width=520, stretch=True)
        self.tree_blur.column("size", width=110, anchor="e", stretch=False)
        self.tree_blur.column("score", width=130, anchor="e", stretch=False)
        self.tree_blur.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb_b = ttk.Scrollbar(self.tab_blur, orient=tk.VERTICAL, command=self.tree_blur.yview)
        self.tree_blur.configure(yscroll=sb_b.set); sb_b.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_blur.bind("<Button-1>", lambda e: self._on_tree_click(e, mode="blur"))
        self.tree_blur.bind("<<TreeviewSelect>>", lambda e: self._on_select_row(mode="blur"))

        # タブ：類似
        self.tab_vis = ttk.Frame(self.nb); self.nb.add(self.tab_vis, text="類似結果")
        cols_v = ("sel", "name", "size", "score")
        self.tree_vis = ttk.Treeview(self.tab_vis, columns=cols_v, show="headings", height=18)
        self.tree_vis.heading("sel", text="✓")
        self.tree_vis.heading("name", text="ファイル名（候補）")
        self.tree_vis.heading("size", text="サイズ")
        self.tree_vis.heading("score", text="スコア（類似度）")
        self.tree_vis.column("sel", width=40, anchor="center", stretch=False)
        self.tree_vis.column("name", width=520, stretch=True)
        self.tree_vis.column("size", width=110, anchor="e", stretch=False)
        self.tree_vis.column("score", width=130, anchor="e", stretch=False)
        self.tree_vis.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb_v = ttk.Scrollbar(self.tab_vis, orient=tk.VERTICAL, command=self.tree_vis.yview)
        self.tree_vis.configure(yscroll=sb_v.set); sb_v.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_vis.bind("<Button-1>", lambda e: self._on_tree_click(e, mode="visual"))
        self.tree_vis.bind("<<TreeviewSelect>>", lambda e: self._on_select_row(mode="visual"))

        # 右ペイン：プレビュー
        right = ttk.Frame(split); split.add(right, weight=2)
        self.prev_title = ttk.Label(right, text="プレビュー", font=("", 10, "bold"))
        self.prev_title.pack(anchor="w", pady=(2,4))

        self.prev_single = ttk.Frame(right)
        self.canvas_single = tk.Canvas(self.prev_single, width=self._preview_w_single,
                                       height=self._preview_h_single, bg="#222", highlightthickness=0)
        self.canvas_single.pack()

        self.prev_pair = ttk.Frame(right)
        top_row = ttk.Frame(self.prev_pair); top_row.pack()
        ttk.Label(top_row, text="保持").pack(side=tk.LEFT, padx=(0, self._preview_w_pair - 40))
        ttk.Label(top_row, text="候補").pack(side=tk.LEFT)
        row = ttk.Frame(self.prev_pair); row.pack()
        self.canvas_keep = tk.Canvas(row, width=self._preview_w_pair, height=self._preview_h_pair, bg="#222", highlightthickness=0)
        self.canvas_cand = tk.Canvas(row, width=self._preview_w_pair, height=self._preview_h_pair, bg="#222", highlightthickness=0)
        self.canvas_keep.pack(side=tk.LEFT, padx=4); self.canvas_cand.pack(side=tk.LEFT, padx=4)

        # 下段：一括操作
        bottom = ttk.Frame(root); bottom.pack(fill=tk.X, pady=(8,0))
        ttk.Button(bottom, text="選択をすべて切替（表示中タブ）", command=self._toggle_all_current).pack(side=tk.LEFT)
        ttk.Button(bottom, text="ごみ箱へ送る（表示中タブの選択分）", command=self._apply_clicked).pack(side=tk.RIGHT)

        self._show_preview("none")

    # ---------- 設定 永続化 ----------
    def _load_settings_if_needed(self):
        target = self.var_target.get().strip()
        if not target or not os.path.isdir(target):
            return
        if self._last_loaded_target == target:
            return
        self._load_settings(target)

    def _load_settings(self, target_dir: str):
        cfg = _cfg_path_for(target_dir)
        if not os.path.isfile(cfg):
            self._last_loaded_target = target_dir
            self.lbl_info.config(text=f"[INFO] 設定ファイルなし（{CONFIG_NAME}）")
            return
        try:
            with open(cfg, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as e:
            self._last_loaded_target = target_dir
            self.lbl_info.config(text=f"[WARN] 設定読込失敗: {e}")
            return

        # 類似系は常時ONのため読込不要
        self.var_exclude.set(data.get("exclude", self.var_exclude.get()))
        self.var_blur_auto.set(bool(data.get("blur_auto", self.var_blur_auto.get())))
        self.var_blur_pct.set(int(data.get("blur_pct", self.var_blur_pct.get())))
        self.var_blur_thr.set(float(data.get("blur_thr", self.var_blur_thr.get())))

        self._last_loaded_target = target_dir
        self.lbl_info.config(text=f"[OK] 設定読込: {CONFIG_NAME}")

    def _save_settings(self, target_dir: str):
        if not target_dir or not os.path.isdir(target_dir):
            self.lbl_info.config(text="[WARN] 設定保存先の対象フォルダが無効です")
            return
        cfg = _cfg_path_for(target_dir)
        data = dict(
            exclude=self.var_exclude.get().strip(),
            blur_auto=bool(self.var_blur_auto.get()),
            blur_pct=int(self.var_blur_pct.get()),
            blur_thr=float(self.var_blur_thr.get()),
            # 類似系は固定のため保存しない
        )
        try:
            with open(cfg, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            self.lbl_info.config(text=f"[OK] 設定保存: {CONFIG_NAME}")
        except Exception as e:
            self.lbl_info.config(text=f"[ERROR] 設定保存失敗: {e}")

    # ---------- オプション ----------
    def _open_options_dialog(self):
        target = self.var_target.get().strip()
        if target and os.path.isdir(target):
            self._load_settings(target)

        dlg = TabbedSettingsDialog(
            self,
            target_dir=self.var_target.get(),
            include=".jpeg;.jpg;.png;.webp",  # 固定表示
            exclude=self.var_exclude.get(),
            blur_auto=self.var_blur_auto.get(),
            blur_pct=self.var_blur_pct.get(),
            blur_thr=self.var_blur_thr.get(),
        )
        if dlg.result:
            r = dlg.result
            # 反映
            self.var_exclude.set(r.get("exclude", ""))
            self.var_blur_auto.set(bool(r.get("blur_auto", True)))
            self.var_blur_pct.set(int(r.get("blur_pct", 10)))
            self.var_blur_thr.set(float(r.get("blur_thr", 400.0)))
            self.lbl_info.config(text="[OK] オプションを更新しました")

            # 保存
            target = self.var_target.get().strip()
            if target and os.path.isdir(target):
                self._save_settings(target)

    # ---------- イベント ----------
    def _browse_target(self):
        d = filedialog.askdirectory(title="対象フォルダを選択")
        if d:
            self.var_target.set(d)
            self._load_settings(d)

    def _on_tab_changed(self, event=None):
        mode = "blur" if self.nb.index(self.nb.select()) == 0 else "visual"
        self._on_select_row(mode=mode)

    # ---------- スキャン/適用 ----------
    def _scan_clicked(self):
        self._load_settings_if_needed()

        target = self.var_target.get().strip()
        if not target or not os.path.isdir(target):
            messagebox.showerror("エラー", "対象フォルダが正しくありません。")
            return
        self._start_busy("スキャン中...")
        threading.Thread(target=self._scan_job, daemon=True).start()

    def _apply_clicked(self):
        mode = "blur" if self.nb.index(self.nb.select()) == 0 else "visual"
        if mode == "blur":
            selected_paths = [self._iid_path_blur[iid] for iid, on in self._iid_checked_blur.items() if on]
        else:
            selected_paths = [self._iid_path_vis[iid] for iid, on in self._iid_checked_vis.items() if on]
        if not selected_paths:
            messagebox.showwarning("注意", "選択された項目がありません。チェックしてから実行してください。")
            return
        if not messagebox.askyesno("確認", f"選択された {len(selected_paths)} 件をごみ箱へ送ります。よろしいですか？"):
            return
        self._start_busy("適用中...")
        threading.Thread(target=self._apply_job, args=(mode, set(selected_paths)), daemon=True).start()

    # ---------- バックグラウンド ----------
    def _scan_job(self):
        try:
            target = self.var_target.get().strip()
            dbpath = _cache_path_for(target)
            exclude = [s.strip() for s in self.var_exclude.get().split(";") if s.strip()] or None

            # 進捗通知コールバック
            def _prog(phase: str, cur: int, tot: int):
                self._task_q.put(("progress", {"phase": phase, "current": cur, "total": tot}))

            # ブレ閾値（自動→統計1パス）
            thr = float(self.var_blur_thr.get())
            _rows_tmp, stats = scan(
                target_dir=target, report_csv=None, dbpath=dbpath,
                blur_threshold=1e9, do_similar=DO_SIMILAR, phash_distance=PHASH_DIST,
                include_exts=None, exclude_substr=exclude, collect_stats=True,
                progress_cb=_prog,
            ) if self.var_blur_auto.get() else (None, None)

            if self.var_blur_auto.get():
                auto_thr = _pick_auto_threshold_from_stats(stats, self.var_blur_pct.get()) if stats else None
                if auto_thr is not None:
                    thr = float(auto_thr)
                    self._task_q.put(("msg", f"[AUTO] ブレしきい値 = {thr:.1f}（下位{self.var_blur_pct.get()}%）"))
                else:
                    self._task_q.put(("msg", f"[WARN] 自動しきい値の取得に失敗。手動値 {thr:.1f} を使用します。"))

            # 本番スキャン
            rows = scan(
                target_dir=target, report_csv=None, dbpath=dbpath,
                blur_threshold=thr, do_similar=DO_SIMILAR, phash_distance=PHASH_DIST,
                include_exts=None, exclude_substr=exclude, collect_stats=False,
                progress_cb=_prog,
            )
            self._rows_all = rows

            rows_blur = [r for r in rows if r.get("type")=="blur_single" and r.get("domain")=="single"]
            rows_vis  = [r for r in rows if r.get("type")=="visual" and r.get("domain")=="group"]

            self._task_q.put(("table_blur", rows_blur))
            self._task_q.put(("table_vis", rows_vis))
            self._task_q.put(("msg", f"[OK] スキャン完了: ブレ {len(rows_blur)} 件 / 類似 {len(rows_vis)} 件"))
        except Exception as e:
            self._task_q.put(("error", f"スキャン失敗: {e}"))
        finally:
            self._task_q.put(("idle", None))

    def _apply_job(self, mode: str, selected_paths: set[str]):
        try:
            filtered: List[Dict[str, str]] = []
            if mode == "blur":
                for r in self._rows_all:
                    if r.get("type")=="blur_single" and r.get("domain")=="single":
                        if (r.get("candidate") or "") in selected_paths:
                            filtered.append(r)
            else:
                for r in self._rows_all:
                    if r.get("type")=="visual" and r.get("domain")=="group":
                        if (r.get("candidate") or "") in selected_paths:
                            filtered.append(r)

            res = apply_from_rows(filtered, only=("blur" if mode=="blur" else "visual"),
                                  protect=None, max_move=None, dry_run=False, log_dir=None)
            self._task_q.put(("msg", f"[{mode}] moved={res['moved']}, missing={res['missing']}, errors={res['errors']}"))

            if mode == "blur":
                to_remove = [iid for iid, p in self._iid_path_blur.items() if p in selected_paths]
                for iid in to_remove:
                    if self.tree_blur.exists(iid): self.tree_blur.delete(iid)
                    self._iid_checked_blur.pop(iid, None); self._iid_path_blur.pop(iid, None)
            else:
                to_remove = [iid for iid, p in self._iid_path_vis.items() if p in selected_paths]
                for iid in to_remove:
                    if self.tree_vis.exists(iid): self.tree_vis.delete(iid)
                    self._iid_checked_vis.pop(iid, None); self._iid_path_vis.pop(iid, None); self._iid_keep_vis.pop(iid, None)
        except Exception as e:
            self._task_q.put(("error", f"適用失敗: {e}"))
        finally:
            self._task_q.put(("idle", None))

    # ---------- Queue反映 ----------
    def _poll_queue(self):
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
                    if self.pb:
                        self.pb["maximum"] = 100
                        self.pb["value"] = pct
                    self.lbl_info.config(text=f"[{phase}] {cur}/{tot}  ({pct}%)")
                elif kind == "table_blur":
                    self._reload_table_blur(payload)
                elif kind == "table_vis":
                    self._reload_table_vis(payload)
                elif kind == "idle":
                    self._end_busy()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    # ---------- 表描画（ブレ） ----------
    def _reload_table_blur(self, rows_view: List[Dict[str, str]]):
        for iid in self.tree_blur.get_children(""):
            self.tree_blur.delete(iid)
        self._iid_path_blur.clear(); self._iid_checked_blur.clear()
        def lap_of(r):
            rel = r.get("relation") or ""
            try:
                if "lap_var=" in rel:
                    return float(rel.split("lap_var=")[1].split(";")[0].strip())
                for part in rel.split(";"):
                    part = part.strip()
                    if part.startswith("lap_cand="):
                        return float(part.split("=",1)[1])
            except Exception:
                return None
        rows_sorted = sorted(rows_view, key=lambda r: (lap_of(r) if lap_of(r) is not None else 1e18))
        for r in rows_sorted:
            cand = r.get("candidate") or ""
            name = os.path.basename(cand)
            try: size_b = os.path.getsize(cand)
            except Exception: size_b = 0
            size_s = _fmt_size(size_b)
            score = _fmt_f(lap_of(r))
            iid = self.tree_blur.insert("", tk.END, values=("☐", name, size_s, score))
            self._iid_path_blur[iid] = cand
            self._iid_checked_blur[iid] = False
        self._show_preview("single" if rows_sorted else "none")

    # ---------- 表描画（類似） ----------
    def _reload_table_vis(self, rows_view: List[Dict[str, str]]):
        for iid in self.tree_vis.get_children(""):
            self.tree_vis.delete(iid)
        self._iid_path_vis.clear(); self._iid_keep_vis.clear(); self._iid_checked_vis.clear()
        def dist_of(r):
            rel = r.get("relation") or ""
            try:
                for part in rel.split(";"):
                    part = part.strip()
                    if part.startswith("dist="):
                        return int(part.split("=",1)[1])
            except Exception:
                return None
        rows_sorted = sorted(rows_view, key=lambda r: (dist_of(r) if dist_of(r) is not None else 1e9))
        for r in rows_sorted:
            cand = r.get("candidate") or ""
            keep = r.get("keep") or ""
            name = os.path.basename(cand)
            try: size_b = os.path.getsize(cand)
            except Exception: size_b = 0
            size_s = _fmt_size(size_b)
            score = str(dist_of(r) if dist_of(r) is not None else "-")
            iid = self.tree_vis.insert("", tk.END, values=("☐", name, size_s, score))
            self._iid_path_vis[iid] = cand
            self._iid_keep_vis[iid] = keep
            self._iid_checked_vis[iid] = False
        self._show_preview("pair" if rows_sorted else "none")

    # ---------- チェック切替 ----------
    def _on_tree_click(self, event, mode: str):
        tree = self.tree_blur if mode=="blur" else self.tree_vis
        checked = self._iid_checked_blur if mode=="blur" else self._iid_checked_vis
        region = tree.identify("region", event.x, event.y)
        if region != "cell": return
        if tree.identify_column(event.x) != "#1": return
        iid = tree.identify_row(event.y)
        if not iid: return
        curr = checked.get(iid, False)
        checked[iid] = not curr
        tree.set(iid, "sel", "☑" if not curr else "☐")

    def _toggle_all_current(self):
        mode = "blur" if self.nb.index(self.nb.select()) == 0 else "visual"
        tree = self.tree_blur if mode=="blur" else self.tree_vis
        checked = self._iid_checked_blur if mode=="blur" else self._iid_checked_vis
        for iid in tree.get_children(""):
            curr = checked.get(iid, False)
            checked[iid] = not curr
            tree.set(iid, "sel", "☑" if not curr else "☐")

    # ---------- 選択→プレビュー ----------
    def _on_select_row(self, mode: str):
        tree = self.tree_blur if mode=="blur" else self.tree_vis
        sel = tree.selection()
        if not sel:
            self._show_preview("none"); return
        iid = sel[0]
        if mode == "blur":
            cand = self._iid_path_blur.get(iid, "")
            if cand: self._show_preview("single", single=cand)
            else: self._show_preview("none")
        else:
            cand = self._iid_path_vis.get(iid, "")
            keep = self._iid_keep_vis.get(iid, "")
            if keep and cand: self._show_preview("pair", left=keep, right=cand)
            elif cand: self._show_preview("single", single=cand)
            else: self._show_preview("none")

    # ---------- プレビュー ----------
    def _show_preview(self, mode: str, single: Optional[str]=None, left: Optional[str]=None, right: Optional[str]=None):
        for c in (getattr(self, "canvas_single", None), getattr(self, "canvas_keep", None), getattr(self, "canvas_cand", None)):
            if c: c.delete("all")
        if mode == "single" and single:
            self.prev_pair.pack_forget()
            self.prev_single.pack(anchor="n")
            img = self._thumbs.get_thumb(single, self._preview_w_single, self._preview_h_single)
            if img:
                w = self._preview_w_single; h = self._preview_h_single
                self.canvas_single.create_image(w//2, h//2, image=img)
                self.canvas_single.image = img
                self.prev_title.config(text=os.path.basename(single))
            else:
                self.prev_title.config(text=f"(表示不可) {os.path.basename(single)}")
        elif mode == "pair" and left and right:
            self.prev_single.pack_forget()
            self.prev_pair.pack(anchor="n")
            imgL = self._thumbs.get_thumb(left,  self._preview_w_pair, self._preview_h_pair)
            imgR = self._thumbs.get_thumb(right, self._preview_w_pair, self._preview_h_pair)
            if imgL:
                self.canvas_keep.create_image(self._preview_w_pair//2, self._preview_h_pair//2, image=imgL)
                self.canvas_keep.image = imgL
            if imgR:
                self.canvas_cand.create_image(self._preview_w_pair//2, self._preview_h_pair//2, image=imgR)
                self.canvas_cand.image = imgR
            self.prev_title.config(text=f"{os.path.basename(left)}   |   {os.path.basename(right)}")
        else:
            self.prev_single.pack_forget()
            self.prev_pair.pack_forget()
            self.prev_title.config(text="プレビュー")

    # ---------- Busy ----------
    def _start_busy(self, text: str):
        self.lbl_info.config(text=text)
        if self.pb:
            self.pb["maximum"] = 100
            self.pb["value"] = 0
        self.config(cursor="watch"); self.update_idletasks()

    def _end_busy(self):
        if self.pb:
            self.pb["value"] = 0
        self.config(cursor=""); self.update_idletasks()

# --------- 起動 ----------
def main():
    app = BlurCleanerGUI()
    app.mainloop()
