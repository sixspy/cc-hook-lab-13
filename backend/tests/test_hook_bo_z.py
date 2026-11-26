# -*- coding: utf-8 -*-
"""hook-13 争用失败测丙（服务：并发闸锁 app/services/退避闸.py + 投递临界区）。

事故：读「上一笔失败 / 退避窗」与「写本笔尝试」不是同一个临界区，窗内两个
请求并发进入，都读到失败次数 0 / 无窗，于是双打：正式流水写出两笔，且两笔
都返回“失败次数=1”（丢失更新）。

正确契约：同一事件号的并发投递必须打同一把事件锁，并在一个 IMMEDIATE 事务里
“读窗 + 写尝试”同成败——恰好一笔落账，其余在窗内被拒。

按题目要求：本文件先于主干修改建立，在旧主干上 pytest 必须见红。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_bo_z_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递  # noqa: E402
import app.db as dbmod  # noqa: E402


@pytest.fixture
def 库(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "数据目录", tmp_path)
    monkeypatch.setattr(dbmod, "库路径", tmp_path / "hook_bo_z.db")
    seed.初始化库()
    try:
        from app.services import 退避闸

        退避闸.重置锁()
    except Exception:
        pass
    yield tmp_path


def _新事件(摘要: str = "bo-z") -> str:
    return 入队.建事件(
        {"事件类型": "invoice.paid", "端点键": "ep-billing", "载荷摘要": 摘要}
    )["事件号"]


def _正式尝试数(eid: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=?", (eid,)
        ).fetchone()["c"]
    finally:
        conn.close()


def _并发投递(eid: str, n: int):
    闸 = threading.Barrier(n)
    结果 = []

    def worker():
        闸.wait()  # 尽量同一瞬间发起
        结果.append(投递.投递一次(eid, 强制失败=True))

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
        assert not t.is_alive(), "并发投递线程超时/死锁"
    return 结果


def test_丙_窗内双请求并发不得双打(库):
    """【争用失败测·必须先红】窗内双请求并发双打。"""
    eid = _新事件("丙-双请求")
    结果 = _并发投递(eid, 2)

    assert len(结果) == 2
    # 核心不变量：并发两下，正式流水只能有一笔。
    # 旧主干稳定为 2（已实测 5/5），这里必然见红。
    assert _正式尝试数(eid) == 1, "窗内双请求并发双打（hook-13 争用事故）"

    放行 = [r for r in 结果 if not r.get("退避拦截")]
    拦截 = [r for r in 结果 if r.get("退避拦截") is True]
    assert len(放行) == 1
    assert len(拦截) == 1


def test_丙_五并发也只许一笔落账(库):
    eid = _新事件("丙-五并发")
    结果 = _并发投递(eid, 5)
    assert _正式尝试数(eid) == 1
    assert len([r for r in 结果 if r.get("退避拦截")]) == 4


def test_丙_同事件打同一把锁且临界区不重叠(库):
    """锁服务本身：同事件号必须取到同一把锁，临界区并发度恒为 1。"""
    # 红阶段该服务尚不存在：硬导入让本用例直接失败（不使用 skip 回避见红）
    from app.services import 退避闸
    eid = _新事件("丙-同锁")

    assert 退避闸.取事件锁(eid) is 退避闸.取事件锁(eid)

    进入 = 0
    最大并发 = 0
    守卫 = threading.Lock()

    def 临界():
        nonlocal 进入, 最大并发
        with 退避闸.取事件锁(eid):
            with 守卫:
                进入 += 1
                最大并发 = max(最大并发, 进入)
            time.sleep(0.01)  # 若锁失效，足够让第二个线程插进来
            with 守卫:
                进入 -= 1

    ts = [threading.Thread(target=临界) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert 最大并发 == 1, "事件锁临界区发生重叠，窗读/尝试写会双打"
