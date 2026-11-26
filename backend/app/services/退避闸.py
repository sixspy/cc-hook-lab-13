"""hook-13-重试退避闸 · 路径甲（全仓唯一兑现的工程形态）。

工程抉择（整仓只兑现这一边）：
    **审计账册钉退避窗指纹，并拒窗内回刷。**
    - 每次放行落账的尝试都在同事务写一行「退避账册」，钉住该退避窗指纹；
    - 窗内再次请求不再写正式「投递尝试」，只在审计账册记一行「窗内拦截」；
    - 读退避窗与写尝试在同一把事件锁 + 同一个 ``BEGIN IMMEDIATE`` 事务里同成败。

明确不做路径乙（双轨分流 / 只读归档 / 两套读引用）：``modules/双写归档轨``
保持空桩，仓内不存在第二套可投集读引用。这里的账册是审计权威，不是投递归档轨。
"""
from __future__ import annotations

import hashlib
import math
import threading

from app.db import 连接
from app.services import 时间

骨签 = "hook-13-重试退避闸"
工程形态 = "路径甲·审计账册钉退避窗指纹·拒窗内回刷"

# 中文闸口命运（页面/接口/对账共用同一套文案）
闸_首投 = "尚无失败·可首投"
闸_可投 = "窗已开·可重试"
闸_拦截 = "退避窗内·禁止连打"
闸_新败 = "失败已记账·进入退避窗"
闸_成功 = "投递成功·结案"

默认规 = {
    "最大重试": "3",
    "退避秒": "30",
    "退避倍数": "1",
    "退避上限秒": "300",
}

# 同事件号 -> 同一把进程内互斥锁（“并发双请求打同锁”的那把锁）
_锁表: dict[str, threading.Lock] = {}
_锁表守卫 = threading.Lock()


def 重置锁() -> None:
    """测试/换库时清空锁表。"""
    with _锁表守卫:
        _锁表.clear()


def 取事件锁(事件号: str) -> threading.Lock:
    """同一事件号恒取同一把锁；不同事件不互斥。"""
    with _锁表守卫:
        lk = _锁表.get(事件号)
        if lk is None:
            lk = threading.Lock()
            _锁表[事件号] = lk
        return lk


def 指纹(事件号: str, 锚尝试号: str) -> str:
    """退避窗指纹：钉住『事件 + 开窗的那笔失败尝试』。"""
    raw = f"hook-13|退避窗|{事件号}|{锚尝试号}".encode("utf-8")
    return "WB-" + hashlib.sha256(raw).hexdigest()[:18]


def 读规(conn=None) -> dict:
    """读签名规里的退避策略（流水与签名规同票）。"""
    own = conn is None
    if own:
        conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        db = {r["键"]: r["值"] for r in rows}
    finally:
        if own:
            conn.close()
    out = dict(默认规)
    for k, v in db.items():
        if v is not None:
            out[k] = v
    return out


def 有效间隔秒(失败次数: int, 规: dict | None = None) -> float:
    """第 N 笔失败后，下一笔重试至少要间隔的秒数。

    间隔 = min(退避秒 * 退避倍数 ** (N-1), 退避上限秒)；倍数默认 1 即固定窗。
    退避秒配成 0 可显式关闸（仅供批量灌测/特殊策略）。
    """
    规 = 规 or 读规()
    try:
        base = float(规.get("退避秒", 30) or 0)
        mult = float(规.get("退避倍数", 1) or 1)
        cap = float(规.get("退避上限秒", 300) or 0)
    except (TypeError, ValueError):
        base, mult, cap = 30.0, 1.0, 300.0
    n = max(1, int(失败次数 or 1))
    if base <= 0:
        return 0.0
    v = base * (mult ** (n - 1))
    if cap and cap > 0:
        v = min(v, cap)
    return max(0.0, v)


def 末笔失败(conn, 事件号: str):
    """取最近一笔正式失败尝试（按物理插入顺序 rowid 最稳）。"""
    return conn.execute(
        "SELECT * FROM 投递尝试 WHERE 事件号=? AND 状态='失败' ORDER BY rowid DESC LIMIT 1",
        (事件号,),
    ).fetchone()


def _尝试纪元(row) -> float:
    ep = row["投递纪元"] if "投递纪元" in row.keys() else None
    if ep:
        return float(ep)
    return 时间.解析纪元(row["投递时"])


def 人工开窗(conn, 事件号: str, 详情: str = "人工重投，旧退避窗作废") -> dict:
    """在同一事务里钉一枚『人工开窗』标记：晚于它的失败锚才生效。

    死信匣人工重投走这里，避免操作员刚处置完就被自动退避窗挡住。
    """
    now = 时间.纪元秒()
    t = 时间.此刻()
    fp = 指纹(事件号, f"RESET-{now:.6f}")
    conn.execute(
        "INSERT INTO 退避账册"
        "(时刻,事件号,窗指纹,尝试号,闸口,结果,窗口起纪元,窗口止纪元,尝试间隔秒,退避剩余秒,详情)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (t, 事件号, fp, "", "人工开窗", "重置", now, now, 0.0, 0.0, 详情),
    )
    return {"时刻": t, "窗指纹": fp, "窗口起纪元": now}


