from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import os, csv, html, urllib.parse
from PIL import Image
from string import Template  # ← 追加

# HEIC対応（入っていれば有効化）
try:
    from pillow_heif import register_heif
    register_heif()
except Exception:
    pass

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Noto Sans,Arial,sans-serif;margin:16px;color:#222;}
h1{font-size:20px;margin:0 0 12px}
h2{font-size:16px;margin:24px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.meta{color:#666;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{border:1px solid #ddd;border-radius:8px;padding:8px;background:#fff}
.card.keep{border-color:#2e7d32}
.badge{display:inline-block;padding:2px 6px;border-radius:999px;font-size:11px;background:#eee;margin-right:6px}
.badge.keep{background:#e8f5e9;color:#2e7d32;border:1px solid #c8e6c9}
.badge.cand{background:#fff3e0;color:#ef6c00;border:1px solid #ffe0b2}
.row{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
figure{margin:0}
figcaption{font-size:12px;color:#555;margin-top:4px;word-break:break-all}
.thumb{width:100%;height:auto;border:1px solid #eee;border-radius:4px;object-fit:contain;background:#fafafa}
.small{font-size:12px;color:#666}
code{background:#f6f8fa;border-radius:4px;padding:0 4px}
.kv{font-size:12px;color:#444}
.kv b{color:#222}
.toolbar{display:flex;gap:16px;align-items:center;margin:12px 0}
.switch{display:flex;gap:6px;align-items:center;font-size:13px;color:#444}
.hide-in-visual .card.in-visual{display:none}
"""

# ${css}/${summary}/${content} を Template で置換する
HTML_SHELL = Template("""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>blur_cleaner report</title>
<style>${css}</style></head><body>
<h1>blur_cleaner report</h1>
<div class="meta">${summary}</div>
<div class="toolbar">
  <label class="switch">
    <input id="toggleHide" type="checkbox">
    <span>Blur一覧から Visual に含まれる項目を隠す</span>
  </label>
</div>
${content}
<script>
  const cb=document.getElementById('toggleHide');
  cb?.addEventListener('change', e=>{
    document.body.classList.toggle('hide-in-visual', cb.checked);
  });
</script>
</body></html>
""")

def _win_path_to_file_uri(p: str) -> str:
    # Windowsパス→file:// URI（UNCも一応考慮）
    p = os.path.abspath(p)
    if p.startswith("\\\\"):
        return "file:" + urllib.parse.quote(p)
    return "file:///" + urllib.parse.quote(p.replace("\\", "/"))

def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def _thumb_path_for(src: Path, out_dir: Path) -> Path:
    safe = src.name.replace(":", "_")
    return out_dir / (safe + ".jpg")

def _make_thumb(src_path: str, thumb_path: Path, max_size: int = 256) -> Tuple[int,int] | None:
    """サムネ生成しつつ、元画像の解像度（W,H）を返す"""
    try:
        with Image.open(src_path) as im:
            orig_w, orig_h = im.size  # 元サイズを確保
            im = im.convert("RGB")
            im.thumbnail((max_size, max_size))
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            im.save(thumb_path, "JPEG", quality=85, optimize=True)
            return (orig_w, orig_h)
    except Exception:
        return None

def _read_csv(report_csv: str) -> List[Dict[str,str]]:
    rows = []
    with open(report_csv, encoding="utf-8") as fp:
        r = csv.DictReader(fp)
        need = {"type","domain","group","keep","candidate","relation"}
        if not need.issubset(set(r.fieldnames or [])):
            raise ValueError("report.csv の列が新仕様と異なります（type,domain,group,keep,candidate,relation）。scanを再実行してね。")
        for row in r:
            rows.append(row)
    return rows

def _gather_visual(rows: List[Dict[str,str]]):
    # group id -> {"keep": str, "cands": List[(path, relation)]}
    groups: Dict[str, Dict[str, object]] = {}
    for row in rows:
        if row.get("type")=="visual" and row.get("domain")=="group":
            gid = row.get("group","").strip() or "(no-group)"
            keep = row.get("keep","").strip()
            cand = row.get("candidate","").strip()
            rel  = row.get("relation","").strip()
            if gid not in groups:
                groups[gid] = {"keep": keep, "cands": []}
            if cand and cand != keep:
                groups[gid]["cands"].append((cand, rel))
    return groups

def _gather_blur(rows: List[Dict[str,str]]):
    singles: List[Tuple[str,str]] = []
    for row in rows:
        if row.get("type")=="blur_single" and row.get("domain")=="single":
            cand = row.get("candidate","").strip()
            rel  = row.get("relation","").strip()
            if cand:
                singles.append((cand, rel))
    return singles

def build_report(
    report_csv: str,
    out_html: str,
    thumb_dir: Optional[str] = None,
    max_thumb: int = 256
):
    rows = _read_csv(report_csv)
    visual = _gather_visual(rows)
    blur_singles = _gather_blur(rows)

    out_path = Path(out_html)
    assets = Path(thumb_dir) if thumb_dir else out_path.with_suffix("").parent / "report_assets"
    thumbs = assets / "thumbs"
    _ensure_dir(thumbs)

    # Visualに含まれる全パス（keep + candidates）
    visual_all_paths = set()
    for gid, data in visual.items():
        k = data.get("keep")
        if isinstance(k, str) and k:
            visual_all_paths.add(os.path.abspath(k))
        for p,_rel in data.get("cands", []):
            visual_all_paths.add(os.path.abspath(p))

    def card_html(img_path: str, role: str, caption_extra: str="", in_visual: bool=False) -> str:
        # サムネ作成＆元解像度取得
        src = Path(img_path)
        th = _thumb_path_for(src, thumbs)
        size = _make_thumb(str(src), th, max_size=max_thumb)
        thumb_rel = os.path.relpath(th, start=out_path.parent).replace("\\", "/")

        # キャプション（元解像度）
        if size:
            w,h = size
        else:
            w,h = (0,0)
        badge_cls = "keep" if role=="keep" else "cand"
        cap = f'<span class="badge {badge_cls}">{html.escape(role)}</span>'
        cap += f'<span class="kv"><b>{html.escape(src.name)}</b> · {w}×{h}</span>'
        if caption_extra:
            cap += f' · <span class="small">{html.escape(caption_extra)}</span>'

        # クリックで原本を開く
        href = _win_path_to_file_uri(str(src))
        card_cls = f"card {role} {'in-visual' if in_visual else ''}".strip()
        return f'''
        <a class="{card_cls}" href="{href}" target="_blank" title="{html.escape(str(src))}">
          <figure>
            <img class="thumb" loading="lazy" src="{html.escape(thumb_rel)}" alt="{html.escape(src.name)}">
            <figcaption>{cap}</figcaption>
          </figure>
        </a>
        '''

    # ----- Visual Section -----
    visual_sections = []
    if visual:
        for gid, data in visual.items():
            keep = data["keep"]
            cands: List[Tuple[str,str]] = data["cands"]  # [(path, relation)]
            cards = []
            if isinstance(keep, str) and keep and os.path.exists(keep):
                cards.append(card_html(keep, "keep", in_visual=True))
            for path, rel in cands:
                if os.path.exists(path):
                    cards.append(card_html(path, "candidate", caption_extra=rel, in_visual=True))
            if cards:
                visual_sections.append(
                    f'<h2>Visual Group: <code>{html.escape(gid)}</code></h2>'
                    f'<div class="grid">{"".join(cards)}</div>'
                )
    visual_html = "\n".join(visual_sections) if visual_sections else "<p class='small'>（該当なし）</p>"

    # ----- Blur Singles -----
    blur_cards = []
    for path, rel in blur_singles:
        if os.path.exists(path):
            in_vis = os.path.abspath(path) in visual_all_paths
            blur_cards.append(card_html(path, "candidate", caption_extra=rel, in_visual=in_vis))
    blur_html = f'<div class="grid">{"".join(blur_cards)}</div>' if blur_cards else "<p class='small'>（該当なし）</p>"

    # ----- Compose -----
    summary = (
        f"source: <code>{html.escape(report_csv)}</code> · "
        f"thumbs: <code>{html.escape(str(thumbs))}</code> · "
        f"visual groups: {len(visual)} · blur singles: {len(blur_cards)}"
    )
    html_text = HTML_SHELL.substitute(css=CSS, summary=summary, content=f"""
    <h2>Visual Groups（duplicate+similar）</h2>
    {visual_html}
    <h2>Blur Singles（ブレ単独）</h2>
    {blur_html}
    """)
    out_path.write_text(html_text, encoding="utf-8")
    return str(out_path), str(thumbs)
