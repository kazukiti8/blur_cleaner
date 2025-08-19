# __main__.py - blur_cleaner CLI エントリポイント（CSVなし運用＋オート閾値既定p10）
from __future__ import annotations
import argparse
import re
from typing import List, Optional, Tuple, Dict, Any
from .scan import scan
from .apply import apply_from_csv, apply_from_rows

# ---------------- ヘルパ ----------------
def _split_semicol(s: str) -> Optional[List[str]]:
    """; 区切りの文字列をリストへ。空なら None"""
    if not s:
        return None
    items = [x.strip() for x in s.split(";") if x.strip()]
    return items or None

def _parse_lap_var(rel: str) -> Optional[float]:
    """relation から lap_var 数値を抽出"""
    m = re.search(r"lap_var=([0-9]+(?:\.[0-9]+)?)", rel or "")
    return float(m.group(1)) if m else None

def _parse_dist(rel: str) -> Optional[str]:
    """relation から dist（pHash距離）を抽出"""
    m = re.search(r"dist=([0-9]+)", rel or "")
    return m.group(1) if m else None

def _summarize(rows: List[Dict[str, str]]) -> Tuple[int, int, int]:
    """rows の内訳を返す (blur件数, visualグループ数, visualペア数)"""
    blur_rows = [r for r in rows if r.get("type") == "blur_single" and r.get("domain") == "single"]
    vis_rows  = [r for r in rows if r.get("type") == "visual" and r.get("domain") == "group"]
    vis_groups = len({r.get("group") for r in vis_rows})
    return len(blur_rows), vis_groups, len(vis_rows)

def _print_blur_list(rows: List[Dict[str, str]], n: int):
    blur_rows = [r for r in rows if r.get("type")=="blur_single" and r.get("domain")=="single"]
    blur_rows.sort(key=lambda r: (_parse_lap_var(r.get("relation") or "") or 1e18))
    print("[LIST] ブレ候補（lap_var 昇順=ブレ強い）:")
    for r in blur_rows[:n]:
        lv = _parse_lap_var(r.get("relation") or "") or 0.0
        print(f"  lap_var={lv:.1f}  {r.get('candidate')}")

def _print_visual_list(rows: List[Dict[str, str]], n: int):
    vis_rows = [r for r in rows if r.get("type")=="visual" and r.get("domain")=="group"]
    print("[LIST] 類似（重複）候補:")
    for r in vis_rows[:n]:
        dist = _parse_dist(r.get("relation") or "") or ""
        print(f"  grp={r.get('group')}  dist={dist}  keep={r.get('keep')}  cand={r.get('candidate')}")

def _print_blur_stats(stats: Dict[str, Any], limit_lowest: int = 20):
    print(f"[STATS] files={stats.get('files_total',0)}, "
          f"lap_min={stats.get('lap_min',0.0):.1f}, "
          f"median={stats.get('lap_median',0.0):.1f}, "
          f"p95={stats.get('lap_p95',0.0):.1f}, "
          f"max={stats.get('lap_max',0.0):.1f}")
    lows = stats.get("lowest") or []
    if lows:
        print("  lowest (lap_var 昇順=ブレ強い順):")
        for p, lv in lows[:limit_lowest]:
            print(f"   {lv:.1f}  {p}")

def _pick_auto_threshold(stats: Dict[str, float], key: str) -> Optional[float]:
    """--blur-auto で選ばれた指標から閾値を取り出す"""
    mapping = {
        "p05": "lap_p05",
        "p10": "lap_p10",
        "p15": "lap_p15",
        "p20": "lap_p20",
        "p25": "lap_p25",
        "p30": "lap_p30",
        "p40": "lap_p40",
        "median": "lap_median",
    }
    name = mapping.get(key)
    if not name:
        return None
    val = stats.get(name)
    try:
        return float(val) if val is not None else None
    except Exception:
        return None

