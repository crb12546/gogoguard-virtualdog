"""假天大 GO2 模拟器 —— 扮演真狗，照 go2_http_server_backend_auto.py 的【后台对接行为】，
打我们的适配层 /api/v1/robot/*。联调前本地端到端演练 / 喂"一周真实例子"数据。

行为（与真狗一致，数据走 go2_protocol 共享构造器）：
  · 一趟 = 开机就绪(ready) → 沿点位巡检(patrolling，5s 一帧心跳，带四元数位姿、可带漂移) → 回待命(idle)。
    适配层据此【自动开/收趟次】；到点录一段 20s 视频上传；命令从心跳响应取并回执；GO2 不报电量。
  · --days N：回填 N 天历史，每天 9:00 一趟（过去日期），出"一周趋势"；不带 --days = 实时持续跑。

用法：
  .venv/bin/python go2_sim.py --robot-id go2-001 --token <令牌> --days 7 --material --incident-days 3,5
  .venv/bin/python go2_sim.py --robot-id go2-001 --token <令牌>            # 实时持续(纯色MP4)
"""
import argparse
import io
import random
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import imageio.v2 as imageio
from PIL import Image

from robot_sim import HTTP, get_plan, lib_image, lib_incident, BACKEND   # 复用 HTTP/取计划/真素材库
import go2_protocol as P

ADP = f"{BACKEND}/api/v1/robot"
INC_KINDS = ["person_down", "fire", "flooding"]


def _mp4(frames, fps=4) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        p = Path(tf.name)
    w = imageio.get_writer(str(p), fps=fps, codec="libx264", macro_block_size=None)
    for fr in frames:
        w.append_data(fr)
    w.close()
    d = p.read_bytes(); p.unlink(missing_ok=True)
    return d


def img_to_mp4(img_bytes, seconds=20, fps=4) -> bytes:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB"); im.thumbnail((640, 640))
    arr = np.array(im); h, w = arr.shape[:2]; arr = arr[: h // 2 * 2, : w // 2 * 2]
    return _mp4([arr] * max(2, int(seconds * fps)), fps)


def plain_mp4(seconds=20, fps=4, seed=0) -> bytes:
    return _mp4([np.full((120, 160, 3), ((i * 20 + seed) % 256, 80, 120), np.uint8) for i in range(max(2, int(seconds * fps)))], fps)


def heartbeat(rid, x, y, z, yaw, speed, status, route_file, when):
    try:
        r = HTTP.post(f"{ADP}/heartbeat", json=P.heartbeat_payload(rid, when, x, y, z, yaw, speed, status, route_file), timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"[go2-sim] 心跳失败: {e}", flush=True); return {}


def report_result(rid, cmd, when):
    try:
        HTTP.post(f"{ADP}/command/result", json=P.command_result(rid, when, cmd.get("id"), action=cmd.get("action") or cmd.get("type")), timeout=10)
    except Exception as e:
        print(f"[go2-sim] 回执失败: {e}", flush=True)


def upload_video(rid, mp4, when):
    try:
        r = HTTP.post(f"{ADP}/video/upload", files={"file": ("clip.mp4", mp4, "video/mp4")},
                      data=P.video_form(rid, when, file_name="clip.mp4", file_size=len(mp4)), timeout=120)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"[go2-sim] 传视频失败: {e}", flush=True); return {}


def _clip_for(material, reached_ck, incident, hazard=False):
    if material and incident:
        img = lib_incident(random.choice(INC_KINDS))
        return (img_to_mp4(img) if img else plain_mp4(), f"⚠突发")
    if material:
        img = lib_image(reached_ck, not hazard)               # hazard → 取该检查项的 fail 真图 → 真判定出常规隐患
        return (img_to_mp4(img) if img else plain_mp4(), "隐患(真图)" if hazard else "正常(真图)")
    return (plain_mp4(), "纯色")


