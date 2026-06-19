"""逐日可控地喂"一周真实例子"——打真适配层(/api/v1/robot/*) + 真 qwen 判定 + 精挑样例图。
叙事：配电间门问题持续数日后整改；外围隐患出现又消除；周中一次真突发(人倒地)；合规率有真趋势。
只经 ingest，零手工造异常。"""
import os, sys, time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 狗仓根: go2_sim/robot_sim/go2_protocol
import httpx
import go2_protocol as Pr
from robot_sim import get_plan, lib_image, lib_incident
from go2_sim import img_to_mp4, plain_mp4

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")
ADP = f"{BACKEND}/api/v1/robot"
RID = os.getenv("GO2_DEMO_RID", "go2-tju-01")
TOKEN = os.getenv("GO2_DEMO_TOKEN", "")          # ① 01_build_project.py 打印的 DEVICE_TOKEN，经环境变量传入
if not TOKEN:
    sys.exit("先 export GO2_DEMO_TOKEN=<01_build_project.py 打印的 DEVICE_TOKEN>")
H = httpx.Client(timeout=120, trust_env=False)

plan = get_plan(TOKEN)
pts = plan["points"]
way = [(p["x"], p["y"], p.get("z", 0), p["name"], (p.get("checkItems") or ["现场是否符合规范"])[0]) for p in pts]
route_file = (plan.get("routes") or [{}])[0].get("name") or "go2_route_001.csv"
print(f"计划 {len(way)} 点位：{[w[3] for w in way]}")

# 一周叙事：每天 = (慢性隐患点集合, (突发点, 突发类型) or None)
today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
WEEK = [
    (6, {"综合楼配电间"},                          None),                         # D1 87.5%
    (5, {"综合楼配电间", "食堂后厨外"},              None),                         # D2 75%
    (4, {"综合楼配电间", "食堂后厨外", "教学楼消防通道"}, None),                       # D3 62.5% 最差
    (3, {"综合楼配电间"},                          ("教学楼A座大厅", "person_down")),# D4 87.5% + 真突发
    (2, {"综合楼配电间"},                          None),                         # D5 87.5%
    (1, set(),                                    None),                         # D6 100% 配电间整改
    (0, set(),                                    None),                         # D7 100% 保持
]


def hb(when, x, y, z, yaw, speed, status):
    r = H.post(f"{ADP}/heartbeat", json=Pr.heartbeat_payload(RID, when, x, y, z, yaw, speed, status, route_file))
    return r.json() if r.status_code == 200 else {}


def upload(when, mp4):
    r = H.post(f"{ADP}/video/upload", files={"file": ("clip.mp4", mp4, "video/mp4")},
               data=Pr.video_form(RID, when, file_name="clip.mp4", file_size=len(mp4)))
    return r.json() if r.status_code == 200 else {}


for d, hazards, incident in WEEK:
    base = today0 - timedelta(days=d) + timedelta(hours=9, minutes=8 + d)
    daynum = 7 - d
    clock = base
    hb(clock, way[0][0], way[0][1], 0, 0, 0, "ready")
    n_ok = n_bad = n_inc = 0
    for x, y, z, yaw, reached in Pr.walk([(w[0], w[1], w[2], w[3]) for w in way], steps_per_seg=5):
        clock += timedelta(seconds=5)
        hb(clock, x, y, z, yaw, 0.7, "patrolling")
        if not reached:
            continue
        ck = next(w[4] for w in way if w[3] == reached)
        if incident and incident[0] == reached:
            img = lib_incident(incident[1])
            mp4 = img_to_mp4(img) if img else plain_mp4()
        else:
            bad = reached in hazards
            mp4 = img_to_mp4(lib_image(ck, not bad))
        r = upload(clock, mp4)
        inc = len(r.get("incidents") or [])
        n_inc += inc
        print(f"   D{daynum} {reached:8s} {'隐患' if reached in hazards else ('突发' if inc else '正常')} 就近={r.get('matchedPoint')} 突发={inc}")
    clock += timedelta(seconds=10)
    hb(clock, way[0][0], way[0][1], 0, 0, 0, "idle")
    print(f"✓ 第{daynum}天 ({base.strftime('%m-%d')}) 完成 · 慢性隐患 {len(hazards)} · 突发 {n_inc}")

print("一周喂数据完成。")
