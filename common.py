# -*- coding: utf-8 -*-
"""异环 · 店长特供自动刷 —— 底层能力层

只依赖 numpy + Pillow + Windows 自带 API（ctypes），不装 cv2/pyautogui，离线可用。

提供：
    get_game_window()   找游戏窗口
    force_foreground()  把游戏抢到前台（不抢前台的话点不到）
    grab()              截屏（整屏 / 指定区域）→ numpy RGB
    click() / press_key()  发送鼠标/键盘（SendInput，UE 游戏能收到）
    Hotkey              全局热键轮询
    Template            模板匹配（FFT + 积分图算 NCC，纯 numpy）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time

import numpy as np
from PIL import Image, ImageGrab

# ---------------------------------------------------------------- DPI / 句柄
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

try:  # 让坐标和屏幕像素 1:1，避免缩放导致点歪
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


# ---------------------------------------------------------------- 窗口
def _exe_of_pid(pid: int) -> str:
    """取进程可执行文件名（不需要 psutil）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def list_windows() -> list[dict]:
    """枚举所有可见顶层窗口。"""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({
            "hwnd": hwnd, "title": title,
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "w": rect.right - rect.left, "h": rect.bottom - rect.top,
            "pid": pid.value,
        })
        return True

    user32.EnumWindows(_cb, 0)
    return out


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕上的矩形 (x1, y1, x2, y2)。无边框全屏时等于窗口矩形。"""
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom)


def get_game_window(exe: str = "HTGame.exe", title_kw: str = "异环", min_size: int = 400):
    """按 exe 名优先、标题兜底找游戏窗口；多个候选取面积最大的。

    返回 {'hwnd','title','rect'(窗口矩形),'client'(客户区矩形),'w','h','pid'}，找不到返回 None。
    'client' 才是截屏和点击该用的坐标系。
    """
    wins = [w for w in list_windows() if w["w"] >= min_size and w["h"] >= min_size]
    by_exe = [w for w in wins if _exe_of_pid(w["pid"]).lower() == exe.lower()]
    cand = by_exe or [w for w in wins if title_kw and title_kw in w["title"]]
    if not cand:
        return None
    win = max(cand, key=lambda w: w["w"] * w["h"])
    win["client"] = client_rect(win["hwnd"])
    win["cw"] = win["client"][2] - win["client"][0]
    win["ch"] = win["client"][3] - win["client"][1]
    return win


def force_foreground(hwnd: int, tries: int = 3) -> bool:
    """把窗口切到前台。后台进程 SetForegroundWindow 常被系统挡，用 AttachThreadInput 绕过。"""
    if user32.GetForegroundWindow() == hwnd:
        return True
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    fg = user32.GetForegroundWindow()
    t_fg = user32.GetWindowThreadProcessId(fg, None)
    t_me = kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(t_me, t_fg, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(t_me, t_fg, False)

    for _ in range(tries):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.15)
        user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


def is_foreground(hwnd: int) -> bool:
    return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------- 权限
def is_elevated(pid: int | None = None) -> bool:
    """当前进程（或指定 pid）是不是管理员权限。

    异环默认是以管理员启动的；脚本不是管理员的话，SendInput 的点击会被
    Windows 的 UIPI 直接丢掉（返回成功、但游戏收不到），表现为"点了没反应"。

    注意：这里所有句柄都必须声明成 c_void_p。以前用默认的 c_int 接
    HANDLE，64 位下被截断成 32 位，导致**提权之后也读成"没提权"**，
    进而无限自我提权套娃。
    """
    TOKEN_QUERY = 0x0008
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 自己：用 shell 的官方判定，最稳
    if pid is None:
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.IsUserAnAdmin.restype = wt.BOOL
            return bool(shell32.IsUserAnAdmin())
        except Exception:
            pid = k32.GetCurrentProcessId()

    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = wt.BOOL
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = wt.BOOL
    advapi.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                           wt.DWORD, ctypes.POINTER(wt.DWORD)]
    advapi.GetTokenInformation.restype = wt.BOOL

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wt.DWORD)]

    h = k32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        tok = ctypes.c_void_p()
        if not advapi.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return False
        try:
            te = TOKEN_ELEVATION()
            size = wt.DWORD()
            if not advapi.GetTokenInformation(tok, 20, ctypes.byref(te),
                                              ctypes.sizeof(te), ctypes.byref(size)):
                return False
            return bool(te.TokenIsElevated)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- 搭台
class Stage:
    """把游戏顶到最前面，顺手把浏览器收起来；退出时原样还原。

    为什么要收浏览器：DeepSeek Harness 的网页会在后台抢焦点，
    几百毫秒内就能把游戏重新压下去，导致截到的是浏览器、点到的也是浏览器。
    """

    SW_MINIMIZE, SW_RESTORE = 6, 9
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x1, 0x2, 0x40

    def __init__(self, exe="HTGame.exe", title_kw="异环", minimize_browsers=True):
        self.exe = exe
        self.title_kw = title_kw
        self.minimize_browsers = minimize_browsers
        self.win = None
        self.browsers: list[int] = []
        self.active = False

    def _find_browsers(self) -> list[int]:
        out = []
        for w in list_windows():
            if w["w"] < 500 or w["h"] < 400:
                continue
            if _exe_of_pid(w["pid"]).lower() in ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe"):
                out.append(w["hwnd"])
        return out

    def enter(self) -> bool:
        self.win = get_game_window(self.exe, self.title_kw)
        if not self.win:
            return False
        if self.minimize_browsers:
            self.browsers = self._find_browsers()
            for b in self.browsers:
                user32.ShowWindow(wt.HWND(b), self.SW_MINIMIZE)
        user32.SetWindowPos(wt.HWND(self.win["hwnd"]), wt.HWND(self.HWND_TOPMOST), 0, 0, 0, 0,
                            self.SWP_NOSIZE | self.SWP_NOMOVE | self.SWP_SHOWWINDOW)
        force_foreground(self.win["hwnd"])
        time.sleep(0.15)
        self.active = force_foreground(self.win["hwnd"])
        self.win = get_game_window(self.exe, self.title_kw)   # 位置可能变了，重新取
        return self.active

    def leave(self) -> None:
        if self.win:
            user32.SetWindowPos(wt.HWND(self.win["hwnd"]), wt.HWND(self.HWND_NOTOPMOST), 0, 0, 0, 0,
                                self.SWP_NOSIZE | self.SWP_NOMOVE)
        for b in self.browsers:
            user32.ShowWindow(wt.HWND(b), self.SW_RESTORE)
            user32.BringWindowToTop(wt.HWND(b))
        self.browsers = []
        self.active = False

    def __enter__(self):
        self.enter()
        return self

    def __exit__(self, *exc):
        self.leave()
        return False



# ---------------------------------------------------------------- 截屏
def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) 整个虚拟桌面（多屏也行）。"""
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x = y = 0
    return x, y, w, h


