# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import threading
import queue
import subprocess
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from ..apply import apply_from_rows
from .table_views import BlurTable, VisualTable
from .preview_panel import PreviewPanel
from .dialogs import TabbedSettingsDialog

# 新ブレ判定（あれば使う）
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
    compute_dhash_parallel,
    build_similar_pairs_bktree_hybrid,
    ssim_filter_pairs,
)
from ..cache_db import open_cache_at, CacheDB

# ===== 既定値 =====
AUTO_PCT   = 10
DEBUG_SIM  = True

# ブレ（GUIで変更可：ms_mode などはダイアログから）
DEFAULT_AND_TEN = True
DEFAULT_MS_MODE = "fixed"
DEFAULT_MS_PARAM = 25.0
DEFAULT_MS_FIXED = 800.0
DEFAULT_TEN_MODE = "fixed"
DEFAULT_TEN_PARAM = 25.0
DEFAULT_TEN_FIXED = 800.0

# 類似（GUIから変更可能）
DEFAULT_PHASH_DIST = 8
DEFAULT_DHASH_DIST = 12
DEFAULT_MNN_K      = 3
DEFAULT_SSIM_THR   = 0.88
DEFAULT_SSIM_N     = 300
DEFAULT_HSV_CORR   = 0.90

# ---- ユーティリティ（ハッシュ正規化） ----
def _as_int64(v):
    if isinstance(v, int):
        return v & 0xFFFFFFFFFFFFFFFF
    if isinstance(v, str):
        vv = v.strip().lower()
        if vv.startswith("0x"):
            vv = vv[2:]
        try:
            return int(vv, 16) & 0xFFFFFFFFFFFFFFFF
        except Exception:
            return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return int.from_bytes(v[:8], "big", signed=False)
        except Exception:
            return None
    return None

# ---- キャッシュ用ヘルパ ----
def _cache_get_cached_records(cache: CacheDB, paths: List[str]):
    try:
        return cache.get_cached_records(paths)
    except Exception:
        return {}

def _cache_upsert_blur(cache: CacheDB, rows, safe_put=None):
    try:
        if rows:
            cache.upsert_blur(rows)
    except Exception as e:
        if safe_put:
            safe_put(("msg", f"［警告］blur保存失敗: {e}"))

def _cache_get_cached_phash(cache: CacheDB, metas):
    try:
        return cache.get_cached_phash(metas)
    except Exception:
        return [p for p, _, _ in metas], {}

def _cache_get_cached_dhash(cache: CacheDB, metas):
    try:
        return cache.get_cached_dhash(metas)
    except Exception:
        return [p for p, _, _ in metas], {}

def _cache_upsert_phash(cache: CacheDB, computed: Dict[str, int], safe_put=None):
    if not cache or not computed:
        return
    items = [(p, h) for p, h in computed.items()]
    for name in ("upsert_phash", "upsert_hash", "save_phash", "set_phash", "put_phash"):
        if hasattr(cache, name):
            try:
                getattr(cache, name)(items)
                return
            except Exception as e:
                if safe_put:
                    safe_put(("msg", f"［警告］pHash保存({name})失敗: {e}"))
    if safe_put:
        safe_put(("msg", "［警告］pHash保存メソッドが見つかりません"))

def _cache_upsert_dhash(cache: CacheDB, computed: Dict[str, int], safe_put=None):
    if not cache or not computed:
        return
    try:
        cache.upsert_dhash([(p, h) for p, h in computed.items()])
    except Exception as e:
        if safe_put:
            safe_put(("msg", f"［警告］dHash保存失敗: {e}"))

class BlurCleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            s = ttk.Style(self)
            s.theme_use("vista" if "vista" in s.theme_names() else "clam")
        except Exception:
            pass

        self.title("画像整理（ブレ／類似）")
        self.geometry("1500x920")
        self.minsize(1240, 720)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.var_target = tk.StringVar(value="")

        # 設定（ブレ）
        self.opt_and_ten = DEFAULT_AND_TEN
        self.opt_ms_mode = DEFAULT_MS_MODE
        self.opt_ms_param = DEFAULT_MS_PARAM
        self.opt_ms_fixed = DEFAULT_MS_FIXED
        self.opt_ten_mode = DEFAULT_TEN_MODE
        self.opt_ten_param = DEFAULT_TEN_PARAM
        self.opt_ten_fixed = DEFAULT_TEN_FIXED

        # 設定（類似）
        self.opt_phash_dist = DEFAULT_PHASH_DIST
        self.opt_dhash_dist = DEFAULT_DHASH_DIST
        self.opt_mnn_k      = DEFAULT_MNN_K
        self.opt_ssim_thr   = DEFAULT_SSIM_THR
        self.opt_ssim_n     = DEFAULT_SSIM_N
        self.opt_hsv_corr   = DEFAULT_HSV_CORR

        self._task_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._all_rows: List[Dict[str, str]] = []
        self._cancel_ev: Optional[threading.Event] = None
        self._alive = True

        self._table_width_px = 820

        self._build_ui()
        self._poll_queue()
        self.bind("<Configure>", self._on_main_resize)

    # ---------- 追加：誤検出低減ユーティリティ ----------
    def _mutual_topk(self, pairs: List[Tuple[str,str,int]], k: int = 3) -> List[Tuple[str,str,int]]:
        from collections import defaultdict
        rank = defaultdict(list)
        for a,b,d in pairs:
            rank[a].append((b,d)); rank[b].append((a,d))
        for key in rank:
            rank[key].sort(key=lambda t: t[1])
        keep = []
        for a,b,d in pairs:
            top_a = {x for x,_ in rank[a][:k]}
            top_b = {x for x,_ in rank[b][:k]}
            if (b in top_a) and (a in top_b):
                keep.append((a,b,d))
        return keep

    def _hsv_corr_ok(self, a: str, b: str, min_corr: float = 0.90) -> bool:
        try:
            import cv2, numpy as np
            ia = cv2.imdecode(np.fromfile(a, dtype=np.uint8), cv2.IMREAD_COLOR)
            ib = cv2.imdecode(np.fromfile(b, dtype=np.uint8), cv2.IMREAD_COLOR)
            if ia is None or ib is None:
                return False
            ia = cv2.resize(ia, (256, 256), interpolation=cv2.INTER_AREA)
            ib = cv2.resize(ib, (256, 256), interpolation=cv2.INTER_AREA)
            ha = cv2.cvtColor(ia, cv2.COLOR_BGR2HSV)
            hb = cv2.cvtColor(ib, cv2.COLOR_BGR2HSV)
            hist_a = cv2.calcHist([ha],[0,1],None,[50,60],[0,180,0,256])
            hist_b = cv2.calcHist([hb],[0,1],None,[50,60],[0,180,0,256])
            import cv2 as _cv2
            _cv2.normalize(hist_a, hist_a)
            _cv2.normalize(hist_b, hist_b)
            corr = float(_cv2.compareHist(hist_a, hist_b, _cv2.HISTCMP_CORREL))
            return corr >= min_corr
        except Exception:
            return False

    # ---------- UI ----------
    def _build_ui(self):
        self.paned_main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.paned_main.pack(fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(self.paned_main, bg="#ffffff", width=340)
        self.paned_main.add(sidebar, weight=0)

        head = tk.Frame(sidebar, bg="#ffffff")
        head.pack(fill=tk.X, padx=12, pady=(12, 0))
        tk.Label(head, text="対象フォルダ", bg="#ffffff").pack(anchor="w")
        row = tk.Frame(head, bg="#ffffff")
        row.pack(fill=tk.X, pady=(4, 6))
        tk.Entry(row, textvariable=self.var_target).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="参照", command=self._choose_dir).pack(side=tk.LEFT, padx=(8, 0))

        btns = tk.Frame(sidebar, bg="#ffffff")
        btns.pack(fill=tk.X, padx=12, pady=(12, 12))
        self.btn_scan = ttk.Button(btns, text="▶ スキャン開始", command=self._scan_clicked)
        self.btn_cancel = ttk.Button(btns, text="⏹ 中止", command=self._cancel_clicked, state=tk.DISABLED)
        self.btn_apply = ttk.Button(btns, text="🗑 ごみ箱へ送る（選択）", command=self._apply_clicked)
        self.btn_scan.pack(fill=tk.X)
        self.btn_cancel.pack(fill=tk.X, pady=(8, 0))
        self.btn_apply.pack(fill=tk.X, pady=(8, 0))
        self.btn_settings = ttk.Button(btns, text="⚙ 設定…", command=self._open_settings)
        self.btn_settings.pack(fill=tk.X, pady=(8, 0))

        self.right_container = tk.Frame(self.paned_main, bg="#ffffff")
        self.paned_main.add(self.right_container, weight=1)

        self.split = ttk.Panedwindow(self.right_container, orient=tk.HORIZONTAL)
        self.split.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.center = tk.Frame(self.split, bg="#ffffff", width=self._table_width_px)
        self.center.pack_propagate(False)
        self.split.add(self.center, weight=0)

        self.nb = ttk.Notebook(self.center)
        self.nb.pack(fill=tk.BOTH, expand=True)

        # ブレ
        self.tab_blur = tk.Frame(self.nb, bg="#ffffff")
        self.nb.add(self.tab_blur, text="ブレ結果（昇順）")
        self.blur_table = BlurTable(
            self.tab_blur,
            on_select=self._on_select_blur,
            on_open=self._open_in_explorer,
            page_size=1000,
        )
        self.blur_table.pack(fill=tk.BOTH, expand=True)

        # 類似
        self.tab_vis = tk.Frame(self.nb, bg="#ffffff")
        self.nb.add(self.tab_vis, text="類似結果（ハイブリッド）")
        self.visual_table = VisualTable(
            self.tab_vis,
            on_select=self._on_select_visual,
            on_open_keep=lambda p: self._open_in_explorer(p),
            on_open_cand=lambda p: self._open_in_explorer(p),
            page_size=1000,
        )
        self.visual_table.pack(fill=tk.BOTH, expand=True)

        # 右プレビュー
        self.preview = PreviewPanel(self.split)
        self.split.add(self.preview, weight=1)

        foot = tk.Frame(self.right_container, bg="#ffffff")
        foot.pack(fill=tk.X)
        self.progress = ttk.Progressbar(foot, mode="determinate", length=260)
        self.progress.pack(side=tk.RIGHT, padx=8, pady=(0, 8))
        self.lbl_info = tk.Label(foot, text="準備完了", bg="#ffffff")
        self.lbl_info.pack(side=tk.LEFT, padx=8, pady=(0, 8))

    # ---------- イベント ----------
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
        try:
            self.progress.start(50) if busy else self.progress.stop()
        except Exception:
            pass
        self.lbl_info.config(text=msg)

    def _open_settings(self):
        d = TabbedSettingsDialog(
            self,
            target_dir=self.var_target.get().strip(),
            include="", exclude="",
            blur_auto=(self.opt_ms_mode != "fixed"),
            blur_pct=int(self.opt_ms_param),
            blur_thr=float(self.opt_ms_fixed),
            and_tenengrad=self.opt_and_ten,
            ms_mode=self.opt_ms_mode, ms_param=self.opt_ms_param,
            ten_mode=self.opt_ten_mode, ten_thr=self.opt_ten_fixed, ten_param=self.opt_ten_param,

            # ★ 類似（現値を渡す）
            init_phash_dist=self.opt_phash_dist,
            init_dhash_dist=self.opt_dhash_dist,
            init_mnn_k=self.opt_mnn_k,
            init_ssim_thr=self.opt_ssim_thr,
            init_ssim_max=self.opt_ssim_n,
            init_hsv_corr=self.opt_hsv_corr,
        )
        self.wait_window(d)
        r = getattr(d, "result", None)
        if r:
            # ブレ
            self.opt_and_ten   = bool(r.get("and_tenengrad", True))
            self.opt_ms_mode   = str(r.get("ms_mode", "fixed"))
            self.opt_ms_param  = float(r.get("ms_param", 25.0))
            self.opt_ms_fixed  = float(r.get("blur_thr", 800.0))
            self.opt_ten_mode  = str(r.get("ten_mode", "fixed"))
            self.opt_ten_param = float(r.get("ten_param", 25.0))
            self.opt_ten_fixed = float(r.get("ten_thr", 800.0))
            # 類似
            self.opt_phash_dist = int(r.get("phash_dist", self.opt_phash_dist))
            self.opt_dhash_dist = int(r.get("dhash_dist", self.opt_dhash_dist))
            self.opt_mnn_k      = int(r.get("mnn_k", self.opt_mnn_k))
            self.opt_ssim_thr   = float(r.get("ssim_thresh", self.opt_ssim_thr))
            self.opt_ssim_n     = int(r.get("ssim_maxpairs", self.opt_ssim_n))
            self.opt_hsv_corr   = float(r.get("hsv_corr", self.opt_hsv_corr))
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
        selected_paths = (
            self.blur_table.selected_candidates()
            if mode == "blur"
            else self.visual_table.selected_paths()
        )
        if not selected_paths:
            messagebox.showwarning("注意", "選択された項目がありません。")
            return
        if not messagebox.askyesno("確認", f"選択された {len(selected_paths)} 件をごみ箱へ送ります。よろしいですか？"):
            return
        self._cancel_ev = threading.Event()
        self._set_busy_state(True, "適用中...")
        threading.Thread(target=self._apply_job, args=(mode, set(selected_paths)), daemon=True).start()

    # ---------- バックグラウンド ----------
    def _scan_job(self):
        cache: Optional[CacheDB] = None
        try:
            target = self.var_target.get().strip()
            try:
                cache = open_cache_at(target); cache.begin_session()
            except Exception as e:
                self._safe_put(("msg", f"［警告］キャッシュ無効: {e}"))
                cache = None

            # 1) 列挙
            all_paths = list_image_files(target, DEFAULT_EXTS)
            if self._cancel_ev and self._cancel_ev.is_set(): return

            metas: List[Tuple[str,int,int]] = []
            for p in all_paths:
                try:
                    st = os.stat(p)
                    metas.append((p, int(st.st_mtime), int(st.st_size)))
                except Exception:
                    pass

            # 2) ブレ
            need_blur: List[str] = []
            blur_map: Dict[str, float] = {}
            if cache:
                recs = _cache_get_cached_records(cache, [p for p,_,_ in metas])
                for p, m, s in metas:
                    r = recs.get(p)
                    if (not r) or (r[0] != m or r[1] != s) or (r[2] is None):
                        need_blur.append(p)
                    else:
                        blur_map[p] = float(r[2])
            else:
                need_blur = [p for p,_,_ in metas]

            used_new_detector = False
            path_is_blur: Dict[str, bool] = {}
            path_to_ten: Dict[str, float] = {}

            if need_blur:
                if detect_blur_paths is not None:
                    used_new_detector = True
                    self._safe_put(("progress", {"phase": "スキャン(新方式)", "current": 0, "total": len(need_blur)}))

                    ms_auto = None if self.opt_ms_mode == "fixed" else self.opt_ms_mode
                    ms_fixed = None if self.opt_ms_mode != "fixed" else self.opt_ms_fixed
                    ten_auto = None if self.opt_ten_mode == "fixed" else self.opt_ten_mode
                    ten_fixed = None if self.opt_ten_mode != "fixed" else self.opt_ten_fixed

                    rows, meta = detect_blur_paths(
                        [Path(p) for p in need_blur],
                        agg="median", gauss_ksize=3, and_tenengrad=self.opt_and_ten,
                        threshold=ms_fixed, auto_th=ms_auto, auto_param=self.opt_ms_param,
                        ten_threshold=ten_fixed, ten_auto_th=ten_auto, ten_auto_param=self.opt_ten_param,
                        max_side=2000, legacy=False,
                    )
                    mlookup = {p: (m, s) for p, m, s in metas}
                    up_rows = []
                    for r in rows:
                        p = str(r.get("path") or ""); 
                        if not p: continue
                        ms = float(r.get("score") or 0.0)
                        ten = float(r.get("ten_score") or 0.0)
                        isb = bool(r.get("is_blur") is True)
                        blur_map[p] = ms
                        path_to_ten[p] = ten
                        path_is_blur[p] = isb
                        if cache and p in mlookup:
                            m, s = mlookup[p]
                            up_rows.append((p, m, s, ms))
                    _cache_upsert_blur(cache, up_rows, safe_put=self._safe_put)

                    self._safe_put(("msg", f"［しきい値］MS:{meta.get('th_ms', 0):.1f} / TEN:{meta.get('th_ten', 0):.1f} / AND:{'ON' if self.opt_and_ten else 'OFF'}"))
                else:
                    def _cb_blur(done, total):
                        self._safe_put(("progress", {"phase": "スキャン", "current": done, "total": total}))
                    comp = compute_blur_parallel(need_blur, None, _cb_blur, self._cancel_ev)
                    if self._cancel_ev and self._cancel_ev.is_set(): return
                    blur_map.update(comp)
                    if cache and comp:
                        mlookup = {p: (m, s) for p, m, s in metas}
                        rows4 = []
                        for p, v in comp.items():
                            if p in mlookup:
                                m, s = mlookup[p]
                                rows4.append((p, m, s, v))
                        _cache_upsert_blur(cache, rows4, safe_put=self._safe_put)

            # 3) ブレ表
            rows_blur: List[Dict[str, str]] = []
            for p in sorted(blur_map.keys(), key=lambda x: blur_map.get(x, 1e18)):
                v_ms = blur_map[p]; v_tn = path_to_ten.get(p, 0.0)
                rows_blur.append({"type":"blur_single","domain":"single","candidate":p,
                                  "relation":f"lap_var={v_ms:.6f}; ten={v_tn:.6f};"})
            self._safe_put(("table_blur", rows_blur))

            # 4) pHash対象
            if used_new_detector and path_is_blur:
                sharp_paths = [p for p,_,_ in metas if not path_is_blur.get(p, False)]
                self._safe_put(("msg", f"［選定］pHash対象 {len(sharp_paths)} / 全{len(metas)}"))
            else:
                vals = list(blur_map.values())
                thr = self._percentile(vals, AUTO_PCT) if vals else 0.0
                sharp_paths = [p for p,_,_ in metas if blur_map.get(p, 0.0) >= thr]
                self._safe_put(("msg", f"［自動］ブレ閾値={thr:.1f} → pHash対象 {len(sharp_paths)}"))

            if len(sharp_paths) < 2:
                extra = [p for p,_m,_s in sorted(metas, key=lambda t: blur_map.get(t[0], 0.0), reverse=True)]
                for p in extra:
                    if p not in sharp_paths: sharp_paths.append(p)
                    if len(sharp_paths) >= 2: break
                self._safe_put(("msg", f"［補助］pHash対象を {len(sharp_paths)} 件へ補充"))

            if self._cancel_ev and self._cancel_ev.is_set():
                if cache: cache.finalize_session([p for p,_,_ in metas])
                return

            # 5) pHash & dHash
            if cache:
                need_p, cached_p = _cache_get_cached_phash(cache, [t for t in metas if t[0] in sharp_paths])
                need_d, cached_d = _cache_get_cached_dhash(cache, [t for t in metas if t[0] in sharp_paths])
            else:
                need_p, cached_p = sharp_paths, {}
                need_d, cached_d = sharp_paths, {}

            def _cb_hash(done, total):
                self._safe_put(("progress", {"phase": "ハッシュ計算", "current": done, "total": total}))

            path_to_phash: Dict[str,int] = dict(cached_p)
            path_to_dhash: Dict[str,int] = dict(cached_d)

            if need_p:
                comp_p = compute_phash_parallel(need_p, None, _cb_hash, self._cancel_ev)
                if self._cancel_ev and self._cancel_ev.is_set():
                    if cache: cache.finalize_session([p for p,_,_ in metas]); return
                path_to_phash.update(comp_p)
                _cache_upsert_phash(cache, comp_p, safe_put=self._safe_put)

            if need_d:
                comp_d = compute_dhash_parallel(need_d, None, _cb_hash, self._cancel_ev)
                if self._cancel_ev and self._cancel_ev.is_set():
                    if cache: cache.finalize_session([p for p,_,_ in metas]); return
                path_to_dhash.update(comp_d)
                _cache_upsert_dhash(cache, comp_d, safe_put=self._safe_put)

            # 正規化
            norm_p, norm_d = {}, {}
            for p, h in path_to_phash.items():
                ih = _as_int64(h)
                if ih is not None: norm_p[p] = ih
            for p, h in path_to_dhash.items():
                ih = _as_int64(h)
                if ih is not None: norm_d[p] = ih
            path_to_phash, path_to_dhash = norm_p, norm_d

            # 6) 類似候補
            self._safe_put(("msg", f"［debug］phash={len(path_to_phash)} dhash={len(path_to_dhash)}"))
            def _cb_bk(done, total):
                self._safe_put(("progress", {"phase": "類似判定", "current": done, "total": total}))

            p_only: List[Tuple[str,str,int]] = []
            d_only: List[Tuple[str,str,int]] = []
            if path_to_phash:
                p_only = build_similar_pairs_bktree(path_to_phash, radius=self.opt_phash_dist,
                                                    progress_cb=_cb_bk, cancel_ev=self._cancel_ev)
            if path_to_dhash:
                d_only = build_similar_pairs_bktree(path_to_dhash, radius=self.opt_dhash_dist,
                                                    progress_cb=_cb_bk, cancel_ev=self._cancel_ev)

            pairs = []
            if path_to_phash or path_to_dhash:
                pairs = build_similar_pairs_bktree_hybrid(
                    path_to_phash, path_to_dhash,
                    radius_p=self.opt_phash_dist, radius_d=self.opt_dhash_dist,
                    progress_cb=_cb_bk, cancel_ev=self._cancel_ev
                )
            pre_hybrid = len(pairs)

            # ---- 3段絞り（MNN → SSIM → HSV）----
            pairs = self._mutual_topk(pairs, k=self.opt_mnn_k)
            pre_ssim = len(pairs)
            try:
                pairs = ssim_filter_pairs(pairs, max_pairs=self.opt_ssim_n, thresh=self.opt_ssim_thr)
            except Exception:
                pass
            pre_hsv = len(pairs)
            filtered = []
            for a,b,d in pairs:
                if self._hsv_corr_ok(a, b, min_corr=self.opt_hsv_corr):
                    filtered.append((a,b,d))
            pairs = filtered

            if DEBUG_SIM:
                self._safe_put(("msg", f"［debug］p_only={len(p_only)} / d_only={len(d_only)} / hybrid={pre_hybrid} / "
                                       f"postMNN={pre_ssim} / postSSIM={pre_hsv} / postHSV={len(pairs)}"))

            # 表示へ
            rows_vis: List[Dict[str,str]] = []
            for a, b, dist in pairs:
                keep, cand = (a, b) if blur_map.get(a, 0.0) >= blur_map.get(b, 0.0) else (b, a)
                rows_vis.append({
                    "type":"visual","domain":"group","keep":keep,"candidate":cand,
                    "relation":f"dist={dist}; lap_keep={blur_map.get(keep,0.0):.6f}; lap_cand={blur_map.get(cand,0.0):.6f};"
                })

            self._all_rows = rows_blur + rows_vis
            self._safe_put(("table_vis", rows_vis))
            self._safe_put(("msg", f"［完了］スキャン: ブレ {len(rows_blur)} 件 / 類似 {len(rows_vis)} 件"))

            if cache:
                try:
                    cache.finalize_session([p for p,_,_ in metas], purge_deleted=True)
                except Exception:
                    pass

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
            filtered: List[Dict[str,str]] = []
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
            if mode == "blur":
                self.blur_table.remove_by_paths(selected_paths)
            else:
                self.visual_table.remove_candidates(selected_paths)
        except Exception as e:
            self._safe_put(("error", f"適用失敗: {e}"))
        finally:
            self._safe_put(("idle", None))

    # ---------- Queue / プレビュー ----------
    def _poll_queue(self):
        if not self._alive: return
        try:
            tag, payload = self._task_q.get_nowait()
        except queue.Empty:
            self.after(60, self._poll_queue); return

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
        try:
            self._task_q.put_nowait(item)
        except Exception:
            pass

    def _on_select_blur(self, path: Optional[str]):
        if path and os.path.isfile(path):
            self.preview.show_single(path)
        else:
            self.preview.clear()

    def _on_select_visual(self, keep: Optional[str], cand: Optional[str]):
        k = keep if (keep and os.path.isfile(keep)) else None
        c = cand if (cand and os.path.isfile(cand)) else None
        if k and c:
            self.preview.show_pair(k, c)
        elif k:
            self.preview.show_single(k)
        elif c:
            self.preview.show_single(c)
        else:
            self.preview.clear()

    @staticmethod
    def _percentile(values: List[float], p: int) -> float:
        if not values: return 0.0
        v = sorted(values)
        p = max(0, min(100, int(p)))
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
        if getattr(self, "_cancel_ev", None) and not self._cancel_ev.is_set():
            try: self._cancel_ev.set()
            except Exception: pass
        try:
            if hasattr(self, "preview"): self.preview.shutdown()
        except Exception:
            pass
        self._alive = False
        try: self.destroy()
        except Exception: pass


def main():
    app = BlurCleanerApp()
    app.mainloop()
