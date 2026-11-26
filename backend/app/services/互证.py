from __future__ import annotations
from app.db import 连接
from app.services.时间 import 此刻, 解析纪元
from app.services import 退避闸


def _尝试纪元(a) -> float:
    try:
        ep = a["投递纪元"]
    except (IndexError, KeyError):
        ep = None
    return float(ep) if ep else 解析纪元(a["投递时"])


def 投递流水对账(事件号: str | None = None, conn=None) -> dict:
    """hook-13 投递流水对账。

    认退避窗与窗指纹，回答三件事：
    1) 哪些正式尝试是『合法重试』（距上一笔失败已越窗）；
    2) 哪些是『窗内连打』——审计里的窗内拦截，或混进正式表的非法落账；
    3) 正式 投递尝试 与审计 退避账册放行记录 是否按尝试号『同票』。
    """
    own = conn is None
    if own:
        conn = 连接()
    try:
        规 = 退避闸.读规(conn)
        if 事件号:
            return _对账一事件(conn, 事件号, 规)

        ids: set[str] = set()
        for sql in (
            "SELECT 事件号 FROM 事件",
            "SELECT DISTINCT 事件号 FROM 投递尝试",
            "SELECT DISTINCT 事件号 FROM 退避账册",
        ):
            for r in conn.execute(sql).fetchall():
                if r[0]:
                    ids.add(r[0])
        逐事件 = [_对账一事件(conn, e, 规) for e in sorted(ids)]
        return _汇总(逐事件)
    finally:
        if own:
            conn.close()


def _对账一事件(conn, 事件号: str, 规: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY rowid ASC",
        (事件号,),
    ).fetchall()

    # 人工开窗锚：与 退避闸.窗状态 同口径——开窗之后的尝试不再按旧失败窗论连打。
    重置锚 = [
        float(r["窗口起纪元"] or 0.0)
        for r in conn.execute(
            "SELECT 窗口起纪元 FROM 退避账册 "
            "WHERE 事件号=? AND 闸口='人工开窗' ORDER BY 编号 ASC",
            (事件号,),
        ).fetchall()
    ]

    流水: list[dict] = []
    失败序号 = 0
    上一失败纪元: float | None = None
    合法重试 = 0
    非法落账 = 0
    正式失败数 = 0

    for i, a in enumerate(rows):
        ep = _尝试纪元(a)

        # 若「上一失败锚 ~ 本行」之间钉过人工开窗，则旧退避窗作废。
        # 不用消费式游标：冻结时钟下开窗/落账可能同纪元，游标会被前一行提前吃掉。
        本行人开窗后 = False
        if i > 0 and 上一失败纪元 is not None:
            for r_ep in 重置锚:
                if 上一失败纪元 - 1e-6 <= r_ep <= ep + 1e-6:
                    上一失败纪元 = None
                    本行人开窗后 = True
                    break

        分类 = "首投"
        合法 = True
        需要间隔: float | None = None
        距上次: float | None = None

        if i > 0:
            if 上一失败纪元 is None:
                分类 = "人工开窗后重投" if 本行人开窗后 else "合法重试"
                合法重试 += 1
            else:
                k = max(1, 失败序号)
                需要间隔 = 退避闸.有效间隔秒(k, 规)
                距上次 = ep - 上一失败纪元
                if 需要间隔 > 0 and 距上次 + 1e-6 < 需要间隔:
                    分类 = "窗内连打"
                    合法 = False
                    非法落账 += 1
                else:
                    分类 = "合法重试"
                    合法重试 += 1

        流水.append(
            {
                "尝试号": a["尝试号"],
                "状态": a["状态"],
                "投递时": a["投递时"],
                "纪元": ep,
                "分类": 分类,
                "合法": 合法,
                "距上一失败秒": None if 距上次 is None else round(距上次, 3),
                "要求间隔秒": 需要间隔,
            }
        )

        if a["状态"] == "失败":
            正式失败数 += 1
            失败序号 += 1
            上一失败纪元 = ep

    # 审计账册（影子）口径
    grp = {
        r["闸口"]: r["c"]
        for r in conn.execute(
            "SELECT 闸口, COUNT(*) AS c FROM 退避账册 WHERE 事件号=? GROUP BY 闸口",
            (事件号,),
        ).fetchall()
    }
    审计拦截 = int(grp.get("窗内拦截", 0))
    审计放行 = int(grp.get("放行落账", 0))
    人工开窗 = int(grp.get("人工开窗", 0))

    # 同票：正式尝试 ↔ 放行账册 按尝试号一一对应
    正式ids = {a["尝试号"] for a in rows}
    影子rows = conn.execute(
        "SELECT 尝试号 FROM 退避账册 WHERE 事件号=? AND 闸口='放行落账'",
        (事件号,),
    ).fetchall()
    影子ids = {r["尝试号"] for r in 影子rows if r["尝试号"]}
    正式缺影子 = 正式ids - 影子ids
    影子缺正式 = 影子ids - 正式ids
    同票 = not 正式缺影子 and not 影子缺正式

    窗内连打 = 非法落账 + 审计拦截
    对齐 = (非法落账 == 0) and 同票
    if not rows and not grp:
        结论 = "尚无投递流水"
    elif 对齐 and 窗内连打:
        结论 = f"闸口已拦截 {窗内连打} 次窗内连打，正式流水干净、账实同票"
    elif 对齐:
        结论 = "流水合规：重试均在退避窗外，正式/影子同票"
    else:
        结论 = "对账撕裂：存在窗内非法落账或正式/影子不同票"

    return {
        "事件号": 事件号,
        "正式尝试数": len(rows),
        "正式失败数": 正式失败数,
        "合法重试数": 合法重试,
        "非法落账数": 非法落账,
        "审计拦截数": 审计拦截,
        "窗内连打拦截数": 审计拦截,
        "审计放行数": 审计放行,
        "人工开窗数": 人工开窗,
        "窗内连打数": 窗内连打,
        "影子正式同票": bool(同票),
        "正式缺影子数": len(正式缺影子),
        "影子缺正式数": len(影子缺正式),
        "对齐": bool(对齐),
        "结论": 结论,
        "流水": 流水,
    }


