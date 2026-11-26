"""hook-13 重试退避闸（已接线，不再是空桩）。

骨签：hook-13-重试退避闸。

工程形态抉择（整仓只兑现一边）：
    路径甲＝审计账册钉退避窗指纹，并拒窗内回刷。
    实现见 app/services/退避闸.py、app/services/投递.py 与
    app/services/互证.py 的「投递流水对账」；落库为 退避账册 表。

明确不兑现路径乙（双轨分流：窗内请求进只读归档、现行流水只读窗外可投集、
两套读引用）：app/modules/双写归档轨 仍为空桩，仓内没有第二套投递读引用。
"""

NAME = "重试退避"
BONE = "hook-13-重试退避闸"
ENGINEERING = "路径甲·审计账册钉退避窗指纹·拒窗内回刷"
WIRED_TO = [
    "app/services/退避闸.py",
    "app/services/投递.py",
    "app/services/重试.py",
    "app/services/互证.py",
]
