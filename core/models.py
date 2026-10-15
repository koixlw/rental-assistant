"""统一房源数据结构（所有渠道归一化后写入 SQLite）。"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional


@dataclass
class Listing:
    source: str
    source_id: str
    url: str = ""
    title: str = ""
    community: str = ""
    area_name: str = ""
    station: str = ""
    station_distance_m: Optional[int] = None
    lng: Optional[float] = None
    lat: Optional[float] = None
    rent: Optional[float] = None
    deposit: str = ""
    service_fee: Optional[float] = None
    agency_fee: str = ""
    monthly_cost: Optional[float] = None
    rent_type: str = ""
    layout: str = ""
    rooms: Optional[int] = None
    halls: Optional[int] = None
    bath: Optional[int] = None
    is_studio: int = 0
    private_bath: Optional[int] = None
    area_sqm: Optional[float] = None
    orientation: str = ""
    floor: str = ""
    total_floor: Optional[int] = None
    elevator: Optional[int] = None
    building_age: str = ""
    decoration: str = ""
    balcony: Optional[int] = None
    tags: str = ""
    publisher_type: str = ""
    confidence: int = 60
    published_at: str = ""
    status: str = "在租"
    first_seen: str = ""
    last_seen: str = ""
    commute_minutes: Optional[int] = None
    distance_km: Optional[float] = None
    score: Optional[float] = None
    duplicate_of: Optional[int] = None
    note: str = ""

    @classmethod
    def column_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]
