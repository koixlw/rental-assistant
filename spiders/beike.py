"""贝壳/链家整租采集（bj.zu.ke.com）。

反爬较强：首次使用先 `python run.py login --source beike` 人工登录一次，
之后复用登录态，按 filters.json 中 beike.delay 低速翻页。
URL/选择器若随站点改版失效，失败快照会存到 data/snapshots/，在此修正即可。
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote

from spiders.base import BrowserSpider, log


class BeikeSpider(BrowserSpider):
    source = "beike"
    PROBE_URL = "https://bj.zu.ke.com/zufang/rt200600000001rp2rp3/"
    # 不能手写 clogin.ke.com?service=（CAS 会报"未认证授权的服务"）。
    # 正确姿势：打开受保护的筛选组合页，贝壳自己 302 到带合法 service 的登录页，
    # 登录成功后 CAS 自动跳回列表页。
    login_url = PROBE_URL

    # 实测有效的筛选参数（2026-09 通过页面筛选器反查确认）：
    #   rt200600000001 = 整租；rp2 = 2000-3000元；rp3 = 3000-4000元
    URL_TMPL = ("https://bj.zu.ke.com/zufang/rs{kw}/"
                "pg{page}rt200600000001rp2rp3/")
    SELECTORS = {
        "item": "div.content__list--item",
        "link": "p.content__list--item--title a",
        "link_alt": "a.content__list--item--aside",
        "title": "p.content__list--item--title a",
        "desc": "p.content__list--item--des",
        "price": "span.content__list--item-price",
        "brand": ".content__list--item--brand .brand",
    }

    def _functional_check(self) -> bool:
        """功能探针：访问受保护筛选页，能看到房源卡片即登录有效。
        复用同一个后台探针标签，反复 goto，不打扰用户操作的登录标签。"""
        pg = self._probe_page
        try:
            if pg is None or pg.is_closed():
                pg = self._context.new_page()
                self._probe_page = pg
            pg.goto(self.PROBE_URL, wait_until="domcontentloaded", timeout=25000)
            pg.wait_for_timeout(1000)
            u = pg.url
            if "clogin" in u or "/login" in u:
                return False
            return pg.query_selector("div.content__list--item") is not None
        except Exception as e:
            log.debug("[beike] 功能探针异常: %s", e)
            return False

    def is_logged_in(self, force_probe: bool = False) -> bool:
        """两层检测，全程不在用户正在操作的标签上跳转：
        1) URL 扫描：任一标签离开 clogin 登录页、落到 ke.com -> 已登录（2 秒级）；
        2) 15 秒后 URL 扫描仍无结论（页面卡在"登录成功"中转页等）时，
           每 5 秒在一个独立后台标签做一次功能探针。
        force_probe=True 时跳过等待直接做功能探针（用于无头复查）。"""
        if force_probe:
            return self._functional_check()

        if time.time() - self._login_started < 5:
            return False  # 打开后前 5 秒忽略，防止重定向途中的瞬间 URL 误判
        urls = []
        try:
            for pg in self._context.pages:
                try:
                    if pg.is_closed():
                        continue
                    u = pg.url
                except Exception:
                    continue
                urls.append(u)
                if ("ke.com" in u and "clogin" not in u
                        and "/login" not in u and u not in ("about:blank", "")):
                    return True
        except Exception:
            pass

        if time.time() - self._login_started < 15:
            return False
        if time.time() - self._probe_at < 5:
            return False
        self._probe_at = time.time()
        log.info("[beike] URL 扫描未确认（当前标签: %s），启用后台功能探针", urls[:4])
        return self._functional_check()

    SEEN_FILE = None  # 初始化时指向 data/state/beike_seen.json

    def _is_captcha(self, html: str) -> bool:
        if not html:
            return False
        return ("<title>CAPTCHA</title>" in html
                or "人机验证" in html
                or "hip.ke.com" in self._page.url
                or "/captcha" in self._page.url)

    def _is_login_wall(self, html: str) -> bool:
        """登录态过期会 302 到 clogin.ke.com 登录页——该页没有房源卡片，
        必须和"真·无结果"区分开，否则关键词会被误标 seen。"""
        if not html:
            return False
        return ("clogin.ke.com" in self._page.url
                or ("<title>登录</title>" in html and "content__list--item" not in html))

    def _wait_login(self, target_url: str) -> str | None:
        """登录态过期：弹窗提醒用户在浏览器里登录，轮询 wait_min 分钟，
        检测到列表卡片后回到目标页继续。登录成功不标记任何缓存。"""
        wait_min = self.cfg.get("captcha_wait_minutes", 30)
        try:
            from core.notify import notify
            notify("贝壳登录已过期",
                   f"请在浏览器里重新登录贝壳，{wait_min}分钟内完成即自动续采")
        except Exception:
            pass
        log.warning("=" * 60)
        log.warning("[beike] 登录态过期！请在浏览器窗口登录，程序等待至多 %d 分钟", wait_min)
        log.warning("=" * 60)
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        deadline = time.time() + wait_min * 60
        last_notify = time.time()
        while time.time() < deadline:
            time.sleep(3)
            if time.time() - last_notify > 180:
                last_notify = time.time()
                try:
                    from core.notify import notify
                    notify("贝壳仍在等待登录", "浏览器里登录完成后会自动继续")
                except Exception:
                    pass
                print("\a", end="", flush=True)
            try:
                html = self._page.content()
            except Exception:
                continue
            if not self._is_login_wall(html):
                try:
                    self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    self._page.wait_for_selector(self.SELECTORS["item"], timeout=15000)
                    return self._page.content()
                except Exception:
                    pass
        log.error("[beike] 等待登录超时（%d分钟），本次采集中止", wait_min)
        return None

    def _wait_captcha(self, target_url: str) -> str | None:
        """弹出人机验证时的人工接管：弹窗+声音提醒用户在浏览器里点选，
        轮询时间取 filters.json 的 captcha_wait_minutes（默认 30 分钟）；
        等待期间每 3 分钟重复一次提醒（人暂时离开也来得及）。
        验证通过后冷却一段时间再继续，降低短时间内再次触发的概率。"""
        wait_min = self.cfg.get("captcha_wait_minutes", 30)
        try:
            from core.notify import notify
            notify("贝壳需要人机验证",
                   f"请在浏览器窗口点选验证，{wait_min}分钟内完成即自动继续")
        except Exception:
            pass
        log.warning("=" * 60)
        log.warning("[beike] 触发人机验证！请在浏览器窗口完成点选，程序将等待至多 %d 分钟",
                    wait_min)
        log.warning("=" * 60)
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        deadline = time.time() + wait_min * 60
        last_notify = time.time()
        while time.time() < deadline:
            time.sleep(3)
            # 每 3 分钟重复提醒并响铃（\a），避免用户错过唯一一次弹窗
            if time.time() - last_notify > 180:
                last_notify = time.time()
                try:
                    from core.notify import notify
                    notify("贝壳仍在等待点选验证",
                           "浏览器里的验证码还没点，点完自动继续")
                except Exception:
                    pass
                print("\a", end="", flush=True)
            try:
                html = self._page.content()
            except Exception:
                continue
            if not self._is_captcha(html):
                # 验证通过后先冷却，再回到目标列表页
                cd = self.cfg.get("cooldown_after_captcha", [60, 120])
                nap = random.uniform(*cd)
                log.info("[beike] 验证通过，冷却 %.0f 秒后继续", nap)
                time.sleep(nap)
                try:
                    self._page.goto(target_url, wait_until="domcontentloaded",
                                    timeout=30000)
                    self._page.wait_for_selector(
                        self.SELECTORS["item"], timeout=15000)
                    return self._page.content()
                except Exception:
                    return self._page.content()
        log.error("[beike] 等待人工验证超时（%d分钟），本次贝壳采集中止"
                  "（稍后可重跑，已采的不丢）", wait_min)
        return None

    def _load_seen(self) -> dict:
        try:
            return json.loads(self.SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _mark_seen(self, seen: dict, kw: str) -> None:
        seen[kw] = datetime.now().isoformat(timespec="seconds")
        try:
            self.SEEN_FILE.write_text(
                json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.debug("[beike] seen 缓存写入失败: %s", e)

    def collect(self, headless: bool = False) -> int:
        from core.config import STATE_DIR
        self.SEEN_FILE = STATE_DIR / "beike_seen.json"
        # 贝壳会把无头浏览器直接送进 hip.ke.com/forbidden，强制有头运行
        if headless:
            log.info("[beike] 无头模式会被贝壳风控拦截，本渠道改用有头浏览器")
        self.open(headless=False)
        saved = 0
        seen = self._load_seen()
        ttl_h = self.cfg.get("seen_ttl_hours", 12)
        cutoff = datetime.now() - timedelta(hours=ttl_h)
        long_pause_every = self.cfg.get("long_pause_every", 8)
        done_kw = 0
        try:
            for area in sorted(self.areas_cfg["areas"],
                               key=lambda a: a.get("priority", 9)):
                for kw in area["keywords"]:
                    ts = seen.get(kw)
                    if ts:
                        try:
                            if datetime.fromisoformat(ts) > cutoff:
                                log.info("[beike] %s %s 小时内已采，跳过",
                                         kw, ttl_h)
                                continue
                        except ValueError:
                            pass

                    old_streak = 0
                    kw_ok = False
                    for page in range(1, self.cfg.get("max_pages", 5) + 1):
                        url = self.URL_TMPL.format(kw=quote(kw), page=page)
                        try:
                            html = self.fetch_html(
                                url, wait_selector=self.SELECTORS["item"])
                        except Exception as e:
                            log.warning("[beike] %s 第%d页打开异常: %s", kw, page, e)
                            break
                        if self._is_captcha(html):
                            html = self._wait_captcha(url)
                            if html is None:
                                self.close()
                                log.info("[beike] 采集提前结束，新增 %d 条", saved)
                                return saved
                        if self._is_login_wall(html):
                            html = self._wait_login(url)
                            if html is None:
                                self.close()
                                log.info("[beike] 采集提前结束，新增 %d 条", saved)
                                return saved
                        if not html:
                            # 抓取失败（浏览器被关/崩溃）绝不能当成"无结果"，
                            # 否则关键词会被误写进 seen 缓存导致 24h 不再采
                            log.error("[beike] %s 第%d页抓取失败（浏览器可能已关闭），"
                                      "采集中止，已采数据不丢，该关键词未标记", kw, page)
                            self.close()
                            log.info("[beike] 采集提前结束，新增 %d 条", saved)
                            return saved
                        soup = self.soup(html)
                        items = soup.select(self.SELECTORS["item"])
                        if not items:
                            # 真·无结果页（非验证码）才快照并结束该关键词
                            log.info("[beike] %s 第%d页无结果，跳过", kw, page)
                            self.snapshot(f"beike_{kw}_p{page}")
                            kw_ok = True  # 空结果也是有效响应，标记避免重复试探
                            break
                        kw_ok = True
                        page_new = 0
                        for it in items:
                            lst = self._parse_card(it, area)
                            if lst is None:
                                continue
                            _lid, is_new = self.save(lst)
                            page_new += int(is_new)
                            saved += int(is_new)
                            old_streak = old_streak + 1 if not is_new else 0
                        log.info("[beike] %s 第%d页: %d 条，新上 %d",
                                 kw, page, len(items), page_new)
                        if old_streak >= 12:
                            log.info("[beike] %s 后续多为已收录房源，停止翻页", kw)
                            break
                        self.delay()

                    if kw_ok:
                        self._mark_seen(seen, kw)
                        done_kw += 1
                        # 每采若干个关键词强制长休息，降低验证码触发概率
                        if done_kw % long_pause_every == 0:
                            rest = self.cfg.get("long_pause", [35, 75])
                            nap = random.uniform(*rest)
                            log.info("[beike] 已完成 %d 个关键词，长休息 %.0f 秒防风控",
                                     done_kw, nap)
                            time.sleep(nap)
        finally:
            self.close()
        log.info("[beike] 采集完成，新增 %d 条", saved)
        return saved

    def _parse_card(self, it, area: dict):
        sel = self.SELECTORS
        link_el = it.select_one(sel["link"]) or it.select_one(sel["link_alt"])
        if not link_el:
            return None
        href = link_el.get("href", "")
        url = urljoin("https://bj.zu.ke.com/", href)
        # data-house_code 是最稳的房源 ID
        sid = it.get("data-house_code") or ""
        m = re.search(r"/zufang/([A-Za-z0-9]+)\.html", href)
        if not sid:
            sid = m.group(1) if m else href

        title = link_el.get_text(strip=True) or ""
        if not title and link_el.get("title"):
            title = link_el["title"]
        img = it.select_one("img")
        if not title and img and img.get("alt"):
            title = img["alt"]

        desc_el = it.select_one(sel["desc"])
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        # 描述行前三个链接固定为：区 - 商圈 - 小区
        # 行政区是权威位置证据：rs 关键词模糊搜索会把门头沟"金泰丽湾"
        # 等同名远郊小区带进来，必须按区名拦掉（目标片区全部在朝阳区）。
        district = ""
        community = ""
        if desc_el:
            loc_links = desc_el.select("a")
            if len(loc_links) >= 1:
                district = loc_links[0].get_text(strip=True) or ""
            if len(loc_links) >= 3:
                community = (loc_links[2].get("title")
                             or loc_links[2].get_text(strip=True) or "")
        allowed = area.get("districts") or ["朝阳"]
        if district:
            if not any(d in district for d in allowed):
                log.info("[beike] 非目标行政区丢弃(%s 关键词下, %s): %s",
                         area["name"], district, title[:24])
                return None
            if not self.area_relevant(f"{title} {desc}", area):
                log.info("[beike] 同区但片区不符丢弃(%s 关键词下): %s",
                         area["name"], title[:24])
                return None
        elif not self.area_relevant(f"{title} {desc}", area):
            # 无区链接的卡片（多为独栋品牌公寓）：标题须含片区关键词或近城地铁站
            log.info("[beike] 无位置证据丢弃(%s 关键词下): %s",
                     area["name"], title[:24])
            return None
        price_el = it.select_one(sel["price"])
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        brand_el = it.select_one(sel["brand"])
        brand = brand_el.get_text(" ", strip=True) if brand_el else ""

        lst = self.build_listing(
            self.source, sid, url, title, desc, price_text, area,
            confidence=90,
            publisher="公寓" if title.startswith("独栋·") else "中介",
        )
        if lst is None:
            return None
        lst.rent_type = lst.rent_type or "整租"
        if community:
            lst.community = community
        elif title.startswith("独栋·"):
            # 品牌公寓卡片没有小区链接，用品牌+店名兜底，便于展示与去重
            m = re.match(r"独栋·(\S+(?:\s+\S+店)?)", title)
            lst.community = m.group(1).strip()[:30] if m else "品牌公寓"
        # 整租 = 整套私有，卫生间必然独用（页面不单独标注"独卫"）
        lst.private_bath = 1
        if brand:
            lst.tags = (lst.tags + f" {brand}").strip()
        lst.agency_fee = "通常为半个月-1个月租金(以经纪人确认为准)"
        return lst
