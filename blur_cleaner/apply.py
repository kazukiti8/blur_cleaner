# apply.py - ごみ箱送り適用ロジック（日本語コメント）
from __future__ import annotations
import os, csv, datetime
from typing import Iterable, Dict, Optional, List
from .trash import to_trash  # send2trash を内包した関数想定

# スキャン結果のフィールド想定
FIELDS = ["type", "domain", "group", "keep", "candidate", "relation"]

def _iter_candidates(rows: Iterable[Dict[str, str]], only: Optional[str] = None):
    """
    適用対象ファイルのパスを列挙するジェネレータ。
    only: None → 両方, "blur" → ブレ単独のみ, "visual" → 類似（重複）のみ
    """
    for r in rows:
        t = (r.get("type") or "").strip()
        d = (r.get("domain") or "").strip()

        if only == "blur":
            if not (t == "blur_single" and d == "single"):
                continue
        elif only == "visual":
            if not (t == "visual" and d == "group"):
                continue
        else:
            # 両方（blur_single/single, visual/group）
            if not ((t == "blur_single" and d == "single") or (t == "visual" and d == "group")):
                continue

        if t == "visual":
            # 類似（重複）は keep 以外（=candidate）を削除対象
            cand = (r.get("candidate") or "").strip()
            keep = (r.get("keep") or "").strip()
            if cand and cand != keep:
                yield cand
        else:
            # ブレ単独は candidate を削除対象
            cand = (r.get("candidate") or "").strip()
            if cand:
                yield cand

def _norm_abs(path: str) -> str:
    """Windows前提：絶対パス化。大文字小文字は区別しない想定なので lower() はしない。"""
    try:
        return os.path.abspath(path)
    except Exception:
        return path

def _is_protected(target_abs: str, protect_list: Optional[List[str]]) -> bool:
    """
    保護パスに一致するかを判定。
    - ファイル一致
    - フォルダ配下（末尾セパレータ考慮）
    """
    if not protect_list:
        return False
    t = _norm_abs(target_abs)
    for p in protect_list:
        if not p:
            continue
        base = _norm_abs(p)
        if os.path.isdir(base):
            # フォルダ配下
            if t.startswith(base.rstrip("\\/") + os.sep):
                return True
        else:
            # ファイル一致
            if t == base:
                return True
    return False

def apply_from_rows(
    rows: Iterable[Dict[str, str]],
    only: Optional[str] = None,                 # "blur" / "visual" / None（両方）
    protect: Optional[List[str]] = None,        # 保護したいファイル/フォルダのリスト
    max_move: Optional[int] = None,             # 1回の実行で移動する最大件数（Noneで無制限）
    dry_run: bool = False,                      # Trueなら実際にはごみ箱へ送らずカウントのみ
    log_dir: Optional[str] = None,              # ログCSVを出力するフォルダ。Noneで出力しない
) -> Dict[str, object]:
    """
    スキャン結果（行リスト）から、ごみ箱へ送る。
    戻り値: {"moved":int, "missing":int, "errors":int, "total":int, "log_path": Optional[str]}
    """
    protect_list = protect or []
    moved = 0
    missing = 0
    errors = 0
    total = 0

    log_rows = []
    for cand in _iter_candidates(rows, only=only):
        total += 1
        ap = _norm_abs(cand)

        # 保護対象ならスキップ
        if _is_protected(ap, protect_list):
            log_rows.append({"path": ap, "result": "skip_protected"})
            continue

        # 実在チェック
        if not os.path.exists(ap):
            missing += 1
            log_rows.append({"path": ap, "result": "missing"})
            continue

        if dry_run:
            moved += 1
            log_rows.append({"path": ap, "result": "dry_run"})
        else:
            try:
                to_trash(ap)
                moved += 1
                log_rows.append({"path": ap, "result": "moved"})
            except Exception as e:
                errors += 1
                log_rows.append({"path": ap, "result": f"error:{e}"})

        if max_move is not None and moved >= max_move:
            # 上限に達したら終了
            break

    # ログ出力
    log_path = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(log_dir, f"applied_{ts}.csv")
            with open(log_path, "w", newline="", encoding="utf-8") as fp:
                w = csv.DictWriter(fp, fieldnames=["path", "result"])
                w.writeheader()
                w.writerows(log_rows)
        except Exception:
            # ログ出力に失敗しても致命ではないので握りつぶす
            log_path = None

    return {"moved": moved, "missing": missing, "errors": errors, "total": total, "log_path": log_path}

def apply_from_csv(
    csv_path: str,
    only: Optional[str] = None,
    protect: Optional[List[str]] = None,
    max_move: Optional[int] = None,
    dry_run: bool = False,
    log_dir: Optional[str] = None,
) -> Dict[str, object]:
    """
    互換用：CSVから読み込んで適用する。
    """
    with open(csv_path, encoding="utf-8") as fp:
        r = csv.DictReader(fp)
        # フィールドが足りない行はスキップ
        rows = [row for row in r if set(FIELDS).issubset(set(r.fieldnames or []))]
    return apply_from_rows(
        rows,
        only=only,
        protect=protect,
        max_move=max_move,
        dry_run=dry_run,
        log_dir=log_dir,
    )
