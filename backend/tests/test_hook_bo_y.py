# -*- coding: utf-8 -*-
"""hook-13 失败测乙（服务：投递流水对账 / 互证服务 app/services/互证.py）。

事故：旧「互证」只数事件状态与死信匣，投递流水里同一退避窗内的连打被当成
一笔笔合法重试，对账照样盖“对齐”（假绿）。

正确契约：投递流水对账必须认退避窗与窗指纹——
* 窗内连打只能是“已拦截的回刷”，绝不能计作合法重试；
* 若正式流水里竟存在窗内紧挨的两笔（旧主干/野写入），对账要判非法落账、
  账实撕裂，而不是追认成合法重试；
* 审计账册（影子）与正式投递尝试按尝试号/窗指纹“同票”。

按题目要求：本文件先于主干修改建立，在旧主干上 pytest 必须见红。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_bo_y_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递, 互证  # noqa: E402
import app.db as dbmod  # noqa: E402


@pytest.fixture
def 库(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "数据目录", tmp_path)
    monkeypatch.setattr(dbmod, "库路径", tmp_path / "hook_bo_y.db")
    seed.初始化库()
    try:
        from app.services import 退避闸

        退避闸.重置锁()
    except Exception:
        pass
    yield tmp_path


def _新事件(摘要: str = "bo-y") -> str:
    return 入队.建事件(
        {"事件类型": "invoice.paid", "端点键": "ep-audit", "载荷摘要": 摘要}
    )["事件号"]


def test_乙_投递流水对账不把窗内连打当合法重试(库):
    """【失败测乙·必须先红】对账把窗内连打当成合法重试。"""
    # 旧主干没有这个对账入口 —— 这里先就是一条红
    assert hasattr(互证, "投递流水对账"), "缺『投递流水对账』，窗内连打会被当成合法重试"

    eid = _新事件("乙-连打")
    投递.投递一次(eid, 强制失败=True)
    投递.投递一次(eid, 强制失败=True)  # 立即连点（修复后应被闸口拦下）

    rep = 互证.投递流水对账(eid)

    assert rep["事件号"] == eid
    # 只有首笔落正式流水，且它是“首投”，不是“重试”
    assert rep["正式失败数"] == 1
    assert rep["合法重试数"] == 0
    # 窗内那一下必须被识别为连打/回刷，而不是合法重试
    assert int(rep["窗内连打数"]) >= 1
    # 闸口正常工作时，账是平的（拦截留痕、正式流水干净）
    assert rep["对齐"] is True
    assert rep.get("影子正式同票") is True


def test_乙_对账能识破正式流水中的窗内双打(库):
    """野写入/旧数据在正式表里塞了同秒两笔失败，对账不得追认成两次合法重试。"""
    assert hasattr(互证, "投递流水对账")
    eid = _新事件("乙-野写")

    # 绕过闸口，直接造“旧主干风格”的同秒连打两笔
    t = "2026-11-26T00:00:00Z"
    conn = 连接()
    try:
        for tid in ("TRY-y-old-1", "TRY-y-old-2"):
            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tid, eid, "ep-audit", "sig-old", "失败", 500, "投递失败", t),
            )
        conn.commit()
    finally:
        conn.close()

    rep = 互证.投递流水对账(eid)
    # 同秒两笔，间隔 0，必在退避窗内
    assert int(rep["合法重试数"]) == 0
    assert int(rep["窗内连打数"]) >= 1 or int(rep["非法落账数"]) >= 1
    # 正式流水被污染 -> 对账必须判撕裂，不许盖对齐
    assert rep["对齐"] is False


def test_乙_人工开窗后的立即重投不被对账误判(库, monkeypatch):
    """默认退避下走满失败进死信，人工开窗后同一时刻立即重投成功——

    对账必须认「人工开窗」锚，把这笔判为『人工开窗后重投』而不是窗内非法落账，
    否则操作员的合法处置反而把账对撕裂。
    """
    from app.services import 时间, 死信

    cur = [1_700_000_000.0]
    monkeypatch.setattr(时间, "纪元秒", lambda: cur[0])

    eid = _新事件("乙-开窗")
    投递.投递一次(eid, 强制失败=True)
    cur[0] += 31
    投递.投递一次(eid, 强制失败=True)
    cur[0] += 31
    r3 = 投递.投递一次(eid, 强制失败=True)
    assert r3["事件状态"] == "死信"

    # 不假钟快进：开窗与重投落在同一纪元，模拟操作员立刻点「死信重投」
    rr = 死信.重投(eid)
    assert rr["状态"] == "成功"

    rep = 互证.投递流水对账(eid)
    assert int(rep["非法落账数"]) == 0
    assert rep["对齐"] is True
    assert rep["影子正式同票"] is True
    分类 = [x["分类"] for x in rep["流水"]]
    assert "人工开窗后重投" in 分类
    assert "窗内连打" not in 分类


def test_乙_互证跑一次不再为连打盖假绿(库):
    """互证批次要把退避对账并入口径：窗内连打拦截数可见且账实同源。"""
    eid = _新事件("乙-全扫")
    投递.投递一次(eid, 强制失败=True)
    投递.投递一次(eid, 强制失败=True)

    rep = 互证.跑一次()
    明细 = rep["明细"]
    assert int(明细.get("窗内连打拦截数", 0)) >= 1
    assert "合法重试数" in 明细
    # 闸口拦下连打后，全局仍应对齐
    assert rep["是否对齐"] is True
    assert rep["是否全部对齐"] is True
