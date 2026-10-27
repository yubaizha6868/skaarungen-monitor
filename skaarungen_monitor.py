#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skaarungen 房型空房监控
=======================
监控 booking.skaarungen.no 上 "Top Row" / "Family Top Row" 在
2027-02-06 ~ 2027-02-08（2 晚）是否有空房放出。

实现说明
--------
该预订页（Next.js + BookVisit）的房态由前端 JS 实时调用
  POST https://restapi.bookvisit.com/accommodation/search-v1
拉取，静态 HTML 不含真实房态，因此本脚本直接调用该接口（网页背后同一份数据），
用返回的 roomsResult[].nrAvailable 判断是否放房。

鉴权
----
首页 HTML 内嵌一个 1 小时有效的 JWT（Bearer），每次运行先抓首页取新 token。

推送
----
结果通过企业微信群机器人 Webhook 推送 Markdown 消息。
- 一旦有空房放出（无房 -> 有房 的上升沿）：立即推送。
- 每个日历日（Asia/Shanghai）第一次运行时：推送一次“监控生效 + 当前是否有房”。
- 无空房且不触发每日首检：不推送。

依赖：仅 Python 标准库。
"""

import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# 配置（优先读同目录 config.json，便于云端/本地共享，且不在脚本里硬编码 webhook）
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "skaarungen_config.json")
STATE_PATH = os.path.join(HERE, "skaarungen_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 默认配置（config.json 不存在时使用）
DEFAULT_CONFIG = {
    "webhook_url": "",                       # 企业微信群机器人 Webhook，必填
    "channel_id": "9f95c31a-85b8-497d-b9d5-7112092bddb6",
    "site_base": "https://booking.skaarungen.no",
    "start_date": "2027-02-06",
    "end_date": "2027-02-08",                # 退房日（2 晚）
    "adults": 4,                             # 对应 URL 里的 roomconfig=a4
    "stay_length": 2,
    "target_rooms": [
        {"id": "9aeea430-f7a0-41fb-8fed-6a2045422423", "name": "Top Row"},
        {"id": "ff182e36-b7d7-460b-9f8c-3710c37de2d5", "name": "Family Top Row"},
    ],
}

# 北京时间偏移（固定 +8，避免依赖 tzdata）
TZ_OFFSET = timezone(timedelta(hours=8))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[WARN] 读取 config 失败，使用默认: {e}", file=sys.stderr)
    # 环境变量优先（GitHub Actions 通过 secrets.WECOM_WEBHOOK_URL 注入）
    env_webhook = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if env_webhook:
        cfg["webhook_url"] = env_webhook
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alerted_available": False, "last_daily_report_date": ""}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 写入 state 失败: {e}", file=sys.stderr)


def beijing_date():
    return datetime.now(TZ_OFFSET).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# 网络
# ----------------------------------------------------------------------------
def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_homepage_token(cfg):
    url = f"{cfg['site_base']}/"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=60, context=_ctx()).read().decode("utf-8", "ignore")
    import re
    m = re.search(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}", html)
    if not m:
        raise RuntimeError("未能从首页提取 JWT token")
    return m.group(0)


def query_availability(cfg, token):
    body = {
        "channelId": cfg["channel_id"],
        "startDate": cfg["start_date"],
        "endDate": cfg["end_date"],
        "roomConfigs": [{"adults": cfg["adults"], "childAges": [], "sharerAge": None}],
        "filter": {"includeRooms": True},
    }
    data = json.dumps(body).encode("utf-8")
    url = "https://restapi.bookvisit.com/accommodation/search-v1"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json-patch+json",
            "Accept": "text/plain",
            "Authorization": "Bearer " + token,
        },
    )
    resp = urllib.request.urlopen(req, timeout=60, context=_ctx())
    return json.loads(resp.read().decode("utf-8", "ignore"))


def parse_availability(cfg, obj):
    """返回 {room_name: nrAvailable}，只含目标房型。"""
    rr = obj.get("roomsResult") or []
    result = {}
    by_id = {r["id"]: r for r in cfg["target_rooms"]}
    for r in rr:
        rid = r.get("roomId")
        if rid in by_id:
            nav = r.get("nrAvailable")
            result[by_id[rid]["name"]] = nav if isinstance(nav, int) else 0
    return result


# ----------------------------------------------------------------------------
# 推送
# ----------------------------------------------------------------------------
def push_wecom(cfg, content):
    webhook = cfg.get("webhook_url", "")
    if not webhook:
        print("[WARN] 未配置 webhook_url，跳过推送。内容：\n" + content, file=sys.stderr)
        return False
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30, context=_ctx())
        txt = resp.read().decode("utf-8", "ignore")
        print(f"[PUSH] webhook 返回: {txt}")
        return True
    except Exception as e:
        print(f"[ERROR] 推送失败: {e}", file=sys.stderr)
        return False


def booking_link(cfg):
    return (f"{cfg['site_base']}/accommodation?channelid={cfg['channel_id']}"
            f"&roomconfig=a{cfg['adults']}&year=2027&month=2&day=6&staylength={cfg['stay_length']}")


def build_alert(cfg, avail):
    lines = [
        "## 🚨 Skaarungen 空房放出！",
        f"> **日期**：{cfg['start_date']} ~ {cfg['end_date']}（{cfg['stay_length']} 晚）",
        "> **房型**：Top Row / Family Top Row",
        ">",
        "**当前可订数量：**",
    ]
    for name, nav in avail.items():
        lines.append(f"- {name}：<font color=\"info\">{nav}</font> 间")
    lines.append(">")
    lines.append(f"[立即预订]({booking_link(cfg)})")
    lines.append("> 监控仍在运行（每 6 小时检查一次）。")
    return "\n".join(lines)


def build_daily(cfg, avail, is_available):
    status = ("<font color=\"info\">**有空房**</font>" if is_available
              else "<font color=\"comment\">暂无可订空房</font>")
    lines = [
        "## ✅ 监控生效 · 每日汇报",
        f"> **监控对象**：Skaarungen — Top Row / Family Top Row",
        f"> **日期**：{cfg['start_date']} ~ {cfg['end_date']}（{cfg['stay_length']} 晚）",
        f"> **当前状态**：{status}",
    ]
    if is_available:
        lines.append(">")
        lines.append("**可订数量：**")
        for name, nav in avail.items():
            lines.append(f"- {name}：<font color=\"info\">{nav}</font> 间")
        lines.append(f"[立即预订]({booking_link(cfg)})")
    else:
        lines.append("> 尚未放房，出现空房将**立即推送**通知。")
    lines.append("> 下次检查：6 小时后。")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    state = load_state()

    try:
        token = fetch_homepage_token(cfg)
        obj = query_availability(cfg, token)
        avail = parse_availability(cfg, obj)
    except Exception as e:
        # 查询失败：不推送、不更新每日首检日期，等下次重试
        print(f"[ERROR] 查询失败，本次跳过：{e}", file=sys.stderr)
        return 1

    is_available = len(avail) > 0 and any(n > 0 for n in avail.values())
    today = beijing_date()

    print(f"[INFO] {today} 检查完成 | 有房={is_available} | 当前可订={avail}")

    # 上升沿：无房 -> 有房，立即推送（仅一次，直到再次变无房才重新触发）
    rising_edge = is_available and not state.get("last_alerted_available", False)
    # 每日首次运行
    daily_due = today != state.get("last_daily_report_date", "")

    pushed = False
    if rising_edge and daily_due:
        # 合并：既是放房又是每日首检，发一条合并消息
        merged = build_daily(cfg, avail, True)
        merged += "\n\n> ⚠️ **本日首次检查即检测到空房放出！**"
        if not dry_run:
            push_wecom(cfg, merged)
        else:
            print("[DRY-RUN] 合并推送:\n" + merged)
        pushed = True
        state["last_alerted_available"] = True
        state["last_daily_report_date"] = today
    elif rising_edge:
        if not dry_run:
            push_wecom(cfg, build_alert(cfg, avail))
        else:
            print("[DRY-RUN] 放房告警:\n" + build_alert(cfg, avail))
        pushed = True
        state["last_alerted_available"] = True
    elif daily_due:
        if not dry_run:
            push_wecom(cfg, build_daily(cfg, avail, is_available))
        else:
            print("[DRY-RUN] 每日汇报:\n" + build_daily(cfg, avail, is_available))
        pushed = True
        state["last_daily_report_date"] = today

    # 变回无房，重置上升沿标志，便于下次放房再告警
    if not is_available:
        state["last_alerted_available"] = False

    save_state(state)
    print(f"[INFO] 完成。pushed={pushed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
