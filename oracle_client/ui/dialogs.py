"""各类对话框：连接管理、导出、设置、备份还原中心、表结构、历史记录、关于。"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk as tkttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import ttkbootstrap as tb

from ..core import backup as BK
from ..core import exporter as EX
from ..core import utils as U
from ..core.config import APP_NAME, APP_VERSION, SqlStore, encryption_hint
from ..core.db import DbSession, create_session
from ..core.models import ConnProfile, TableMeta
from ..core.validator import format_value
from . import theme as TH
from . import widgets as W

HELP_TEXT = """【快速上手】

1. 先连数据库
   · 菜单“连接 → 连接管理”，填主机、端口、服务名、用户名、口令 → 测试连接 → 保存并连接。
   · 没有 Oracle 环境也能先玩：内置“演示数据库”，功能完全一样。

2. 不用写 SQL 也能查（查询向导）
   · 菜单“工具 → 查询向导”：选表 → 选字段 → 加条件 → 排序 → 看语句 → 执行。
   · 生成的语句可以自己改，改完再执行。
   · 左侧“对象浏览器”双击表名可直接打开该表的数据窗口。

3. 结果表格像 Excel 一样用
   · 点列标题排序；右键可以复制整行（直接粘贴到 Excel）、查看整行详情。
   · 工具栏“导出 Excel”可把结果存成 .xlsx / .csv / .json / INSERT 脚本。
   · 字段很多时表格会横向滚动，列宽不会被压扁（点列标题可排序）。

4. 数据探查：右键列名就能查（不用写 SQL）
   · “查此列重复值…”      —— 找出这张表里该列的所有重复数据（可按多列组合判断）。
   · “此列值分布…”        —— 列出每个值出现了多少次，点某个值可看明细。
   · “用此列的值去查…”    —— 拿这列的取值去另一张关联表里捞数据（跨表钻取）。
   · 每一步都进“探查链”，可以随时回退、继续往下追、导出 Excel。

5. 运维概览（Ctrl+Shift+O，只读页面）
   · 实例概览 / 慢 SQL / 表空间 / 权限自检 四个页签，可设自动刷新。
   · 慢 SQL 支持按平均耗时、总耗时、逻辑读等排序，可关键字过滤，
     双击看完整 SQL，也能一键发到查询窗口接着分析。
   · 表空间按使用率着色（≥75% 黄、≥90% 红），可同时看占空间最大的段。

6. 数据库档案：把“库长什么样”复制出去求助（Ctrl+Shift+D）
   · 内网连不了外网时，用它生成一段文字版的数据库档案：连接与环境、权限自检、
     对象清单、表结构（字段/主键/索引/外键/行数）、慢 SQL 与表空间。
   · 点“生成档案”后可一键复制，或存成 .md 文件，发给远程工程师 / 贴给 AI 工具。
   · 报错时（查询或执行失败）的提示框里有“复制问题上下文”，会把报错原文、当前
     SQL、相关表结构和权限情况一起打包，不用自己再手打一遍。
   · 档案只含结构元数据，不含任何业务数据行，也不含口令，可以放心外发。

7. 改数据前先备份
   · 执行 增/删/改 时会先问你要不要备份受影响的数据（默认勾选）。
   · 后悔了就到“工具 → 备份与还原”，选中记录点“还原”。

7. 多任务
   · 菜单“文件 → 新建查询窗口 / 新建数据窗口”可开多个子窗口并行工作，
     “窗口”菜单里可以层叠、平铺、快速切换。

【常用快捷键】
   F5 执行当前语句     F9 执行选中的语句     Ctrl+S 保存脚本
   Ctrl+O 打开脚本     Ctrl+Shift+O 运维概览   Ctrl+F 在结果中查找   Ctrl+C 复制选中行

【权限说明】
   · 不同人账号权限不同：看不到 V$SESSION / V$SQL / DBA_* 是正常的。
   · “运维概览 → 权限自检”会逐项列出能看什么、缺什么、该找 DBA 要哪个权限，
     还能一键复制诊断信息发给 DBA。功能不会因此消失，只会降级并说明原因。

【安全提示】
   · 默认不自动提交：改完数据记得点“提交”，点“回滚”可以撤销未提交的改动。
   · 未提交前，其他用户看不到你的改动，你也能一键回滚。
   · 连接可标记环境（开发/测试/预发/生产）；标记为“生产”的连接会自动进入
     只读模式，写操作会被直接拦下。工具栏“只读”可随时手动开关。
