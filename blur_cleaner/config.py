# blur_cleaner/config.py
from importlib import resources
import yaml

DEFAULT = { ... }  # 既定値（保険）

def load(path: str | None = None):
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return {**DEFAULT, **(yaml.safe_load(f) or {})}
    # パッケージ同梱 default.yaml を読む
    with resources.files("blur_cleaner").joinpath("config/default.yaml").open("r", encoding="utf-8") as f:
        return {**DEFAULT, **(yaml.safe_load(f) or {})}
