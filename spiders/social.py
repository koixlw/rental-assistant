"""社交渠道半自动采集。

- 豆瓣：登录态下按"片区+整租/一居室"关键词检索小组话题；
  首次使用先 `python run.py login --source social` 登录豆瓣（如需验证码人工过一下）。
- 闲鱼/小红书：无稳定公开检索接口，请把房源链接粘到 manual_links.txt，
  每行一条，格式 `平台|链接`（平台可省略，按域名自动识别），collect 时自动补采。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote, unquote

from spiders.base import BrowserSpider, log, pick_price, parse_rent_info

DOMAIN_SOURCE = {
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",
    "goofish.com": "xianyu",
    "2.taobao.com": "xianyu",
    "tb.cn": "xianyu",
    "douban.com": "douban",
}

PLATFORM_HINT = {"小红书": "xiaohongshu", "闲鱼": "xianyu", "豆瓣": "douban"}

# 标题含这些词直接排除（用户只要整租整套）
TITLE_EXCLUDE = ("求租", "合租", "次卧", "主卧", "侧卧", "卧室", "床位",
                 "隔断", "单间", "插间", "日租", "时租")


class SocialSpider(BrowserSpider):
    source = "social"
    login_url = "https://www.douban.com/"

    # 北京租房类活跃小组（按成员量/活跃度排序），列表页结构：
    # table.olt > tr > td.title > a(/group/topic/{id}/)，末列是最后回复时间
    DOUBAN_GROUPS = [
        ("35417", "北京租房·百万人组"),
        ("232413", "北京租房·50万组"),
        ("516673", "北京租房·13万组"),
        ("258919", "北京租房·11万组"),
        ("596202", "北京租房·10万组A"),
        ("467340", "北京租房·10万组B"),
        ("549574", "北京租房·9.6万组"),
        ("419918", "北京租房·8万组"),
        ("519274", "北京租房吧·7万组"),
    ]
    GROUP_TMPL = "https://www.douban.com/group/{gid}/discussion?start={start}"
    TOPIC_URL = "https://www.douban.com/group/topic/{tid}/"

    def is_logged_in(self, force_probe: bool = False) -> bool:
        """豆瓣登录后会写入 httpOnly cookie: dbcl2。"""
        try:
            names = {c.get("name") for c in self._context.cookies()}
            return "dbcl2" in names
        except Exception:
            return False

    def collect(self, headless: bool = False) -> int:
        saved = 0
        self.open(headless=headless)
        try:
            saved += self._collect_douban()
            saved += self._collect_manual_links()
        finally:
            self.close()
        log.info("[social] 采集完成，新增 %d 条", saved)
        return saved

    # --------------------------------------------------------------豆瓣
    def _all_keywords(self) -> list[str]:
        kws = []
        for a in self.areas_cfg["areas"]:
            for k in a.get("keywords", []):
                if k and k not in kws:
                    kws.append(k)
        return kws

    def _match_area(self, text: str) -> dict | None:
        best = None
        for a in self.areas_cfg["areas"]:
            for k in a.get("keywords", []):
                if k and k in text:
                    # priority 数字越小越优先
                    if best is None or a.get("priority", 9) < best.get("priority", 9):
                        best = a
                    break
        return best

    @staticmethod
    def _row_date(text: str) -> date | None:
        """列表时间列：当年显示 'MM-DD HH:MM'，跨年显示 'YYYY-MM-DD'。"""
        text = text.strip()
        now = datetime.now()
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"(\d{1,2})-(\d{1,2})", text)
        if m:
            d = date(now.year, int(m.group(1)), int(m.group(2)))
            if d > now.date() + timedelta(days=2):  # 实际是去年的帖
                d = date(now.year - 1, d.month, d.day)
            return d
        return None

    def _collect_douban(self) -> int:
        saved = 0
        keywords = self._all_keywords()
        max_pages = self.cfg.get("douban_group_pages", 4)
        max_age_days = self.cfg.get("douban_max_age_days", 30)
        max_details = self.cfg.get("douban_max_details", 60)
        detail_opened = 0
        cutoff = datetime.now().date() - timedelta(days=max_age_days)

        for gid, gname in self.DOUBAN_GROUPS:
            group_stop = False
            for page in range(max_pages):
                start = page * 25
                url = self.GROUP_TMPL.format(gid=gid, start=start)
                try:
                    html = self.fetch_html(url, wait_selector="table.olt")
                except Exception as e:
                    log.warning("[douban] %s 列表打开失败: %s", gname, e)
                    self.snapshot(f"douban_{gid}_{start}")
                    break
                soup = self.soup(html)
                rows = [tr for tr in soup.select("table.olt tr")
                        if tr.select_one("td.title a")]
                if not rows:
                    break

                page_dates = []
                page_new = 0
                for tr in rows:
                    a = tr.select_one("td.title a")
                    href = a.get("href", "")
                    m = re.search(r"/group/topic/(\d+)/", href)
                    if not m:
                        continue
                    tds = tr.select("td")
                    row_dt = self._row_date(tds[-1].get_text(" ", strip=True)
                                            if tds else "")
                    if row_dt:
                        page_dates.append(row_dt)
                        if row_dt < cutoff:
                            group_stop = True
                            continue

                    title = a.get_text(" ", strip=True)
                    if any(bad in title for bad in TITLE_EXCLUDE):
                        continue
                    if not any(k in title for k in keywords):
                        continue  # 不涉及目标片区，不进详情，省请求

                    tid = m.group(1)
                    sid = f"douban_{tid}"
                    area = self._match_area(title) or {
                        "name": "待核实", "station": "", "bike_direct": None}
                    body = ""
                    # 标题没有价格才进详情页（列表里约一半标题直接带价）
                    if pick_price(title) is None and detail_opened < max_details:
                        body, title = self._fetch_topic(tid, title)
                        detail_opened += 1
                        if body is None:  # 页面打不开/被删
                            continue

                    full_title = title
                    if "合租" in f"{title} {body[:80]}":
                        continue  # 详情确认是合租则剔除
                    lst = self.build_listing(
                        "douban", sid, self.TOPIC_URL.format(tid=tid),
                        full_title, body, price_text="", area=area,
                        confidence=60,
                    )
                    if lst is None:
                        continue
                    # 非合租的开间/一居/一室户视为整租，卫生间天然独用
                    if (lst.is_studio or lst.rooms == 1) and not lst.rent_type:
                        lst.rent_type = "整租"
                    if lst.rent_type == "整租" and not lst.private_bath:
                        lst.private_bath = 1
                    if any(w in f"{full_title} {body[:200]}"
                           for w in ("直租", "房东", "业主", "个人转", "无中介")):
                        lst.publisher_type = "房东"
                    if "无中介" in full_title or "无中介" in body:
                        lst.tags = (lst.tags + " 无中介费").strip()
                    _lid, is_new = self.save(lst)
                    page_new += int(is_new)
                    saved += int(is_new)

                log.info("[douban] %s 第%d页: %d 行，新上 %d",
                         gname, page + 1, len(rows), page_new)
                if group_stop:
                    log.info("[douban] %s 已翻到 %d 天前的帖，停止翻页",
                             gname, max_age_days)
                    break
                self.delay()
        log.info("[douban] 详情页累计打开 %d 个（上限 %d）",
                 detail_opened, max_details)
        return saved

    def _fetch_topic(self, tid: str, list_title: str) -> tuple[str | None, str]:
        """打开话题详情，返回 (正文前1000字, 更完整的标题)。失败返回 (None, 原标题)。"""
        try:
            self._page.goto(self.TOPIC_URL.format(tid=tid),
                            wait_until="domcontentloaded", timeout=45000)
            self._page.wait_for_timeout(1800)
            soup = self.soup(self._page.content())
        except Exception as e:
            log.warning("[douban] 话题 %s 打开失败: %s", tid, e)
            return None, list_title
        h1 = soup.select_one("h1")
        title = h1.get_text(" ", strip=True) if h1 else list_title
        body_el = soup.select_one("#link-report") or soup.select_one(".topic-content")
        body = body_el.get_text(" ", strip=True)[:1000] if body_el else ""
        self.delay()
        return body, title or list_title

    # -----------------------------------------------------手动链接（闲鱼/小红书等）
    def _collect_manual_links(self) -> int:
        from core.config import MANUAL_LINKS

        if not MANUAL_LINKS.exists():
            return 0
        saved = 0
        for raw in MANUAL_LINKS.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            source = "xianyu"
            if "|" in line:
                hint, url = line.split("|", 1)
                source = PLATFORM_HINT.get(hint.strip(), source)
                url = url.strip()
            else:
                url = line
                for domain, name in DOMAIN_SOURCE.items():
                    if domain in url:
                        source = name
                        break
            sid = self._detail_sid(source, url)
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self._page.wait_for_timeout(2500)
                title = self._page.title() or ""
                body = self._page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
                )
            except Exception as e:
                log.warning("[manual] 打开失败 %s : %s", url, e)
                self.snapshot(f"manual_{sid}")
                continue

            text = f"{title} {body}"
            if pick_price(text) is None:
                log.info("[manual] %s 未识别到价格，跳过", sid)
                continue
            area = self.geo.match_area(text) or {
                "name": "手动补录", "station": "", "bike_direct": None
            }
            lst = self.build_listing(
                source, sid, url, title, body[:1500], price_text="",
                area=area, confidence=45,
            )
            if lst is None:
                continue
            lst.rent_type = lst.rent_type or "整租"
            lst.note = "手工链接-请人工核实户型/独卫/价格"
            info = parse_rent_info(text)
            if not lst.layout:
                lst.layout = info["layout"]
                lst.is_studio = info["is_studio"]
                lst.area_sqm = info["area_sqm"]
            _lid, is_new = self.save(lst)
            saved += int(is_new)
            log.info("[manual] %s -> %s 新上=%s", source, sid, is_new)
            self.delay()
        return saved

    @staticmethod
    def _detail_sid(source: str, url: str) -> str:
        if source == "xiaohongshu":
            m = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", url)
        elif source == "xianyu":
            m = re.search(r"[?&]id=(\d+)", url)
        else:
            m = re.search(r"topic/(\d+)", unquote(url))
        return f"{source}_{m.group(1)}" if m else f"{source}_{url}"
