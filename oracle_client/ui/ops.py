"""运维概览：实例概览 / 慢 SQL / 表空间 / 权限自检。

给运维和 DBA 做的“开机先看一眼”的页面。三条硬规则：
  1. 每个人权限不同 → 所有数据都按权限级别降级，绝不因为缺权限就白屏；
  2. 降级要在界面上写清楚原因（“需要 SELECT_CATALOG_ROLE”），让人知道该找谁要权限；
  3. 只读——这个页面不提供任何改数据的入口。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ttkbootstrap as tb

from ..core import ops as OPS
from ..core import perms as PM
from ..core import utils as U
from . import widgets as W
from .grid import DataTable, show_row_detail
from .templates import TemplatePicker
from .windows import BaseSubWindow

AUTO_INTERVALS = {"不自动刷新": 0, "每 60 秒": 60000, "每 5 分钟": 300000, "每 15 分钟": 900000}


class OpsWindow(BaseSubWindow):
    window_kind = "运维概览"

    def __init__(self, app, session_name: str = ""):
        super().__init__(app, "运维概览", session_name)
        self.title("运维概览（只读）")
        self.probe: Optional[PM.PrivilegeProbe] = None
        self._auto_key = "不自动刷新"
        self._after_id = None
        self._build()
        U.center_on_parent(self, app.root, 1200, 800)
        self.after(200, self.refresh_all)

    # ================================================================ 界面
    def _build(self):
        toolbar = W.Toolbar(self, padding=(8, 4))
        toolbar.pack(fill=tk.X)
        self._add_session_selector(toolbar, on_change=self._on_session_change)
        toolbar.add_sep()
        toolbar.add_button("刷新  F5", self.refresh_all, "success")
        toolbar.add_widget(tb.Label(toolbar, text="自动刷新"), padx=(6, 2))
        self.auto_var = tk.StringVar(value=self._auto_key)
        cb = toolbar.add_combo(self.auto_var, list(AUTO_INTERVALS.keys()), width=12,
                               command=self._on_auto_change)
        toolbar.add_spacer()

        self.nb = tkttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 0))

        self._build_overview()
        self._build_slow()
        self._build_tablespace()
        self._build_templates()
        self._build_perms()

        self.statusbar = W.StatusBar(self, parts=2, padding=(8, 2))
        self.statusbar.pack(fill=tk.X)
        self.bind("<F5>", lambda e: self.refresh_all())

    def _grid(self, master, callbacks=None, height: int = 12) -> DataTable:
        colors = self.app.grid_colors()
        g = DataTable(master, callbacks=callbacks or {},
                      date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"),
                      null_display=self.app.settings.get("null_display", ""),
                      show_rowno=bool(self.app.settings.get("show_row_number", True)),
                      max_rows=int(self.app.settings.get("max_grid_rows", 20000)),
                      even_color=colors["even"], odd_color=colors["odd"])
        g.pack(fill=tk.BOTH, expand=True)
        return g

    # ---------------- 实例概览 ----------------
    def _build_overview(self):
        page = tb.Frame(self.nb, padding=10)
        self.nb.add(page, text=" 实例概览 ")
        self.ov_note = tb.Label(page, text="", bootstyle="secondary", wraplength=1100, justify="left")
        self.ov_note.pack(anchor="w", pady=(0, 6))
        self.ov_grid = self._grid(page, {"view": lambda: show_row_detail(
            self, self.ov_grid.columns, (self.ov_grid.selected_rows() or [()])[0])})
        tb.Label(page, text="会话分布（按状态 / 类型）", bootstyle="primary").pack(anchor="w", pady=(10, 4))
        self.sess_note = tb.Label(page, text="", bootstyle="secondary", wraplength=1100, justify="left")
        self.sess_note.pack(anchor="w")
        self.sess_grid = self._grid(page)

    # ---------------- 慢 SQL ----------------
    def _build_slow(self):
        page = tb.Frame(self.nb, padding=10)
        self.nb.add(page, text=" 慢 SQL ")

        bar = tb.Frame(page)
        bar.pack(fill=tk.X)
        self.top_var = tk.StringVar(value="50")
        tb.Label(bar, text="取前").pack(side=tk.LEFT)
        tk.Spinbox(bar, from_=10, to=1000, increment=10, textvariable=self.top_var,
                   width=6).pack(side=tk.LEFT, padx=(4, 12))
        tb.Label(bar, text="排序").pack(side=tk.LEFT)
        self.order_var = tk.StringVar(value="平均耗时")
        tb.Combobox(bar, textvariable=self.order_var, values=list(OPS.SLOW_ORDER.keys()),
                    width=12, state="readonly").pack(side=tk.LEFT, padx=(4, 12))
        tb.Label(bar, text="平均耗时≥").pack(side=tk.LEFT)
        self.avg_var = tk.StringVar(value="0")
        tb.Entry(bar, textvariable=self.avg_var, width=7).pack(side=tk.LEFT, padx=(4, 2))
        tb.Label(bar, text="秒").pack(side=tk.LEFT, padx=(0, 12))
        tb.Label(bar, text="执行次数≥").pack(side=tk.LEFT)
        self.exec_var = tk.StringVar(value="1")
        tb.Entry(bar, textvariable=self.exec_var, width=7).pack(side=tk.LEFT, padx=(4, 12))
        tb.Label(bar, text="SQL 里包含").pack(side=tk.LEFT)
        self.kw_var = tk.StringVar(value="")
        e = tb.Entry(bar, textvariable=self.kw_var, width=18)
        e.pack(side=tk.LEFT, padx=(4, 12))
        e.bind("<Return>", lambda ev: self.refresh_slow())
        tb.Button(bar, text="查询", bootstyle="success", command=self.refresh_slow).pack(side=tk.LEFT)
        btn_send = tb.Button(bar, text="发到查询窗口", bootstyle="info-outline", command=self.send_sql)
        btn_send.pack(side=tk.LEFT, padx=(8, 0))
        W._attach_tooltip(btn_send, "把选中的慢 SQL 丢进查询窗口接着改、看执行计划")

        self.slow_note = tb.Label(page, text="", bootstyle="secondary", wraplength=1100, justify="left")
        self.slow_note.pack(anchor="w", pady=(8, 4))
        self.slow_grid = self._grid(page, {"view": self.view_sql,
                                           "export": self.export_slow})
        self.slow_grid.tree.tag_configure("hot", background="#fde2e1")

    # ---------------- 表空间 ----------------
    def _build_tablespace(self):
        page = tb.Frame(self.nb, padding=10)
        self.nb.add(page, text=" 表空间 ")

        bar = tb.Frame(page)
        bar.pack(fill=tk.X)
        self.show_seg_var = tk.BooleanVar(value=False)
        tb.Checkbutton(bar, text="同时显示占空间最大的段", variable=self.show_seg_var,
                       bootstyle="primary-round-toggle",
                       command=self._toggle_segments).pack(side=tk.LEFT)
        self.seg_top_var = tk.StringVar(value="30")
        tk.Spinbox(bar, from_=10, to=200, increment=10, textvariable=self.seg_top_var,
                   width=6).pack(side=tk.LEFT, padx=(6, 0))
        tb.Label(bar, text="个").pack(side=tk.LEFT)

        self.ts_note = tb.Label(page, text="", bootstyle="secondary", wraplength=1100, justify="left")
        self.ts_note.pack(anchor="w", pady=(8, 4))
        self.ts_grid = self._grid(page, {"export": self.export_ts})
        for name, color in (("warn", "#fff3cd"), ("danger", "#fde2e1")):
            self.ts_grid.tree.tag_configure(name, background=color)
        self.ts_grid.set_row_tag_fn(self._ts_tag)

        self.seg_box = tb.Labelframe(page, text="占空间最大的段", padding=6)
        self.seg_grid = DataTable(self.seg_box, callbacks={},
                                  date_format=self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"),
                                  null_display=self.app.settings.get("null_display", ""),
                                  even_color=self.app.grid_colors()["even"],
                                  odd_color=self.app.grid_colors()["odd"])
        self.seg_grid.pack(fill=tk.BOTH, expand=True)

    def _ts_tag(self, row: Sequence[Any]) -> str:
        """使用率 ≥90 标红、≥75 标黄。"""
        try:
            cols = [str(c) for c in self.ts_grid.columns]
            for key in ("使用率%", "占配额%"):
                if key in cols:
                    v = float(row[cols.index(key)])
                    return "danger" if v >= 90 else ("warn" if v >= 75 else "")
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _toggle_segments(self):
        if self.show_seg_var.get():
            self.seg_box.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
            self.refresh_segments()
        else:
            self.seg_box.pack_forget()

    # ---------------- SQL 模板库 ----------------
    def _build_templates(self):
        page = tb.Frame(self.nb, padding=10)
        self.tpl_page = page
        self.nb.add(page, text=" SQL 模板 ")

        self.tpl_picker = TemplatePicker(
            page, self.app, session_name=self.session_name,
            callbacks={"run": self._fill_template_result},
            on_status=lambda text: self.statusbar.set(0, text))
        self.tpl_picker.pack(fill=tk.BOTH, expand=True)

        self.tpl_note = tb.Label(page, text="", bootstyle="secondary", wraplength=1100,
                                 justify="left")
        self.tpl_note.pack(anchor="w", pady=(8, 2))
        self.tpl_grid = self._grid(page, {"export": self.export_template})

    def _fill_template_result(self, cols, rows, note, template):
        self.tpl_grid.set_data(cols, rows)
        head = f"“{template.name}”"
        self.tpl_note.configure(text=((head + "　·　" + note) if note else head))
        try:
            self.nb.select(self.tpl_page)      # 结果出来后把模板页签切到最前
        except Exception:
            pass

    def export_template(self):
        if not self.tpl_grid.columns:
            W.warning(self, "还没有执行任何模板")
            return
        self._export(self.tpl_grid, (self.tpl_picker.current.name
                                     if self.tpl_picker.current else "模板结果"))

    # ---------------- 权限自检 ----------------
    def _build_perms(self):
        page = tb.Frame(self.nb, padding=10)
        body = W.ScrolledBody(self.nb, padding=10)
        self.nb.add(body, text=" 权限自检 ")
        page = body.inner
        bar = tb.Frame(page)
        bar.pack(fill=tk.X)
        tb.Button(bar, text="重新检测", bootstyle="info", command=self.refresh_perms).pack(side=tk.LEFT)
        tb.Button(bar, text="复制诊断信息（发给 DBA）", bootstyle="success-outline",
                  command=self.copy_diag).pack(side=tk.LEFT, padx=(8, 0))
        tb.Label(bar, text="　每个人账号权限不同，这里如实展示当前账号能看什么、缺什么",
                 bootstyle="secondary").pack(side=tk.LEFT)

        tb.Label(page, text="功能可用情况", bootstyle="primary").pack(anchor="w", pady=(10, 4))
        self.feat_grid = self._grid(page)
        tb.Label(page, text="视图 / 字典可用性", bootstyle="primary").pack(anchor="w", pady=(10, 4))
        self.perm_grid = self._grid(page)
        self.perm_grid.set_row_tag_fn(lambda r: "no" if (len(r) > 1 and str(r[1]) == "✘") else "")
        self.perm_grid.tree.tag_configure("no", background="#fde2e1")
        tb.Label(page, text="角色与系统权限", bootstyle="primary").pack(anchor="w", pady=(10, 4))
        self.role_grid = self._grid(page)

    # ================================================================ 取数
    def _on_session_change(self):
        self.statusbar.set(1, self.session_name)
        self.probe = None
        # 模板库要按新连接重新判断每条模板能不能跑
        picker = getattr(self, "tpl_picker", None)
        if picker is not None:
            picker.set_session(self.session_name)
        self.refresh_all()

    def _on_auto_change(self):
        self._auto_key = self.auto_var.get()
        self._schedule_auto()

    def _schedule_auto(self):
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
            self._after_id = None
        ms = AUTO_INTERVALS.get(self._auto_key, 0)
        if ms and self.winfo_exists():
            self._after_id = self.after(ms, self._auto_tick)

    def _auto_tick(self):
        self._after_id = None
        if self.winfo_exists():
            self.refresh_all()

    def _probe(self):
        session = self.session
        if not session:
            return None
        if self.probe is None:
            self.probe = PM.probe_for(session)
        return self.probe

    def _slow_kwargs(self) -> dict:
        def num(var, default):
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return default
        return dict(top_n=int(num(self.top_var, 50) or 50),
                    min_avg_sec=num(self.avg_var, 0.0),
                    min_execs=int(num(self.exec_var, 1) or 1),
                    order_by=self.order_var.get() or "平均耗时",
                    keyword=self.kw_var.get().strip())

    def refresh_all(self):
        session = self.session
        if not session:
            W.warning(self, "当前没有数据库连接")
            return
        probe = self._probe()
        kw = self._slow_kwargs()
        want_seg = bool(self.show_seg_var.get())

        def work(ev):
            out: Dict[str, Any] = {}
            out["instance"] = OPS.fetch(session, probe, "instance")
            out["sessions"] = OPS.fetch(session, probe, "sessions")
            out["slow"] = OPS.fetch(session, probe, "slow", **kw)
            out["tablespace"] = OPS.fetch(session, probe, "tablespace")
            out["segments"] = OPS.fetch(session, probe, "segments",
                                        top_n=int(self.seg_top_var.get() or 30)) if want_seg else None
            out["features"] = probe.feature_report()
            out["perms"] = probe.report()
            out["roles"] = probe.role_states()
            return out

        ok, res = W.run_async(self, work, title="正在读取", message="正在检测权限并查询实例信息…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"读取失败：{res}")
            self.statusbar.set(0, "读取失败")
            return
        self._fill(res)
        self.statusbar.set(0, "已刷新")
        self.statusbar.set(1, self.session_name)
        self._schedule_auto()

    def refresh_slow(self):
        session = self.session
        if not session:
            return
        probe = self._probe()
        kw = self._slow_kwargs()

        def work(ev):
            return OPS.fetch(session, probe, "slow", **kw)

        ok, res = W.run_async(self, work, title="正在查询", message="正在统计 Top SQL…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"查询失败：{res}")
            return
        self._fill_slow(res)

    def refresh_segments(self):
        session = self.session
        if not session:
            return
        probe = self._probe()

        def work(ev):
            return OPS.fetch(session, probe, "segments", top_n=int(self.seg_top_var.get() or 30))

        ok, res = W.run_async(self, work, title="正在查询", message="正在统计段占用…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"查询失败：{res}")
            return
        cols, rows, note, level = res
        self.seg_grid.set_data(cols, rows)
        if note:
            self.seg_box.configure(text="占空间最大的段　·　" + note)

    def refresh_perms(self):
        if self.probe:
            self.probe.refresh()
        session = self.session
        if not session:
            return

        def work(ev):
            probe = self._probe()
            return {"features": probe.feature_report(), "perms": probe.report(),
                    "roles": probe.role_states()}

        ok, res = W.run_async(self, work, title="正在检测", message="正在逐项检测视图可用性…",
                              cancellable=False)
        if not ok:
            W.error(self, f"检测失败：{res}")
            return
        self._fill_perms(res)

    # ================================================================ 填充
    def _fill(self, data: dict):
        self._fill_kv(self.ov_grid, self.ov_note, data.get("instance"))
        self._fill_kv(self.sess_grid, self.sess_note, data.get("sessions"))
        self._fill_slow(data.get("slow"))
        self._fill_kv(self.ts_grid, self.ts_note, data.get("tablespace"))
        seg = data.get("segments")
        if seg:
            cols, rows, note, level = seg
            self.seg_grid.set_data(cols, rows)
            if note:
                self.seg_box.configure(text="占空间最大的段　·　" + note)
        self._fill_perms(data)

    def _fill_kv(self, grid: DataTable, note_label, payload):
        if not payload:
            grid.set_data([], [])
            return
        cols, rows, note, level = payload
        grid.set_data(cols, rows)
        if note_label is not None:
            note_label.configure(text=note or "")

    def _fill_slow(self, payload):
        if not payload:
            return
        cols, rows, note, level = payload
        self.slow_grid.set_data(cols, rows)
        self.slow_grid.set_row_tag_fn(
            (lambda r: "hot" if self._slow_hot(r) else "") if cols else None)
        self.slow_note.configure(text=note or "")

    def _slow_hot(self, row) -> bool:
        try:
            cols = [str(c) for c in self.slow_grid.columns]
            i = cols.index("平均耗时秒")
            return float(row[i]) >= 1.0
        except Exception:  # noqa: BLE001
            return False

    def _fill_perms(self, data: dict):
        feats = data.get("features") or []
        self.feat_grid.set_data(
            ["功能", "级别", "说明"],
            [(name, ("不可用" if lv == "none" else ("完整" if lv == "full" else lv)), desc)
             for name, lv, desc in feats])
        perms = data.get("perms") or []
        self.perm_grid.set_data(
            ["能力", "可用", "说明 / 缺少时需要什么"],
            [(p["label"], "✔" if p["ok"] else "✘",
              ("" if p["ok"] else (p.get("need", "") or p.get("error", "")))) for p in perms])
        roles = data.get("roles") or []
        self.role_grid.set_data(
            ["角色 / 系统权限", "是否拥有", "缺少时怎么申请"],
            [(r["label"], "✔" if r["ok"] else "✘", "" if r["ok"] else r["need"]) for r in roles])

    # ================================================================ 操作
    def _cell(self, grid: DataTable, row: Sequence[Any], name: str) -> Any:
        cols = [str(c) for c in grid.columns]
        return row[cols.index(name)] if name in cols else None

    def view_sql(self):
        """看某条慢 SQL 的完整文本（双击或右键“查看整行详情”）。"""
        rows = self.slow_grid.selected_rows()
        if not rows:
            W.warning(self, "请先在列表里选中一行")
            return
        row = rows[0]
        sql_id = self._cell(self.slow_grid, row, "SQL_ID")
        session = self.session
        if not session or not sql_id:
            show_row_detail(self, self.slow_grid.columns, row)
            return
        level = self._probe().level("slow_sql") if self._probe() else "sqlarea"

        def work(ev):
            return self._full_text(row)

        ok, text = W.run_async(self, work, title="正在读取", message="正在取完整 SQL 文本…",
                               cancellable=False)
        text = (text if ok else "") or ""
        if not text:
            W.warning(self, "没有取到 SQL 文本")
            return
        W.show_text(self, f"完整 SQL（SQL_ID={sql_id}）", text, width=1000, height=660)

    def send_sql(self):
        """把选中的慢 SQL 丢进查询窗口，方便接着改、看执行计划。"""
        rows = self.slow_grid.selected_rows()
        if not rows:
            W.warning(self, "请先在列表里选中一行")
            return
        text = self._full_text(rows[0])
        if not text:
            W.warning(self, "没有取到 SQL 文本")
            return
        try:
            self.app.new_query_window(sql=text.strip().rstrip(";") + ";")
        except Exception as e:  # noqa: BLE001
            W.error(self, f"打开查询窗口失败：{e}")

    def _full_text(self, row: Sequence[Any]) -> str:
        """优先取完整文本，取不到就退回列表里那 220 字符。"""
        session = self.session
        sql_id = self._cell(self.slow_grid, row, "SQL_ID")
        if session and sql_id:
            level = self._probe().level("slow_sql") if self._probe() else "sqlarea"
            try:
                res = session.query(OPS.sql_text_by_id(sql_id, level), max_rows=1)
                if res.rows and res.rows[0][0]:
                    return str(res.rows[0][0])
            except Exception:  # noqa: BLE001
                pass
        return str(self._cell(self.slow_grid, row, "SQL 文本（前 220 字符）") or "")

    def export_slow(self):
        self._export(self.slow_grid, "慢SQL")

    def export_ts(self):
        self._export(self.ts_grid, "表空间")

    def _export(self, grid: DataTable, name: str):
        if not grid.columns:
            W.warning(self, "没有可导出的数据")
            return
        from .dialogs import ExportDialog

        visible = grid.visible_rows()
        allrows = [grid.raw[i] for i in grid.order]
        ExportDialog(self, self.app, default_name=name,
                     providers={"当前视图（含排序）": lambda: (grid.columns, visible),
                                "全部数据": lambda: (grid.columns, allrows)})

    def copy_diag(self):
        session = self.session
        if not session:
            W.warning(self, "当前没有连接")
            return
        probe = self._probe()
        user = getattr(session.adapter, "current_user", "") or ""
        version = getattr(session.adapter, "server_version", "") or ""
        text = probe.summary_text(user=user, version=version)
        U.copy_to_clipboard(self, text)
        W.show_text(self, "权限自检结果（已复制到剪贴板）", text, width=900, height=620)

    def apply_theme(self):
        colors = self.app.grid_colors()
        for g in (self.ov_grid, self.sess_grid, self.slow_grid, self.ts_grid,
                  self.seg_grid, self.tpl_grid, self.feat_grid, self.perm_grid,
                  self.role_grid):
            try:
                g.apply_colors(colors["even"], colors["odd"])
            except Exception:  # noqa: BLE001
                pass
