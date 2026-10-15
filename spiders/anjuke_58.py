"""安居客 / 58 同城采集（低可信渠道，默认关闭）。

- 两站同属 58 系，反爬共用一套：触发风控会 302 到
  callback.58.com/antibot/verifycode（"访问过于频繁"），需人工点验证码。
- 强制有头运行；遇到验证页会弹系统通知，等用户在浏览器里点选（最长 5 分钟）。
- 虚假房源较多，confidence 强制压低，推荐排序自动降权。
- 默认在 filters.json 中 enabled=false，贝壳/自如/豆瓣够用；
  想补充这两个渠道时改 true，首次需人工过一次验证码。
"""
from __future__ import annotations

import time
from urllib.parse import quote

from spiders.base import BrowserSpider, log


def _is_blocked(spider: BrowserSpider, html: str) -> bool:
    try:
        u = spider._page.url
    except Exception:
        u = ""
    return ("antibot/verifycode" in u
            or "请输入验证码" in (html or "")
            or "访问过于频繁" in (html or ""))


def _wait_verify(spider: BrowserSpider, target_url: str,
                 item_selector: str) -> str | None:
    try:
        from core.notify import notify
        notify("58/安居客需要验证码",
               "请在弹出的浏览器窗口完成点选验证，完成后采集自动继续（5分钟内有效）")
    except Exception:
        pass
    log.warning("[%s] 触发 58 风控验证，请在浏览器中点选，等待至多 5 分钟",
                spider.source)
    try:
        spider._page.bring_to_front()
    except Exception:
        pass
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(3)
        try:
            html = spider._page.content()
        except Exception:
            continue
        if not _is_blocked(spider, html):
            try:
                spider._page.goto(target_url, wait_until="domcontentloaded",
                                  timeout=30000)
                spider._page.wait_for_selector(item_selector, timeout=15000)
                return spider._page.content()
            except Exception:
                return spider._page.content()
    log.error("[%s] 等待验证码超时，跳过本渠道（可稍后重跑）", spider.source)
    return None


class AnjukeSpider(BrowserSpider):
    source = "anjuke"
    login_url = "https://bj.zu.anjuke.com/"

    URL_TMPL = "https://bj.zu.anjuke.com/fangyuan/p{page}/?kw={kw}"
    SELECTORS = {
        "item": "div.zu-itemmod, .zu-list .zu-itemmod",
        "link": ".zu-info h3 a, .zu-info a[href]",
        "desc": ".zu-info p.details, .details-item, .zu-info p",
        "price": ".zu-side p.price, .price b, b.price",
    }

    def collect(self, headless: bool = False) -> int:
        return _collect(self, headless=headless, confidence=40)


class A58Spider(BrowserSpider):
    source = "58"
    login_url = "https://bj.58.com/zufang/"

    URL_TMPL = "https://bj.58.com/zufang/pg{page}/?key={kw}"
    SELECTORS = {
        "item": "section.list-main li, ul.house-list li, li.house-cell",
        "link": "a.t, h3.title a, a[href*='zufang']",
        "desc": "p.hyd, .info-desc, p.baseinfo",
        "price": "b.rent, .money b, .price",
    }

    def collect(self, headless: bool = False) -> int:
        return _collect(self, headless=headless, confidence=35)


def _collect(spider: BrowserSpider, headless: bool, confidence: int) -> int:
    if headless:
        log.info("[%s] 无头必触发验证码，本渠道改用有头浏览器", spider.source)
    saved = 0
    spider.open(headless=False)
    try:
        for area in sorted(spider.areas_cfg["areas"],
                           key=lambda a: a.get("priority", 9)):
            for kw in area["keywords"]:
                old_streak = 0
                for page in range(1, spider.cfg.get("max_pages", 4) + 1):
                    url = spider.URL_TMPL.format(page=page, kw=quote(kw))
                    html = spider.fetch_html(
                        url, wait_selector=spider.SELECTORS["item"])
                    if _is_blocked(spider, html):
                        html = _wait_verify(spider, url,
                                            spider.SELECTORS["item"])
                        if html is None:
                            log.info("[%s] 采集提前结束，已入库 %d 条",
                                     spider.source, saved)
                            return saved
                    soup = spider.soup(html)
                    items = soup.select(spider.SELECTORS["item"])
                    if not items:
                        log.info("[%s] %s 第%d页无结果，保存快照",
                                 spider.source, kw, page)
                        spider.snapshot(f"{spider.source}_{kw}_p{page}")
                        break
                    page_new = 0
                    for it in items:
                        lst = _parse_card(spider, it, area, confidence)
                        if lst is None:
                            continue
                        _lid, is_new = spider.save(lst)
                        page_new += int(is_new)
                        saved += int(is_new)
                        old_streak = old_streak + 1 if not is_new else 0
                    log.info("[%s] %s 第%d页: %d 条，新上 %d",
                             spider.source, kw, page, len(items), page_new)
                    if old_streak >= 10:
                        break
                    spider.delay()
    finally:
        spider.close()
    log.info("[%s] 采集完成，新增 %d 条", spider.source, saved)
    return saved


def _parse_card(spider: BrowserSpider, it, area: dict, confidence: int):
    sel = spider.SELECTORS
    link_el = it.select_one(sel["link"])
    if not link_el or not link_el.get("href"):
        return None
    url = link_el.get("href", "")
    if url.startswith("//"):
        url = "https:" + url
    sid = url.split("?")[0]

    title = link_el.get_text(" ", strip=True)
    desc_parts = [el.get_text(" ", strip=True) for el in it.select(sel["desc"])]
    desc = " ".join(desc_parts)
    price_parts = [el.get_text(" ", strip=True) for el in it.select(sel["price"])]
    price_text = " ".join(price_parts)

    lst = spider.build_listing(
        spider.source, sid, url, title, desc, price_text, area,
        confidence=confidence, publisher="中介",
    )
    if lst is None:
        return None
    lst.note = "低可信度渠道,务必实地核实"
    return lst
