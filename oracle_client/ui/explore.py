"""数据探查界面：查重复值、值分布、跨表钻取。

入口统一在数据网格里右键某一列，产出的每一步都进“探查链”，
可以随时回退、继续往下追、导出 Excel。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ttkbootstrap as tb

from ..core import explore as EX
from ..core import utils as U
from . import widgets as W
from .editor import SqlEditor
from .grid import DataTable, show_row_detail
from .windows import BaseSubWindow


# ================================================================== 查重复值
class DupDialog(tb.Toplevel):
    """选择按哪些字段查重复，先看语句再执行。"""

    def __init__(self, parent, app, source_sql: str, columns: Sequence[str],
                 default_column: str = "", dialect: str = "oracle"):
        super().__init__(parent)
        self.title("查重复值")
        self.transient(parent)
        self.app = app
        self.source_sql = source_sql
        self.dialect = dialect
        self.ok = False
        self.sql = ""
        self.mode = "summary"
        self.columns: List[str] = []

        body = W.ScrolledBody(self)
        body.pack(fill=tk.BOTH, expand=True)

        tb.Label(body.inner, text="① 选择按哪些字段判断重复（可多选，比如“姓名+电话”都相同才算重复）",
                 bootstyle="secondary").pack(anchor="w", pady=(0, 6))
        self.check = W.CheckList(body.inner, list(columns), columns=3, on_change=self.generate)
        self.check.pack(fill=tk.X, pady=(0, 10))
        self.check.none()
        if default_column and default_column in list(columns):
            self.check.set_checked([default_column])

        row = tb.Frame(body.inner)
        row.pack(fill=tk.X, pady=(0, 8))
        tb.Label(row, text="最少重复次数").pack(side=tk.LEFT)
        self.min_var = tk.StringVar(value="2")
        tb.Spinbox(row, from_=2, to=9999, textvariable=self.min_var, width=6).pack(side=tk.LEFT, padx=(6, 20))
        tb.Label(row, text="输出").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="summary")
        tb.Radiobutton(row, text="先给汇总（值+次数）", variable=self.mode_var,
                       value="summary", command=self.generate).pack(side=tk.LEFT, padx=(6, 0))
        tb.Radiobutton(row, text="直接给重复的明细行", variable=self.mode_var,
                       value="detail", command=self.generate).pack(side=tk.LEFT, padx=(6, 0))

        tb.Label(body.inner, text="② 生成的语句（可以直接改）", bootstyle="secondary").pack(anchor="w", pady=(8, 4))
        self.preview = SqlEditor(body.inner, height=8, colors=app.editor_colors(), wrap=False)
        self.preview.pack(fill=tk.BOTH, expand=True)
        self.generate()

        bar = tb.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="执行查询", bootstyle="success", command=self._ok).pack(side=tk.RIGHT, padx=(0, 8))
        U.fit_to_content(self, parent, min_w=560, min_h=420)
        self.grab_set()

    def generate(self):
        cols = self.check.checked()
        try:
            if not cols:
                self.preview.set_sql("-- 请至少勾选一个字段")
                return
            n = max(2, int(self.min_var.get() or 2))
            if self.mode_var.get() == "detail":
                self.preview.set_sql(EX.dup_detail_sql(self.source_sql, cols, self.dialect, n))
            else:
                self.preview.set_sql(EX.dup_summary_sql(self.source_sql, cols, self.dialect, n))
        except Exception as e:  # noqa: BLE001
            self.preview.set_sql(f"-- 生成失败：{e}")

    def _ok(self):
        cols = self.check.checked()
        if not cols:
            W.warning(self, "请至少勾选一个字段")
            return
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "没有可执行的语句")
            return
        self.ok = True
        self.sql = sql
        self.mode = self.mode_var.get()
        self.columns = cols
        self.destroy()


# ================================================================== 跨表钻取
class DrillDialog(tb.Toplevel):
    """拿当前结果某列的值，去另一张表的关联字段里查。"""

    def __init__(self, parent, app, session_name: str, source_sql: str, source_column: str,
                 selected_values: Sequence[Any] = (), schema: str = "", source_table: str = "",
                 dialect: str = "oracle"):
        super().__init__(parent)
        self.title("跨表钻取")
        self.transient(parent)
        self.app = app
        self.session_name = session_name
        self.source_sql = source_sql
        self.source_column = source_column
        self.selected_values = list(selected_values or [])
        self.schema = schema
        self.dialect = dialect
        self.ok = False
        self.sql = ""
        self.target_table = ""
        self.target_column = ""

        self.session = app.get_session(session_name)
        self._fk: Dict[str, Tuple[str, str]] = {}
        if self.session and source_table:
            try:
                for fk in self.session.foreign_keys(schema, source_table) or []:
                    self._fk[str(fk.get("column", "")).upper()] = (fk.get("ref_table") or "",
                                                                   fk.get("ref_column") or "")
            except Exception:  # noqa: BLE001
                self._fk = {}

        body = W.ScrolledBody(self)
        body.pack(fill=tk.BOTH, expand=True)

        tb.Label(body.inner, text="① 用哪些值去查", bootstyle="secondary").pack(anchor="w", pady=(0, 6))
        self.src_var = tk.StringVar(value="all")
        n_sel = len(self.selected_values)
        tb.Radiobutton(body.inner, text="当前结果的全部行（推荐：语句短、不受条数限制）",
                       variable=self.src_var, value="all", command=self.generate).pack(anchor="w")
        tb.Radiobutton(body.inner,
                       text=(f"仅当前选中的行（{n_sel} 个值）" if n_sel else "仅当前选中的行（未选中行）"),
                       variable=self.src_var, value="selected", command=self.generate,
                       state="normal" if n_sel else "disabled").pack(anchor="w", pady=(2, 12))

        tb.Label(body.inner, text="② 查哪张表、哪个字段", bootstyle="secondary").pack(anchor="w", pady=(0, 6))
        frm = tb.Frame(body.inner)
        frm.pack(fill=tk.X, pady=(0, 6))
        tb.Label(frm, text="目标表").pack(side=tk.LEFT)
        self.table_var = tk.StringVar()
        tables = self._table_names()
        self.table_cb = tb.Combobox(frm, textvariable=self.table_var, values=tables,
                                    width=28, state="readonly")
        self.table_cb.pack(side=tk.LEFT, padx=(6, 16))
        self.table_cb.bind("<<ComboboxSelected>>", lambda e: self._on_table())
        tb.Label(frm, text="目标字段").pack(side=tk.LEFT)
        self.col_var = tk.StringVar()
        self.col_cb = tb.Combobox(frm, textvariable=self.col_var, values=[], width=24, state="readonly")
        self.col_cb.pack(side=tk.LEFT, padx=(6, 0))
        self.col_cb.bind("<<ComboboxSelected>>", lambda e: self.generate())

        self.hint_var = tk.StringVar(value="")
        tb.Label(body.inner, textvariable=self.hint_var, bootstyle="info").pack(anchor="w", pady=(0, 10))

        tb.Label(body.inner, text="③ 生成的语句（可以直接改）", bootstyle="secondary").pack(anchor="w", pady=(0, 4))
        self.preview = SqlEditor(body.inner, height=9, colors=app.editor_colors(), wrap=False)
        self.preview.pack(fill=tk.BOTH, expand=True)

        self._preselect()

        bar = tb.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="执行查询", bootstyle="success", command=self._ok).pack(side=tk.RIGHT, padx=(0, 8))
        U.fit_to_content(self, parent, min_w=620, min_h=460)
        self.grab_set()

    def _table_names(self) -> List[str]:
        if not self.session:
            return []
        try:
            objs = self.session.objects(self.schema or self.session.default_schema)
            return [o.name for o in objs]
        except Exception:  # noqa: BLE001
            return []

    def _preselect(self):
        """按外键约束推荐，其次按同名字段，都没有就留空让用户选。"""
        ref = self._fk.get((self.source_column or "").upper())
        tables = list(self.table_cb.cget("values") or [])
        if ref and ref[0] in tables:
            self.table_var.set(ref[0])
            self._on_table()
            if ref[1]:
                self.col_var.set(ref[1])
            self.hint_var.set(f"已按外键约束自动选择：{self.source_column} → {ref[0]}.{ref[1]}")
            self.generate()
            return
        # 退化：找一个有同名字段的表
        if self.session and self.source_column:
            for t in tables:
                try:
                    cols = [c.name for c in self.session.columns(self.schema or self.session.default_schema, t)]
                except Exception:  # noqa: BLE001
                    continue
                if self.source_column.upper() in [c.upper() for c in cols]:
                    self.table_var.set(t)
                    self._on_table()
                    self.col_var.set(self.source_column)
                    self.hint_var.set(f"按同名字段自动选择：{t}.{self.source_column}（未找到外键约束）")
                    self.generate()
                    return
        self.hint_var.set("请手动选择目标表和目标字段")

    def _on_table(self):
        t = self.table_var.get()
        cols: List[str] = []
        if self.session and t:
            try:
                cols = [c.name for c in self.session.columns(self.schema or self.session.default_schema, t)]
            except Exception:  # noqa: BLE001
                cols = []
        self.col_cb.configure(values=cols)
        if cols and self.col_var.get() not in cols:
            # 优先同名字段，否则第一个
            same = [c for c in cols if c.upper() == (self.source_column or "").upper()]
            self.col_var.set(same[0] if same else cols[0])
        self.generate()

    def generate(self):
        t, c = self.table_var.get(), self.col_var.get()
        if not t or not c:
            self.preview.set_sql("-- 请选择目标表和目标字段")
            return
        try:
            vals = self.selected_values if self.src_var.get() == "selected" else None
            self.preview.set_sql(EX.drill_sql(self.source_sql, self.source_column,
                                              self.schema, t, c, self.dialect,
                                              selected_values=vals))
        except Exception as e:  # noqa: BLE001
            self.preview.set_sql(f"-- 生成失败：{e}")

    def _ok(self):
        t, c = self.table_var.get(), self.col_var.get()
        if not t or not c:
            W.warning(self, "请选择目标表和目标字段")
            return
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "没有可执行的语句")
            return
        self.ok = True
        self.sql = sql
        self.target_table = t
        self.target_column = c
        self.destroy()


# ================================================================== 探查窗口
class ExploreWindow(BaseSubWindow):
    """展示探查结果，并把每一步串成可回退、可继续的探查链。"""

    window_kind = "探查窗口"

    def __init__(self, app, session_name: str, sql: str, title: str = "数据探查",
                 dialect: str = "oracle", schema: str = "", source_table: str = "",
                 action: str = "", column: str = ""):
        super().__init__(app, title, session_name)
        self.schema = schema
        self.source_table = source_table
        self.dialect = dialect
        self.steps: List[dict] = []
        self.cur = -1
        self.title(title)
        self._build()
        U.center_on_parent(self, app.root, 1180, 780)
        # 右键某列直接触发的动作，等源数据这一步查成功后再接着做
        if self.push_step(title, sql) and action and column:
            fn = {"dup": self.on_dup, "distinct": self.on_distinct, "drill": self.on_drill}.get(action)
            if fn:
                fn(column)

    # ---------------- 界面 ----------------
    def _build(self):
        toolbar = W.Toolbar(self, padding=(8, 4))
        toolbar.pack(fill=tk.X)
        toolbar.add_button("重新执行 (F5)", self.rerun, "success",
                           tooltip="按当前语句重新查一次")
        toolbar.add_button("← 上一步", self.step_back, "secondary-outline",
                           tooltip="回到探查链的上一跳")
        toolbar.add_button("查看该值明细", self.detail_of_value, "primary-outline",
                           tooltip="汇总结果里选中一行，看这组值对应的原始行")
        toolbar.add_button("导出 Excel", self.export, "success-outline")
        toolbar.add_spacer()
        toolbar.add_button("关闭", self._on_close, "secondary")

        self.crumb = tb.Frame(self, padding=(8, 2))
        self.crumb.pack(fill=tk.X)

        box = tb.Labelframe(self, text="当前这一步的语句（可以直接改，改完按 F5）", padding=6)
        box.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.preview = SqlEditor(box, height=6, colors=self.app.editor_colors(), wrap=False)
        self.preview.pack(fill=tk.X)
        self.preview.text.bind("<F5>", lambda e: self.rerun())

        bar = tb.Frame(self, padding=(8, 6, 8, 0))
        bar.pack(fill=tk.X)
        self.info_var = tk.StringVar(value="还没有数据")
        tb.Label(bar, textvariable=self.info_var, bootstyle="secondary").pack(side=tk.LEFT)

        colors = self.app.grid_colors()
        self.data_grid = DataTable(self, callbacks={
            "view": self.view_row, "export": self.export,
            "dup": self.on_dup, "distinct": self.on_distinct, "drill": self.on_drill,
        }, date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"),
            null_display=self.app.settings.get("null_display", ""),
            show_rowno=bool(self.app.settings.get("show_row_number", True)),
            max_rows=int(self.app.settings.get("max_grid_rows", 20000)),
            even_color=colors["even"], odd_color=colors["odd"])
        self.data_grid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))

        self.statusbar = W.StatusBar(self, parts=2, padding=(8, 2))
        self.statusbar.pack(fill=tk.X)
        self.statusbar.set(1, self.session_name)

    # ---------------- 探查链 ----------------
    def _render_crumbs(self):
        for w in self.crumb.winfo_children():
            w.destroy()
        for i, st in enumerate(self.steps):
            if i:
                tb.Label(self.crumb, text="›", bootstyle="secondary").pack(side=tk.LEFT, padx=4)
            tb.Button(self.crumb, text=st["title"], bootstyle="info-link" if i == self.cur else "secondary-link",
                      command=lambda k=i: self.goto(k)).pack(side=tk.LEFT)

    def push_step(self, title: str, sql: str, detail_sql: str = "", detail_columns: Sequence[str] = ()) -> bool:
        session = self.session
        if not session:
            W.warning(self, "没有可用的数据库连接")
            return False
        sql = (sql or "").strip()
        if not sql:
            return False
        fetch = int(self.app.settings.get("fetch_rows", 200))

        def work(event):
            return session.query(sql.rstrip(";"), max_rows=fetch)

        ok, res = W.run_async(self, work, title="正在查询", message="正在探查数据…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"查询失败：{res}")
            self.statusbar.set(0, "查询失败")
            return False
        # 截断当前链之后的分叉，再追加新一步
        self.steps = self.steps[: self.cur + 1]
        self.steps.append({"title": title, "sql": sql, "rows": res.rows, "columns": res.columns,
                           "detail_sql": detail_sql, "detail_columns": list(detail_columns)})
        self.cur = len(self.steps) - 1
        self._render_crumbs()
        self._show_current()
        self.statusbar.set(0, f"{U.human_number(len(res.rows))} 行")
        try:
            self.app.history.add(sql, tag="SELECT", connection=session.label)
        except Exception:
            pass
        return True

    def goto(self, idx: int):
        if 0 <= idx < len(self.steps):
            self.cur = idx
            self._render_crumbs()
            self._show_current()

    def step_back(self):
        if self.cur > 0:
            self.goto(self.cur - 1)

    def _show_current(self):
        if self.cur < 0 or not self.steps:
            return
        st = self.steps[self.cur]
        self.preview.set_sql(st["sql"])
        self.data_grid.set_data(st["columns"], st["rows"])
        txt = f"{U.human_number(len(st['rows']))} 行"
        if st.get("detail_columns"):
            txt += "   选中一行后点“查看该值明细”可看原始行"
        self.info_var.set(txt)

    def rerun(self):
        if self.cur < 0:
            return
        st = self.steps[self.cur]
        sql = self.preview.get_sql().strip()
        if not sql:
            return
        st["sql"] = sql
        session = self.session
        if not session:
            return
        fetch = int(self.app.settings.get("fetch_rows", 200))

        def work(event):
            return session.query(sql.rstrip(";"), max_rows=fetch)

        ok, res = W.run_async(self, work, title="正在查询", message="正在执行…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"查询失败：{res}")
            return
        st["rows"], st["columns"] = res.rows, res.columns
        self._show_current()
        self.statusbar.set(0, f"{U.human_number(len(res.rows))} 行")

    # ---------------- 列级动作 ----------------
    def _col_index(self, col: str) -> int:
        try:
            return list(self.steps[self.cur]["columns"]).index(col)
        except Exception:
            return -1

    def _current_sql(self) -> str:
        return self.steps[self.cur]["sql"] if self.cur >= 0 else ""

    def on_dup(self, col: str):
        if not col:
            return
        cols = list(self.steps[self.cur]["columns"]) if self.cur >= 0 else [col]
        dlg = DupDialog(self, self.app, self._current_sql(), cols, col, self.dialect)
        self.wait_window(dlg)
        if not dlg.ok:
            return
        if dlg.mode == "detail":
            self.push_step(f"重复明细：{','.join(dlg.columns)}", dlg.sql)
        else:
            self.push_step(f"查重：{','.join(dlg.columns)}", dlg.sql,
                           detail_sql=self._current_sql(), detail_columns=dlg.columns)

    def on_distinct(self, col: str):
        if not col:
            return
        try:
            sql = EX.distinct_sql(self._current_sql(), col, self.dialect)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"生成失败：{e}")
            return
        self.push_step(f"值分布：{col}", sql,
                       detail_sql=self._current_sql(), detail_columns=[col])

    def on_drill(self, col: str):
        if not col or self.cur < 0:
            return
        idx = self._col_index(col)
        vals = []
        if idx >= 0:
            vals = [r[idx] for r in self.data_grid.selected_rows() if idx < len(r)]
        dlg = DrillDialog(self, self.app, self.session_name, self._current_sql(), col,
                          selected_values=vals, schema=self.schema,
                          source_table=self.source_table, dialect=self.dialect)
        self.wait_window(dlg)
        if not dlg.ok:
            return
        self.push_step(EX.drill_title(self.source_table or "结果", col,
                                      dlg.target_table, dlg.target_column), dlg.sql)
        self.source_table = dlg.target_table

    def detail_of_value(self):
        if self.cur < 0:
            return
        st = self.steps[self.cur]
        cols = st.get("detail_columns") or []
        base = st.get("detail_sql") or ""
        if not cols or not base:
            W.warning(self, "当前这一步不是“汇总”结果，没有可展开的明细。\n"
                            "请先做一次“查重复值”或“值分布”，再选中一行查看明细。")
            return
        rows = self.data_grid.selected_rows()
        if not rows:
            W.warning(self, "请先在表格里选中一行")
            return
        vals = list(rows[0][: len(cols)])
        try:
            sql = EX.value_detail_sql(base, cols, vals, {}, self.dialect)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"生成失败：{e}")
            return
        desc = ", ".join(f"{c}={v}" for c, v in zip(cols, vals))
        self.push_step(f"明细：{desc[:40]}", sql)

    # ---------------- 其它 ----------------
    def view_row(self):
        rows = self.data_grid.selected_rows()
        if rows:
            show_row_detail(self, self.data_grid.columns, rows[0],
                            date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"))

    def export(self):
        from .dialogs import ExportDialog

        visible = self.data_grid.visible_rows()
        allrows = [self.data_grid.raw[i] for i in self.data_grid.order]
        cols = self.data_grid.columns
        session = self.session
        sql = self._current_sql()

        def p_visible():
            return cols, visible

        def p_all():
            return cols, allrows

        def p_db():
            if not session:
                return cols, []
            for _c, batch in session.stream(EX.normalize(sql, self.dialect), chunk=1000):
                for r in batch:
                    yield r

        ExportDialog(self, self.app, default_name=(self.source_table or "探查结果"),
                     providers={"当前视图（含筛选和排序）": p_visible,
                                "已取回的全部数据": p_all,
                                "重新从数据库取全部（不限制行数）": p_db},
                     table_name=self.source_table)

    def apply_theme(self):
        self.preview.apply_colors(self.app.editor_colors())
