#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import threading
import queue
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 既存パッケージの検出ロジックを使用
try:
    from blur_cleaner.detectors import detect_blur_paths
except Exception as e:
    raise SystemExit("blur_cleaner が見つかりません。先に `pip install -e .` を実行してください。\n" + str(e))

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

def collect_images(root: Path):
    return [p for p in root.rglob("*") if p.suffix.lower() in IMG_EXTS]

class BlurCleanerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Blur Cleaner (Multi-scale + Tenengrad AND)")
        self.geometry("980x600")
        self.minsize(900, 560)

        # 状態
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.and_ten = tk.BooleanVar(value=True)
        self.gauss_ksize = tk.IntVar(value=3)
        self.lap_ksize = tk.IntVar(value=3)
        self.agg = tk.StringVar(value="median")
        self.scales = tk.StringVar(value="1.0,0.5,0.25")
        self.max_side = tk.IntVar(value=2000)

        # 閾値（MS側）
        self.ms_mode = tk.StringVar(value="fixed")  # fixed | percentile | zscore
        self.ms_fixed = tk.DoubleVar(value=800.0)
        self.ms_param = tk.DoubleVar(value=25.0)    # percentile値 or zscoreのα

        # 閾値（TEN側）
        self.ten_mode = tk.StringVar(value="fixed")
        self.ten_fixed = tk.DoubleVar(value=800.0)
        self.ten_param = tk.DoubleVar(value=25.0)

        # 実行用
        self._worker = None
        self._q = queue.Queue()
        self._last_rows = []
        self._last_meta = {}
        self._images = []

        self._build_ui()

    def _build_ui(self):
        frm_top = ttk.LabelFrame(self, text="設定")
        frm_top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8, ipady=4)

        # 入出力
        row1 = ttk.Frame(frm_top); row1.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(row1, text="入力フォルダ:").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.input_dir, width=70).pack(side=tk.LEFT, padx=6)
        ttk.Button(row1, text="参照", command=self.browse_input).pack(side=tk.LEFT)
        ttk.Label(row1, text="   出力(ブレ移動先):").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(row1, textvariable=self.output_dir, width=30).pack(side=tk.LEFT, padx=6)
        ttk.Button(row1, text="参照", command=self.browse_output).pack(side=tk.LEFT)

        # パラメータ行
        row2 = ttk.Frame(frm_top); row2.pack(fill=tk.X, padx=6, pady=4)
        ttk.Checkbutton(row2, text="AND(Tenengrad併用)", variable=self.and_ten).pack(side=tk.LEFT)
        ttk.Label(row2, text="Gaussian ksize:").pack(side=tk.LEFT, padx=(12,0))
        ttk.Spinbox(row2, from_=0, to=15, textvariable=self.gauss_ksize, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Lap ksize:").pack(side=tk.LEFT, padx=(12,0))
        ttk.Spinbox(row2, from_=1, to=15, textvariable=self.lap_ksize, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Agg:").pack(side=tk.LEFT, padx=(12,0))
        ttk.Combobox(row2, textvariable=self.agg, values=["mean","median","max","min"], width=8, state="readonly").pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Scales:").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(row2, textvariable=self.scales, width=16).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Max side(px):").pack(side=tk.LEFT, padx=(12,0))
        ttk.Spinbox(row2, from_=0, to=4000, textvariable=self.max_side, width=7).pack(side=tk.LEFT, padx=4)

        # 閾値カード（MS/TEN）
        cards = ttk.Frame(frm_top); cards.pack(fill=tk.X, padx=6, pady=4)

        card_ms = ttk.LabelFrame(cards, text="MS(多尺度ラプラシアン) しきい値")
        card_ms.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,6))
        ms_row1 = ttk.Frame(card_ms); ms_row1.pack(fill=tk.X, padx=6, pady=4)
        ttk.Radiobutton(ms_row1, text="固定値", variable=self.ms_mode, value="fixed").pack(side=tk.LEFT)
        ttk.Entry(ms_row1, textvariable=self.ms_fixed, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ms_row1, text="Percentile(%)", variable=self.ms_mode, value="percentile").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(ms_row1, textvariable=self.ms_param, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ms_row1, text="Z-score(α)", variable=self.ms_mode, value="zscore").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(ms_row1, textvariable=self.ms_param, width=6).pack(side=tk.LEFT, padx=4)

        card_ten = ttk.LabelFrame(cards, text="Tenengrad(勾配) しきい値")
        card_ten.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6,0))
        ten_row1 = ttk.Frame(card_ten); ten_row1.pack(fill=tk.X, padx=6, pady=4)
        ttk.Radiobutton(ten_row1, text="固定値", variable=self.ten_mode, value="fixed").pack(side=tk.LEFT)
        ttk.Entry(ten_row1, textvariable=self.ten_fixed, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ten_row1, text="Percentile(%)", variable=self.ten_mode, value="percentile").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(ten_row1, textvariable=self.ten_param, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ten_row1, text="Z-score(α)", variable=self.ten_mode, value="zscore").pack(side=tk.LEFT, padx=(12,0))
        ttk.Entry(ten_row1, textvariable=self.ten_param, width=6).pack(side=tk.LEFT, padx=4)

        # 実行ボタン & 進捗
        row3 = ttk.Frame(frm_top); row3.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(row3, text="スキャン実行", command=self.run_scan).pack(side=tk.LEFT)
        ttk.Button(row3, text="ブレ画像を移動", command=self.move_blurs).pack(side=tk.LEFT, padx=(8,0))
        self.progress = ttk.Progressbar(row3, mode="indeterminate", length=220)
        self.progress.pack(side=tk.LEFT, padx=10)
        self.status_lbl = ttk.Label(row3, text="準備OK")
        self.status_lbl.pack(side=tk.LEFT, padx=6)

        # 結果テーブル
        frm_table = ttk.Frame(self); frm_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        cols = ("path", "score", "ten_score", "is_blur", "rule")
        self.tree = ttk.Treeview(frm_table, columns=cols, show="headings", height=18)
        for c, w in zip(cols, (520, 110, 110, 70, 100)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frm_table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set); vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ダブルクリックで画像を開く（既定ビューア）
        self.tree.bind("<Double-1>", self.open_selected_image)

    def browse_input(self):
        d = filedialog.askdirectory()
        if d:
            self.input_dir.set(d)

    def browse_output(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir.set(d)

    def open_selected_image(self, _evt=None):
        sel = self.tree.selection()
        if not sel: return
        p = self.tree.item(sel[0], "values")[0]
        try:
            os.startfile(p)  # Windows: 既定アプリで開く
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def run_scan(self):
        in_dir = Path(self.input_dir.get())
        if not in_dir.exists():
            messagebox.showerror("エラー", "入力フォルダが存在しません")
            return

        # 画像一覧
        self._images = collect_images(in_dir)
        if not self._images:
            messagebox.showwarning("注意", "対象画像が見つかりません")
            return

        # UIロック & 進捗
        self.status_lbl.config(text="スキャン中…")
        self.progress.start(10)
        self.tree.delete(*self.tree.get_children())

        # バックグラウンド実行
        self._worker = threading.Thread(target=self._worker_scan, daemon=True)
        self._worker.start()
        self.after(100, self._poll_q)

    def _poll_q(self):
        try:
            msg = self._q.get_nowait()
        except queue.Empty:
            if self._worker and self._worker.is_alive():
                self.after(100, self._poll_q)
            return
        finally:
            pass

        if msg["type"] == "done":
            self.progress.stop()
            self.status_lbl.config(text=f"完了: 画像{len(self._images)}件 / ブレ{msg['blur_cnt']}件")
            self._last_rows = msg["rows"]
            self._last_meta = msg["meta"]
            # テーブルに流し込み（MSスコア昇順）
            ok_rows = [r for r in self._last_rows if r.get("status") == "ok"]
            ok_rows.sort(key=lambda x: x["score"])
            for r in ok_rows:
                self.tree.insert("", tk.END, values=(r["path"], f"{r['score']:.3f}", f"{r['ten_score']:.3f}", str(r["is_blur"]), r["rule"]))
        elif msg["type"] == "error":
            self.progress.stop()
            self.status_lbl.config(text="エラー")
            messagebox.showerror("エラー", msg["err"])
        # まだ動いていれば再ポーリング
        if self._worker and self._worker.is_alive():
            self.after(100, self._poll_q)

    def _worker_scan(self):
        try:
            # 閾値設定の解釈
            def mode_to_args(mode, fixed, param):
                mode = (mode or "fixed").lower()
                if mode == "fixed":
                    return None, fixed  # auto_mode=None, fixed=値
                # auto
                return mode, None     # auto_mode="percentile"/"zscore", fixed=None

            ms_auto, ms_fixed = mode_to_args(self.ms_mode.get(), self.ms_fixed.get(), self.ms_param.get())
            ten_auto, ten_fixed = mode_to_args(self.ten_mode.get(), self.ten_fixed.get(), self.ten_param.get())

            scales = tuple(float(s.strip()) for s in self.scales.get().split(",") if s.strip())

            rows, meta = detect_blur_paths(
                self._images,
                scales=scales,
                lap_ksize=self.lap_ksize.get(),
                agg=self.agg.get(),
                gauss_ksize=self.gauss_ksize.get(),
                gauss_sigma=0.0,
                and_tenengrad=self.and_ten.get(),
                ten_ksize=3,
                threshold=ms_fixed,
                auto_th=ms_auto,
                auto_param=self.ms_param.get(),
                ten_threshold=ten_fixed,
                ten_auto_th=ten_auto,
                ten_auto_param=self.ten_param.get(),
                max_side=(self.max_side.get() if self.max_side.get() > 0 else None),
                legacy=False
            )

            blur_cnt = sum(1 for r in rows if r.get("is_blur") is True)
            self._q.put({"type": "done", "rows": rows, "meta": meta, "blur_cnt": blur_cnt})
        except Exception as e:
            self._q.put({"type": "error", "err": str(e)})

    def move_blurs(self):
        if not self._last_rows:
            messagebox.showwarning("注意", "先にスキャンを実行してください")
            return
        out = self.output_dir.get().strip()
        if not out:
            messagebox.showwarning("注意", "出力フォルダを指定してください")
            return
        outp = Path(out); outp.mkdir(parents=True, exist_ok=True)

        moved = 0
        for r in self._last_rows:
            if r.get("is_blur"):
                src = Path(r["path"])
                try:
                    dst = outp / src.name
                    src.replace(dst)
                    moved += 1
                except Exception:
                    pass
        messagebox.showinfo("移動完了", f"ブレ画像 {moved} 件を移動しました。\n出力: {outp}")

if __name__ == "__main__":
    app = BlurCleanerGUI()
    app.mainloop()
