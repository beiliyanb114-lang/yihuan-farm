# -*- coding: utf-8 -*-
"""体检工具 —— 开刷前先跑这个，把"为什么点了没反应"一次性说清楚。

    python doctor.py

检查项：
    1. 游戏窗口（进程 / 尺寸 / 是否全屏）
    2. 权限（游戏是不是管理员，我是不是管理员）← 这是点击失效最常见的原因
    3. 截屏是不是活的（连续抓帧，看画面有没有在变）
    4. 游戏是不是在渲染（CPU 占用）
    5. config.json 的坐标有没有越界
    6. 给出结论和该怎么做
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import time

import numpy as np

import common as c

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
advapi = ctypes.windll.advapi32
k32 = ctypes.windll.kernel32


def is_elevated(pid: int | None = None) -> bool:
    return c.is_elevated(pid)


def check_window():
    win = c.get_game_window()
    if not win:
        print("[X] 没找到游戏窗口（异环没开？）")
        return None
    x1, y1, x2, y2 = win["client"]
    sw, sh = c.virtual_screen()[2:]
    full = (x2 - x1) >= sw - 4 and (y2 - y1) >= sh - 4
    print(f"[OK] 游戏窗口: {win['title'].strip()!r}  {win['w']}x{win['h']}  "
          f"客户区 ({x1},{y1})-({x2},{y2})  {'全屏尺寸' if full else '窗口化'}")
    return win


def check_elevation(win) -> bool:
    game_elev = is_elevated(win["pid"])
    me_elev = is_elevated()
    print(f"[..] 游戏进程 HTGame.exe 管理员: {'是' if game_elev else '否'}")
    print(f"[..] 本脚本 管理员: {'是' if me_elev else '否'}")
    if game_elev and not me_elev:
        print("[X] 权限不匹配！游戏是管理员，脚本不是 → Windows(UIPI) 会把脚本的点击/按键丢掉。")
        print("    解决：右键 3-开始刷.bat → 以管理员身份运行（bat 现在也会自己申请提权）。")
        return False
    if not game_elev and me_elev:
        print("[!] 脚本比游戏权限高，一般也能点，但注意别误点到别的窗口。")
        return True
    print("[OK] 权限一致，点击可以被游戏收到。")
    return True


def check_capture(win) -> bool:
    frames = []
    for _ in range(6):
        a = c.grab(win["client"])
        frames.append(a)
        time.sleep(0.25)
    uniq = len({hash(f.tobytes()) for f in frames})
    diff = float(np.abs(frames[0].astype(np.int16) - frames[-1].astype(np.int16)).mean())
    print(f"[..] 截屏活性: 6 帧里 {uniq} 个不同画面，首末帧平均差 {diff:.2f}")
    if uniq == 1:
        print("[!] 画面完全没动。可能是：① 游戏本来就是静止画面（菜单/暂停）")
        print("    ② 独占全屏 —— 系统截不到它的实时画面，去游戏设置改成「无边框窗口」。")
        print("    判断方法：在游戏里走动一下，或切到桌面再切回来，再跑一次本检查。")
        return False
    print("[OK] 截屏是活的，脚本能看见游戏画面。")
    return True


def check_render(win) -> bool:
    try:
        import subprocess
        p1 = subprocess.run(["powershell", "-NoProfile", "-Command",
                             f"(Get-Process -Id {win['pid']}).TotalProcessorTime.TotalMilliseconds"],
                            capture_output=True, text=True, timeout=10)
        t1 = float(p1.stdout.strip())
        time.sleep(1.5)
        p2 = subprocess.run(["powershell", "-NoProfile", "-Command",
                             f"(Get-Process -Id {win['pid']}).TotalProcessorTime.TotalMilliseconds"],
                            capture_output=True, text=True, timeout=10)
        t2 = float(p2.stdout.strip())
        used = t2 - t1
        print(f"[..] 游戏 1.5 秒用了 {used:.0f} ms CPU")
        if used < 30:
            print("[!] 游戏几乎不占 CPU —— 可能卡住/在后台暂停了，切回游戏窗口再看。")
            return False
        print("[OK] 游戏在正常渲染。")
        return True
    except Exception as e:
        print(f"[!] 读不到 CPU 占用: {e}")
        return True


def check_config():
    if not os.path.exists(CFG_PATH):
        print("[!] 还没有 config.json —— 先跑 1-标定.bat")
        return
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    pts = cfg.get("points") or {}
    sw, sh = c.virtual_screen()[2:]
    bad = []
    for name, p in pts.items():
        x, y = p["center"]
        if not (0 <= x < sw and 0 <= y < sh):
            bad.append(f"{name}({x},{y})")
    print(f"[..] config.json: {len(pts)} 个点 {list(pts)}")
    if bad:
        print(f"[X] 这些点跑到屏幕外了（改过分辨率？）→ 重新标定: {bad}")
    else:
        print("[OK] 坐标都在屏幕范围内。")
    if "serve_area" not in pts:
        print("[!] 没标顾客区域(serve_area)，锤顾客会退化成点屏幕中间。")
    if "money" not in pts:
        print("[!] 没标钱数区域(money)，只能按时间收工，没法自动判断'收满'。")
    if "entry" not in pts:
        print("[!] 没标入口(entry)，脚本不会自己进店，需要你手动站在店长特供里。")


def main():
    print("=" * 66)
    print(" 异环 · 店长特供 自动刷 —— 开刷前体检")
    print("=" * 66)
    win = check_window()
    if not win:
        return 1
    print("-" * 66)
    perm = check_elevation(win)
    print("-" * 66)
    live = check_capture(win)
    print("-" * 66)
    render = check_render(win)
    print("-" * 66)
    check_config()
    print("=" * 66)
    if perm and live and render:
        print(" 结论：一切正常，可以开刷（3-开始刷.bat，F9 开始）。")
    else:
        print(" 结论：有必须先解决的问题，见上面的 [X] / [!]。")
        if not perm:
            print("   → 最重要：用管理员身份运行（bat 会自己弹 UAC，点『是』）。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(c.run_main(main))
