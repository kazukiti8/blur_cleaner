import argparse, csv, shutil
from pathlib import Path
from .detectors import detect_blur_paths

def _collect_images(root: Path):
    exts = {".jpg",".jpeg",".png",".bmp",".tif",".tiff",".webp"}
    return [p for p in root.rglob("*") if p.suffix.lower() in exts]

def main():
    ap = argparse.ArgumentParser(description="Blur Cleaner (Multi-scale + Tenengrad AND as default)")
    ap.add_argument("input", type=str)
    ap.add_argument("--csv", type=str, default="blur_scores.csv")
    ap.add_argument("--move-blur", type=str, default=None)
    ap.add_argument("--copy-blur", action="store_true")
    ap.add_argument("--preview", type=int, default=20)

    # デフォ設定（旧との互換フラグも用意）
    ap.add_argument("--legacy-detector", action="store_true", help="旧方式(ラプラシアン単独)に戻す")
    ap.add_argument("--and-tenengrad", action="store_true", default=True, help="AND判定を有効化（デフォルトON）")
    ap.add_argument("--no-and-tenengrad", action="store_false", dest="and_tenengrad", help="AND判定をOFFにする")

    ap.add_argument("--threshold", type=float, default=800.0)
    ap.add_argument("--ten-threshold", type=float, default=800.0)
    ap.add_argument("--auto-th", type=str, default="percentile", choices=["percentile","zscore","none"])
    ap.add_argument("--auto-param", type=float, default=25.0)
    ap.add_argument("--ten-auto-th", type=str, default="percentile", choices=["percentile","zscore","none"])
    ap.add_argument("--ten-auto-param", type=float, default=25.0)

    ap.add_argument("--gauss-ksize", type=int, default=3)
    ap.add_argument("--gauss-sigma", type=float, default=0.0)
    ap.add_argument("--lap-ksize", type=int, default=3)
    ap.add_argument("--scales", type=str, default="1.0,0.5,0.25")
    ap.add_argument("--agg", type=str, default="median", choices=["mean","median","max","min"])
    ap.add_argument("--max-side", type=int, default=2000)

    args = ap.parse_args()
    root = Path(args.input)
    imgs = _collect_images(root)

    scales = tuple(float(s.strip()) for s in args.scales.split(",") if s.strip())

    rows, meta = detect_blur_paths(
        imgs,
        scales=scales,
        lap_ksize=args.lap_ksize,
        agg=args.agg,
        gauss_ksize=args.gauss_ksize,
        gauss_sigma=args.gauss_sigma,
        and_tenengrad=args.and_tenengrad,
        ten_ksize=3,
        threshold=(None if args.auto_th!="none" else args.threshold),
        auto_th=(None if args.auto_th=="none" else args.auto_th),
        auto_param=args.auto_param,
        ten_threshold=(None if args.ten_auto_th!="none" else args.ten_threshold),
        ten_auto_th=(None if args.ten_auto_th=="none" else args.ten_auto_th),
        ten_auto_param=args.ten_auto_param,
        max_side=args.max_side,
        legacy=args.legacy_detector
    )

    # 仕分け
    if args.move_blur:
        out = Path(args.move_blur); out.mkdir(parents=True, exist_ok=True)
        for r in rows:
            if r.get("is_blur"):
                try: shutil.move(r["path"], out / Path(r["path"]).name)
                except Exception as e: r["status"] = f"move_error:{e}"

    # CSV
    fieldnames = sorted(set().union(*[set(r.keys()) for r in rows]) | {"meta_th_ms","meta_th_ten"})
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            r = dict(r); r.setdefault("meta_th_ms", meta.get("th_ms")); r.setdefault("meta_th_ten", meta.get("th_ten"))
            w.writerow(r)

    # プレビュー
    if args.preview > 0:
        preview = sorted([r for r in rows if r.get("status")=="ok"], key=lambda x: x["score"])[:args.preview]
        print("\n=== PREVIEW (low MS score) ===")
        for r in preview:
            mark = "BLUR" if r["is_blur"] else "SHARP"
            print(f'{r["score"]:10.3f} | Ten {r["ten_score"]:10.3f} | {mark:5s} | {r["rule"]:11s} | {r["path"]}')

    print(f"\n[RESULT] images={len(imgs)}  th_ms={meta.get('th_ms'):.3f}  th_ten={meta.get('th_ten'):.3f}  AND={'ON' if args.and_tenengrad else 'OFF'}  LEGACY={'ON' if args.legacy_detector else 'OFF'}")
