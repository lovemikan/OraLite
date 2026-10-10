"""左侧对象浏览器：模式 → 表/视图 → 常用操作。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk as tkttk
from typing import List, Optional

import ttkbootstrap as tb

from ..core import sqlbuilder as SB
from ..core import utils as U
from ..core.models import TableMeta
from . import widgets as W


class SchemaPanel(tb.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(8, 8, 4, 8))
        self.app = app
        self.objects: List[TableMeta] = []

        top = tb.Frame(self)
        top.pack(fill=tk.X)
        tb.Label(top, text="对象浏览器", bootstyle="primary",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)
        tb.Button(top, text="⟳", width=3, bootstyle="secondary-link",
                  command=self.refresh).pack(side=tk.RIGHT)

        row1 = tb.Frame(self)
        row1.pack(fill=tk.X, pady=(6, 0))
        tb.Label(row1, text="模式", width=4, anchor="w").pack(side=tk.LEFT)
        self.schema_var = tk.StringVar()
        self.schema_combo = tb.Combobox(row1, textvariable=self.schema_var, width=14, state="readonly")
        self.schema_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.schema_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        row2 = tb.Frame(self)
        row2.pack(fill=tk.X, pady=(6, 0))
        self.search_var = tk.StringVar()
        e = tb.Entry(row2, textvariable=self.search_var)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        W._attach_tooltip(e, "输入表名关键字过滤")
        self.search_var.trace_add("write", lambda *a: self._filter())
        self.kind_var = tk.StringVar(value="全部")
        cb = tb.Combobox(row2, textvariable=self.kind_var, values=["全部", "表", "视图"], width=6,
                         state="readonly")
        cb.pack(side=tk.LEFT, padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._filter())

        body = tb.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.tree = tb.Treeview(body, columns=("名称", "类型", "行数"), show="headings",
                                bootstyle="primary")
        for c, w in zip(("名称", "类型", "行数"), (170, 52, 74)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = tkttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self._action_browse())
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Return>", lambda e: self._action_browse())

        self.info = tb.Label(self, text="未连接", bootstyle="secondary", wraplength=280, justify="left")
        self.info.pack(fill=tk.X, pady=(6, 0))
        self._build_menu()

    # ---------------------------------------------------------------- 菜单
    def _build_menu(self):
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="浏览数据（前 200 行）", command=self._action_browse)
        self.menu.add_command(label="在查询窗口里打开 SELECT", command=self._action_select)
        self.menu.add_separator()
        self.menu.add_command(label="查看表结构", command=self._action_structure)
        self.menu.add_command(label="新增一行…", command=self._action_insert)
        self.menu.add_separator()
        self.menu.add_command(label="复制表名", command=self._action_copy_name)
        self.menu.add_command(label="复制 SELECT 语句", command=self._action_copy_select)

    def _popup(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------------------------------------------------------------- 数据
    def refresh(self):
        session = self.app.current_session
        if not session:
            self.tree.delete(*self.tree.get_children())
            self.info.configure(text="未连接数据库")
            return
        try:
            schemas = session.schemas()
        except Exception:
            schemas = [session.default_schema]
        cur = self.schema_var.get() or session.default_schema
        if cur not in schemas:
            schemas = [cur] + list(schemas)
        self.schema_combo.configure(values=schemas)
        if cur:
            self.schema_var.set(cur)

        def work(ev):
            return session.objects(cur)

        ok, res = True, None
        try:
            res = session.objects(cur)
        except Exception as e:  # noqa: BLE001
            ok, res = False, e
        if not ok:
            self.info.configure(text=f"读取失败：{res}")
            return
        self.objects = res
        self._filter()
        self.info.configure(text=f"{cur}：共 {len(res)} 个对象（双击打开）")

    def _filter(self):
        kw = (self.search_var.get() or "").strip().upper()
        kind = self.kind_var.get()
        self.tree.delete(*self.tree.get_children())
        for o in self.objects:
            if kind == "表" and o.object_type != "TABLE":
                continue
            if kind == "视图" and o.object_type != "VIEW":
                continue
            if kw and kw not in o.name.upper():
                continue
            rows = "" if o.num_rows is None else U.human_number(o.num_rows)
            self.tree.insert("", "end", iid=o.name,
                             values=(o.name, _type_label(o.object_type), rows))

    # ---------------------------------------------------------------- 动作
    def _selected(self) -> Optional[TableMeta]:
        sel = self.tree.selection()
        if not sel:
            return None
        name = sel[0]
        for o in self.objects:
            if o.name == name:
                return o
        return None

    def _action_browse(self):
        o = self._selected()
        if not o:
            return
        self.app.new_table_window(o.owner, o.name)

    def _action_select(self):
        o = self._selected()
        if not o:
            return
        from ..core.models import QuerySpec
        from ..core import sqlbuilder as SB2

        session = self.app.current_session
        dialect = session.dialect if session else "oracle"
        spec = QuerySpec(schema=o.owner, table=o.name, limit=200)
        sql = SB2.apply_row_limit(SB2.build_select(spec, {}, dialect), 200, dialect)
        self.app.new_query_window(sql=sql)

    def _action_structure(self):
        o = self._selected()
        if not o:
            return
        from .dialogs import TableInfoDialog

        TableInfoDialog(self.app.root, self.app, self.app.current_session_name, o.owner, o.name)

    def _action_insert(self):
        o = self._selected()
        if not o:
            return
        session = self.app.current_session
        try:
            cols = session.columns(o.owner, o.name)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"读取字段失败：{e}")
            return
        from .builders import InsertDialog

        dlg = InsertDialog(self.app.root, self.app, self.app.current_session_name, o.owner, o.name, cols)
        self.app.root.wait_window(dlg)

    def _action_copy_name(self):
        o = self._selected()
        if o and U.copy_to_clipboard(self, o.full_name):
            W.toast(self, f"已复制 {o.full_name}", "success")

    def _action_copy_select(self):
        o = self._selected()
        if not o:
            return
        sql = f"SELECT *\nFROM   {SB.full_table_name(o.owner, o.name)}\nWHERE  ROWNUM <= 200;"
        if U.copy_to_clipboard(self, sql):
            W.toast(self, "SELECT 语句已复制", "success")


def _type_label(t: str) -> str:
    return {"TABLE": "表", "VIEW": "视图", "SYNONYM": "同义词"}.get(t, t)
