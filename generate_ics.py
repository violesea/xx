#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime as dt
import hashlib, json, os, re, sys, urllib.request, urllib.error, yaml

# ===== 基本参数 =====
DAYS_AHEAD = 270
TZID = "Asia/Shanghai"
CAL_NAME = "北京尾号限行·节假日自动更新（轮换表自动尝试+兜底提醒）"
CAL_DESC = "节假日与调休自动同步；轮换表先抓取、失败用rotations.yml兜底；到期自动告警。五环内（不含）工作日7:00–20:00；字母按0管理。"

# ===== Timor 节假日API（自动）=====
TIMOR_ENDPOINT = "https://timor.tech/api/holiday/year/{}?type=Y&week=Y"
UA = "beijing-xianxing-ics/1.1 (+github actions)"

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
            t = (val.get("type") or {}).get("type") # 0工作日/1周末/2节假日
            if bool(val.get("holiday")) or t == 2:
                holidays[d] = val.get("name") or "节假日"
            else:
                if d.weekday() >= 5:
                    # 周末但标为工作日 => 调休上班
                    if t == 0:
                        adjusted.add(d)
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

# ===== 轮换表获取：在线尝试 + 本地rotations.yml兜底 =====
# 思路：
# 1) 预留抓取入口：给出若干官方页面URL（可随时间替换），尝试解析“起止日期 + 周一..周五两尾号”
# 2) 抓取失败或校验不过，则读取仓库里的 rotations.yml
# 3) 如果未来窗口超出最后一个区间，则写出标志文件，供工作流开 Issue 告警

ROTATION_SOURCES = [
    # 这些URL只是“占位入口”，你后续可以在GitHub上改成当期公告页
    # 例如 jtgl.beijing.gov.cn 或 “首都之窗”相关栏目页的最新一条通告
    # "https://jtgl.beijing.gov.cn/xxx/xxx.html",
    # "https://www.beijing.gov.cn/xxx/xxx.html",
]

WEEKMAP = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri"}

def parse_rotation_from_html(html):
    """
    极简解析：从纯文本中搜“2025年X月X日至2025年X月X日”“周一/星期一 1和6/1、6”之类模式。
    不保证每次成功，但足以覆盖大部分文本版公告。
    """
    text = re.sub(r'\s+', ' ', html)
    # 日期段
    date_spans = []
    for m in re.finditer(r'(\d{4})[年\-/.](\d{1,2})[月\-/.](\d{1,2})日?\D{0,8}?至\D{0,8}?(\d{4})[年\-/.](\d{1,2})[月\-/.](\d{1,2})日?', text):
        y1,m1,d1,y2,m2,d2 = map(int, m.groups())
        s,e = dt.date(y1,m1,d1), dt.date(y2,m2,d2)
        if s<=e: date_spans.append((s,e))
    # 映射：周一..周五 两个尾号
    # 匹配“周一 1和6 / 1、6 / 1和6（含0视为0）”
    pair = lambda s: tuple(map(int, re.findall(r'\d', s)[:2])) if re.findall(r'\d', s) else None
    maps = {}
    for monday_label in ["周一","星期一"]:
        m = re.search(monday_label + r'.{0,6}?([0-9和、,，/ ]{3,})', text)
        if m: 
            p = pair(m.group(1))
            if p: maps["Mon"] = p
    for tues_label, wkey in [("周二","Tue"),("星期二","Tue"),("周三","Wed"),("星期三","Wed"),
                             ("周四","Thu"),("星期四","Thu"),("周五","Fri"),("星期五","Fri")]:
        m = re.search(tues_label + r'.{0,6}?([0-9和、,，/ ]{3,})', text)
        if m:
            p = pair(m.group(1))
            if p: maps[wkey] = p
    if date_spans and len(maps)==5:
        return [{"start": s, "end": e, "map": maps} for s,e in date_spans]
    return []

def try_fetch_rotations_online():
    rotations = []
    for url in ROTATION_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode("utf-8","ignore")
            got = parse_rotation_from_html(html)
            if got:
                rotations.extend(got)
        except Exception:
            continue
    # 去重/合并相邻同映射区间
    out = []
    for it in sorted(rotations, key=lambda x: x["start"]):
        if out and it["start"] <= out[-1]["end"] and it["map"]==out[-1]["map"]:
            out[-1]["end"] = max(out[-1]["end"], it["end"])
        else:
            out.append(it)
    return out