def grab(bbox: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """截屏，返回 RGB 的 numpy 数组 (H, W, 3)。bbox=(x1,y1,x2,y2)。"""
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    return np.asarray(img.convert("RGB"))


def save_shot(path: str, arr: np.ndarray | None = None, bbox=None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if arr is None:
        arr = grab(bbox)
    Image.fromarray(arr).save(path)
    return path


def to_gray(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        return arr.astype(np.float32)
    return (arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114).astype(np.float32)


# ---------------------------------------------------------------- 输入
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, KEYEVENTF_EXTENDED = 0x0002, 0x0008, 0x0001
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(inp: INPUT) -> None:
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _to_abs(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    ax = int(round((x - vx) * 65535 / max(1, vw - 1)))
    ay = int(round((y - vy) * 65535 / max(1, vh - 1)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def move_to(x: int, y: int) -> None:
    ax, ay = _to_abs(x, y)
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
    _send(inp)


_BTN = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left", hold: float = 0.03, settle: float = 0.06) -> None:
    """先移到坐标再按下抬起。settle 是移动后的停顿，很多游戏需要它才认。"""
    down, up = _BTN.get(button, _BTN["left"])
    move_to(x, y)
    if settle:
        time.sleep(settle)
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(0, 0, 0, down, 0, 0)
    _send(inp)
    time.sleep(max(0.005, hold))
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(0, 0, 0, up, 0, 0)
    _send(inp)


VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
for i in range(1, 13):
    VK[f"f{i}"] = 0x6F + i
for c in "0123456789":
    VK[c] = ord(c)
for c in "abcdefghijklmnopqrstuvwxyz":
    VK[c] = ord(c.upper())


def vk_of(name: str) -> int:
    key = str(name).strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        return ord(key.upper())
    if key.startswith("0x"):
        return int(key, 16)
    raise KeyError(f"不认识这个键: {name}")


def key_down(name: str) -> None:
    vk = vk_of(name)
    scan = user32.MapVirtualKeyW(vk, 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE, 0, 0)
    _send(inp)


def key_up(name: str) -> None:
    vk = vk_of(name)
    scan = user32.MapVirtualKeyW(vk, 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)
    _send(inp)


def press_key(name: str, hold: float = 0.04) -> None:
    key_down(name)
    time.sleep(max(0.005, hold))
    key_up(name)


# ---------------------------------------------------------------- 热键
class Hotkey:
    """轮询式热键：比装 keyboard 库稳，也不吃麦克风权限。"""

    def __init__(self, mapping: dict[str, str] | None = None):
        self.map = {k: vk_of(v) for k, v in (mapping or {}).items()}
        self._armed = {k: True for k in self.map}

    def edge(self, action: str) -> bool:
        """这一轮是否刚按下（按下瞬间返回 True 一次）。"""
        vk = self.map.get(action)
        if not vk:
            return False
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        if down and self._armed.get(action, True):
            self._armed[action] = False
            return True
        if not down:
            self._armed[action] = True
        return False

    def held(self, action: str) -> bool:
        vk = self.map.get(action)
        return bool(vk and (user32.GetAsyncKeyState(vk) & 0x8000))


# ---------------------------------------------------------------- 模板匹配
def _box_sum(a: np.ndarray, th: int, tw: int) -> np.ndarray:
    """滑窗求和，返回 (H-th+1, W-tw+1)。"""
    ii = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = np.cumsum(np.cumsum(a, axis=0, dtype=np.float64), axis=1)
    return ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]


def _resize(arr: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return arr
    im = Image.fromarray(arr if arr.dtype == np.uint8 else arr.astype(np.uint8))
    w = max(1, int(round(im.width * scale)))
    h = max(1, int(round(im.height * scale)))
    return np.asarray(im.resize((w, h), Image.BILINEAR))


def match_template(hay: np.ndarray, tpl: np.ndarray, scale: float = 1.0) -> tuple[int, int, float]:
    """归一化互相关匹配 (等价 cv2.TM_CCOEFF_NORMED)，纯 numpy/FFT 实现。

    scale<1 时先降采样再匹配（快 4~16 倍，坐标已换算回原图）。
    返回 (x, y, score)，score ∈ [-1, 1]；坐标是模板**中心**在 hay 里的像素位置。
    找不到（模板比图大）返回 (-1, -1, -1.0)。
    """
    if scale != 1.0:
        hay = _resize(hay, scale)
        tpl = _resize(tpl, scale)

    I = to_gray(hay)
    T = to_gray(tpl)
    th, tw = T.shape
    H, W = I.shape
    if th > H or tw > W or th < 2 or tw < 2:
        return -1, -1, -1.0

    fh, fw = H + th - 1, W + tw - 1
    FI = np.fft.rfft2(I, s=(fh, fw))
    FT = np.fft.rfft2(T[::-1, ::-1], s=(fh, fw))
    full = np.fft.irfft2(FI * FT, s=(fh, fw))
    # 卷积的"合法区"从 (th-1, tw-1) 开始，不是 0
    corr = full[th - 1: H, tw - 1: W]

    s1 = _box_sum(I, th, tw)
    s2 = _box_sum(I * I, th, tw)
    n = float(th * tw)
    sum_t = float(T.sum())
    var_t = float((T * T).sum() - (sum_t ** 2) / n)

    num = corr - s1 * (sum_t / n)
    var_i = np.maximum(s2 - (s1 * s1) / n, 0.0)
    den = np.sqrt(var_i * max(var_t, 1e-9))
    score = np.where(den > 1e-6, num / np.maximum(den, 1e-6), -1.0)
    score = np.clip(score, -1.0, 1.0)

    idx = int(np.argmax(score))
    y, x = divmod(idx, score.shape[1])
    cx = int(round((x + tw / 2.0) / scale))
    cy = int(round((y + th / 2.0) / scale))
    return cx, cy, float(score[y, x])


def color_close(a: np.ndarray, b: np.ndarray, tol: int = 24) -> bool:
    """两点颜色是否接近（判断某个像素是不是"变了颜色"）。"""
    return bool(np.max(np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))) <= tol)


# ---------------------------------------------------------------- 提权 / 崩溃日志
def relaunch_as_admin() -> tuple[bool, str]:
    """用管理员身份重新启动当前脚本，返回 (是否已发起, 说明)。

    两个坑都踩过，这里都绕开了：
      1) 提权后新进程的工作目录会变成 C:\\Windows\\System32，
         所以用 cmd /c 先 cd /d 回脚本目录；
      2) 新窗口一闪而过看不到报错，所以把控制台输出重定向进 logs\\console.log。
    """
    if is_elevated():
        return False, "已经是管理员了"
    import subprocess
    import sys
    script = os.path.abspath(sys.argv[0])
    here = os.path.dirname(script)
    log_dir = os.path.join(here, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    out = os.path.join(log_dir, "console.log")
    inner = subprocess.list2cmdline([sys.executable, script] + sys.argv[1:])
    params = f'/c cd /d "{here}" && set YHF_LAUNCHED=1 && {inner} >> "{out}" 2>&1'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", params, here, 1)
    except Exception as e:
        return False, f"调用提权失败: {e}"
    if rc > 32:
        return True, f"已提权启动（输出会写进 logs\\console.log）"
    return False, {5: "提权被取消（UAC 里点了否）", 2: "找不到文件",
                   31: "无法打开目标"}.get(int(rc), f"提权失败 code={rc}")


def crash_log(exc: BaseException | None = None) -> str:
    """把异常写进 logs/crash_*.log，方便事后照着修。"""
    import sys
    import traceback
    log_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv else ".")), "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"crash_{time.strftime('%Y%m%d_%H%M%S')}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"脚本: {sys.argv[0]}\n参数: {sys.argv[1:]}\n")
        f.write(f"Python: {sys.version}\n管理员: {is_elevated()}\n")
        f.write("-" * 60 + "\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__ if exc else None, file=f)
    return path


def run_main(main_fn) -> int:
    """包一层：崩了就把错误存文件并打印出来，不让窗口一闪而过。"""
    import sys
    import traceback
    try:
        rc = main_fn()
        return 0 if rc is None else int(rc)
    except SystemExit:
        raise
    except BaseException as e:          # noqa: BLE001
        try:
            p = crash_log(e)
            print("\n" + "!" * 60)
            print(f"[崩溃] 错误已存到: {p}")
            print("       把这个文件发给主人我，我照着修。")
            print("!" * 60)
        except Exception:
            pass
        traceback.print_exc()
        return 1
    finally:
        if sys.stdout is not None:
            try:
                sys.stdout.flush()
            except Exception:
                pass



# ---------------------------------------------------------------- 小工具
def human_sleep(a: float, b: float | None = None) -> None:
    """带点随机性的等待，避免节拍太机械。"""
    if b is None:
        time.sleep(a)
    else:
        time.sleep(np.random.uniform(min(a, b), max(a, b)))


def jitter_rect(area: tuple[int, int, int, int], pad: int = 0) -> tuple[int, int]:
    x1, y1, x2, y2 = area
    x1, y1 = x1 + pad, y1 + pad
    x2, y2 = x2 - pad, y2 - pad
    return int(np.random.uniform(x1, max(x1 + 1, x2))), int(np.random.uniform(y1, max(y1 + 1, y2)))


def beep(freq: int = 880, ms: int = 90) -> None:
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


def now() -> str:
    return time.strftime("%H:%M:%S")