"""


# ================================================================== 连接
class ConnectionDialog(tb.Toplevel):
    def __init__(self, parent, app, profile: Optional[ConnProfile] = None):
        super().__init__(parent)
        self.title("连接设置" if profile else "新建连接")
        self.transient(parent)
        self.app = app
        self.profile = profile or ConnProfile()
        self.saved: Optional[ConnProfile] = None
        self.connect_after_save = False

        # 内容区可滚动：屏幕再小也能滚到全部字段，不会“显示不全”
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body = W.ScrolledBody(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        frm = body.inner

        form = W.FormBuilder(frm, label_width=12)
        form.column = 1
        self.form = form
        self._build(frm, form)
        U.fit_to_content(self, parent, min_w=760, min_h=560)
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self, frm, form: W.FormBuilder):
        p = self.profile
        form.entry("name", "连接名称", p.name, 34)
        tb.Label(frm, text="连接信息", bootstyle="primary").grid(row=form.row, column=0, columnspan=3,
                                                              sticky="w", pady=(10, 2))
        form.row += 1
        form.entry("host", "主机/IP", p.host, 34, hint="例如 192.168.1.10")
        form.spin("port", "端口", int(p.port or 1521), 1, 65535, 10)

        kind_row = form.row
        tb.Label(frm, text="连接方式", width=12, anchor="e").grid(row=kind_row, column=0, sticky="e", padx=(0, 8))
        self.kind_var = tk.StringVar(value=("SID" if (p.sid and not p.service) else "SERVICE"))
        box = tb.Frame(frm)
        box.grid(row=kind_row, column=1, sticky="w")
        tb.Radiobutton(box, text="服务名 Service Name", variable=self.kind_var, value="SERVICE",
                       bootstyle="primary").pack(side=tk.LEFT)
        tb.Radiobutton(box, text="SID", variable=self.kind_var, value="SID",
                       bootstyle="primary").pack(side=tk.LEFT, padx=(12, 0))
        form.row += 1
        form.entry("service", "服务名", p.service, 34, hint="例如 ORCLPDB1")
        self.sid_entry = form.entry("sid", "SID", p.sid, 34, hint="例如 ORCL")

        tb.Label(frm, text="登录身份", bootstyle="primary").grid(row=form.row, column=0, columnspan=3,
                                                             sticky="w", pady=(10, 2))
        form.row += 1
        form.entry("user", "用户名", p.user, 34)
        self.pwd_var = tk.StringVar(value=p.password)
        tb.Label(frm, text="口令", width=12, anchor="e").grid(row=form.row, column=0, sticky="e", padx=(0, 8), pady=4)
        self.pwd_entry = tb.Entry(frm, textvariable=self.pwd_var, width=34, show="•")
        self.pwd_entry.grid(row=form.row, column=1, sticky="w", pady=4)
        self.show_var = tk.BooleanVar(value=False)
        tb.Checkbutton(frm, text="显示", variable=self.show_var, bootstyle="secondary-round-toggle",
                       command=lambda: self.pwd_entry.configure(show="" if self.show_var.get() else "•")).grid(
            row=form.row, column=2, sticky="w", padx=(8, 0))
        form.row += 1
        form.entry("schema", "默认模式", p.schema, 34, hint="留空则用登录用户自己的")
        form.combo("role", "角色", ["Normal", "SYSDBA"], p.role or "Normal")
        env_row = form.row
        form.combo("env", "环境标识", list(ConnProfile.ENV_CHOICES), p.env_label, 16)
        tb.Label(frm, text="标为“生产”的连接会自动进入只读模式，写操作会被拦下",
                 bootstyle="secondary", wraplength=380, justify="left").grid(
            row=env_row, column=2, sticky="w", padx=(8, 0))

        adv = tb.Labelframe(frm, text="高级（一般不用填）", padding=10)
        adv.grid(row=form.row, column=0, columnspan=3, sticky="we", pady=(12, 0))
        form.row += 1
        sub = W.FormBuilder(adv, label_width=12)
        sub.entry("conn_string", "完整连接串", p.conn_string, 40,
                  hint="TNS 别名或 host:port/service，填了就忽略上面几项")
        sub.check("thick", "使用本地 Instant Client（厚模式）", bool(p.thick), "")
        sub.entry("lib_dir", "Instant Client 目录", p.lib_dir, 40)
        sub.spin("timeout", "连接超时(秒)", int(p.timeout or 10), 1, 600, 10)
        sub.text("note", "备注", p.note, 3)
        self.sub = sub

        bar = tb.Frame(frm)
        bar.grid(row=form.row + 1, column=0, columnspan=3, sticky="we", pady=(16, 0))
        tb.Button(bar, text="测试连接", bootstyle="info", command=self._test).pack(side=tk.LEFT)
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="保存并连接", bootstyle="success", command=lambda: self._save(True)).pack(
            side=tk.RIGHT, padx=(0, 8))
        tb.Button(bar, text="仅保存", bootstyle="primary-outline", command=lambda: self._save(False)).pack(
            side=tk.RIGHT, padx=(0, 8))
        tb.Label(frm, text=encryption_hint(), bootstyle="secondary", wraplength=660,
                 justify="left").grid(row=form.row + 2, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _collect(self) -> ConnProfile:
        f, s = self.form, self.sub
        p = ConnProfile()
        p.name = f.get("name").strip() or "未命名连接"
        p.host = f.get("host").strip()
        try:
            p.port = int(f.fields["port"].get())
        except Exception:
            p.port = 1521
        p.service = f.get("service").strip() if self.kind_var.get() == "SERVICE" else ""
        p.sid = f.get("sid").strip() if self.kind_var.get() == "SID" else ""
        p.user = f.get("user").strip()
        p.password = self.pwd_var.get()
        p.schema = f.get("schema").strip().upper()
        p.role = f.fields["role"].get()
        env = f.fields["env"].get()
        p.env = "" if env in ("", "未标注") else env
        p.conn_string = s.get("conn_string").strip()
        p.thick = bool(s.fields["thick"].get())
        p.lib_dir = s.get("lib_dir").strip()
        try:
            p.timeout = int(s.fields["timeout"].get())
        except Exception:
            p.timeout = 10
        p.note = s.get("note").strip()
        p.demo = self.profile.demo
        return p

    def _test(self):
        p = self._collect()
        self.withdraw()
        ok, msg = self.app.test_connection(p)
        self.deiconify()
        if ok:
            W.info(self, f"连接成功！\n{msg}", "测试连接")
        else:
            W.error(self, f"连接失败：\n{msg}", "测试连接")

    def _save(self, connect: bool):
        p = self._collect()
        if not p.demo and not p.conn_string and not p.host:
            W.warning(self, "请填写主机地址或完整连接串")
            return
        self.saved = p
        self.connect_after_save = connect
        self.destroy()


class ConnectionManager(tb.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("连接管理")
        self.transient(parent)
        self.app = app
        self._build()
        U.fit_to_content(self, parent, min_w=860, min_h=520)
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self):
        frm = tb.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        cols = ("名称", "用户", "环境", "连接目标", "备注")
        self.tree = tb.Treeview(frm, columns=cols, show="headings", bootstyle="primary")
        for c, w in zip(cols, (180, 110, 70, 320, 200)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = tkttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self._connect())

        bar = tb.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="新建", bootstyle="success", command=self._new).pack(side=tk.LEFT)
        tb.Button(bar, text="编辑", bootstyle="primary-outline", command=self._edit).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="删除", bootstyle="danger-outline", command=self._delete).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="刷新", bootstyle="secondary-outline", command=self._reload).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="关闭", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="连接", bootstyle="primary", command=self._connect).pack(side=tk.RIGHT, padx=(0, 8))
        self._reload()

    def _reload(self):
        self.tree.delete(*self.tree.get_children())
        try:
            self.tree.tag_configure("prod", foreground="#c9302c")
        except Exception:
            pass
        for p in self.app.conn_store.profiles:
            env = p.env_label
            self.tree.insert("", "end", iid=p.name,
                             values=(p.name, p.user or "", env, p.display_dsn(), p.note or ""),
                             tags=("prod",) if p.is_prod else ())

    def _selected(self) -> Optional[ConnProfile]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.app.conn_store.find(sel[0])

    def _new(self):
        dlg = ConnectionDialog(self, self.app, None)
        self.wait_window(dlg)
        if dlg.saved:
            self.app.conn_store.add(dlg.saved)
            self._reload()
            if dlg.connect_after_save:
                self.app.open_connection(dlg.saved.name, parent=self)

    def _edit(self):
        p = self._selected()
        if not p:
            W.warning(self, "请先选择一条连接")
            return
        dlg = ConnectionDialog(self, self.app, p)
        self.wait_window(dlg)
        if dlg.saved:
            for i, q in enumerate(self.app.conn_store.profiles):
                if q.name == p.name:
                    self.app.conn_store.profiles[i] = dlg.saved
            self.app.conn_store.save()
            self._reload()

    def _delete(self):
        p = self._selected()
        if not p:
            return
        if not W.ask_yes(self, f"删除连接“{p.name}”？（只删除本机保存的配置，不会影响数据库）"):
            return
        self.app.conn_store.remove(p.name)
        self._reload()

    def _connect(self):
        p = self._selected()
        if not p:
            W.warning(self, "请先选择一条连接")
            return
        if self.app.open_connection(p.name, parent=self):
            self.destroy()


# ================================================================== 导出
class ExportDialog(tb.Toplevel):
    """把结果集导出为 Excel / CSV / JSON / INSERT 脚本。"""

    def __init__(self, parent, app, default_name: str = "查询结果",
                 providers: Optional[Dict[str, Callable[[], Tuple[List[str], List[tuple]]]]] = None,
                 table_name: str = ""):
        super().__init__(parent)
        self.title("导出数据")
        self.transient(parent)
        self.app = app
        self.providers = providers or {}
        self.table_name = table_name
        self.default_name = default_name

        frm = tb.Frame(self, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        tb.Label(frm, text="导出范围", bootstyle="primary").pack(anchor="w")
        self.scope_var = tk.StringVar()
        scopes = list(self.providers.keys())
        if scopes:
            self.scope_var.set(scopes[0])
            for s in scopes:
                tb.Radiobutton(frm, text=s, variable=self.scope_var, value=s,
                               bootstyle="primary").pack(anchor="w", padx=(14, 0), pady=2)

        tb.Label(frm, text="导出格式", bootstyle="primary").pack(anchor="w", pady=(14, 0))
        self.fmt_var = tk.StringVar(value="xlsx")
        row = tb.Frame(frm)
        row.pack(anchor="w", padx=(14, 0))
        for label, val, tip in (
            ("Excel 工作簿 (.xlsx)", "xlsx", "推荐，带表头、冻结首行、自动筛选"),
            ("CSV 文本 (.csv)", "csv", "Excel 可直接打开，中文不乱码"),
            ("JSON (.json)", "json", "给程序用"),
            ("INSERT 脚本 (.sql)", "sql", "可拿到别的库里回放"),
        ):
            rb = tb.Radiobutton(row, text=label, variable=self.fmt_var, value=val, bootstyle="primary")
            rb.pack(side=tk.LEFT, padx=(0, 14))
            W._attach_tooltip(rb, tip)

        opts = tb.Labelframe(frm, text="选项", padding=10)
        opts.pack(fill=tk.X, pady=(14, 0))
        r1 = tb.Frame(opts)
        r1.pack(fill=tk.X)
        tb.Label(r1, text="工作表名称", width=12, anchor="e").pack(side=tk.LEFT)
        self.sheet_var = tk.StringVar(value=(table_name or "数据")[:31])
        tb.Entry(r1, textvariable=self.sheet_var, width=28).pack(side=tk.LEFT, padx=(6, 0))
        self.header_var = tk.BooleanVar(value=True)
        tb.Checkbutton(r1, text="包含列名", variable=self.header_var,
                       bootstyle="primary-round-toggle").pack(side=tk.LEFT, padx=(20, 0))
        r2 = tb.Frame(opts)
        r2.pack(fill=tk.X, pady=(8, 0))
        tb.Label(r2, text="保存到", width=12, anchor="e").pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value=str(Path.home() / "Desktop" / EX.suggest_filename(default_name, "xlsx")))
        tb.Entry(r2, textvariable=self.path_var, width=52).pack(side=tk.LEFT, padx=(6, 0))
        tb.Button(r2, text="浏览…", bootstyle="secondary-outline", command=self._browse).pack(side=tk.LEFT, padx=(8, 0))
        self.open_var = tk.BooleanVar(value=True)
        tb.Checkbutton(opts, text="导出后打开文件", variable=self.open_var,
                       bootstyle="primary-round-toggle").pack(anchor="w", pady=(8, 0))

        bar = tb.Frame(self, padding=(16, 0, 16, 16))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="开始导出", bootstyle="success", command=self._do_export).pack(side=tk.RIGHT, padx=(0, 8))
        self.fmt_var.trace_add("write", lambda *a: self._sync_ext())
        U.fit_to_content(self, parent, min_w=720, min_h=520)
        try:
            self.grab_set()
        except Exception:
            pass

    def _sync_ext(self):
        path = self.path_var.get()
        if not path:
            return
        stem = str(Path(path).with_suffix(""))
        self.path_var.set(f"{stem}.{self.fmt_var.get()}")

    def _browse(self):
        from tkinter import filedialog

        ext = self.fmt_var.get()
        types = {
            "xlsx": [("Excel 工作簿", "*.xlsx")],
            "csv": [("CSV 文件", "*.csv")],
            "json": [("JSON 文件", "*.json")],
            "sql": [("SQL 脚本", "*.sql")],
        }.get(ext, [("所有文件", "*.*")])
        path = filedialog.asksaveasfilename(parent=self, defaultextension=f".{ext}",
                                            filetypes=types,
                                            initialfile=Path(self.path_var.get()).name)
        if path:
            self.path_var.set(path)

    def _do_export(self):
        scope = self.scope_var.get()
        provider = self.providers.get(scope)
        if not provider:
            W.warning(self, "请选择导出范围")
            return
        path = self.path_var.get().strip()
        if not path:
            W.warning(self, "请选择保存位置")
            return
        fmt = self.fmt_var.get()
        sheet = self.sheet_var.get().strip() or "数据"
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        table = self.table_name or sheet

        def work(event):
            columns, rows = provider()
            if fmt == "xlsx":
                n, sheets = EX.export_excel(path, columns, iter(rows), sheet_name=sheet,
                                            date_format=dfmt)
                return f"已导出 {U.human_number(n)} 行到 {path}" + (f"（工作表：{'、'.join(sheets)}）" if len(sheets) > 1 else "")
            if fmt == "csv":
                n = EX.export_csv(path, columns, iter(rows), date_format=dfmt)
            elif fmt == "json":
                n = EX.export_json(path, columns, iter(rows), date_format=dfmt)
            else:
                n = EX.export_insert_sql(path, table, columns, iter(rows), date_format=dfmt)
            return f"已导出 {U.human_number(n)} 行到 {path}"

        ok, result = W.run_async(self, work, title="正在导出", message="正在写入文件，请稍候…", cancellable=False)
        if ok:
            W.toast(self, result, "success", 5000)
            if self.open_var.get():
                U.open_path(path)
            self.destroy()
        else:
            W.error(self, f"导出失败：{result}")


# ================================================================== 设置
class SettingsDialog(tb.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("设置")
        self.transient(parent)
        self.app = app
        body = W.ScrolledBody(self, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        frm = body.inner
        form = W.FormBuilder(frm, label_width=16)
        themes = TH.all_themes()
        names = [f"{k}  ·  {v}" for k, v in themes.items()]
        cur = app.settings.get("theme", TH.DEFAULT_THEME)
        cur_label = next((n for n in names if n.split("  ·  ")[0] == cur), names[0])
        self.theme_var = tk.StringVar(value=cur_label)
        form.combo("theme", "界面主题", names, cur_label, 34)
        form.spin("font_size", "字体大小", int(app.settings.get("font_size", 10)), 8, 20, 8)
        form.entry("font_family", "字体", app.settings.get("font_family", "Microsoft YaHei UI"), 26)
        form.spin("fetch_rows", "每次最多取", int(app.settings.get("fetch_rows", 200)), 10, 200000, 12)
        form.spin("max_grid_rows", "表格最多显示", int(app.settings.get("max_grid_rows", 20000)), 100, 500000, 12)
        form.entry("date_format", "日期显示格式", app.settings.get("date_format", "%Y-%m-%d %H:%M:%S"), 22)
        form.entry("null_display", "空值显示", app.settings.get("null_display", ""), 12)
        form.check("auto_commit", "自动提交", bool(app.settings.get("auto_commit", False)), "写操作后自动 COMMIT")
        form.check("read_only", "只读模式", bool(app.settings.get("read_only", False)),
                   "开启后一切写操作（增/删/改/DDL/还原）都会被拦下")
        form.check("env_read_only", "生产环境自动只读", bool(app.settings.get("env_read_only", True)),
                   "连接标记为“生产”时自动进入只读（强烈建议保持开启）")
        form.check("confirm_dml", "写操作前确认", bool(app.settings.get("confirm_dml", True)), "增/删/改 前弹出确认框")
        form.check("auto_backup", "默认备份", bool(app.settings.get("auto_backup", True)), "写操作前默认勾选备份")
        form.check("show_row_number", "显示行号列", bool(app.settings.get("show_row_number", True)), "")
        form.check("schema_panel", "显示对象浏览器", bool(app.settings.get("schema_panel", True)), "")

        row = form.row
        tb.Label(frm, text="备份目录", width=16, anchor="e").grid(row=row, column=0, sticky="e", padx=(0, 8))
        self.dir_var = tk.StringVar(value=str(app.settings.backup_dir))
        tb.Entry(frm, textvariable=self.dir_var, width=40).grid(row=row, column=1, sticky="w")
        tb.Button(frm, text="浏览…", bootstyle="secondary-outline", command=self._browse).grid(
            row=row, column=2, sticky="w", padx=(8, 0))
        form.row += 1

        bar = tb.Frame(frm)
        bar.grid(row=form.row + 1, column=0, columnspan=3, sticky="we", pady=(18, 0))
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        tb.Button(bar, text="保存", bootstyle="success", command=self._save).pack(side=tk.RIGHT, padx=(0, 8))
        tb.Button(bar, text="恢复默认", bootstyle="warning-outline", command=self._reset).pack(side=tk.LEFT)
        self.form = form
        U.fit_to_content(self, parent, min_w=700, min_h=560)
        try:
            self.grab_set()
        except Exception:
            pass

    def _browse(self):
        from tkinter import filedialog

        d = filedialog.askdirectory(parent=self, initialdir=self.dir_var.get() or str(Path.home()))
        if d:
            self.dir_var.set(d)

    def _reset(self):
        from ..core.config import DEFAULTS

        # 备份目录、窗口位置这类“个人环境”设置不跟着一起重置，
        # 否则已有的备份会从列表里消失
        keep = {k: self.app.settings.get(k) for k in ("backup_dir", "window_geometry", "recent_files")}
        values = dict(DEFAULTS)
        values.update({k: v for k, v in keep.items() if v not in ("", None, [])})
        self.app.settings.update(values)
        self.destroy()
        W.toast(self.app.root, "已恢复默认设置，部分设置重启后生效", "success")

    def _save(self):
        f = self.form
        theme_label = f.get("theme")
        theme_key = theme_label.split("  ·  ")[0].strip()
        values = {
            "theme": theme_key,
            "font_size": int(f.fields["font_size"].get()),
            "font_family": f.get("font_family"),
            "fetch_rows": int(f.fields["fetch_rows"].get()),
            "max_grid_rows": int(f.fields["max_grid_rows"].get()),
            "date_format": f.get("date_format"),
            "null_display": f.fields["null_display"].get(),
            "auto_commit": bool(f.fields["auto_commit"].get()),
            "read_only": bool(f.fields["read_only"].get()),
            "env_read_only": bool(f.fields["env_read_only"].get()),
            "confirm_dml": bool(f.fields["confirm_dml"].get()),
            "auto_backup": bool(f.fields["auto_backup"].get()),
            "show_row_number": bool(f.fields["show_row_number"].get()),
            "schema_panel": bool(f.fields["schema_panel"].get()),
            "backup_dir": self.dir_var.get().strip(),
        }
        self.app.apply_settings(values)
        self.destroy()
        W.toast(self.app.root, "设置已保存", "success")


# ================================================================== 备份与还原
class BackupCenter(tb.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("备份与还原")
        self.transient(parent)
        self.app = app
        self._build()
        self._reload()
        U.fit_to_content(self, parent, min_w=1000, min_h=640)

    def _build(self):
        frm = tb.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        cols = ("创建时间", "操作", "表", "行数", "连接", "可精确还原", "大小", "备注")
        self.tree = tb.Treeview(frm, columns=cols, show="headings", bootstyle="primary")
        widths = (150, 70, 190, 70, 190, 90, 80, 140)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = tkttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        bar = tb.Frame(self, padding=(12, 0, 12, 6))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="刷新", bootstyle="secondary-outline", command=self._reload).pack(side=tk.LEFT)
        tb.Button(bar, text="查看备份数据", bootstyle="info-outline", command=self._view).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="导出为 Excel", bootstyle="info-outline", command=self._export).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="删除", bootstyle="danger-outline", command=self._delete).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="打开备份文件夹", bootstyle="secondary-link", command=self._open_dir).pack(side=tk.LEFT, padx=(8, 0))
        self.restore_btn = tb.Button(bar, text="还原到数据库", bootstyle="success", command=self._restore)
        self.restore_btn.pack(side=tk.RIGHT)
        tb.Button(bar, text="查看还原语句", bootstyle="warning-outline", command=self._preview).pack(
            side=tk.RIGHT, padx=(0, 8))

        self.info = tb.Label(self, text="", bootstyle="secondary", wraplength=960, justify="left")
        self.info.pack(fill=tk.X, padx=(12, 12))
        tb.Label(self, text="说明：还原只针对备份中记录的行。若目标表在备份后又被改过，还原会覆盖这些行的当前值。",
                 bootstyle="secondary", wraplength=960, justify="left").pack(fill=tk.X, padx=12, pady=(0, 8))

    def _reload(self):
        self.tree.delete(*self.tree.get_children())
        for item in self.app.backup_manager.list():
            self.tree.insert("", "end", iid=item.get("id", ""), values=(
                item.get("created_at", ""), item.get("operation", ""),
                f"{item.get('schema','')}.{item.get('table','')}".lstrip("."),
                U.human_number(item.get("rowcount", 0)), item.get("connection", ""),
                "是（ROWID）" if item.get("has_rowid") else "按主键/全列",
                U.format_size(item.get("size_bytes")), item.get("note", ""),
            ))
        self.info.configure(text=f"共 {len(self.app.backup_manager.list())} 个备份，"
                                 f"占用 {U.format_size(self.app.backup_manager.total_size())}"
                                 f"　目录：{self.app.settings.backup_dir}")

    def _selected_id(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_select(self):
        bid = self._selected_id()
        if not bid:
            return
        rec = self.app.backup_manager.load(bid)
        if rec:
            self.info.configure(
                text=f"备份 {rec.id}：{rec.operation} {rec.schema}.{rec.table}，{rec.rowcount} 行，"
                     f"创建于 {rec.created_at}"
                     + (f"，已于 {rec.restored_at} 还原过" if rec.restored_at else "")
            )

    def _view(self):
        bid = self._selected_id()
        if not bid:
            W.warning(self, "请先选择一个备份")
            return
        rec = self.app.backup_manager.load(bid)
        if not rec:
            return
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        rows = BK.decode_rows(rec.columns, rec.rows)
        lines = ["\t".join(rec.columns)]
        for r in rows[:2000]:
            lines.append("\t".join(format_value(r.get(c), dfmt) for c in rec.columns))
        if len(rows) > 2000:
            lines.append(f"…… 其余 {len(rows) - 2000} 行未显示")
        lines.append("")
        lines.append("触发备份的语句：")
        lines.append(rec.sql)
        W.show_text(self, f"备份数据 —— {rec.table}", "\n".join(lines), width=1000, height=640)

    def _export(self):
        bid = self._selected_id()
        if not bid:
            W.warning(self, "请先选择一个备份")
            return
        rec = self.app.backup_manager.load(bid)
        if not rec:
            return
        rows = [tuple(r.get(c) for c in rec.columns) for r in BK.decode_rows(rec.columns, rec.rows)]
        dlg = ExportDialog(self, self.app, default_name=f"备份_{rec.table}",
                           providers={"备份的全部数据": lambda: (rec.columns, rows)},
                           table_name=rec.table)
        self.wait_window(dlg)

    def _preview(self):
        bid = self._selected_id()
        if not bid:
            W.warning(self, "请先选择一个备份")
            return
        rec = self.app.backup_manager.load(bid)
        if not rec:
            return
        session = self.app.current_session
        dialect = session.dialect if session else "oracle"
        text = BK.restore_preview(rec, dialect, sample=5)
        W.show_text(self, "还原语句预览", text, width=900, height=520)

    def _restore(self):
        bid = self._selected_id()
        if not bid:
            W.warning(self, "请先选择一个备份")
            return
        rec = self.app.backup_manager.load(bid)
        if not rec:
            return
        session = self.app.get_session_by_label(rec.connection) or self.app.current_session
        if not session:
            W.error(self, "没有可用的数据库连接，请先连接数据库")
            return
        guard = getattr(self.app, "guard_write", None)
        if guard is not None and not guard(self, "数据还原"):
            return
        stmts = BK.build_restore_statements(rec, session.dialect)
        if not stmts:
            W.warning(self, "无法生成还原语句（可能备份里没有数据）")
            return
        tip = f"即将把 {rec.rowcount} 行数据还原到 {rec.schema}.{rec.table}：\n" \
              f"· {rec.operation} 操作 → 还原方式：{_restore_desc(rec)}\n" \
              f"· 共 {len(stmts)} 条语句\n\n" \
              f"建议先“查看还原语句”确认无误后再执行。是否继续？"
        if not W.ask_yes(self, tip, "确认还原"):
            return

        def work(event):
            done = 0
            affected = 0
            missed = []
            failed = []
            for sql, params in stmts:
                if event.is_set():
                    break
                try:
                    rc = session.execute(sql, params)
                    done += 1
                    if rc:
                        affected += rc
                    else:
                        # 语句没报错但一行都没命中：不写进 failed，单独提示，
                        # 否则用户会以为还原成功，其实数据没变
                        missed.append(sql[:160])
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{sql[:120]} → {e}")
            return done, affected, missed, failed

        ok, res = W.run_async(self, work, title="正在还原", message="正在把备份数据写回数据库…",
                              cancellable=True, on_cancel=lambda: session.cancel())
        if not ok:
            W.error(self, f"还原过程出错：{res}")
            return
        done, affected, missed, failed = res
        summary = (f"还原完成：执行 {done} 条语句，实际改动 {affected} 行"
                   + (f"，其中 {len(missed)} 条未命中任何行" if missed else "")
                   + (f"，{len(failed)} 条报错" if failed else "") + "。")
        if missed:
            W.warning(self, f"{summary}\n\n有 {len(missed)} 条语句没有命中任何行"
                            f"（目标行可能已被删除，或当前值本来就与备份一致）。"
                            f"点“确定”后可查看明细。")
        commit = W.ask_yes(self, f"{summary}\n\n是否提交（COMMIT）？\n"
                                 f"选“否”则保持未提交，可到工具栏回滚。", "提交还原结果")
        if commit:
            try:
                session.commit()
                self.app.backup_manager.mark_restored(bid)
                W.toast(self, "已提交还原结果", "success", 4000)
            except Exception as e:  # noqa: BLE001
                W.error(self, f"提交失败：{e}")
        if failed:
            W.show_text(self, "还原失败明细", "\n".join(failed[:200]), width=900, height=520)
        elif missed:
            W.show_text(self, "未命中的还原语句",
                        "以下语句执行成功但没有命中任何行（数据未发生变化）：\n\n"
                        + "\n\n".join(missed[:200]), width=900, height=520)
        self._reload()

    def _delete(self):
        bid = self._selected_id()
        if not bid:
            return
        if not W.ask_yes(self, "删除这条备份记录？（删除后无法还原）"):
            return
        self.app.backup_manager.delete(bid)
        self._reload()

    def _open_dir(self):
        self.app.backup_manager.open_folder()


def _restore_desc(rec) -> str:
    op = (rec.operation or "").upper()
    if op in ("DELETE", "MERGE"):
        return "把删除的行重新插入回去"
    if op == "UPDATE":
        return "用旧值覆盖当前值" + ("（按 ROWID 精确定位）" if rec.has_rowid else "（按主键定位）")
    if op == "INSERT":
        return "把插入的行删除"
    return "按备份内容回写"


# ================================================================== 表结构
class TableInfoDialog(tb.Toplevel):
    def __init__(self, parent, app, session_name: str, schema: str, table: str):
        super().__init__(parent)
        self.title(f"表结构 —— {schema}.{table}")
        self.transient(parent)
        self.app = app
        self.session_name = session_name
        self.schema, self.table = schema, table
        nb = tb.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        page1 = tb.Frame(nb, padding=10)
        nb.add(page1, text="字段")
        cols = ("#", "字段名", "类型", "可为空", "主键", "默认值", "说明")
        self.tree = tb.Treeview(page1, columns=cols, show="headings", bootstyle="primary")
        for c, w in zip(cols, (40, 170, 140, 70, 50, 140, 200)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = tkttk.Scrollbar(page1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self._copy_column())

        page2 = tb.Frame(nb, padding=10)
        nb.add(page2, text="索引")
        self.idx_tree = tb.Treeview(page2, columns=("索引名", "是否唯一", "字段", "状态"),
                                    show="headings", bootstyle="primary")
        for c, w in zip(("索引名", "是否唯一", "字段", "状态"), (220, 90, 320, 90)):
            self.idx_tree.heading(c, text=c)
            self.idx_tree.column(c, width=w, anchor="w")
        self.idx_tree.pack(fill=tk.BOTH, expand=True)

        page3 = tb.Frame(nb, padding=10)
        nb.add(page3, text="建表语句")
        self.ddl_text = tk.Text(page3, wrap="none", font=("Consolas", 11))
        sbx = tkttk.Scrollbar(page3, orient="vertical", command=self.ddl_text.yview)
        self.ddl_text.configure(yscrollcommand=sbx.set)
        self.ddl_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sbx.pack(side=tk.RIGHT, fill=tk.Y)

        bar = tb.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="生成 SELECT 语句", bootstyle="info-outline",
                  command=self._gen_select).pack(side=tk.LEFT)
        tb.Button(bar, text="复制建表语句", bootstyle="secondary-outline",
                  command=lambda: U.copy_to_clipboard(self, self.ddl_text.get("1.0", "end-1c"))).pack(
            side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="关闭", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        self._load()
        U.fit_to_content(self, parent, min_w=900, min_h=620)

    def _load(self):
        session = self.app.get_session(self.session_name)
        if not session:
            return
        try:
            cols = session.columns(self.schema, self.table, refresh=True)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"读取字段失败：{e}")
            return
        for c in cols:
            self.tree.insert("", "end", values=(
                c.position, c.name, c.type_label, "是" if c.nullable else "否",
                "✔" if c.is_pk else "", (c.default or "").strip()[:40], c.comment))
        try:
            for idx in session.indexes(self.schema, self.table):
                self.idx_tree.insert("", "end", values=(idx.get("name"), idx.get("unique"),
                                                        idx.get("columns"), idx.get("status")))
        except Exception:
            pass
        ddl = session.ddl(self.schema, self.table)
        self.ddl_text.insert("1.0", ddl or "-- 当前连接无法读取建表语句（可能没有 DBMS_METADATA 权限）")
        self.ddl_text.configure(state="disabled")

    def _copy_column(self):
        iid = self.tree.focus()
        if not iid:
            return
        vals = self.tree.item(iid, "values")
        if len(vals) > 1:
            U.copy_to_clipboard(self, vals[1])
            W.toast(self, "已复制字段名", "success")

    def _gen_select(self):
        from ..core import sqlbuilder as SB
        from ..core.models import QuerySpec

        spec = QuerySpec(schema=self.schema, table=self.table, limit=200)
        sql = SB.build_select(spec, {}, "oracle")
        sql = SB.apply_row_limit(sql, 200, "oracle")
        self.app.new_query_window(sql=sql, session_name=self.session_name)
        self.destroy()


# ================================================================== 历史 / 收藏
class HistoryDialog(tb.Toplevel):
    def __init__(self, parent, app, favorites: SqlStore, on_pick: Callable[[str], None]):
        super().__init__(parent)
        self.title("SQL 历史与收藏")
        self.geometry("900x560")
        self.transient(parent)
        self.app = app
        self.favorites = favorites
        self.on_pick = on_pick
        nb = tb.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 0))
        self.hist_tree = self._make_page(nb, "历史记录", ("时间", "类型", "连接", "语句"))
        self.fav_tree = self._make_page(nb, "收藏", ("时间", "名称", "连接", "语句"))
        bar = tb.Frame(self, padding=(12, 4, 12, 12))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="插入到查询窗口", bootstyle="primary", command=self._pick).pack(side=tk.LEFT)
        tb.Button(bar, text="收藏/取消收藏", bootstyle="warning-outline", command=self._fav).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="删除", bootstyle="danger-outline", command=self._delete).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="清空历史", bootstyle="secondary-link", command=self._clear).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="关闭", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        self._load()
        U.fit_to_content(self, parent, min_w=900, min_h=560)

    def _make_page(self, nb, title, cols):
        page = tb.Frame(nb, padding=8)
        nb.add(page, text=title)
        tree = tb.Treeview(page, columns=cols, show="headings", bootstyle="primary")
        for c, w in zip(cols, (140, 110, 160, 420)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        sb = tkttk.Scrollbar(page, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind("<Double-1>", lambda e: self._pick())
        return tree

    def _load(self):
        for t in (self.hist_tree, self.fav_tree):
            t.delete(*t.get_children())
        for i, item in enumerate(self.app.history.items):
            self.hist_tree.insert("", "end", iid=f"h{i}", values=(
                item.get("time", ""), item.get("tag", ""), item.get("connection", ""),
                U.clip_text(item.get("sql", ""), 200)))
        for i, item in enumerate(self.favorites.items):
            self.fav_tree.insert("", "end", iid=f"f{i}", values=(
                item.get("time", ""), item.get("tag", ""), item.get("connection", ""),
                U.clip_text(item.get("sql", ""), 200)))

    def _current_sql(self) -> str:
        tree = self.hist_tree if self.hist_tree.selection() else self.fav_tree
        sel = tree.selection()
        if not sel:
            return ""
        idx = int(sel[0][1:])
        store = self.app.history if tree is self.hist_tree else self.favorites
        if 0 <= idx < len(store.items):
            return store.items[idx].get("sql", "")
        return ""

    def _pick(self):
        sql = self._current_sql()
        if not sql:
            W.warning(self, "请先选择一条记录")
            return
        self.on_pick(sql)
        self.destroy()

    def _fav(self):
        sql = self._current_sql()
        if not sql:
            return
        self.favorites.add(sql, tag="收藏")
        self._load()
        W.toast(self, "已加入收藏", "success")

    def _delete(self):
        sql = self._current_sql()
        if not sql:
            return
        self.app.history.remove(sql)
        self.favorites.remove(sql)
        self._load()

    def _clear(self):
        if W.ask_yes(self, "清空全部历史记录？（收藏不受影响）"):
            self.app.history.clear()
            self._load()


# ================================================================== 关于
class AboutDialog(tb.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("关于")
        self.geometry("560x420")
        self.transient(parent)
        frm = tb.Frame(self, padding=24)
        frm.pack(fill=tk.BOTH, expand=True)
        tb.Label(frm, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tb.Label(frm, text=f"版本 {APP_VERSION}　·　面向 Oracle 初学者与 Excel 用户",
                 bootstyle="secondary").pack(anchor="w", pady=(4, 2))
        tb.Label(frm, text="作者：Lovemikan", bootstyle="secondary").pack(anchor="w", pady=(0, 6))
        hist = ""
        try:
            from ..core.config import VERSION_HISTORY

            ver, date, items = VERSION_HISTORY[0]
            hist = f"本次更新（{ver}　{date}）：\n" + "".join(f"· {x}\n" for x in items)
        except Exception:
            hist = ""
        if hist:
            tb.Label(frm, text=hist, justify="left", wraplength=500,
                     bootstyle="info").pack(anchor="w", pady=(0, 10))
        text = (
            "特点：\n"
            "· 免安装 Oracle 客户端（thin 模式直连），也支持本机 Instant Client\n"
            "· 引导式生成 SQL，先看语句再执行\n"
            "· 多条件筛选、点列排序、结果一键导出 Excel\n"
            "· 增/删/改 前自动备份，可一键还原\n"
            "· 多子窗口并行，支持同时连多个库\n\n"
            "技术栈：Python + Tkinter(ttkbootstrap) + python-oracledb + openpyxl\n"
        )
        tb.Label(frm, text=text, justify="left", wraplength=500).pack(anchor="w")

        from ..core.config import data_dir as _dd, is_portable as _portable

        try:
            loc = f"数据存储位置：{_dd()}{'（便携模式）' if _portable() else ''}"
        except Exception:
            loc = ""
        if loc:
            tb.Label(frm, text=loc, justify="left", wraplength=500,
                     bootstyle="secondary", cursor="hand2").pack(anchor="w", pady=(8, 0))
        tb.Button(frm, text="查看使用说明", bootstyle="info-outline",
                  command=lambda: W.show_text(self, "使用说明", HELP_TEXT, width=880, height=640,
                                              mono=False)).pack(anchor="w", pady=(12, 0))
        tb.Button(frm, text="关闭", bootstyle="secondary", command=self.destroy).pack(anchor="w", pady=(16, 0))
        U.fit_to_content(self, parent, min_w=560, min_h=420)
        try:
            self.grab_set()
        except Exception:
            pass
