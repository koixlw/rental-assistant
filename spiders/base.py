"""爬虫基类与通用解析：Playwright 有头浏览器、登录态复用、限速、失败快照。

注意：各渠道页面结构经常变化，SELECTORS/URL_TMPL 集中定义在各 spider 顶部，
解析失败时会把 HTML 与截图存到 data/snapshots/，按快照单点修复即可。
字段提取以"CSS 选取 + 文本正则兜底"双层方式工作。
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.config import LOG_DIR, SNAP_DIR, STATE_DIR, PROFILE_DIR, ensure_dirs
from core.geo import GeoEstimator
from core.models import Listing

TAG_KEYWORDS = [
    "独立卫浴", "独卫", "阳台", "有电梯", "无电梯", "电梯房", "精装修", "精装",
    "简装修", "简装", "毛坯", "近地铁", "随时看房", "押一付一", "集中供暖",
    "包取暖", "包供暖", "民水民电", "拎包入住", "朝南", "房东直租", "无中介费",
    " loft", "LOFT", "复式", "双层", "可短租",
]

# ----------------------------------------------------------------------日志
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("room")
    if not logger.handlers:
        ensure_dirs()
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh = logging.FileHandler(LOG_DIR / "spider.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


log = setup_logger()


# ------------------------------------------------------------------文本解析
def pick_price(*texts: str) -> float | None:
    blob = " ".join(t or "" for t in texts)
    patterns = [
        r"(\d{3,5})\s*元\s*/\s*月",
        r"(\d{3,5})\s*/\s*月",
        r"月租\D{0,4}(\d{3,5})",
        r"租金\D{0,4}(\d{3,5})",
        r"(\d{3,5})\s*元(?!\s*每)",
    ]
    for pat in patterns:
        m = re.search(pat, blob)
        if m:
            val = float(m.group(1))
            if 500 <= val <= 100000:
                return val
    return None


def parse_rent_info(text: str) -> dict:
    """从房源描述文本提取户型/面积/朝向/楼层等，缺失字段返回 None。"""
    t = re.sub(r"\s+", " ", text or "")
    info: dict = {
        "rent_type": "", "rooms": None, "halls": None, "layout": "",
        "is_studio": 0, "area_sqm": None, "orientation": "", "floor": "",
        "total_floor": None, "elevator": None, "private_bath": None,
        "balcony": None, "decoration": "", "building_age": "", "tags": "",
    }

    m = re.search(r"(整租|合租)", t)
    if m:
        info["rent_type"] = m.group(1)

    m = re.search(r"(\d)\s*室\s*(\d)?\s*厅", t)
    if m:
        info["rooms"] = int(m.group(1))
        info["halls"] = int(m.group(2) or 0)
        if info["rooms"] == 1:
            info["layout"] = "一室一厅" if info["halls"] >= 1 else "一居室"
        else:
            info["layout"] = f"{info['rooms']}室{info['halls']}厅"
    elif "开间" in t:
        info["is_studio"] = 1
        info["rooms"] = 1
        info["halls"] = 0
        info["layout"] = "开间"
    else:
        # 自如标题用"1居/2居"，贝壳部分卡片用"3房间1卫"
        m = re.search(r"(\d)\s*居(?!民|住)", t) or re.search(r"(\d)\s*房间", t)
        if m:
            info["rooms"] = int(m.group(1))
            info["halls"] = 0
            info["layout"] = ("一室一厅" if info["rooms"] == 1
                              else f"{info['rooms']}居室")

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|平米|平(?:米)?)(?:\s|$|/|\))", t)
    if m:
        info["area_sqm"] = float(m.group(1))

    ori_tokens = ["南北通透", "东南", "西南", "东北", "西北", "南北", "东西",
                  "朝南", "东向", "西向", "南向", "北向"]
    found = [tok for tok in ori_tokens if tok in t]
    if not found:
        m = re.search(r"(?:^|[\s/])([东南西北])(?:[\s/]|向|$)", t)
        if m:
            found = [m.group(1)]
    info["orientation"] = "".join(dict.fromkeys("".join(found)))[:4]

    m = re.search(r"([高低中])楼层\s*/\s*(\d{1,2})\s*层", t)
    if m:
        info["floor"] = f"{m.group(1)}楼层"
        info["total_floor"] = int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*层", t)
        if m:
            info["floor"] = f"{m.group(1)}层"
            info["total_floor"] = int(m.group(2))

    if "有电梯" in t or "电梯房" in t:
        info["elevator"] = 1
    elif "无电梯" in t:
        info["elevator"] = 0

    if "独卫" in t or "独立卫" in t:
        info["private_bath"] = 1

    if "阳台" in t:
        info["balcony"] = 1

    if "精装" in t:
        info["decoration"] = "精装"
    elif "简装" in t:
        info["decoration"] = "简装"
    elif "毛坯" in t:
        info["decoration"] = "毛坯"

    m = re.search(r"(19\d{2}|20[0-2]\d)\s*年?\s*建", t)
    if m:
        info["building_age"] = f"{m.group(1)}年建"

    tags = [kw for kw in TAG_KEYWORDS if kw in t]
    info["tags"] = " ".join(dict.fromkeys(tags))
    return info


def extract_community(*texts: str) -> str:
    blob = " ".join(t or "" for t in texts)
    m = re.search(
        r"(?:整租|合租)\s*[·\-_]?\s*([0-9A-Za-z一-龥]{2,20}?)"
        r"(?:\s+\d\s*室|\s+\d+(?:\.\d+)?\s*(?:㎡|平米|平)|\s+开间|$)",
        blob,
    )
    if m:
        return m.group(1).strip(" ·-_")
    return ""


# ----------------------------------------------------------------浏览器基类
class BrowserSpider:
    source = "base"
    login_url = "about:blank"

    def __init__(self, db, areas_cfg: dict, src_cfg: dict | None = None):
        self.db = db
        self.areas_cfg = areas_cfg
        self.cfg = src_cfg or {}
        self.geo = GeoEstimator()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def delay(self) -> None:
        a, b = self.cfg.get("delay", [6, 12])
        time.sleep(random.uniform(a, b))

    def open(self, headless: bool = False) -> None:
        """每个渠道使用独立的永久 profile 目录（data/profile/<source>）。
        登录后的 cookie/localStorage 由浏览器实时写入磁盘，即使检测/退出
        出任何岔子，登录态也不会丢失；下次采集复用同一 profile 即已登录。"""
        from playwright.sync_api import sync_playwright

        ensure_dirs()
        (PROFILE_DIR / self.source).mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR / self.source),
            headless=headless,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._browser = self._context.browser
        self._page = (self._context.pages[0]
                      if self._context.pages else self._context.new_page())
        # 登录探针的临时状态（子类使用）
        self._probe_page = None
        self._probe_at = 0.0
        self._login_started = time.time()

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None
        self._probe_page = None

    def is_logged_in(self, force_probe: bool = False) -> bool:
        """子类覆盖：轮询判断当前浏览器上下文是否已完成登录。"""
        return False

    def wait_for_login(self, timeout: int = 480) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.is_logged_in():
                    return True
            except Exception as e:
                # 检测代码自身出错必须可见（历史上 NameError 曾导致无限静默等待）
                log.warning("登录检测异常: %s: %s", type(e).__name__, e)
            remain = int(deadline - time.time())
            print(f"[{self.source}] 等待登录... 剩余 {remain} 秒"
                  f"（在弹出的浏览器里完成登录即可，会自动检测，无需回车）", flush=True)
            time.sleep(2)
        return False

    def save_login(self, url: str | None = None, timeout: int = 480) -> bool:
        """打开有头浏览器等用户登录 -> 关闭 -> 用【同一个永久 profile】无头复查
        -> 复查通过才导出 storage_state。全程 cookie 已实时落盘，复查失败
        也不会丢，重新 login 即可，无需再扫码的概率取决于站点 cookie 有效期。"""
        print(f"[{self.source}] 即将打开浏览器，请在弹出的窗口里完成登录（扫码/短信均可）")
        self.open(headless=False)
        self._page.goto(url or self.login_url, wait_until="domcontentloaded", timeout=60000)
        live = self.wait_for_login(timeout)
        log.info("[%s] 实时检测结果: %s，关闭窗口并用无头模式复查...",
                 self.source, "已登录" if live else "未明确，进入复查")
        self.close()

        self.open(headless=True)
        try:
            verified = self.is_logged_in(force_probe=True)
        except Exception as e:
            log.warning("[%s] 复查异常: %s", self.source, e)
            verified = False
        if verified:
            path = STATE_DIR / f"{self.source}_state.json"
            self._context.storage_state(path=str(path))
            try:
                cknames = sorted({c.get("name", "") for c in self._context.cookies()})
                log.info("[%s] cookie(%d): %s", self.source, len(cknames),
                         ",".join(cknames[:40]))
            except Exception:
                pass
        self.close()
        if verified:
            log.info("[%s] 登录成功，登录态已确认并保存", self.source)
        else:
            log.warning("[%s] 复查未通过：profile 里还没有有效登录态，请重新执行 login",
                        self.source)
        return verified

    def fetch_html(self, url: str, wait_selector: str | None = None,
                   timeout: int = 60000) -> str:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            self._page.wait_for_timeout(random.randint(1200, 3000))
            if wait_selector:
                try:
                    self._page.wait_for_selector(wait_selector, timeout=12000)
                except Exception:
                    pass
            return self._page.content()
        except Exception as e:
            log.warning("抓取失败 %s : %s", url, e)
            self.snapshot(url)
            return ""

    def scroll(self, times: int) -> None:
        for _ in range(times):
            self._page.mouse.wheel(0, 2200)
            self._page.wait_for_timeout(random.randint(1000, 2200))

    def snapshot(self, tag: str) -> None:
        if not self._page:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^0-9A-Za-z]+", "_", tag)[-50:]
        try:
            self._page.screenshot(
                path=str(SNAP_DIR / f"{self.source}_{ts}_{safe}.png"), full_page=True
            )
            (SNAP_DIR / f"{self.source}_{ts}_{safe}.html").write_text(
                self._page.content(), encoding="utf-8"
            )
        except Exception:
            pass

    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "html.parser")

    def area_relevant(self, text: str, area: dict) -> bool:
        """关键词搜索是模糊匹配（搜"南皋"会返回顺义"南法信"、搜"金泰丽湾"
        会返回门头沟同名小区），用卡片自身文本校验是否真在目标片区：
        命中片区关键词/基准站，或识别出近城区站点（到将台<=25分钟，
        含新线东段 null），或同线相邻站（通勤差<=6分钟）。"""
        tokens = list(area.get("keywords", [])) + [area.get("station", "")]
        if any(t and t in (text or "") for t in tokens):
            return True
        st, _dist = self.geo.find_station(text or "")
        if st:
            stations = self.areas_cfg.get("stations", {})
            info = stations.get(st, {})
            tj = info.get("to_jiangtai")
            if tj is None or tj <= 25:
                return True
            base_st = stations.get(area.get("station", ""), {})
            btj = base_st.get("to_jiangtai")
            if btj is not None and abs(tj - btj) <= 6 and \
                    set(info.get("lines", [])) & set(base_st.get("lines", [])):
                return True
        return False

    def build_listing(self, source: str, sid: str, url: str, title: str,
                      desc: str, price_text: str, area: dict,
                      confidence: int, publisher: str = "") -> Listing | None:
        full_text = f"{title} {desc}"
        info = parse_rent_info(full_text)
        rent = pick_price(price_text) or pick_price(desc) or pick_price(title)
        if not sid:
            sid = url
        if not rent or not sid:
            return None
        station, dist = self.geo.find_station(full_text)
        if not station:
            station = area.get("station", "")
        community = extract_community(desc, title)
        return Listing(
            source=source,
            source_id=str(sid),
            url=url,
            title=re.sub(r"\s+", " ", title or "").strip()[:200],
            community=community,
            area_name=area["name"],
            station=station,
            station_distance_m=dist,
            rent=rent,
            rent_type=info["rent_type"],
            layout=info["layout"],
            rooms=info["rooms"],
            halls=info["halls"],
            is_studio=info["is_studio"],
            private_bath=info["private_bath"],
            area_sqm=info["area_sqm"],
            orientation=info["orientation"],
            floor=info["floor"],
            total_floor=info["total_floor"],
            elevator=info["elevator"],
            building_age=info["building_age"],
            decoration=info["decoration"],
            balcony=info["balcony"],
            tags=info["tags"],
            publisher_type=publisher,
            confidence=confidence,
        )

    def save(self, lst: Listing) -> tuple[int, bool]:
        return self.db.upsert(lst)

    def collect(self, headless: bool = False) -> int:
        raise NotImplementedError
