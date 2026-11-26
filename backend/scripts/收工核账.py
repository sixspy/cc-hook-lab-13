#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-13 收工核账：按指定顺序浏览五页 + 核账实同源。

顺序（题目规定）：
  1 端点名册列表  /端点名册.html
  2 投递互证详情  /投递互证.html
  3 事件队列卡片  /事件队列.html
  4 死信匣目录    /死信匣.html
  5 投递流水策略  /投递流水.html

每页校验 200 与必备中文线索；再用各页背后的 API 交叉核对计数同源，
并先造一笔“强制失败 + 立即连点”的受控事故，确认页面数据来自同一本账。

用法：python scripts/收工核账.py --base http://127.0.0.1:8500
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar


def _q(url: str) -> str:
    # 真实 HTTP 请求行只允许 ASCII；中文路由/查询值按 UTF-8 百分号编码。
    return urllib.parse.quote(url, safe=":/?=&%+")


def 开瓶():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def 调(op, method, url, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(_q(url), data=data, headers=h, method=method)
    try:
        with op.open(req, timeout=15) as r:
            txt = r.read().decode("utf-8")
            return r.status, (txt if raw else json.loads(txt))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8")
        if raw:
            return e.code, txt
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"错误": txt}


检查 = []


def 核(名: str, ok: bool, 详情: str = ""):
    检查.append((名, bool(ok), 详情))
    print(("  ✅ " if ok else "  ❌ ") + 名 + ((" — " + 详情) if 详情 else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    默认 = os.environ.get("地址") or os.environ.get("BASE") or "http://127.0.0.1:8513"
    ap.add_argument("--base", default=默认)
    base = ap.parse_args().base.rstrip("/")
    op = 开瓶()

    code, me = 调(op, "POST", base + "/api/登录", {"账号": "admin", "密码": "123456"})
    if code != 200:
        print("登录失败", code, me)
        return 2

    print("\n[造受控事故] 入队 → 强制失败一笔 → 立即连点第二下")
    code, ev = 调(op, "POST", base + "/api/事件/入队", {
        "事件类型": "hook13.browse.fail", "端点键": "ep-audit",
        "载荷摘要": "收工核账", "优先级": "普通"})
    eid = ev["事件号"]
    c1, r1 = 调(op, "POST", base + "/api/投递", {"事件号": eid, "强制失败": True})
    c2, r2 = 调(op, "POST", base + "/api/投递", {"事件号": eid, "强制失败": True})
    print(f"  事件 {eid}：首笔 HTTP {c1}/{r1.get('状态')}，连点 HTTP {c2}/{r2.get('状态')}")
    核("受控事故被闸口接住（第二下 429 退避拦截）",
       c1 == 200 and c2 == 429 and r2.get("退避拦截") is True,
       r2.get("闸口", ""))

    页面 = [
        ("1 端点名册列表", "/端点名册.html", ["端点名册", "待处置死信"]),
        ("2 投递互证详情", "/投递互证.html", ["投递互证", "投递流水对账"]),
        ("3 事件队列卡片", "/事件队列.html", ["事件队列", "退避闸"]),
        ("4 死信匣目录", "/死信匣.html", ["死信匣", "人工开窗"]),
        ("5 投递流水策略", "/投递流水.html",
         ["投递流水", "退避剩余", "尝试间隔", "退避窗内", "hook-13-重试退避闸"]),
    ]
    print("\n[依次浏览五页]")
    for 名, path, needles in 页面:
        code, html = 调(op, "GET", base + path, raw=True)
        missing = [s for s in needles if s not in html]
        核(f"{名} 200 且含必备中文线索", code == 200 and not missing,
           "缺 " + ",".join(missing) if missing else f"{len(html)} 字节")

    print("\n[账实同源交叉核对]")
    _, ov = 调(op, "GET", base + "/api/概览")
    _, events = 调(op, "GET", base + "/api/事件")
    _, dead = 调(op, "GET", base + "/api/死信")
    _, deliveries = 调(op, "GET", base + "/api/投递")
    _, eps = 调(op, "GET", base + "/api/端点")
    _, rec = 调(op, "GET", base + "/api/流水对账")
    _, myrec = 调(op, "GET", base + f"/api/流水对账?事件号={eid}")

    核("事件列表数 == 概览事件总数", len(events) == ov["事件总数"],
       f"{len(events)} vs {ov['事件总数']}")
    待匣 = [d for d in dead if d.get("状态") == "待处置"]
    核("死信匣待处置数 == 概览死信匣", len(待匣) == ov["死信匣"],
       f"{len(待匣)} vs {ov['死信匣']}")
    if ov["尝试数"] <= 200:
        核("投递流水行数 == 概览尝试数", len(deliveries) == ov["尝试数"],
           f"{len(deliveries)} vs {ov['尝试数']}")
    else:
        核("投递流水接口按 200 上限截断", len(deliveries) == 200)
    核("端点联查事件数之和 == 事件总数",
       sum(int(e.get("事件数", 0)) for e in eps) == ov["事件总数"])
    核("全局流水对账对齐且影子/正式同票",
       rec.get("对齐") is True and rec.get("影子正式同票") is True,
       rec.get("结论", ""))
    核("受控事件：窗内连打=1、合法重试=0、对齐同票",
       myrec.get("窗内连打数") == 1 and myrec.get("合法重试数") == 0
       and myrec.get("对齐") is True and myrec.get("影子正式同票") is True,
       myrec.get("结论", ""))

    c, rep = 调(op, "POST", base + "/api/互证/跑", {})
    核("跑互证后批次判定对齐（含退避口径）",
       c == 200 and rep.get("是否对齐") is True, rep.get("说明", ""))

    失败 = [n for n, ok, _ in 检查 if not ok]
    print("\n" + ("=" * 60))
    print(f"五页核账：{len(检查) - len(失败)}/{len(检查)} 通过")
    if 失败:
        print("未过项：", "；".join(失败))
        return 1
    print("✅ 五页可浏览、账实同源；可继续跑并发双请求脚本打同锁。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
