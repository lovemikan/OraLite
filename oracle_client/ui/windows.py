"""子窗口：SQL 查询窗口、数据表浏览与编辑窗口、备份中心外壳。"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import ttkbootstrap as tb

from ..core import backup as BK
from ..core import sqlbuilder as SB
from ..core import utils as U
from ..core.db import DbSession
from ..core.models import ColumnMeta, QuerySpec, Condition
from ..core.validator import format_value
from . import widgets as W
from .builders import (ConditionEditor, DeleteDialog, InsertDialog, UpdateDialog,
                       build_capture_sql, confirm_and_execute_dml)
from .editor import SqlEditor
from .grid import DataTable, show_row_detail

def prepare_statements(sql: str) -> List[str]:
    """把编辑框里的文本拆成可逐条执行的语句。

    PL/SQL 块（BEGIN/DECLARE）内部的分号不能当分隔符，这一点由
    sqlbuilder.split_statements 统一处理，这里不再另写一套规则。
    """
    s = (sql or "").strip()
    if not s:
        return []
    return SB.split_statements(s)


# ================================================================== 窗口基类
class BaseSubWindow(tb.Toplevel):
    window_kind = "子窗口"

    def __init__(self, app, title: str, session_name: str = "", width: int = 1100, height: int = 720):
        super().__init__(app.root)
        self.app = app
        self.session_name = session_name or app.current_session_name
        self.title(title)
        self.geometry(f"{width}x{height}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self.focus_set())
        app.register_window(self)

    # ---------------- 连接 ----------------
    @property
    def session(self) -> Optional[DbSession]:
        return self.app.get_session(self.session_name)

    def session_names(self) -> List[str]:
        return self.app.session_names()

    def _add_session_selector(self, toolbar: W.Toolbar, on_change: Optional[Callable] = None):
        self.session_var = tk.StringVar(value=self.session_name)
        names = self.session_names()
        if self.session_name not in names and self.session_name:
            names = names + [self.session_name]
        cb = toolbar.add_combo(self.session_var, names, width=26,
                               command=lambda: self._switch_session(on_change))
        self.session_combo = cb
        return cb

    def _switch_session(self, on_change: Optional[Callable] = None):
        self.session_name = self.session_var.get()
        self.app.current_session_name = self.session_name
        if on_change:
            on_change()

    # ---------------- 生命周期 ----------------
    def destroy(self):
        """无论从哪条路径销毁（关闭按钮 / _on_close / 代码直接 destroy / 父窗口连带），
        都先从主窗口注册表里注销，避免“窗口菜单”里残留已销毁的窗口。"""
        try:
            self.app.unregister_window(self)
        except Exception:
            pass
        try:
            super().destroy()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.destroy()
        except Exception:
            pass

    def apply_theme(self):
        """主题变化时刷新自身配色，子类可覆盖。"""
        pass

    @staticmethod
    def _break_after(fn):
        """执行动作并返回 "break"，阻止快捷键继续冒泡到全局绑定。"""
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
        return "break"

    def sync_read_only(self, active: bool):
        """主窗口切换连接 / 开关只读时，同步在子窗口上给出可见标识。"""
        try:
            if hasattr(self, "statusbar") and len(self.statusbar.vars) >= 3:
                self.statusbar.set(2, "🔒 只读" if active else "")
        except Exception:
            pass

    def status(self, text: str, kind: str = "info"):
        if hasattr(self, "statusbar"):
            self.statusbar.set(0, text)


# ================================================================== SQL 查询窗口
class ResultTab(tb.Frame):
    """一个结果集标签页。"""

    def __init__(self, master, window: "QueryWindow", columns: Sequence[str], rows: Sequence[tuple],
                 sql: str, elapsed: float, truncated: bool = False,
                 base_table: Tuple[str, str] = ("", "")):
        super().__init__(master)
        self.window = window
        self.app = window.app
        self.columns = list(columns or [])
        self.sql = sql
        self.base_table = base_table
        self.elapsed = elapsed

        head = tb.Frame(self, padding=(8, 6, 8, 0))
        head.pack(fill=tk.X)
        self.info_var = tk.StringVar()
        self._update_info(len(rows), truncated)
        tb.Label(head, textvariable=self.info_var, bootstyle="secondary").pack(side=tk.LEFT)

        tb.Label(head, text="   查找：").pack(side=tk.LEFT)
        self.find_var = tk.StringVar()
        e = tb.Entry(head, textvariable=self.find_var, width=22)
        e.pack(side=tk.LEFT)
        e.bind("<Return>", lambda ev: self.do_find())
        tb.Button(head, text="筛选", bootstyle="info-outline", command=self.do_find).pack(side=tk.LEFT, padx=(4, 0))
        tb.Button(head, text="清除", bootstyle="secondary-link", command=self.clear_find).pack(side=tk.LEFT, padx=(4, 0))

        tb.Button(head, text="导出 Excel", bootstyle="success-outline", command=self.export).pack(side=tk.RIGHT)
        self.more_btn = tb.Button(head, text="加载更多", bootstyle="primary-outline", command=self.load_more)
        if truncated:
            self.more_btn.pack(side=tk.RIGHT, padx=(0, 8))

        colors = self.app.grid_colors()
        self.data_grid = DataTable(self, callbacks={
            "view": self.view_row, "export": self.export,
            "edit": self.edit_row, "delete": self.delete_row, "insert": self.insert_row,
            "dup": lambda c: self._explore(c, "dup"),
            "distinct": lambda c: self._explore(c, "distinct"),
            "drill": lambda c: self._explore(c, "drill"),
        }, date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"),
            null_display=self.app.settings.get("null_display", ""),
            show_rowno=bool(self.app.settings.get("show_row_number", True)),
            max_rows=int(self.app.settings.get("max_grid_rows", 20000)),
            even_color=colors["even"], odd_color=colors["odd"])
        self.data_grid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))
        self.data_grid.set_data(columns, rows, truncated)

        editable = bool(base_table and base_table[1])
        if not editable:
            for key in ("edit", "delete", "insert"):
                self.data_grid.callbacks.pop(key, None)

    # ---------------- 信息 ----------------
    def _update_info(self, count: int, truncated: bool):
        txt = f"{U.human_number(count)} 行   耗时 {self.elapsed:.2f}s"
        if truncated:
            txt += "   已截断（可点“加载更多”）"
        grid = getattr(self, "data_grid", None)
        shown = grid.visible_count() if grid is not None else count
        if grid is not None and shown != count:
            txt += f"   当前显示 {U.human_number(shown)} 行"
        self.info_var.set(txt)

    def on_refresh(self, shown: int, total: int):
        self._update_info(total, self.data_grid.truncated)

    # ---------------- 查找 ----------------
    def do_find(self):
        kw = (self.find_var.get() or "").strip().lower()
        if not kw:
            self.clear_find()
            return

        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        nd = self.app.settings.get("null_display", "")

        def pred(row):
            for v in row:
                if kw in format_value(v, dfmt, nd).lower():
                    return True
            return False

        self.data_grid.set_filter(pred)
        self._update_info(len(self.data_grid.raw), self.data_grid.truncated)

    def clear_find(self):
        self.find_var.set("")
        self.data_grid.clear_filter()
        self._update_info(len(self.data_grid.raw), self.data_grid.truncated)

    # ---------------- 加载更多 ----------------
    def load_more(self):
        session = self.window.session
        if not session:
            return
        offset = len(self.data_grid.raw)
        fetch = int(self.app.settings.get("fetch_rows", 200))
        paged = session.paginate(self.sql.rstrip(";"), offset, fetch)

        def work(event):
            return session.query(paged, max_rows=fetch)

        ok, res = W.run_async(self, work, title="正在取数", message="正在加载更多数据…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"加载失败：{res}")
            return
        if not res.rows:
            self.more_btn.configure(state="disabled")
            W.toast(self, "没有更多数据了", "info")
            return
        self.data_grid.append_data(res.rows, truncated=res.truncated)
        self._update_info(len(self.data_grid.raw), self.data_grid.truncated)

    # ---------------- 行操作 ----------------
    def _row_dict(self, row: Sequence[Any]) -> Dict[str, Any]:
        return {c: v for c, v in zip(self.columns, row)}

    def view_row(self):
        rows = self.data_grid.selected_rows()
        if not rows:
            return
        show_row_detail(self, self.columns, rows[0],
                        date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"))

    def _explore(self, col: str, action: str):
        """把当前结果当作探查起点，继续查重/值分布/跨表钻取。"""
        if not self.sql:
            W.warning(self, "当前结果没有可追查的语句")
            return
        schema, table = self.base_table
        open_explore(self, self.app, self.window.session_name, self.sql,
                     title=f"探查 {table or '查询结果'}",
                     schema=schema, table=table, action=action, column=col)

    def _table_context(self) -> Tuple[str, str, List[ColumnMeta]]:
        schema, table = self.base_table
        session = self.window.session
        cols: List[ColumnMeta] = []
        if session and table:
            try:
                cols = session.columns(schema, table)
            except Exception:
                cols = []
        return schema, table, cols

    def edit_row(self):
        rows = self.data_grid.selected_rows()
        if not rows:
            W.warning(self, "请先选中一行")
            return
        schema, table, cols = self._table_context()
        if not table or not cols:
            W.warning(self, "只有简单的单表查询才能直接编辑，请在 SQL 窗口手写 UPDATE 或用数据窗口。")
            return
        dlg = UpdateDialog(self, self.app, self.window.session_name, schema, table, cols,
                           self._row_dict(rows[0]))
        self.wait_window(dlg)
        if dlg.ok:
            self.window.refresh_current()

    def delete_row(self):
        rows = self.data_grid.selected_rows()
        if not rows:
            W.warning(self, "请先选中一行")
            return
        schema, table, cols = self._table_context()
        if not table or not cols:
            W.warning(self, "只有简单的单表查询才能直接删除。")
            return
        dlg = DeleteDialog(self, self.app, self.window.session_name, schema, table, cols,
                           self._row_dict(rows[0]))
        self.wait_window(dlg)
        if dlg.ok:
            self.window.refresh_current()

    def insert_row(self):
        schema, table, cols = self._table_context()
        if not table or not cols:
            return
        dlg = InsertDialog(self, self.app, self.window.session_name, schema, table, cols)
        self.wait_window(dlg)
        if dlg.ok:
            self.window.refresh_current()

    # ---------------- 导出 ----------------
    def export(self):
        session = self.window.session
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        visible_rows = self.data_grid.visible_rows()
        all_rows = [self.data_grid.raw[i] for i in self.data_grid.order]

        def provider_visible():
            return self.columns, visible_rows

        def provider_all():
            return self.columns, all_rows

        def provider_db():
            if not session:
                return self.columns, []
            return self.columns, _stream_rows(session, self.sql)

        providers = {
            "当前视图（含筛选和排序）": provider_visible,
            "已取回的全部数据": provider_all,
            "重新从数据库取全部（不限制行数）": provider_db,
        }
        from .dialogs import ExportDialog

        name = (self.base_table[1] or "查询结果")
        dlg = ExportDialog(self, self.app, default_name=name, providers=providers,
                           table_name=self.base_table[1])
        self.wait_window(dlg)


def open_explore(parent, app, session_name: str, sql: str, title: str = "数据探查",
                 schema: str = "", table: str = "", action: str = "", column: str = ""):
    """打开数据探查窗口（查重复值 / 值分布 / 跨表钻取）。"""
    from .explore import ExploreWindow  # 延迟导入，避免与 explore 模块循环引用

    dialect = "oracle"
    try:
        s = app.get_session(session_name)
        if s:
            dialect = s.dialect
    except Exception:  # noqa: BLE001
        pass
    return ExploreWindow(app, session_name, sql, title=title, dialect=dialect,
                         schema=schema, source_table=table, action=action, column=column)


def _stream_rows(session: DbSession, sql: str):
    for _cols, batch in session.stream(sql.rstrip(";"), chunk=1000):
        for r in batch:
            yield r


class QueryWindow(BaseSubWindow):
    window_kind = "SQL 窗口"

    def __init__(self, app, sql: str = "", session_name: str = ""):
        super().__init__(app, "SQL 查询窗口", session_name)
        self.title(f"SQL 查询 —— {self.session_name}")
        self.sql_file: Optional[str] = None
        self.auto_commit_var = tk.BooleanVar(value=bool(app.settings.get("auto_commit", False)))
        self._build(sql)
        U.center_on_parent(self, app.root, 1100, 720)

    # ---------------- 界面 ----------------
    def _build(self, sql: str):
        toolbar = W.Toolbar(self, padding=(8, 4))
        toolbar.pack(fill=tk.X)
        self._add_session_selector(toolbar, on_change=self._on_session_change)
        toolbar.add_sep()
        toolbar.add_button("▶ 执行 (F5)", self.run_all, "success", tooltip="执行编辑框中的全部语句")
        toolbar.add_button("执行选中 (F9)", self.run_selected, "primary-outline", tooltip="只执行鼠标选中的部分")
        toolbar.add_button("执行计划", self.explain, "info-outline", tooltip="查看 Oracle 执行计划")
        toolbar.add_sep()
        toolbar.add_button("查询向导", self.open_wizard, "primary-outline",
                           tooltip="不用写 SQL，一步步生成查询语句")
        toolbar.add_button("模板", self.open_templates, "info-outline",
                           tooltip="运维诊断 SQL 模板库：阻塞、长事务、空间、统计信息…选完填参数直接插进来")
        toolbar.add_button("历史", self.open_history, "secondary-outline")
        toolbar.add_sep()
        toolbar.add_button("保存脚本", self.save_script, "secondary-outline")
        toolbar.add_button("打开脚本", self.open_script, "secondary-outline")
        toolbar.add_sep()
        toolbar.add_button("提交", self.do_commit, "warning", tooltip="COMMIT：确认保存改动")
        toolbar.add_button("回滚", self.do_rollback, "secondary-outline", tooltip="ROLLBACK：撤销未提交的改动")
        toolbar.add_check("自动提交", self.auto_commit_var, command=self._on_auto_commit)
        toolbar.add_spacer()

        paned = tkttk.PanedWindow(self, orient="vertical")
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        top = tb.Frame(paned)
        paned.add(top, weight=2)
        tb.Label(top, text="SQL 语句（可写多条，用分号隔开）", bootstyle="secondary").pack(anchor="w", pady=(4, 2))
        self.editor = SqlEditor(top, height=12, colors=self.app.editor_colors(), wrap=False)
        self.editor.pack(fill=tk.BOTH, expand=True)
        self.editor.set_sql(sql or "")
        self.editor.text.bind("<F5>", lambda e: self.run_all())
        self.editor.text.bind("<F9>", lambda e: self.run_selected())
        # return "break"：阻止事件继续冒泡到 bind_all 的全局快捷键
        self.editor.text.bind("<Control-s>", lambda e: self._break_after(self.save_script))
        self.editor.text.bind("<Control-o>", lambda e: self._break_after(self.open_script))

        bottom = tb.Frame(paned)
        paned.add(bottom, weight=3)
        self.result_nb = tb.Notebook(bottom, bootstyle="primary")
        self.result_nb.pack(fill=tk.BOTH, expand=True)
        self._welcome_tab()

        self.statusbar = W.StatusBar(self, parts=3, padding=(8, 2))
        self.statusbar.pack(fill=tk.X)
        self.statusbar.set(1, self.session_name)
        self.editor.focus_text()

    def _welcome_tab(self):
        frm = tb.Frame(self.result_nb)
        self.result_nb.add(frm, text="开始")
        tb.Label(frm, text="在这里写 SQL，或点工具栏“查询向导”由系统帮你生成。",
                 bootstyle="secondary").pack(anchor="w", padx=16, pady=14)
        tips = (
            "快捷键：F5 执行全部语句，F9 执行选中语句，Ctrl+S 保存脚本\n"
            "常用语句示例：\n"
            "  SELECT * FROM EMP WHERE DEPTNO = 10;\n"
            "  SELECT DEPTNO, COUNT(*) 人数, AVG(SAL) 平均工资 FROM EMP GROUP BY DEPTNO;\n"
            "  SELECT * FROM EMP ORDER BY HIREDATE DESC;\n"
        )
        tb.Label(frm, text=tips, justify="left").pack(anchor="w", padx=16)

    def _on_session_change(self):
        self.title(f"SQL 查询 —— {self.session_name}")
        self.statusbar.set(1, self.session_name)

    def _on_auto_commit(self):
        self.app.settings.set("auto_commit", bool(self.auto_commit_var.get()))

    # ---------------- 执行 ----------------
    def _current_sql(self, selected_only: bool = False) -> str:
        if selected_only:
            sel = self.editor.get_selected()
            if sel.strip():
                return sel
        return self.editor.get_sql()

    def run_selected(self):
        self._run(self._current_sql(True))

    def run_all(self):
        self._run(self._current_sql(False))

    def _run(self, sql: str):
        sql = (sql or "").strip()
        if not sql:
            W.warning(self, "请输入要执行的 SQL 语句")
            return
        session = self.session
        if not session:
            W.warning(self, "当前没有可用的数据库连接，请先连接数据库")
            return
        stmts = prepare_statements(sql)
        if not stmts:
            return
        for st in stmts:
            kind = SB.statement_kind(st)
            if kind == "SELECT":
                self._run_select(session, st)
            elif kind in ("INSERT", "UPDATE", "DELETE", "MERGE"):
                self._run_dml(session, st, kind)
            elif kind in ("DDL", "PLSQL", "OTHER"):
                self._run_other(session, st, kind)

    def _run_select(self, session: DbSession, sql: str):
        fetch = int(self.app.settings.get("fetch_rows", 200))

        def work(event):
            return session.query(sql, max_rows=fetch)

        ok, res = W.run_async(self, work, title="正在查询", message="数据库正在执行，请稍候…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            from .snapshot import show_error_with_context

            sc, tb_ = SB.detect_table(sql)
            show_error_with_context(self, self.app, f"查询失败：{res}", "查询失败",
                                    sql=sql, schema=sc, table=tb_)
            self.statusbar.set(0, "查询失败")
            return
        try:
            self.app.history.add(sql, tag="SELECT", connection=session.label)
        except Exception:
            pass
        schema, table = SB.detect_table(sql)
        self._add_result_tab(res.columns, res.rows, sql, res.elapsed, res.truncated, (schema, table))
        self.statusbar.set(0, f"查询完成：{U.human_number(len(res.rows))} 行 / {res.elapsed:.2f}s")

    def _add_result_tab(self, columns, rows, sql, elapsed, truncated, base_table):
        idx = self.result_nb.index("end")
        tab = ResultTab(self.result_nb, self, columns, rows, sql, elapsed, truncated, base_table)
        label = f"结果 {idx}" + (f"（{base_table[1]}）" if base_table and base_table[1] else "")
        self.result_nb.add(tab, text=label)
        self.result_nb.select(tab)
        # 只保留最近 10 个结果页
        tabs = self.result_nb.tabs()
        if len(tabs) > 12:
            try:
                self.result_nb.forget(tabs[1])
            except Exception:
                pass

    def _run_dml(self, session: DbSession, sql: str, kind: str):
        schema, table = SB.detect_table(sql)
        ok, msg = confirm_and_execute_dml(self.app, self, session, sql, schema, table, kind)
        self.statusbar.set(0, msg)
        if ok:
            self._show_message(f"{kind} 成功", msg, sql)
            try:
                self.app.refresh_schema()
            except Exception:
                pass
        else:
            self._show_message("已取消", msg, sql)

    def _run_other(self, session: DbSession, sql: str, kind: str):
        guard = getattr(self.app, "guard_write", None)
        if guard is not None and not guard(self, f"{kind} 语句"):
            self.statusbar.set(0, "只读模式下已阻止")
            return
        tip = f"即将执行 {kind} 语句：\n\n{sql[:400]}\n\n这类语句无法自动备份，且通常会立即生效，确认执行吗？"
        if not W.ask_yes(self, tip, "确认执行"):
            return

        def work(event):
            return session.execute(sql)

        ok, res = W.run_async(self, work, title="正在执行", message="正在执行语句…", cancellable=False)
        if not ok:
            from .snapshot import show_error_with_context

            sc, tb_ = SB.detect_table(sql)
            show_error_with_context(self, self.app, f"执行失败：{res}", "执行失败",
                                    sql=sql, schema=sc, table=tb_)
            return
        try:
            session.commit()
        except Exception:
            pass
        msg = f"{kind} 执行完成（已提交），影响 {res} 行"
        try:
            self.app.history.add(sql, tag=kind, connection=session.label)
        except Exception:
            pass
        self._show_message(kind, msg, sql)
        self.statusbar.set(0, msg)

    def _show_message(self, title: str, message: str, sql: str = ""):
        frm = tb.Frame(self.result_nb)
        txt = tk.Text(frm, wrap="word", font=("Microsoft YaHei UI", 10), height=8)
        txt.insert("1.0", (message or "") + ("\n\n" + sql if sql else ""))
        txt.configure(state="disabled")
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.result_nb.add(frm, text=title)
        self.result_nb.select(frm)

    def refresh_current(self):
        """重新执行最近一次查询，刷新结果。"""
        try:
            tab = self.result_nb.nametowidget(self.result_nb.select())
        except Exception:
            return
        if isinstance(tab, ResultTab):
            self._run_select(self.session, tab.sql)

    # ---------------- 其它动作 ----------------
    def explain(self):
        session = self.session
        sql = self._current_sql(True) or self._current_sql(False)
        if not sql.strip():
            return
        sql = prepare_statements(sql)[0]

        def work(event):
            return session.explain(sql)

        ok, res = W.run_async(self, work, title="正在生成执行计划", message="请稍候…", cancellable=False)
        if not ok:
            W.error(self, f"获取执行计划失败：{res}")
            return
        text = "\n".join(str(r[0]) for r in res.rows)
        W.show_text(self, "执行计划", text, width=1000, height=620)

    def open_wizard(self):
        from .builders import QueryWizard

        dlg = QueryWizard(self, self.app, session_name=self.session_name)
        self.wait_window(dlg)
        if dlg.result.get("sql"):
            self.editor.set_sql(dlg.result["sql"])
            if dlg.result.get("execute"):
                self.run_all()

    def open_templates(self):
        from .templates import TemplateDialog

        TemplateDialog(self, self.app, session_name=self.session_name,
                       on_insert=lambda sql: self.editor.insert_text(sql))

    def open_history(self):
        from .dialogs import HistoryDialog

        HistoryDialog(self, self.app, self.app.favorites, on_pick=lambda sql: self.editor.insert_text(sql))

    def save_script(self):
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(parent=self, defaultextension=".sql",
                                            filetypes=[("SQL 脚本", "*.sql"), ("文本文件", "*.txt")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.get_sql())
        self.sql_file = path
        W.toast(self, f"已保存到 {path}", "success")

    def open_script(self):
        from tkinter import filedialog

        path = filedialog.askopenfilename(parent=self, filetypes=[("SQL 脚本", "*.sql"),
                                                                  ("文本文件", "*.txt"),
                                                                  ("所有文件", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            self.editor.set_sql(f.read())
        self.sql_file = path

    def do_commit(self):
        session = self.session
        if not session:
            return
        if not W.ask_yes(self, "提交（COMMIT）后改动将永久生效，无法回滚。是否继续？", "提交"):
            return
        try:
            session.commit()
            W.toast(self, "已提交", "success")
            self.statusbar.set(0, "已提交 COMMIT")
        except Exception as e:  # noqa: BLE001
            W.error(self, f"提交失败：{e}")

    def do_rollback(self):
        session = self.session
        if not session:
            return
        if not W.ask_yes(self, "回滚（ROLLBACK）将撤销所有未提交的改动。是否继续？", "回滚"):
            return
        try:
            session.rollback()
            W.toast(self, "已回滚", "success")
            self.statusbar.set(0, "已回滚 ROLLBACK")
        except Exception as e:  # noqa: BLE001
            W.error(self, f"回滚失败：{e}")

    def apply_theme(self):
        self.editor.apply_colors(self.app.editor_colors())


# ================================================================== 数据表窗口
class TableWindow(BaseSubWindow):
    window_kind = "数据窗口"

    def __init__(self, app, schema: str = "", table: str = "", session_name: str = ""):
        super().__init__(app, "数据表", session_name)
        self.schema = schema
        self.table = table
        self.columns: List[ColumnMeta] = []
        self.last_sql = ""
        self.title("数据表浏览")
        self._build()
        if schema and table:
            self._load_columns(silent=True)
            self.refresh_data()
        U.center_on_parent(self, app.root, 1160, 760)

    # ---------------- 界面 ----------------
    def _build(self):
        toolbar = W.Toolbar(self, padding=(8, 4))
        toolbar.pack(fill=tk.X)
        self._add_session_selector(toolbar, on_change=self._on_session_change)
        toolbar.add_sep()
        toolbar.add_widget(tb.Label(toolbar, text="模式"), padx=(0, 2))
        self.schema_var = tk.StringVar()
        self.schema_combo = toolbar.add_widget(
            tb.Combobox(toolbar, textvariable=self.schema_var, width=16, state="readonly"), padx=(0, 10))
        self.schema_combo.bind("<<ComboboxSelected>>", lambda e: self._load_tables())
        toolbar.add_widget(tb.Label(toolbar, text="表"), padx=(0, 2))
        self.table_var = tk.StringVar()
        self.table_combo = toolbar.add_widget(
            tb.Combobox(toolbar, textvariable=self.table_var, width=26, state="readonly"), padx=(0, 10))
        self.table_combo.bind("<<ComboboxSelected>>", lambda e: self._on_table_change())
        toolbar.add_button("刷新列表", self._load_tables, "secondary-outline")
        toolbar.add_sep()
        toolbar.add_button("表结构", self.show_structure, "info-outline")
        toolbar.add_button("提交", self.do_commit, "warning")
        toolbar.add_button("回滚", self.do_rollback, "secondary-outline")
        toolbar.add_spacer()

        # ---- 引导区 ----
        guide = tb.Labelframe(self, text="① 设置筛选条件（可留空 = 查整表）", padding=8)
        guide.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.cond_editor = ConditionEditor(guide, [], on_change=self._auto_generate)
        self.cond_editor.pack(fill=tk.X)

        row2 = tb.Frame(guide)
        row2.pack(fill=tk.X, pady=(6, 0))
        tb.Label(row2, text="排序字段").pack(side=tk.LEFT)
        self.order_col = tk.StringVar()
        self.order_combo = tb.Combobox(row2, textvariable=self.order_col, width=20, state="readonly")
        self.order_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.order_dir = tk.StringVar(value="ASC  升序")
        tb.Combobox(row2, textvariable=self.order_dir, values=["ASC  升序", "DESC  降序"], width=11,
                    state="readonly").pack(side=tk.LEFT, padx=(4, 12))
        tb.Label(row2, text="最多行数").pack(side=tk.LEFT)
        self.limit_var = tk.StringVar(value=str(self.app.settings.get("fetch_rows", 200)))
        tb.Spinbox(row2, from_=0, to=500000, textvariable=self.limit_var, width=9).pack(side=tk.LEFT, padx=(4, 0))
        tb.Button(row2, text="生成语句", bootstyle="info", command=self.generate_sql).pack(side=tk.LEFT, padx=(14, 0))
        tb.Button(row2, text="执行查询 (F5)", bootstyle="success", command=self.refresh_data).pack(side=tk.LEFT, padx=(6, 0))
        self.note_var = tk.StringVar(value="")
        tb.Label(row2, textvariable=self.note_var, bootstyle="secondary").pack(side=tk.LEFT, padx=(12, 0))

        preview_box = tb.Labelframe(self, text="② 生成的语句（可以直接改，改完点“执行查询”）", padding=6)
        preview_box.pack(fill=tk.X, padx=8, pady=(6, 0))
        self.preview = SqlEditor(preview_box, height=6, colors=self.app.editor_colors(), wrap=False)
        self.preview.pack(fill=tk.X)
        self.preview.text.bind("<F5>", lambda e: self.refresh_data())

        # ---- 数据区 ----
        data_bar = tb.Frame(self, padding=(8, 6, 8, 0))
        data_bar.pack(fill=tk.X)
        self.info_var = tk.StringVar(value="还没有数据")
        tb.Label(data_bar, textvariable=self.info_var, bootstyle="secondary").pack(side=tk.LEFT)
        tb.Button(data_bar, text="导出 Excel", bootstyle="success-outline", command=self.export).pack(side=tk.RIGHT)
        tb.Button(data_bar, text="删除行…", bootstyle="danger-outline", command=self.delete_row).pack(side=tk.RIGHT, padx=(0, 8))
        tb.Button(data_bar, text="修改行…", bootstyle="warning-outline", command=self.edit_row).pack(side=tk.RIGHT, padx=(0, 8))
        tb.Button(data_bar, text="新增行…", bootstyle="primary-outline", command=self.insert_row).pack(side=tk.RIGHT, padx=(0, 8))

        # ---- 分页栏（行数多时只渲染一页，翻页浏览） ----
        pager = tb.Frame(self, padding=(8, 4, 8, 0))
        pager.pack(fill=tk.X)
        tb.Label(pager, text="每页").pack(side=tk.LEFT)
        self.page_size_var = tk.StringVar(value="200")
        ps = tb.Combobox(pager, textvariable=self.page_size_var,
                         values=["100", "200", "500", "1000", "2000", "5000"],
                         width=6, state="readonly")
        ps.pack(side=tk.LEFT, padx=(2, 0))
        ps.bind("<<ComboboxSelected>>", lambda e: self._on_page_size())
        tb.Label(pager, text="行").pack(side=tk.LEFT, padx=(0, 12))
        tb.Button(pager, text="⏮ 首页", width=7, bootstyle="secondary-outline",
                  command=lambda: self.data_grid.first_page()).pack(side=tk.LEFT)
        tb.Button(pager, text="◀ 上一页", width=8, bootstyle="secondary-outline",
                  command=lambda: self.data_grid.prev_page()).pack(side=tk.LEFT, padx=(4, 0))
        self.page_var = tk.StringVar(value="")
        tb.Label(pager, textvariable=self.page_var).pack(side=tk.LEFT, padx=10)
        tb.Button(pager, text="下一页 ▶", width=8, bootstyle="secondary-outline",
                  command=lambda: self.data_grid.next_page()).pack(side=tk.LEFT)
        tb.Button(pager, text="末页 ⏭", width=7, bootstyle="secondary-outline",
                  command=lambda: self.data_grid.last_page()).pack(side=tk.LEFT, padx=(4, 0))

        colors = self.app.grid_colors()
        self.data_grid = DataTable(self, callbacks={
            "view": self.view_row, "edit": self.edit_row, "delete": self.delete_row,
            "insert": self.insert_row, "export": self.export,
            "dup": lambda c: self._explore(c, "dup"),
            "distinct": lambda c: self._explore(c, "distinct"),
            "drill": lambda c: self._explore(c, "drill"),
        }, date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"),
            null_display=self.app.settings.get("null_display", ""),
            show_rowno=bool(self.app.settings.get("show_row_number", True)),
            max_rows=int(self.app.settings.get("max_grid_rows", 20000)),
            page_size=200,
            even_color=colors["even"], odd_color=colors["odd"])
        self.data_grid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))
        self.data_grid.on_refresh = self._on_grid_refresh

        self.statusbar = W.StatusBar(self, parts=2, padding=(8, 2))
        self.statusbar.pack(fill=tk.X)

        self._load_schemas()

    def _on_session_change(self):
        self.statusbar.set(1, self.session_name)
        self._load_schemas()

    def _load_schemas(self):
        session = self.session
        if not session:
            return
        try:
            schemas = session.schemas()
        except Exception:
            schemas = []
        cur = self.schema or session.default_schema
        if cur and cur not in schemas:
            schemas = [cur] + list(schemas)
        self.schema_combo.configure(values=schemas)
        if cur:
            self.schema_var.set(cur)
            self.schema = cur
        self._load_tables()

    def _load_tables(self):
        session = self.session
        if not session:
            return
        self.schema = self.schema_var.get()
        try:
            objs = session.objects(self.schema)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"读取表列表失败：{e}")
            return
        names = [o.name for o in objs]
        self.table_combo.configure(values=names)
        if self.table and self.table in names:
            self.table_var.set(self.table)
        elif names:
            self.table_var.set(names[0])
            self.table = names[0]

    def _on_table_change(self):
        self.table = self.table_var.get()
        self._load_columns()
        self.generate_sql()
        self.refresh_data()

    def _load_columns(self, silent: bool = False):
        session = self.session
        if not session or not self.table:
            return
        try:
            self.columns = session.columns(self.schema, self.table)
        except Exception as e:  # noqa: BLE001
            if not silent:
                W.error(self, f"读取字段失败：{e}")
            self.columns = []
            return
        self.cond_editor.set_columns(self.columns)
        if not self.cond_editor.rows:
            self.cond_editor.add_row()
        names = [""] + [c.name for c in self.columns]
        self.order_combo.configure(values=names)
        self.order_col.set("")
        self.title(f"数据表 —— {self.schema}.{self.table}")

    # ---------------- 生成与执行 ----------------
    def _spec(self) -> QuerySpec:
        conds = self.cond_editor.conditions()
        terms = []
        if self.order_col.get():
            from ..core.models import OrderTerm

            terms.append(OrderTerm(column=self.order_col.get(),
                                   direction=(self.order_dir.get() or "ASC").split()[0]))
        try:
            limit = int(self.limit_var.get() or 0)
        except Exception:
            limit = 200
        return QuerySpec(schema=self.schema, table=self.table, conditions=conds,
                         order_by=terms, limit=limit)

    def _col_types(self) -> Dict[str, str]:
        return {c.name.upper(): c.data_type for c in self.columns}

    def generate_sql(self):
        if not self.table:
            return ""
        session = self.session
        dialect = session.dialect if session else "oracle"
        sql = SB.build_select(self._spec(), self._col_types(), dialect)
        limit = self._spec().limit
        if limit:
            sql = SB.apply_row_limit(sql, limit, dialect)
        self.last_sql = sql
        self.preview.set_sql(sql)
        return sql

    def _auto_generate(self):
        try:
            self.generate_sql()
        except Exception:
            pass

    def refresh_data(self):
        session = self.session
        if not session:
            W.warning(self, "没有可用的数据库连接")
            return
        if not self.table:
            W.warning(self, "请先选择一张表")
            return
        if not self.winfo_exists():
            return
        ok, msg = self.cond_editor.validate_all()
        if not ok:
            W.warning(self, msg)
            return
        sql = self.preview.get_sql().strip() or self.generate_sql()
        if not sql:
            return
        self.last_sql = sql
        # “最多行数”就是这次要取回的行数，别再拿全局 fetch_rows 截一道——
        # 之前界面填 500、底下仍只显示 200 就是这个原因。
        # 填 0 表示不限制行数，用网格上限（默认 2 万行）兜底，防止大表拖垮内存。
        try:
            limit = int(self.limit_var.get() or 0)
        except Exception:
            limit = 0
        fetch = limit if limit > 0 else int(self.app.settings.get("max_grid_rows", 20000))

        def work(event):
            return session.query(sql.rstrip(";"), max_rows=fetch)

        ok, res = W.run_async(self, work, title="正在查询", message=f"正在读取 {self.schema}.{self.table} …",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            from .snapshot import show_error_with_context

            show_error_with_context(self, self.app, f"查询失败：{res}", "查询失败",
                                    sql=sql, schema=self.schema, table=self.table)
            self.statusbar.set(0, "查询失败")
            return
        self.data_grid.set_data(res.columns, res.rows, res.truncated)
        txt = f"共 {U.human_number(len(res.rows))} 行   耗时 {res.elapsed:.2f}s"
        if res.truncated:
            txt += "   （数据库中还有更多，可调大“最多行数”）"
        self.info_var.set(txt)
        self.statusbar.set(0, f"查询完成 {U.human_number(len(res.rows))} 行")
        try:
            self.app.history.add(sql, tag="SELECT", connection=session.label)
        except Exception:
            pass

    # ---------------- 分页 ----------------
    def _on_page_size(self):
        try:
            n = int(self.page_size_var.get())
        except Exception:
            n = 200
        self.data_grid.set_page_size(n)

    def _on_grid_refresh(self, shown: int, total: int):
        """网格每次重绘后同步分页信息（由 DataTable.on_refresh 回调）。"""
        g = self.data_grid
        if g.page_size > 0:
            self.page_var.set(f"第 {g.current_page()} / {g.page_count()} 页")
        else:
            self.page_var.set("")

    # ---------------- 行操作 ----------------
    def _row_dict(self, row: Sequence[Any]) -> Dict[str, Any]:
        return {c: v for c, v in zip(self.data_grid.columns, row)}

    def view_row(self):
        rows = self.data_grid.selected_rows()
        if rows:
            show_row_detail(self, self.data_grid.columns, rows[0],
                            date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"))

    def _explore(self, col: str, action: str):
        """以当前表 + 当前筛选条件为起点做数据探查。"""
        sql = (self.preview.get_sql().strip() or self.last_sql or self.generate_sql())
        if not sql:
            W.warning(self, "请先生成或执行一条查询语句")
            return
        open_explore(self, self.app, self.session_name, sql,
                     title=f"探查 {self.schema}.{self.table}".strip("."),
                     schema=self.schema, table=self.table, action=action, column=col)

    def edit_row(self):
        rows = self.data_grid.selected_rows()
        if not rows:
            W.warning(self, "请先在表格里选中一行")
            return
        if not self.columns:
            return
        dlg = UpdateDialog(self, self.app, self.session_name, self.schema, self.table, self.columns,
                           self._row_dict(rows[0]))
        self.wait_window(dlg)
        if dlg.ok:
            self.refresh_data()

    def delete_row(self):
        rows = self.data_grid.selected_rows()
        if not rows:
            W.warning(self, "请先在表格里选中一行")
            return
        if not self.columns:
            return
        dlg = DeleteDialog(self, self.app, self.session_name, self.schema, self.table, self.columns,
                           self._row_dict(rows[0]))
        self.wait_window(dlg)
        if dlg.ok:
            self.refresh_data()

    def insert_row(self):
        if not self.columns:
            return
        dlg = InsertDialog(self, self.app, self.session_name, self.schema, self.table, self.columns)
        self.wait_window(dlg)
        if dlg.ok:
            self.refresh_data()

    def show_structure(self):
        if not self.table:
            return
        from .dialogs import TableInfoDialog

        TableInfoDialog(self, self.app, self.session_name, self.schema, self.table)

    def export(self):
        from .dialogs import ExportDialog

        visible = self.data_grid.visible_rows()
        allrows = [self.data_grid.raw[i] for i in self.data_grid.order]
        session = self.session

        def provider_visible():
            return self.data_grid.columns, visible

        def provider_all():
            return self.data_grid.columns, allrows

        def provider_db():
            if not session:
                return self.data_grid.columns, []
            sql = (self.preview.get_sql().strip() or self.last_sql).rstrip(";")
            if self._spec().limit:
                sql = SB.apply_row_limit(sql.rstrip(";"), 0, session.dialect).rstrip(";")
            return self.data_grid.columns, _stream_rows(session, sql)

        providers = {"当前视图（含筛选和排序）": provider_visible,
                     "已取回的全部数据": provider_all,
                     "重新从数据库取全部（不限制行数）": provider_db}
        ExportDialog(self, self.app, default_name=self.table or "数据", providers=providers,
                     table_name=self.table)

    def do_commit(self):
        session = self.session
        if not session:
            return
        if W.ask_yes(self, "提交（COMMIT）后改动永久生效且无法回滚。是否继续？", "提交"):
            try:
                session.commit()
                W.toast(self, "已提交", "success")
            except Exception as e:  # noqa: BLE001
                W.error(self, f"提交失败：{e}")

    def do_rollback(self):
        session = self.session
        if not session:
            return
        if W.ask_yes(self, "回滚（ROLLBACK）会撤销所有未提交的改动。是否继续？", "回滚"):
            try:
                session.rollback()
                W.toast(self, "已回滚", "success")
            except Exception as e:  # noqa: BLE001
                W.error(self, f"回滚失败：{e}")

    def apply_theme(self):
        self.preview.apply_colors(self.app.editor_colors())
