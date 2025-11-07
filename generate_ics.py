#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime as dt
import hashlib
import json
import os
import urllib.request

import yaml                      # 读取 rotations.yml
from lunardate import LunarDate  # 农历节日（元宵/七夕/重阳等）
from lunar_python import Solar   # 二十四节气

# ===== 基本参数 =====
DAYS_AHEAD = 270
TZID = "Asia/Shanghai"
CAL_NAME = "北京尾号限行 + 放假/节日/二十四节气提醒（节假日自动）"
CAL_DESC = "工作日限行（含调休上班）；周末/法定节假日不限行。节假日与调休自动同步；二十四节气、国内节日与“洋节”自动提示；若与放假重合，仅显示放假。范围：五环内（不含），7:00–20:00，字母按0。"

# ===== 轮换表：从 rotations.yml 读取；不存在时也能运行（会出现“轮换未匹配”提示） =====
def read_rotations_yaml(path="rotations.yml"):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    out = []
    for item in raw:
        s = dt.date.fromisoformat(str(item["start"]))
        e = dt.date.fromisoformat(str(item["end"]))
        m = item["map"]  # {Mon:[1,6], ...}
        out.append({
            "start": s, "end": e,
            "map": {0: tuple(m["Mon"]), 1: tuple(m["Tue"]), 2: tuple(m["Wed"]),
                    3: tuple(m["Thu"]), 4: tuple(m["Fri"])}
        })
    return out

ROTATIONS = read_rotations_yaml()

def rotation_for(d: dt.date):
    for ro in ROTATIONS:
        if ro["start"] <= d <= ro["end"]:
            return ro
    return None

# ===== 法定节假日/调休（Timor API，自动：今年+明年） =====
TIMOR_ENDPOINT = "https://timor.tech/api/holiday/year/{}?type=Y&week=Y"
UA = "beijing-xianxing-ics/1.4 (+github actions)"

def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def fetch_cn_calendar(years):
    holidays, adjusted, weekends = {}, set(), set()
    for y in years:
        try:
            data = http_json(TIMOR_ENDPOINT.format(y))
        except Exception:
            continue
        table = data.get("holiday", {}) or {}
        for _, val in table.items():
            try:
                d = dt.date.fromisoformat(val.get("date"))
            except Exception:
                continue
            t = (val.get("type") or {}).get("type")  # 0工作日/1周末/2节假日
            if bool(val.get("holiday")) or t == 2:
                holidays[d] = val.get("name") or "节假日"
            else:
                if d.weekday() >= 5:
                    if t == 0:
                        adjusted.add(d)  # 周末被调为工作日
                    else:
                        weekends.add(d)
    # 连休分块：第 i/共 N 天
    day_idx = {}
    by_name = {}
    for d, nm in holidays.items():
        by_name.setdefault(nm, []).append(d)
    for nm, ds in by_name.items():
        ds.sort()
        i = 0
        while i < len(ds):
            j = i
            while j + 1 < len(ds) and ds[j + 1] == ds[j] + dt.timedelta(days=1):
                j += 1
            total = (ds[j] - ds[i]).days + 1
            for k in range(total):
                day_idx[ds[i] + dt.timedelta(days=k)] = (nm, k + 1, total)
            i = j + 1
    return holidays, adjusted, weekends, day_idx

# ===== 节日层：固定阳历 + 指定周序 + 农历固定 + 二十四节气 =====
SOLAR_FIXED = {  # 常见国内/“洋节”
    (1, 1): "元旦", (2, 14): "情人节", (3, 8): "妇女节", (3, 12): "植树节",
    (4, 1): "愚人节", (5, 4): "青年节", (5, 20): "520", (6, 1): "儿童节",
    (8, 1): "建军节", (9, 10): "教师节", (10, 31): "万圣节",
    (12, 24): "平安夜", (12, 25): "圣诞节",
}
# 母亲节：5月第2个周日；父亲节：6月第3个周日；感恩节：11月第4个周四
WEEKDAY_RULES = [  # (月, 第n个, 周几0-6, 名称)
    (5, 2, 6, "母亲节"), (6, 3, 6, "父亲节"), (11, 4, 3, "感恩节"),
]
# 农历固定（不含已放假的春节/端午/中秋）
LUNAR_FIXED = [  # (农月, 农日, 名称)
    (1, 15, "元宵节"), (7, 7, "七夕"), (9, 9, "重阳节"),
]

def nth_weekday_of_month(year, month, n, weekday):
    d = dt.date(year, month, 1)
    add = (weekday - d.weekday() + 7) % 7
    return d + dt.timedelta(days=add) + dt.timedelta(weeks=n - 1)

def gregorian_from_lunar_for_year(g_year, l_month, l_day, name):
    out = []
    for ly in (g_year - 1, g_year, g_year + 1):
        try:
            d = LunarDate(ly, l_month, l_day).toSolarDate()
            if d.year == g_year:
                out.append((d, name))
        except Exception:
            continue
    seen, result = set(), []
    for d, nm in out:
        if d not in seen:
            seen.add(d)
            result.append((d, nm))
    return result

