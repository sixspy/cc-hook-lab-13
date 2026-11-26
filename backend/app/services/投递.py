from __future__ import annotations
import hashlib
import hmac
import math
import uuid

from app.db import 立即连接
from app.services import 时间, 签名目录, 退避闸


def _签名(密钥版本: str, 事件号: str, 摘要: str) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = f"hook-secret-{密钥版本}".encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def _拦截返回(窗: dict) -> dict:
    剩 = int(窗["退避剩余秒"])
    return {
        "ok": False,
        "拦截": True,
        "退避拦截": True,
        "写出": False,
        "状态": "退避拦截",
        "事件状态": "退避拦截",
        "闸口": 窗["闸口"],
        "窗指纹": 窗["窗指纹"],
        "锚尝试号": 窗.get("锚尝试号", ""),
        "退避剩余秒": 剩,
        "退避剩余": 窗["退避剩余"],
        "尝试间隔秒": 窗["尝试间隔秒"],
        "尝试间隔": 窗["尝试间隔"],
        "骨签": 退避闸.骨签,
        "错误": f"退避窗内不得连打：{窗['闸口']}，约 {剩} 秒后再试"
                f"（尝试间隔 {窗['尝试间隔']}）",
    }


def 投递一次(事件号: str, 强制失败: bool = False) -> dict:
    """对事件执行一次签名投递。

    hook-13：读退避窗与写尝试在『同一把事件锁 + 同一个 IMMEDIATE 事务』内同成败。
    窗内再次投递 -> 只写审计账册『窗内拦截』，不写正式投递尝试（拒窗内回刷）。
    本地模拟：默认成功；强制失败或类型以 .fail 结尾则记失败并开退避窗。
    """
    # 同一事件打同一把锁：并发双请求在进程内被串行化，第二个进事务后必见首笔
    with 退避闸.取事件锁(事件号):
        conn = 立即连接()
        try:
            conn.execute("BEGIN IMMEDIATE")

            ev = conn.execute(
                "SELECT * FROM 事件 WHERE 事件号=?", (事件号,)
            ).fetchone()
            if not ev:
                raise ValueError("无此事件")
            if ev["状态"] in ("已投递", "死信"):
                raise ValueError(f"事件状态为{ev['状态']}，不可再投")
            ep = conn.execute(
                "SELECT * FROM 投递端点 WHERE 端点键=?", (ev["端点键"],)
            ).fetchone()
            if not ep:
                raise ValueError("端点缺失")
            if int(ep["启用"]) != 1:
                raise ValueError("端点停用")

            # 在 IMMEDIATE 事务内、拿到写锁之后重读退避窗，杜绝并发双打
            now = 时间.纪元秒()
            t = 时间.此刻()
            窗 = 退避闸.窗状态(事件号, now=now, conn=conn)
            if 窗 is None:
                raise ValueError("无此事件")

            if 窗["在窗内"]:
                退避闸.钉账(
                    conn,
                    时刻=t,
                    事件号=事件号,
                    窗指纹=窗["窗指纹"],
                    闸口="窗内拦截",
                    结果="拦截",
                    窗口起=窗["窗口起纪元"],
                    窗口止=窗["窗口止纪元"],
                    间隔=窗["尝试间隔秒"],
                    剩余=窗["退避剩余秒"],
                    详情="退避窗内连点/并发回刷，已拒绝且未写正式流水",
                )
                conn.execute("COMMIT")
                return _拦截返回(窗)

            规 = 退避闸.读规(conn)
            最大 = int(规.get("最大重试") or 3)
            失败次数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'",
                (事件号,),
            ).fetchone()["c"]

            尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
            sig = _签名(ep["密钥版本"], 事件号, ev["载荷摘要"])
            成功 = (not 强制失败) and (失败次数 < 1000)
            if 强制失败 or str(ev["事件类型"]).endswith(".fail"):
                成功 = False

            if 成功:
                conn.execute(
                    "INSERT INTO 投递尝试"
                    "(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时,投递纪元)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (尝试号, 事件号, ev["端点键"], sig, "成功", 200, "投递成功", t, now),
                )
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("已投递", t, 事件号),
                )
                conn.execute(
                    "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                    (t, "投递成功", 事件号, 尝试号),
                )
                fp = 退避闸.指纹(事件号, 尝试号)
                退避闸.钉账(
                    conn, 时刻=t, 事件号=事件号, 窗指纹=fp, 尝试号=尝试号,
                    闸口="放行落账", 结果="成功", 窗口起=now, 窗口止=now,
                    间隔=0, 剩余=0, 详情="投递成功，事件结案",
                )
                conn.execute("COMMIT")
                return {
                    "尝试号": 尝试号, "状态": "成功", "事件状态": "已投递",
                    "签名摘要": sig, "退避拦截": False, "写出": True,
                    "闸口": 退避闸.闸_成功, "窗指纹": fp,
                    "退避剩余秒": 0, "尝试间隔秒": 窗["尝试间隔秒"],
                    "骨签": 退避闸.骨签,
                }

            # —— 失败：正式尝试 + 放行账册同事务双写，并开一扇新退避窗 ——
            conn.execute(
                "INSERT INTO 投递尝试"
                "(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时,投递纪元)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, ev["端点键"], sig, "失败", 500, "投递失败", t, now),
            )
            新失败 = 失败次数 + 1
            新间隔 = 退避闸.有效间隔秒(新失败, 规)
            窗止 = now + 新间隔
            fp = 退避闸.指纹(事件号, 尝试号)

            if 新失败 >= 最大:
                dl = f"DL-{uuid.uuid4().hex[:10]}"
                if not conn.execute(
                    "SELECT 1 FROM 死信 WHERE 事件号=?", (事件号,)
                ).fetchone():
                    conn.execute(
                        "INSERT INTO 死信"
                        "(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (dl, 事件号, ev["端点键"], 新失败, "重试耗尽", "待处置", t),
                    )
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("死信", t, 事件号),
                )
                事件状态 = "死信"
                详情 = f"失败{新失败}/{最大}，重试耗尽入死信匣"
            else:
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("投递中", t, 事件号),
                )
                事件状态 = "投递中"
                详情 = f"失败{新失败}/{最大}，进入退避窗 {_整(新间隔)} 秒"

            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "投递失败", 事件号, f"失败{新失败}/{最大}"),
            )
            退避闸.钉账(
                conn, 时刻=t, 事件号=事件号, 窗指纹=fp, 尝试号=尝试号,
                闸口="放行落账", 结果="失败", 窗口起=now, 窗口止=窗止,
                间隔=新间隔, 剩余=新间隔, 详情=详情,
            )
            conn.execute("COMMIT")
            return {
                "尝试号": 尝试号, "状态": "失败", "事件状态": 事件状态,
                "失败次数": 新失败, "签名摘要": sig,
                "退避拦截": False, "写出": True,
                "闸口": 退避闸.闸_新败, "窗指纹": fp,
                "退避剩余秒": int(math.ceil(新间隔)),
                "退避剩余": f"约 {int(math.ceil(新间隔))} 秒",
                "尝试间隔秒": 新间隔,
                "尝试间隔": f"{_整(新间隔)} 秒",
                "骨签": 退避闸.骨签,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def _整(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"
