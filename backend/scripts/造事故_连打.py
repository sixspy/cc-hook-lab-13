"""hook-13 事故夹具：先用夹具/强制失败造一笔失败，再「立即连点投递」。

修复前事故：重试没有退避窗，同一事件在同一秒内被连打 5 次，
投递尝试表写出多笔失败，失败次数被快速累加，对端被短时间打爆。

修复后（路径甲）：只有首笔放行落账，其余连点被退避闸以
「退避窗内·禁止连打」拦截（返回退避拦截=True、写出=False），
并在审计账册逐笔登记「窗内拦截」，正式流水不再出现连打失败。
本脚本会按实际结果给出「事故复现」或「闸口已生效」两种结论。

用法：
  . .venv/bin/activate
  PYTHONPATH=/workspace/backend python scripts/造事故_连打.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 必须在 import app.db 之前指定独立数据目录，避免污染开发库
事故库 = tempfile.mkdtemp(prefix="hook13_incident_")
os.environ["数据目录"] = 事故库
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递, 夹具, 签名目录  # noqa: E402


def _尝试行(eid: str):
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT 尝试号,状态,响应码,说明,投递时 FROM 投递尝试 WHERE 事件号=? ORDER BY rowid",
            (eid,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 造事故() -> dict:
    seed.初始化库()

    # 1) 用「失败投递样例」夹具造一笔注定失败的事件（sync.fail）
    体 = 夹具.读取("失败投递样例")
    item = 体["事件"][0]
    ev = 入队.建事件(item)
    eid = ev["事件号"] if "事件号" in ev else ev["事件号"]

    # 2) 先强制失败一笔
    first = 投递.投递一次(eid, 强制失败=True)

    # 3) 立即「连点投递」五次（无任何等待 / 无退避窗）
    clicks = []
    for i in range(5):
        try:
            clicks.append(投递.投递一次(eid, 强制失败=True))
        except ValueError as e:
            clicks.append({"第几下": i + 2, "错误": str(e)})

    rows = _尝试行(eid)
    fails = [r for r in rows if r["状态"] == "失败"]
    intercepted = [c for c in clicks if isinstance(c, dict) and c.get("退避拦截")]
    report = {
        "数据目录": 事故库,
        "事件号": eid,
        "首笔": first,
        "连点返回": clicks,
        "流水尝试总数": len(rows),
        "其中失败笔数": len(fails),
        "连点被闸口拦截笔数": len(intercepted),
        "流水": rows,
        "事件现状": 签名目录.事件一条(eid)["状态"],
    }
    return report


if __name__ == "__main__":
    import json

    rep = 造事故()
    # 控制台只打摘要，完整流水仍以 JSON 形式落屏
    摘要 = {k: v for k, v in rep.items() if k != "流水"}
    print(json.dumps(摘要, ensure_ascii=False, indent=2))
    print("\n=== 结论 ===")
    if rep["其中失败笔数"] >= 2:
        print(f"❌ 事故复现：仍有 {rep['其中失败笔数']} 笔失败尝试被写出"
              f"（连打被逐笔落账），退避闸未生效。")
        raise SystemExit(1)
    print(f"✅ 闸口已生效：正式失败仅 {rep['其中失败笔数']} 笔，"
          f"{rep['连点被闸口拦截笔数']} 次连点被拦在退避窗外、未写出；"
          f"审计账册可查每一次窗内拦截。")
