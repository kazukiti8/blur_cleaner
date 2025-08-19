from __future__ import annotations
import os
import tkinter as tk
from tkinter import ttk
from typing import Dict, Any, Optional

__all__ = ["TabbedSettingsDialog"]

class TabbedSettingsDialog(tk.Toplevel):
    """
    タブ: スキャン条件 / ブレ設定 / 類似設定
    ※ キャッシュDBは target_dir/scan_cshe に固定（表示のみ）
    """
    def __init__(self, master,
                 target_dir: str,
                 include: str, exclude: str,
                 blur_auto: bool, blur_pct: int, blur_thr: float,
                 visual_enabled: bool, phash_dist: int):
        super().__init__(master)
        self.title("オプション")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        # 値
        self.var_target    = tk.StringVar(value=target_dir)
        self.var_include   = tk.StringVar(value=include)
        self.var_exclude   = tk.StringVar(value=exclude)
        self.var_cacheinfo = tk.StringVar(value=self._build_cache_info(target_dir))

        self.var_blur_auto = tk.BooleanVar(value=blur_auto)
        self.var_blur_pct  = tk.IntVar(value=blur_pct)
        self.var_blur_thr  = tk.DoubleVar(value=blur_thr)

        self.var_visual    = tk.BooleanVar(value=visual_enabled)
        self.var_phash_d   = tk.IntVar(value=phash_dist)

        frm = ttk.Frame(self, padding=10); frm.pack(fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(frm); nb.pack(fill=tk.BOTH, expand=True)

        # --- スキャン条件 ---
        tab_scan = ttk.Frame(nb); nb.add(tab_scan, text="スキャン条件")
        trow = ttk.Frame(tab_scan); trow.pack(fill=tk.X, padx=6, pady=(6,0))
        ttk.Label(trow, text="対象フォルダ:").pack(side=tk.LEFT)
        ttk.Label(trow, textvariable=self.var_target, foreground="#444").pack(side=tk.LEFT, padx=(4,0))

        sec1 = ttk.LabelFrame(tab_scan, text="拡張子（;区切り 例: .jpg;.png;.jpeg）")
        sec1.pack(fill=tk.X, padx=6, pady=(8,4))
        ttk.Entry(sec1, textvariable=self.var_include, width=64).pack(fill=tk.X, padx=8, pady=6)

        sec2 = ttk.LabelFrame(tab_scan, text="除外（パスに含む文字列を;区切り 例: thumb;backup;@eaDir）")
        sec2.pack(fill=tk.X, padx=6, pady=(4,6))
        ttk.Entry(sec2, textvariable=self.var_exclude, width=64).pack(fill=tk.X, padx=8, pady=6)

        info = ttk.LabelFrame(tab_scan, text="キャッシュ（自動）")
        info.pack(fill=tk.X, padx=6, pady=(0,8))
        ttk.Label(info, textvariable=self.var_cacheinfo, foreground="#555", justify="left").pack(fill=tk.X, padx=8, pady=6)

        pres = ttk.Frame(tab_scan); pres.pack(fill=tk.X, padx=6, pady=(0,6))
        ttk.Label(pres, text="プリセット:").pack(side=tk.LEFT)
        ttk.Button(pres, text="写真（JPG/PNG）", command=self._preset_photo).pack(side=tk.LEFT, padx=4)
        ttk.Button(pres, text="写真+RAW", command=self._preset_photo_raw).pack(side=tk.LEFT, padx=4)
        ttk.Button(pres, text="除外:サムネ/バックアップ", command=self._preset_exclude).pack(side=tk.LEFT, padx=4)

        # --- ブレ設定 ---
        tab_blur = ttk.Frame(nb); nb.add(tab_blur, text="ブレ設定")
        b1 = ttk.Frame(tab_blur); b1.pack(fill=tk.X, padx=6, pady=(8,2))
        ttk.Checkbutton(b1, text="自動（下位 % をしきい値）", variable=self.var_blur_auto,
                        command=lambda: self._sync_blur_state()).pack(side=tk.LEFT)
        ttk.Spinbox(b1, from_=1, to=50, textvariable=self.var_blur_pct, width=6).pack(side=tk.LEFT, padx=(6,2))
        ttk.Label(b1, text="%").pack(side=tk.LEFT)
        b2 = ttk.Frame(tab_blur); b2.pack(fill=tk.X, padx=6, pady=(8,6))
        ttk.Label(b2, text="手動しきい値:").pack(side=tk.LEFT)
        self.ent_thr = ttk.Entry(b2, textvariable=self.var_blur_thr, width=10)
        self.ent_thr.pack(side=tk.LEFT, padx=(6,0))
        ttk.Label(tab_blur, foreground="#555",
                  text="※ 自動ON: データの下位%（ブレが強い側）で自動設定。OFF: 上の値を使用。値が小さいほどブレ扱い。"
        ).pack(fill=tk.X, padx=8, pady=(4,8))

        # --- 類似設定 ---
        tab_vis = ttk.Frame(nb); nb.add(tab_vis, text="類似設定")
        v1 = ttk.Frame(tab_vis); v1.pack(fill=tk.X, padx=6, pady=(8,2))
        ttk.Checkbutton(v1, text="類似判定を有効化（pHash）", variable=self.var_visual).pack(side=tk.LEFT)
        v2 = ttk.Frame(tab_vis); v2.pack(fill=tk.X, padx=6, pady=(8,6))
        ttk.Label(v2, text="pHash距離（小さいほど厳密）:").pack(side=tk.LEFT)
        ttk.Spinbox(v2, from_=0, to=32, textvariable=self.var_phash_d, width=6).pack(side=tk.LEFT, padx=(6,0))
        ttk.Label(tab_vis, foreground="#555",
                  text="※ 距離0はほぼ同一。6前後が実用ライン。大きすぎると誤検出が増える。"
        ).pack(fill=tk.X, padx=8, pady=(4,8))

        # --- ボタン ---
        btns = ttk.Frame(frm); btns.pack(fill=tk.X, pady=(10,0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="キャンセル", command=self._cancel).pack(side=tk.RIGHT)

        self.result: Optional[Dict[str, Any]] = None
        self._sync_blur_state()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.wait_visibility(); self.focus_set()

    def _build_cache_info(self, target_dir: str) -> str:
        if not target_dir:
            return "キャッシュ先: （対象フォルダ未選択）\nファイル名: scan_cshe"
        return f"キャッシュ先: {os.path.join(target_dir, 'scan_cshe')}\nファイル名: scan_cshe（SQLite）\n※ 削除しても再スキャンで自動再生成されます。"

    def _preset_photo(self): self.var_include.set(".jpg;.jpeg;.png")
    def _preset_photo_raw(self): self.var_include.set(".jpg;.jpeg;.png;.heic;.heif;.arw;.cr2;.nef")
    def _preset_exclude(self):
        base = self.var_exclude.get().strip()
        add = "thumb;thumbnails;backup;_bak;@eaDir;.AppleDouble"
        self.var_exclude.set(add if not base else (base + ";" + add))

    def _sync_blur_state(self):
        self.ent_thr.configure(state=("disabled" if self.var_blur_auto.get() else "normal"))

    def _ok(self):
        self.result = dict(
            include=self.var_include.get().strip(),
            exclude=self.var_exclude.get().strip(),
            blur_auto=bool(self.var_blur_auto.get()),
            blur_pct=int(self.var_blur_pct.get()),
            blur_thr=float(self.var_blur_thr.get()),
            visual=bool(self.var_visual.get()),
            phash_d=int(self.var_phash_d.get()),
        )
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
