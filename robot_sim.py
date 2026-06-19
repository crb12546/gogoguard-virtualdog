#!/usr/bin/env python3
"""真天大机器狗的「参考实现 / 替换点」—— 平台完全分不出真假。
只拿 {设备令牌 + 上报地址}，其余全走公开设备接口，一行平台内部代码都不碰：
  · 按令牌从 /devices/plan 拉自己的巡检计划（点位+三维坐标+检查项、路线）
  · 用百炼万相为每个检查项生成 ok/fail 示例图（图库一次生成、复用 → 平台按图缓存判定，省钱）
  · 沿路线走：心跳带位姿、到点 /captures/batch 批量推「带时间戳照片 + xyz」
  · 回填 N 天历史 + 实时 N 天
真狗就绪后照这套接口实现即可，平台、令牌、接口全不动。

  python robot_sim.py --token dev-xxx --backfill 2 --live --days 2
"""
import os
import re
import sys
import time
import math
import json
import random
import argparse
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# 加载 backend/.env（拿百炼 key 生成图库）；已存在的环境变量优先
_ENVF = Path(__file__).resolve().parent / ".env"
if _ENVF.exists():
    for _l in _ENVF.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
WANX_MODEL = os.getenv("WANX_MODEL", "wanx2.1-t2i-turbo")
WANX_VIDEO_MODEL = os.getenv("WANX_VIDEO_MODEL", "wanx2.1-t2v-turbo")
LIB_DIR = Path(__file__).resolve().parent / "sim_library"
VID_DIR = Path(__file__).resolve().parent / "sim_video_library"
LIB_DIR.mkdir(exist_ok=True)
VID_DIR.mkdir(exist_ok=True)
HTTP = httpx.Client(timeout=90, trust_env=False)   # 直连，绕开本机代理
BASE_FAIL = 0.11        # 每次抓拍不合规概率（造异常 churn；偏低=运行良好的站点，AI 仍逐张真判）
INCIDENT_PROB = 0.5     # 实时每趟巡检注入一个突发(人倒地/火/积水)的概率 —— 给"下限"演示真实告警

# 突发场景素材（与 incident_lab.py 同一套高召回已验证的提示词；t2i 对"人躺地上"易画错，故强调平躺俯视）
INCIDENT_SCENES = {
    "person_down": "监控俯拍画面：一名男子面朝上平躺、昏倒在办公楼大厅光洁瓷砖地面上，身体完全贴地水平伸展、四肢摊开一动不动，正上方俯视监控视角能清楚看到整个人躺在地面，写实纪实风格，光线明亮，无任何文字水印",
    "fire": "一张安防监控摄像头画面：室内墙角与垃圾桶正在起火，清晰可见橙红色明火与升腾的黑色浓烟，写实监控纪实风格，光线偏暗带火光，无任何文字与水印",
    "flooding": "一张安防监控摄像头画面：地下车库走廊地面大面积积水水浸，水面反光淹没地面并漫向远处，写实监控纪实风格，光线清晰，无任何文字与水印",
    "normal": "一张安防监控摄像头画面：空旷整洁的写字楼室内走廊，地面干净干燥、无任何杂物、无人员异常，一切正常，写实监控纪实风格，光线明亮，无任何文字与水印",
}


def H(token):
    return {"X-Device-Token": token}


