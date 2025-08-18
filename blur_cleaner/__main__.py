from __future__ import annotations
import argparse
from .scan import scan
from .apply import apply_from_csv

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="blur_cleaner",
        description="Image cleaner (Windows): visual grouping (duplicate+similar) & blur single detection"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # scan
    sscan = sub.add_parser("scan", help="Scan directory and write report.csv")
    sscan.add_argument("target_dir", help="Target directory (e.g. D:\\tests)")
    sscan.add_argument("--report", default="report.csv")
    sscan.add_argument("--db", default=".imgclean.db")
    sscan.add_argument("--blur-threshold", type=float, default=120.0)
    sscan.add_argument("--similar", action="store_true", help="Enable pHash-based visual grouping (slow on huge sets)")
    sscan.add_argument("--phash-distance", type=int, default=6)
    sscan.add_argument("--include-exts", default="", help="e.g. .jpg;.png (semicolon separated)")
    sscan.add_argument("--exclude", default="", help="substring filter; semicolon separated")

    # apply
    sapp = sub.add_parser("apply", help="Apply deletions to Recycle Bin")
    sapp.add_argument("--from", dest="csv", default="report.csv")
    sapp.add_argument("--only", choices=["visual","blur"], help="Apply only a specific decision domain")
    sapp.add_argument("--protect", default="", help="Semicolon separated paths to protect (folders/files)")
    sapp.add_argument("--max-move", type=int, default=None, help="Max number of files to move for this run")
    sapp.add_argument("--confirm", action="store_true", help="Ask for confirmation before applying")
    sapp.add_argument("--dry-run", action="store_true", help="Do not actually move files; just log actions")
    sapp.add_argument("--log-dir", default="", help="Directory to write applied_YYYYMMDD_HHMM.csv")

    args = p.parse_args(argv)

    if args.cmd == "scan":
        exts = [e.strip().lower() for e in args.include_exts.split(";") if e.strip()] or None
        excl = [e.strip() for e in args.exclude.split(";") if e.strip()] or None
        n = scan(args.target_dir, report_csv=args.report, dbpath=args.db,
                 blur_threshold=args.blur_threshold, do_similar=args.similar,
                 phash_distance=args.phash_distance, include_exts=exts, exclude_substr=excl)
        print(f"[OK] wrote {args.report} ({n} rows)")

    elif args.cmd == "apply":
        apply_from_csv(
            csv_path=args.csv,
            only=args.only,
            protect=args.protect or None,
            max_move=args.max_move,
            confirm=args.confirm,
            dry_run=args.dry_run,
            log_dir=(args.log_dir or None),
        )
        if args.only:
            print(f"[OK] applied deletions for: {args.only}")
        else:
            print("[OK] applied deletions for: visual and blur (both)")

if __name__ == "__main__":
    main()
