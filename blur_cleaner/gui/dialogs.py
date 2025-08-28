from __future__ import annotations
import os
import tkinter as tk
from tkinter import ttk
from typing import Dict, Any, Optional

__all__ = ["TabbedSettingsDialog"]

FIXED_EXT_TEXT = ".jpeg;.jpg;.png;.webp"

class TabbedSettingsDialog(tk.Toplevel):
    """
    タブ: スキャン条件 / ブレ設定
    ※ キャッシュDBは target_dir/scan_cshe に固定（表示のみ）
    ※ 拡張子は固定: jpeg / jpg / png / webp
    ※ 類似判定は常時ONのためUIは無し
    """
    def __init__(self, master,
                 target_dir: str,
                 include: str,
                 exclude: str,
                 blur_auto: bool,
                 blur_pct: int,
                 blur_thr: float,
                 # 追加（新方式）
                 and_tenengrad: bool = True,
                 ms_mode: str = "fixed",
                 ms_param: float = 25.0,
                 ten_mode: str = "fixed",
                 ten_thr: float = 800.0,
                 ten_param: float = 25.0):
        super().__init__(master)
        self.title("オプション")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        # 値
        self.var_target    = tk.StringVar(value=target_dir)
        self.var_include   = tk.StringVar(value=FIXED_EXT_TEXT)  # 固定表示のみ
        self.var_exclude   = tk.StringVar(value=exclude)
        self.var_cacheinfo = tk.StringVar(value=self._build_cache_info(target_dir))

        # 旧UI（MS側にマッピング）
        self.var_blur_auto = tk.BooleanVar(value=blur_auto)
        self.var_blur_pct  = tk.IntVar(value=blur_pct)
        self.var_blur_thr  = tk.DoubleVar(value=blur_thr)

        # 新方式
        self.var_and_ten   = tk.BooleanVar(value=and_tenengrad)
        self.var_ms_mode   = tk.StringVar(value=ms_mode if ms_mode in ("fixed","percentile","zscore") else ("percentile" if blur_auto else "fixed"))
        self.var_ms_param  = tk.DoubleVar(value=ms_param if ms_mode!="fixed" else float(blur_pct))
        self.var_ms_fixed  = tk.DoubleVar(value=blur_thr)
        self.var_ten_mode  = tk.StringVar(value=ten_mode if ten_mode in ("fixed","percentile","zscore") else "fixed")
        self.var_ten_param = tk.DoubleVar(value=ten_param)
        self.var_ten_fixed = tk.DoubleVar(value=ten_thr)

        frm = ttk.Frame(self, padding=10); frm.pack(fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(frm); nb.pack(fill=tk.BOTH, expand=True)

        # --- スキャン条件 ---
        tab_scan = ttk.Frame(nb); nb.add(tab_scan, text="スキャン条件")

        note = ttk.Label(
            tab_scan,
            text="対応拡張子：jpeg / jpg / png / webp のみ（固定）",
            foreground="gray"
        )
        note.pack(anchor="w", padx=8, pady=(8, 4))

        trow = ttk.Frame(tab_scan); trow.pack(fill=tk.X, padx=6, pady=(2,0))
        ttk.Label(trow, text="対象フォルダ:").pack(side=tk.LEFT)
        ttk.Label(trow, textvariable=self.var_target, foreground="#444").pack(side=tk.LEFT, padx=(4,0))

        sec1 = ttk.LabelFrame(tab_scan, text="拡張子（固定）")
        sec1.pack(fill=tk.X, padx=6, pady=(8,4))
        ttk.Entry(sec1, textvariable=self.var_include, width=64, state="disabled").pack(fill=tk.X, padx=8, pady=6)

        sec2 = ttk.LabelFrame(tab_scan, text="除外（パスに含む文字列を;区切り 例: thumb;backup;@eaDir）")
        sec2.pack(fill=tk.X, padx=6, pady=(4,6))
        ttk.Entry(sec2, textvariable=self.var_exclude, width=64).pack(fill=tk.X, padx=8, pady=6)

        info = ttk.LabelFrame(tab_scan, text="キャッシュ（自動）")
        info.pack(fill=tk.X, padx=6, pady=(0,8))
        ttk.Label(info, textvariable=self.var_cacheinfo, foreground="#555", justify="left").pack(fill=tk.X, padx=8, pady=6)

        # --- ブレ設定 ---
        tab_blur = ttk.Frame(nb); nb.add(tab_blur, text="ブレ設定")

        b0 = ttk.Frame(tab_blur); b0.pack(fill=tk.X, padx=6, pady=(8,2))
        ttk.Checkbutton(b0, text="AND(Tenengrad併用)", variable=self.var_and_ten).pack(side=tk.LEFT)

        # MS（多尺度）
        msf = ttk.LabelFrame(tab_blur, text="多尺度ラプラシアン（MS）しきい値")
        msf.pack(fill=tk.X, padx=6, pady=(8,4))
        r1 = ttk.Frame(msf); r1.pack(fill=tk.X, padx=6, pady=6)
        ttk.Radiobutton(r1, text="固定値", variable=self.var_ms_mode, value="fixed").pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_ms_fixed, width=10).pack(side=tk.LEFT, padx=(6,12))
        ttk.Radiobutton(r1, text="Percentile(%)", variable=self.var_ms_mode, value="percentile").pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_ms_param, width=6).pack(side=tk.LEFT, padx=(6,12))
        ttk.Radiobutton(r1, text="Z-score(α)", variable=self.var_ms_mode, value="zscore").pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_ms_param, width=6).pack(side=tk.LEFT, padx=(6,12))

        # Tenengrad
        tenf = ttk.LabelFrame(tab_blur, text="Tenengrad（勾配）しきい値")
        tenf.pack(fill=tk.X, padx=6, pady=(4,8))
        r2 = ttk.Frame(tenf); r2.pack(fill=tk.X, padx=6, pady=6)
        ttk.Radiobutton(r2, text="固定値", variable=self.var_ten_mode, value="fixed").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self.var_ten_fixed, width=10).pack(side=tk.LEFT, padx=(6,12))
        ttk.Radiobutton(r2, text="Percentile(%)", variable=self.var_ten_mode, value="percentile").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self.var_ten_param, width=6).pack(side=tk.LEFT, padx=(6,12))
        ttk.Radiobutton(r2, text="Z-score(α)", variable=self.var_ten_mode, value="zscore").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self.var_ten_param, width=6).pack(side=tk.LEFT, padx=(6,12))

        # OK/Cancel
        btns = ttk.Frame(frm); btns.pack(fill=tk.X, pady=(10,0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="キャンセル", command=self._cancel).pack(side=tk.RIGHT, padx=4)

        self._sync_blur_state()

    def _build_cache_info(self, target_dir: str) -> str:
        try:
            path = os.path.join(target_dir or "", "scan_cache")
            return f"DB: {path}（自動）"
        except Exception:
            return "DB: -"

    def _sync_blur_state(self):
        # 旧UIとの整合を保つ（MS側を代表とする）
        auto = bool(self.var_ms_mode.get() != "fixed")
        self.var_blur_auto.set(auto)

    def _ok(self):
        self.result = dict(
            include=self.var_include.get().strip(),  # 固定（互換のため返す）
            exclude=self.var_exclude.get().strip(),
            # 互換キー（旧UIの呼び出しに対応）
            blur_auto=bool(self.var_blur_auto.get()),
            blur_pct=int(self.var_blur_pct.get()),
            blur_thr=float(self.var_blur_thr.get()),
            # 新キー
            and_tenengrad=bool(self.var_and_ten.get()),
            ms_mode=self.var_ms_mode.get(),
            ms_param=float(self.var_ms_param.get()),
            ten_mode=self.var_ten_mode.get(),
            ten_thr=float(self.var_ten_fixed.get()),
            ten_param=float(self.var_ten_param.get()),
        )
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