def build_festival_layer(start: dt.date, end: dt.date):
    fest = {}
    cur = start
    while cur <= end:
        names = set()
        # 固定阳历
        nm = SOLAR_FIXED.get((cur.month, cur.day))
        if nm: names.add(nm)
        # 指定周序节日
        for (m, n, wd, nm2) in WEEKDAY_RULES:
            if cur.month == m and cur == nth_weekday_of_month(cur.year, m, n, wd):
                names.add(nm2)
        # 农历固定
        for (lm, ld, nm3) in LUNAR_FIXED:
            for (dd, nm3_) in gregorian_from_lunar_for_year(cur.year, lm, ld, nm3):
                if dd == cur:
                    names.add(nm3_)
        # 二十四节气
        try:
            lunar = Solar.fromYmd(cur.year, cur.month, cur.day).getLunar()
            jq = lunar.getJieQi()  # 无则空
            if jq:
                names.add(jq)
        except Exception:
            pass
        if names:
            fest[cur] = names
        cur += dt.timedelta(days=1)
    return fest  # dict[date] -> set(names)

# ===== 公用工具 =====
def dtstamp(): return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
def fmt_dt(d, h, m): return f"{d.strftime('%Y%m%d')}T{h:02d}{m:02d}00"
def uid_for(d, s=""): return hashlib.md5(f"bjxx-{d.isoformat()}-{s}".encode()).hexdigest() + "@beijing-xianxing"

weekday_cn = "一二三四五六日"

# ===== 主流程 =====
today = dt.date.today()
end = today + dt.timedelta(days=DAYS_AHEAD)
years_needed = {today.year, end.year}

holidays, ADJUSTED, WEEKENDS, HOLIDAY_IDX = fetch_cn_calendar(years_needed)
FEST = build_festival_layer(today, end)

def is_workday(d: dt.date):
    if d in ADJUSTED: return True
    if d in holidays: return False
    return d.weekday() < 5

def holiday_info(d: dt.date):
    return (True, *HOLIDAY_IDX[d]) if d in HOLIDAY_IDX else (False, None, None, None)

lines = [
    "BEGIN:VCALENDAR",
    "PRODID:-//beijing-xianxing//CN//",
    "VERSION:2.0",
    f"X-WR-CALNAME:{CAL_NAME}",
    f"X-WR-CALDESC:{CAL_DESC}",
    f"X-WR-TIMEZONE:{TZID}",
]

for i in range((end - today).days + 1):
    d = today + dt.timedelta(days=i)
    wcn = weekday_cn[d.weekday()]
    is_h, hname, hidx, htot = holiday_info(d)

    # 放假优先：当天是法定放假则不展示节日/节气
    fest_names = ""
    if not is_h and d in FEST:
        fest_names = " / ".join(sorted(FEST[d]))

    if is_workday(d):
        ro = rotation_for(d)
        key = d.weekday()
        if d in ADJUSTED and key > 4:
            key = 4  # 调休周末按周五映射兜底

        if ro and key in ro.get("map", {}):
            a, b = ro["map"][key]
            summary = f"北京尾号限行｜周{wcn} {a}/{b}"
            line1 = f"北京尾号限行：今日限行：{a}&{b}"
            line2 = f"节日/节气：{'无' if is_h else (fest_names if fest_names else '无')}"
            line3 = f"假期：{(hname + f' {htot}天假 {hidx}/{htot}') if is_h else '无'}"
            desc = (
                line1 + "\n" + line2 + "\n" + line3 +
                "\n\n执行规则：工作日 7:00–20:00；范围：五环内（不含）；字母按0。\n"
                "来源：北京交管（轮换）；Timor节假日API；lunar-python（二十四节气）；自定义节日库。"
            )
        else:
            summary = f"北京尾号限行｜周{wcn}（轮换未匹配）"
            line1 = "北京尾号限行：今日限行：未知（轮换未匹配）"
            line2 = f"节日/节气：{'无' if is_h else (fest_names if fest_names else '无')}"
            line3 = f"假期：{(hname + f' {htot}天假 {hidx}/{htot}') if is_h else '无'}"
            desc = (
                line1 + "\n" + line2 + "\n" + line3 +
                "\n\n提示：未匹配到当期轮换区间，请更新 rotations.yml。"
            )

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_for(d,'work')}",
            f"DTSTAMP:{dtstamp()}",
            f"DTSTART;TZID={TZID}:{fmt_dt(d,7,0)}",
            f"DTEND;TZID={TZID}:{fmt_dt(d,20,0)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "LOCATION:北京（五环内，不含）",
            "URL:https://jtgl.beijing.gov.cn/",
            "END:VEVENT"
        ]
    else:
        # 非工作日：周末或法定放假
        reason = f"{hname} {htot}天假 {hidx}/{htot}" if is_h else "周末"
        summary = f"不限行｜{reason}"
        line1 = f"北京尾号限行：不限行（{reason}）"
        line2 = "节日/节气：" + ("无" if is_h else (fest_names if fest_names else "无"))
        line3 = "假期：" + (f"{hname} {htot}天假 {hidx}/{htot}" if is_h else "无")
        desc = (
            line1 + "\n" + line2 + "\n" + line3 +
            "\n\n说明：非工作日不执行尾号限行；工作日 7:00–20:00 于五环内（不含）执行，字母按0。"
        )

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_for(d,'free')}",
            f"DTSTAMP:{dtstamp()}",
            f"DTSTART;TZID={TZID}:{fmt_dt(d,0,0)}",
            f"DTEND;TZID={TZID}:{fmt_dt(d,23,59)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "LOCATION:北京（全市）",
            "URL:https://jtgl.beijing.gov.cn/",
            "END:VEVENT"
        ]

lines.append("END:VCALENDAR")

with open("beijing-xianxing.ics", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("ICS 已生成：限行 + 放假（优先） + 节日/二十四节气（三行展示）")
