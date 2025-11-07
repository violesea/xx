#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime as dt
import hashlib, json, os, re, urllib.request
import yaml  # 兜底轮换用
from lunardate import LunarDate  # 农历节日用

# ===== 基本参数 =====
DAYS_AHEAD = 270
TZID = "Asia/Shanghai"
CAL_NAME = "北京尾号限行 + 节假日与节日提醒（节假日自动）"
CAL_DESC = "法定节假日/调休自动同步；工作日限行（含调休上班），周末/法定休不限行；叠加提醒：情人节/母亲节/七夕/万圣节/圣诞等。五环内（不含），7:00–20:00，字母按0。"

# ===== 尾号轮换（先用 rotations.yml 兜底；如你未启用抓取，保留这份即可）=====
def read_rotations_yaml(path="rotations.yml"):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    out = []
    for item in raw:
        s = dt.date.fromisoformat(str(item["start"]))
        e = dt.date.fromisoformat(str(item["end"]))
        m = item["map"]  # {Mon: [1,6], ...}
        out.append({
            "start": s, "end": e,
            "map": {0: tuple(m["Mon"]),1:tuple(m["Tue"]),2:tuple(m["Wed"]),
                    3:tuple(m["Thu"]),4:tuple(m["Fri"])}
        })
    return out

ROTATIONS = read_rotations_yaml()  # 没有 rotations.yml 也能继续生成“不限行/轮换未匹配”事件

def rotation_for(d: dt.date):
    for ro in ROTATIONS:
        if ro["start"] <= d <= ro["end"]:
            return ro
    return None

# ===== 法定节假日/调休（Timor API，自动更新）=====
TIMOR_ENDPOINT = "https://timor.tech/api/holiday/year/{}?type=Y&week=Y"
UA = "beijing-xianxing-ics/1.2 (+github actions)"

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
    # 连续节日分组 -> 第i/共N天
    day_idx = {}
    for name in set(holidays.values()):
        dates = sorted([d for d, n in holidays.items() if n == name])
        i = 0
        while i < len(dates):
            j = i
            while j + 1 < len(dates) and dates[j+1] == dates[j] + dt.timedelta(days=1):
                j += 1
            total = (dates[j] - dates[i]).days + 1
            for k in range(total):
                day_idx[dates[i] + dt.timedelta(days=k)] = (name, k+1, total)
            i = j + 1
    return holidays, adjusted, weekends, day_idx

# ===== “非放假节日”库：固定阳历 + “第n个星期x” + 农历 =====
# 不改变“是否限行”的判断，只做提醒
SOLAR_FIXED = {  # (月,日): 名称
    (1,1): "元旦", (2,14): "情人节", (3,8): "妇女节", (4,1): "愚人节",
    (5,20): "520", (6,1): "儿童节", (9,10): "教师节", (10,31): "万圣节",
    (11,11): "光棍节", (12,24): "平安夜", (12,25): "圣诞节",
}

# 母亲节：5月第2个星期日；父亲节：6月第3个星期日；感恩节：11月第4个星期四
WEEKDAY_RULES = [  # (月, 第n个, 周几0-6, 名称)
    (5, 2, 6, "母亲节"), (6, 3, 6, "父亲节"), (11, 4, 3, "感恩节"),
]

# 农历：元宵(正月十五)、七夕(七月初七)、重阳(九月初九) —— 春节/端午/中秋已由法定节假日覆盖
LUNAR_FIXED = [  # (月, 日, 名称)
    (1, 15, "元宵节"), (7, 7, "七夕"), (9, 9, "重阳节"),
]

def nth_weekday_of_month(year, month, n, weekday):
    # 返回“year年month月 第n个weekday”的日期
    d = dt.date(year, month, 1)
    add = (weekday - d.weekday() + 7) % 7
    d = d + dt.timedelta(days=add) + dt.timedelta(weeks=n-1)
    return d

def gregorian_from_lunar_for_year(g_year, l_month, l_day, name):
    # 在“农历年 g_year 与 g_year-1”中各尝试一次，找出落在公历 g_year 的对应日期
    out = []
    for ly in (g_year-1, g_year, g_year+1):
        try:
            d = LunarDate(ly, l_month, l_day).toSolarDate()
            if d.year == g_year:
                out.append((d, name))
        except Exception:
            continue
    # 去重
    seen, result = set(), []
    for d, nm in out:
        if d not in seen:
            seen.add(d); result.append((d, nm))
    return result