def wait_backend():
    for _ in range(40):
        try:
            if HTTP.get(f"{BACKEND}/health", timeout=3).status_code == 200:
                print(f"[sim] backend up: {BACKEND}", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def get_plan(token):
    return HTTP.get(f"{BACKEND}/api/v1/devices/plan", headers=H(token)).json()


def hb(token, battery, status, speed, loc, task, position=None, pid=None, ts=None):
    body = {"battery": int(battery), "status": status, "speed": speed, "loc": loc, "task": task}
    if position:
        body["position"] = position
    if pid:
        body["patrol_id"] = pid
    if ts:
        body["ts"] = ts
    try:
        HTTP.post(f"{BACKEND}/api/v1/devices/heartbeat", json=body, headers=H(token))
    except Exception:
        pass


# ---------------- AI 图库：每个检查项 ok/fail 各一张（万相生成、缓存复用）----------------
def _wanx(prompt):
    sub = HTTP.post(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "X-DashScope-Async": "enable"},
        json={"model": WANX_MODEL, "input": {"prompt": prompt}, "parameters": {"n": 1, "size": "1024*1024"}},
    ).json()
    tid = sub["output"]["task_id"]
    for _ in range(40):
        time.sleep(2)
        r = HTTP.get(f"https://dashscope.aliyuncs.com/api/v1/tasks/{tid}",
                     headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"}).json()
        st = r["output"]["task_status"]
        if st == "SUCCEEDED":
            return HTTP.get(r["output"]["results"][0]["url"]).content
        if st in ("FAILED", "UNKNOWN"):
            break
    return None


# 检查项 → 精挑真实样例图(assets/inspections，狗自带)的精确对应：(ok 图, fail 图)。
# 优先于万相随机生成：贴检查项语义、可控、且 fail 图不含水/火元素(避免被突发检测误判)。
CURATED_SAMPLES = {
    "消防通道是否畅通":          ("corridor-clutter-pass.png", "corridor-clutter-fail.png"),
    "配电箱/电表箱门是否关闭":    ("electrical-box-door-closed.png", "electrical-box-door-open.png"),
    "危化品/易燃物堆放是否规范":  ("storage-mess-pass.png", "storage-mess-fail.png"),
    "地面是否有垃圾/杂物":        ("floor-garbage-pass.png", "floor-garbage-fail.png"),
    "标识牌/警示标志是否完好":    ("signage-ok.png", "signage-fallen.png"),
    "灭火器是否就位且在有效期":    ("fire-extinguisher-ok.png", "fire-extinguisher-issue.png"),
    "应急照明/疏散指示是否正常":  ("lobby-pass.jpg", "atrium-floor-fail.jpg"),
    "是否有违规停放车辆":        ("parking-entrance-pass.jpg", "west-gate-fail.jpg"),
    "护栏/围挡是否牢固规范":      ("barrier-aligned-pass.png", "barrier-misaligned-fail.png"),
}
_SAMP_DIR = Path(__file__).resolve().parent / "assets" / "inspections"
_INC_DIR = Path(__file__).resolve().parent / "assets" / "incidents"   # 自带突发图(人倒地/火/积水/normal)


def lib_image(check_name, ok):
    """取该检查项的 ok/fail 图。优先用精挑样例图(贴检查项、可控)；否则万相生成存盘；万相不可用退回样例。"""
    pair = CURATED_SAMPLES.get(check_name)
    if pair:
        sp = _SAMP_DIR / pair[0 if ok else 1]
        if sp.exists():
            return sp.read_bytes()
    key = hashlib.md5(f"{check_name}|{ok}".encode()).hexdigest()[:12]
    f = LIB_DIR / f"{key}.png"
    if f.exists():
        return f.read_bytes()
    prompt = ((f"一张安防巡检现场照片：{check_name}，状态完全正常、干净整洁、物品规范摆放、设施完好无损、明显符合安全规范、毫无隐患"
               if ok else
               f"一张安防巡检现场照片：{check_name} 明显不合规：存在突出且清晰可见的安全隐患（如杂物堆放、通道或器材被遮挡、门未关闭、设施损坏、地面脏污积水等）")
              + "。真实抓拍风格、光线清晰、写字楼/学校/园区等室内外安防场景，无任何文字与水印")
    img = None
    if DASHSCOPE_API_KEY:
        try:
            img = _wanx(prompt)
        except Exception:
            img = None
    if img is None:                                  # 万相不可用 → 退回自带真实样例图
        cand = (list(_SAMP_DIR.glob("*ok*")) if ok else list(_SAMP_DIR.glob("*mess*"))) or list(_SAMP_DIR.glob("*.png"))
        img = cand[0].read_bytes() if cand else b"\x89PNG\r\n\x1a\n"
    f.write_bytes(img)
    return img


def _wanx_video(prompt):
    """万相 文生视频(t2v)：异步 提交→轮询→下载。返回 MP4 字节；失败 None。"""
    sub = HTTP.post(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "X-DashScope-Async": "enable"},
        json={"model": WANX_VIDEO_MODEL, "input": {"prompt": prompt}, "parameters": {"size": "1280*720"}},
    ).json()
    tid = sub.get("output", {}).get("task_id")
    if not tid:
        return None
    for _ in range(60):
        time.sleep(5)
        r = HTTP.get(f"https://dashscope.aliyuncs.com/api/v1/tasks/{tid}",
                     headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"}).json()
        st = r.get("output", {}).get("task_status")
        if st == "SUCCEEDED":
            return HTTP.get(r["output"]["video_url"], timeout=120).content
        if st in ("FAILED", "UNKNOWN"):
            break
    return None


def lib_video(check_name, ok):
    """该检查项 ok/fail 视频段(MP4)。本地有就用；否则万相 t2v 生成存盘。失败 None（上层回退图片）。"""
    key = hashlib.md5(f"{check_name}|{ok}".encode()).hexdigest()[:12]
    f = VID_DIR / f"{key}.mp4"
    if f.exists():
        return f.read_bytes()
    if not DASHSCOPE_API_KEY:
        return None
    prompt = ((f"安防巡检监控视角：{check_name}，状态完全正常、干净整洁、物品规范、设施完好、无任何隐患，镜头缓慢平移"
               if ok else
               f"安防巡检监控视角：{check_name} 明显不合规：存在突出可见的安全隐患（杂物堆放/通道遮挡/门未关闭/设施损坏/地面脏污积水等），镜头缓慢平移扫过隐患")
              + "。真实监控纪实风格、光线清晰、写字楼/学校/园区室内外安防场景、无任何文字与水印")
    try:
        vid = _wanx_video(prompt)
    except Exception:
        vid = None
    if vid:
        f.write_bytes(vid)
    return vid


def lib_incident(kind):
    """突发场景素材(人倒地/火/积水/normal)。优先用自带突发图(assets/incidents，不依赖 key)；
    否则本地缓存；否则万相生成存盘。失败 None。"""
    bundled = _INC_DIR / f"incident_{kind}.png"
    if bundled.exists():
        return bundled.read_bytes()
    f = LIB_DIR / f"incident_{kind}.png"
    if f.exists():
        return f.read_bytes()
    if not DASHSCOPE_API_KEY:
        return None
    img = _wanx(INCIDENT_SCENES[kind])
    if img:
        f.write_bytes(img)
    return img


def stream_frame(token, pid, point_label, kind, ts):
    """推一帧"连续巡检画面"到 /captures/stream（每分钟1+张的沿路捕获，只跑突发检测）。
    kind=None → 正常途中帧；否则推对应突发素材 → 平台高召回检测命中 → 建紧急告警并升级。"""
    img = lib_incident(kind or "normal")
    if not img:
        return
    name = f"incident_{kind}.png" if kind else "path_normal.png"
    try:
        HTTP.post(f"{BACKEND}/api/v1/captures/stream", headers=H(token),
                  files=[("file", (name, img, "image/png"))],
                  data={"point": point_label, "patrol_id": pid, "captured_at": ts.isoformat()})
    except Exception as e:
        print(f"[sim] stream fail: {e}", flush=True)


def goto_point(token, plan, point_name, ts):
    """执行"指哪走哪"：奔赴该点 → 心跳更新位姿 → 到点拍一张 → 播报该点语音(stub)。真导航由天大。"""
    p = {x["name"]: x for x in plan["points"]}.get(point_name)
    if not p:
        return
    xyz = (p["x"], p["y"], p.get("z", 0))
    hb(token, 80, "巡检中", 0.8, f"奉命前往 {point_name}", "按需调度", {"x": xyz[0], "y": xyz[1], "z": xyz[2], "yaw": 0}, None, ts.isoformat())
    check = (p.get("checkItems") or ["现场是否符合规范"])[0]
    img = lib_image(check, True)
    try:
        HTTP.post(f"{BACKEND}/api/v1/captures/batch", headers=H(token),
                  files=[("files", (f"{point_name}_goto.png", img, "image/png"))],
                  data={"meta": json.dumps([{"point": point_name, "ts": ts.isoformat(), "x": xyz[0], "y": xyz[1], "z": xyz[2], "yaw": 0}]), "patrol_id": 0})
    except Exception as e:
        print(f"[sim] goto capture fail: {e}", flush=True)
    if p.get("voice"):
        print(f"[sim] 🔊 到点播报：{p['voice']}", flush=True)
    print(f"[sim] ✓ 已执行调度：前往 {point_name}", flush=True)


def poll_commands(token, plan, ts):
    """轮询下行命令（指哪走哪 / 语音播报），执行后回报完成。真狗按 type 调用导航/TTS。"""
    did = plan.get("device_id", "dog")
    try:
        cmds = HTTP.get(f"{BACKEND}/api/v1/devices/{did}/commands", headers=H(token), timeout=10).json().get("commands", [])
    except Exception:
        return
    for c in cmds:
        if c["type"] == "goto":
            goto_point(token, plan, c["payload"].get("point", ""), ts)
        elif c["type"] == "speak":
            print(f"[sim] 🔊 播报指令：{c['payload'].get('text', '')}", flush=True)
        try:
            HTTP.post(f"{BACKEND}/api/v1/devices/{did}/commands/{c['id']}/done", headers=H(token), json={"note": "sim executed"}, timeout=10)
        except Exception:
            pass


def build_library(plan):
    checks = sorted({c for p in plan["points"] for c in (p.get("checkItems") or [])})
    print(f"[sim] 生成素材库：{len(checks)} 检查项 × ok/fail（图+视频，万相一次性，视频较慢请耐心）…", flush=True)
    for c in checks:
        lib_image(c, True); lib_image(c, False)
        lib_video(c, True); lib_video(c, False)
    print("[sim] ✓ 素材库就绪（图+视频）", flush=True)


# ---------------- 巡检 ----------------
def lerp(a, b, t):
    return a + (b - a) * t


def run_patrol(token, plan, route, start_dt, live=False, battery=80):
    by_name = {p["name"]: p for p in plan["points"]}
    seq = [by_name[n] for n in (route.get("points") or []) if n in by_name] or plan["points"]
    if not seq:
        return battery
    try:
        pid = HTTP.post(f"{BACKEND}/api/v1/patrols/start", headers=H(token),
                        json={"route": route["name"], "pts": len(seq), "name": route["name"],
                              "started_at": start_dt.isoformat()}).json()["patrol_id"]
    except Exception as e:
        print(f"[sim] start fail: {e}", flush=True)
        return battery
    t, prev, fails, poses = start_dt, None, 0, []
    inc_kind = random.choice(["person_down", "fire", "flooding"]) if (live and random.random() < INCIDENT_PROB) else None
    inc_at = random.randint(0, len(seq) - 1) if inc_kind else -1
    for idx, p in enumerate(seq):
        xyz = (p["x"], p["y"], p.get("z", 0))
        p0 = prev or xyz
        seg = 8 if live else 4
        for i in range(1, seg + 1):
            fr = i / seg
            cx, cy, cz = round(lerp(p0[0], xyz[0], fr), 2), round(lerp(p0[1], xyz[1], fr), 2), round(lerp(p0[2], xyz[2], fr), 2)
            yaw = round(math.atan2(xyz[1] - p0[1], xyz[0] - p0[0]), 3)
            # 回填用真实节奏的时间戳（每段 1-2 分钟），让历史巡检时长贴近真机(~40-55分钟)；实时则按真节奏走
            t += timedelta(seconds=(2 if live else random.randint(70, 110)))
            poses.append({"ts": t.isoformat(), "x": cx, "y": cy, "z": cz, "yaw": yaw})
            if live:
                battery = max(5, battery - 0.06)
                hb(token, battery, "巡检中", round(random.uniform(0.6, 1.1), 1), f"前往 {p['name']}",
                   f"{route['name']} 进行中", {"x": cx, "y": cy, "z": cz, "yaw": yaw}, pid, t.isoformat())
                time.sleep(1.2)
        t += timedelta(seconds=(2 if live else random.randint(40, 80)))    # 到点停留观察
        if live:                                          # 连续巡检：每分钟1+张沿路捕获 + 偶发突发（只跑突发检测）
            k = inc_kind if idx == inc_at else None
            stream_frame(token, pid, (p["name"] if k else f"{p['name']}途中"), k, t)
            if k:
                print(f"[sim] ⚠ 注入突发场景：{k} @ {p['name']}", flush=True)
        if live and p.get("voice"):                       # 到点语音播报(问候/警告)，真 TTS 由天大播放
            print(f"[sim] 🔊 到点播报：{p['voice']}", flush=True)
        # 到点：批量推一批（十几张证据帧；平台只判代表图，同图缓存）
        check = (p.get("checkItems") or ["现场是否符合规范"])[0]
        fail = random.random() < BASE_FAIL
        fails += 1 if fail else 0
        vid = lib_video(check, not fail)
        if vid:                                  # 推视频段：平台存 MP4 + 抽帧判定，大屏播真实视频
            files = [("files", (f"{p['name']}.mp4", vid, "video/mp4"))]
            meta = [{"point": p["name"], "ts": t.isoformat(), "x": xyz[0], "y": xyz[1], "z": xyz[2], "yaw": 0}]
        else:                                    # 视频不可用 → 回退图片帧
            img = lib_image(check, not fail)
            n = random.randint(8, 16)
            files = [("files", (f"{p['name']}_{k}.png", img, "image/png")) for k in range(n)]
            meta = [{"point": p["name"], "ts": t.isoformat(), "x": xyz[0], "y": xyz[1], "z": xyz[2], "yaw": 0} for _ in range(n)]
        try:
            HTTP.post(f"{BACKEND}/api/v1/captures/batch", headers=H(token), files=files,
                      data={"meta": json.dumps(meta), "patrol_id": pid})
        except Exception as e:
            print(f"[sim] batch fail {p['name']}: {e}", flush=True)
        prev = xyz
    if not live and poses:
        try:
            HTTP.post(f"{BACKEND}/api/v1/track", headers=H(token), json={"patrol_id": pid, "poses": poses})
        except Exception:
            pass
    try:
        HTTP.post(f"{BACKEND}/api/v1/patrols/{pid}/complete", headers=H(token), json={"ended_at": t.isoformat()})
    except Exception:
        pass
    print(f"[sim] 巡检「{route['name']}」{start_dt.strftime('%m-%d %H:%M')} 完成：{len(seq)}点 / {fails}异常", flush=True)
    return battery


# ---------------- 回填 / 实时 ----------------
_WD = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6}


