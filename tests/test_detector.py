from pathlib import Path
from blur_cleaner.detectors import detect_blur_paths

def test_detect_runs_on_sample_images(tmp_path: Path):
    # ダミー: 手元の小画像2枚を用意できるならここでコピーする
    # ここでは空でもAPIが落ちないことだけを確認
    rows, meta = detect_blur_paths([], threshold=800, ten_threshold=800)
    assert isinstance(rows, list)
    assert "th_ms" in meta and "th_ten" in meta
