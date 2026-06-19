"""天大 GO2 协议 · 数据构造（纯函数，单一真源）。

模拟器(go2_sim.py，HTTP 活工具) 和 场景测试(pytest，TestClient 隔离) 都用这里的构造器，
保证「测试数据 = 真狗 go2_http_server_backend_auto.py 实际发的数据」逐字一致。
"""
import json
import math
from datetime import datetime


def fmt(dt: datetime) -> str:
    """真狗 now_human() 的格式：本地时间字符串、无时区。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def quat_from_yaw(yaw: float) -> dict:
    """只绕竖直轴转 yaw → 四元数（与真狗 /Odometry orientation 同形）。"""
    return {"x": 0.0, "y": 0.0, "z": round(math.sin(yaw / 2), 5), "w": round(math.cos(yaw / 2), 5)}


def heartbeat_payload(robot_id: str, when: datetime, x, y, z, yaw, speed, status, route_file) -> dict:
    """一帧 GO2 心跳体（与真狗 build_status_payload 的关键字段一致）。GO2 不报电量 → 不带 battery。"""
    return {
        "robotId": robot_id, "time": fmt(when), "timestamp": int(when.timestamp()), "status": status,
        "motion": {"position": {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)},
                   "orientation": quat_from_yaw(yaw), "yaw_rad": round(yaw, 3),
                   "twist": {"linear": {"x": speed, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}},
        "patrol": {"running": status == "patrolling", "route_file": route_file},
    }


def video_form(robot_id: str, when: datetime, seg: int = 20, file_name: str = "clip.mp4", file_size: int = 0) -> dict:
    """GO2 视频段上传的表单字段（multipart 里 file 之外的部分）。视频不带坐标/精确帧时间。"""
    return {"robotId": robot_id, "time": fmt(when), "fileName": file_name, "fileSize": str(file_size),
            "meta": json.dumps({"segmentSeconds": seg, "recordedAt": fmt(when)})}


def command_result(robot_id: str, when: datetime, command_id, ok=True, msg="sim executed", action=None) -> dict:
    return {"robotId": robot_id, "time": fmt(when),
            "result": {"command_id": command_id, "ok": ok, "msg": msg, "action": action}}


def walk(waypoints, steps_per_seg: int = 8, drift_per_step: float = 0.0):
    """沿航点(每个=(x,y,z,name))一步步走，产出 (x, y, z, yaw, reached_name_or_None)。
    drift_per_step = 每步累积的单向漂移(米)，模拟 FAST-LIO 里程计漂移（默认 0=不漂）。"""
    dx = dy = 0.0
    n = len(waypoints)
    for i in range(n):
        a, b = waypoints[i], waypoints[(i + 1) % n]
        yaw = math.atan2(b[1] - a[1], b[0] - a[0])
        for s in range(1, steps_per_seg + 1):
            f = s / steps_per_seg
            x = a[0] + (b[0] - a[0]) * f + dx
            y = a[1] + (b[1] - a[1]) * f + dy
            z = a[2] + (b[2] - a[2]) * f
            dx += drift_per_step
            dy += drift_per_step * 0.3
            yield (x, y, z, yaw, b[3] if s == steps_per_seg else None)
