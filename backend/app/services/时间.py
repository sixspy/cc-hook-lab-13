from __future__ import annotations

import time as _time
from datetime import datetime, timezone

格式 = "%Y-%m-%dT%H:%M:%SZ"


def 此刻() -> str:
    """秒级 UTC 中文账册时刻（沿用基线格式）。"""
    return datetime.now(timezone.utc).strftime(格式)


def 纪元秒() -> float:
    """浮点纪元秒：退避窗/尝试间隔用它精确判定，测试可打桩快进。"""
    return _time.time()


def 解析纪元(s: str | None) -> float:
    """把基线秒级时刻反解为纪元秒；解析失败返回 0.0。"""
    if not s:
        return 0.0
    try:
        return datetime.strptime(str(s), 格式).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0
