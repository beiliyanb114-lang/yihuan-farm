# -*- coding: utf-8 -*-
"""异环 · 店长特供 自动刷

流程：进店 -> 用锤子把顾客全砸走（狂点/按键）-> 收满 -> 结算 -> 再来一轮 -> 循环

用法:
    python farm.py               正常跑（F9 开始 / F10 暂停 / F12 停）
    python farm.py --auto        倒计时 5 秒后自己开始
    python farm.py --rounds 10   只跑 10 轮
    python farm.py --dry-run     只走流程不点鼠标（先确认坐标对不对）
    python farm.py --teach       教学模式：跑的时候钱一满就按 F8，让它记住"满"的样子

安全:
    * 任何时刻 F12 = 立刻停手；鼠标甩到屏幕左上角 (0,0) 也会停。
    * 只发鼠标键盘 + 看屏幕，不读写游戏内存、不注入。
    * 游戏必须在前台（它自己会抢前台；你手动切走超过 20 秒会自动暂停）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

import common as c

# 控制台编码兜底：别因为某个符号打不出来就整个脚本崩掉
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
SHOT_DIR = os.path.join(HERE, "shots")
FULL_REF = os.path.join(HERE, "full_ref.png")


# ---------------------------------------------------------------- 日志
class Log:
    def __init__(self, name="run"):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.log")
        self.f = open(self.path, "w", encoding="utf-8")

    def __call__(self, msg: str):
        line = f"[{c.now()}] {msg}"
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def snap(tag: str) -> str:
    """出错/关键节点截个图，方便事后看是哪一步歪了。"""
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, f"{time.strftime('%H%M%S')}_{tag}.png")
    try:
        win = c.get_game_window()
        if win:
            c.save_shot(path, bbox=win["client"])
            return path
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 配置
def load_config(path=CFG_PATH) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"没有找到 {path}\n先跑一遍标定：  python calibrate.py")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # 模板路径可能是相对的（tpl/xxx.png），按配置文件所在目录补全，
    # 这样整个文件夹搬到别的电脑/别的盘也能直接用
    base = os.path.dirname(os.path.abspath(path))
    for pt in (cfg.get("points") or {}).values():
        t = pt.get("tpl")
        if t and not os.path.isabs(t):
            pt["tpl"] = os.path.normpath(os.path.join(base, t))
    return cfg


# ---------------------------------------------------------------- 钱满检测
class MoneyWatcher:
    """没有 OCR 也能判断"收满了"：看钱数/进度条那块区域**不再变化**就算满。

    只有在"看见过它变化"之后才会报警，避免区域本来就静止导致一上来就误判。
    """

    def __init__(self, cfg, win, log, stable=2.5, eps=2.0):
        p = (cfg.get("points") or {}).get("money")
        self.rect = None
        if p:
            self.rect = tuple(int(v) for v in p["rect"])   # 已经是屏幕绝对坐标
        mc = cfg.get("money") or {}
        self.log = log
        self.stable = float(mc.get("stable_seconds", stable))
        self.eps = float(mc.get("eps", eps))
        self.prev = None
        self.last_change = time.time()
        self.seen_change = False
        self.ref = None
        if self.rect and os.path.exists(FULL_REF):
            try:
                self.ref = np.asarray(c.Image.open(FULL_REF).convert("RGB"))
                if self.ref.shape[:2] != (self.rect[3] - self.rect[1], self.rect[2] - self.rect[0]):
                    self.ref = None
            except Exception:
                self.ref = None

    @property
    def enabled(self) -> bool:
        return self.rect is not None

    def teach(self) -> bool:
        """记录当前（满）状态为参考图。"""
        if not self.rect:
            return False
        cur = c.grab(self.rect)
        c.Image.fromarray(cur).save(FULL_REF)
        self.ref = cur
        return True

    def update(self) -> str:
        """返回 "" 表示还没满，返回原因字符串表示该结束了。"""
        if not self.rect:
            return ""
        cur = c.grab(self.rect)
        if self.prev is not None and cur.shape == self.prev.shape:
            diff = float(np.abs(cur.astype(np.int16) - self.prev.astype(np.int16)).mean())
            if diff > self.eps:
                self.seen_change = True
                self.last_change = time.time()

        if self.ref is not None and self.ref.shape == cur.shape:
            score = 1.0 - float(np.abs(cur.astype(np.int16) - self.ref.astype(np.int16)).mean()) / 255.0
            if score >= 0.955:
                self.prev = cur
                return f"钱满了(和教过的样子吻合 {score:.3f})"

        self.prev = cur
        if self.seen_change and time.time() - self.last_change >= self.stable:
            return f"钱数 {self.stable:.1f}s 没再跳了(≈满)"
        return ""


# ---------------------------------------------------------------- 动作
class Bot:
    def __init__(self, cfg: dict, log: Log, dry: bool = False, shots_every: float = 0):
        self.cfg = cfg
        self.log = log
        self.dry = dry
        self.shots_every = shots_every
        self.round_no = 0
        self.win = None
        self.points = cfg.get("points") or {}
        self.hk = c.Hotkey(cfg.get("hotkeys") or {"start": "F9", "pause": "F10", "stop": "F12"})
        self.paused = False
        self.stopped = False
        self.start_requested = False
        self.rounds_done = 0
        self.bg_since = None

    # --- 窗口 ---
    def find_window(self) -> bool:
        w = c.get_game_window(self.cfg["window"]["exe"], self.cfg["window"]["title_kw"])
        if w:
            exe = c._exe_of_pid(w["pid"]).lower()
            want = self.cfg["window"]["exe"].lower()
            if exe != want:
                self.log(f"[X] 挑中的窗口进程是 {exe}，不是 {want}，不用它")
                w = None
        self.win = w
        return w is not None

    def ensure_front(self) -> bool:
        if self.dry:
            return True
        if not self.win:
            if not self.find_window():
                return False
        if c.is_foreground(self.win["hwnd"]):
            return True
        ok = c.force_foreground(self.win["hwnd"])
        if ok:
            c.human_sleep(0.25, 0.4)
        return ok

    # --- 热键 ---
    def poll_keys(self) -> None:
        if self.hk.edge("stop"):
            self.log("[STOP] 收到 F12，停止")
            self.stopped = True
        if self.hk.edge("pause"):
            self.paused = not self.paused
            self.log("[PAUSE] 暂停" if self.paused else "[GO] 继续")
            c.beep(660 if self.paused else 990)
        if self.hk.edge("start"):
            self.start_requested = True
            if self.paused:
                self.paused = False
                self.log("[GO] 继续")
                c.beep(990)

    def guard(self) -> bool:
        """每步之前调用：处理暂停/停止/前台丢失/鼠标甩角落。返回 False = 该停了。"""
        self.poll_keys()
        if self.stopped:
            return False
        if self.paused:
            while self.paused and not self.stopped:
                self.poll_keys()
                time.sleep(0.08)
                if c.user32.GetAsyncKeyState(0x70) & 0x8000:  # F1 = 强制退出暂停
                    self.stopped = True
            if self.stopped:
                return False

        # 鼠标甩到左上角 = 急停
        pt = c.wt.POINT()
        c.user32.GetCursorPos(c.ctypes.byref(pt))
        if pt.x <= 2 and pt.y <= 2:
            self.log("[STOP] 鼠标在左上角，按急停处理")
            self.stopped = True
            return False

        # 前台丢失
        if self.win and not c.is_foreground(self.win["hwnd"]):
            self.bg_since = self.bg_since or time.time()
            if self.cfg.get("safety", {}).get("restart_on_lost_focus", True):
                self.ensure_front()
            if time.time() - self.bg_since > self.cfg.get("safety", {}).get("stop_if_bg_seconds", 20):
                self.log("[PAUSE] 游戏不在前台太久了，自动暂停")
                self.paused = True
                self.bg_since = None
        else:
            self.bg_since = None
        return not self.stopped

    # --- 认画面 ---
    def find_point(self, name: str, pad: int = 150, scale: float = 0.5, thresh: float = 0.75):
        """在某个按钮原本位置附近找它的模板，找到返回 (x, y, 分数)。"""
        p = self.points.get(name)
        if not p or not p.get("tpl") or not os.path.exists(p["tpl"]):
            return None
        x1, y1, x2, y2 = p["rect"]
        bx1, by1 = max(0, x1 - pad), max(0, y1 - pad)
        bx2, by2 = x2 + pad, y2 + pad
        try:
            hay = c.grab((bx1, by1, bx2, by2))
            tpl = np.asarray(c.Image.open(p["tpl"]).convert("RGB"))
            x, y, s = c.match_template(hay, tpl, scale=scale)
        except Exception:
            return None
        if s < thresh:
            return None
        return (bx1 + x, by1 + y, s)

    def wait_for(self, name: str, timeout: float = 6.0, thresh: float = 0.75):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.stopped:
                return None
            hit = self.find_point(name, thresh=thresh)
            if hit:
                return hit
            time.sleep(0.4)
        return None

    # --- 点击 ---
    def click_point(self, name: str) -> bool:
        p = self.points.get(name)
        if not p:
            self.log(f"  (跳过 {name}：没标定)")
            return False
        x, y = p["center"]
        if self.dry:
            self.log(f"  [dry] 点击 {name} @ ({x},{y})")
            return True
        c.click(x, y, hold=self.cfg["round"]["serve"].get("click_hold", 0.03))
        self.log(f"  点击 {name} @ ({x},{y})")
        return True

    def run_sequence(self, steps: list[dict], tag: str) -> None:
        for st in steps:
            if not self.guard():
                return
            if "click" in st:
                if not self.ensure_front():
                    self.log("  [!] 窗口抢不到前台")
                    return
                self.click_point(st["click"])
                c.human_sleep(st.get("wait", 0.6) * 0.85, st.get("wait", 0.6) * 1.15)
            elif "wait" in st:
                c.human_sleep(st["wait"] * 0.8, st["wait"] * 1.2)
            elif "shot" in st:
                sp = snap(str(st["shot"]))
                if sp:
                    self.log(f"  [SHOT] {os.path.basename(sp)}")
            elif "key" in st:
                if self.dry:
                    self.log(f"  [dry] 按键 {st['key']}")
                else:
                    c.press_key(st["key"], st.get("hold", 0.05))
                c.human_sleep(st.get("wait", 0.3))

    # --- 营业中 ---
    def serve(self, watcher: MoneyWatcher, seconds: float) -> None:
        s = self.cfg["round"]["serve"]

        # 模式 A：反复点锤子，用「营业额有没有跳」判断这一下砸中没有
        if s.get("point"):
            p = self.points.get(s["point"])
            if not p:
                self.log(f"[!] 配置里没有点 {s['point']}，没法锤")
                return
            hx, hy = p["center"]
            jitter = int(s.get("jitter", 4))
            interval = float(s.get("interval", 0.9))
            max_tries = int(s.get("max_tries", 18))
            target = int(s.get("target_hits", 3))
            mrect = (self.points.get("money") or {}).get("rect")
            mrect = tuple(int(v) for v in mrect) if mrect else None
            t0 = time.time()
            hits = tries = 0
            self.log(f"  锤子模式：要砸中 {target} 个顾客（最多试 {max_tries} 下）")
            while tries < max_tries and hits < target and time.time() - t0 < seconds:
                if not self.guard():
                    return
                # 锤子图标没了 = 已经进结算界面了
                if p.get("tpl") and os.path.exists(p["tpl"]):
                    if not self.find_point(s["point"], pad=50, thresh=0.6):
                        self.log(f"  锤子图标不见了 -> 结算界面出现（已砸中 {hits} 个）")
                        return
                before = c.grab(mrect) if mrect else None
                x = hx + int(np.random.randint(-jitter, jitter + 1))
                y = hy + int(np.random.randint(-jitter, jitter + 1))
                if self.dry:
                    self.log(f"  [dry] 锤 {tries + 1} 下 @ ({x},{y})")
                    tries += 1
                    time.sleep(0.3)
                    continue
                c.click(x, y, hold=s.get("click_hold", 0.05), settle=0.01)
                tries += 1
                time.sleep(interval)
                if before is None:
                    hits += 1
                    self.log(f"  锤第 {tries} 下")
                    continue
                after = c.grab(mrect)
                changed = (after.shape == before.shape and
                           float(np.abs(after.astype(np.int16) - before.astype(np.int16)).mean()) > 1.5)
                if changed:
                    hits += 1
                    self.log(f"  第 {tries} 下砸中了 -> 第 {hits}/{target} 个")
                    time.sleep(0.45)
                else:
                    self.log(f"  第 {tries} 下空了，等客人上门再锤")
                    time.sleep(0.55)
            self.log(f"[TIME] 收工：砸中 {hits} 个（共锤 {tries} 下）")
            return

        # 模式 B：在区域里狂点（老模式，兜底）
        area = self.points.get("serve_area", {}).get("rect")
        if not area:
            self.log("[!] 没标定顾客区域(serve_area)，退回点屏幕中下部分")
            cx, cy = self.win["client"][0] + self.win["cw"] // 2, self.win["client"][1] + self.win["ch"] // 2
            area = [cx - 300, cy - 200, cx + 300, cy + 200]
        jitter = int(s.get("jitter", 12))
        rate = max(1.0, float(s.get("rate", 6.0)))
        keys = s.get("keys") or []
        key_every = float(s.get("key_every", 5.0))

        t0 = time.time()
        next_key = t0 + key_every if keys else 1e18
        next_check = 0.0
        next_front = 0.0
        next_shot = t0 + 1.0
        clicks = 0

        while time.time() - t0 < seconds:
            if not self.guard():
                return
            now = time.time()

            if self.shots_every and now >= next_shot:
                p = snap(f"r{self.round_no}_t{int(now - t0):03d}s")
                if p:
                    self.log(f"  [SHOT] {os.path.basename(p)}")
                next_shot = now + self.shots_every
            if now >= next_check:
                why = watcher.update()
                next_check = now + 0.5
                if why:
                    self.log(f"[MONEY] {why} -> 提前收工")
                    return
            if now >= next_front:
                self.ensure_front()
                next_front = now + 2.0
            if keys and now >= next_key:
                k = keys[int(now) % len(keys)]
                if not self.dry:
                    c.press_key(k, 0.05)
                self.log(f"  [KEY] 按 {k}")
                next_key = now + key_every

            x, y = c.jitter_rect(tuple(area), pad=jitter)
            if not self.dry:
                c.click(x, y, hold=s.get("click_hold", 0.03), settle=0.02)
            clicks += 1
            time.sleep(max(0.01, 1.0 / rate))
        self.log(f"[TIME] 时间到（这轮点了 {clicks} 下）")

    # --- 一轮 ---
    def one_round(self, idx: int, watcher: MoneyWatcher) -> None:
        r = self.cfg["round"]
        self.round_no = idx
        self.log(f"=== 第 {idx} 轮：进店 ===")
        self.run_sequence(r.get("start_sequence", []), "start")
        if self.stopped:
            return

        # 认一下到底进没进店：看得到「100T 锤子」图标才算进店
        if not self.dry and self.points.get("hammer", {}).get("tpl"):
            hit = self.wait_for("hammer", timeout=9.0, thresh=0.6)
            if not hit:
                self.log("  [!] 9 秒内没看到锤子图标 -> 可能没进店，这轮不乱点了")
                snap(f"noenter{idx}")
                return
            self.log(f"  已进店（锤子图标吻合度 {hit[2]:.2f}）")

        self.log(f"=== 第 {idx} 轮：锤顾客（≤{r.get('serve_seconds', 120)}s）===")
        watcher.prev = None
        watcher.last_change = time.time()
        watcher.seen_change = False
        self.serve(watcher, float(r.get("serve_seconds", 120)))
        if self.stopped:
            return

        self.log(f"=== 第 {idx} 轮：结算 + 下一轮 ===")
        if not self.dry and not self.cfg["round"]["serve"].get("point"):
            if not self.find_point("settle"):
                self.log("  [!] 看不到返回箭头 —— 可能这局已经自己结束了，按配置继续走")
                snap(f"endshape{idx}")
        self.run_sequence(r.get("end_sequence", []), "end")
        self.rounds_done = idx

    # --- 主循环 ---
    def run(self, max_rounds: int = 0, auto: bool = False) -> None:
        if not self.find_window():
            raise SystemExit(f"没找到游戏窗口（{self.cfg['window']['exe']} 没开？）")
        self.log(f"游戏窗口: {self.win['title'].strip()!r} {self.win['w']}x{self.win['h']} "
                 f"@ ({self.win['rect'][0]},{self.win['rect'][1]})")
        if not self.points:
            raise SystemExit("config.json 里没有坐标，先跑 calibrate.py")

        watcher = MoneyWatcher(self.cfg, self.win, self.log)
        if not watcher.enabled and not self.cfg["round"]["serve"].get("point"):
            self.log("提示：没标定 money 区域 -> 只能按固定时间收工")

        # 把游戏顶到最前、浏览器收起来（不然 harness 网页会一直抢焦点）
        stage = c.Stage(self.cfg["window"]["exe"], self.cfg["window"]["title_kw"],
                        minimize_browsers=not self.dry)
        if not stage.enter() and not self.dry:
            raise SystemExit("抢不到游戏窗口前台，先把游戏点出来再运行")
        if not self.dry:
            self.log("已把游戏顶到最前面（浏览器暂时收起，跑完自动还原）")
        try:
            if auto:
                for i in (5, 4, 3, 2, 1):
                    print(f"  {i} 秒后开始…（F12 取消）", flush=True)
                    self.poll_keys()
                    if self.stopped:
                        return
                    time.sleep(1)
            else:
                self.log("待命：按 F9 开始（F10 暂停 / F12 停）")
                while not self.stopped:
                    self.poll_keys()
                    if self.start_requested:
                        break
                    time.sleep(0.06)
                if self.stopped:
                    return

            self.ensure_front()
            c.beep(1200, 120)
            self.log("[GO] 开始刷")

            enter = self.cfg["round"].get("enter_sequence") or []
            if enter:
                self.log("=== 进场（进店长特供）===")
                self.run_sequence(enter, "enter")
                if self.stopped:
                    return

            idx = 0
            while not self.stopped and (max_rounds <= 0 or idx < max_rounds):
                idx += 1
                try:
                    self.one_round(idx, watcher)
                except Exception as e:
                    self.log(f"[ERR] 第 {idx} 轮出异常: {e!r}")
                    p = snap(f"err{idx}")
                    if p:
                        self.log(f"   截图: {p}")
                    time.sleep(1.5)
                if self.stopped:
                    break
                c.human_sleep(0.6, 1.2)

            self.log(f"[END] 结束，完成 {self.rounds_done} 轮")
            c.beep(520, 200)
        finally:
            stage.leave()


# ---------------------------------------------------------------- 入口
def ask_rounds() -> int:
    """开跑前问一句要刷多少轮。"""
    print("=" * 58)
    print("  异环 · 店长特供 1-1 自动刷")
    print("  一轮：进店 -> 砸中 3 个顾客 -> 左上返回 -> 领取（约 30 秒）")
    print("=" * 58)
    try:
        while True:
            raw = input("要刷多少轮？（直接回车 = 一直刷；也可以输入数字，比如 20）> ").strip()
            if not raw:
                print("  -> 一直刷，按 F12 停")
                return 0
            if raw.isdigit():
                n = int(raw)
                if n <= 0:
                    print("  -> 一直刷，按 F12 停")
                    return 0
                print(f"  -> 刷 {n} 轮，大约 {n * 30 // 60} 分 {n * 30 % 60} 秒")
                return n
            print("  要输入数字哦，比如 20")
    except (EOFError, KeyboardInterrupt):
        print("\n  -> 没等到输入，默认一直刷")
        return 0


def notify_done(rounds: int, by_user: bool) -> None:
    """跑完弹个窗口，免得到时候不知道刷完没有。"""
    if by_user:
        title = "异环自动刷 · 已手动停止"
        text = (f"已经停下来了（F12）。\n\n"
                f"完成轮数：{rounds} 轮\n"
                f"浏览器已还原。")
    elif rounds > 0:
        title = "异环自动刷 · 全部完成"
        text = (f"刷完啦！\n\n"
                f"完成轮数：{rounds} 轮\n"
                f"浏览器已还原，游戏停在店长特供界面。\n\n"
                f"（这个窗口点「确定」关掉就行）")
    else:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x00040040)   # 置顶 + 信息图标
    except Exception:
        pass


def rewrite_argv_rounds(n: int) -> None:
    """把 --rounds N 塞进 sys.argv，这样提权重启时子进程也知道要刷几轮。"""
    keep, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--rounds":
            skip = True
            continue
        keep.append(a)
    keep += ["--rounds", str(n)]
    sys.argv = [sys.argv[0]] + keep


def main() -> int:
    ap = argparse.ArgumentParser(description="异环 店长特供 自动刷")
    ap.add_argument("--rounds", type=int, default=0, help="跑几轮，0=一直跑")
    ap.add_argument("--auto", action="store_true", help="倒计时 5 秒自动开始")
    ap.add_argument("--dry-run", action="store_true", help="只走流程不发鼠标键盘")
    ap.add_argument("--teach", action="store_true", help="教学模式：钱满时按 F8 记录样子")
    ap.add_argument("--config", default=CFG_PATH, help="配置文件路径（默认同目录 config.json）")
    ap.add_argument("--probe", metavar="NAME", help="只点一次某个标定点，用来验证“点了有没有反应”")
    ap.add_argument("--list-points", action="store_true", help="列出所有标定过的点")
    ap.add_argument("--shots", type=float, default=0, metavar="秒", help="每 N 秒存一张截图到 shots/，方便事后回看")
    args = ap.parse_args()

    # 提权后新进程的工作目录会变成 C:\Windows\System32，
    # 所以这里先把路径参数转成绝对路径，并把 sys.argv 一起改掉，
    # 这样提权重启时带过去的也是绝对路径。
    _orig_cfg = args.config
    args.config = os.path.abspath(args.config)
    if _orig_cfg != args.config:
        sys.argv = [args.config if a == _orig_cfg else a for a in sys.argv]

    # 开跑前问一句刷多少轮（提权后的子进程带 YHF_LAUNCHED，不再重复问）
    if not (args.list_points or args.probe or args.dry_run or args.rounds):
        if not os.environ.get("YHF_LAUNCHED") and sys.stdin and sys.stdin.isatty():
            args.rounds = ask_rounds()
            if args.rounds:
                rewrite_argv_rounds(args.rounds)

    cfg = load_config(args.config)

    def perm_problem() -> bool:
        """游戏是管理员、而脚本不是 -> 点击会被 UIPI 丢掉。"""
        g = c.get_game_window(cfg["window"]["exe"], cfg["window"]["title_kw"])
        return bool(g) and c.is_elevated(g["pid"]) and not c.is_elevated()

    def perm_help():
        print("=" * 62)
        print("[X] 游戏是『管理员权限』运行，本脚本不是。")
        print("    Windows 的 UIPI 会把脚本发的点击/按键全部丢掉 ——")
        print("    表现就是『点了没反应』，脚本本身没病。")
        print("    解决：用管理员身份运行（3-开始刷.bat 会自动弹 UAC 申请）。")
        print("=" * 62)

    # 只是看坐标：不过任何闸
    if args.list_points:
        print(f"配置: {os.path.abspath(args.config)}")
        for k, v in (cfg.get("points") or {}).items():
            tpl = os.path.basename(v["tpl"]) if v.get("tpl") else "-"
            print(f"  {k:11s} center={str(v['center']):14s} rect={str(v['rect']):26s} tpl={tpl}")
        print(f"  轮次流程: enter={len(cfg['round'].get('enter_sequence') or [])} 步 "
              f"start={len(cfg['round'].get('start_sequence') or [])} 步 "
              f"end={len(cfg['round'].get('end_sequence') or [])} 步")
        return 0

    # 单点试按：即使权限不对也照点一次，好让主人看到"确实没反应"
    if args.probe:
        log = Log("probe")
        p = (cfg.get("points") or {}).get(args.probe)
        if not p:
            raise SystemExit(f"没有标定过 {args.probe}；现有: {list(cfg.get('points') or {})}")
        if perm_problem():
            perm_help()
            log("仍然会点一次给你看效果（大概率无反应）")
        log(f"3 秒后点击 {args.probe} @ {p['center']}")
        time.sleep(3)
        st = c.Stage(cfg["window"]["exe"], cfg["window"]["title_kw"])
        st.enter()
        try:
            c.click(p["center"][0], p["center"][1], hold=0.05)
        finally:
            time.sleep(0.5)
            st.leave()
        log("已点一下。没反应 = 权限不够（先跑 0-体检.bat）")
        log.close()
        return 0

    # 真跑：权限不对就自己提权重来（不再让主人手动右键）
    if not args.dry_run and perm_problem() and not os.environ.get("YHF_LAUNCHED"):
        perm_help()
        ok, why = c.relaunch_as_admin()
        print(f"[>] 自动提权: {why}")
        if ok:
            print("    马上会弹一个【新的黑窗口】，那才是真正干活的；这个窗口可以关掉。")
            print("    如果 UAC 弹窗被关掉了，就右键 3-开始刷.bat → 以管理员身份运行。")
            return 3
        print("    手动办法：右键 3-开始刷.bat → 以管理员身份运行。")
        return 2

    log = Log("farm")
    mode = "dry" if args.dry_run else ("teach" if args.teach else "run")
    log(f"启动 mode={mode} rounds={args.rounds or '∞'} 配置={os.path.basename(args.config)}")

    bot = Bot(cfg, log, dry=args.dry_run, shots_every=args.shots)
    if args.teach:
        log("教学模式：跑到钱满的那一刻按 F8，以后它就能自己认出来")
        orig_one = bot.one_round

        def one_round_teach(idx, watcher):
            # 每 0.2 秒看一眼 F8
            import threading
            stop_flag = {"v": False}

            def watcher_thread():
                while not stop_flag["v"] and not bot.stopped:
                    if bot.hk.edge("teach"):
                        if watcher.teach():
                            log("[SHOT] 已记住“满”的样子 -> full_ref.png")
                            c.beep(1400, 150)
                        else:
                            log("[!] 没法记录：没标定 money 区域")
                    time.sleep(0.15)

            t = threading.Thread(target=watcher_thread, daemon=True)
            t.start()
            try:
                orig_one(idx, watcher)
            finally:
                stop_flag["v"] = True

        bot.one_round = one_round_teach

    try:
        bot.run(max_rounds=args.rounds, auto=args.auto)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，停")
    finally:
        log.close()

    # 跑完/被停，弹个窗口告诉主人（试运行不弹，免得太吵）
    if not args.dry_run:
        notify_done(bot.rounds_done, bot.stopped)
    return 0


if __name__ == "__main__":
    raise SystemExit(c.run_main(main))
