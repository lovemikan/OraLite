"""通用界面组件：进度框、提示气泡、复选列表、文本查看器、条件编辑器等。"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Callable, Dict, List, Optional, Sequence

import ttkbootstrap as tb
from ttkbootstrap.constants import *  # noqa: F401,F403
from ttkbootstrap.dialogs import Messagebox

from ..core import utils as U


def _attach_tooltip(widget, text: str):
    """给控件绑定提示气泡（不同版本 ttkbootstrap 位置不同，逐个尝试）。"""
    for mod_path in ("ttkbootstrap.tooltip", "ttkbootstrap.widgets"):
        try:
            mod = __import__(mod_path, fromlist=["ToolTip"])
            ToolTip = getattr(mod, "ToolTip")
            ToolTip(widget, text=text)
            return
        except Exception:
            continue


# ---------------------------------------------------------------- 消息框
def info(parent, message: str, title: str = "提示"):
    try:
        Messagebox.show_info(message, title=title, parent=parent)
    except Exception as e:  # noqa: BLE001
        print(f"[提示框异常] {e}", flush=True)


def warning(parent, message: str, title: str = "注意"):
    try:
        Messagebox.show_warning(message, title=title, parent=parent)
    except Exception as e:  # noqa: BLE001
        print(f"[提示框异常] {e}", flush=True)


def error(parent, message: str, title: str = "出错"):
    try:
        Messagebox.show_error(message, title=title, parent=parent)
    except Exception as e:  # noqa: BLE001
        print(f"[提示框异常] {e}", flush=True)


def ask_yes(parent, message: str, title: str = "请确认") -> bool:
    try:
        return bool(Messagebox.yesno(message, title=title, parent=parent))
    except Exception:
        return False


def ask_yes_no_cancel(parent, message: str, title: str = "请确认") -> Optional[bool]:
    try:
        r = Messagebox.yesnocancel(message, title=title, parent=parent)
        return None if r is None else bool(r)
    except Exception:
        return None


# ---------------------------------------------------------------- 气泡提示
def toast(parent, text: str, kind: str = "info", ms: int = 2600):
    colors = {
        "info": "#2f6fb3", "success": "#2e7d52", "warning": "#b07d12",
        "danger": "#b23b3b", "dark": "#334155",
    }
    bg = colors.get(kind, colors["info"])
    try:
        top = tk.Toplevel(parent)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        frame = tk.Frame(top, bg=bg, padx=1, pady=1)
        frame.pack(fill=tk.BOTH, expand=True)
        lbl = tk.Label(frame, text=text, bg=bg, fg="white",
                       font=("Microsoft YaHei UI", 10), padx=14, pady=8,
                       wraplength=420, justify="left")
        lbl.pack()
        top.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + pw - top.winfo_width() - 24
            y = py + ph - top.winfo_height() - 24
        except Exception:
            x = y = 120
        top.geometry(f"+{max(x, 10)}+{max(y, 10)}")
        top.after(ms, top.destroy)
    except Exception:
        pass


# ---------------------------------------------------------------- 进度框
class ProgressDialog:
    """模态进度框（不确定模式），可选取消按钮。"""

    def __init__(self, parent, title: str = "请稍候", message: str = "正在执行…",
                 cancellable: bool = True):
        self.parent = parent
        self.cancelled = False
        self.top = tb.Toplevel(parent)
        self.top.title(title)
        self.top.transient(parent)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)
        frm = tb.Frame(self.top, padding=18)
        frm.pack(fill=tk.BOTH, expand=True)
        self.msg_var = tk.StringVar(value=message)
        tb.Label(frm, textvariable=self.msg_var, wraplength=380, justify="left").pack(pady=(0, 12))
        self.bar = tb.Progressbar(frm, mode="indeterminate", length=360, bootstyle="primary-striped")
        self.bar.pack()
        try:
            self.bar.start(12)
        except Exception:
            pass
        self.btn = tb.Button(frm, text="取消", bootstyle="secondary-outline", command=self._on_close)
        if cancellable:
            self.btn.pack(pady=(12, 0))
        else:
            self.btn.pack_forget()
        U.center_on_parent(self.top, parent)
        try:
            self.top.grab_set()
        except Exception:
            pass
        self.top.update()

    def update_message(self, text: str):
        try:
            self.msg_var.set(text)
            self.top.update()
        except Exception:
            pass

    def _on_close(self):
        self.cancelled = True
        try:
            self.btn.configure(text="正在取消…", state="disabled")
        except Exception:
            pass

    def close(self):
        try:
            self.bar.stop()
        except Exception:
            pass
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass


def run_async(parent, fn: Callable, title: str = "请稍候", message: str = "正在执行…",
              cancellable: bool = True, on_cancel: Optional[Callable] = None,
              timeout: float = 0.0) -> tuple:
    """在后台线程执行 fn(cancel_event)，返回 (ok, 结果或异常信息)。

    fn 的签名为 fn(event) -> Any，event 是 threading.Event，可用于判断是否请求取消。

    等待方式：用 Tk 自己的事件循环（``wait_variable`` + ``after`` 轮询），
    而不是 ``while ...: parent.update(); sleep()``。后者会反复重入事件处理、
    导致界面卡顿，且关闭父窗口时容易抛 TclError。

    timeout > 0 时，超时后放弃等待并返回失败，避免数据库不响应时整个界面卡死。
    """
    box: Dict[str, Any] = {}
    event = threading.Event()
    finished = threading.Event()

    def worker():
        try:
            box["value"] = fn(event)
        except Exception as e:  # noqa: BLE001
            box["error"] = e
        finally:
            finished.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    dlg = ProgressDialog(parent, title=title, message=message, cancellable=cancellable)
    try:
        host = dlg.top
        flag = tk.BooleanVar(master=host, value=False)
        cancelled = {"sent": False}

        def poll():
            if finished.is_set():
                flag.set(True)
                return
            if dlg.cancelled and not cancelled["sent"]:
                cancelled["sent"] = True
                event.set()
                if on_cancel:
                    try:
                        on_cancel()
                    except Exception:  # noqa: BLE001
                        pass
                dlg.update_message("正在取消，请稍候…")
            host.after(30, poll)

        host.after(30, poll)
        if timeout and timeout > 0:
            def _timeout():
                if not finished.is_set():
                    box["error"] = TimeoutError(f"操作超过 {int(timeout)} 秒仍未完成，已放弃等待")
                    event.set()
                flag.set(True)

            host.after(int(timeout * 1000), _timeout)
        host.wait_variable(flag)
    except Exception as e:  # noqa: BLE001
        box.setdefault("error", e)
    finally:
        dlg.close()
    if "error" in box:
        return False, box["error"]
    if "value" not in box:
        return False, "操作未完成（已取消或超时）"
    return True, box.get("value")


# ---------------------------------------------------------------- 复选列表
class CheckList(tb.Frame):
    """带滚动条的多选列表（用于选择列）。"""

    def __init__(self, master, items: Sequence[str] = (), columns: int = 3,
                 on_change: Optional[Callable] = None, **kw):
        super().__init__(master, **kw)
        self.vars: Dict[str, tk.BooleanVar] = {}
        self._on_change = on_change
        self._columns = max(1, columns)
        canvas = tk.Canvas(self, highlightthickness=0, height=140)
        sb = tkttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = tb.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas = canvas
        self._inner_win = None
        if items:
            self.set_items(items)

    def set_items(self, items: Sequence[str], checked: bool = True):
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars.clear()
        items = list(items)
        rows = (len(items) + self._columns - 1) // max(1, self._columns)
        # 竖排优先，读起来更像表格
        for r in range(rows):
            for c in range(self._columns):
                idx = c * rows + r
                if idx >= len(items):
                    continue
                name = items[idx]
                v = tk.BooleanVar(value=checked)
                self.vars[name] = v
                tb.Checkbutton(self.inner, text=name, variable=v, bootstyle="primary-round-toggle",
                               command=self._fire).grid(
                    row=r, column=c, sticky="w", padx=(0, 14), pady=2
                )
        self.inner.update_idletasks()

    def checked(self) -> List[str]:
        return [k for k, v in self.vars.items() if v.get()]

    def set_checked(self, names: Sequence[str]):
        s = set(n for n in names)
        for k, v in self.vars.items():
            v.set(k in s)

    def all(self):
        for v in self.vars.values():
            v.set(True)

    def none(self):
        for v in self.vars.values():
            v.set(False)

    def _fire(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 文本查看器
def show_text(parent, title: str, content: str, width: int = 900, height: int = 620,
              mono: bool = True):
    top = tb.Toplevel(parent)
    top.title(title)
    # 小屏幕上不要把窗口开得超出可视区域
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    width = min(width, int(sw * 0.92))
    height = min(height, int(sh * 0.9))
    top.geometry(f"{width}x{height}")
    frm = tb.Frame(top, padding=8)
    frm.pack(fill=tk.BOTH, expand=True)

    font = ("Consolas", 11) if mono else ("Microsoft YaHei UI", 10)
    txt = tk.Text(frm, wrap="none", font=font, undo=False)
    sb_y = tkttk.Scrollbar(frm, orient="vertical", command=txt.yview)
    sb_x = tkttk.Scrollbar(frm, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
    txt.grid(row=0, column=0, sticky="nsew")
    sb_y.grid(row=0, column=1, sticky="ns")
    sb_x.grid(row=1, column=0, sticky="ew")
    frm.rowconfigure(0, weight=1)
    frm.columnconfigure(0, weight=1)
    txt.insert("1.0", content or "")
    txt.configure(state="disabled")

    bar = tb.Frame(top, padding=(8, 0, 8, 8))
    bar.pack(fill=tk.X)

    def do_copy():
        if U.copy_to_clipboard(top, content or ""):
            toast(top, "已复制到剪贴板", "success")

    tb.Button(bar, text="复制全文", bootstyle="primary", command=do_copy).pack(side=tk.LEFT)
    tb.Button(bar, text="关闭", bootstyle="secondary", command=top.destroy).pack(side=tk.RIGHT)
    U.center_on_parent(top, parent, width, height)
    return top


# ---------------------------------------------------------------- 可滚动主体
class ScrolledBody(tb.Frame):
    """内容可能超出窗口的主体区：装不下就出现滚动条。

    用法：
        body = ScrolledBody(parent, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        # 之后把控件放进 body.inner
    """

    def __init__(self, master, padding=16, **kwargs):
        super().__init__(master, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar = tb.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview,
                                 bootstyle="secondary-round")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.vbar.set)

        self.inner = tb.Frame(self.canvas, padding=padding)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._sync)
        # 鼠标进入才接管滚轮，离开立即释放，避免影响其它窗口
        for wgt in (self.canvas, self.inner):
            wgt.bind("<Enter>", self._wheel_on)
            wgt.bind("<Leave>", self._wheel_off)

    def _sync(self, _e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            # 至少给内容它“自然需要”的宽度，避免被压窄后窗口算小、内容显示不全
            need = self.inner.winfo_reqwidth()
            cw = self.canvas.winfo_width()
            self.canvas.itemconfigure(self._win_id, width=max(cw, need))
        except Exception:
            pass

    def _scroll(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def _wheel_on(self, _e=None):
        try:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
        except Exception:
            pass

    def _wheel_off(self, _e=None):
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass


# ---------------------------------------------------------------- 简易表单
class FormBuilder:
    """快速生成“标签 + 输入”表单。"""

    def __init__(self, master, label_width: int = 14):
        self.master = master
        self.row = 0
        self.fields: Dict[str, Any] = {}
        self.label_width = label_width

    def _label(self, text):
        lbl = tb.Label(self.master, text=text, width=self.label_width, anchor="e")
        lbl.grid(row=self.row, column=0, sticky="e", padx=(0, 8), pady=4)
        return lbl

    def entry(self, key: str, label: str, value: str = "", width: int = 34,
              password: bool = False, hint: str = ""):
        self._label(label)
        var = tk.StringVar(value=value)
        e = tb.Entry(self.master, textvariable=var, width=width, show="•" if password else "")
        e.grid(row=self.row, column=1, sticky="we", pady=4)
        if hint:
            # 加 wraplength：长提示自动换行，而不是把窗口撑得很宽
            tb.Label(self.master, text=hint, bootstyle="secondary", wraplength=300,
                     justify="left").grid(row=self.row, column=2, sticky="w", padx=(8, 0))
        self.fields[key] = var
        self.row += 1
        return e

    def spin(self, key: str, label: str, value: int = 1521, low: int = 1, high: int = 65535,
             width: int = 10):
        self._label(label)
        var = tk.IntVar(value=value)
        tb.Spinbox(self.master, from_=low, to=high, textvariable=var, width=width).grid(
            row=self.row, column=1, sticky="w", pady=4)
        self.fields[key] = var
        self.row += 1

    def combo(self, key: str, label: str, values: Sequence[str], value: str = "", width: int = 32):
        self._label(label)
        var = tk.StringVar(value=value)
        cb = tb.Combobox(self.master, textvariable=var, values=list(values), width=width, state="readonly")
        cb.grid(row=self.row, column=1, sticky="we", pady=4)
        self.fields[key] = var
        self.row += 1
        return cb

    def check(self, key: str, label: str, value: bool = False, text: str = ""):
        self._label(label)
        var = tk.BooleanVar(value=value)
        tb.Checkbutton(self.master, text=text or label, variable=var, bootstyle="primary-round-toggle").grid(
            row=self.row, column=1, sticky="w", pady=4)
        self.fields[key] = var
        self.row += 1

    def text(self, key: str, label: str, value: str = "", height: int = 4, width: int = 40):
        self._label(label)
        # 不指定宽度时 tk.Text 默认 80 字符，会把窗口撑得很宽
        txt = tk.Text(self.master, height=height, width=width, wrap="word",
                      font=("Microsoft YaHei UI", 10))
        txt.insert("1.0", value or "")
        txt.grid(row=self.row, column=1, sticky="we", pady=4)
        self.fields[key] = txt
        self.row += 1
        return txt

    def get(self, key: str) -> str:
        f = self.fields.get(key)
        if f is None:
            return ""
        if isinstance(f, tk.Text):
            return f.get("1.0", "end-1c")
        return str(f.get())


# ---------------------------------------------------------------- 工具栏按钮
class Toolbar(tb.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._col = 0

    def add_button(self, text: str, command, bootstyle="secondary", width=None, tooltip: str = ""):
        b = tb.Button(self, text=text, command=command, bootstyle=bootstyle, width=width)
        b.grid(row=0, column=self._col, padx=(0, 6), pady=4)
        if tooltip:
            _attach_tooltip(b, tooltip)
        self._col += 1
        return b

    def add_sep(self):
        tkttk.Separator(self, orient="vertical").grid(row=0, column=self._col, sticky="ns", padx=8, pady=4)
        self._col += 1

    def add_label(self, text: str, bootstyle="secondary"):
        lbl = tb.Label(self, text=text, bootstyle=bootstyle)
        lbl.grid(row=0, column=self._col, padx=(0, 6))
        self._col += 1
        return lbl

    def add_check(self, text: str, variable, command=None, bootstyle="primary-round-toggle"):
        c = tb.Checkbutton(self, text=text, variable=variable, command=command, bootstyle=bootstyle)
        c.grid(row=0, column=self._col, padx=(0, 6))
        self._col += 1
        return c

    def add_combo(self, variable, values, width=20, command=None):
        cb = tb.Combobox(self, textvariable=variable, values=list(values), width=width, state="readonly")
        cb.grid(row=0, column=self._col, padx=(0, 6))
        if command:
            cb.bind("<<ComboboxSelected>>", lambda e: command())
        self._col += 1
        return cb

    def add_widget(self, widget, padx=(0, 6), **kw):
        """把自定义控件放进工具栏（内部统一用 grid 布局）。"""
        widget.grid(row=0, column=self._col, padx=padx, pady=4, **kw)
        self._col += 1
        return widget

    def add_spacer(self):
        lbl = tb.Label(self, text="")
        lbl.grid(row=0, column=self._col, sticky="ew")
        self.columnconfigure(self._col, weight=1)
        self._col += 1
        return lbl


# ---------------------------------------------------------------- 状态栏
class StatusBar(tb.Frame):
    def __init__(self, master, parts: int = 3, **kw):
        super().__init__(master, **kw)
        self.vars: List[tk.StringVar] = []
        for i in range(parts):
            v = tk.StringVar(value="")
            lbl = tb.Label(self, textvariable=v, anchor="w", bootstyle="secondary")
            if i == parts - 1:
                lbl.pack(side=tk.RIGHT, padx=(10, 6))
            else:
                lbl.pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=(i == 0))
            self.vars.append(v)

    def set(self, index: int, text: str):
        try:
            self.vars[index].set(text)
        except Exception:
            pass
