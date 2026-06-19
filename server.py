#!/usr/bin/env python3
"""虚拟狗 · 控制服务 —— FastAPI + 单页控制台。
后台线程持有狗状态(位姿/趟次/路线/速度/隐患)，按 GO2 协议【实时】对平台上报；
控制台(/)用按钮驱动：连接平台、起步/暂停/停、注入突发、设隐患点、调速度、看上报日志。
平台分不出它和真狗。CLI(go2_sim.py)是另一个入口，互不影响。

  uvicorn server:app --host 0.0.0.0 --port 8088     # 或 python server.py
心跳 status 发英文串(patrolling/idle)，平台适配层 STATUS_MAP 再映射。
"""
import os
import base64
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import go2_protocol as Pr
from robot_sim import lib_image, lib_incident
from go2_sim import img_to_mp4, plain_mp4

HTTP = httpx.Client(timeout=120, trust_env=False)
WEB = Path(__file__).resolve().parent / "web"


class Dog:
    """一只虚拟狗的状态机 + 后台上报线程。线程内读状态、网络在锁外。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.backend = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
        self.robot_id = os.getenv("GO2_DEMO_RID", "go2-tju-01")
        self.token = os.getenv("GO2_DEMO_TOKEN", "")
        self.connected = False
        self.route_file = "go2_demo.csv"
        self.way = []                       # [(x,y,z,name,check)]
        self.pose = (0.0, 0.0, 0.0, 0.0)    # x,y,z,yaw
        self.status = "在线待命"             # 中文展示态
        self.cur_point = "—"
        self.speed = 0.7
        self.material = True
        self.hazards = set()                # 这些点位到点用 fail 图 → 平台真判不合规
        self._pending_incident = None       # 下一个到点注入的突发(也可立即注入)
        self.mode = "idle"                  # idle / patrolling / paused
        self._walk = None
        self.log = []

    # ---------- 上报原语 ----------
    def addlog(self, msg):
        self.log.insert(0, f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
        del self.log[40:]

    def _hb(self, x, y, z, yaw, speed, wire_status):
        try:
            r = HTTP.post(f"{self.backend}/api/v1/robot/heartbeat",
                          json=Pr.heartbeat_payload(self.robot_id, datetime.now(), x, y, z, yaw, speed, wire_status, self.route_file))
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            self.addlog(f"!! 心跳失败 {e}")
            return {}

    def _video(self, mp4, fn):
        try:
            r = HTTP.post(f"{self.backend}/api/v1/robot/video/upload",
                          files={"file": (fn, mp4, "video/mp4")},
                          data=Pr.video_form(self.robot_id, datetime.now(), file_name=fn, file_size=len(mp4)))
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            self.addlog(f"!! 传视频失败 {e}")
            return {}

    def _near_name(self, x, y):
        best, bestd = "巡逻途中", 4.0
        for w in self.way:
            d = ((w[0] - x) ** 2 + (w[1] - y) ** 2) ** 0.5
            if d <= bestd:
                best, bestd = w[3], d
        return best

    # ---------- 控制动作(被 API 调) ----------
    def connect(self, backend, robot_id, token):
        backend = backend.rstrip("/")
        r = HTTP.get(f"{backend}/api/v1/devices/plan", headers={"X-Device-Token": token}, timeout=15)
        if r.status_code != 200:
            raise ValueError(f"拉计划失败 {r.status_code}（令牌/地址不对？）")
        plan = r.json()
        pts = plan.get("points") or []
        if not pts:
            raise ValueError("该项目没有点位，先去平台配置中心标点")
        with self.lock:
            self.backend, self.robot_id, self.token = backend, robot_id, token
            self.way = [(p["x"], p["y"], p.get("z", 0), p["name"], (p.get("checkItems") or ["现场是否符合规范"])[0]) for p in pts]
            self.route_file = (plan.get("routes") or [{}])[0].get("name") or "go2_demo.csv"
            self.pose = (self.way[0][0], self.way[0][1], 0.0, 0.0)
            self.connected = True
            self.status = "在线待命"
        self.addlog(f"已连接 {backend} · {len(pts)} 点位")

    def start_patrol(self):
        with self.lock:
            if not self.connected:
                return
            self._walk = Pr.walk([(w[0], w[1], w[2], w[3]) for w in self.way],
                                 steps_per_seg=5, drift_per_step=0.02 if self.material else 0.0)
            self.mode = "patrolling"
            self.status = "巡检中"
        self.addlog("▶ 开始巡检")

    def pause(self):
        with self.lock:
            if self.mode == "patrolling":
                self.mode = "paused"; self.status = "暂停"; self.addlog("⏸ 暂停")
            elif self.mode == "paused":
                self.mode = "patrolling"; self.status = "巡检中"; self.addlog("▶ 继续")

    def stop(self):
        with self.lock:
            self.mode = "idle"; self.status = "在线待命"; self._walk = None
            x0, y0 = (self.way[0][0], self.way[0][1]) if self.way else (0, 0)
        self._hb(x0, y0, 0, 0, 0, "idle")     # 回待命 → 平台收尾趟次
        self.addlog("⏹ 回待命")

    def inject_incident(self, kind):
        """立刻在当前位置传一段突发视频 → 平台秒弹告警(演示临门一脚)。"""
        if not self.connected:
            return
        x, y, z, yaw = self.pose
        wire = "patrolling" if self.mode in ("patrolling", "paused") else "idle"
        self._hb(x, y, z, yaw, self.speed, wire)      # 先给一帧当前位姿，突发好定位
        img = lib_incident(kind)
        mp4 = img_to_mp4(img) if img else plain_mp4()
        r = self._video(mp4, f"{kind}.mp4")
        n = len(r.get("incidents") or [])
        self.addlog(f"🆘 注入{kind} → 平台告警 {'已弹✓' if n else '未命中'}")

    def toggle_hazard(self, point, on):
        with self.lock:
            if on:
                self.hazards.add(point)
            else:
                self.hazards.discard(point)
        self.addlog(f"{'⚠ 设隐患 ' if on else '✓ 清隐患 '}{point}")

    def set_config(self, speed=None, material=None):
        with self.lock:
            if speed is not None:
                self.speed = max(0.2, min(2.0, float(speed)))
            if material is not None:
                self.material = bool(material)
        self.addlog(f"速度 {self.speed} · 素材 {'真图' if self.material else '纯色'}")

    def _clip_at(self, name):
        ck = next((w[4] for w in self.way if w[3] == name), "现场是否符合规范")
        if self._pending_incident:
            kind = self._pending_incident
            self._pending_incident = None
            img = lib_incident(kind)
            return (img_to_mp4(img) if img else plain_mp4()), f"突发:{kind}"
        bad = name in self.hazards
        return img_to_mp4(lib_image(ck, not bad)), ("隐患" if bad else "正常")

    # ---------- 后台循环 ----------
    def run(self):
        last_idle = 0.0
        while True:
            try:
                with self.lock:
                    mode, walk = self.mode, self._walk
                    px, py, pz, pyaw = self.pose
                    way, connected, speed = self.way, self.connected, self.speed
                if mode == "patrolling" and walk is not None:
                    try:
                        x, y, z, yaw, reached = next(walk)
                    except StopIteration:
                        with self.lock:
                            self.mode = "idle"; self.status = "在线待命"; self._walk = None
                        self._hb(way[0][0], way[0][1], 0, 0, 0, "idle")
                        self.addlog("✓ 一趟巡检完成 · 回待命")
                        continue
                    with self.lock:                       # 步进后若已被 stop/重开则丢弃这步
                        if self.mode != "patrolling" or self._walk is not walk:
                            continue
                        self.pose = (x, y, z, yaw)
                        self.cur_point = self._near_name(x, y)
                    self._hb(x, y, z, yaw, speed, "patrolling")
                    if reached:
                        mp4, tag = self._clip_at(reached)
                        r = self._video(mp4, f"{reached}.mp4")
                        inc = len(r.get("incidents") or [])
                        self.addlog(f"⬆ {reached} 视频({tag}) → 就近 {r.get('matchedPoint')}{' · 突发✓' if inc else ''}")
                    time.sleep(max(0.4, 1.2 / max(0.2, speed)))
                elif mode == "paused":
                    self._hb(px, py, pz, pyaw, 0, "patrolling")   # 暂停=原地保活，保持趟次开着
                    time.sleep(2.5)
                else:                                             # idle：每 5s 一帧保活 + 拉命令
                    if connected and time.time() - last_idle > 5:
                        x0, y0 = (way[0][0], way[0][1]) if way else (0, 0)
                        self._hb(x0, y0, 0, 0, 0, "idle")
                        last_idle = time.time()
                    time.sleep(1.0)
            except Exception as e:
                self.addlog(f"!! 循环异常 {e}")
                time.sleep(2)

    def plan(self):
        with self.lock:
            return {"connected": self.connected, "robotId": self.robot_id, "routeFile": self.route_file,
                    "points": [{"name": w[3], "x": w[0], "y": w[1], "check": w[4]} for w in self.way]}

    def state(self):
        with self.lock:
            return {
                "connected": self.connected, "backend": self.backend, "robotId": self.robot_id,
                "status": self.status, "mode": self.mode, "pose": {"x": round(self.pose[0], 1), "y": round(self.pose[1], 1)},
                "curPoint": self.cur_point, "speed": self.speed, "material": self.material,
                "points": [w[3] for w in self.way], "hazards": sorted(self.hazards),
                "routeFile": self.route_file, "log": self.log,
            }


dog = Dog()
app = FastAPI(title="虚拟狗控制台")
app.mount("/lib", StaticFiles(directory=str(WEB / "lib")), name="lib")    # three.module.js 等静态


WORLD_FILE = Path(__file__).resolve().parent / "world.json"


@app.on_event("startup")
def _start():
    if os.getenv("DOG_AUTO") == "1":          # 无脑自走模式(用库图);默认关，由浏览器世界驱动
        threading.Thread(target=dog.run, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    for name in ("world.html", "index.html"):
        f = WEB / name
        if f.exists():
            return f.read_text(encoding="utf-8")
    return "<h1>缺 web/world.html</h1>"


@app.get("/simple", response_class=HTMLResponse)
def simple():
    f = WEB / "index.html"
    return f.read_text(encoding="utf-8") if f.exists() else "<h1>缺 web/index.html</h1>"


@app.get("/api/state")
def api_state():
    return dog.state()


@app.post("/api/connect")
def api_connect(body: dict = Body(...)):
    try:
        dog.connect(body.get("backend") or dog.backend, body.get("robotId") or dog.robot_id, body.get("token") or dog.token)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/patrol/{action}")
def api_patrol(action: str):
    {"start": dog.start_patrol, "pause": dog.pause, "stop": dog.stop}.get(action, lambda: None)()
    return {"ok": True}


@app.post("/api/incident")
def api_incident(body: dict = Body(...)):
    dog.inject_incident(body.get("kind", "person_down"))
    return {"ok": True}


@app.post("/api/hazard")
def api_hazard(body: dict = Body(...)):
    dog.toggle_hazard(body.get("point"), bool(body.get("on")))
    return {"ok": True}


@app.post("/api/config")
def api_config(body: dict = Body(...)):
    dog.set_config(speed=body.get("speed"), material=body.get("material"))
    return {"ok": True}


# ============ 浏览器虚拟世界驱动:狗服务当"平台桥" ============
@app.get("/api/plan")
def api_plan():
    return dog.plan()


@app.post("/api/heartbeat")
def api_heartbeat(body: dict = Body(...)):
    """浏览器世界:把狗当前位姿/状态转发成平台心跳(英文 status)。"""
    if not dog.connected:
        return {"ok": False, "error": "未连接"}
    resp = dog._hb(body.get("x", 0), body.get("y", 0), body.get("z", 0), body.get("yaw", 0),
                   body.get("speed", 0.6), body.get("status", "patrolling"))
    return {"ok": True, "commands": resp.get("commands", [])}


@app.post("/api/snap")
def api_snap(body: dict = Body(...)):
    """浏览器渲染的狗相机帧(png base64)→ 包成 mp4 → 经适配层传平台 → 回判定结果。"""
    if not dog.connected:
        return JSONResponse({"ok": False, "error": "未连接"}, status_code=400)
    try:
        png = base64.b64decode((body.get("png") or "").split(",")[-1])
    except Exception:
        return JSONResponse({"ok": False, "error": "png 解码失败"}, status_code=400)
    point = body.get("point") or "巡逻途中"
    dog._hb(body.get("x", 0), body.get("y", 0), body.get("z", 0), body.get("yaw", 0), body.get("speed", 0.6), "patrolling")
    mp4 = img_to_mp4(png)
    r = dog._video(mp4, f"{point}.mp4")
    inc = [h.get("label") for h in (r.get("incidents") or [])]
    matched = r.get("matchedPoint")
    dog.addlog(f"⬆ {point} 相机帧 → 就近 {matched}{' · 突发✓ ' + ','.join(inc) if inc else ''}")
    return {"ok": True, "matchedPoint": matched, "incidents": inc}


@app.get("/api/world")
def api_world_get():
    if WORLD_FILE.exists():
        try:
            return json.loads(WORLD_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"props": []}


@app.post("/api/world")
def api_world_set(body: dict = Body(...)):
    WORLD_FILE.write_text(json.dumps({"props": body.get("props", [])}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8088")))