def build_festival_layer(years):
    fest = {}  # dict[date] -> [names]
    for y in years:
        # 固定阳历
        for (m, d), nm in SOLAR_FIXED.items():
            try:
                dd = dt.date(y, m, d)
                fest.setdefault(dd, []).append(nm)
            except Exception:
                pass
        # 第n个星期x
        for (m, n, wd, nm) in WEEKDAY_RULES:
            try:
                dd = nth_weekday_of_month(y, m, n, wd)
                fest.setdefault(dd, []).append(nm)
            except Exception:
                pass
        # 农历
        for (lm, ld, nm) in LUNAR_FIXED:
            for (dd, nm2) in gregorian_from_lunar_for_year(y, lm, ld, nm):
                fest.setdefault(dd, []).append(nm2)
    return fest  # date -> [节日名...]

# ===== 生成 ICS =====
today = dt.date.today()
end = today + dt.timedelta(days=DAYS_AHEAD)
years_needed = {today.year, end.year}

holidays, ADJUSTED, WEEKENDS, HOLIDAY_IDX = fetch_cn_calendar(years_needed)
FEST = build_festival_layer(years_needed)

def is_workday(d: dt.date):
    if d in ADJUSTED: return True
    if d in holidays: return False
    return d.weekday() < 5

def holiday_info(d: dt.date):
    return (True, *HOLIDAY_IDX[d]) if d in HOLIDAY_IDX else (False,None,None,None)

def dtstamp(): return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
def fmt_dt(d,h,m): return f"{d.strftime('%Y%m%d')}T{h:02d}{m:02d}00"
def uid_for(d,s=""): 
    return hashlib.md5(f"bjxx-{d.isoformat()}-{s}".encode()).hexdigest()+"@beijing-xianxing"

weekday_cn = "一二三四五六日"

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
    extra_fests = "，".join(sorted(set(FEST.get(d, [])))) if d in FEST else ""

    if is_workday(d):
        ro = rotation_for(d)
        key = d.weekday()
        if d in ADJUSTED and key > 4: key = 4  # 调休周末按周五映射兜底
        if ro and key in ro.get("map", {}):
            a,b = ro["map"][key]
            holitag = "调休上班" if d in ADJUSTED else ("无" if not is_h else hname)
            fest_tag = f"；节日：{extra_fests}" if extra_fests else ""
            summary = f"北京尾号限行：周{wcn} {a}/{b}｜节日：{holitag}{fest_tag}"
            desc = (
                "执行：工作日 7:00–20:00；范围：五环内（不含）；字母按0。\n"
                f"节日提示：{'调休上班（执行限行）' if d in ADJUSTED else ('无' if not is_h else hname)}"
                + (f"；其他：{extra_fests}" if extra_fests else "") +
                "\n来源：北京交管（轮换）；Timor节假日API；自定义节日库。"
            )
        else:
            fest_tag = f"｜节日：{('无' if not is_h else hname)}" + (f"；{extra_fests}" if extra_fests else "")
            summary = f"北京尾号限行：周{wcn}（轮换未匹配）{fest_tag}"
            desc = "未匹配轮换区间，请更新 rotations.yml。"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_for(d,'work')}", f"DTSTAMP:{dtstamp()}",
            f"DTSTART;TZID={TZID}:{fmt_dt(d,7,0)}", f"DTEND;TZID={TZID}:{fmt_dt(d,20,0)}",
            f"SUMMARY:{summary}", f"DESCRIPTION:{desc}",
            "LOCATION:北京（五环内，不含）",
            "URL:https://jtgl.beijing.gov.cn/",
            "END:VEVENT"
        ]
    else:
        reason = f"{hname} 第{hidx}/{htot}天" if is_h else "周末"
        fest_tail = f"；节日：{extra_fests}" if extra_fests else ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_for(d,'free')}", f"DTSTAMP:{dtstamp()}",
            f"DTSTART;TZID={TZID}:{fmt_dt(d,0,0)}", f"DTEND;TZID={TZID}:{fmt_dt(d,23,59)}",
            f"SUMMARY:不限行：{reason}{fest_tail}",
            f"DESCRIPTION:{reason}为非工作日，不执行尾号限行；工作日7:00–20:00五环内（不含）执行。"
            + (f" 其他节日：{extra_fests}" if extra_fests else ""),
            "LOCATION:北京（全市）",
            "URL:https://jtgl.beijing.gov.cn/",
            "END:VEVENT"
        ]

lines.append("END:VCALENDAR")
with open("beijing-xianxing.ics","w",encoding="utf-8") as f:
    f.write("\n".join(lines))
print("ICS 已生成：限行 + 法定节假日 + 非放假节日提醒")
