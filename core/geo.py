"""通勤估算：高德公交 API（可选，AMAP_KEY 环境变量）+ 地铁站耗时表启发式兜底。"""
from __future__ import annotations

import json
import math
import os
import re
import time

import requests

from core.config import areas_config

OFFSITE_MARKERS = ("将台", "高家园", "驼房营", "将台西", "东坝西", "石佛营", "崔各庄")


class GeoEstimator:
    def __init__(self) -> None:
        cfg = areas_config()
        self.work = cfg["work"]
        self.stations: dict = cfg["stations"]
        self.areas: list[dict] = cfg["areas"]
        self.walk_speed = self.work.get("walk_speed_m_min", 75)
        self.last_mile = self.work.get("last_mile_minutes", 8)
        self.key = os.environ.get("AMAP_KEY", "").strip()
        self._geo_cache: dict[str, str] = {}
        self._ride_cache: dict[str, tuple[float, int]] = {}
        self._load_disk_cache()
        # 北京骑行实际均速（含红绿灯）：高德实测导航 1.9km/10min ≈ 190m/min
        self.bike_speed_m_min = 190

    def _load_disk_cache(self) -> None:
        """地理编码与骑行路径结果持久化缓存，保证免费额度内每天只增几十次调用。"""
        from core.config import STATE_DIR
        self._cache_file = STATE_DIR / "geo_cache.json"
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            self._geo_cache = data.get("geo", {})
            self._ride_cache = {k: tuple(v) for k, v in data.get("ride", {}).items()}
        except Exception:
            self._geo_cache, self._ride_cache = {}, {}

    def _save_disk_cache(self) -> None:
        try:
            self._cache_file.write_text(
                json.dumps(
                    {"geo": self._geo_cache,
                     "ride": {k: list(v) for k, v in self._ride_cache.items()}},
                    ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    # ----------------------------------------------------------片区/站点识别
    def match_area(self, text: str) -> dict | None:
        """根据标题/小区文本命中配置中的片区。"""
        for area in self.areas:
            for kw in area.get("keywords", []):
                if kw and kw in (text or ""):
                    return area
        return None

    def find_station(self, text: str) -> tuple[str, int | None]:
        """从'距14号线将台站560米'之类文本里识别站点与步行距离。"""
        for name in sorted(self.stations, key=len, reverse=True):
            if name in (text or ""):
                m = re.search(
                    r"距\s*(?:\d+\s*号线?)?" + re.escape(name) + r"站?\s*(\d{2,5})\s*[米m]",
                    text,
                )
                dist = int(m.group(1)) if m else None
                return name, dist
        return "", None

    # --------------------------------------------------------------估算入口
    def estimate(self, lst, area: dict | None = None) -> tuple[int | None, str]:
        """返回 (通勤分钟数, 估算方式)。启发式结果；
        精确的电动车分钟由 distance_km 返回并在评分时覆盖。"""
        name = lst.station or ""
        dist = lst.station_distance_m
        if not name:
            name, dist = self.find_station(" ".join([lst.title or "", lst.tags or "", lst.note or ""]))
        if not name and area:
            name = area.get("station", "")

        bike = area.get("bike_direct") if area else None
        st = self.stations.get(name)
        candidates: list[tuple[int, str]] = []

        if st and st.get("to_jiangtai") is not None:
            if dist:
                walk = max(2, round(dist / self.walk_speed))
            else:
                walk = st.get("walk_default", 10)
            candidates.append((walk + int(st["to_jiangtai"]) + self.last_mile, "地铁估算"))

        # 近片区骑行直达；或该站无地铁换乘路径时（驼房营/将台西/石佛营）
        if bike is not None and (
            not st or st.get("to_jiangtai") is None or name in OFFSITE_MARKERS or dist is None
        ):
            candidates.append((int(bike), "骑行估算"))
        elif bike is not None and not candidates:
            candidates.append((int(bike), "骑行估算"))

        if not candidates:
            return None, "无位置信息"
        return min(candidates, key=lambda x: x[0])

    # ----------------------------------------------------------启发式距离
    @staticmethod
    def _haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    def _heuristic_distance(self, lst, area, method: str
                            ) -> tuple[float | None, int | None]:
        """无 API 时的距离估算。"""
        bike = area.get("bike_direct") if area else None
        if method == "骑行估算" and bike:
            return round(bike * self.bike_speed_m_min / 1000, 1), int(bike)
        name = lst.station or ""
        dist_m = lst.station_distance_m
        if not name:
            name, dist_m = self.find_station(
                " ".join([lst.title or "", lst.tags or "", lst.note or ""]))
        if not name and area:
            name = area.get("station", "")
        st = self.stations.get(name)
        if not st or st.get("lng") is None:
            return None, None
        d = self._haversine_km(st["lng"], st["lat"],
                               self.work["lng"], self.work["lat"])
        if dist_m:
            walk_km = dist_m / 1000.0
        else:
            walk_km = st.get("walk_default", 10) * self.walk_speed / 1000.0
        return round(d + walk_km, 1), None

    def distance_km(self, lst, area: dict | None = None,
                    method: str = "") -> tuple[float | None, int | None]:
        """返回 (到公司路径距离公里, 电动车导航分钟)。

        有 AMAP_KEY 时：小区名地理编码 → 高德 v5 电动车路径规划，
        拿到的就是高德 App 两轮车导航的路径距离/耗时；结果持久化缓存。
        电动车配额仅 100 次/日：先启发式粗算，明显超界（>13km）的
        远郊记录不浪费调用；API 失败/超配额自动退回启发式。
        """
        rough_km, rough_mins = self._heuristic_distance(lst, area, method)
        if self.key and (rough_km is None or rough_km <= 13):
            ride = self._amap_ride(lst, area)
            if ride is not None:
                dist_m, mins = ride
                return round(dist_m / 1000, 1), mins
        return rough_km, rough_mins

    # --------------------------------------------------------------高德 API
    # 粗粒度/非居住地标：命中说明地址没被真正识别，坐标是区县兜底
    _GEO_BAD_LEVEL = {"国家", "省", "城市", "市", "区县", "开发区",
                      "公交地铁站点"}

    def _origin_location(self, lst) -> str | None:
        """房源精确坐标；没有则用小区名地理编码。
        两道可信度校验：①geocode 粒度必须细于区县（否则高德会返回
        朝阳区政府坐标兜底）；②坐标必须与卡片声明的地铁站相容
        （品牌公寓同名 POI 可能在十几公里外）。"""
        if lst.lng and lst.lat:
            return f"{lst.lng},{lst.lat}"
        if lst.community:
            loc = self._geocode(f"北京市朝阳区{lst.community}")
            if loc and self._consistent_with_station(lst, loc):
                return loc
        return None

    def _consistent_with_station(self, lst, loc: str) -> bool:
        """地理编码坐标与卡片声明的地铁站不能矛盾：
        标题/标签提到的站，坐标必须在站周围 2km 内；
        卡片给了明确步行米数时收紧到站距 +0.8km。"""
        name = lst.station
        if not name:
            name, _ = self.find_station(
                " ".join([lst.title or "", lst.tags or "", lst.note or ""]))
        st = self.stations.get(name or "")
        if not st or st.get("lng") is None:
            return True
        lng_s, lat_s = (float(x) for x in loc.split(","))
        d = self._haversine_km(lng_s, lat_s, st["lng"], st["lat"])
        limit = max(1.5, lst.station_distance_m / 1000 + 0.8
                    ) if lst.station_distance_m else 2.0
        return d <= limit

    def _amap_ride(self, lst, area: dict | None) -> tuple[int, int] | None:
        """高德 v5 电动车路径规划（个人 key 100 次/日，并发 1 次/秒），
        返回 (路径米, 分钟)；配额用完/失败返回 None 由上层降级。
        按 origin 持久化缓存。"""
        origin = self._origin_location(lst)
        if not origin:
            return None
        if origin in self._ride_cache:
            return self._ride_cache[origin]
        dest = f"{self.work['lng']},{self.work['lat']}"
        try:
            time.sleep(0.25)  # 并发上限 1 次/秒
            r = requests.get(
                "https://restapi.amap.com/v5/direction/electrobike",
                params={"key": self.key, "origin": origin,
                        "destination": dest},
                timeout=8)
            paths = (r.json().get("route") or {}).get("paths") or []
        except Exception:
            return None
        if not paths:
            return None
        p = paths[0]
        result = (int(p["distance"]), max(1, round(int(p["duration"]) / 60)))
        self._ride_cache[origin] = result
        self._save_disk_cache()
        return result

    def _geocode(self, address: str) -> str | None:
        if address in self._geo_cache:
            return self._geo_cache[address]
        try:
            r = requests.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"address": address, "city": "北京", "key": self.key,
                        "extensions": "base"},
                timeout=8,
            )
            g = (r.json().get("geocodes") or [{}])[0]
            loc = g.get("location")
            # 粒度太粗 = 没真正识别（返回区政府坐标兜底），不可信
            if g.get("level") in self._GEO_BAD_LEVEL or not loc:
                loc = None
        except Exception:
            loc = None
        self._geo_cache[address] = loc
        if loc:
            self._save_disk_cache()
        return loc
