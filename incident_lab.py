#!/usr/bin/env python3
"""突发素材库 + 端到端召回验证（虚拟狗侧，自包含、不依赖平台代码）。
万相生成「人倒地 / 火 / 积水 / 正常」四类安防监控图，缓存到 sim_library/incident_<kind>.png 供狗复用；
--validate 时把每张图经【平台 GO2 视频接口】(img→mp4→/api/v1/robot/video/upload)上传，核对平台是否真识别出突发
——比在进程内调 recognizer 更真(走完整管道)，且狗不碰平台代码。

  python incident_lab.py                                   # 仅生成素材
  BACKEND_URL=http://localhost:8000 GO2_DEMO_ROBOTID=go2-xxx \
    python incident_lab.py --validate                      # 生成 + 经平台端到端验召回(需已注册 robotId)
"""
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

import httpx

# 先加载 .env（拿百炼 key）
_ENVF = Path(__file__).resolve().parent / ".env"
if _ENVF.exists():
    for _l in _ENVF.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from robot_sim import _wanx, LIB_DIR, BACKEND   # 复用万相生成 + 上报地址(狗内部依赖)
from go2_sim import img_to_mp4

# 每类突发一张"明显能看出来"的监控场景（高召回验证用，场景越清楚越能证明 prompt 有效）
SCENES = {
    # t2i 模型对"人躺地上"易画成坐姿/站姿 → 必须强调"完全贴地水平平躺、四肢摊开、俯视能看到整个人在地面"
    "person_down": "监控俯拍画面：一名男子面朝上平躺、昏倒在办公楼大厅光洁瓷砖地面上，身体完全贴地水平伸展、四肢摊开一动不动，正上方俯视监控视角能清楚看到整个人躺在地面，写实纪实风格，光线明亮，无任何文字水印",
    "fire": "一张安防监控摄像头画面：室内墙角与垃圾桶正在起火，清晰可见橙红色明火与升腾的黑色浓烟，写实监控纪实风格，光线偏暗带火光，无任何文字与水印",
    "flooding": "一张安防监控摄像头画面：地下车库走廊地面大面积积水水浸，水面反光淹没地面并漫向远处，写实监控纪实风格，光线清晰，无任何文字与水印",
    "normal": "一张安防监控摄像头画面：空旷整洁的写字楼室内走廊，地面干净干燥、无任何杂物、无人员异常，一切正常，写实监控纪实风格，光线明亮，无任何文字与水印",
}


def gen(kind):
    f = LIB_DIR / f"incident_{kind}.png"
    if f.exists():
        return f
    print(f"[lab] 万相生成 {kind} …", flush=True)
    img = _wanx(SCENES[kind])
    if not img:
        print(f"[lab] !! 生成失败 {kind}", flush=True)
        return None
    f.write_bytes(img)
    return f


def validate():
    """把每类突发图经平台 GO2 视频接口上传，核对平台是否识别出突发（端到端、不碰平台代码）。"""
    rid = os.getenv("GO2_DEMO_ROBOTID")
    if not rid:
        sys.exit("需 export GO2_DEMO_ROBOTID=<平台已注册的 robotId>，平台据它认狗")
    http = httpx.Client(timeout=120, trust_env=False)
    recall = 0
    for kind in ("person_down", "fire", "flooding", "normal"):
        f = gen(kind)
        if not f:
            continue
        mp4 = img_to_mp4(f.read_bytes())
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        r = http.post(f"{BACKEND}/api/v1/robot/video/upload",
                      files={"file": (f"{kind}.mp4", mp4, "video/mp4")},
                      data={"robotId": rid, "time": when, "fileName": f"{kind}.mp4", "fileSize": str(len(mp4))})
        hits = (r.json() or {}).get("incidents", []) if r.status_code == 200 else []
        labels = [h.get("label") for h in hits]
        ok = (len(labels) == 0) if kind == "normal" else (len(labels) > 0)
        recall += 1 if (ok and kind != "normal") else 0
        print(f"[lab] {kind:12} → 平台命中={labels or '无'}  {'✓' if ok else '✗'}", flush=True)
    print(f"\n[lab] 三类突发端到端召回 {recall}/3（经平台真管道）", flush=True)
    return recall


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true", help="经平台 GO2 视频接口端到端验突发召回(需 GO2_DEMO_ROBOTID)")
    args = ap.parse_args()
    if args.validate:
        validate()
    else:
        for k in SCENES:
            gen(k)
        print("[lab] 素材已生成/缓存到 sim_library/。--validate 走平台端到端验召回。", flush=True)
