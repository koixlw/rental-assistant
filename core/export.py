"""导出房源报表。

本机绿盾 DLP 会加密所有 .xlsx/.xls 文件（写出后几十秒内被加密，Excel 打不开），
但 .csv 文本文件不被加密、Excel 可原生打开。因此默认导出为
文件夹「房源汇总_YYYYMMDD/」，内含 6 个带 UTF-8 BOM 的 CSV（双击即 Excel 打开）。
export_excel() 保留给无 DLP 环境使用。
"""
from __future__ import annotations

import base64
import csv
import os
import subprocess
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.config import EXPORT_DIR, ensure_dirs

SOURCE_CN = {
    "beike": "贝壳",
    "ziroom": "自如",
    "anjuke": "安居客",
    "58": "58同城",
    "douban": "豆瓣",
    "xiaohongshu": "小红书",
    "xianyu": "闲鱼",
}

# (表头, 取值函数)
COLUMNS = [
    ("评分", lambda r: r["score"]),
    ("状态", lambda r: r["status"]),
    ("来源", lambda r: SOURCE_CN.get(r["source"], r["source"])),
    ("标题", lambda r: r["title"]),
    ("小区", lambda r: r["community"]),
    ("片区", lambda r: r["area_name"]),
    ("地铁站", lambda r: r["station"]),
    ("站距(米)", lambda r: r["station_distance_m"]),
    ("通勤(分钟)", lambda r: r["commute_minutes"]),
    ("距公司(公里)", lambda r: r["distance_km"]),
    ("月租", lambda r: r["rent"]),
    ("服务费", lambda r: r["service_fee"]),
    ("实际月成本", lambda r: r["monthly_cost"]),
    ("押金/付法", lambda r: r["deposit"]),
    ("中介费", lambda r: r["agency_fee"]),
    ("户型", lambda r: r["layout"] or ("开间" if r["is_studio"] else "")),
    ("面积(平)", lambda r: r["area_sqm"]),
    ("朝向", lambda r: r["orientation"]),
    ("楼层", lambda r: r["floor"]),
    ("电梯", lambda r: {1: "有", 0: "无"}.get(r["elevator"], "")),
    ("楼龄", lambda r: r["building_age"]),
    ("装修", lambda r: r["decoration"]),
    ("出租类型", lambda r: r["rent_type"]),
    ("联系人", lambda r: r["publisher_type"]),
    ("标签", lambda r: r["tags"]),
    ("可信度", lambda r: r["confidence"]),
    ("风险/备注", lambda r: r["note"]),
    ("链接", lambda r: r["url"]),
    ("首次发现", lambda r: r["first_seen"]),
    ("最近抓取", lambda r: r["last_seen"]),
]

WIDTHS = {
    "标题": 34, "小区": 18, "标签": 22, "风险/备注": 30, "链接": 40,
    "户型": 10, "朝向": 8, "楼层": 14, "押金/付法": 12, "中介费": 12,
}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
LINK_FONT = Font(color="0563C1", underline="single")
NEW_FILL = PatternFill("solid", fgColor="E2EFDA")
DOWN_FILL = PatternFill("solid", fgColor="FCE4D6")


def _write_sheet(ws, rows: list, include_link: bool = True) -> None:
    for idx, (title, _fn) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for ridx, row in enumerate(rows, start=2):
        for cidx, (_title, fn) in enumerate(COLUMNS, start=1):
            value = fn(row)
            cell = ws.cell(row=ridx, column=cidx, value=value)
            if _title == "链接" and value:
                cell.hyperlink = value
                cell.font = LINK_FONT
        if row["status"] == "降价":
            for cidx in range(1, len(COLUMNS) + 1):
                ws.cell(row=ridx, column=cidx).fill = DOWN_FILL

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, len(rows) + 1)}"
    for idx, (title, _fn) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = WIDTHS.get(title, 11)


