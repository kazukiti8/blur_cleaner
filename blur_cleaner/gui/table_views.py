from __future__ import annotations
import os
from typing import Callable, Dict, List, Optional, Tuple, Set

import tkinter as tk
import tkinter.ttk as ttk


# ---- 共通ユーティリティ ----
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


# =========================
# ブレ結果テーブル（昇順）＋ページング
# =========================
class BlurTable(tk.Frame):
    def __init__(
        self,
        master,
        on_select: Callable[[Optional[str]], None],
        on_open: Optional[Callable[[str], None]] = None,
        page_size: int = 1000,
    ):
        super().__init__(master, bg="#ffffff")

        self._on_select = on_select
        self._on_open = on_open
        self._page_size = max(100, int(page_size))
        self._rows: List[Dict[str, str]] = []
        self._page = 0

        # ページングバー
        bar = tk.Frame(self, bg="#ffffff")
        bar.pack(fill=tk.X, pady=(0, 4))
        self._lbl_pg = tk.Label(bar, text="ページ 0/0", bg="#ffffff")
        self._btn_prev = ttk.Button(bar, text="◀ 前へ", width=8, command=self.prev_page)
        self._btn_next = ttk.Button(bar, text="次へ ▶", width=8, command=self.next_page)
        self._btn_prev.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_next.pack(side=tk.LEFT)
        self._lbl_pg.pack(side=tk.RIGHT)

        # Treeview
        cols = ("sel", "name", "size", "score")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=18)
        self.tree.heading("sel", text="✓")
        self.tree.heading("name", text="ファイル名")
        self.tree.heading("size", text="サイズ")
        self.tree.heading("score", text="スコア（ブレ値）")

        self.tree.column("sel", width=60, anchor="center", stretch=False)
        self.tree.column("name", width=520, anchor="w", stretch=True)
        self.tree.column("size", width=120, anchor="e", stretch=False)
        self.tree.column("score", width=140, anchor="e", stretch=False)

        sb_y = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sb_x = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=sb_y.set, xscroll=sb_x.set)

        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        sb_x.pack(fill=tk.X)

        # 行→パス、チェック状態
        self._path: Dict[str, str] = {}
        self._checked: Dict[str, bool] = {}

        # イベント
        self.tree.bind("<Button-1>", self._on_click_checkbox)      # チェック切替
        self.tree.bind("<ButtonRelease-1>", self._on_any_click)    # ★行クリックでも強制選択→プレビュー
        self.tree.bind("<<TreeviewSelect>>", self._on_select_event)
        if on_open:
            self.tree.bind("<Double-1>", self._on_double_click)
            
        print("[DBG] BlurTable binds:",
      "Release-1=" + ("ON" if self.tree.bind("<ButtonRelease-1>") else "OFF"),
      "Select=" + ("ON" if self.tree.bind("<<TreeviewSelect>>") else "OFF"))



    # ---- 内部ヘルパ ----
    def _on_any_click(self, e: tk.Event):
        """どのセルをクリックしても選択を更新→プレビュー連動"""
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        # 既に選択されていない場合のみ更新（不要な多重発火を抑制）
        sel = self.tree.selection()
        if not sel or sel[0] != iid:
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self._on_select_event(None)

    def _on_click_checkbox(self, e: tk.Event):
        # チェック列クリック時も選択させてプレビュー更新
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        col = self.tree.identify_column(e.x)
        iid = self.tree.identify_row(e.y)
        if not iid:
            return

        if col == "#1":  # チェックボックス列
            cur = self._checked.get(iid, False)
            self._checked[iid] = not cur
            self.tree.set(iid, "sel", "☑" if not cur else "☐")
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self._on_select_event(None)

    def _on_select_event(self, e: Optional[tk.Event]):
        sel = self.tree.selection()
        path = self._path.get(sel[0]) if sel else None
        self._on_select(path)

    def _on_double_click(self, e: tk.Event):
        if not self._on_open:
            return
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        p = self._path.get(iid, "")
        if p:
            self._on_open(p)

    def _lap_of(self, r: Dict[str, str]) -> Optional[float]:
        rel = r.get("relation") or ""
        try:
            if "lap_var=" in rel:
                return float(rel.split("lap_var=")[1].split(";")[0].strip())
            for part in rel.split(";"):
                part = part.strip()
                if part.startswith("lap_cand="):
                    return float(part.split("=", 1)[1])
        except Exception:
            pass
        return None

    # ---- API ----
    def load(self, rows: List[Dict[str, str]]):
        # ブレ値の小さい順（昇順）
        self._rows = sorted(rows, key=lambda r: (self._lap_of(r) if self._lap_of(r) is not None else 1e18))
        self._page = 0
        self._render_page()

    def _render_page(self):
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self._path.clear()
        self._checked.clear()

        total = len(self._rows)
        pages = max(1, (total + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, pages - 1))
        self._lbl_pg.config(text=f"ページ {self._page + 1}/{pages}")

        start = self._page * self._page_size
        end = min(total, start + self._page_size)

        first_iid = None
        for r in self._rows[start:end]:
            p = r.get("candidate") or ""
            name = os.path.basename(p)
            try:
                size_b = os.path.getsize(p)
            except Exception:
                size_b = 0
            size_s = _fmt_size(size_b)
            lv = self._lap_of(r)
            score = f"{lv:.1f}" if lv is not None else "-"
            iid = self.tree.insert("", tk.END, values=("☐", name, size_s, score))
            if first_iid is None:
                first_iid = iid
            self._path[iid] = p
            self._checked[iid] = False

        if first_iid:
            self.tree.selection_set(first_iid)
            self.tree.see(first_iid)
            # 初回描画時にもプレビューを明示更新
            self._on_select_event(None)

        self._btn_prev.config(state=(tk.NORMAL if self._page > 0 else tk.DISABLED))
        self._btn_next.config(state=(tk.NORMAL if self._page < pages - 1 else tk.DISABLED))

    def next_page(self):
        self._page += 1
        self._render_page()

    def prev_page(self):
        self._page -= 1
        self._render_page()

    def selected_candidates(self) -> Set[str]:
        return {p for iid, p in self._path.items() if self._checked.get(iid, False)}

    def remove_by_paths(self, paths: Set[str]):
        # 現在ページ内のみ即時削除（次回レンダで整合）
        to_remove = [iid for iid, p in self._path.items() if p in paths]
        for iid in to_remove:
            if self.tree.exists(iid):
                self.tree.delete(iid)
            self._checked.pop(iid, None)
            self._path.pop(iid, None)


