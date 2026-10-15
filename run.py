"""北京租房助手命令行入口。

用法：
  python run.py init                          初始化目录，查看依赖安装提示
  python run.py login --source beike          首次使用前人工登录并保存登录态
                                              （beike / ziroom / social / anjuke / 58）
  python run.py collect --source beike        采集单个渠道
  python run.py collect                       采集 filters.json 里启用的全部渠道
  python run.py score                         计算通勤、硬过滤、评分
  python run.py check                         复访旧链接，标记已下架（默认30条）
  python run.py export                        导出当日 Excel 到 room 根目录
  python run.py daily                         采集 -> 下架检查 -> 评分 -> 导出 一条龙
                                              （跑完桌面弹窗通知并自动打开 Excel）

可选 --headless 使用无头模式（定时任务建议加；手动跑默认有头，反爬拦截概率更低）。
可选 --no-open 仅在 daily 中生效：跑完不自动打开 Excel。
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime

from core.config import (
    DB_PATH, STATE_DIR, areas_config, ensure_dirs, filters_config,
)
from core.export import export_csv
from core.geo import GeoEstimator
from core.models import Listing
from core.notify import notify, open_file
from core.score import hard_filter, score_listing
from core.storage import Storage
from spiders.anjuke_58 import A58Spider, AnjukeSpider
from spiders.base import BrowserSpider, log
from spiders.beike import BeikeSpider
from spiders.social import SocialSpider
from spiders.ziroom import ZiroomSpider

SPIDERS = {
    "beike": BeikeSpider,
    "ziroom": ZiroomSpider,
    "anjuke": AnjukeSpider,
    "58": A58Spider,
    "social": SocialSpider,
}

REMOVED_MARKERS = [
    "已下架", "您访问的页面不见了", "房源已下架", "信息已删除",
    "该房源已删除", "页面不存在", "404", "已成交",
]

WARN_HINTS = ("低可信", "服务费", "待人工", "警惕", "实地核实")


def cmd_init(_args) -> None:
    ensure_dirs()
    print("目录已就绪：")
    print(f"  数据库      {DB_PATH}")
    print("首次使用请依次执行：")
    print("  pip install -r requirements.txt")
    print("  python -m playwright install chromium")
    print("  python run.py login --source beike")
    print("  python run.py login --source ziroom")
    print("  python run.py login --source social")
    print("之后双击【每日找房.bat】或等定时任务自动跑即可。")
    print("可选：设置环境变量 AMAP_KEY（高德免费 key）启用精确公交通勤。")


def cmd_login(args) -> None:
    ensure_dirs()
    cls = SPIDERS.get(args.source)
    if cls is None:
        sys.exit(f"未知渠道：{args.source}，可选 {list(SPIDERS)}")
    spider = cls(db=None, areas_cfg=areas_config())
    spider.save_login()


def _state_ready(source: str) -> bool:
    return (STATE_DIR / f"{source}_state.json").exists()


def cmd_collect(args) -> dict:
    """采集，返回 {渠道: 新增条数或错误信息}；单个渠道失败不影响其它渠道。"""
    ensure_dirs()
    flt = filters_config()
    areas = areas_config()
    sources = [args.source] if args.source else [
        name for name, cfg in flt["sources"].items() if cfg.get("enabled")
    ]
    results: dict[str, object] = {}
    db = Storage()
    try:
        for name in sources:
            cls = SPIDERS.get(name)
            if cls is None:
                log.warning("未知渠道：%s，跳过", name)
                continue
            # 自如列表页免登录；anjuke/58 走各自的人工验证码流程
            if name not in ("anjuke", "58", "ziroom") and not _state_ready(name):
                msg = f"未找到登录态，请先执行: python run.py login --source {name}"
                log.warning("[%s] %s", name, msg)
                results[name] = f"跳过(未登录)"
                continue
            try:
                log.info("==== 开始采集：%s ====", name)
                spider = cls(db, areas, flt["sources"].get(name))
                results[name] = spider.collect(headless=args.headless)
            except Exception as e:
                log.error("[%s] 采集异常: %s", name, e)
                log.debug(traceback.format_exc())
                results[name] = f"异常: {e}"
    finally:
        db.close()
    return results


def _row_to_listing(r) -> Listing:
    return Listing(**{k: r[k] for k in Listing.column_names()})


def _keep_warn(note: str) -> str:
    note = note or ""
    bad = ("超出", "超限", "不符", "过小", "非整租", "无价格", "无独卫")
    if any(x in note for x in bad):
        return ""
    return note if any(h in note for h in WARN_HINTS) else ""


def cmd_score(_args) -> None:
    ensure_dirs()
    db = Storage()
    areas = areas_config()
    flt = filters_config()
    geo = GeoEstimator()
    name_map = {a["name"]: a for a in areas["areas"]}
    avgs = db.area_avg_rent()
    try:
        for r in db.all_rows():
            lst = _row_to_listing(r)
            area = name_map.get(lst.area_name)
            if area is None:
                area = geo.match_area(
                    f"{lst.area_name} {lst.community} {lst.title} {lst.tags}"
                )
                if area:
                    lst.area_name = area["name"]
            if not lst.station:
                st, dist = geo.find_station(
                    f"{lst.title} {lst.tags} {lst.community}"
                )
                if st:
                    lst.station = st
                if dist and not lst.station_distance_m:
                    lst.station_distance_m = dist
            mins, _method = geo.estimate(lst, area)
            dist_km, ebike_mins = geo.distance_km(lst, area, method=_method)
            lst.distance_km = dist_km
            # 有高德数据时通勤统一采用电动车导航口径（距离/分钟同源）
            if ebike_mins is not None:
                mins = ebike_mins
                lst.commute_minutes = mins
            reasons = hard_filter(lst, flt)
            score, risk = score_listing(lst, avgs.get(lst.area_name), flt)

            status = r["status"]
            if status != "下架":
                status = "排除" if reasons else ("在租" if status == "排除" else status or "在租")
            note = "；".join(reasons) if reasons else "；".join(
                x for x in [risk, _keep_warn(r["note"])] if x
            )
            db.update_values(
                r["id"],
                commute_minutes=mins,
                distance_km=lst.distance_km,
                score=score,
                status=status,
                note=note,
                area_name=lst.area_name,
                station=lst.station or r["station"],
            )
        print("评分完成，当前库内统计：", db.counts())
    finally:
        db.close()


def cmd_check(args) -> None:
    ensure_dirs()
    db = Storage()
    areas = areas_config()
    try:
        rows = db.rows_for_check(limit=args.limit)
        by_source: dict[str, list] = {}
        for r in rows:
            by_source.setdefault(r["source"], []).append(r)
        for source, items in by_source.items():
            try:
                checker_cls = type(
                    f"Check_{source}",
                    (BrowserSpider,),
                    {"source": source, "login_url": "about:blank"},
                )
                checker = checker_cls(db, areas, {"delay": [6, 12]})
                checker.open(headless=args.headless)
                try:
                    for r in items:
                        try:
                            html = checker.fetch_html(r["url"])
                            if html and any(m in html for m in REMOVED_MARKERS):
                                db.mark_status(r["id"], "下架", "复查时页面显示已下架/删除")
                                log.info("[check] id=%s %s -> 下架",
                                         r["id"], (r["title"] or "")[:30])
                        except Exception as e:
                            log.warning("[check] 复查失败 id=%s: %s", r["id"], e)
                        checker.delay()
                finally:
                    checker.close()
            except Exception as e:
                log.error("[check] 渠道 %s 复查异常: %s", source, e)
    finally:
        db.close()


def cmd_export(_args) -> str:
    ensure_dirs()
    db = Storage()
    try:
        path = export_csv(db)
        print("房源报表已生成：", path)
        print("（CSV 格式，双击用 Excel 打开；本目录下 01_推荐排序.csv 优先看）")
        print("库内统计：", db.counts())
        return path
    finally:
        db.close()


def cmd_daily(args) -> None:
    started = datetime.now()
    print(f"===== 每日找房日报开始 {started:%Y-%m-%d %H:%M} =====")
    collect_args = argparse.Namespace(source=None, headless=args.headless)
    collected = cmd_collect(collect_args)
    try:
        cmd_check(argparse.Namespace(headless=args.headless, limit=15))
    except Exception as e:
        log.error("下架检查阶段异常: %s", e)
    cmd_score(argparse.Namespace())
    path = cmd_export(argparse.Namespace())

    db = Storage()
    try:
        today = started.strftime("%Y-%m-%d")
        fresh = [r for r in db.new_since(today) if r["status"] not in ("排除",)]
        drops = db.price_drops()
        top = db.top_recommended(5)
        counts = db.counts()
    finally:
        db.close()

    lines = [f"在租 {counts.get('在租', 0) + counts.get('降价', 0)} 套",
             f"今日新上 {len(fresh)} 套", f"降价 {len(drops)} 套"]
    errs = [f"{k}:{v}" for k, v in collected.items() if isinstance(v, str)]
    if errs:
        lines.append("异常渠道 " + ",".join(errs))
    summary = "，".join(lines)
    print("===== 日报摘要 =====")
    print(summary)
    for i, r in enumerate(top, 1):
        print(f"  TOP{i} [{r['score']}分] {r['area_name']} "
              f"{r['rent']:.0f}元 {r['title'][:34]}")

    notify("今日找房日报完成", summary[:200])
    if not getattr(args, "no_open", False):
        open_file(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="北京租房助手")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p_login = sub.add_parser("login")
    p_login.add_argument("--source", required=True, choices=list(SPIDERS))

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--source", choices=list(SPIDERS))
    p_collect.add_argument("--headless", action="store_true")

    sub.add_parser("score")

    p_check = sub.add_parser("check")
    p_check.add_argument("--limit", type=int, default=30)
    p_check.add_argument("--headless", action="store_true")

    p_export = sub.add_parser("export")

    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--headless", action="store_true")
    p_daily.add_argument("--no-open", action="store_true")

    args = parser.parse_args()
    {
        "init": cmd_init,
        "login": cmd_login,
        "collect": cmd_collect,
        "score": cmd_score,
        "check": cmd_check,
        "export": cmd_export,
        "daily": cmd_daily,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