def read_rotations_yaml(path="rotations.yml"):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    out = []
    for item in raw:
        s = dt.date.fromisoformat(str(item["start"]))
        e = dt.date.fromisoformat(str(item["end"]))
        m = item["map"]
        out.append({
            "start": s, "end": e,
            "map": {0: tuple(m["Mon"]),1:tuple(m["Tue"]),2:tuple(m["Wed"]),3:tuple(m["Thu"]),4:tuple(m["Fri"])}
        })
    return out

def load_rotations():
    # 1) 在线尝试
    online = try_fetch_rotations_online()
    if online:
        # 标准化键
        norm = []
        for it in online:
            amap = it["map"]
            if isinstance(list(amap.keys())[0], str):
                amap = {i: tuple(amap[k]) for i,k in enumerate(["Mon","Tue","Wed","Thu","Fri"])}
            norm.append({"start": it["start"], "end": it["end"], "map": amap})
        return norm, "online"
    # 2) 本地兜底
    yaml_rot = read_rotations_yaml()
    if yaml_rot:
        return yaml_rot, "yaml"
    return [], "none"

# ===== ICS 生成辅助 =====
def dtstamp(): return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
def fmt_dt(d,h,m): return f"{d.strftime('%Y%m%d')}T{h:02d}{m:02d}00"
def uid_for(d,s=""): 
    return hashlib.md5(f"bjxx-{d.isoformat()}-{s}".encode()).hexdigest()+"@beijing-xianxing"

def rotation_for(d, rotations):
    for ro in rotations:
        if ro["start"] <= d <= ro["end"]:
            return ro
    return None

# ===== 主流程 =====
today = dt.date.today()
end = today + dt.timedelta(days=DAYS_AHEAD)
years = {today.year, end.year}

holidays, ADJ, WEEKENDS, HOLIDAY_IDX = fetch_cn_calendar(years)
def is_workday(d: dt.date):
    if d in ADJ: return True
    if d in holidays: return False
    return d.weekday() < 5
def holiday_info(d: dt.date):
    return (True, *HOLIDAY_IDX[d]) if d in HOLIDAY_IDX else (False,None,None,None)

ROTATIONS, ROT_SRC = load_rotations()

# 若未来窗口后半段超出已知轮换区间，创建一个标志文件，供工作流开Issue提示
last_end = max([r["end"] for r in ROTATIONS], default=today - dt.timedelta(days=1))
need_warn = end > last_end
if need_warn:
    with open("NEED_ROTATION_UPDATE.txt","w",encoding="utf-8") as f:
        f.write(f"轮换表覆盖至 {last_end.isoformat()}，已不足以覆盖未来 {DAYS_AHEAD} 天。\n来源={ROT_SRC}\n请更新 rotations.yml 或补充 ROTATION_SOURCES。")

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
    work = is_workday(d)
    is_h, hname, hidx, htot = holiday_info(d)
    wcn = weekday_cn[d.weekday()]
    if work:
        ro = rotation_for(d, ROTATIONS)
        key = d.weekday()
        if d in ADJ and key>4: key=4
        if ro and key in ro["map"]:
            a,b = ro["map"][key]
            tag = "调休上班" if d in ADJ else ("无" if not is_h else hname)
            summary = f"北京尾号限行：周{wcn} {a}/{b}｜节日：{tag}"
            desc = ("执行口径：工作日 7:00–20:00；范围：五环内（不含五环）；字母按0管理。\n"
                    f"节日提示：{'调休上班（执行限行）' if d in ADJ else ('无' if not is_h else hname)}\n"
                    f"轮换来源：{ROT_SRC}；节假日来源：Timor API。")
        else:
            summary = f"北京尾号限行：周{wcn}（轮换未匹配）"
            desc = "未匹配轮换区间，请更新 rotations.yml 或补充 ROTATION_SOURCES。"
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
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_for(d,'free')}", f"DTSTAMP:{dtstamp()}",
            f"DTSTART;TZID={TZID}:{fmt_dt(d,0,0)}", f"DTEND;TZID={TZID}:{fmt_dt(d,23,59)}",
            f"SUMMARY:不限行：{reason}",
            f"DESCRIPTION:{reason}为非工作日，不执行尾号限行；工作日7:00–20:00五环内（不含）执行。",
            "LOCATION:北京（全市）",
            "URL:https://jtgl.beijing.gov.cn/",
            "END:VEVENT"
        ]

lines.append("END:VCALENDAR")

with open("beijing-xianxing.ics","w",encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"ICS生成完成；轮换来源={ROT_SRC}；到期告警={'YES' if need_warn else 'NO'}")
