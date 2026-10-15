"""硬条件过滤 + 综合评分（0-100）。"""
from __future__ import annotations

from core.models import Listing


def _clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def hard_filter(lst: Listing, flt: dict) -> list[str]:
    """返回不通过原因列表；空列表代表通过硬条件。"""
    reasons: list[str] = []

    allowed_types = flt.get("rent_types", ["整租"])
    if lst.rent_type and lst.rent_type not in allowed_types:
        reasons.append(f"非整租({lst.rent_type})")

    if flt.get("require_private_bath", True) and lst.private_bath == 0:
        reasons.append("无独卫")

    cost = lst.monthly_cost if lst.monthly_cost is not None else lst.rent
    lo = flt["price"]["min"]
    hi = flt["price"]["max"]
    if cost is None:
        reasons.append("无价格信息")
    elif not (lo <= cost <= hi):
        reasons.append(f"实际月成本{cost:.0f}元超出{lo}-{hi}")

    layout = lst.layout or ("开间" if lst.is_studio else "")
    accepted_layouts = flt.get("accepted_layouts", [])
    if not layout:
        reasons.append("户型不明")
    elif layout not in accepted_layouts:
        reasons.append(f"户型不符({layout})")

    min_area = flt.get("min_area_sqm")
    if min_area and lst.area_sqm is not None and lst.area_sqm < min_area:
        reasons.append(f"面积过小({lst.area_sqm:g}平)")

    limit = flt.get("commute_limit_minutes", 60)
    if lst.commute_minutes is not None and lst.commute_minutes > limit:
        reasons.append(f"通勤约{lst.commute_minutes}分钟,超{limit}分钟")

    max_km = flt.get("max_distance_km")
    if max_km and lst.distance_km is not None and lst.distance_km > max_km:
        reasons.append(f"距公司约{lst.distance_km:g}公里,超{max_km}公里")

    return reasons


def score_listing(lst: Listing, area_avg: float | None, flt: dict) -> tuple[float, str]:
    """返回 (评分, 风险提示)。"""
    w = flt["scoring"]
    risk = ""
    cost = lst.monthly_cost if lst.monthly_cost is not None else (lst.rent or 0)

    # 通勤：25 分钟满分，60 分钟约 40 分
    if lst.commute_minutes is None:
        metro_score = 40.0
    else:
        metro_score = _clamp(100 - max(0, lst.commute_minutes - 25) * (60 / 35))

    # 直线距离：3 公里内满分，10 公里约 40 分（每远 1 公里扣 8.6 分）
    # 与地铁耗时 4:6 合成"通勤"项，避免同耗时但实际远近不同的房源同分
    if lst.distance_km is None:
        commute_score = metro_score
    else:
        dist_score = _clamp(100 - max(0, lst.distance_km - 3) * (60 / 7))
        commute_score = 0.6 * metro_score + 0.4 * dist_score

    # 成本：2000 元满分，4000 元 0 分
    cost_score = _clamp((4000 - cost) / 2000 * 100) if cost else 40.0

    layout = lst.layout or ("开间" if lst.is_studio else "")
    layout_score = {"一室一厅": 100, "一居室": 78, "开间": 60}.get(layout, 50)

    # 面积：25 平 40 分，45 平 80 分，60 平以上满分
    if lst.area_sqm is None:
        area_score = 50.0
    else:
        area_score = _clamp(40 + (lst.area_sqm - 25) * (60 / 35))

    comfort = 0.0
    if lst.elevator == 1:
        comfort += 25
    if any(x in lst.orientation for x in ("南",)):
        comfort += 30
    if "精装" in (lst.decoration or "") or "精装" in (lst.tags or ""):
        comfort += 20
    if lst.balcony == 1 or "阳台" in (lst.tags or ""):
        comfort += 15
    if "集中供暖" in (lst.tags or ""):
        comfort += 10
    comfort_score = _clamp(comfort)

    publisher_score = {
        "房东": 100,
        "个人": 90,
        "公寓": 70,
        "中介": 55,
    }.get(lst.publisher_type, 50)

    confidence_score = float(lst.confidence)

    total = (
        commute_score * w["commute"]
        + cost_score * w["cost"]
        + layout_score * w["layout"]
        + area_score * w["area"]
        + comfort_score * w["comfort"]
        + publisher_score * w["publisher"]
        + confidence_score * w["confidence"]
    ) / 100.0

    ratio = flt.get("anomaly_ratio", 0.75)
    if area_avg and cost and cost < area_avg * ratio:
        risk = f"价格低于片区均价{int((1-cost/area_avg)*100)}%,警惕虚假房源"

    return round(total, 1), risk
