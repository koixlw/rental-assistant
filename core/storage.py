"""SQLite 存储：upsert、跨平台去重、状态流转、查询。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import fields
from datetime import datetime
from difflib import SequenceMatcher

from core.config import DB_PATH, ensure_dirs
from core.models import Listing

# 渠道可信度优先级，去重时保留数值高的那条作为主记录
SOURCE_PRIORITY = {
    "beike": 90,
    "ziroom": 85,
    "douban": 60,
    "xiaohongshu": 55,
    "xianyu": 55,
    "anjuke": 40,
    "58": 35,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  url TEXT DEFAULT '',
  title TEXT DEFAULT '',
  community TEXT DEFAULT '',
  area_name TEXT DEFAULT '',
  station TEXT DEFAULT '',
  station_distance_m INTEGER,
  lng REAL,
  lat REAL,
  rent REAL,
  deposit TEXT DEFAULT '',
  service_fee REAL,
  agency_fee TEXT DEFAULT '',
  monthly_cost REAL,
  rent_type TEXT DEFAULT '',
  layout TEXT DEFAULT '',
  rooms INTEGER,
  halls INTEGER,
  bath INTEGER,
  is_studio INTEGER DEFAULT 0,
  private_bath INTEGER,
  area_sqm REAL,
  orientation TEXT DEFAULT '',
  floor TEXT DEFAULT '',
  total_floor INTEGER,
  elevator INTEGER,
  building_age TEXT DEFAULT '',
  decoration TEXT DEFAULT '',
  balcony INTEGER,
  tags TEXT DEFAULT '',
  publisher_type TEXT DEFAULT '',
  confidence INTEGER DEFAULT 60,
  published_at TEXT DEFAULT '',
  status TEXT DEFAULT '在租',
  first_seen TEXT DEFAULT '',
  last_seen TEXT DEFAULT '',
  commute_minutes INTEGER,
  score REAL,
  prev_rent REAL,
  duplicate_of INTEGER,
  note TEXT DEFAULT '',
  UNIQUE(source, source_id)
);
"""


def _norm_community(name: str) -> str:
    s = re.sub(r"\s+", "", name or "")
    for suffix in ("小区", "社区", "家园", "公寓", "花苑", "新村"):
        s = s.replace(suffix, "")
    return s