def run_patrol(rid, way, base_dt, route_file, material, incident_point, live, step, hazard_points=()):
    """一趟巡检：ready→patrolling(沿点位走)→idle。base_dt=这趟起始时刻(回填用过去日期)。
    hazard_points 里的点位用 fail 真图 → 真判定出常规隐患(慢性);incident_point 注入一处突发。"""
    clock = base_dt
    heartbeat(rid, way[0][0], way[0][1], way[0][2], 0.0, 0.0, "ready", route_file, clock)
    drift = 0.02 if material else 0.0
    for x, y, z, yaw, reached in P.walk([(w[0], w[1], w[2], w[3]) for w in way], steps_per_seg=6, drift_per_step=drift):
        clock += timedelta(seconds=5)
        resp = heartbeat(rid, x, y, z, yaw, round(random.uniform(0.5, 0.9), 2), "patrolling", route_file, clock)
        for cmd in resp.get("commands", []):
            print(f"[go2-sim] ⬇ 命令 #{cmd.get('id')} {cmd.get('action') or cmd.get('type')} {cmd.get('params')}", flush=True)
            report_result(rid, cmd, clock)
        if reached:
            ck = next(w[4] for w in way if w[3] == reached)
            mp4, tag = _clip_for(material, ck, incident_point == reached, hazard=reached in hazard_points)
            r = upload_video(rid, mp4, clock)
            print(f"[go2-sim]   ⬆ {reached} 视频({tag})→ 就近 {r.get('matchedPoint')} · 突发 {len(r.get('incidents') or [])}", flush=True)
        if live:
            time.sleep(step)
    clock += timedelta(seconds=10)
    heartbeat(rid, way[0][0], way[0][1], way[0][2], 0.0, 0.0, "idle", route_file, clock)   # 回待命 → 收尾趟次
    return clock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot-id", default="go2-001")
    ap.add_argument("--token", required=True)
    ap.add_argument("--days", type=int, default=0, help="回填 N 天历史(每天 9:00 一趟);0=实时持续")
    ap.add_argument("--step", type=float, default=1.2, help="实时模式每帧间隔秒")
    ap.add_argument("--material", action="store_true", help="视频用素材库真图(含突发);默认纯色MP4")
    ap.add_argument("--incident-days", default="", help="第几天注入突发(1基,逗号分隔),如 3,5")
    ap.add_argument("--hazard-points", default="", help="慢性隐患点位(逗号分隔点位名):该点用 fail 真图 → 真判定出常规隐患")
    args = ap.parse_args()
    rid = args.robot_id

    plan = get_plan(args.token)
    pts = plan.get("points") or []
    if not pts:
        print("该项目还没点位，先去配置中心标几个点。"); return
    way = [(p["x"], p["y"], p.get("z", 0), p["name"], (p.get("checkItems") or ["现场是否符合规范"])[0]) for p in pts]
    route_file = (plan.get("routes") or [{}])[0].get("name") or "sim_route.csv"
    inc_days = {int(x) for x in args.incident_days.split(",") if x.strip().isdigit()}
    hazard_pts = {x.strip() for x in args.hazard_points.split(",") if x.strip()}
    print(f"[go2-sim] robotId={rid} · {len(way)} 点位 · {'回填 ' + str(args.days) + ' 天' if args.days else '实时'} · {'真图' if args.material else '纯色'}"
          + (f" · 慢性隐患@{','.join(hazard_pts)}" if hazard_pts else ""), flush=True)

    if args.days > 0:                                   # 回填一周：过去日期、每天一趟
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for d in range(args.days - 1, -1, -1):
            day_num = args.days - d
            base = today0 - timedelta(days=d) + timedelta(hours=9, minutes=random.randint(0, 20))
            inc_pt = random.choice([w[3] for w in way]) if day_num in inc_days else None
            run_patrol(rid, way, base, route_file, args.material, inc_pt, live=False, step=0, hazard_points=hazard_pts)
            print(f"[go2-sim] ✓ 第 {day_num} 天({base.strftime('%m-%d')})巡检完成{' · 注入突发@' + inc_pt if inc_pt else ''}", flush=True)
        print("[go2-sim] 一周回填完成。", flush=True)
    else:                                               # 实时持续
        while True:
            run_patrol(rid, way, datetime.now(), route_file, args.material, None, live=True, step=args.step, hazard_points=hazard_pts)
            for _ in range(random.randint(6, 12)):      # 待命一会儿(响应命令)
                resp = heartbeat(rid, way[0][0], way[0][1], way[0][2], 0, 0, "idle", route_file, datetime.now())
                for cmd in resp.get("commands", []):
                    report_result(rid, cmd, datetime.now())
                time.sleep(args.step * 3)


if __name__ == "__main__":
    main()
