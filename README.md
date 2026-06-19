# gogoguard-virtualdog · 虚拟狗

一只**独立的虚拟巡检狗**——按天大宇树 GO2 的后台对接协议，对任意 GoGoGuard 平台上报数据。
平台分不出它和真狗（同一套 `/api/v1/robot/*` 接口、同一份数据形状）。两个用途：

1. **联调/自测前的主力**：真狗接入前，先用它把"建项目→标点→注册狗→喂数据→看大屏/异常/报告"整条链路跑通。
2. **演示用的"活体狗"**：可长期指向 gogoguard.cn 的演示项目，在大屏上活着走、注入突发，做销售演示。

> 它是平台的**客户端**，不含任何平台代码。与平台唯一的约定是数据契约 `go2_protocol.py`（平台是唯一真源，这里 vendor 一份，`tests/test_contract_drift.py` 防漂）。

## 结构
```
go2_sim.py            主体：照 GO2 协议打适配层(心跳/视频/命令回执)；--days 回填 / 实时 / --material 真图 / 突发
robot_sim.py          走路、拉计划、图库(精挑样例图 + 万相生成)
go2_protocol.py       数据契约(vendored 自平台；勿单独改，改了两边同步)
incident_lab.py       突发素材库 + 经平台端到端验召回
scenarios/week.py     逐日叙事："一周完整示例"(合规率趋势 + 一次真突发)
assets/inspections/   自带的精挑现场样例图(ok/fail)
tests/                漂移哨兵
```

## 跑起来
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 填 BACKEND_URL；要真图再填 DASHSCOPE_API_KEY(可空)
# 平台侧先建好项目+点位+注册狗(robotId)，拿到设备令牌
.venv/bin/python go2_sim.py --robot-id <robotId> --token <设备令牌> --days 7 --material --incident-days 4
```

## 跑"一周完整示例"
```bash
export GO2_DEMO_TOKEN=<设备令牌> GO2_DEMO_RID=<robotId> BACKEND_URL=http://localhost:8000
.venv/bin/python scenarios/week.py
```

## 防漂
```bash
.venv/bin/python -m pytest tests/ -q     # 校验 vendored 契约与平台逐字一致
```
契约（`go2_protocol.py`）是平台的。这里只放副本；平台改了契约，把 `go2_protocol.py` 同步过来即可，哨兵会盯着。
