"""全局路径与配置加载。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
EXPORT_DIR = ROOT  # Excel 直接输出到 room 根目录（房源汇总_日期.xlsx）
SNAP_DIR = DATA_DIR / "snapshots"
LOG_DIR = ROOT / "logs"
STATE_DIR = DATA_DIR / "state"
PROFILE_DIR = DATA_DIR / "profile"   # 每个渠道一个永久浏览器 profile，登录 cookie 实时落盘
DB_PATH = DATA_DIR / "listings.db"
MANUAL_LINKS = ROOT / "manual_links.txt"

_DIRS = (CONFIG_DIR, DATA_DIR, EXPORT_DIR, SNAP_DIR, LOG_DIR, STATE_DIR, PROFILE_DIR)


def ensure_dirs() -> None:
    for d in _DIRS:
        d.mkdir(parents=True, exist_ok=True)


def load_config(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def areas_config() -> dict:
    return load_config("areas.json")


def filters_config() -> dict:
    return load_config("filters.json")
