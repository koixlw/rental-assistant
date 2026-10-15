"""自如整租采集（www.ziroom.com，北京）。

- bj.ziroom.com 已整站 403，必须走 www 域；EdgeOne WAF 会拦无头浏览器，强制有头。
- 列表 URL：/z/z2-p{页码}/?qwd={关键词}（z2=整租）。
- 价格是数字雪碧图反爬：每个 span.num 带 background-position（步长 21.4px），
  槽位→数字的映射按雪碧图 hash 存 data/state/ziroom_price_map.json；
  遇到没见过的雪碧图会自动下载 png 到 data/state/ 并发警告，补映射后重跑即可。
- 自如服务费一般约为月租的 8%-10%（以合同为准），标签中提示。
"""
from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import quote

from spiders.base import BrowserSpider, log

BASE = "https://www.ziroom.com"
SPRITE_STEP = 21.4  # 数字槽位步长（CSS px），位置 -192.6px => 槽位 9
# 参考雪碧图（data/state/ziroom_ref.png）槽位 0..9 的数字排列
REF_ORDER = [6, 5, 4, 7, 0, 9, 8, 2, 1, 3]


class ZiroomSpider(BrowserSpider):
    source = "ziroom"
    login_url = "https://www.ziroom.com/"

    URL_TMPL = BASE + "/z/z2-p{page}/?qwd={kw}"
    # 自如寓（品牌公寓）卡片走独立域名
    APT_RE = r"ziroomapartment\.com/houseType/getDetail/([0-9a-fA-F]+)\.html"
    SELECTORS = {
        "item": ".Z_list-box .item",
        "link": "a[href*='/x/']",
        "apt_link": "a[href*='ziroomapartment.com/']",
        "title": "h5 a",
        "desc": ".info-box .desc > div",
        "location": ".location",
        "price": ".price",
    }

    def is_logged_in(self, force_probe: bool = False) -> bool:
        """自如列表页不强制登录；页面正常渲染（非 403/拦截）即可用。"""
        try:
            pg = self._page
            if pg.is_closed() or "ziroom.com" not in pg.url:
                return False
            title = pg.title() or ""
            if "403" in title or "拦截" in title:
                return False
            return "自如" in title
        except Exception:
            return False

    def _load_price_map(self) -> dict:
        from core.config import STATE_DIR
        self._map_path = STATE_DIR / "ziroom_price_map.json"
        try:
            return json.loads(self._map_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _sprite_hash(bg_url: str) -> str:
        m = re.search(r"/price/new-list/([0-9a-f]+)\.png", bg_url or "")
        return m.group(1) if m else ""

    def _download_sprite(self, h: str) -> bytes | None:
        url = (f"https://static8.ziroom.com/phoenix/pc/images/price/"
               f"new-list/{h}.png")
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://www.ziroom.com/",
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/127 Safari/537.36"),
            })
            return urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            log.warning("[ziroom] 雪碧图下载失败 %s: %s", h, e)
            return None

    def _learn_sprite(self, h: str, price_map: dict) -> list | None:
        """新雪碧图只是把同一套字形重新洗牌：下载后用 alpha 通道
        与参考图 ziroom_ref.png 逐格做像素匹配，自动得出槽位→数字映射。"""
        import io
        from core.config import STATE_DIR
        ref_path = STATE_DIR / "ziroom_ref.png"
        data = self._download_sprite(h)
        if not data or not ref_path.exists():
            return None
        try:
            from PIL import Image
            ref = Image.open(ref_path).convert("RGBA").split()[3]
            new = Image.open(io.BytesIO(data)).convert("RGBA").split()[3]
            if new.size != ref.size or new.size[0] % 10:
                raise ValueError(f"尺寸异常 {new.size}")
            cw = new.size[0] // 10

            def cells(im):
                return [im.crop((i * cw, 0, (i + 1) * cw, im.size[1])).tobytes()
                        for i in range(10)]

            ref_cells, new_cells = cells(ref), cells(new)

            def diff(a: bytes, b: bytes) -> int:
                return sum(abs(x - y) for x, y in zip(a, b))

            order, used = [], set()
            for c in new_cells:
                scores = sorted((diff(c, rc), j) for j, rc in enumerate(ref_cells))
                best, j = scores[0]
                if best != 0 or j in used:  # 必须逐格全等且一一对应
                    raise ValueError("模板匹配不唯一/非零误差")
                used.add(j)
                order.append(REF_ORDER[j])
            price_map[h] = order
            self._map_path.write_text(
                json.dumps(price_map, ensure_ascii=False, indent=1),
                encoding="utf-8")
            log.info("[ziroom] 自动识别新价格雪碧图 %s -> %s", h[:12], order)
            return order
        except Exception as e:
            png = STATE_DIR / f"ziroom_sprite_{h}.png"
            try:
                png.write_bytes(data)
            except Exception:
                pass
            log.warning("[ziroom] 雪碧图自动识别失败(%s)，图已存 %s，可人工补映射",
                        e, png.name)
            return None

    def _decode_price(self, card, price_map: dict) -> float | None:
        price_el = card.select_one(self.SELECTORS["price"])
        if not price_el:
            return None
        digits = ""
        for sp in price_el.select("span.num"):
            style = sp.get("style", "")
            mh = re.search(r"new-list/([0-9a-f]+)\.png", style)
            mp = re.search(r"background-position:\s*(-?\d+(?:\.\d+)?)px", style)
            if not mh or not mp:
                continue
            h = mh.group(1)
            order = price_map.get(h)
            if order is None and h not in self._tried_sprites:
                self._tried_sprites.add(h)
                order = self._learn_sprite(h, price_map)
            if not order:
                continue
            idx = int(round(-float(mp.group(1)) / SPRITE_STEP))
            if 0 <= idx <= 9:
                digits += str(order[idx])
        return float(digits) if digits else None

    def collect(self, headless: bool = False) -> int:
        # EdgeOne WAF 拦截无头浏览器，强制有头
        if headless:
            log.info("[ziroom] 无头模式会被 WAF 拦截，本渠道改用有头浏览器")
        price_map = self._load_price_map()
        self._tried_sprites: set[str] = set()
        self.open(headless=False)
        saved = 0
        try:
            for area in sorted(self.areas_cfg["areas"],
                               key=lambda a: a.get("priority", 9)):
                for kw in area["keywords"]:
                    old_streak = 0
                    for page in range(1, self.cfg.get("max_pages", 3) + 1):
                        url = self.URL_TMPL.format(kw=quote(kw), page=page)
                        html = self.fetch_html(
                            url, wait_selector=self.SELECTORS["item"])
                        soup = self.soup(html)
                        items = [it for it in soup.select(self.SELECTORS["item"])
                                 if it.get("data-inv-no")]
                        if not items:
                            log.info("[ziroom] %s 第%d页无结果，保存快照", kw, page)
                            self.snapshot(f"ziroom_{kw}_p{page}")
                            break
                        page_new = 0
                        for it in items:
                            lst = self._parse_card(it, area, price_map)
                            if lst is None:
                                continue
                            _lid, is_new = self.save(lst)
                            page_new += int(is_new)
                            saved += int(is_new)
                            old_streak = old_streak + 1 if not is_new else 0
                        log.info("[ziroom] %s 第%d页: %d 条，新上 %d",
                                 kw, page, len(items), page_new)
                        if old_streak >= 10:
                            break
                        self.delay()
        finally:
            self.close()
        log.info("[ziroom] 采集完成，新增 %d 条", saved)
        return saved

    def _parse_card(self, it, area: dict, price_map: dict):
        # 两类卡片：普通房源 /x/{9位id}.html；自如寓走 ziroomapartment.com GUID
        apt_el = it.select_one(self.SELECTORS["apt_link"])
        if apt_el and re.search(self.APT_RE, apt_el.get("href", "")):
            href = apt_el.get("href", "")
            guid = re.search(self.APT_RE, href).group(1)
            url = ("https:" + href) if href.startswith("//") else href
            sid = "apt_" + guid
        else:
            link_el = it.select_one(self.SELECTORS["link"])
            href = link_el.get("href", "") if link_el else ""
            m = re.search(r"/x/(\d+)\.html", href)
            if not m:
                return None
            sid = m.group(1)
            url = ("https:" + href) if href.startswith("//") else href

        title = ""
        for sel in ("h5", "h5 a", ".title-info a", "a[href*='/x/']"):
            el = it.select_one(sel)
            if el and el.get_text(strip=True):
                title = el.get_text(" ", strip=True)
                break

        desc_parts = [d.get_text(" ", strip=True)
                      for d in it.select(self.SELECTORS["desc"])]
        loc_el = it.select_one(self.SELECTORS["location"])
        if loc_el:
            desc_parts.append(loc_el.get_text(" ", strip=True))
        desc = " ".join(p for p in desc_parts if p)

        if not self.area_relevant(f"{title} {desc}", area):
            log.info("[ziroom] 模糊结果丢弃(%s 关键词下): %s",
                     area["name"], title[:24])
            return None

        rent = self._decode_price(it, price_map)
        if not rent:
            return None  # 解不出价格（通常是停租/未知雪碧图），无法入硬筛选

        lst = self.build_listing(
            self.source, sid, url, title, desc, price_text=f"{rent:g}元",
            area=area, confidence=85, publisher="公寓",
        )
        if lst is None:
            return None
        lst.rent_type = "整租"
        lst.private_bath = 1
        if sid.startswith("apt_"):
            lst.tags = (lst.tags + " 自如寓(品牌公寓,ziroomapartment.com)").strip()
        else:
            lst.tags = (lst.tags + " 自如公寓(服务费另计约月租8%-10%)").strip()
        return lst
