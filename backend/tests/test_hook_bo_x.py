# -*- coding: utf-8 -*-
"""hook-13 失败测甲（服务：投递服务 app/services/投递.py）。

事故：一笔失败后立即连点「签名投递」，没有退避窗，第二笔失败尝试仍被写出，
失败次数被瞬间累加。正确契约（路径甲：审计账册钉退避窗指纹、拒窗内回刷）：

* 失败后进入退避窗，窗内的再次投递必须被闸口拦下；
* 拦下的一笔不得写进正式「投递尝试」流水（窗内第二笔失败尝试不得写出）；
* 窗过之后，重试仍可落账，主链（失败累计 -> 死信）不被改坏。

按题目要求：本文件先于主干修改建立，在旧主干上 pytest 必须见红。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_bo_x_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递, 签名目录  # noqa: E402
import app.db as dbmod  # noqa: E402


@pytest.fixture
def 库(tmp_path, monkeypatch):
    """每个用例独立 SQLite 库，避免与基线用例共享模块级临时库。"""
    monkeypatch.setattr(dbmod, "数据目录", tmp_path)
    monkeypatch.setattr(dbmod, "库路径", tmp_path / "hook_bo_x.db")
    seed.初始化库()
    try:
        from app.services import 退避闸

        退避闸.重置锁()
    except Exception:
        pass  # 见红阶段退避闸尚不存在
    yield tmp_path


@pytest.fixture
def 假钟(monkeypatch):
    from app.services import 时间

    class 钟:
        def __init__(self):
            self.t = 1_700_000_000.0

        def now(self):
            return self.t

        def 前进(self, s):
            self.t += float(s)

    z = 钟()
    # raising=False：旧主干还没有「纪元秒」，见红阶段也允许打桩
    monkeypatch.setattr(时间, "纪元秒", z.now, raising=False)
    return z


def _新事件(摘要: str = "bo-x") -> str:
    return 入队.建事件(
        {"事件类型": "invoice.paid", "端点键": "ep-billing", "载荷摘要": 摘要}
    )["事件号"]


def _正式失败数(eid: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'",
            (eid,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _正式尝试数(eid: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=?", (eid,)
        ).fetchone()["c"]
    finally:
        conn.close()


def test_甲_窗内第二笔失败尝试不得写出(库):
    """【失败测甲·必须先红】退避窗内第二笔失败尝试仍写出。"""
    eid = _新事件("甲-连打")

    r1 = 投递.投递一次(eid, 强制失败=True)
    assert r1["状态"] == "失败"

    # 立即连点第二下（没有任何等待）——正落在退避窗内
    r2 = 投递.投递一次(eid, 强制失败=True)

    # 最硬的一条：正式流水里只能有第一笔失败。
    # 旧主干这里是 2 == 1，必然见红（证明“第二笔仍写出”）。
    assert _正式失败数(eid) == 1, "退避窗内第二笔失败尝试仍写出（hook-13 事故）"

    # 第二下必须是结构化的“退避拦截”，而不是又一笔失败
    assert r2.get("退避拦截") is True
    assert r2.get("写出") is False
    assert r2.get("状态") == "退避拦截"
    assert "退避窗内" in str(r2.get("闸口", ""))
    # 页面要读得到的三件套
    assert int(r2.get("退避剩余秒", 0)) > 0
    assert float(r2.get("尝试间隔秒", 0)) > 0
    assert r2.get("窗指纹")


def test_甲_窗内连点不得累加失败次数(库):
    eid = _新事件("甲-次数")
    投递.投递一次(eid, 强制失败=True)
    r2 = 投递.投递一次(eid, 强制失败=True)
    assert r2.get("退避拦截") is True
    d = 签名目录.事件一条(eid)
    # 只有第一笔被落账，故事件详情里也只能数到一笔失败
    assert len([x for x in d["尝试"] if x["状态"] == "失败"]) == 1


def test_甲_越窗之后重试可落账并能走到死信(库, 假钟):
    """闸不是永久封死：越过退避窗后重试合法，失败累计到上限仍进死信。"""
    签名目录.改签名规({"退避秒": "10", "退避倍数": "1", "最大重试": "3"})
    eid = _新事件("甲-越窗")

    last = 投递.投递一次(eid, 强制失败=True)
    assert last["状态"] == "失败"

    # 窗内连点被拦
    blocked = 投递.投递一次(eid, 强制失败=True)
    assert blocked.get("退避拦截") is True
    assert _正式失败数(eid) == 1

    # 每一笔合法重试之间都越过退避窗
    结局 = None
    for _ in range(4):
        假钟.前进(11)
        结局 = 投递.投递一次(eid, 强制失败=True)
        if 结局.get("事件状态") == "死信":
            break
    assert 结局 is not None and 结局.get("事件状态") == "死信"
    assert any(x["事件号"] == eid for x in 签名目录.死信列表())
    # 三笔正式失败（窗内那下没有写出）
    assert _正式失败数(eid) == 3
