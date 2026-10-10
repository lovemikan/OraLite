"""运维 SQL 模板库的界面：选模板 → 填参数 → 看权限 → 执行。

界面刻意做成“先看清要跑什么，再决定要不要跑”：
  * 左侧选模板，右侧同时显示渲染后的完整 SQL、每个参数的含义、当前账号能不能跑；
  * 权限不够时按钮直接禁用并写明缺什么，而不是让人点了才报 ORA-00942；
  * 结果区由宿主决定（运维概览填到自己的表格，查询窗口插进编辑器）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import ttkbootstrap as tb

from ..core import perms as PM
from ..core import templates as TP
from ..core import utils as U
from . import widgets as W
from .editor import SqlEditor

# 模板执行的结果上限：防止漏写 ROWNUM 的模板把整张动态视图拉回来
TEMPLATE_MAX_ROWS = 2000


class TemplatePicker(tb.Frame):
    """模板选择 + 参数填写 + SQL 预览。

    callbacks 可包含：
      run(cols, rows, note, template)   执行完成后的结果处理
      insert(sql, template)             插入到某处（查询窗口用它写进编辑器）
    两个都不给时，“执行”按钮不出现，只保留复制和打开查询窗口。
    """

    def __init__(self, master, app, session_name: str = "",
                 callbacks: Optional[Dict[str, Callable]] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        super().__init__(master)
        self.app = app
        self.session_name = session_name or app.current_session_name
        self.callbacks = callbacks or {}
        self.on_status = on_status
        self.store: TP.TemplateStore = getattr(app, "templates", None) or TP.TemplateStore()
        self.current: Optional[TP.Template] = None
        self.param_vars: Dict[str, Any] = {}
        self._hidden_by_version = 0
        self._note = ""
        self._resolved_ok = False
        self._use_fallback = False

        self._build()
        self.refresh_list()
        self._sync_perm()

    # ================================================================ 界面
    def _build(self):
        body = tb.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)

        # ---- 左侧：搜索 / 分类 / 列表 ----
        left = tb.Frame(body, width=340)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        left.pack_propagate(False)

        self.search_var = tk.StringVar()
        e = tb.Entry(left, textvariable=self.search_var, width=30)
        e.pack(fill=tk.X, pady=(0, 6))
        e.insert(0, "")
        tb.Label(left, text="搜索模板名 / 用途 / 视图名", bootstyle="secondary").pack(anchor="w")
        e.bind("<KeyRelease>", lambda ev: self.refresh_list())

        self.cat_var = tk.StringVar(value="（全部分类）")
        cats = ["（全部分类）"] + self.store.categories()
        cb = tb.Combobox(left, textvariable=self.cat_var, values=cats, width=20,
                         state="readonly")
        cb.pack(fill=tk.X, pady=(8, 6))
        cb.bind("<<ComboboxSelected>>", lambda ev: self.refresh_list())

        wrap = tb.Frame(left)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = tb.Treeview(wrap, columns=("模板", "分类"), show="headings",
                                selectmode="browse", bootstyle="primary", height=18)
        self.tree.heading("模板", text="模板")
        self.tree.heading("分类", text="分类")
        self.tree.column("模板", width=210, anchor="w")
        self.tree.column("分类", width=90, anchor="w")
        sb = tkttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda ev: self._on_select())

        self.count_label = tb.Label(left, text="", bootstyle="secondary")
        self.count_label.pack(anchor="w", pady=(6, 0))

        # ---- 右侧：详情 ----
        right = tb.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tb.Frame(right)
        head.pack(fill=tk.X)
        self.name_label = tb.Label(head, text="", font=("Microsoft YaHei UI", 11, "bold"))
        self.name_label.pack(side=tk.LEFT, anchor="w")
        self.perm_label = tb.Label(head, text="", bootstyle="secondary")
        self.perm_label.pack(side=tk.RIGHT, anchor="e")

        self.note_label = tb.Label(right, text="", bootstyle="warning", wraplength=760,
                                   justify="left")
        self.note_label.pack(fill=tk.X, pady=(2, 4))

        tb.Label(right, text="将执行的 SQL", bootstyle="secondary").pack(anchor="w")
        self.preview = SqlEditor(right, height=10, colors=self.app.editor_colors(),
                                 wrap=False, read_only=True)
        self.preview.pack(fill=tk.BOTH, expand=True, pady=(2, 6))

        self.param_box = tb.Labelframe(right, text="参数", padding=8)
        self.param_box.pack(fill=tk.X, pady=(0, 6))

        self.hint_label = tb.Label(right, text="", bootstyle="info", wraplength=760,
                                   justify="left")
        self.hint_label.pack(fill=tk.X)

        # ---- 底部按钮 ----
        bar = tb.Frame(self)
        bar.pack(fill=tk.X, pady=(8, 0))
        if "run" in self.callbacks:
            tb.Button(bar, text="▶ 执行", bootstyle="success",
                      command=self.run_current).pack(side=tk.LEFT)
        if "insert" in self.callbacks:
            tb.Button(bar, text="插入到编辑器", bootstyle="primary-outline",
                      command=self.insert_current).pack(side=tk.LEFT, padx=(6, 0))
        tb.Button(bar, text="复制 SQL", bootstyle="secondary-outline",
                  command=self.copy_current).pack(side=tk.LEFT, padx=(6, 0))
        tb.Button(bar, text="在查询窗口打开", bootstyle="secondary-outline",
                  command=self.open_in_query).pack(side=tk.LEFT, padx=(6, 0))
        tb.Label(bar, text="只读诊断：这里不提供任何改数据的模板",
                 bootstyle="secondary").pack(side=tk.RIGHT)

    # ================================================================ 列表
    def set_session(self, name: str):
        """外部切换连接后调用：重新判断每条模板能不能跑。"""
        self.session_name = name
        self.refresh_list()
        self._sync_perm()

    def _server_version(self) -> str:
        session = self.app.get_session(self.session_name)
        if session is None:
            return ""
        return str(getattr(getattr(session, "adapter", None), "server_version", "") or "")

    def _visible(self) -> List[TP.Template]:
        kw = self.search_var.get().strip()
        cat = self.cat_var.get()
        out = []
        hidden = 0
        version = self._server_version()
        for t in self.store.all():
            if cat and cat != "（全部分类）" and t.category != cat:
                continue
            if kw and kw.lower() not in t.search_text():
                continue
            if not t.version_ok(version):
                hidden += 1
                continue
            if t in out:      # 同名内置模板被用户版顶掉时只显示一次
                continue
            out.append(t)
        self._hidden_by_version = hidden
        return out

    def refresh_list(self):
        items = self._visible()
        keep = self.current.id if self.current else None
        self.tree.delete(*self.tree.get_children())
        for t in items:
            mark = "★ " if t.source == "user" else ""
            self.tree.insert("", "end", iid=t.id, values=(mark + t.name, t.category))
        total = len(self.store.all())
        text = f"共 {len(items)} 条"
        if len(items) != total:
            text += f"（共 {total} 条，已筛选）"
        if self._hidden_by_version:
            text += f"　·　{self._hidden_by_version} 条因数据库版本过低未显示"
        self.count_label.configure(text=text)
        if keep and keep in self.tree.get_children():
            self.tree.selection_set(keep)
        elif items:
            self.tree.selection_set(items[0].id)
        self._on_select()

    def _on_select(self):
        sel = self.tree.selection()
        if not sel:
            self.current = None
            return
        t = self.store.find(sel[0])
        self.current = t
        if t is None:
            return
        self.name_label.configure(text=t.name)
        self.hint_label.configure(text=t.hint or "")
        self._build_params(t)
        self._sync_perm()

    # ================================================================ 参数表单
    def _build_params(self, t: TP.Template):
        for w in self.param_box.winfo_children():
            w.destroy()
        self.param_vars = {}
        if not t.params:
            tb.Label(self.param_box, text="该模板无需填参数", bootstyle="secondary").pack(anchor="w")
            return
        for p in t.params:
            row = tb.Frame(self.param_box)
            row.pack(fill=tk.X, pady=2)
            tb.Label(row, text=p.label, width=20, anchor="e").pack(side=tk.LEFT, padx=(0, 8))
            if p.type == "enum":
                var = tk.StringVar(value=p.default or (p.values[0] if p.values else ""))
                tb.Combobox(row, textvariable=var, values=list(p.values), width=22,
                            state="readonly").pack(side=tk.LEFT)
            elif p.type == "bool":
                var = tk.BooleanVar(value=str(p.default).lower() in ("1", "y", "yes", "true", "是"))
                tb.Checkbutton(row, variable=var, bootstyle="primary-round-toggle").pack(side=tk.LEFT)
            elif p.type in ("int", "float"):
                var = tk.StringVar(value=str(p.default))
                low = int(p.min) if p.min is not None else 0
                high = int(p.max) if p.max is not None else 999999
                tb.Spinbox(row, from_=low, to=high, textvariable=var, width=10).pack(side=tk.LEFT)
            else:
                var = tk.StringVar(value=str(p.default))
                tb.Entry(row, textvariable=var, width=26).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.param_vars[p.name] = var
            # 参数一改就重渲染预览，让人在按下执行之前就看清会跑什么
            try:
                var.trace_add("write", lambda *a: self._refresh_preview())
            except Exception:
                pass
            if p.hint:
                tb.Label(row, text=p.hint, bootstyle="secondary").pack(side=tk.LEFT, padx=(8, 0))

    def _values(self) -> Dict[str, Any]:
        return {k: v.get() for k, v in self.param_vars.items()}

    # ================================================================ 权限
    def _probe(self):
        session = self.app.get_session(self.session_name)
        if session is None:
            return None
        return PM.probe_for(session)

    def _sync_perm(self):
        """刷新“能不能跑”的提示。"""
        t = self.current
        if t is None:
            self.perm_label.configure(text="")
            self.note_label.configure(text="")
            return
        _, note, ok = t.resolve(self._probe())
        self._note = note or ""
        self._resolved_ok = ok
        # 有降级版且已启用降级时，说明里必然非空 —— 这就是“用了哪条语句”的标记
        self._use_fallback = bool(ok and note)
        if ok:
            if self._use_fallback:
                self.perm_label.configure(text="可用（已降级）", bootstyle="warning")
            else:
                self.perm_label.configure(text="可用", bootstyle="success")
        else:
            self.perm_label.configure(text="当前账号跑不了", bootstyle="danger")
        self.note_label.configure(text=self._note)
        self._refresh_preview()

    def _refresh_preview(self):
        """参数变化时重新渲染预览。

        预览和执行用的是同一条语句（含降级），保证“所见即所跑”；
        权限完全不足时仍显示原始 SQL，让人知道本来想跑什么，只是不能点执行。
        """
        t = self.current
        if t is None:
            return
        try:
            sql = t.render(self._values(), use_fallback=bool(self._use_fallback))
        except TP.TemplateError as e:
            # 参数填了一半，预览出不来 —— 不阻塞，填完就好
            self.preview.set_sql(f"-- {e}")
            return
        except Exception:
            return
        self.preview.set_sql(sql)

    def current_sql(self) -> Tuple[bool, str, str]:
        """返回 (是否可执行, SQL 或错误信息, 说明)。"""
        t = self.current
        if t is None:
            return False, "请先选择一条模板", ""
        ok, err = t.check_params(self._values())
        if not ok:
            return False, err, ""
        if not self._resolved_ok:
            return False, "当前账号缺少运行这条模板所需的权限", self._note
        try:
            sql = t.render(self._values(), use_fallback=bool(self._use_fallback))
        except TP.TemplateError as e:
            return False, str(e), ""
        return True, sql, self._note

    # ================================================================ 动作
    def run_current(self):
        cb = self.callbacks.get("run")
        if not cb:
            return
        session = self.app.get_session(self.session_name)
        if session is None:
            W.warning(self, "当前没有数据库连接")
            return
        ok, sql, note = self.current_sql()
        if not ok:
            W.error(self, sql)
            return
        t = self.current

        def work(ev):
            return session.query(sql, max_rows=TEMPLATE_MAX_ROWS)

        ok, res = W.run_async(self, work, title="正在执行",
                              message=f"正在执行“{t.name}”…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"执行失败：{res}")
            return
        rows = list(res.rows or [])
        cols = list(res.columns or [])
        extra = note
        if len(rows) >= TEMPLATE_MAX_ROWS:
            extra = (extra + "　" if extra else "") + f"结果已截断到 {TEMPLATE_MAX_ROWS} 行"
        cb(cols, rows, extra, t)
        if self.on_status:
            self.on_status(f"“{t.name}”返回 {len(rows)} 行")

    def insert_current(self):
        cb = self.callbacks.get("insert")
        ok, sql, _ = self.current_sql()
        if not ok:
            W.error(self, sql)
            return
        cb(sql, self.current)

    def copy_current(self):
        if self.current is None:
            return
        try:
            sql = self.current.render(self._values())
        except TP.TemplateError as e:
            W.warning(self, str(e))
            return
        U.copy_to_clipboard(self, sql)
        W.toast(self, "SQL 已复制到剪贴板", "success")

    def open_in_query(self):
        if self.current is None:
            return
        try:
            sql = self.current.render(self._values())
        except TP.TemplateError as e:
            W.warning(self, str(e))
            return
        try:
            self.app.new_query_window(sql=sql.rstrip().rstrip(";") + ";",
                                      session_name=self.session_name)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"打开查询窗口失败：{e}")


class TemplateDialog(tb.Toplevel):
    """独立的模板库窗口（查询窗口工具栏、工具菜单入口）。"""

    def __init__(self, parent, app, session_name: str = "",
                 on_insert: Optional[Callable[[str], None]] = None):
        super().__init__(parent)
        self.app = app
        self.title("运维 SQL 模板库（只读诊断）")
        self.geometry("1080x720")
        self.transient(parent)
        self.on_insert = on_insert

        frm = tb.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        callbacks = {}
        if on_insert:
            callbacks["insert"] = lambda sql, t: self._insert(sql)
        self.picker = TemplatePicker(frm, app, session_name=session_name,
                                     callbacks=callbacks)
        self.picker.pack(fill=tk.BOTH, expand=True)

        bar = tb.Frame(frm)
        bar.pack(fill=tk.X, pady=(8, 0))
        tb.Button(bar, text="关闭", bootstyle="secondary",
                  command=self.destroy).pack(side=tk.RIGHT)
        U.center_on_parent(self, parent, 1080, 720)
        try:
            self.grab_set()
        except Exception:
            pass

    def _insert(self, sql: str):
        try:
            self.on_insert(sql)
            W.toast(self, "已插入到编辑器", "success")
        except Exception as e:  # noqa: BLE001
            W.error(self, f"插入失败：{e}")
