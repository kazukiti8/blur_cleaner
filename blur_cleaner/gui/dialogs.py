# -*- coding: utf-8 -*-
from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from typing import Optional, Dict, Any


class LabeledEntry(ttk.Frame):
    def __init__(self, master, text: str, width: int = 8, initial: str = ""):
        super().__init__(master)
        ttk.Label(self, text=text).pack(side=tk.LEFT, padx=(0, 8))
        self.var = tk.StringVar(value=initial)
        e = ttk.Entry(self, textvariable=self.var, width=width)
        e.pack(side=tk.LEFT, fill=tk.X, expand=False)
        self.entry = e

    def get_str(self) -> str:
        return self.var.get()

    def get_float(self, default: float) -> float:
        v = self.get_str().strip()
        try:
            return float(v)
        except Exception:
            return default

    def get_int(self, default: int) -> int:
        v = self.get_str().strip()
        try:
            return int(v)
        except Exception:
            return default


class LabeledSpin(ttk.Frame):
    def __init__(self, master, text: str, from_: int, to: int, initial: int):
        super().__init__(master)
        ttk.Label(self, text=text).pack(side=tk.LEFT, padx=(0, 8))
        self.var = tk.IntVar(value=initial)
        sp = ttk.Spinbox(self, from_=from_, to=to, textvariable=self.var, width=6)
        sp.pack(side=tk.LEFT)
        self.spin = sp

    def get_int(self, default: Optional[int] = None) -> int:
        """
        例外時は default があればそれ、なければ 0 を返す。
        既存コードで get_int(25) のように呼んでいる箇所に対応。
        """
        try:
            return int(self.var.get())
        except Exception:
            try:
                return int(self.spin.get())
            except Exception:
                return int(default) if default is not None else 0


class LabeledScale(ttk.Frame):
    def __init__(self, master, text: str, from_: float, to: float, resolution: float, initial: float, fmt="{:.2f}"):
        super().__init__(master)
        ttk.Label(self, text=text).pack(side=tk.LEFT, padx=(0, 8))
        self.var = tk.DoubleVar(value=initial)
        self.scale = ttk.Scale(self, from_=from_, to=to, orient="horizontal", variable=self.var)
        self.scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        self.lbl = ttk.Label(self, text=fmt.format(initial))
        self.lbl.pack(side=tk.LEFT)
        self.resolution = resolution
        self.fmt = fmt
        self.scale.bind("<B1-Motion>", self._on_change)
        self.scale.bind("<ButtonRelease-1>", self._on_change)

    def _on_change(self, _e=None):
        v = float(self.var.get())
        r = self.resolution
        v = round(v / r) * r
        self.var.set(v)
        self.lbl.config(text=self.fmt.format(v))

    def get_float(self) -> float:
        return float(self.var.get())


