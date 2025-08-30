1) 概要

ブレ検出：多尺度ラプラシアン分散（＋任意でTenengrad AND）

類似検出：pHash＋dHashのハイブリッド → MNN（相互トップK）→ SSIM → HSV色ヒストで誤検出を削減

GUI/CLI両対応、大量データ（数万枚）対応の並列スキャン＆キャッシュ

2) 動作環境

Windows 10/11（PowerShell想定）

Python 3.9–3.13

主要ライブラリ：opencv-python, numpy, tkinter（標準）

インストール：

# 任意の仮想環境（推奨）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 依存関係
pip install -U pip wheel
pip install opencv-python numpy

3) リポジトリ構成（主要）
blur_cleaner/
  __init__.py
  apply.py
  cache_db.py          # pHash/dHash/blurをSQLiteにキャッシュ
  detectors.py         # 多尺度ラプラシアン分散 + Tenengrad（あれば）
  fast_scan.py         # 列挙/並列ブレ/ハッシュ/ハイブリッド類似/SSIM
  gui/
    __init__.py
    __main__.py        # `python -m blur_cleaner.gui`
    main_window.py     # GUI本体（MNN/SSIM/HSV・しきい値GUI対応）
    dialogs.py         # 設定ダイアログ（しきい値可視）
    preview_panel.py   # プレビュー
    table_views.py     # テーブルUI
    thumbs.py          # サムネ生成
scripts/
  blur_detector_multiscale.py  # CLIブレ検出ツール


初回スキャン時、対象フォルダ直下に /.blur_cleaner_cache.sqlite が作成されます（WAL）。

4) インストール（編集可能モード）

プロジェクトルート（setup.cfg/pyproject.tomlがある場所）で：

pip install -e .


もし「複数トップレベルパッケージ」エラーが出たら、srcレイアウト or packages=find の設定を使う構成に直してください（本リポは既に解消済み前提）。

5) 使い方（CLI：ブレ検出）
5.1 独立ツールとして実行
# 例: D:\tests を走査し、ブレ疑いを D:\blurred_out に移動
.\.venv\Scripts\python.exe "D:\blur_cleaner\scripts\blur_detector_multiscale.py" "D:\tests" `
  --auto-th percentile --auto-param 25 --agg median --gauss-ksize 3 `
  --and-tenengrad --ten-auto-th percentile --ten-auto-param 25 `
  --move-blur "D:\blurred_out" --preview 20


主なオプション：

--threshold / --auto-th percentile|zscore：MS-LapVarしきい値（固定/自動）

--and-tenengrad：TenengradをANDで併用（取りこぼし↓・精度↑）

--csv blur_scores.csv：スコア出力（解析/検証に）

6) 使い方（GUI：ブレ＋類似）
6.1 起動
python -m blur_cleaner.gui


「対象フォルダ」を選択 → ▶スキャン

「ブレ結果」タブ：ブレ値の昇順

「類似結果」タブ：ペア表示（keep/candidate）

選択して「🗑 ごみ箱へ送る（選択）」で一括整理

6.2 設定（⚙）

ブレ判定

多尺度ラプラシアン（固定/パーセンタイル/z-score）

Tenengrad（固定/パーセンタイル/z-score）をAND併用可

類似判定（リアルタイム調整）

pHash半径 / dHash半径（数値↑でゆるく、多めに拾う）

MNN K：相互トップK（↑で緩く、↓で厳しく）

SSIM閾値 / 上位N件再判定数（SSIM↑で厳しく）

HSV相関の下限（↑で色違いペアを強く除外）

7) チューニングの指針
7.1 見逃しを減らしたい（検出を増やす）

pHash半径：6 → 8〜10

dHash半径：8 → 12〜16

SSIM閾値：0.82 → 0.78〜0.80

SSIM上位N：100 → 200〜300

7.2 誤検出を減らしたい（精度を上げる）

MNN K：2〜3（相互一致のみ残す）

SSIM閾値：0.88〜0.92

HSV相関：0.90〜0.95

pHash半径：6〜8、dHash半径：8〜12

8) デバッグ／ログ

画面下ステータスに内部件数を表示（例）

［debug］phash=12345 dhash=12345
［debug］p_only=320 / d_only=410 / hybrid=550 / postMNN=220 / postSSIM=180 / postHSV=150


p_only/d_only：ハッシュ単独候補

hybrid：統合直後

postMNN/SSIM/HSV：各フィルタ後件数

CSV出力（有効時）：<対象フォルダ>\.blur_debug_pairs.csv

p_only / d_only / final の各ステージのペアを記録

9) パフォーマンスTips（数万枚向け）

NVMe推奨、並列I/Oが効く

SSIMは重いので上位N件に限定（GUIで調整）

1回目が重くてもキャッシュで2回目以降は高速化

類似が少なすぎる場合は半径を段階的に緩める→フィルタで落とす

10) よくあるエラーと対処

No module named cv2
→ pip install opencv-python

can't open file '...blur_detector_multiscale.py': [Errno 2]
→ パスが正しいか再確認（PowerShellはバッククォートで改行、^はCmd）

GUI起動時のImportError（open_cache_atなど）
→ cache_db.py/fast_scan.pyを最新に差し替え

VisualTable引数エラー（on_open_*）
→ gui/main_window.py と gui/table_views.py を揃えて最新に

画像はあるのに類似0
→ 半径↑ → SSIM OFF → pHash対象を一時的に全件で切り分け
→ ダミーとして同一画像を複製して動作確認

11) 保管・キャッシュ

/.blur_cleaner_cache.sqlite（対象フォルダ直下）

blur（ラプラシアン値）, phash, dhash を保存

壊れたら一度削除して再スキャン（自動再生成）

12) ライセンス

プロジェクトのライセンスに従ってください（未記載なら社内私用前提）。

13) 変更履歴（抜粋）

GUI設定に類似しきい値を追加（pHash/dHash半径、MNN K、SSIM閾値・上位N、HSV相関）

ハイブリッド類似 + MNN + SSIM + HSV の誤検出削減パイプを実装

CSVデバッグ出力（.blur_debug_pairs.csv）を追加