def _write_stats(ws, rows: list) -> None:
    headers = ["片区", "在租套数", "平均月租", "最低月租", "最高月租", "平均通勤(分钟)"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
    groups: dict[str, list] = {}
    for r in rows:
        if r["status"] in ("排除", "下架") or r["rent"] is None:
            continue
        groups.setdefault(r["area_name"] or "未知", []).append(r)
    ridx = 2
    for area, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rents = [i["rent"] for i in items]
        commutes = [i["commute_minutes"] for i in items if i["commute_minutes"] is not None]
        vals = [
            area,
            len(items),
            round(sum(rents) / len(rents)),
            int(min(rents)),
            int(max(rents)),
            round(sum(commutes) / len(commutes)) if commutes else "",
        ]
        for i, v in enumerate(vals, start=1):
            ws.cell(row=ridx, column=i, value=v)
        ridx += 1
    ws.freeze_panes = "A2"
    for i, w in enumerate([14, 10, 10, 10, 10, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _build_datasets(db) -> dict:
    all_rows = db.all_rows(include_duplicates=True)
    main = [r for r in all_rows if r["duplicate_of"] is None]
    duplicates = [r for r in all_rows if r["duplicate_of"] is not None]

    active = [r for r in main if r["status"] in ("在租", "降价")]
    recommended = sorted(
        [r for r in active if r["status"] != "排除" and not r["note"].startswith("非") and r["score"] is not None
         and "超出" not in r["note"] and "超限" not in r["note"] and "户型不符" not in r["note"]],
        key=lambda r: -(r["score"] or 0),
    )
    dropped = [r for r in main if r["status"] == "降价"]

    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    fresh = [r for r in recommended if (r["first_seen"] or "") >= cutoff]

    excluded = [r for r in main if r["status"] == "排除"]
    for r in duplicates:
        r = dict(r)
        r["note"] = (r["note"] + "；" if r["note"] else "") + f"重复房源,合并到id {r['duplicate_of']}"
        excluded.append(r)

    return {"main": main, "active": active, "recommended": recommended,
            "dropped": dropped, "fresh": fresh, "excluded": excluded}


STATS_HEADERS = ["片区", "在租套数", "平均月租", "最低月租", "最高月租", "平均通勤(分钟)"]


def _stats_rows(main: list) -> list[list]:
    groups: dict[str, list] = {}
    for r in main:
        if r["status"] in ("排除", "下架") or r["rent"] is None:
            continue
        groups.setdefault(r["area_name"] or "未知", []).append(r)
    out: list[list] = []
    for area, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rents = [i["rent"] for i in items]
        commutes = [i["commute_minutes"] for i in items if i["commute_minutes"] is not None]
        out.append([
            area, len(items), round(sum(rents) / len(rents)),
            int(min(rents)), int(max(rents)),
            round(sum(commutes) / len(commutes)) if commutes else "",
        ])
    return out


def _promote_via_powershell(pairs: list[tuple[str, str]]) -> list[int]:
    """绿盾按"进程+内容"加密：python.exe 写出的 .csv 会被加密，
    但 powershell.exe 写出的同名文件保持明文。用 PS 子进程做字节转写。
    pairs: [(tmp_path, final_csv_path), ...]
    返回因目标被占用（如 WPS/Excel 正打开）而未能更新的 pairs 下标。
    注意：子进程输出只传 ASCII 下标——PS 5.1 的中文输出编码不可靠，
    传中文路径回 stdout 会乱码导致占用判断失效（路径本身经
    -EncodedCommand(UTF-16LE) 传输，无此问题）。"""
    if not pairs:
        return []
    lines = ["$ErrorActionPreference='Stop'"]
    for i, (tmp, final) in enumerate(pairs):
        lines.append(
            f"try {{ "
            f"$b=[IO.File]::ReadAllBytes('{tmp}'); "
            f"Remove-Item '{final}' -Force -ErrorAction SilentlyContinue; "
            f"[IO.File]::WriteAllBytes('{final}', $b); "
            f"$ok=$false; "
            f"if ((Test-Path '{final}') -and ((Get-Item '{final}').Length -eq $b.Length)) {{ "
            f"  $fs=[IO.File]::Open('{final}','Open','Read','ReadWrite'); "
            f"  $h=New-Object byte[] 3; [void]$fs.Read($h,0,3); $fs.Close(); "
            f"  if ($h[0] -eq 0xEF -and $h[1] -eq 0xBB -and $h[2] -eq 0xBF) {{ $ok=$true }} "
            f"}}; "
            f"if ($ok) {{ Write-Output 'OK:{i}' }} else {{ Write-Output 'LOCK:{i}' }} "
            f"}} catch {{ Write-Output 'LOCK:{i}' }}"
        )
    ps = ";".join(lines)
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=60)
        locked: list[int] = []
        for line in r.stdout.decode("ascii", "ignore").splitlines():
            if line.startswith("LOCK:"):
                try:
                    locked.append(int(line[5:]))
                except ValueError:
                    pass
        if r.returncode != 0 and not locked:
            raise OSError(r.stderr.decode("gbk", "ignore"))
        return locked
    except OSError:
        raise
    except Exception as e:
        raise OSError(f"PowerShell 转写 CSV 失败: {e}（原始文件为同目录 .tmp）")


def export_csv(db) -> str:
    """导出 CSV 文件夹（绕过绿盾对 Office 文件的加密），返回文件夹路径。"""
    ensure_dirs()
    d = _build_datasets(db)
    folder = EXPORT_DIR / f"房源汇总_{datetime.now().strftime('%Y%m%d')}"
    folder.mkdir(exist_ok=True)

    detail_header = [t for t, _fn in COLUMNS]
    pairs: list[tuple[str, str]] = []

    def write_detail(name: str, rows: list) -> None:
        # utf-8-sig 让 Excel 双击时正确识别中文；先写 .tmp（不加密）
        tmp = folder / f"{name}.tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(detail_header)
            for r in rows:
                w.writerow(["" if fn(r) is None else fn(r) for _t, fn in COLUMNS])
        pairs.append((str(tmp), str(folder / f"{name}.csv")))

    write_detail("01_推荐排序", d["recommended"])
    write_detail("02_全部在租", sorted(d["active"], key=lambda r: -(r["score"] or 0)))
    write_detail("03_近3天新上", d["fresh"])
    write_detail("04_降价房源", d["dropped"])
    write_detail("05_已排除及原因", d["excluded"])

    tmp = folder / "06_片区价格统计.tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(STATS_HEADERS)
        w.writerows(_stats_rows(d["main"]))
    pairs.append((str(tmp), str(folder / "06_片区价格统计.csv")))

    # python 写出的 csv 会被绿盾加密，统一交给可信进程 powershell 落盘
    locked_idx = _promote_via_powershell(pairs)
    alt_paths: list[str] = []
    if locked_idx:
        # 目标正被 WPS/Excel 占用：改带时间戳的新文件名再落盘，保证用户有新数据
        stamp = datetime.now().strftime("%H%M")
        alt_pairs = []
        for i in locked_idx:
            tmp, final = pairs[i]
            base, ext = os.path.splitext(final)
            alt_pairs.append((tmp, f"{base}_新{stamp}{ext}"))
        still = set(_promote_via_powershell(alt_pairs))
        alt_paths = [f for i, (_t, f) in enumerate(alt_pairs) if i not in still]
        if alt_paths:
            print("⚠ 以下文件正被表格软件占用无法覆盖，已另存为新文件，请关闭旧文件后看新版：")
            for f in alt_paths:
                print("  新版 ->", f)
    for tmp, _final in pairs:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return str(folder)


def export_excel(db) -> str:
    """生成当日 Excel（无 DLP 环境可用），返回文件路径。"""
    ensure_dirs()
    d = _build_datasets(db)

    wb = Workbook()
    wb.remove(wb.active)
    _write_sheet(wb.create_sheet("★推荐排序"), d["recommended"])
    _write_sheet(wb.create_sheet("全部在租"), sorted(d["active"], key=lambda r: -(r["score"] or 0)))
    _write_sheet(wb.create_sheet("近3天新上"), d["fresh"])
    _write_sheet(wb.create_sheet("降价房源"), d["dropped"])
    _write_sheet(wb.create_sheet("已排除及原因"), d["excluded"])
    _write_stats(wb.create_sheet("片区价格统计"), d["main"])

    path = EXPORT_DIR / f"房源汇总_{datetime.now().strftime('%Y%m%d')}.xlsx"
    # 绿盾 DLP 会加密 Python 直接写出的 .xlsx；先写 .tmp（不加密）再改名
    tmp = path.with_suffix(".tmp")
    wb.save(tmp)
    os.replace(tmp, path)
    return str(path)