class Storage:
    def __init__(self, path=DB_PATH):
        ensure_dirs()
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库增量加列（SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
        if "distance_km" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN distance_km REAL")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------写入
    def upsert(self, lst: Listing) -> tuple[int, bool]:
        """写入或更新一条房源，返回 (id, 是否新上)。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if lst.monthly_cost is None and lst.rent is not None:
            lst.monthly_cost = lst.rent + (lst.service_fee or 0.0)
        row = self.conn.execute(
            "SELECT * FROM listings WHERE source=? AND source_id=?",
            (lst.source, lst.source_id),
        ).fetchone()

        if row is not None:
            lid = row["id"]
            prev_rent = row["rent"]
            status = row["status"]
            if lst.rent and prev_rent and lst.rent < prev_rent - 0.01 and status in ("在租", "降价"):
                status = "降价"
            if status == "下架":
                status = "在租"
            sets, vals = [], []
            for f in fields(lst):
                if f.name in ("first_seen",):
                    continue
                sets.append(f"{f.name}=?")
                vals.append(getattr(lst, f.name))
            sets += ["last_seen=?", "status=?", "prev_rent=?"]
            vals += [now, status, prev_rent if prev_rent is not None else lst.rent]
            vals.append(lid)
            self.conn.execute(
                f"UPDATE listings SET {', '.join(sets)} WHERE id=?", vals
            )
            self.conn.commit()
            return lid, False

        lst.first_seen = now
        lst.last_seen = now
        cols = Listing.column_names()
        placeholders = ",".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT INTO listings ({','.join(cols)}) VALUES ({placeholders})",
            [getattr(lst, c) for c in cols],
        )
        lid = cur.lastrowid
        self.conn.commit()
        self._dedup_new(lid, lst)
        return lid, True

    def _dedup_new(self, new_id: int, lst: Listing) -> None:
        """同平台唯一键之外，按小区+面积+租金+标题相似度做跨平台去重。"""
        if not lst.community or lst.rent is None:
            return
        rows = self.conn.execute(
            """SELECT id, source, title, community, area_sqm, rent
               FROM listings
               WHERE id != ? AND duplicate_of IS NULL AND rent IS NOT NULL
                 AND ABS(rent - ?) <= 200""",
            (new_id, lst.rent),
        ).fetchall()
        new_norm = _norm_community(lst.community)
        for r in rows:
            other_norm = _norm_community(r["community"])
            if not new_norm or not other_norm:
                continue
            if not (
                new_norm == other_norm
                or new_norm in other_norm
                or other_norm in new_norm
            ):
                continue
            ratio = SequenceMatcher(None, lst.title or "", r["title"] or "").ratio()
            area_ok = (
                lst.area_sqm is not None
                and r["area_sqm"] is not None
                and abs(lst.area_sqm - r["area_sqm"]) <= 4
            )
            rent_close = abs((lst.rent or 0) - (r["rent"] or 0)) <= 50
            if ratio >= 0.8 or (area_ok and (ratio >= 0.35 or rent_close)):
                keep_new = SOURCE_PRIORITY.get(lst.source, 50) >= SOURCE_PRIORITY.get(
                    r["source"], 50
                )
                dup_id, _keep = (new_id, r["id"]) if not keep_new else (r["id"], new_id)
                self.conn.execute(
                    "UPDATE listings SET duplicate_of=? WHERE id=? AND duplicate_of IS NULL",
                    (_keep, dup_id),
                )
                self.conn.commit()
                break

    # ------------------------------------------------------------------更新
    def update_values(self, lid: int, **values) -> None:
        if not values:
            return
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(
            f"UPDATE listings SET {sets} WHERE id=?", list(values.values()) + [lid]
        )
        self.conn.commit()

    def mark_status(self, lid: int, status: str, note: str | None = None) -> None:
        if note:
            self.conn.execute(
                "UPDATE listings SET status=?, note=? WHERE id=?", (status, note, lid)
            )
        else:
            self.conn.execute("UPDATE listings SET status=? WHERE id=?", (status, lid))
        self.conn.commit()

    # ------------------------------------------------------------------查询
    def all_rows(self, include_duplicates: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM listings"
        if not include_duplicates:
            sql += " WHERE duplicate_of IS NULL"
        sql += " ORDER BY id"
        return list(self.conn.execute(sql).fetchall())

    def get(self, lid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()

    def rows_for_check(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM listings
                   WHERE duplicate_of IS NULL AND status != '下架' AND url != ''
                   ORDER BY last_seen ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        )

    def area_avg_rent(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for r in self.conn.execute(
            """SELECT area_name, AVG(rent) AS avg_rent
               FROM listings
               WHERE area_name != '' AND rent IS NOT NULL AND duplicate_of IS NULL
               GROUP BY area_name"""
        ):
            result[r["area_name"]] = r["avg_rent"]
        return result

    def new_since(self, date_str: str) -> list:
        """first_seen >= date_str 的房源（用于"今日新上"）。"""
        return list(
            self.conn.execute(
                """SELECT * FROM listings
                   WHERE duplicate_of IS NULL AND first_seen >= ?
                   ORDER BY id DESC""",
                (date_str,),
            ).fetchall()
        )

    def top_recommended(self, limit: int = 5) -> list:
        return list(
            self.conn.execute(
                """SELECT * FROM listings
                   WHERE duplicate_of IS NULL AND status IN ('在租','降价')
                     AND score IS NOT NULL
                     AND note NOT LIKE '%超出%' AND note NOT LIKE '%非整租%'
                     AND note NOT LIKE '%户型不符%' AND note NOT LIKE '%通勤%'
                     AND note NOT LIKE '%过小%' AND note NOT LIKE '%无独卫%'
                   ORDER BY score DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        )

    def price_drops(self) -> list:
        return list(
            self.conn.execute(
                """SELECT * FROM listings
                   WHERE duplicate_of IS NULL AND status = '降价'
                   ORDER BY last_seen DESC"""
            ).fetchall()
        )
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM listings WHERE duplicate_of IS NULL GROUP BY status"
        ):
            out[r["status"]] = r["c"]
        dup = self.conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE duplicate_of IS NOT NULL"
        ).fetchone()["c"]
        out["重复"] = dup
        return out