def 窗状态(事件号: str, now: float | None = None, conn=None) -> dict | None:
    """裁决某事件当前的退避窗状态。读操作；可复用调用方事务连接。"""
    own = conn is None
    if own:
        conn = 连接()
    try:
        ev = conn.execute(
            "SELECT 事件号,状态 FROM 事件 WHERE 事件号=?", (事件号,)
        ).fetchone()
        if not ev:
            return None
        规 = 读规(conn)
        失败次数 = conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'",
            (事件号,),
        ).fetchone()["c"]

        if now is None:
            now = 时间.纪元秒()

        reset_row = conn.execute(
            "SELECT MAX(窗口起纪元) AS m FROM 退避账册 "
            "WHERE 事件号=? AND 闸口='人工开窗'",
            (事件号,),
        ).fetchone()
        reset_at = float(reset_row["m"] or 0.0)

        last = 末笔失败(conn, 事件号)
        anchor = 0.0
        锚尝试号 = ""
        if last is not None:
            anchor = _尝试纪元(last)
            锚尝试号 = last["尝试号"]
            if reset_at and reset_at >= anchor:
                # 已有更新的人工开窗标记，旧失败窗作废
                last = None
                anchor = 0.0
                锚尝试号 = ""

        if last is None:
            # 无生效中的失败锚：闸开。展示“下一笔失败后”将采用的间隔。
            间隔 = 有效间隔秒(int(失败次数 or 0) + 1, 规)
            return {
                "事件号": 事件号,
                "在窗内": False,
                "允许": True,
                "闸口": 闸_首投 if not 失败次数 else 闸_可投,
                "窗指纹": "",
                "锚尝试号": "",
                "失败次数": int(失败次数),
                "尝试间隔秒": 间隔,
                "尝试间隔": f"{_格式秒(间隔)} 秒",
                "退避剩余秒": 0,
                "退避剩余": "0 秒（可投）",
                "窗口起纪元": 0.0,
                "窗口止纪元": 0.0,
                "当前纪元": now,
                "骨签": 骨签,
                "工程形态": 工程形态,
            }

        间隔 = 有效间隔秒(int(失败次数), 规)
        窗止 = anchor + 间隔
        剩余 = max(0.0, 窗止 - now)
        在窗内 = 间隔 > 0 and 剩余 > 0
        剩余整 = int(math.ceil(剩余)) if 在窗内 else 0
        fp = 指纹(事件号, 锚尝试号)
        return {
            "事件号": 事件号,
            "在窗内": bool(在窗内),
            "允许": not 在窗内,
            "闸口": 闸_拦截 if 在窗内 else 闸_可投,
            "窗指纹": fp,
            "锚尝试号": 锚尝试号,
            "失败次数": int(失败次数),
            "尝试间隔秒": 间隔,
            "尝试间隔": f"{_格式秒(间隔)} 秒",
            "退避剩余秒": 剩余整,
            "退避剩余": f"约 {剩余整} 秒" if 在窗内 else "0 秒（可投）",
            "窗口起纪元": anchor,
            "窗口止纪元": 窗止,
            "当前纪元": now,
            "骨签": 骨签,
            "工程形态": 工程形态,
        }
    finally:
        if own:
            conn.close()


def _格式秒(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def 钉账(conn, **kw) -> None:
    """在调用方事务里写审计账册（与正式尝试同提交/同回滚）。"""
    conn.execute(
        "INSERT INTO 退避账册"
        "(时刻,事件号,窗指纹,尝试号,闸口,结果,窗口起纪元,窗口止纪元,"
        " 尝试间隔秒,退避剩余秒,详情)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            kw.get("时刻") or 时间.此刻(),
            kw["事件号"],
            kw.get("窗指纹", ""),
            kw.get("尝试号", ""),
            kw["闸口"],
            kw.get("结果", ""),
            float(kw.get("窗口起", 0.0) or 0.0),
            float(kw.get("窗口止", 0.0) or 0.0),
            float(kw.get("间隔", 0.0) or 0.0),
            float(kw.get("剩余", 0.0) or 0.0),
            kw.get("详情", ""),
        ),
    )


def 账册列表(事件号: str | None = None, 限制: int = 200) -> list[dict]:
    conn = 连接()
    try:
        if 事件号:
            rows = conn.execute(
                "SELECT * FROM 退避账册 WHERE 事件号=? ORDER BY 编号 DESC LIMIT ?",
                (事件号, 限制),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 退避账册 ORDER BY 编号 DESC LIMIT ?", (限制,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
