#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime as dt
import hashlib

# ---------- 参数 ----------
DAYS_AHEAD = 270            # 生成未来 N 天
TZID = "Asia/Shanghai"
CAL_NAME = "北京尾号限行（工作日 7:00–20:00，五环内）"
CAL_DESC = "数据源：北京交管局/首都之窗；节假日按国务院通知；字母按 0 管理。"

# ---------- 2025~2026 尾号轮换（来自北京交管局通告） ----------
# 规则：每个区间内，周一~周五对应的限行尾号（两组）；字母按0管理
# 参考：2025-03-31→2025-06-29、06-30→09-28、09-29→12-28、2025-12-29→2026-03-29
ROTATIONS = [
    {
        "start": dt.date(2025, 3, 31), "end": dt.date(2025, 6, 29),
        "map": {0: (1,6), 1: (2,7), 2: (3,8), 3: (4,9), 4: (5,0)}  # Mon..Fri
    },
    {
        "start": dt.date(2025, 6, 30), "end": dt.date(2025, 9, 28),
        "map": {0: (5,0), 1: (1,6), 2: (2,7), 3: (3,8), 4: (4,9)}
    },
    {
        "start": dt.date(2025, 9, 29), "end": dt.date(2025, 12, 28),
        "map": {0: (4,9), 1: (5,0), 2: (1,6), 3: (2,7), 4: (3,8)}
    },
    {
        "start": dt.date(2025, 12, 29), "end": dt.date(2026, 3, 29),
        "map": {0: (3,8), 1: (4,9), 2: (5,0), 3: (1,6), 4: (2,7)}
    },
]

# ---------- 2025 法定节假日与调休（来自国务院通知） ----------
# 休：不执行尾号；调休上班：执行尾号
HOLIDAYS_2025 = set()
for d in range(1, 2):  # 元旦 1/1
    HOLIDAYS_2025.add(dt.date(2025, 1, 1))
for d in range(28, 32): HOLIDAYS_2025.add(dt.date(2025, 1, d))     # 春节 1/28-2/4
for d in range(1, 5):   HOLIDAYS_2025.add(dt.date(2025, 2, d))
HOLIDAYS_2025 |= {dt.date(2025,4,4), dt.date(2025,4,5), dt.date(2025,4,6)}  # 清明
HOLIDAYS_2025 |= {dt.date(2025,5,1), dt.date(2025,5,2), dt.date(2025,5,3), dt.date(2025,5,4), dt.date(2025,5,5)}  # 劳动节
HOLIDAYS_2025 |= {dt.date(2025,5,31), dt.date(2025,6,1), dt.date(2025,6,2)}  # 端午
HOLIDAYS_2025 |= {dt.date(2025,10,1), dt.date(2025,10,2), dt.date(2025,10,3), dt.date(2025,10,4),
                  dt.date(2025,10,5), dt.date(2025,10,6), dt.date(2025,10,7), dt.date(2025,10,8)}  # 国庆+中秋

# 调休上班（属于“工作日”，应执行尾号）
ADJUSTED_WORKDAYS_2025 = {
    dt.date(2025,1,26), dt.date(2025,2,8), dt.date(2025,4,27),
    dt.date(2025,9,28), dt.date(2025,10,11)
}

# ---------- 工具 ----------
def find_rotation(d: dt.date):
    for ro in ROTATIONS:
        if ro["start"] <= d <= ro["end"]:
            return ro
    return None

def is_china_workday(d: dt.date):
    # 周一~周五且不在节假日；或为调休上班的周六/周日
    if d in ADJUSTED_WORKDAYS_2025:
        return True
    if d.weekday() < 5 and d not in HOLIDAYS_2025:
        return True
    return False

def dtstamp():
    return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def fmt_dt(date_obj, hour, minute):
    return f"{date_obj.strftime('%Y%m%d')}T{hour:02d}{minute:02d}00"

def uid_for(d: dt.date):
    raw = f"bjxx-{d.isoformat()}@beijing-xianxing"
    return hashlib.md5(raw.encode()).hexdigest() + "@beijing-xianxing"

# ---------- 生成 ICS ----------
today = dt.date.today()
end = today + dt.timedelta(days=DAYS_AHEAD)

lines = []
lines += [
"BEGIN:VCALENDAR",
"PRODID:-//beijing-xianxing//CN//",
"VERSION:2.0",
f"X-WR-CALNAME:{CAL_NAME}",
f"X-WR-CALDESC:{CAL_DESC}",
f"X-WR-TIMEZONE:{TZID}",
]

# 简化处理：直接用 TZID，iOS 能识别 Asia/Shanghai
for i in range((end - today).days + 1):
    d = today + dt.timedelta(days=i)
    if not is_china_workday(d):
        continue
    ro = find_rotation(d)
    if not ro:
        continue
    if d.weekday() > 4:  # 仅周一~周五（调休周末已在 is_china_workday 放行）
        continue
    digits = ro["map"][d.weekday()]
    # 事件
    start_local = fmt_dt(d, 7, 0)
    end_local   = fmt_dt(d, 20, 0)
    weekday_cn = "一二三四五六日"[d.weekday()]
    summary = f"北京尾号限行：周{weekday_cn} {digits[0]}/{digits[1]}"
    desc = ("工作日 7:00–20:00，范围：五环路以内（不含五环）。"
            "字母按 0 管理；临时号牌同。来源：北京交管局通告。")
    event = [
        "BEGIN:VEVENT",
        f"UID:{uid_for(d)}",
        f"DTSTAMP:{dtstamp()}",
        f"DTSTART;TZID={TZID}:{start_local}",
        f"DTEND;TZID={TZID}:{end_local}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
        "LOCATION:北京（五环内，不含五环）",
        "URL:https://jtgl.beijing.gov.cn/",
        "END:VEVENT"
    ]
    lines += event

lines.append("END:VCALENDAR")

with open("beijing-xianxing.ics", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("Generated beijing-xianxing.ics")
