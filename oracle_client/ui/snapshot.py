"""数据库档案（文本快照）：把"库长什么样"写成一段可以复制出去的文本。

内网没有外网，求助只能靠"把文本搬出去"。这个模块负责两件事：
  1. 档案窗口：主动排查时，选好范围生成一份结构档案，复制或存成 .md；
  2. 报错对话框：出错时给一个「复制问题上下文」，把报错 + SQL + 相关表结构 + 权限
     一起打包，人直接贴出去就行。

安全底线：档案里只有结构元数据，没有任何业务数据行，也没有连接口令。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog
from typing import Any, Dict, List, Optional

import ttkbootstrap as tb

from ..core import perms as PM
from ..core import snapshot as SNAP
from ..core import utils as U
from . import widgets as W
from .windows import BaseSubWindow

LEVELS = {"精简（只要对象清单）": "brief", "标准（对象清单 + 表结构 + 运行状况）": "standard"}


class SnapshotWindow(BaseSubWindow):
    window_kind = "数据库档案"

    def __init__(self, app, session_name: str = "", preset: Optional[Dict[str, Any]] = None):
        preset = preset or {}
        self.preset = preset
        super().__init__(app, "数据库档案（离线求助用）", session_name, width=1080, height=740)
        self.text = preset.get("text") or ""
        self._build()
        U.center_on_parent(self, app.root, 1080, 740)
        if preset.get("auto_generate"):
            self.after(150, self.generate)

    # ================================================================ 界面
    def _build(self):
        toolbar = W.Toolbar(self, padding=(8, 4))
        toolbar.pack(fill=tk.X)
        self._add_session_selector(toolbar, on_change=self._on_session_change)
        toolbar.add_sep()
        toolbar.add_button("生成档案", self.generate, "success")
        toolbar.add_button("复制全文", self.copy_text, "primary")
        toolbar.add_button("保存为 .md", self.save_text, "info")
        toolbar.add_spacer()

        opt = tb.Labelframe(self, text="档案范围", padding=10)
        opt.pack(fill=tk.X, padx=8, pady=(6, 4))

        self.level_var = tk.StringVar(value=list(LEVELS.keys())[-1])
        self.schema_var = tk.StringVar(value=(self.preset.get("schema") or ""))
        self.kw_var = tk.StringVar(value=(self.preset.get("keyword") or ""))
        self.max_var = tk.StringVar(value=str(self.preset.get("max_tables", 30)))
        self.exact_var = tk.BooleanVar(value=False)
        self.ddl_var = tk.BooleanVar(value=False)
        self.ops_var = tk.BooleanVar(value=True)

        row = tb.Frame(opt)
        row.pack(fill=tk.X)
        tb.Label(row, text="篇幅").pack(side=tk.LEFT)
        tb.Combobox(row, textvariable=self.level_var, values=list(LEVELS.keys()),
                    width=34, state="readonly").pack(side=tk.LEFT, padx=(4, 14))
        tb.Label(row, text="schema").pack(side=tk.LEFT)
        self.schema_combo = tb.Combobox(row, textvariable=self.schema_var, values=[], width=22)
        self.schema_combo.pack(side=tk.LEFT, padx=(4, 14))
        tb.Button(row, text="读取", bootstyle="secondary-outline", command=self._load_schemas,
                  width=6).pack(side=tk.LEFT)

        row2 = tb.Frame(opt)
        row2.pack(fill=tk.X, pady=(8, 0))
        tb.Label(row2, text="表名包含").pack(side=tk.LEFT)
        tb.Entry(row2, textvariable=self.kw_var, width=18).pack(side=tk.LEFT, padx=(4, 14))
        tb.Label(row2, text="最多收录").pack(side=tk.LEFT)
        tk.Spinbox(row2, from_=1, to=500, textvariable=self.max_var, width=6).pack(side=tk.LEFT, padx=(4, 2))
        tb.Label(row2, text="个对象").pack(side=tk.LEFT, padx=(0, 14))
        tb.Checkbutton(row2, text="精确行数（会跑 COUNT(*)，大表慢）", variable=self.exact_var,
                       bootstyle="warning-round-toggle").pack(side=tk.LEFT, padx=(0, 12))
        tb.Checkbutton(row2, text="含建表语句", variable=self.ddl_var,
                       bootstyle="primary-round-toggle").pack(side=tk.LEFT, padx=(0, 12))
        tb.Checkbutton(row2, text="含运行状况", variable=self.ops_var,
                       bootstyle="primary-round-toggle").pack(side=tk.LEFT)

        row3 = tb.Frame(opt)
        row3.pack(fill=tk.X, pady=(8, 0))
        tb.Label(row3, text="问题描述").pack(side=tk.LEFT)
        self.problem_var = tk.StringVar(value=(self.preset.get("problem") or ""))
        tb.Entry(row3, textvariable=self.problem_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        tip = tb.Label(self, text="档案只含结构元数据（表/字段/索引/约束/行数），不含任何业务数据行，可放心外发。",
                       bootstyle="secondary")
        tip.pack(anchor="w", padx=10, pady=(2, 0))

        body = tb.Frame(self, padding=(8, 6))
        body.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(body, wrap="none", font=("Consolas", 10))
        sb_y = tb.Scrollbar(body, orient=tk.VERTICAL, command=self.txt.yview, bootstyle="secondary-round")
        sb_x = tb.Scrollbar(body, orient=tk.HORIZONTAL, command=self.txt.xview, bootstyle="secondary-round")
        self.txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.statusbar = W.StatusBar(self, parts=2, padding=(8, 2))
        self.statusbar.pack(fill=tk.X)
        self.statusbar.set(0, "点“生成档案”开始")
        self._load_schemas()

    def _load_schemas(self):
        s = self.session
        if not s:
            return
        try:
            names = list(s.schemas())
        except Exception:  # noqa: BLE001
            names = []
        cur = self.schema_var.get().strip()
        if not cur:
            try:
                cur = s.default_schema or ""
            except Exception:  # noqa: BLE001
                cur = ""
        if cur and cur not in names:
            names.insert(0, cur)
        self.schema_combo.configure(values=names)
        self.schema_var.set(cur)

    def _on_session_change(self):
        self.schema_var.set("")
        self._load_schemas()

    # ================================================================ 生成
    def _collect_options(self) -> SNAP.SnapshotOptions:
        """注意：不能叫 _options，tkinter 的 Toplevel 初始化会用到同名方法。"""
        try:
            mx = int(str(self.max_var.get()).strip() or 30)
        except (TypeError, ValueError):
            mx = 30
        return SNAP.SnapshotOptions(
            level=LEVELS.get(self.level_var.get(), "standard"),
            schema=self.schema_var.get().strip(),
            tables=list(self.preset.get("tables") or []),
            max_tables=mx,
            keyword=self.kw_var.get().strip(),
            exact_counts=bool(self.exact_var.get()),
            include_ddl=bool(self.ddl_var.get()),
            include_ops=bool(self.ops_var.get()),
            problem=self.problem_var.get().strip(),
            error_text=self.preset.get("error_text") or "",
            sql_text=self.preset.get("sql_text") or "",
        )

    def generate(self):
        s = self.session
        if not s:
            W.warning(self, "当前没有可用的数据库连接，请先连接数据库")
            return
        opts = self._collect_options()

        def work(ev):
            try:
                probe = PM.probe_for(s)
            except Exception:  # noqa: BLE001
                probe = None
            return SNAP.build(s, probe, opts)

        ok, res = W.run_async(self, work, title="正在生成档案",
                              message="正在读取表结构与权限信息，表多时会慢一些…", cancellable=True)
        if not ok:
            W.error(self, f"生成失败：{res}")
            return
        self.text = str(res)
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", self.text)
        self.txt.configure(state="disabled")
        n = len(self.text)
        self.statusbar.set(0, f"生成完成：{U.human_number(n)} 字符 / 约 {max(1, n // 900)} KB")
        self.statusbar.set(1, "可直接复制或存成 .md")

    def copy_text(self):
        if not self.text.strip():
            W.warning(self, "请先生成档案")
            return
        if U.copy_to_clipboard(self, self.text):
            W.toast(self, "已复制，可直接粘贴给对方", "success")

    def save_text(self):
        if not self.text.strip():
            W.warning(self, "请先生成档案")
            return
        s = self.session
        base = "数据库档案"
        try:
            base = f"数据库档案_{s.profile.name}_{U.now_str()[:10]}"
        except Exception:  # noqa: BLE001
            pass
        path = filedialog.asksaveasfilename(
            parent=self, title="保存档案", defaultextension=".md",
            initialfile=U.safe_filename(base) + ".md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"保存失败：{e}")
            return
        W.toast(self, f"已保存：{os.path.basename(path)}", "success")


# ================================================================== 报错场景
def show_error_with_context(parent, app, message: str, title: str = "出错",
                            sql: str = "", schema: str = "", table: str = "",
                            session_name: str = ""):
    """出错提示框，多给一个「复制问题上下文」按钮。

    出问题时人正站在报错框前，这时候最需要的是"一键把上下文带走"，
    而不是自己回去翻表结构再手打一遍。
    """
    top = tb.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    top.geometry("640x380")
    frm = tb.Frame(top, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    tb.Label(frm, text="执行出错：", bootstyle="danger").pack(anchor="w")
    txt = tk.Text(frm, wrap="word", font=("Microsoft YaHei UI", 10), height=8)
    txt.insert("1.0", str(message or ""))
    txt.configure(state="disabled")
    txt.pack(fill=tk.BOTH, expand=True, pady=(6, 10))

    def close():
        try:
            top.destroy()
        except Exception:  # noqa: BLE001
            pass

    def copy_raw():
        if U.copy_to_clipboard(top, str(message or "")):
            W.toast(top, "报错原文已复制", "success")

    def copy_context():
        name = session_name or getattr(parent, "session_name", "") or app.current_session_name
        s = app.get_session(name)
        if not s:
            W.warning(top, "当前没有可用的数据库连接，无法生成上下文。")
            return
        try:
            probe = PM.probe_for(s)
        except Exception:  # noqa: BLE001
            probe = None
        try:
            ctx = SNAP.problem_context(
                s, probe,
                problem="执行 SQL 时出错，下面是报错原文与相关表结构。",
                error_text=str(message or ""),
                sql_text=sql or "",
                schema=schema or (s.default_schema if s else ""),
                tables=[table] if table else [],
            )
        except Exception as e:  # noqa: BLE001
            W.error(top, f"生成上下文失败：{e}")
            return
        if U.copy_to_clipboard(top, ctx):
            W.toast(top, "问题上下文已复制，可粘贴给对方", "success")
        W.show_text(top, "问题上下文（已复制到剪贴板）", ctx)

    bar = tb.Frame(top, padding=(12, 0, 12, 12))
    bar.pack(fill=tk.X)
    tb.Button(bar, text="复制问题上下文（推荐）", bootstyle="primary", command=copy_context).pack(side=tk.LEFT)
    tb.Button(bar, text="只复制报错原文", bootstyle="secondary-outline", command=copy_raw).pack(side=tk.LEFT, padx=(8, 0))
    tb.Button(bar, text="关闭", bootstyle="secondary", command=close).pack(side=tk.RIGHT)
    U.center_on_parent(top, parent, 640, 380)
    try:
        top.grab_set()
    except Exception:  # noqa: BLE001
        pass
    return top
