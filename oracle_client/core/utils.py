"""通用小工具。"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_number(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}"


def format_size(num: Optional[int]) -> str:
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", (name or "").strip())[:80] or "未命名"


def clip_text(text: str, limit: int = 400) -> str:
    t = (text or "").replace("\n", " ").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def copy_to_clipboard(root, text: str) -> bool:
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        return True
    except Exception:
        return False


def open_path(path: str):
    """用系统默认程序打开文件或文件夹。"""
    try:
        p = str(path)
        if os.path.isdir(p):
            subprocess.Popen(["explorer", p])
        else:
            os.startfile(p)  # noqa: S606 - Windows 专用
    except Exception:
        try:
            subprocess.Popen(["explorer", str(path)])
        except Exception:
            pass


def reveal_in_explorer(path: str):
    try:
        subprocess.Popen(["explorer", "/select,", str(Path(path).resolve())])
    except Exception:
        open_path(str(Path(path).parent))


def center_on_screen(win, width: int = None, height: int = None):
    """把窗口居中显示。"""
    try:
        win.update_idletasks()
        w = width or win.winfo_width()
        h = height or win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass


def _scrolled_content_size(win):
    """窗口内可滚动主体的内容尺寸（canvas 不会把内容尺寸传递给父窗口）。"""
    sizes = []
    stack = list(win.winfo_children())
    while stack:
        c = stack.pop()
        try:
            canvas = getattr(c, "canvas", None)
            if canvas is not None and hasattr(c, "inner"):
                c.update_idletasks()
                bb = canvas.bbox("all")
                if bb:
                    sizes.append((bb[2] - bb[0], bb[3] - bb[1]))
                continue
        except Exception:
            pass
        try:
            stack.extend(c.winfo_children())
        except Exception:
            pass
    return sizes


def fit_to_content(win, parent=None, min_w: int = 520, min_h: int = 380,
                   max_w_ratio: float = 0.92, max_h_ratio: float = 0.9,
                   recenter: bool = True):
    """按内容实际需要的尺寸撑开窗口，再居中；装不下屏幕时才滚动。

    解决“对话框内容显示不全，必须手动拉大”的问题。
    返回 (w, h, 是否需要滚动)。
    """
    try:
        win.update_idletasks()
        rw, rh = win.winfo_reqwidth(), win.winfo_reqheight()
        for cw, ch in _scrolled_content_size(win):
            rw = max(rw, cw + 12)
            rh = max(rh, ch + 12)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = max(int(rw), min_w) + 24
        h = max(int(rh), min_h) + 24
        w = min(w, int(sw * max_w_ratio))
        h = min(h, int(sh * max_h_ratio))
        win.geometry(f"{w}x{h}")
        win.update_idletasks()
        if recenter:
            center_on_parent(win, parent, w, h)
        else:
            # 只调整大小，保持用户拖动过的位置
            try:
                x, y = win.winfo_x(), win.winfo_y()
                x = max(0, min(x, sw - w))
                y = max(0, min(y, sh - h))
                win.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass
        return w, h, int(rh) + 24 > int(sh * max_h_ratio)
    except Exception:
        return 0, 0, False


def center_on_parent(win, parent=None, width: int = None, height: int = None):
    try:
        win.update_idletasks()
        w = width or win.winfo_width()
        h = height or win.winfo_height()
        if parent is not None:
            try:
                px, py = parent.winfo_rootx(), parent.winfo_rooty()
                pw, ph = parent.winfo_width(), parent.winfo_height()
                x = px + max(0, (pw - w) // 2)
                y = py + max(0, (ph - h) // 3)
            except Exception:
                x = y = None
        else:
            x = y = None
        if x is None:
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        # 保证整个窗口落在屏幕内（尤其是小屏/窗口很大时）
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass


class Stopwatch:
    def __init__(self):
        self.t0 = time.time()

    @property
    def elapsed(self) -> float:
        return time.time() - self.t0

    def text(self) -> str:
        e = self.elapsed
        if e < 1:
            return f"{e * 1000:.0f} ms"
        return f"{e:.2f} s"


def chunked(items: Iterable[Any], size: int) -> Iterable[List[Any]]:
    buf: List[Any] = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def dedup(seq: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def truncate_middle(text: str, limit: int = 60) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    head = limit // 2
    return t[:head] + " … " + t[-(limit - head):]
