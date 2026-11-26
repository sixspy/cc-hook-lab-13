#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-13 收工争用验证：并发双请求打同一把事件锁。

对【正在运行】的服务，登录后新建一个事件，用栅栏让两个线程在同一瞬间
并发 POST /api/投递（都强制失败）。正确结果：
  * 恰好 1 笔 200 放行落账；
  * 另 1 笔 429 退避拦截（带 Retry-After 与中文闸口）；
  * 正式投递尝试表里该事件只有 1 行（不双打）；
  * 对账：窗内连打数=1、非法落账=0、对齐、同票。

用法：
  python scripts/并发双请求.py --base http://127.0.0.1:8500
不传 --base 时读环境变量 地址 / BASE，默认 http://127.0.0.1:8513（compose 口）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar


def _q(url: str) -> str:
    # 真实 HTTP 请求行只允许 ASCII；中文路由按 UTF-8 百分号编码。
    return urllib.parse.quote(url, safe=":/?=&%+")


def 建开瓶器():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def 请求(opener, method, url, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_q(url), data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        return e.code, payload, dict(e.headers or {})


def main() -> int:
    ap = argparse.ArgumentParser()
    默认 = os.environ.get("地址") or os.environ.get("BASE") or "http://127.0.0.1:8513"
    ap.add_argument("--base", default=默认)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    op = 建开瓶器()
    st, body, _ = 请求(op, "POST", base + "/api/登录", {"账号": "admin", "密码": "123456"})
    if st != 200:
        print("登录失败", st, body)
        return 2
    print("已登录", body.get("用户"))

    st, ev, _ = 请求(op, "POST", base + "/api/事件/入队", {
        "事件类型": "hook13.concurrent.fail",
        "端点键": "ep-billing",
        "载荷摘要": "并发同锁验证",
        "优先级": "紧急",
    })
    if st != 200:
        print("入队失败", st, ev)
        return 2
    eid = ev["事件号"]
    print("新建事件", eid, "（两个并发请求将打同一事件号＝同一把退避事件锁）")

    闸 = threading.Barrier(2)
    收获: list[tuple] = []

    def 双发():
        闸.wait()
        code, payload, headers = 请求(op, "POST", base + "/api/投递",
                                      {"事件号": eid, "强制失败": True})
        收获.append((code, payload, headers))

    ts = [threading.Thread(target=双发) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)

    print("\n=== 两个并发 /api/投递 响应 ===")
    for i, (code, p, h) in enumerate(收获, 1):
        print(f"[{i}] HTTP {code} 状态={p.get('状态')} 写出={p.get('写出')} "
              f"退避拦截={p.get('退避拦截')} Retry-After={h.get('Retry-After')}")
        print(f"    闸口：{p.get('闸口')}")
        print(f"    剩余={p.get('退避剩余')} 间隔={p.get('尝试间隔')} 指纹={p.get('窗指纹')}")

    _, detail, _ = 请求(op, "GET", base + f"/api/事件/{eid}")
    正式 = detail.get("尝试", [])
    失败正式 = [a for a in 正式 if a.get("状态") == "失败"]
    _, rec, _ = 请求(op, "GET", base + f"/api/流水对账?事件号={eid}")

    print("\n=== 同锁核对 ===")
    print("正式投递尝试行数 =", len(正式), " 其中失败 =", len(失败正式))
    print("对账 =", {k: rec.get(k) for k in
          ("合法重试数", "窗内连打数", "非法落账数", "对齐", "影子正式同票")})

    codes = sorted(c for c, _, _ in 收获)
    ok = (
        len(收获) == 2
        and codes == [200, 429]
        and len(正式) == 1
        and len(失败正式) == 1
        and rec.get("非法落账数") == 0
        and rec.get("对齐") is True
    )
    print("\n结论：", "✅ 同锁有效，未双打（一放一截，正式流水仅一笔）" if ok
                     else "❌ 仍存在双打/账实不同源")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