# =========================
# 類似結果テーブル（一致度%列・列幅少し小さく）＋ページング
# =========================
class VisualTable(tk.Frame):
    def __init__(
        self,
        master,
        on_select: Callable[[Optional[str], Optional[str]], None],
        on_open_keep: Optional[Callable[[str], None]] = None,
        on_open_cand: Optional[Callable[[str], None]] = None,
        page_size: int = 1000,
    ):
        super().__init__(master, bg="#ffffff")

        self._on_select = on_select
        self._on_open_keep = on_open_keep
        self._on_open_cand = on_open_cand
        self._page_size = max(100, int(page_size))
        self._rows: List[Dict[str, str]] = []
        self._page = 0

        # ページングバー
        bar = tk.Frame(self, bg="#ffffff")
        bar.pack(fill=tk.X, pady=(0, 4))
        self._lbl_pg = tk.Label(bar, text="ページ 0/0", bg="#ffffff")
        self._btn_prev = ttk.Button(bar, text="◀ 前へ", width=8, command=self.prev_page)
        self._btn_next = ttk.Button(bar, text="次へ ▶", width=8, command=self.next_page)
        self._btn_prev.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_next.pack(side=tk.LEFT)
        self._lbl_pg.pack(side=tk.RIGHT)

        # Treeview
        cols = ("keep_sel", "keep_name", "cand_sel", "cand_name", "similarity")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=18)

        self.tree.heading("keep_sel", text="✓(保持)")
        self.tree.heading("keep_name", text="保持ファイル名")
        self.tree.heading("cand_sel", text="✓(候補)")
        self.tree.heading("cand_name", text="候補ファイル名")
        self.tree.heading("similarity", text="一致度")

        self.tree.column("keep_sel", width=70, anchor="center", stretch=False)
        self.tree.column("keep_name", width=300, anchor="w", stretch=True)  # 少し狭め
        self.tree.column("cand_sel", width=70, anchor="center", stretch=False)
        self.tree.column("cand_name", width=300, anchor="w", stretch=True)  # 少し狭め
        self.tree.column("similarity", width=100, anchor="e", stretch=False)

        sb_y = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sb_x = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=sb_y.set, xscroll=sb_x.set)

        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        sb_x.pack(fill=tk.X)

        # 行データ保持
        self._keep: Dict[str, str] = {}
        self._cand: Dict[str, str] = {}
        self._kchk: Dict[str, bool] = {}
        self._cchk: Dict[str, bool] = {}
        self._dist: Dict[str, int] = {}

        # イベント
        self.tree.bind("<Button-1>", self._on_click_checkbox)
        self.tree.bind("<ButtonRelease-1>", self._on_any_click)  # ★ 行クリックで選択更新
        self.tree.bind("<<TreeviewSelect>>", self._on_select_event)
        self.tree.bind("<Double-1>", self._on_double_click)

        print("[DBG] BlurTable binds:",
      "Release-1=" + ("ON" if self.tree.bind("<ButtonRelease-1>") else "OFF"),
      "Select=" + ("ON" if self.tree.bind("<<TreeviewSelect>>") else "OFF"))


    # ---- 内部ヘルパ ----
    def _on_any_click(self, e: tk.Event):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        sel = self.tree.selection()
        if not sel or sel[0] != iid:
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self._on_select_event(None)

    def _on_click_checkbox(self, e: tk.Event):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        col = self.tree.identify_column(e.x)
        if col not in ("#1", "#3"):
            return
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        if col == "#1":
            cur = self._kchk.get(iid, False)
            self._kchk[iid] = not cur
            self.tree.set(iid, "keep_sel", "☑" if not cur else "☐")
        elif col == "#3":
            cur = self._cchk.get(iid, False)
            self._cchk[iid] = not cur
            self.tree.set(iid, "cand_sel", "☑" if not cur else "☐")
        # 選択も合わせて更新（プレビュー連動）
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self._on_select_event(None)

    def _on_select_event(self, e: Optional[tk.Event]):
        kp, cp = self.current_pair_paths()
        self._on_select(kp, cp)

    def _on_double_click(self, e: tk.Event):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        col = self.tree.identify_column(e.x)
        if col == "#2" and self._on_open_keep:
            p = self._keep.get(iid, "")
            if p:
                self._on_open_keep(p)
        elif col == "#4" and self._on_open_cand:
            p = self._cand.get(iid, "")
            if p:
                self._on_open_cand(p)

    def _dist_of(self, r: Dict[str, str]) -> Optional[int]:
        rel = r.get("relation") or ""
        try:
            for part in rel.split(";"):
                part = part.strip()
                if part.startswith("dist="):
                    return int(part.split("=", 1)[1])
        except Exception:
            pass
        return None

    # ---- API ----
    def load(self, rows: List[Dict[str, str]]):
        # 距離が小さい順（＝一致度が高い順）
        self._rows = sorted(rows, key=lambda r: (self._dist_of(r) if self._dist_of(r) is not None else 10**9))
        self._page = 0
        self._render_page()

    def _render_page(self):
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self._keep.clear()
        self._cand.clear()
        self._kchk.clear()
        self._cchk.clear()
        self._dist.clear()

        total = len(self._rows)
        pages = max(1, (total + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, pages - 1))
        self._lbl_pg.config(text=f"ページ {self._page + 1}/{pages}")

        start = self._page * self._page_size
        end = min(total, start + self._page_size)

        first_iid = None
        for r in self._rows[start:end]:
            kp = r.get("keep") or ""
            cp = r.get("candidate") or ""
            d = self._dist_of(r)
            sim = "-" if d is None else f"{(64 - d) * 100.0 / 64.0:.1f}%"
            iid = self.tree.insert("", tk.END, values=("☐", os.path.basename(kp), "☐", os.path.basename(cp), sim))
            if first_iid is None:
                first_iid = iid
            self._keep[iid] = kp
            self._cand[iid] = cp
            self._kchk[iid] = False
            self._cchk[iid] = False
            if d is not None:
                self._dist[iid] = d

        if first_iid:
            self.tree.selection_set(first_iid)
            self.tree.see(first_iid)
            self._on_select_event(None)

        self._btn_prev.config(state=(tk.NORMAL if self._page > 0 else tk.DISABLED))
        self._btn_next.config(state=(tk.NORMAL if self._page < pages - 1 else tk.DISABLED))

    def next_page(self):
        self._page += 1
        self._render_page()

    def prev_page(self):
        self._page -= 1
        self._render_page()

    def current_pair_paths(self) -> Tuple[Optional[str], Optional[str]]:
        sel = self.tree.selection()
        if not sel:
            return (None, None)
        iid = sel[0]
        return (self._keep.get(iid), self._cand.get(iid))

    def selected_paths(self) -> set[str]:
        out: Set[str] = set()
        for iid, on in self._kchk.items():
            if on:
                p = self._keep.get(iid, "")
                if p:
                    out.add(p)
        for iid, on in self._cchk.items():
            if on:
                p = self._cand.get(iid, "")
                if p:
                    out.add(p)
        return out

    def remove_by_paths(self, paths: set[str]):
        to_remove = []
        for iid, kp in list(self._keep.items()):
            cp = self._cand.get(iid, "")
            if kp in paths or cp in paths:
                to_remove.append(iid)
        for iid in to_remove:
            if self.tree.exists(iid):
                self.tree.delete(iid)
            self._kchk.pop(iid, None)
            self._cchk.pop(iid, None)
            self._keep.pop(iid, None)
            self._cand.pop(iid, None)
            self._dist.pop(iid, None)
