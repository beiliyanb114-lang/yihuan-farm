# -*- coding: utf-8 -*-
"""异环 · 店长特供自动刷 —— 标定工具

用法:
    1) 先把游戏切到"能看见店长特供入口"的界面（站着别动）
    2) python calibrate.py
    3) 它会把游戏画面截下来显示，你按提示在画面上**框住**对应按钮：
         按住左键拖一个框 = 记录这一步（框中心就是以后点击的位置）
         右键 / Esc      = 跳过这一步
         Backspace       = 撤销上一步
         R               = 重新截一张
         S               = 保存退出
    4) 保存后生成 config.json + tpl/ 模板图，再运行 farm.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox

import numpy as np
from PIL import Image, ImageTk

import common as c

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
TPL_DIR = os.path.join(HERE, "tpl")
REF_PATH = os.path.join(HERE, "ref.png")

# key, 提示文字, 类型
STEPS = [
    ("entry", "① 主城里「店长特供」的入口（已经在店长特供界面里就右键跳过）", "button"),
    ("lvl", "② 关卡列表里的「1-1 新品练习!」那张卡", "button"),
    ("begin", "③ 右下角「开始营业」按钮", "button"),
    ("hammer", "④ 店里左边那个发光的「100T 锤子」图标", "button"),
    ("back", "⑤ 店里左上角的返回箭头（收满了用它提前退出）", "button"),
    ("claim", "⑥ 结算界面右边那个「领取」按钮", "button"),
]

# 这一步要框的东西在哪个界面（用户需要按 R 切过去截）
SCREEN_HINT = {
    "entry": "主城",
    "lvl": "店长特供菜单",
    "begin": "店长特供菜单",
    "hammer": "店里",
    "back": "店里",
    "claim": "结算界面",
}

DEFAULT_CONFIG = {
    "_说明": "这个文件由 calibrate.py 生成，坐标是屏幕像素（左上角为 0,0）。可以直接手改。",
    "window": {"exe": "HTGame.exe", "title_kw": "异环"},
    "hotkeys": {"start": "F9", "pause": "F10", "stop": "F12", "teach": "F8"},
    "round": {
        "serve_seconds": 110,
        "serve": {"rate": 6.0, "jitter": 14, "click_hold": 0.03, "keys": [], "key_every": 5.0},
        "enter_sequence": [],
        "start_sequence": [],
        "end_sequence": [],
        "settle_wait": 1.6,
    },
    "money": {"stable_seconds": 8.0, "eps": 2.0},
    "safety": {"max_rounds": 0, "restart_on_lost_focus": True, "stop_if_bg_seconds": 20},
}


# ---------------------------------------------------------------- 配置生成
def build_config(marks: dict[str, dict], shot_size: tuple[int, int],
                 offset: tuple[int, int] = (0, 0)) -> dict:
    """把标定结果变成 config.json 的内容。

    marks 里的 rect 是相对截图的坐标；offset 是客户区左上角在屏幕上的位置，
    加完之后 config 里存的就是**屏幕绝对坐标**，点击时不用再换算。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["screen"] = {"w": shot_size[0], "h": shot_size[1]}
    ox, oy = int(offset[0]), int(offset[1])

    points = {}
    for key, info in marks.items():
        x1, y1, x2, y2 = info["rect"]
        points[key] = {
            "rect": [int(x1) + ox, int(y1) + oy, int(x2) + ox, int(y2) + oy],
            "center": [int((x1 + x2) // 2) + ox, int((y1 + y2) // 2) + oy],
            "tpl": info.get("tpl"),
        }
    cfg["points"] = points

    def has(k):
        return k in points

    # 进场：只跑一次（从主城进店长特供）
    enter = []
    if has("entry"):
        enter.append({"click": "entry", "wait": 1.8})

    # 每轮开始：选 1-1 -> 开始营业
    start = []
    if has("lvl"):
        start.append({"click": "lvl", "wait": 0.9})
    if has("begin"):
        start.append({"click": "begin", "wait": 6.0})

    # 每轮结束：返回箭头退出 -> 领取
    end = []
    if has("back"):
        end.append({"click": "back", "wait": 2.5})
    if has("claim"):
        end.append({"click": "claim", "wait": 3.2})

    cfg["round"]["enter_sequence"] = enter
    # 锤子模式：反复点那个点
    if has("hammer"):
        # 注意：这里的键名要和 farm.py 读的一致（target_hits / max_tries），
        # 别写成 max_clicks，否则标定出来的配置跟文档对不上。
        cfg["round"]["serve"] = {"point": "hammer", "target_hits": 3, "max_tries": 18,
                                   "interval": 0.9, "jitter": 4, "click_hold": 0.05,
                                   "keys": [], "key_every": 5.0}
    cfg["round"]["start_sequence"] = start
    cfg["round"]["end_sequence"] = end
    return cfg


def crop_template(shot: np.ndarray, rect) -> tuple[np.ndarray, float]:
    x1, y1, x2, y2 = [int(v) for v in rect]
    patch = shot[max(0, y1):y2, max(0, x1):x2]
    return patch, float(patch.std()) if patch.size else 0.0


# ---------------------------------------------------------------- 截图
def shoot(cfg: dict) -> tuple[np.ndarray, dict]:
    """截图前先把浏览器收起来、把游戏顶上来，否则截到的是浏览器。"""
    stage = c.Stage(cfg["window"]["exe"], cfg["window"]["title_kw"])
    if not stage.enter():
        raise RuntimeError("没找到游戏窗口（异环没开？）")
    try:
        c.human_sleep(0.6, 0.8)
        arr = c.grab(stage.win["client"])          # 用客户区，坐标和点击一致
    finally:
        stage.leave()                              # 还原，好让标定窗口显示在最上面
    return arr, stage.win


# ---------------------------------------------------------------- GUI
class Calibrator:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(CFG_PATH):
            try:
                self.cfg = json.load(open(CFG_PATH, encoding="utf-8"))
            except Exception:
                pass

        root.title("异环 · 店长特供 标定")
        root.configure(bg="#111318")
        root.attributes("-topmost", True)

        self.status = tk.Label(root, text="正在截图…", bg="#111318", fg="#8fe3ff",
                               font=("Microsoft YaHei UI", 13, "bold"), anchor="w", padx=10)
        self.status.pack(fill="x")
        self.hint = tk.Label(root, text="", bg="#111318", fg="#9aa4b2",
                             font=("Microsoft YaHei UI", 10), anchor="w", padx=10)
        self.hint.pack(fill="x")

        self.canvas = tk.Canvas(root, bg="#000000", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.shot = None
        self.scale = 1.0
        self.photo = None
        self.marks: dict[str, dict] = {}
        self.order: list[str] = []
        self.i = 0
        self.drag_start = None
        self.drag_rect = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self.skip())
        root.bind("<Escape>", lambda e: self.skip())
        root.bind("<BackSpace>", lambda e: self.undo())
        root.bind("<Key-r>", lambda e: self.retake())
        root.bind("<Key-R>", lambda e: self.retake())
        root.bind("<Key-s>", lambda e: self.save())
        root.bind("<Key-S>", lambda e: self.save())

        self.root.withdraw()
        self.root.after(200, self.retake)

    # --- 截图 ---
    def retake(self):
        # 先把自己藏起来，否则截到的是标定窗口本身
        self.root.withdraw()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        time.sleep(0.35)
        try:
            arr, win = shoot(self.cfg)
        except Exception as e:
            messagebox.showerror("截图失败", str(e))
            self.root.destroy()
            return
        self.shot = arr
        self.win = win
        self.offset = (win["client"][0], win["client"][1])
        self.show()
        self.redraw()
        self.update_prompts()

    def show(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        h, w = self.shot.shape[:2]
        self.scale = min((sw - 80) / w, (sh - 170) / h, 1.0)
        disp = Image.fromarray(self.shot).resize(
            (max(1, int(w * self.scale)), max(1, int(h * self.scale))), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(disp)
        self.canvas.config(width=disp.width, height=disp.height)
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw", tags="bg")
        self.canvas.tag_lower("bg")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

    # --- 坐标换算 ---
    def to_shot(self, cx: float, cy: float) -> tuple[int, int]:
        return int(round(cx / self.scale)), int(round(cy / self.scale))

    # --- 交互 ---
    def on_press(self, e):
        if self.shot is None or self.i >= len(STEPS):
            return
        self.drag_start = (e.x, e.y)
        if self.drag_rect:
            self.canvas.delete(self.drag_rect)
            self.drag_rect = None

    def on_drag(self, e):
        if not self.drag_start:
            return
        x0, y0 = self.drag_start
        if self.drag_rect:
            self.canvas.delete(self.drag_rect)
        self.drag_rect = self.canvas.create_rectangle(
            x0, y0, e.x, e.y, outline="#ffd166", width=2, dash=(4, 3))

    def on_release(self, e):
        if not self.drag_start:
            return
        x0, y0 = self.drag_start
        x1, y1 = e.x, e.y
        if abs(x1 - x0) < 4 and abs(y1 - y0) < 4:      # 当作点：给个 56x40 的默认框
            x0, y0, x1, y1 = x0 - 28, y0 - 20, x0 + 28, y0 + 20
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        self.drag_start = None
        if self.drag_rect:
            self.canvas.delete(self.drag_rect)
            self.drag_rect = None

        sx1, sy1 = self.to_shot(x0, y0)
        sx2, sy2 = self.to_shot(x1, y1)
        sx1, sy1 = max(0, sx1), max(0, sy1)
        sx2 = min(self.shot.shape[1], sx2)
        sy2 = min(self.shot.shape[0], sy2)
        if sx2 - sx1 < 6 or sy2 - sy1 < 6:
            return

        key, _, kind = STEPS[self.i]
        tpl_path = None
        if kind == "button" or key == "money":
            patch, sd = crop_template(self.shot, (sx1, sy1, sx2, sy2))
            if sd < 8:
                self.hint.config(text=f"[!] 「{key}」这块画面太平了（标准差 {sd:.1f}），"
                                      "自动识别会不稳，建议框到有字/有边框的地方。仍可继续。")
            os.makedirs(TPL_DIR, exist_ok=True)
            tpl_path = os.path.join("tpl", f"{key}.png")   # 相对路径，搬电脑也不怕
            Image.fromarray(patch).save(tpl_path)

        self.marks[key] = {"rect": [sx1, sy1, sx2, sy2], "tpl": tpl_path}
        self.order.append(key)
        self.i += 1
        self.redraw()
        self.update_prompts()

    def skip(self):
        if self.i >= len(STEPS):
            return
        self.i += 1
        self.update_prompts()

    def undo(self):
        """回退一步：不管上一步是框了还是跳过了，都退回去重新来。"""
        if self.i <= 0:
            return
        self.i -= 1
        key = STEPS[self.i][0]
        info = self.marks.pop(key, None)
        if info and info.get("tpl") and os.path.exists(info["tpl"]):
            try:
                os.remove(info["tpl"])
            except OSError:
                pass
        if key in self.order:
            self.order.remove(key)
        self.redraw()
        self.update_prompts()

    def redraw(self):
        self.canvas.delete("mark")
        for idx, (key, label, kind) in enumerate(STEPS):
            info = self.marks.get(key)
            if not info:
                continue
            x1, y1, x2, y2 = [int(v * self.scale) for v in info["rect"]]
            color = "#8fe3ff" if kind == "button" else "#7CFFB2"
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2, tags="mark")
            self.canvas.create_text(x1 + 4, y1 - 9, text=label[0], fill=color, anchor="w",
                                    font=("Microsoft YaHei UI", 10, "bold"), tags="mark")

    def update_prompts(self):
        if self.i >= len(STEPS):
            self.status.config(text="全部标定完成 [OK]  按 S 保存并退出（Backspace 撤销）")
            self.hint.config(text=f"将写入: {CFG_PATH}")
            return
        key, label, kind = STEPS[self.i]
        self.status.config(text=f"请框住 {label}")
        tip = ("按住左键拖出一个框；框中心 = 以后点击的位置" if kind == "button"
               else "按住左键拖出一个区域")
        where = SCREEN_HINT.get(key, "")
        if where:
            tip = f"{tip}　【{where}看不到就切到游戏，回来按 R 重新截图】"
        self.hint.config(text=f"{tip} ｜ 右键/Esc 跳过 ｜ Backspace 撤销 ｜ R 重新截图 ｜ S 保存")
        self.last_key = key

    def save(self):
        if not self.marks:
            messagebox.showwarning("还没标定", "至少标定一个按钮再保存。")
            return
        cfg = build_config(self.marks, (self.shot.shape[1], self.shot.shape[0]),
                           getattr(self, "offset", (0, 0)))
        # 保留用户手改过的部分（hotkeys / round 参数等）
        old = {}
        if os.path.exists(CFG_PATH):
            try:
                old = json.load(open(CFG_PATH, encoding="utf-8"))
            except Exception:
                old = {}
        for k in ("hotkeys", "window", "safety"):
            if k in old:
                cfg[k] = old[k]
        if "round" in old:
            for k in ("serve_seconds", "serve"):
                if k in old["round"]:
                    cfg["round"][k] = old["round"][k]

        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        Image.fromarray(self.shot).save(REF_PATH)
        print(f"[标定完成] 已写入 {CFG_PATH}")
        for k, v in cfg["points"].items():
            print(f"  {k:11s} center={v['center']} tpl={v['tpl']}")
        messagebox.showinfo("搞定", f"已保存到\n{CFG_PATH}\n\n接下来运行 farm.py（F9 开始 / F12 停）。")
        self.root.destroy()


def main():
    if "--print-steps" in sys.argv:
        for k, label, kind in STEPS:
            print(k, "|", kind, "|", label)
        return
    try:
        root = tk.Tk()
    except Exception as e:
        print("tkinter 起不来:", e)
        return 1
    Calibrator(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(c.run_main(main))
