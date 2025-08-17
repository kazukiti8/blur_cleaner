from __future__ import annotations
import csv, os, datetime
from typing import Optional, Set, Iterable
from .trash import to_trash

REQUIRED_FIELDS = {"type","domain","group","keep","candidate","relation"}

def _exists(p: str) -> bool:
    try:
        return os.path.exists(p)
    except Exception:
        return False

def _norm(p: str) -> str:
    # Windows想定：大文字小文字・区切りを正規化
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    except Exception:
        return p

def _parse_list(s: Optional[str]) -> list[str]:
    # セミコロン/カンマ/改行で区切り
    if not s: return []
    parts = []
    for token in s.replace(",", ";").split(";"):
        t = token.strip()
        if t:
            parts.append(t)
    return parts

def _in_protect(cand: str, protect_paths: Iterable[str]) -> bool:
    c = _norm(cand)
    for base in protect_paths:
        nb = _norm(base)
        # パス一致 or サブパスなら保護
        if c == nb: return True
        if c.startswith(nb + os.sep): return True
    return False

def _open_log(csv_src_path: str, log_dir: Optional[str]) -> tuple[csv.writer, any, str]:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = (log_dir if log_dir else os.path.dirname(os.path.abspath(csv_src_path))) or "."
    os.makedirs(base_dir, exist_ok=True)
    out_path = os.path.join(base_dir, f"applied_{ts}.csv")
    fp = open(out_path, "w", newline="", encoding="utf-8")
    w = csv.writer(fp)
    w.writerow(["ts","mode","status","type","domain","group","keep","candidate","relation","note"])
    return w, fp, out_path

def apply_from_csv(
    csv_path: str = "report.csv",
    only: Optional[str] = None,
    protect: Optional[str] = None,
    max_move: Optional[int] = None,
    confirm: bool = False,
    dry_run: bool = False,
    log_dir: Optional[str] = None,
):
    """
    only:
      - None       : visual と blur の両方を適用（※慎重に）
      - "visual"   : 見た目グループ（duplicate+similar）だけ適用
      - "blur"     : ブレ単独だけ適用
    protect: セミコロン(;)区切りの保護パス（フォルダ/ファイル）。配下はスキップ
    max_move: この実行でごみ箱へ送る上限枚数（超えたら以降はスキップ）
    confirm: True の場合、実行前に枚数を表示して Y/N 確認
    dry_run: True の場合、実際には移動せずログだけ出力
    log_dir: 実行ログCSVを書き出すフォルダ（未指定なら report.csv と同じ場所）
    """
    # 1) CSVスキーマ確認
    with open(csv_path, encoding="utf-8") as fp:
        r = csv.DictReader(fp)
        fields = set(r.fieldnames or [])
        if not REQUIRED_FIELDS.issubset(fields):
            raise ValueError("report.csv の列が新仕様と異なります。scanを再実行してください。")

    # 2) 候補の事前スキャン（件数把握）
    def collect_targets(kind: str):
        items = []
        with open(csv_path, encoding="utf-8") as fp:
            r = csv.DictReader(fp)
            for row in r:
                t = row.get("type"); d = row.get("domain")
                if kind == "visual":
                    if t == "visual" and d == "group":
                        keep = (row.get("keep") or "").strip()
                        cand = (row.get("candidate") or "").strip()
                        if cand and cand != keep:
                            items.append(row)
                elif kind == "blur":
                    if t == "blur_single" and d == "single":
                        cand = (row.get("candidate") or "").strip()
                        if cand:
                            items.append(row)
        return items

    kinds = []
    if only in (None, "visual"): kinds.append("visual")
    if only in (None, "blur"): kinds.append("blur")

    targets_by_kind = {k: collect_targets(k) for k in kinds}
    total_targets = sum(len(v) for v in targets_by_kind.values())

    # 3) 確認
    if confirm:
        print(f"[PLAN] target files: {total_targets} (visual={len(targets_by_kind.get('visual',[]))}, blur={len(targets_by_kind.get('blur',[]))})")
        ans = input("Proceed? [y/N]: ").strip().lower()
        if ans not in ("y","yes"):
            print("[CANCELLED] no action taken.")
            return

    # 4) ログ準備
    writer, fp_log, log_path = _open_log(csv_path, log_dir)
    mode = "dry-run" if dry_run else "apply"
    prot_list = [_norm(p) for p in _parse_list(protect)]
    moved = {"visual":0, "blur":0}
    missing = {"visual":0, "blur":0}
    errors = {"visual":0, "blur":0}
    protected = {"visual":0, "blur":0}
    skipped_max = {"visual":0, "blur":0}

    def process(kind: str):
        nonlocal moved, missing, errors, protected, skipped_max
        count_moved = 0
        # 再読み込みして順に処理
        with open(csv_path, encoding="utf-8") as fp:
            r = csv.DictReader(fp)
            for row in r:
                if kind == "visual":
                    if not (row.get("type")=="visual" and row.get("domain")=="group"):
                        continue
                    keep = (row.get("keep") or "").strip()
                    cand = (row.get("candidate") or "").strip()
                    if not cand or cand == keep:
                        continue
                else:  # blur
                    if not (row.get("type")=="blur_single" and row.get("domain")=="single"):
                        continue
                    cand = (row.get("candidate") or "").strip()
                    keep = ""

                # 上限チェック
                if max_move is not None and (moved["visual"] + moved["blur"]) >= max_move:
                    skipped_max[kind] += 1
                    writer.writerow([datetime.datetime.now().isoformat(), mode, "skipped_max",
                                     row.get("type"), row.get("domain"), row.get("group"),
                                     keep, cand, row.get("relation"), f"max_move={max_move}"])
                    continue

                # 存在チェック
                if not _exists(cand):
                    missing[kind] += 1
                    writer.writerow([datetime.datetime.now().isoformat(), mode, "missing",
                                     row.get("type"), row.get("domain"), row.get("group"),
                                     keep, cand, row.get("relation"), "not found"])
                    continue

                # 保護チェック
                if prot_list and _in_protect(cand, prot_list):
                    protected[kind] += 1
                    writer.writerow([datetime.datetime.now().isoformat(), mode, "protected",
                                     row.get("type"), row.get("domain"), row.get("group"),
                                     keep, cand, row.get("relation"), "protected path"])
                    continue

                # 実行
                try:
                    if dry_run:
                        writer.writerow([datetime.datetime.now().isoformat(), mode, "would_move",
                                         row.get("type"), row.get("domain"), row.get("group"),
                                         keep, cand, row.get("relation"), "dry-run"])
                    else:
                        to_trash(cand)
                        writer.writerow([datetime.datetime.now().isoformat(), mode, "moved",
                                         row.get("type"), row.get("domain"), row.get("group"),
                                         keep, cand, row.get("relation"), ""])
                        moved[kind] += 1
                except Exception as e:
                    errors[kind] += 1
                    writer.writerow([datetime.datetime.now().isoformat(), mode, "error",
                                     row.get("type"), row.get("domain"), row.get("group"),
                                     keep, cand, row.get("relation"), str(e)])

    # 5) 実行
    for k in kinds:
        process(k)

    fp_log.close()
    print(f"[LOG] written: {log_path}")
    for k in kinds:
        print(f"[{k}] moved={moved[k]}, missing={missing[k]}, protected={protected[k]}, errors={errors[k]}, skipped_by_max={skipped_max[k]}")
