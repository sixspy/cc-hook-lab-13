from datetime import datetime, timezone
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 用户 (
  账号 TEXT PRIMARY KEY,
  密码哈希 TEXT NOT NULL,
  角色 TEXT NOT NULL DEFAULT '管理员'
);
CREATE TABLE IF NOT EXISTS 签名规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递端点 (
  端点键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  回调URL TEXT NOT NULL,
  密钥版本 TEXT NOT NULL DEFAULT 'v1',
  启用 INTEGER NOT NULL DEFAULT 1,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 事件 (
  事件号 TEXT PRIMARY KEY,
  事件类型 TEXT NOT NULL,
  端点键 TEXT NOT NULL DEFAULT '',
  载荷摘要 TEXT NOT NULL DEFAULT '',
  优先级 TEXT NOT NULL DEFAULT '普通',
  状态 TEXT NOT NULL DEFAULT '待投',
  入队时 TEXT NOT NULL,
  更新时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递尝试 (
  尝试号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  端点键 TEXT NOT NULL,
  签名摘要 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '失败',
  响应码 INTEGER NOT NULL DEFAULT 0,
  说明 TEXT NOT NULL DEFAULT '',
  投递时 TEXT NOT NULL,
  投递纪元 REAL,
  FOREIGN KEY (事件号) REFERENCES 事件(事件号),
  FOREIGN KEY (端点键) REFERENCES 投递端点(端点键)
);
CREATE TABLE IF NOT EXISTS 死信 (
  死信号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL UNIQUE,
  端点键 TEXT NOT NULL,
  失败次数 INTEGER NOT NULL DEFAULT 0,
  末次原因 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '待处置',
  入匣时 TEXT NOT NULL,
  FOREIGN KEY (事件号) REFERENCES 事件(事件号)
);
CREATE TABLE IF NOT EXISTS 互证批次 (
  批次号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  事件总数 INTEGER NOT NULL,
  已投递数 INTEGER NOT NULL,
  死信数 INTEGER NOT NULL,
  待投数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 操作留痕 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  操作 TEXT NOT NULL,
  事件号 TEXT NOT NULL DEFAULT '',
  详情 TEXT NOT NULL DEFAULT ''
);
-- hook-13 路径甲：审计账册钉住“退避窗指纹”，窗内回刷在此被记为拦截且不写正式流水
CREATE TABLE IF NOT EXISTS 退避账册 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  事件号 TEXT NOT NULL,
  窗指纹 TEXT NOT NULL,
  尝试号 TEXT NOT NULL DEFAULT '',
  闸口 TEXT NOT NULL,
  结果 TEXT NOT NULL DEFAULT '',
  窗口起纪元 REAL NOT NULL DEFAULT 0,
  窗口止纪元 REAL NOT NULL DEFAULT 0,
  尝试间隔秒 REAL NOT NULL DEFAULT 0,
  退避剩余秒 REAL NOT NULL DEFAULT 0,
  详情 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_退避账册_事件 ON 退避账册(事件号, 编号);
CREATE INDEX IF NOT EXISTS idx_退避账册_指纹 ON 退避账册(窗指纹);
CREATE INDEX IF NOT EXISTS idx_退避账册_闸口 ON 退避账册(闸口);
"""

# hook-13 重试退避策略默认值（与签名规同票，可在签名规页热改）
默认退避规 = {
    "退避秒": "30",
    "退避倍数": "1",
    "退避上限秒": "300",
}


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 初始化库() -> None:
    conn = 连接()
    conn.executescript(建表)
    _迁移(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM 用户").fetchone()["c"] == 0:
        conn.execute("INSERT INTO 用户(账号,密码哈希,角色) VALUES (?,?,?)", ("admin", "123456", "管理员"))
    if conn.execute("SELECT COUNT(*) AS c FROM 签名规").fetchone()["c"] == 0:
        for k, v in {
            "最大重试": "3",
            "产品名": "投递台",
            "主链": "端点登记→事件入队→签名投递→失败重试与死信→投递账互证",
            "签名算法": "HMAC-SHA256",
            "超时秒": "5",
            **默认退避规,
        }.items():
            conn.execute("INSERT INTO 签名规(键,值) VALUES (?,?)", (k, v))
    # 老库升级：补齐退避策略键（不覆盖运营已改值）
    for k, v in 默认退避规.items():
        conn.execute("INSERT OR IGNORE INTO 签名规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 投递端点").fetchone()["c"] == 0:
        for row in [
            ("ep-billing", "账单回调", "https://example.local/hooks/billing", "v1", 1, "账务"),
            ("ep-notify", "通知回调", "https://example.local/hooks/notify", "v1", 1, "通知"),
            ("ep-audit", "审计回调", "https://example.local/hooks/audit", "v2", 1, "审计"),
            ("ep-legacy", "旧版回调", "https://example.local/hooks/legacy", "v1", 0, "停用"),
            ("ep-partner", "合作方回调", "https://partner.local/ingest", "v1", 1, "外部"),
        ]:
            conn.execute(
                "INSERT INTO 投递端点(端点键,名称,回调URL,密钥版本,启用,说明) VALUES (?,?,?,?,?,?)",
                row,
            )
    if conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"] == 0:
        t = 此刻()
        for row in [
            ("EVT-demo-01", "invoice.paid", "ep-billing", "invoice=1001", "普通", "待投"),
            ("EVT-demo-02", "user.created", "ep-notify", "user=u42", "普通", "投递中"),
            ("EVT-demo-03", "order.refund", "ep-audit", "order=908", "紧急", "待投"),
            ("EVT-demo-04", "user.updated", "ep-notify", "user=u7", "低", "已投递"),
            ("EVT-demo-05", "invoice.failed", "ep-billing", "invoice=1002", "普通", "死信"),
        ]:
            conn.execute(
                "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) VALUES (?,?,?,?,?,?,?,?)",
                (*row, t, t),
            )
        conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) VALUES (?,?,?,?,?,?,?,?)",
            ("TRY-demo-01", "EVT-demo-04", "ep-notify", "sig-demo", "成功", 200, "ok", t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) VALUES (?,?,?,?,?,?,?)",
            ("DL-demo-01", "EVT-demo-05", "ep-billing", 3, "连接超时", "待处置", t),
        )
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "种子", "", "初始化投递台样例"),
        )
    _补票同票(conn)
    conn.commit()
    conn.close()


def _迁移(conn) -> None:
    """轻量幂等迁移：老库补『投递纪元』列。新表已由 建表 建好。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(投递尝试)").fetchall()}
    if cols and "投递纪元" not in cols:
        conn.execute("ALTER TABLE 投递尝试 ADD COLUMN 投递纪元 REAL")


def _补票同票(conn) -> None:
    """路径甲『同票』：为历史正式流水补一条放行账册（影子），
    使 投递尝试(正式) ↔ 退避账册放行记录(影子) 按尝试号一一对应。"""
    from app.services.时间 import 解析纪元
    from app.services.退避闸 import 指纹

    缺票 = conn.execute(
        """
        SELECT a.尝试号, a.事件号, a.状态, a.投递时, a.投递纪元
        FROM 投递尝试 a
        WHERE NOT EXISTS (
            SELECT 1 FROM 退避账册 z
            WHERE z.尝试号 = a.尝试号 AND z.闸口 = '放行落账'
        )
        """
    ).fetchall()
    for a in 缺票:
        ep = a["投递纪元"] if a["投递纪元"] else 解析纪元(a["投递时"])
        conn.execute(
            "INSERT INTO 退避账册"
            "(时刻,事件号,窗指纹,尝试号,闸口,结果,窗口起纪元,窗口止纪元,尝试间隔秒,退避剩余秒,详情)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                a["投递时"],
                a["事件号"],
                指纹(a["事件号"], a["尝试号"]),
                a["尝试号"],
                "放行落账",
                a["状态"] or "",
                ep or 0.0,
                0.0,
                0.0,
                0.0,
                "历史流水补票同票",
            ),
        )
