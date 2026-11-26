from __future__ import annotations
from app.db import 连接
from app.services import 校验, 退避闸


def 签名规() -> dict:
    conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        return {r["键"]: r["值"] for r in rows}
    finally:
        conn.close()


def 改签名规(body: dict) -> dict:
    conn = 连接()
    try:
        for k, v in body.items():
            键 = 校验.非空("键", k)
            值 = 校验.非空("值", v)
            conn.execute(
                "INSERT INTO 签名规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (键, 值),
            )
        conn.commit()
        return 签名规()
    finally:
        conn.close()


def 概览() -> dict:
    conn = 连接()
    try:
        def c(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "端点数": c("SELECT COUNT(*) FROM 投递端点"),
            "启用端点": c("SELECT COUNT(*) FROM 投递端点 WHERE 启用=1"),
            "事件总数": c("SELECT COUNT(*) FROM 事件"),
            "待投": c("SELECT COUNT(*) FROM 事件 WHERE 状态='待投'"),
            "投递中": c("SELECT COUNT(*) FROM 事件 WHERE 状态='投递中'"),
            "已投递": c("SELECT COUNT(*) FROM 事件 WHERE 状态='已投递'"),
            "死信": c("SELECT COUNT(*) FROM 事件 WHERE 状态='死信'"),
            "尝试数": c("SELECT COUNT(*) FROM 投递尝试"),
            "死信匣": c("SELECT COUNT(*) FROM 死信 WHERE 状态='待处置'"),
            "退避拦截": c("SELECT COUNT(*) FROM 退避账册 WHERE 闸口='窗内拦截'"),
            "签名规": 签名规(),
        }
    finally:
        conn.close()


def 事件列表() -> list:
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 事件 ORDER BY 入队时 DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["退避"] = 退避闸.窗状态(d["事件号"], conn=conn)
            except Exception:
                d["退避"] = None
            out.append(d)
        return out
    finally:
        conn.close()


def 事件一条(事件号: str) -> dict | None:
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["尝试"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY 投递时 DESC", (事件号,)
        ).fetchall()]
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        d["死信"] = dict(dl) if dl else None
        d["退避"] = 退避闸.窗状态(事件号, conn=conn)
        return d
    finally:
        conn.close()


def 投递列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            """
            SELECT a.*,
                   (SELECT z.闸口 FROM 退避账册 z
                      WHERE z.尝试号 = a.尝试号 AND z.闸口 = '放行落账' LIMIT 1) AS 账册闸口,
                   (SELECT z.窗指纹 FROM 退避账册 z
                      WHERE z.尝试号 = a.尝试号 AND z.闸口 = '放行落账' LIMIT 1) AS 窗指纹
            FROM 投递尝试 a
            ORDER BY a.rowid DESC LIMIT 200
            """
        ).fetchall()]
    finally:
        conn.close()


def 死信列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 死信 ORDER BY 入匣时 DESC").fetchall()]
    finally:
        conn.close()


def 端点列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.*,
                   (SELECT COUNT(*) FROM 事件 v WHERE v.端点键 = e.端点键) AS 事件数,
                   (SELECT COUNT(*) FROM 投递尝试 a WHERE a.端点键 = e.端点键) AS 尝试数,
                   (SELECT COUNT(*) FROM 死信 d
                      WHERE d.端点键 = e.端点键 AND d.状态 = '待处置') AS 待处置死信
            FROM 投递端点 e
            ORDER BY e.端点键
            """
        ).fetchall()]
    finally:
        conn.close()


def 互证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 互证批次 ORDER BY 批次号 DESC LIMIT 50").fetchall()]
    finally:
        conn.close()