def _times(route, day0, now):
    """排班灵活化：schedule 支持「可选周几 + 多个时间」。
    例：'每天 09:00 / 14:00 / 20:00'、'周一,周三,周五 08:30 / 18:00'。无周几标记=每天。"""
    sched = route.get("schedule") or "09:00 / 14:00 / 20:00"
    days = {v for k, v in _WD.items() if k in sched}
    if days and day0.weekday() not in days:                # 指定了周几且今天不在其中 → 今天不巡
        return []
    out = []
    for hh, mm in re.findall(r"(\d{1,2}):(\d{2})", sched):
        s = day0.replace(hour=int(hh), minute=int(mm), second=random.randint(0, 40))
        if s <= now:
            out.append(s)
    return out


def _routes(plan):
    return plan.get("routes") or [{"name": "日常巡检", "points": [p["name"] for p in plan["points"]],
                                   "schedule": "09:00 / 14:00 / 20:00"}]


def backfill(token, plan, days):
    now = datetime.now()
    print(f"[sim] 回填 {days} 天历史 …", flush=True)
    for day in range(days, -1, -1):
        d0 = (now - timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for route in _routes(plan):
            for start in _times(route, d0, now):
                run_patrol(token, plan, route, start, live=False)
    print("[sim] ✓ 回填完成", flush=True)


def live_loop(token, plan, days):
    end = datetime.now() + timedelta(days=days)
    route = _routes(plan)[0]
    battery = random.randint(70, 95)
    print(f"[sim] 实时模式，持续到 {end.strftime('%m-%d %H:%M')} …", flush=True)
    while datetime.now() < end:
        battery = run_patrol(token, plan, route, datetime.now(), live=True, battery=battery)
        for _ in range(random.randint(14, 26)):
            battery = min(100, battery + 1.3)
            hb(token, battery, "充电中", 0, "充电桩", "—")
            time.sleep(2)
        for _ in range(random.randint(50, 80)):        # 待命 ~8-13 分钟（真实节奏，不连轴转）
            hb(token, battery, "在线待命", 0, "充电桩待命区", "—")
            poll_commands(token, plan, datetime.now())   # 待命期间响应"指哪走哪 / 语音播报"下行命令
            time.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="设备令牌（注册狗时拿到的）")
    ap.add_argument("--backfill", type=int, default=0, help="回填 N 天历史")
    ap.add_argument("--live", action="store_true", help="进入实时模式")
    ap.add_argument("--days", type=int, default=2, help="实时持续天数")
    ap.add_argument("--no-ai-images", action="store_true", help="不调万相，直接用样例图（快/省钱，调试用）")
    args = ap.parse_args()
    if args.no_ai_images:
        globals()["DASHSCOPE_API_KEY"] = ""
    if not wait_backend():
        print("[sim] backend 不可达", flush=True)
        return
    plan = get_plan(args.token)
    if not plan.get("points"):
        print("[sim] 该狗所属项目还没配点位（先在配置中心 / PCD 上标点）", flush=True)
        return
    print(f"[sim] 计划：{len(plan['points'])} 点位 / {len(plan.get('routes', []))} 路线", flush=True)
    build_library(plan)
    if not args.backfill and not args.live:
        args.backfill, args.live = 2, True
    if args.backfill:
        backfill(args.token, plan, args.backfill)
    if args.live:
        live_loop(args.token, plan, args.days)


if __name__ == "__main__":
    main()