class TabbedSettingsDialog(tk.Toplevel):
    """
    設定ダイアログ（ブレ+類似）
    """
    def __init__(
        self,
        master,
        *,
        target_dir: str = "",
        include: str = "",
        exclude: str = "",
        blur_auto: bool = False,
        blur_pct: int = 25,
        blur_thr: float = 800.0,
        and_tenengrad: bool = True,
        ms_mode: str = "fixed",
        ms_param: float = 25.0,
        ten_mode: str = "fixed",
        ten_thr: float = 800.0,
        ten_param: float = 25.0,

        # 類似（初期値）
        init_phash_dist: int = 8,
        init_dhash_dist: int = 12,
        init_mnn_k:     int = 3,
        init_ssim_thr:  float = 0.88,
        init_ssim_max:  int = 300,
        init_hsv_corr:  float = 0.90,
    ):
        super().__init__(master)
        self.title("オプション")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.result: Optional[Dict[str, Any]] = None

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # --- タブ1：ブレ ---
        tab_blur = ttk.Frame(nb)
        nb.add(tab_blur, text="ブレ判定")

        frm_b1 = ttk.Labelframe(tab_blur, text="多尺度ラプラシアン")
        frm_b1.pack(fill=tk.X, padx=8, pady=8)

        self.var_ms_mode = tk.StringVar(value=ms_mode)  # "fixed" / "percentile" / "zscore"
        self.var_and_ten = tk.BooleanVar(value=and_tenengrad)

        row1 = ttk.Frame(frm_b1); row1.pack(fill=tk.X, padx=8, pady=4)
        ttk.Radiobutton(row1, text="固定しきい値", value="fixed", variable=self.var_ms_mode).pack(side=tk.LEFT)
        ttk.Radiobutton(row1, text="自動: パーセンタイル", value="percentile", variable=self.var_ms_mode).pack(side=tk.LEFT, padx=(12,0))
        ttk.Radiobutton(row1, text="自動: z-score", value="zscore", variable=self.var_ms_mode).pack(side=tk.LEFT, padx=(12,0))

        row2 = ttk.Frame(frm_b1); row2.pack(fill=tk.X, padx=8, pady=4)
        self.ent_ms_thr  = LabeledEntry(row2, "固定しきい値(例:800)", width=8, initial=f"{blur_thr:.1f}")
        self.ent_ms_thr.pack(side=tk.LEFT)
        self.ent_ms_pct  = LabeledSpin(row2, "自動: パーセンタイル(%)", from_=1, to=50, initial=int(ms_param))
        self.ent_ms_pct.pack(side=tk.LEFT, padx=(16,0))

        frm_b2 = ttk.Labelframe(tab_blur, text="Tenengrad（AND条件で厳しめ）")
        frm_b2.pack(fill=tk.X, padx=8, pady=8)

        self.var_ten_mode = tk.StringVar(value=ten_mode)
        row3 = ttk.Frame(frm_b2); row3.pack(fill=tk.X, padx=8, pady=4)
        ttk.Checkbutton(row3, text="Tenengradを併用（AND）", variable=self.var_and_ten).pack(side=tk.LEFT)
        ttk.Radiobutton(row3, text="固定", value="fixed", variable=self.var_ten_mode).pack(side=tk.LEFT, padx=(24,0))
        ttk.Radiobutton(row3, text="自動: パーセンタイル", value="percentile", variable=self.var_ten_mode).pack(side=tk.LEFT, padx=(12,0))
        ttk.Radiobutton(row3, text="自動: z-score", value="zscore", variable=self.var_ten_mode).pack(side=tk.LEFT, padx=(12,0))

        row4 = ttk.Frame(frm_b2); row4.pack(fill=tk.X, padx=8, pady=4)
        self.ent_ten_thr  = LabeledEntry(row4, "固定しきい値(例:800)", width=8, initial=f"{ten_thr:.1f}")
        self.ent_ten_thr.pack(side=tk.LEFT)
        self.ent_ten_pct  = LabeledSpin(row4, "自動: パーセンタイル(%)", from_=1, to=50, initial=int(ten_param))
        self.ent_ten_pct.pack(side=tk.LEFT, padx=(16,0))

        # --- タブ2：類似 ---
        tab_sim = ttk.Frame(nb)
        nb.add(tab_sim, text="類似判定")

        frm_h = ttk.Labelframe(tab_sim, text="距離（ハッシュ半径）")
        frm_h.pack(fill=tk.X, padx=8, pady=(8,4))
        self.sp_phash = LabeledSpin(frm_h, "pHash 半径", from_=2, to=32, initial=init_phash_dist)
        self.sp_phash.pack(side=tk.LEFT, padx=8, pady=4)
        self.sp_dhash = LabeledSpin(frm_h, "dHash 半径", from_=2, to=32, initial=init_dhash_dist)
        self.sp_dhash.pack(side=tk.LEFT, padx=8, pady=4)

        frm_m = ttk.Labelframe(tab_sim, text="MNN（相互トップK）")
        frm_m.pack(fill=tk.X, padx=8, pady=(4,4))
        self.sp_mnnk = LabeledSpin(frm_m, "K", from_=1, to=10, initial=init_mnn_k)
        self.sp_mnnk.pack(side=tk.LEFT, padx=8, pady=4)

        frm_s = ttk.Labelframe(tab_sim, text="SSIM 最終フィルタ")
        frm_s.pack(fill=tk.X, padx=8, pady=(4,4))
        self.sc_ssim = LabeledScale(frm_s, "SSIM閾値", 0.70, 0.98, 0.01, initial=init_ssim_thr, fmt="{:.2f}")
        self.sc_ssim.pack(fill=tk.X, padx=8, pady=4)
        self.sp_ssim_n = LabeledSpin(frm_s, "SSIM検査件数(上位N)", from_=20, to=1000, initial=init_ssim_max)
        self.sp_ssim_n.pack(side=tk.LEFT, padx=8, pady=4)

        frm_c = ttk.Labelframe(tab_sim, text="色ヒスト相関（HSV）")
        frm_c.pack(fill=tk.X, padx=8, pady=(4,8))
        self.sc_hsv = LabeledScale(frm_c, "相関の下限", 0.70, 0.99, 0.01, initial=init_hsv_corr, fmt="{:.2f}")
        self.sc_hsv.pack(fill=tk.X, padx=8, pady=4)

        # --- ボタン列 ---
        row_btn = ttk.Frame(self)
        row_btn.pack(fill=tk.X, padx=12, pady=(0,12))
        ttk.Button(row_btn, text="OK", command=self._ok).pack(side=tk.RIGHT, padx=(8,0))
        ttk.Button(row_btn, text="キャンセル", command=self._cancel).pack(side=tk.RIGHT)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())

        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())
        self.focus_set()

    def _ok(self):
        self.result = {
            # ブレ
            "ms_mode": self.var_ms_mode.get(),
            "ms_param": float(self.ent_ms_pct.get_int(25)),
            "blur_thr": float(self.ent_ms_thr.get_float(800.0)),
            "and_tenengrad": bool(self.var_and_ten.get()),
            "ten_mode": self.var_ten_mode.get(),
            "ten_param": float(self.ent_ten_pct.get_int(25)),
            "ten_thr": float(self.ent_ten_thr.get_float(800.0)),

            # 類似
            "phash_dist": int(self.sp_phash.get_int(8)),
            "dhash_dist": int(self.sp_dhash.get_int(12)),
            "mnn_k": int(self.sp_mnnk.get_int(3)),
            "ssim_thresh": float(self.sc_ssim.get_float()),
            "ssim_maxpairs": int(self.sp_ssim_n.get_int(300)),
            "hsv_corr": float(self.sc_hsv.get_float()),
        }
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
