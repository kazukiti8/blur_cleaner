# gui.py - blur_cleaner の簡易GUI（日本語版・HTMLレポ削除）
from __future__ import annotations
import threading, queue, os, csv
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

# パッケージ内ロジック
from blur_cleaner.scan import scan
from blur_cleaner.apply import apply_from_csv

APP_TITLE = "blur_cleaner（簡易GUI）"
THUMB_SIZE = 128  # プレビューの最大辺
CSV_FIELDS = ["type","domain","group","keep","candidate","relation"]

# Combobox表示名 ↔ 内部値の対応
APPLY_MAP_VIEW2INTERNAL = {"類似（重複）": "visual", "ブレ": "blur"}
APPLY_MAP_INTERNAL2VIEW = {v:k for k,v in APPLY_MAP_VIEW2INTERNAL.items()}

class OptionsDialog(tk.Toplevel):
    """ブレ閾値 / 類似（pHash） / 距離 を設定するダイアログ"""
    def __init__(self, master, blur_th: tk.DoubleVar, similar: tk.BooleanVar, phash_d: tk.IntVar):
        super().__init__(master)
        self.title("オプション")
        self.resizable(False, False)
        self.blur_th = blur_th
        self.similar = similar
        self.phash_d = phash_d
        self.result_ok = False

        self.update_idletasks()
        self.geometry("+%d+%d" % (self.winfo_screenwidth()//2 - 150, self.winfo_screenheight()//2 - 80))

        frm = ttk.Frame(self, padding=12); frm.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(frm, text="ブレ閾値（小さいほどブレ強）:").grid(row=row, column=0, sticky="w", pady=4)
        self.ent_blur = ttk.Entry(frm, width=10)
        self.ent_blur.insert(0, f"{self.blur_th.get():.1f}")
        self.ent_blur.grid(row=row, column=1, sticky="w", padx=6)

        row += 1
        self.chk_similar = ttk.Checkbutton(frm, text="類似（pHash）を有効化", variable=self.similar)
        self.chk_similar.grid(row=row, column=0, columnspan=2, sticky="w", pady=4)

        row += 1
        ttk.Label(frm, text="距離（小さいほど厳密 目安: 6）:").grid(row=row, column=0, sticky="w", pady=4)
        self.spn_phash = tk.Spinbox(frm, from_=0, to=32, width=6)
        self.spn_phash.delete(0, tk.END); self.spn_phash.insert(0, str(int(self.phash_d.get())))
        self.spn_phash.grid(row=row, column=1, sticky="w", padx=6)

        row += 1
        btns = ttk.Frame(frm); btns.grid(row=row, column=0, columnspan=2, pady=(10,0))
        ttk.Button(btns, text="OK", command=self._on_ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="キャンセル", command=self._on_cancel).pack(side=tk.LEFT, padx=4)

        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self._on_cancel())

        self.transient(master); self.grab_set(); self.wait_window(self)

    def _on_ok(self):
        try:
            v_blur = float(self.ent_blur.get().strip())
        except ValueError:
            messagebox.showerror("入力エラー", "ブレ閾値は数値で入力してください"); return
        try:
            v_phash = int(self.spn_phash.get().strip())
        except ValueError:
            messagebox.showerror("入力エラー", "距離は整数で入力してください"); return
        self.blur_th.set(v_blur); self.phash_d.set(v_phash)
        self.result_ok = True; self.destroy()

    def _on_cancel(self):
        self.result_ok = False; self.destroy()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x700"); self.minsize(900, 600)

        # オプション（ダイアログで変更）
        self.var_blur_th = tk.DoubleVar(value=80.0)
        self.var_similar = tk.BooleanVar(value=False)
        self.var_phash_d = tk.IntVar(value=6)

        # パス類
        self.var_target = tk.StringVar(value=r"D:\tests")
        self.var_report = tk.StringVar(value=str(Path.cwd()/ "report.csv"))
        self.var_db     = tk.StringVar(value=str(Path.cwd()/ ".imgclean.db"))

        # 表示切替（内部値は固定：blur/visual、ラベルは日本語）
        self.var_view = tk.StringVar(value="blur")
        # 適用対象（Comboboxは表示名を持つ）
        self.var_only_view = tk.StringVar(value="ブレ")

        # ブレ一覧から類似（重複）に含まれる項目を隠す
        self.var_hide_visual_in_blur = tk.BooleanVar(value=True)

        self.task_q: queue.Queue = queue.Queue()
        self._thumb_cache: dict[str, ImageTk.PhotoImage] = {}

        self._build_ui()
        self.after(100, self._poll_task_queue)

    # ---------- UI ----------
    def _build_ui(self):
        # 上段：操作
        top = ttk.Frame(self); top.pack(fill=tk.X, padx=8, pady=6)

        ttk.Label(top, text="対象フォルダ:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.var_target, width=60).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="参照", command=self._choose_dir).grid(row=0, column=2, padx=2)

        ttk.Label(top, text="レポートCSV:").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.var_report, width=60).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(top, text="参照", command=self._choose_report).grid(row=1, column=2, padx=2)

        ttk.Button(top, text="オプション…", command=self._open_options).grid(row=0, column=3, padx=6)
        ttk.Button(top, text="スキャン", command=self._scan_async).grid(row=0, column=4, padx=6)

        for i in range(5): top.columnconfigure(i, weight=1 if i==1 else 0)

        # 中段：表示と検索
        mid = ttk.Frame(self); mid.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(mid, text="表示切替:").pack(side=tk.LEFT)
        ttk.Radiobutton(mid, text="ブレ", value="blur", variable=self.var_view, command=self._reload_view).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(mid, text="類似（重複）", value="visual", variable=self.var_view, command=self._reload_view).pack(side=tk.LEFT, padx=2)

        ttk.Checkbutton(mid, text="ブレ一覧から 類似（重複）に含まれる項目を隠す", 
                        variable=self.var_hide_visual_in_blur, command=self._reload_view).pack(side=tk.LEFT, padx=10)

        ttk.Label(mid, text="検索:").pack(side=tk.LEFT, padx=(20,2))
        self.var_search = tk.StringVar(value="")
        ent = ttk.Entry(mid, textvariable=self.var_search, width=30)
        ent.pack(side=tk.LEFT); ent.bind("<KeyRelease>", lambda e: self._filter_rows())

        ttk.Label(mid, text="適用:").pack(side=tk.LEFT, padx=(20,2))
        ttk.Combobox(mid, textvariable=self.var_only_view, values=list(APPLY_MAP_VIEW2INTERNAL.keys()),
                     width=12, state="readonly").pack(side=tk.LEFT)
        ttk.Button(mid, text="ごみ箱へ送る", command=self._apply_async).pack(side=tk.LEFT, padx=6)

        # 進捗
        prog = ttk.Frame(self); prog.pack(fill=tk.X, padx=8, pady=4)
        self.pb = ttk.Progressbar(prog, mode="indeterminate"); self.pb.pack(fill=tk.X)
        self.var_status = tk.StringVar(value="待機中")
        ttk.Label(prog, textvariable=self.var_status).pack(anchor="w")

        # 表（一覧）
        body = ttk.Frame(self); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        HEADERS_JP = ("種別","区分","グループ","残す","候補","理由")
        self.tree = ttk.Treeview(body, columns=CSV_FIELDS, show="headings", selectmode="extended")
        for c, jp in zip(CSV_FIELDS, HEADERS_JP):
            self.tree.heading(c, text=jp)
            self.tree.column(c, width=120 if c not in ("keep","candidate","relation") else 320, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_preview())

        # プレビュー
        right = ttk.Frame(self); right.pack(fill=tk.X, padx=8, pady=4)
        self.canvas = tk.Canvas(right, width=THUMB_SIZE*2, height=THUMB_SIZE*2, bg="#f2f2f2",
                                highlightthickness=1, highlightbackground="#ddd")
        self.canvas.pack(side=tk.LEFT)
        self.var_info = tk.StringVar(value="（プレビュー）")
        ttk.Label(right, textvariable=self.var_info).pack(side=tk.LEFT, padx=10)

        # 初回ロード
        self._load_csv_safe()

    # ---------- ボタン動作 ----------
    def _open_options(self):
        OptionsDialog(self, self.var_blur_th, self.var_similar, self.var_phash_d)
        self._info(f"設定：ブレ閾値={self.var_blur_th.get():.1f}, 類似={'ON' if self.var_similar.get() else 'OFF'}, 距離={self.var_phash_d.get()}")

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_target.get() or "C:\\")
        if d: self.var_target.set(d)

    def _choose_report(self):
        f = filedialog.askopenfilename(filetypes=[("CSV","*.csv"),("すべてのファイル","*.*")],
                                       initialdir=str(Path(self.var_report.get()).parent))
        if f: 
            self.var_report.set(f); self._load_csv_safe()

    def _scan_async(self):
        target = self.var_target.get().strip()
        report = self.var_report.get().strip()
        dbpath = self.var_db.get().strip()
        blur_th = float(self.var_blur_th.get())
        similar = bool(self.var_similar.get())
        phash_d = int(self.var_phash_d.get())

        if not target or not os.path.isdir(target):
            messagebox.showerror("エラー","対象フォルダが正しくありません"); return

        self._start_busy("スキャン中...")
        def job():
            try:
                n = scan(target_dir=target, report_csv=report, dbpath=dbpath,
                         blur_threshold=blur_th, do_similar=similar, phash_distance=phash_d)
                self.task_q.put(("info", f"[OK] 作成: {report}（{n}行）"))
                self.task_q.put(("reload", None))
            except Exception as e:
                self.task_q.put(("error", f"スキャン失敗: {e}"))
            finally:
                self.task_q.put(("idle", None))
        threading.Thread(target=job, daemon=True).start()

    def _apply_async(self):
        report = self.var_report.get().strip()
        if not os.path.isfile(report):
            messagebox.showerror("エラー","レポートCSVが見つかりません"); return
        only_view = self.var_only_view.get()
        only = APPLY_MAP_VIEW2INTERNAL.get(only_view, "blur")

        if not messagebox.askyesno("確認", f"「{only_view}」の候補を ごみ箱へ送ります。よろしいですか？"):
            return

        self._start_busy("適用中...")
        def job():
            try:
                apply_from_csv(csv_path=report, only=only)
                self.task_q.put(("info", f"[OK] 適用完了: {only_view}"))
                self.task_q.put(("reload", None))
            except Exception as e:
                self.task_q.put(("error", f"適用失敗: {e}"))
            finally:
                self.task_q.put(("idle", None))
        threading.Thread(target=job, daemon=True).start()

    # ---------- 補助 ----------
    def _start_busy(self, msg="作業中..."):
        self.var_status.set(msg); self.pb.start(10)

    def _stop_busy(self):
        self.pb.stop(); self.var_status.set("待機中")

    def _info(self, msg): 
        self.var_status.set(msg)
        self.after(3000, lambda: self.var_status.set("待機中"))

    def _error(self, msg):
        messagebox.showerror("エラー", msg)
        self.var_status.set("エラー: " + msg)

    def _poll_task_queue(self):
        try:
            while True:
                k, v = self.task_q.get_nowait()
                if k == "info": self._info(v)
                elif k == "error": self._error(v)
                elif k == "reload": self._load_csv_safe()
                elif k == "idle": self._stop_busy()
        except queue.Empty:
            pass
        self.after(100, self._poll_task_queue)

    def _load_csv_safe(self):
        path = self.var_report.get().strip()
        if not path or not os.path.isfile(path):
            self._fill_tree([]); return
        try:
            with open(path, encoding="utf-8") as fp:
                r = csv.DictReader(fp)
                rows = [row for row in r if set(CSV_FIELDS).issubset(set(r.fieldnames or []))]
            self._all_rows = rows
            self._reload_view()
            self._info(f"CSV読み込み: {len(rows)} 行")
        except Exception as e:
            self._error(f"CSV読み込み失敗: {e}")
            self._fill_tree([])

    def _reload_view(self):
        rows = getattr(self, "_all_rows", [])
        view = self.var_view.get()
        hide_visual_in_blur = self.var_hide_visual_in_blur.get()

        visual_paths = set()
        for row in rows:
            if row.get("type")=="visual" and row.get("domain")=="group":
                keep = (row.get("keep") or "").strip()
                cand = (row.get("candidate") or "").strip()
                if keep: visual_paths.add(os.path.abspath(keep))
                if cand: visual_paths.add(os.path.abspath(cand))

        if view == "blur":
            filt = []
            for row in rows:
                if row.get("type")=="blur_single" and row.get("domain")=="single":
                    cand = (row.get("candidate") or "").strip()
                    if hide_visual_in_blur and cand and os.path.abspath(cand) in visual_paths:
                        continue
                    filt.append(row)
            rows = filt
        else:
            rows = [r for r in rows if r.get("type")=="visual" and r.get("domain")=="group"]

        key = getattr(self, "var_search", tk.StringVar(value="")).get().strip().lower()
        if key:
            rows = [r for r in rows if key in (r.get("candidate","")+r.get("keep","")+r.get("group","")).lower()]
        self._fill_tree(rows)

    def _fill_tree(self, rows):
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            vals = [row.get(f,"") for f in CSV_FIELDS]
            self.tree.insert("", tk.END, values=vals)

    def _show_preview(self):
        sel = self.tree.selection()
        if not sel: return
        item = self.tree.item(sel[0])["values"]
        try:
            path = item[4] or item[3]
            self._render_thumb(path)
            self.var_info.set(path)
        except Exception:
            self.var_info.set("プレビューに失敗しました")

    def _render_thumb(self, path: str):
        if not path or not os.path.exists(path):
            self.canvas.delete("all")
            self.canvas.create_text(10,10, anchor="nw", text="ファイルがありません", fill="#666")
            return
        key = f"{path}|{THUMB_SIZE}"
        if key in self._thumb_cache:
            img = self._thumb_cache[key]
        else:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_SIZE*2-10, THUMB_SIZE*2-10))
                img = ImageTk.PhotoImage(im)
            self._thumb_cache[key] = img
        self.canvas.delete("all")
        w = self.canvas.winfo_width(); h = self.canvas.winfo_height()
        self.canvas.create_image(w//2, h//2, image=img, anchor="center")
        self.canvas.image = img  # 参照保持

if __name__ == "__main__":
    App().mainloop()
