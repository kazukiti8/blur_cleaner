from __future__ import annotations
import os
import math
import tkinter as tk
import tkinter.ttk as ttk
from typing import Callable, Dict, List, Optional, Tuple, Any

# --------- 共通ユーティリティ ----------
def _fmt_size(bytes_: int) -> str:
    try:
        b = int(bytes_)
    except Exception:
        return "-"
    if b < 1024:
        return f"{b} B"
    kb = b / 1024.0
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.2f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"


# --------- ブレ結果テーブル ----------
class BlurTable(tk.Frame):
    """
    ブレ結果（昇順表示）
      列: [✓, ファイル名, サイズ, スコア]
      ・チェックは1列目クリックでトグル
      ・ダブルクリックでファイルをエクスプローラ選択表示（on_open）
      ・ページング対応
    """
    def __init__(self, master,
                 on_select: Callable[[Optional[str]], None],
                 on_open: Callable[[str], None],
                 page_size: int = 1000):
        super().__init__(master, bg="#ffffff")
        self._on_select = on_select
        self._on_open = on_open
        self._page_size = page_size

        top = tk.Frame(self, bg="#ffffff")
        top.pack(fill=tk.X, pady=(0, 4))
        tk.Label(top, text="ブレ結果（昇順）", bg="#ffffff").pack(side=tk.LEFT)
        self.lbl_page = tk.Label(top, text="ページ", bg="#ffffff")
        self.lbl_page.pack(side=tk.RIGHT)
        ttk.Button(top, text="次へ", command=self._next).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="前へ", command=self._prev).pack(side=tk.RIGHT, padx=4)

        cols = ("sel", "name", "size", "score")
        tv = ttk.Treeview(self, columns=cols, show="headings", height=18)
        self.tree = tv
        tv.heading("sel", text="✓")
        tv.heading("name", text="ファイル名")
        tv.heading("size", text="サイズ")
        tv.heading("score", text="スコア（ブレ値）")

        # 列幅固定
        tv.column("sel", width=36, anchor="center", stretch=False)
        tv.column("name", width=460, stretch=False)
        tv.column("size", width=100, anchor="e", stretch=False)
        tv.column("score", width=120, anchor="e", stretch=False)

        tv.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscroll=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        tv.bind("<<TreeviewSelect>>", self._on_sel)
        tv.bind("<ButtonRelease-1>", self._toggle_check)
        tv.bind("<Double-1>", self._on_open_file)  # ★ 追加：ダブルクリックで開く

        self._rows: List[Dict[str, Any]] = []
        self._start = 0
        self._checked: Dict[str, bool] = {}
        self._iid_path: Dict[str, str] = {}

    # ページング
    def _prev(self):
        if self._start == 0:
            return
        self._start = max(0, self._start - self._page_size)
        self._redraw()

    def _next(self):
        if self._start + self._page_size >= len(self._rows):
            return
        self._start = self._start + self._page_size
        self._redraw()

    def load(self, rows: List[Dict[str, Any]]):
        self._rows = rows or []
        self._start = 0
        self._checked.clear()
        self._iid_path.clear()
        self._redraw()

    def _redraw(self):
        tv = self.tree
        for iid in tv.get_children(""):
            tv.delete(iid)
        end = min(len(self._rows), self._start + self._page_size)
        view = self._rows[self._start:end]
        for r in view:
            p = r.get("candidate") or ""
            name = os.path.basename(p)
            try:
                size_b = os.path.getsize(p)
            except Exception:
                size_b = 0
            size_s = _fmt_size(size_b)
            # relationからブレ値
            score = "-"
            rel = str(r.get("relation") or "")
            if "lap_var=" in rel:
                try:
                    score = f"{float(rel.split('lap_var=')[1].split(';')[0]):.1f}"
                except Exception:
                    pass
            iid = tv.insert("", tk.END, values=("☐", name, size_s, score))
            self._iid_path[iid] = p
            self._checked[iid] = False
        self.lbl_page.config(
            text=f"ページ {self._start // self._page_size + 1} / {max(1, math.ceil(len(self._rows) / self._page_size))}"
        )

    def _on_sel(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            self._on_select(None)
            return
        p = self._iid_path.get(sel[0])
        self._on_select(p)

    def _toggle_check(self, e: tk.Event):
        tv = self.tree
        if tv.identify("region", e.x, e.y) != "cell":
            return
        if tv.identify_column(e.x) != "#1":
            return
        iid = tv.identify_row(e.y)
        if not iid:
            return
        cur = self._checked.get(iid, False)
        self._checked[iid] = not cur
        tv.set(iid, "sel", "☑" if not cur else "☐")

    def _on_open_file(self, _e=None):
        """ダブルクリックで OS のエクスプローラ等で開く"""
        sel = self.tree.selection()
        if not sel:
            return
        p = self._iid_path.get(sel[0])
        if p:
            try:
                self._on_open(p)
            except Exception:
                pass

    def selected_candidates(self) -> List[str]:
        return [p for iid, p in self._iid_path.items() if self._checked.get(iid, False)]

    def remove_by_paths(self, paths: List[str] | set[str]):
        to_remove = [iid for iid, p in self._iid_path.items() if p in paths]
        for iid in to_remove:
            if self.tree.exists(iid):
                self.tree.delete(iid)
            self._checked.pop(iid, None)
            self._iid_path.pop(iid, None)


# --------- 類似結果テーブル ----------
class VisualTable(tk.Frame):
    """
    類似結果（保持/候補の✓を個別に持つ）
      列: [保✓, 保持名, 候✓, 候補名, 一致度]
      ・1列目 or 3列目クリックでチェックトグル
      ・ダブルクリックした列に応じて保持/候補を開く
    """
    def __init__(self, master,
                 on_select: Callable[[Optional[str], Optional[str]], None],
                 on_open_keep: Callable[[str], None],
                 on_open_cand: Callable[[str], None],
                 page_size: int = 1000):
        super().__init__(master, bg="#ffffff")
        self._on_select = on_select
        self._open_keep = on_open_keep
        self._open_cand = on_open_cand
        self._page_size = page_size

        top = tk.Frame(self, bg="#ffffff")
        top.pack(fill=tk.X, pady=(0, 4))
        tk.Label(top, text="類似結果（一致度は右端）", bg="#ffffff").pack(side=tk.LEFT)
        self.lbl_page = tk.Label(top, text="ページ", bg="#ffffff")
        self.lbl_page.pack(side=tk.RIGHT)
        ttk.Button(top, text="次へ", command=self._next).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="前へ", command=self._prev).pack(side=tk.RIGHT, padx=4)

        cols = ("kchk", "kname", "cchk", "cname", "dist")
        tv = ttk.Treeview(self, columns=cols, show="headings", height=18)
        self.tree = tv
        tv.heading("kchk", text="保✓")
        tv.heading("kname", text="保持ファイル")
        tv.heading("cchk", text="候✓")
        tv.heading("cname", text="候補ファイル")
        tv.heading("dist", text="一致度")

        # 幅固定（名前はやや小さめ）
        tv.column("kchk", width=36, anchor="center", stretch=False)
        tv.column("kname", width=260, stretch=False)
        tv.column("cchk", width=36, anchor="center", stretch=False)
        tv.column("cname", width=260, stretch=False)
        tv.column("dist", width=80, anchor="e", stretch=False)

        tv.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscroll=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        tv.bind("<<TreeviewSelect>>", self._on_sel)
        tv.bind("<ButtonRelease-1>", self._toggle_check)
        tv.bind("<Double-1>", self._on_open_file)

        self._rows: List[Dict[str, Any]] = []
        self._start = 0
        self._kchk: Dict[str, bool] = {}
        self._cchk: Dict[str, bool] = {}
        self._iid_keep: Dict[str, str] = {}
        self._iid_cand: Dict[str, str] = {}

    # ページング
    def _prev(self):
        if self._start == 0:
            return
        self._start = max(0, self._start - self._page_size)
        self._redraw()

    def _next(self):
        if self._start + self._page_size >= len(self._rows):
            return
        self._start = self._start + self._page_size
        self._redraw()

    def load(self, rows: List[Dict[str, Any]]):
        self._rows = rows or []
        self._start = 0
        self._kchk.clear()
        self._cchk.clear()
        self._iid_keep.clear()
        self._iid_cand.clear()
        self._redraw()

    def _redraw(self):
        tv = self.tree
        for iid in tv.get_children(""):
            tv.delete(iid)
        end = min(len(self._rows), self._start + self._page_size)
        view = self._rows[self._start:end]
        for r in view:
            keep = r.get("keep") or ""
            cand = r.get("candidate") or ""
            kname = os.path.basename(keep)
            cname = os.path.basename(cand)
            # relationから距離
            dist = "-"
            rel = str(r.get("relation") or "")
            for part in rel.split(";"):
                part = part.strip()
                if part.startswith("dist="):
                    try:
                        dist = str(int(part.split("=", 1)[1]))
                    except Exception:
                        pass
            iid = tv.insert("", tk.END, values=("☐", kname, "☐", cname, dist))
            self._iid_keep[iid] = keep
            self._iid_cand[iid] = cand
            self._kchk[iid] = False
            self._cchk[iid] = False
        self.lbl_page.config(
            text=f"ページ {self._start // self._page_size + 1} / {max(1, math.ceil(len(self._rows) / self._page_size))}"
        )

    def _on_sel(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            self._on_select(None, None)
            return
        iid = sel[0]
        self._on_select(self._iid_keep.get(iid), self._iid_cand.get(iid))

    def _toggle_check(self, e: tk.Event):
        tv = self.tree
        if tv.identify("region", e.x, e.y) != "cell":
            return
        col = tv.identify_column(e.x)
        iid = tv.identify_row(e.y)
        if not iid:
            return
        if col == "#1":  # 保持✓
            cur = self._kchk.get(iid, False)
            self._kchk[iid] = not cur
            tv.set(iid, "kchk", "☑" if not cur else "☐")
        elif col == "#3":  # 候補✓
            cur = self._cchk.get(iid, False)
            self._cchk[iid] = not cur
            tv.set(iid, "cchk", "☑" if not cur else "☐")

    def _on_open_file(self, e: Optional[tk.Event] = None):
        """ダブルクリックした列に応じて保持/候補を開く"""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        col = None
        if e is not None:
            col = self.tree.identify_column(e.x)
        # 列が分かればそれに合わせる、無ければ保持優先
        if col == "#2":
            p = self._iid_keep.get(iid)
            if p:
                try:
                    self._open_keep(p)
                except Exception:
                    pass
        elif col == "#4":
            p = self._iid_cand.get(iid)
            if p:
                try:
                    self._open_cand(p)
                except Exception:
                    pass
        else:
            p = self._iid_keep.get(iid) or self._iid_cand.get(iid)
            if p:
                try:
                    self._open_keep(p)
                except Exception:
                    pass

    def selected_paths(self) -> List[str]:
        """保持✓と候補✓の両方を返す（ごみ箱対象）"""
        out: List[str] = []
        for iid, p in self._iid_keep.items():
            if self._kchk.get(iid, False):
                out.append(p)
        for iid, p in self._iid_cand.items():
            if self._cchk.get(iid, False):
                out.append(p)
        return out

    def remove_by_paths(self, paths: List[str] | set[str]):
        tv = self.tree
        to_remove = [iid for iid, p in list(self._iid_keep.items()) if p in paths]
        to_remove += [iid for iid, p in list(self._iid_cand.items()) if p in paths]
        for iid in set(to_remove):
            if tv.exists(iid):
                tv.delete(iid)
            self._kchk.pop(iid, None)
            self._cchk.pop(iid, None)
            self._iid_keep.pop(iid, None)
            self._iid_cand.pop(iid, None)

    def current_pair_paths(self) -> Tuple[Optional[str], Optional[str]]:
        sel = self.tree.selection()
        if not sel:
            return (None, None)
        iid = sel[0]
        return (self._iid_keep.get(iid), self._iid_cand.get(iid))
