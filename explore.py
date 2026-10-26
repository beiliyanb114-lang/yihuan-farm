# -*- coding: utf-8 -*-
"""店里探索工具：按指令做动作 + 截图，用来搞清楚"锤子"到底怎么用。

自动提权（因为游戏是管理员进程，不提权点不动）。

用法:
    python explore.py "s before | c 640 1400 | w 1 | s after"
动作语法（用 | 分隔）:
    c X Y      点击
    h X Y      只移动鼠标（看提示框）
    k 键名     按键
    w 秒       等待
    s 名字     截图到 shots/名字.png
    t 点名     点击 config.json 里的点（lvl/begin/settle/money）
"""
from __future__ import annotations

import os
import sys
import time

import common as c

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "shots")
sys.path.insert(0, HERE)


def parse(script: str) -> list[tuple]:
    acts = []
    for raw in script.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split()
        op = parts[0].lower()
        if op == "c":
            acts.append(("click", int(parts[1]), int(parts[2])))
        elif op == "h":
            acts.append(("hover", int(parts[1]), int(parts[2])))
        elif op == "k":
            acts.append(("key", parts[1]))
        elif op == "w":
            acts.append(("wait", float(parts[1])))
        elif op == "s":
            acts.append(("shot", parts[1]))
        elif op == "t":
            acts.append(("point", parts[1]))
        else:
            print(f"看不懂的动作: {raw}")
    return acts


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    # ---- 提权（游戏是管理员进程）----
    if not c.is_elevated() and not os.environ.get("YHF_LAUNCHED"):
        ok, why = c.relaunch_as_admin()
        print(f"[>] 提权: {why}（跑完看 logs\\console.log）")
        return 3 if ok else 2

    script = " ".join(sys.argv[1:])
    acts = parse(script)
    os.makedirs(SHOTS, exist_ok=True)

    import json
    cfg_path = os.path.join(HERE, "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    points = cfg.get("points") or {}

    stage = c.Stage(cfg.get("window", {}).get("exe", "HTGame.exe"),
                    cfg.get("window", {}).get("title_kw", "异环"))
    if not stage.enter():
        print("[X] 找不到游戏窗口")
        return 1
    hwnd = stage.win["hwnd"]
    print(f"[OK] 已把游戏顶到最前 窗口={stage.win['w']}x{stage.win['h']} "
          f"前台={c.is_foreground(hwnd)}")
    time.sleep(0.6)
    if not c.is_foreground(hwnd):
        print("[!] 游戏没拿到前台，再抢一次")
        c.force_foreground(hwnd)
        time.sleep(0.4)
        print("    重试后前台:", c.is_foreground(hwnd))
    # 先把可能开着的开始菜单/弹窗关掉
    c.press_key("esc", 0.05)
    time.sleep(0.5)
    c.force_foreground(hwnd)
    print("    当前前台是游戏:", c.is_foreground(hwnd))

    try:
        for act in acts:
            op = act[0]
            if op == "click":
                c.click(act[1], act[2], hold=0.05)
                print(f"  点击 ({act[1]},{act[2]})")
            elif op == "hover":
                c.move_to(act[1], act[2])
                print(f"  悬停 ({act[1]},{act[2]})")
                time.sleep(0.7)
            elif op == "key":
                c.press_key(act[1], 0.06)
                print(f"  按键 {act[1]}")
            elif op == "wait":
                time.sleep(act[1])
                print(f"  等 {act[1]}s")
            elif op == "shot":
                arr = c.grab(stage.win["client"])
                p = os.path.join(SHOTS, f"{act[1]}.png")
                c.Image.fromarray(arr).save(p)
                print(f"  截图 -> shots/{act[1]}.png  mean={arr.mean():.1f}")
            elif op == "point":
                p = points.get(act[1])
                if not p:
                    print(f"  [!] 没有点 {act[1]}")
                    continue
                c.click(p["center"][0], p["center"][1], hold=0.05)
                print(f"  点击点 {act[1]} @ {p['center']}")
            time.sleep(0.15)
    finally:
        time.sleep(0.4)
        stage.leave()
        print("[OK] 已还原（浏览器放回来）")
    return 0


if __name__ == "__main__":
    raise SystemExit(c.run_main(main))