# ---------------- メイン ----------------
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="blur_cleaner",
        description="Windows向け：ブレ判定と類似（重複）整理ツール（CSVなし運用・オート閾値対応）"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- scan（CSVは任意）----
    sscan = sub.add_parser("scan", help="フォルダを走査（CSVは任意）")
    sscan.add_argument("target_dir", help=r"対象ディレクトリ（例：D:\tests）")
    sscan.add_argument("--report", default="", help="CSVに書き出す場合のパス（省略可）")
    sscan.add_argument("--db", default=".imgclean.db", help="特徴量キャッシュDB（sqlite）")
    sscan.add_argument("--blur-threshold", type=float, default=400.0, help="ブレ閾値（小さいほどブレ強）")
    sscan.add_argument("--blur-stats", action="store_true", help="ブレ値の統計を表示")
    sscan.add_argument("--blur-auto",
                       choices=["off","p05","p10","p15","p20","p25","p30","p40","median"],
                       default="p10",
                       help="ブレ閾値を自動決定（下位％や中央値）。off で手動に戻す")
    sscan.add_argument("--similar", action="store_true", help="類似（pHash）を有効化")
    sscan.add_argument("--phash-distance", type=int, default=6, help="pHash距離（小さいほど厳密）")
    sscan.add_argument("--include-exts", default="", help="対象拡張子を;区切りで（例：.jpg;.png）")
    sscan.add_argument("--exclude", default="", help="パスに含むと除外する文字列を;区切りで")
    sscan.add_argument("--list", type=int, default=0, help="候補を最大N件表示（0で非表示）")

    # ---- apply（互換：CSVから適用）----
    sapp = sub.add_parser("apply", help="CSVからごみ箱へ適用")
    sapp.add_argument("--from", dest="csv", default="report.csv", help="report.csv のパス")
    sapp.add_argument("--only", choices=["visual","blur"], help="visual or blur のみ適用")
    sapp.add_argument("--protect", default="", help="保護パス（;区切り、フォルダ/ファイル）")
    sapp.add_argument("--max-move", type=int, default=None, help="今回移動の上限件数")
    sapp.add_argument("--dry-run", action="store_true", help="実際には移動せず件数のみカウント")
    sapp.add_argument("--confirm", action="store_true", help="適用前に確認する")
    sapp.add_argument("--log-dir", default="", help="適用ログCSVの出力フォルダ")

    # ---- run（CSVなし：スキャン→即適用）----
    srun = sub.add_parser("run", help="CSVなしで スキャン→即適用（ワンショット）")
    srun.add_argument("target_dir", help=r"対象ディレクトリ（例：D:\tests）")
    srun.add_argument("--only", choices=["visual","blur"], help="visual or blur のみ適用")
    srun.add_argument("--db", default=".imgclean.db", help="特徴量キャッシュDB（sqlite）")
    srun.add_argument("--blur-threshold", type=float, default=400.0, help="ブレ閾値（小さいほどブレ強）")
    srun.add_argument("--blur-stats", action="store_true", help="ブレ値の統計を表示")
    srun.add_argument("--blur-auto",
                      choices=["off","p05","p10","p15","p20","p25","p30","p40","median"],
                      default="p10",
                      help="ブレ閾値を自動決定（下位％や中央値）。off で手動に戻す")
    srun.add_argument("--similar", action="store_true", help="類似（pHash）を有効化")
    srun.add_argument("--phash-distance", type=int, default=6, help="pHash距離（小さいほど厳密）")
    srun.add_argument("--include-exts", default="", help="対象拡張子を;区切りで（例：.jpg;.png）")
    srun.add_argument("--exclude", default="", help="パスに含むと除外する文字列を;区切りで")
    srun.add_argument("--protect", default="", help="保護パス（;区切り、フォルダ/ファイル）")
    srun.add_argument("--max-move", type=int, default=None, help="今回移動の上限件数")
    srun.add_argument("--dry-run", action="store_true", help="実際には移動せず件数のみカウント")
    srun.add_argument("--confirm", action="store_true", help="適用前に確認する")
    srun.add_argument("--log-dir", default="", help="適用ログCSVの出力フォルダ")
    srun.add_argument("--list", type=int, default=0, help="適用前に候補を最大N件表示（0で非表示）")

    args = p.parse_args(argv)

    # ---------------- scan ----------------
    if args.cmd == "scan":
        exts = _split_semicol(args.include_exts)
        excl = _split_semicol(args.exclude)

        # オート閾値（統計パス→本スキャン）
        auto_thr = None
        if args.blur_auto and args.blur_auto != "off":
            _rows_tmp, stats = scan(
                target_dir=args.target_dir,
                report_csv=None,
                dbpath=args.db,
                blur_threshold=1e9,          # 一旦ヒットしない値
                do_similar=args.similar,
                phash_distance=args.phash_distance,
                include_exts=exts,
                exclude_substr=excl,
                collect_stats=True,
            )
            auto_thr = _pick_auto_threshold(stats, args.blur_auto)
            if auto_thr is None:
                print(f"[WARN] auto threshold not available for {args.blur_auto}")
            else:
                print(f"[AUTO] blur_threshold = {auto_thr:.1f}  (source={args.blur_auto})")

        rows = scan(
            target_dir=args.target_dir,
            report_csv=(args.report or None),
            dbpath=args.db,
            blur_threshold=(auto_thr if auto_thr is not None else args.blur_threshold),
            do_similar=args.similar,
            phash_distance=args.phash_distance,
            include_exts=exts,
            exclude_substr=excl,
            collect_stats=False,
        )

        b, vg, vp = _summarize(rows)
        print(f"[OK] scanned rows={len(rows)} (blur={b}, visual_groups={vg}, visual_pairs={vp})")
        if args.report:
            print(f"[OK] wrote {args.report}")

        if args.blur_stats:
            # 統計表示が欲しい場合は同条件で stats を取得して表示
            _rows_tmp, stats = scan(
                target_dir=args.target_dir,
                report_csv=None,
                dbpath=args.db,
                blur_threshold=(auto_thr if auto_thr is not None else args.blur_threshold),
                do_similar=args.similar,
                phash_distance=args.phash_distance,
                include_exts=exts,
                exclude_substr=excl,
                collect_stats=True,
            )
            _print_blur_stats(stats)

        if args.list:
            if b:
                _print_blur_list(rows, args.list)
            if vg or vp:
                _print_visual_list(rows, args.list)

    # ---------------- apply ----------------
    elif args.cmd == "apply":
        protect = _split_semicol(args.protect)
        if args.confirm:
            yn = input(f"CSV '{args.csv}' から適用します（only={args.only or 'both'}）。よろしいですか？ [y/N]: ").strip().lower()
            if yn not in ("y", "yes"):
                print("canceled.")
                return
        res = apply_from_csv(
            csv_path=args.csv,
            only=args.only,
            protect=protect,
            max_move=args.max_move,
            dry_run=args.dry_run,
            log_dir=(args.log_dir or None) if args.log_dir else None,
        )
        which = args.only or "both"
        print(f"[{which}] moved={res['moved']}, missing={res['missing']}, errors={res['errors']}")

    # ---------------- run ----------------
    elif args.cmd == "run":
        exts = _split_semicol(args.include_exts)
        excl = _split_semicol(args.exclude)
        protect = _split_semicol(args.protect)

        # オート閾値（統計パス）
        auto_thr = None
        if args.blur_auto and args.blur_auto != "off":
            _rows_tmp, stats = scan(
                target_dir=args.target_dir,
                report_csv=None,
                dbpath=args.db,
                blur_threshold=1e9,
                do_similar=args.similar,
                phash_distance=args.phash_distance,
                include_exts=exts,
                exclude_substr=excl,
                collect_stats=True,
            )
            auto_thr = _pick_auto_threshold(stats, args.blur_auto)
            if auto_thr is None:
                print(f"[WARN] auto threshold not available for {args.blur_auto}")
            else:
                print(f"[AUTO] blur_threshold = {auto_thr:.1f}  (source={args.blur_auto})")

        # 本スキャン（必要なら統計も）
        if args.blur_stats:
            rows, stats = scan(
                target_dir=args.target_dir,
                report_csv=None,
                dbpath=args.db,
                blur_threshold=(auto_thr if auto_thr is not None else args.blur_threshold),
                do_similar=args.similar,
                phash_distance=args.phash_distance,
                include_exts=exts,
                exclude_substr=excl,
                collect_stats=True,
            )
            _print_blur_stats(stats, limit_lowest=10)
        else:
            rows = scan(
                target_dir=args.target_dir,
                report_csv=None,
                dbpath=args.db,
                blur_threshold=(auto_thr if auto_thr is not None else args.blur_threshold),
                do_similar=args.similar,
                phash_distance=args.phash_distance,
                include_exts=exts,
                exclude_substr=excl,
                collect_stats=False,
            )

        b, vg, vp = _summarize(rows)
        print(f"[INFO] candidates: blur={b}, visual_groups={vg}, visual_pairs={vp}")

        if args.list:
            if (args.only in (None, "blur")) and b:
                _print_blur_list(rows, args.list)
            if (args.only in (None, "visual")) and (vg or vp):
                _print_visual_list(rows, args.list)

        which = args.only or "both"
        if args.confirm:
            yn = input(f"適用します（only={which} / scanned={len(rows)}）。よろしいですか？ [y/N]: ").strip().lower()
            if yn not in ("y", "yes"):
                print("canceled.")
                return

        res = apply_from_rows(
            rows,
            only=args.only,
            protect=protect,
            max_move=args.max_move,
            dry_run=args.dry_run,
            log_dir=(args.log_dir or None) if args.log_dir else None,
        )
        print(f"[{which}] moved={res['moved']}, missing={res['missing']}, errors={res['errors']}")

if __name__ == "__main__":
    main()