def _汇总(逐事件: list[dict]) -> dict:
    键 = (
        "正式尝试数", "正式失败数", "合法重试数", "非法落账数",
        "审计拦截数", "窗内连打拦截数", "审计放行数", "人工开窗数",
        "窗内连打数", "正式缺影子数", "影子缺正式数",
    )
    agg = {k: sum(int(p.get(k, 0)) for p in 逐事件) for k in 键}
    同票 = all(p["影子正式同票"] for p in 逐事件)
    对齐 = all(p["对齐"] for p in 逐事件)
    return {
        "事件号": None,
        "事件数": len(逐事件),
        **agg,
        "影子正式同票": bool(同票),
        "对齐": bool(对齐),
        "结论": "全量投递流水对账对齐" if 对齐 else "投递流水对账存在窗内连打/不同票",
        "逐事件": 逐事件[:50],
    }


def 跑一次() -> dict:
    t = 此刻()
    conn = 连接()
    try:
        总 = conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"]
        已 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='已投递'").fetchone()["c"]
        死 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='死信'").fetchone()["c"]
        待 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态 IN ('待投','投递中')").fetchone()["c"]
        匣 = conn.execute("SELECT COUNT(*) AS c FROM 死信 WHERE 状态='待处置'").fetchone()["c"]

        退 = 投递流水对账(None, conn=conn)
        基础对齐 = 1 if (死 == 匣 and 已 + 死 + 待 <= 总) else 0
        对齐 = 1 if (基础对齐 and 退["对齐"]) else 0
        if 对齐:
            说明 = "事件/投递/死信口径对齐，退避流水同票、无窗内连打落账"
        elif not 基础对齐:
            说明 = "死信匣与事件死信数不一致或水位异常"
        else:
            说明 = "退避流水对账未过：存在窗内连打落账或正式/影子不同票"
        conn.execute(
            "INSERT INTO 互证批次(时刻,事件总数,已投递数,死信数,待投数,是否对齐,说明) VALUES (?,?,?,?,?,?,?)",
            (t, 总, 已, 死, 待, 对齐, 说明),
        )
        conn.commit()
        return {
            "时刻": t,
            "事件总数": 总,
            "已投递数": 已,
            "死信数": 死,
            "待投数": 待,
            "死信匣待处置": 匣,
            "是否对齐": bool(对齐),
            "是否全部对齐": bool(对齐),
            "说明": 说明,
            "重试对账": 退,
            "明细": {
                "总": 总, "已": 已, "死": 死, "待": 待, "匣": 匣,
                "合法重试数": 退["合法重试数"],
                "窗内连打数": 退["窗内连打数"],
                "窗内连打拦截数": 退["窗内连打拦截数"],
                "非法落账数": 退["非法落账数"],
                "影子正式同票": 退["影子正式同票"],
            },
        }
    finally:
        conn.close()
