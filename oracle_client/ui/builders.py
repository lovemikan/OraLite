"""引导式 SQL 构建：条件编辑器、查询向导、插入/更新/删除向导、写操作备份确认。"""
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
from ..core.models import ColumnMeta, Condition, OrderTerm, QuerySpec
from ..core.validator import format_value, hint_for, is_null_token, validate
from . import widgets as W
from .editor import SqlEditor

# ================================================================== 值输入
class ValueInput(tb.Frame):
    """单行值输入，支持“设为 NULL”开关与类型提示。"""

    def __init__(self, master, data_type: str = "VARCHAR2", value: str = "", width: int = 24,
                 on_change: Optional[Callable] = None):
        super().__init__(master)
        self.data_type = data_type
        self.on_change = on_change
        self.null_var = tk.BooleanVar(value=False)
        self.var = tk.StringVar(value=value or "")
        self.entry = tb.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.null_btn = tb.Checkbutton(self, text="NULL", variable=self.null_var,
                                       bootstyle="warning-round-toggle", command=self._toggle)
        self.null_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.hint = tb.Label(self, text="", bootstyle="secondary")
        self.hint.pack(side=tk.LEFT, padx=(6, 0))
        self.set_type(data_type)
        self.var.trace_add("write", lambda *a: self._fire())

    def _fire(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _toggle(self):
        if self.null_var.get():
            self.entry.configure(state="disabled")
        else:
            self.entry.configure(state="normal")
        self._fire()

    def set_type(self, data_type: str):
        self.data_type = data_type or "VARCHAR2"
        try:
            self.hint.configure(text=hint_for(self.data_type))
        except Exception:
            pass

    def set_value(self, text: str):
        if text is None:
            self.null_var.set(True)
            self.entry.configure(state="disabled")
            self.var.set("")
        else:
            self.null_var.set(False)
            self.entry.configure(state="normal")
            self.var.set(str(text))

    def get_text(self) -> str:
        return "NULL" if self.null_var.get() else self.var.get().strip()

    def is_null(self) -> bool:
        return self.null_var.get()


# ================================================================== 条件编辑器
class ConditionRow:
    def __init__(self, master, editor: "ConditionEditor", index: int):
        self.editor = editor
        self.index = index
        self.frame = tb.Frame(master)
        self.frame.pack(fill=tk.X, pady=2)

        self.logic_var = tk.StringVar(value="AND")
        self.logic = tb.Combobox(self.frame, textvariable=self.logic_var, values=["AND", "OR"],
                                 width=5, state="readonly")
        self.logic.set("AND")
        self.logic.pack(side=tk.LEFT)

        self.col_var = tk.StringVar()
        names = [c.name for c in editor.columns]
        self.col = tb.Combobox(self.frame, textvariable=self.col_var, values=names, width=20,
                               state="readonly" if names else "normal")
        self.col.pack(side=tk.LEFT, padx=(6, 0))
        self.col.bind("<<ComboboxSelected>>", lambda e: self._on_col_change())

        self.op_var = tk.StringVar(value="=")
        op_items = [f"{k}  {v}" for k, v in SB.OPERATORS.items()]
        self.op = tb.Combobox(self.frame, textvariable=self.op_var, values=op_items, width=17,
                              state="readonly")
        self.op.pack(side=tk.LEFT, padx=(6, 0))
        self.op.bind("<<ComboboxSelected>>", lambda e: self._on_op_change())

        self.value = ValueInput(self.frame, "VARCHAR2", "", 20, on_change=editor._notify)
        self.value.pack(side=tk.LEFT, padx=(6, 0))

        self.and_label = tb.Label(self.frame, text="和", bootstyle="secondary")
        self.value2 = ValueInput(self.frame, "VARCHAR2", "", 16, on_change=editor._notify)

        self.del_btn = tb.Button(self.frame, text="✕", width=3, bootstyle="danger-link",
                                 command=lambda: editor.remove_row(self))
        self.del_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._on_op_change()

    # ---------- 状态 ----------
    def _op_key(self) -> str:
        return (self.op_var.get() or "=").split("  ")[0].strip()

    def _on_col_change(self):
        ctype = self.editor.col_type(self.col_var.get())
        self.value.set_type(ctype)
        self.value2.set_type(ctype)
        self.editor._notify()

    def _on_op_change(self):
        op = self._op_key()
        for w in (self.and_label, self.value2):
            try:
                w.pack_forget()
            except Exception:
                pass
        self.value.pack(side=tk.LEFT, padx=(6, 0))
        if op == "BETWEEN":
            self.and_label.pack(side=tk.LEFT, padx=(6, 2))
            self.value2.pack(side=tk.LEFT)
        if op in SB.NO_VALUE_OPS:
            self.value.entry.configure(state="disabled")
            self.value.var.set("")
        else:
            if not self.value.is_null():
                self.value.entry.configure(state="normal")
        if op in ("IN", "NOT_IN"):
            self.value.hint.configure(text="多个值用英文逗号分隔：10,20,30")
        elif op not in SB.NO_VALUE_OPS:
            self.value.hint.configure(text=hint_for(self.value.data_type))
        self.editor._notify()

    def set_condition(self, cond: Condition):
        if cond.column:
            self.col_var.set(cond.column)
            self._on_col_change()
        op_key = cond.op
        for item in self.op.cget("values") or ():
            if item.split("  ")[0].strip() == op_key:
                self.op_var.set(item)
                break
        self.value.set_value(cond.value)
        self.value2.set_value(cond.value2)
        self.logic_var.set(cond.logic or "AND")
        self._on_op_change()

    def get_condition(self) -> Condition:
        return Condition(column=self.col_var.get().strip(), op=self._op_key(),
                         value=self.value.get_text(), value2=self.value2.get_text(),
                         logic=self.logic_var.get())

    def set_logic_visible(self, visible: bool):
        try:
            if visible:
                self.logic.configure(state="readonly")
            else:
                self.logic.set("AND")
                self.logic.configure(state="disabled")
        except Exception:
            pass

    def validate(self) -> Optional[str]:
        c = self.get_condition()
        # 没填完整的行视为“不参与筛选”，不报错
        if c.is_empty() or not c.column:
            return None
        if c.op in SB.NO_VALUE_OPS:
            return None
        ctype = self.editor.col_type(c.column)
        if c.op in SB.NO_VALUE_OPS:
            return None
        if c.op in ("IN", "NOT_IN"):
            items = [x.strip() for x in str(c.value).split(",") if x.strip()]
            if not items:
                return f"“{c.column}”需要填写至少一个值"
            for it in items:
                ok, _, msg = validate(ctype, it)
                if not ok:
                    return f"“{c.column}”的值有问题 → {msg}"
            return None
        for v in ([c.value] if c.op not in SB.TWO_VALUE_OPS else [c.value, c.value2]):
            ok, _, msg = validate(ctype, v)
            if not ok:
                return f"“{c.column}”的值有问题 → {msg}"
        return None


class ConditionEditor(tb.Frame):
    """多行条件编辑器（WHERE 子句）。"""

    def __init__(self, master, columns: Sequence[ColumnMeta], on_change: Optional[Callable] = None,
                 title: str = "筛选条件（相当于 Excel 的筛选器）"):
        super().__init__(master)
        self.columns = list(columns or [])
        self.on_change = on_change
        self.rows: List[ConditionRow] = []

        head = tb.Frame(self)
        head.pack(fill=tk.X)
        tb.Label(head, text=title, bootstyle="secondary").pack(side=tk.LEFT)
        tb.Label(head, text="  条件之间按顺序组合，默认全部满足（AND）",
                 bootstyle="secondary").pack(side=tk.LEFT)

        self.body = tb.Frame(self)
        self.body.pack(fill=tk.X, pady=(4, 0))

        bar = tb.Frame(self)
        bar.pack(fill=tk.X, pady=(6, 0))
        tb.Button(bar, text="＋ 添加条件", bootstyle="success-outline",
                  command=self.add_row).pack(side=tk.LEFT)
        tb.Button(bar, text="清空条件", bootstyle="secondary-link",
                  command=self.clear_rows).pack(side=tk.LEFT, padx=(8, 0))
        self.tip = tb.Label(bar, text="", bootstyle="secondary")
        self.tip.pack(side=tk.LEFT, padx=(10, 0))

    # ---------- 行管理 ----------
    def add_row(self, cond: Optional[Condition] = None) -> ConditionRow:
        row = ConditionRow(self.body, self, len(self.rows))
        self.rows.append(row)
        if cond:
            row.set_condition(cond)
        elif self.columns:
            row.col_var.set(self.columns[0].name)
            row._on_col_change()
        self._sync_logic()
        self._notify()
        return row

    def remove_row(self, row: ConditionRow):
        try:
            row.frame.destroy()
        except Exception:
            pass
        if row in self.rows:
            self.rows.remove(row)
        self._sync_logic()
        self._notify()

    def clear_rows(self):
        for r in list(self.rows):
            try:
                r.frame.destroy()
            except Exception:
                pass
        self.rows = []
        self._notify()

    def _sync_logic(self):
        for i, r in enumerate(self.rows):
            r.set_logic_visible(i > 0)

    def set_columns(self, columns: Sequence[ColumnMeta]):
        self.columns = list(columns or [])
        names = [c.name for c in self.columns]
        for r in self.rows:
            try:
                r.col.configure(values=names)
            except Exception:
                pass

    def col_type(self, name: str) -> str:
        up = (name or "").upper()
        for c in self.columns:
            if c.name.upper() == up:
                return c.data_type
        return "VARCHAR2"

    def conditions(self) -> List[Condition]:
        return [r.get_condition() for r in self.rows if not r.get_condition().is_empty()]

    def set_conditions(self, conds: Sequence[Condition]):
        self.clear_rows()
        for c in conds or []:
            self.add_row(c)

    def _set_tip(self, text: str):
        """窗口可能已在销毁过程中，避免在已销毁控件上 configure。"""
        try:
            if self.tip is not None and self.tip.winfo_exists():
                self.tip.configure(text=text, bootstyle="secondary")
        except Exception:
            pass

    def validate_all(self) -> Tuple[bool, str]:
        msgs = []
        for r in self.rows:
            try:
                m = r.validate()
            except Exception:
                m = None
            if m:
                msgs.append(m)
        if msgs:
            self._set_tip("")
            return False, "\n".join(msgs)
        if not self.conditions():
            self._set_tip("提示：不填条件表示查询整张表")
        else:
            self._set_tip("")
        return True, ""

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


# ================================================================== 查询向导
class QueryWizard(tb.Toplevel):
    """一步步引导用户生成 SELECT 语句。"""

    STEPS = ["① 选择表", "② 选择列", "③ 设置条件", "④ 排序与行数", "⑤ 确认语句"]

    def __init__(self, parent, app, session_name: str = "", schema: str = "", table: str = ""):
        super().__init__(parent)
        self.title("查询向导 —— 不用写 SQL 也能查数据")
        self.transient(parent)
        self.app = app
        self.session_name = session_name or app.current_session_name
        self.session: Optional[DbSession] = app.get_session(self.session_name)
        self.result: Dict[str, Any] = {"sql": "", "execute": False, "schema": "", "table": ""}
        self.spec = QuerySpec()
        self.spec.schema = schema
        self.spec.table = table
        self.step = 0

        self._build()
        if schema and table:
            pass
        self._render_step()
        U.fit_to_content(self, parent, min_w=920, min_h=680)
        try:
            self.grab_set()
        except Exception:
            pass

    # ---------------- 界面 ----------------
    def _build(self):
        top = tb.Frame(self, padding=(14, 12, 14, 0))
        top.pack(fill=tk.X)
        self.step_label = tb.Label(top, text="", font=("Microsoft YaHei UI", 12, "bold"))
        self.step_label.pack(side=tk.LEFT)
        self.conn_label = tb.Label(top, text="", bootstyle="secondary")
        self.conn_label.pack(side=tk.RIGHT)
        if self.session:
            self.conn_label.configure(text=f"连接：{self.session.label}")

        # 内容区可滚动：条件多了也不会被窗口裁掉
        body = W.ScrolledBody(self, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        self.content = body.inner

        bar = tb.Frame(self, padding=(14, 0, 14, 14))
        bar.pack(fill=tk.X)
        self.prev_btn = tb.Button(bar, text="上一步", bootstyle="secondary", command=self._prev)
        self.prev_btn.pack(side=tk.LEFT)
        self.next_btn = tb.Button(bar, text="下一步", bootstyle="primary", command=self._next)
        self.next_btn.pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="取消", bootstyle="secondary-link", command=self._cancel).pack(side=tk.LEFT, padx=(8, 0))
        self.exec_btn = tb.Button(bar, text="执行查询", bootstyle="success", command=self._finish_execute)
        self.exec_btn.pack(side=tk.RIGHT)
        self.fill_btn = tb.Button(bar, text="仅填入查询窗口", bootstyle="info-outline",
                                  command=self._finish_fill)
        self.fill_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def _render_step(self):
        self._clear_content()
        self.step_label.configure(text=f"{self.STEPS[self.step]}   共 {len(self.STEPS)} 步")
        self.prev_btn.configure(state="disabled" if self.step == 0 else "normal")
        last = self.step == len(self.STEPS) - 1
        self.next_btn.configure(state="disabled" if last else "normal")
        self.exec_btn.configure(state="normal" if last else "disabled")
        self.fill_btn.configure(state="normal" if last else "disabled")
        [self._step_table, self._step_columns, self._step_conditions,
         self._step_order, self._step_preview][self.step]()
        # 每步内容高度不同，切换后按内容调整高度（不改变用户拖过的位置）
        U.fit_to_content(self, getattr(self, "master", None), min_w=920, min_h=680, recenter=False)

    def _next(self):
        if self.step == 0 and not self.spec.table:
            W.warning(self, "请先选择一张表")
            return
        if self.step == 2:
            ok, msg = self.cond_editor.validate_all()
            if not ok:
                W.warning(self, msg)
                return
            self.spec.conditions = self.cond_editor.conditions()
        if self.step == 3:
            self._collect_order()
        self.step = min(self.step + 1, len(self.STEPS) - 1)
        self._render_step()

    def _prev(self):
        self.step = max(0, self.step - 1)
        self._render_step()

    def _cancel(self):
        self.result = {"sql": "", "execute": False}
        self.destroy()

    # ---------------- 步骤 1：选表 ----------------
    def _step_table(self):
        frm = self.content
        tb.Label(frm, text="第 1 步：告诉我想看哪张表",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 10))
        row = tb.Frame(frm)
        row.pack(fill=tk.X)
        tb.Label(row, text="模式（Schema）：").pack(side=tk.LEFT)
        self.schema_var = tk.StringVar(value=self.spec.schema or (self.session.default_schema if self.session else ""))
        schemas = self.session.schemas() if self.session else []
        if self.schema_var.get() and self.schema_var.get() not in schemas:
            schemas = [self.schema_var.get()] + schemas
        cb = tb.Combobox(row, textvariable=self.schema_var, values=schemas, width=22, state="readonly")
        cb.pack(side=tk.LEFT, padx=(6, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._load_tables())

        tb.Label(row, text="搜索：").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        e = tb.Entry(row, textvariable=self.search_var, width=22)
        e.pack(side=tk.LEFT, padx=(6, 0))
        self.search_var.trace_add("write", lambda *a: self._filter_tables())
        tb.Button(row, text="刷新列表", bootstyle="secondary-outline",
                  command=self._load_tables).pack(side=tk.LEFT, padx=(10, 0))

        body = tb.Frame(frm)
        body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.table_list = tk.Listbox(body, font=("Microsoft YaHei UI", 10), activestyle="dotbox")
        sb = tkttk.Scrollbar(body, orient="vertical", command=self.table_list.yview)
        self.table_list.configure(yscrollcommand=sb.set)
        self.table_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.table_list.bind("<<ListboxSelect>>", lambda e: self._on_table_select())
        self.table_list.bind("<Double-1>", lambda e: self._next())

        self.table_info = tb.Label(frm, text="", bootstyle="secondary", wraplength=860, justify="left")
        self.table_info.pack(anchor="w", pady=(8, 0))
        self._load_tables()

    def _load_tables(self):
        if not self.session:
            return
        self.spec.schema = self.schema_var.get()
        try:
            objs = self.session.objects(self.spec.schema)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"读取表列表失败：{e}")
            objs = []
        self._objects = objs
        self._filter_tables()

    def _filter_tables(self):
        kw = (self.search_var.get() or "").strip().upper()
        self.table_list.delete(0, "end")
        for o in getattr(self, "_objects", []):
            if kw and kw not in o.name.upper():
                continue
            self.table_list.insert("end", f"{o.name}   [{o.object_type}]")
        if self.spec.table:
            for i, o in enumerate(getattr(self, "_objects", [])):
                if o.name == self.spec.table:
                    try:
                        self.table_list.selection_set(i)
                        self.table_list.see(i)
                    except Exception:
                        pass
                    break

    def _on_table_select(self):
        sel = self.table_list.curselection()
        if not sel:
            return
        objs = [o for o in getattr(self, "_objects", [])]
        visible = [o for o in objs if (self.search_var.get().strip().upper() or "") in o.name.upper()]
        idx = sel[0]
        if idx >= len(visible):
            return
        o = visible[idx]
        self.spec.table = o.name
        self.spec.columns = []
        info = f"已选择：{o.full_name}（{o.object_type}）"
        if o.num_rows is not None:
            info += f"  约 {U.human_number(o.num_rows)} 行"
        try:
            cols = self.session.columns(self.spec.schema, o.name)
            pks = [c.name for c in cols if c.is_pk]
            info += f"  共 {len(cols)} 个字段" + (f"  主键：{', '.join(pks)}" if pks else "  （未定义主键）")
        except Exception:
            pass
        self.table_info.configure(text=info)

    # ---------------- 步骤 2：选列 ----------------
    def _step_columns(self):
        frm = self.content
        tb.Label(frm, text=f"第 2 步：选择要查看的字段（表：{self.spec.schema}.{self.spec.table}）",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        try:
            self.spec_columns_meta = self.session.columns(self.spec.schema, self.spec.table)
        except Exception:
            self.spec_columns_meta = []
        names = [c.name for c in self.spec_columns_meta]
        bar = tb.Frame(frm)
        bar.pack(fill=tk.X)
        tb.Button(bar, text="全选", bootstyle="success-outline",
                  command=lambda: self.checklist.all()).pack(side=tk.LEFT)
        tb.Button(bar, text="全不选", bootstyle="secondary-outline",
                  command=lambda: self.checklist.none()).pack(side=tk.LEFT, padx=(8, 0))
        self.distinct_var = tk.BooleanVar(value=self.spec.distinct)
        tb.Checkbutton(bar, text="去掉重复行（DISTINCT）", variable=self.distinct_var,
                       bootstyle="primary-round-toggle").pack(side=tk.LEFT, padx=(16, 0))
        tb.Label(bar, text="不选任何字段 = 查看全部字段（*）", bootstyle="secondary").pack(side=tk.LEFT, padx=(16, 0))

        self.checklist = W.CheckList(frm, names, columns=4)
        self.checklist.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.checklist.configure(height=320)
        if self.spec.columns:
            self.checklist.set_checked(self.spec.columns)

    # ---------------- 步骤 3：条件 ----------------
    def _step_conditions(self):
        frm = self.content
        tb.Label(frm, text="第 3 步：加筛选条件（可加多条；不填则查整表）",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        meta = getattr(self, "spec_columns_meta", None) or (
            self.session.columns(self.spec.schema, self.spec.table) if self.session else [])
        self.cond_editor = ConditionEditor(frm, meta)
        self.cond_editor.pack(fill=tk.BOTH, expand=True)
        if self.spec.conditions:
            self.cond_editor.set_conditions(self.spec.conditions)
        else:
            self.cond_editor.add_row()
        tb.Label(frm, text="小提示：日期请写 2024-01-15；文本区分大小写；想查空值用“为空”。",
                 bootstyle="secondary").pack(anchor="w", pady=(10, 0))

    # ---------------- 步骤 4：排序与行数 ----------------
    def _step_order(self):
        frm = self.content
        tb.Label(frm, text="第 4 步：排序方式与最多返回多少行",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        meta = getattr(self, "spec_columns_meta", None) or []
        names = [""] + [c.name for c in meta]
        self.order_widgets: List[Tuple[tk.StringVar, tk.StringVar]] = []
        box = tb.Labelframe(frm, text="排序（可留空）", padding=10)
        box.pack(fill=tk.X)
        for i in range(3):
            r = tb.Frame(box)
            r.pack(fill=tk.X, pady=3)
            tb.Label(r, text=f"第 {i+1} 排序：", width=10, anchor="e").pack(side=tk.LEFT)
            cv = tk.StringVar()
            dv = tk.StringVar(value="ASC")
            cb = tb.Combobox(r, textvariable=cv, values=names, width=24, state="readonly")
            cb.pack(side=tk.LEFT, padx=(6, 0))
            cb2 = tb.Combobox(r, textvariable=dv, values=["ASC  升序", "DESC  降序"], width=12,
                              state="readonly")
            cb2.set("ASC  升序")
            cb2.pack(side=tk.LEFT, padx=(6, 0))
            self.order_widgets.append((cv, dv))
        for i, t in enumerate(self.spec.order_by or []):
            if i < len(self.order_widgets):
                self.order_widgets[i][0].set(t.column)
                self.order_widgets[i][1].set("DESC  降序" if t.direction == "DESC" else "ASC  升序")

        row2 = tb.Frame(frm)
        row2.pack(fill=tk.X, pady=(14, 0))
        tb.Label(row2, text="最多返回行数：", width=14, anchor="e").pack(side=tk.LEFT)
        self.limit_var = tk.StringVar(value=str(self.spec.limit or 200))
        tb.Spinbox(row2, from_=0, to=1000000, textvariable=self.limit_var, width=10).pack(side=tk.LEFT)
        tb.Label(row2, text="  填 0 表示不限制（大表慎用，可能很慢）", bootstyle="secondary").pack(side=tk.LEFT)
        tb.Label(frm, text="提示：执行后在结果表格里点列标题也能随时重新排序。",
                 bootstyle="secondary").pack(anchor="w", pady=(10, 0))

    def _collect_order(self):
        terms = []
        for cv, dv in getattr(self, "order_widgets", []):
            col = (cv.get() or "").strip()
            if col:
                terms.append(OrderTerm(column=col, direction=(dv.get() or "ASC").split()[0]))
        self.spec.order_by = terms
        try:
            self.spec.limit = int(self.limit_var.get() or 0)
        except Exception:
            self.spec.limit = 200

    # ---------------- 步骤 5：预览 ----------------
    def _step_preview(self):
        frm = self.content
        tb.Label(frm, text="第 5 步：确认下面这条语句，可以直接改，改完再执行",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        if hasattr(self, "checklist"):
            self.spec.columns = self.checklist.checked()
        self.spec.distinct = bool(getattr(self, "distinct_var", tk.BooleanVar()).get())
        sql = self._generate()
        colors = self.app.editor_colors()
        self.preview = SqlEditor(frm, height=16, colors=colors, wrap=False)
        self.preview.pack(fill=tk.BOTH, expand=True)
        self.preview.set_sql(sql)
        bar = tb.Frame(frm)
        bar.pack(fill=tk.X, pady=(8, 0))
        tb.Button(bar, text="重新生成", bootstyle="secondary-outline",
                  command=lambda: self.preview.set_sql(self._generate())).pack(side=tk.LEFT)
        tb.Button(bar, text="复制语句", bootstyle="secondary-outline",
                  command=lambda: U.copy_to_clipboard(self, self.preview.get_sql())).pack(side=tk.LEFT, padx=(8, 0))
        self.preview_note = tb.Label(bar, text="", bootstyle="secondary")
        self.preview_note.pack(side=tk.LEFT, padx=(12, 0))

    def _generate(self) -> str:
        col_types = {}
        try:
            col_types = self.session.column_types(self.spec.schema, self.spec.table)
        except Exception:
            pass
        sql = SB.build_select(self.spec, col_types, self.session.dialect if self.session else "oracle")
        if self.spec.limit:
            from ..core.sqlbuilder import apply_row_limit

            sql = apply_row_limit(sql, self.spec.limit, self.session.dialect if self.session else "oracle")
        return sql

    def _finish(self, execute: bool):
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "语句为空，无法执行")
            return
        self.result = {"sql": sql, "execute": execute, "schema": self.spec.schema,
                       "table": self.spec.table, "session": self.session_name}
        self.destroy()

    def _finish_execute(self):
        self._finish(True)

    def _finish_fill(self):
        self._finish(False)


# ================================================================== 写操作确认 + 备份
class DmlGuardDialog(tb.Toplevel):
    """执行写操作前的确认框：是否备份、备份哪些数据、是否提交。"""

    def __init__(self, parent, app, sql: str, schema: str, table: str, operation: str,
                 capture_sql: str = "", rowcount_hint: Optional[int] = None):
        super().__init__(parent)
        self.title(f"确认执行 {operation} 操作")
        self.transient(parent)
        self.app = app
        self.result: Dict[str, Any] = {"ok": False, "backup": False, "capture_sql": "", "commit": False}
        self.capture_sql = capture_sql

        frm = tb.Frame(self, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)

        warn = tb.Label(frm, text=f"即将对 {schema}.{table} 执行 {operation} 操作。写操作会真正改动数据库，请先确认。",
                        bootstyle="danger", wraplength=820, justify="left",
                        font=("Microsoft YaHei UI", 10, "bold"))
        warn.pack(anchor="w", pady=(0, 10))

        tb.Label(frm, text="将要执行的语句：", bootstyle="secondary").pack(anchor="w")
        colors = app.editor_colors()
        ed = SqlEditor(frm, height=9, colors=colors, wrap=False, read_only=True)
        ed.pack(fill=tk.X, pady=(4, 10))
        ed.set_sql(sql)

        self.backup_var = tk.BooleanVar(value=bool(app.settings.get("auto_backup", True)))
        tb.Checkbutton(frm, text="执行前先备份受影响的数据（强烈建议，之后可一键还原）",
                       variable=self.backup_var, bootstyle="success-round-toggle",
                       command=self._toggle_backup).pack(anchor="w")

        box = tb.Labelframe(frm, text="备份哪些数据（可修改，确认无误再执行）", padding=10)
        box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.capture_editor = SqlEditor(box, height=8, colors=colors, wrap=False)
        self.capture_editor.pack(fill=tk.BOTH, expand=True)
        self.capture_editor.set_sql(capture_sql)
        bar = tb.Frame(box)
        bar.pack(fill=tk.X, pady=(6, 0))
        self.est_btn = tb.Button(bar, text="估算影响行数", bootstyle="info-outline",
                                 command=self._estimate)
        self.est_btn.pack(side=tk.LEFT)
        self.est_label = tb.Label(bar, text="", bootstyle="secondary")
        self.est_label.pack(side=tk.LEFT, padx=(10, 0))
        if (operation or "").upper() == "INSERT":
            self.capture_editor.set_sql(
                "-- INSERT 的备份方式：直接记录本次要插入的数据，不需要预先查询数据库。\n"
                "-- 还原时会自动把这行删除。")
            self.capture_editor.text.configure(state="disabled")
            self.est_btn.configure(state="disabled")
            self.est_label.configure(text="本次将新增 1 行")
        tb.Label(box, text="说明：备份会把这些行的当前内容保存到本地文件，需要时用“备份与还原”恢复。",
                 bootstyle="secondary").pack(anchor="w", pady=(6, 0))

        self.commit_var = tk.BooleanVar(value=bool(app.settings.get("auto_commit", False)))
        tb.Checkbutton(frm, text="执行成功后立即提交（COMMIT，提交后其他用户也能看到改动）",
                       variable=self.commit_var, bootstyle="warning-round-toggle").pack(anchor="w", pady=(12, 0))
        tb.Label(frm, text="不勾选也可以：执行后数据处于未提交状态，可在工具栏点“提交”或“回滚”。",
                 bootstyle="secondary").pack(anchor="w")

        foot = tb.Frame(self, padding=(14, 0, 14, 14))
        foot.pack(fill=tk.X)
        tb.Button(foot, text="取消", bootstyle="secondary", command=self._cancel).pack(side=tk.RIGHT)
        tb.Button(foot, text="确认执行", bootstyle="danger", command=self._ok).pack(side=tk.RIGHT, padx=(0, 8))
        self._toggle_backup()
        U.fit_to_content(self, parent, min_w=860, min_h=640)
        try:
            self.grab_set()
        except Exception:
            pass

    def _toggle_backup(self):
        state = "normal" if self.backup_var.get() else "disabled"
        try:
            self.capture_editor.text.configure(state=state)
        except Exception:
            pass

    def _estimate(self):
        sql = self.capture_editor.get_sql().strip().rstrip(";")
        if not sql:
            return
        session = self.app.current_session
        if not session:
            return
        try:
            cnt_sql = f"SELECT COUNT(*) FROM (\n{sql}\n)"
            v = session.query_value(cnt_sql)
            self.est_label.configure(text=f"预计影响 {U.human_number(v)} 行", bootstyle="warning")
        except Exception as e:  # noqa: BLE001
            self.est_label.configure(text=f"无法估算：{e}", bootstyle="danger")

    def _ok(self):
        self.result = {
            "ok": True,
            "backup": bool(self.backup_var.get()),
            "capture_sql": self.capture_editor.get_sql().strip(),
            "commit": bool(self.commit_var.get()),
        }
        self.destroy()

    def _cancel(self):
        self.destroy()


CAP_ROW_LIMIT = 100000


def build_capture_sql(session: DbSession, schema: str, table: str, sql: str,
                      conditions: Optional[List[Condition]] = None,
                      col_types: Optional[Dict[str, str]] = None) -> str:
    """为写操作构造“抓取受影响数据”的 SELECT。"""
    if conditions is not None:
        return SB.build_select_for_conditions(schema, table, conditions, col_types or {},
                                              session.dialect, with_rowid=True)
    where = SB.extract_where(sql)
    quoted = SB.full_table_name(schema, table)
    # 沿用原语句的表别名：UPDATE EMP E ... WHERE E.DEPTNO=10 若写成 FROM EMP t 会 ORA-00904
    alias = SB.extract_table_alias(sql)
    if not alias and table and re.search(r"\b" + re.escape(table) + r"\s*\.", where or "", re.IGNORECASE):
        # 条件里用的是“表名.列名”，那就用表名本身当别名，同样成立
        alias = table
    alias = SB.quote_ident(alias or "t")
    if session.dialect == "oracle":
        base = f"SELECT ROWID AS ROWID__, {alias}.*\nFROM   {quoted} {alias}"
    else:
        base = f'SELECT rowid AS ROWID__, {alias}.*\nFROM   "{table}" {alias}'
    if where:
        base += f"\nWHERE  {where}"
    else:
        # 没有 WHERE 条件意味着整表，备份时强制加行数上限，避免把整表拉下来
        base += f"\nWHERE  ROWNUM <= {CAP_ROW_LIMIT}" if session.dialect == "oracle" \
            else f" LIMIT {CAP_ROW_LIMIT}"
    return base + ";"


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """按顶层分隔符切分（忽略字符串/括号内部）。"""
    out, buf = [], []
    in_str = False
    depth = 0
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_str:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _unquote_literal(text: str) -> Any:
    t = (text or "").strip()
    if t.upper() == "NULL":
        return None
    if len(t) >= 2 and t.startswith("'") and t.endswith("'"):
        return t[1:-1].replace("''", "'")
    return t


def parse_insert_values(sql: str, fallback_columns: Optional[List[str]] = None
                        ) -> Optional[Tuple[List[str], tuple]]:
    """从 INSERT 语句里解析出 (列名列表, 一行值)，用于备份“将要插入的数据”。"""
    s = (sql or "").strip().rstrip(";").strip()
    m = re.search(r"INSERT\s+INTO\s+(?P<table>[\w\"\.]+)\s*(?:\((?P<cols>[^()]*)\))?\s*"
                  r"VALUES\s*\((?P<vals>.*)\)\s*$", s, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    vals = [_unquote_literal(x) for x in _split_top_level(m.group("vals"))]
    if m.group("cols"):
        cols = [SB.strip_quotes(c.strip()) for c in _split_top_level(m.group("cols")) if c.strip()]
    else:
        cols = list(fallback_columns or [])
    if not cols or len(cols) != len(vals):
        return None
    return cols, tuple(vals)


def confirm_and_execute_dml(app, parent, session: DbSession, sql: str, schema: str, table: str,
                            operation: str, capture_sql: str = "", pk_columns: Optional[List[str]] = None,
                            note: str = "") -> Tuple[bool, str]:
    """统一的写操作入口：确认 → 备份 → 执行 → （可选）提交。

    返回 (是否执行, 说明文本)。
    """
    guard = getattr(app, "guard_write", None)
    if guard is not None and not guard(parent, f"{operation} 操作"):
        return False, "只读模式下未执行任何操作。"

    if not table:
        schema, table = SB.detect_table(sql)

    auto_confirm = bool(app.settings.get("confirm_dml", True))
    do_backup = bool(app.settings.get("auto_backup", True))
    do_commit = bool(app.settings.get("auto_commit", False))
    capture = capture_sql or build_capture_sql(session, schema, table, sql)

    if auto_confirm:
        dlg = DmlGuardDialog(parent, app, sql, schema, table, operation, capture)
        parent.wait_window(dlg)
        if not dlg.result.get("ok"):
            return False, "已取消，未执行任何操作。"
        do_backup = dlg.result.get("backup", False)
        capture = dlg.result.get("capture_sql") or capture
        do_commit = dlg.result.get("commit", False)

    backup_rec = None
    if do_backup and operation.upper() == "INSERT":
        # INSERT 的备份 = 记下要插进去的那些值（还原时删掉），不用去抓整张表
        parsed = parse_insert_values(sql, pk_columns)
        if parsed:
            cols_i, row_i = parsed
            backup_rec = app.backup_manager.create(
                connection=session.label, schema=schema or "", table=table, operation="INSERT",
                sql=sql, capture_sql="-- INSERT：备份内容为本次插入的数据", columns=cols_i,
                rows=[row_i], pk_columns=pk_columns or [], note=note or "插入前备份（还原=删除该行）",
            )
        elif auto_confirm:
            if not W.ask_yes(parent, "无法自动识别这条 INSERT 要插入的数据，因此无法备份。\n\n仍要执行吗？",
                             "无法备份"):
                return False, "已取消：无法备份。"

    if do_backup and operation.upper() != "INSERT":
        cap = (capture or "").strip().rstrip(";")
        too_many = False
        if cap:
            try:
                # 必须带上限：DELETE FROM 大表 WHERE 状态='X' 不带 max_rows 会把百万行拉进内存
                res = session.query(cap, max_rows=CAP_ROW_LIMIT)
                rows, cols = res.rows, res.columns
                if getattr(res, "truncated", False):
                    too_many = True
            except Exception as e:  # noqa: BLE001
                # ROWID 取不到时（如视图）再试一次不带 ROWID
                try:
                    cap2 = cap.replace("ROWID AS ROWID__, ", "").replace("rowid AS ROWID__, ", "")
                    res = session.query(cap2, max_rows=CAP_ROW_LIMIT)
                    rows, cols = res.rows, res.columns
                    cap = cap2
                    if getattr(res, "truncated", False):
                        too_many = True
                except Exception:
                    if auto_confirm:
                        if not W.ask_yes(parent, f"备份数据抓取失败：{e}\n\n仍要继续 {operation} 吗？（不推荐）",
                                         "备份失败"):
                            return False, "已取消：无法备份。"
                    rows, cols = [], []
        else:
            rows, cols = [], []

        if too_many:
            # 备份不完整 = 将来还原不完整，必须让用户知情后再决定
            if not W.ask_yes(parent,
                             f"这次 {operation} 影响的行数超过 {CAP_ROW_LIMIT} 行，"
                             f"备份只能保存其中一部分，将来无法完整还原。\n\n"
                             f"仍要继续吗？（建议先缩小条件范围，或分批执行）",
                             "备份不完整"):
                return False, "已取消：影响行数过多，备份不完整。"

        if rows:
            backup_rec = app.backup_manager.create(
                connection=session.label, schema=schema or "", table=table, operation=operation,
                sql=sql, capture_sql=capture, columns=cols, rows=rows,
                pk_columns=pk_columns or [], note=note,
            )
            if backup_rec is None:
                W.warning(parent, "备份失败，为安全起见已中止操作。")
                return False, "备份失败，已中止。"

    try:
        rc = session.execute(sql)
    except Exception as e:  # noqa: BLE001
        return False, f"执行失败：{e}"

    if do_commit:
        try:
            session.commit()
        except Exception as e:  # noqa: BLE001
            return True, f"已执行但提交失败：{e}"

    msg = f"{operation} 执行成功，影响 {rc} 行。"
    if backup_rec:
        msg += f"  已备份 {backup_rec.rowcount} 行 → {backup_rec.id}"
    if not do_commit:
        msg += "  （尚未提交，可在工具栏提交或回滚）"
    try:
        app.history.add(sql, tag=operation, connection=session.label)
    except Exception:
        pass
    return True, msg


# ================================================================== 插入 / 更新 / 删除 向导
class _DmlDialogBase(tb.Toplevel):
    """插入 / 更新 / 删除 共用的外壳：上半部分表单，下半部分 SQL 预览。"""

    def __init__(self, parent, app, session_name: str, schema: str, table: str, mode: str,
                 title: str, columns: Sequence[ColumnMeta]):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.app = app
        self.session_name = session_name
        self.session: Optional[DbSession] = app.get_session(session_name)
        self.schema = schema
        self.table = table
        self.mode = mode
        self.columns = list(columns or [])
        self.ok = False

        head = tb.Frame(self, padding=(14, 12, 14, 0))
        head.pack(fill=tk.X)
        tb.Label(head, text=f"表：{schema}.{table}", font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.LEFT)
        tb.Label(head, text=self._hint_text(), bootstyle="secondary").pack(side=tk.LEFT, padx=(14, 0))

        paned = tkttk.PanedWindow(self, orient="vertical")
        paned.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)
        form_body = W.ScrolledBody(paned, padding=0)  # 字段很多时可滚动
        paned.add(form_body, weight=3)
        self.form = form_body.inner
        bottom = tb.Frame(paned)
        paned.add(bottom, weight=2)

        tb.Label(bottom, text="生成的语句（可以先修改，再执行）：", bootstyle="secondary").pack(anchor="w")
        colors = app.editor_colors()
        self.preview = SqlEditor(bottom, height=10, colors=colors, wrap=False)
        self.preview.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        bar = tb.Frame(self, padding=(14, 0, 14, 14))
        bar.pack(fill=tk.X)
        tb.Button(bar, text="生成语句", bootstyle="info", command=self.generate).pack(side=tk.LEFT)
        tb.Button(bar, text="复制", bootstyle="secondary-outline",
                  command=lambda: U.copy_to_clipboard(self, self.preview.get_sql())).pack(side=tk.LEFT, padx=(8, 0))
        tb.Button(bar, text="取消", bootstyle="secondary", command=self.destroy).pack(side=tk.RIGHT)
        self.exec_btn = tb.Button(bar, text="执行", bootstyle="danger", command=self.execute)
        self.exec_btn.pack(side=tk.RIGHT, padx=(0, 8))
        # 子类还会往 self.form 里填字段，真正定尺寸在 _fit() 里做
        self.geometry("940x700")
        U.center_on_parent(self, parent)

    def _fit(self):
        """表单填完后按内容定尺寸（字段多的表会自动加高加宽）。"""
        U.fit_to_content(self, getattr(self, "master", None), min_w=940, min_h=700)
        try:
            self.grab_set()
        except Exception:
            pass

    def _hint_text(self) -> str:
        return ""

    def col_types(self) -> Dict[str, str]:
        return {c.name.upper(): c.data_type for c in self.columns}

    def generate(self):
        raise NotImplementedError

    def execute(self):
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "请先生成语句")
            return
        if not sql.endswith(";"):
            sql += ";"
        ok, msg = confirm_and_execute_dml(
            self.app, self, self.session, sql, self.schema, self.table, self.mode.upper(),
            pk_columns=[c.name for c in self.columns if c.is_pk],
        )
        if ok:
            W.toast(self, msg, "success", 4000)
            self.ok = True
            self.destroy()
        else:
            W.warning(self, msg)


class InsertDialog(_DmlDialogBase):
    def __init__(self, parent, app, session_name: str, schema: str, table: str,
                 columns: Sequence[ColumnMeta], template: Optional[Dict[str, str]] = None):
        super().__init__(parent, app, session_name, schema, table, "INSERT", "新增一行数据（向导）",
                         columns)
        self.inputs: Dict[str, Tuple[tk.BooleanVar, ValueInput]] = {}
        self._build_form(template or {})
        self._fit()

    def _hint_text(self) -> str:
        return "勾选要填写的字段，填完点“生成语句”检查，再执行。"

    def _build_form(self, template: Dict[str, str]):
        inner = self.form
        for c in self.columns:
            row = tb.Frame(inner)
            row.pack(fill=tk.X, pady=2)
            var = tk.BooleanVar(value=bool((c.name in template) or c.is_pk
                                           or (not c.nullable and c.default is None)))
            tb.Checkbutton(row, variable=var, bootstyle="primary-round-toggle").pack(side=tk.LEFT)
            tb.Label(row, text=c.name, width=24, anchor="w").pack(side=tk.LEFT)
            tb.Label(row, text=c.type_label, width=16, anchor="w", bootstyle="secondary").pack(side=tk.LEFT)
            if c.is_pk:
                tb.Label(row, text="主键", width=6, bootstyle="danger").pack(side=tk.LEFT)
            elif not c.nullable:
                tb.Label(row, text="必填", width=6, bootstyle="warning").pack(side=tk.LEFT)
            else:
                tb.Label(row, text="", width=6).pack(side=tk.LEFT)
            vi = ValueInput(row, c.data_type, template.get(c.name, ""), 26)
            vi.pack(side=tk.LEFT, padx=(6, 0))
            self.inputs[c.name] = (var, vi)
        self.preview.set_sql("-- 请在上方勾选要填写的字段、输入值，然后点“生成语句”检查\n"
                             "-- 确认无误后再点“执行”。\n"
                             "-- 想让某个字段留空（NULL），勾选该行末尾的 NULL。")

    def generate(self):
        values: Dict[str, Any] = {}
        errors = []
        for name, (var, vi) in self.inputs.items():
            if not var.get():
                continue
            ctype = self.col_types().get(name.upper(), "VARCHAR2")
            text = vi.get_text()
            if vi.is_null():
                values[name] = None
                continue
            if text == "":
                values[name] = None
                continue
            ok, py, msg = validate(ctype, text)
            if not ok:
                errors.append(msg)
                continue
            values[name] = py
        if errors:
            W.warning(self, "\n".join(errors[:6]))
            return
        if not values:
            W.warning(self, "至少要填写一个字段")
            return
        sql = SB.build_insert(self.schema, self.table, values, self.col_types(),
                              self.session.dialect if self.session else "oracle")
        self.preview.set_sql(sql)
        self._pending_values = values
        return sql


class UpdateDialog(_DmlDialogBase):
    def __init__(self, parent, app, session_name: str, schema: str, table: str,
                 columns: Sequence[ColumnMeta], row: Optional[Dict[str, Any]] = None):
        super().__init__(parent, app, session_name, schema, table, "UPDATE", "修改数据（向导）", columns)
        self.row = row or {}
        self.pk = [c.name for c in columns if c.is_pk]
        self.inputs: Dict[str, Tuple[tk.BooleanVar, ValueInput]] = {}
        self._build_form()
        self.preview.set_sql(
            "-- 1) 在上方勾选要修改的字段，填写新值\n"
            "-- 2) 确认下方的定位条件（已按主键填好，只会改这一行）\n"
            "-- 3) 点“生成语句”检查，再点“执行”\n"
        )
        self._fit()

    def _hint_text(self) -> str:
        return "上面勾要改的字段并填新值，下面填定位条件（一般按主键定位一行）。"

    def _build_form(self):
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        setbox = tb.Labelframe(self.form, text="要改成什么（新版值）", padding=10)
        setbox.pack(fill=tk.X)
        for c in self.columns:
            r = tb.Frame(setbox)
            r.pack(fill=tk.X, pady=2)
            var = tk.BooleanVar(value=False)
            tb.Checkbutton(r, variable=var, bootstyle="primary-round-toggle").pack(side=tk.LEFT)
            tb.Label(r, text=c.name, width=24, anchor="w").pack(side=tk.LEFT)
            tb.Label(r, text=c.type_label, width=16, anchor="w", bootstyle="secondary").pack(side=tk.LEFT)
            old = self.row.get(c.name)
            vi = ValueInput(r, c.data_type, format_value(old, dfmt), 26)
            vi.pack(side=tk.LEFT, padx=(6, 0))
            if old is not None:
                tb.Label(r, text=f"原值：{U.clip_text(format_value(old, dfmt), 22)}",
                         bootstyle="secondary").pack(side=tk.LEFT, padx=(8, 0))
            self.inputs[c.name] = (var, vi)

        condbox = tb.Labelframe(self.form, text="改哪些行（定位条件，必填）", padding=10)
        condbox.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.cond_editor = ConditionEditor(condbox, self.columns)
        self.cond_editor.pack(fill=tk.X)
        conds = []
        if self.row and self.pk:
            dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
            for i, pk in enumerate(self.pk):
                if pk in self.row:
                    conds.append(Condition(column=pk, op="=", value=format_value(self.row[pk], dfmt),
                                           logic="AND" if i else "AND"))
        elif self.row:
            dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
            for i, c in enumerate(self.columns):
                conds.append(Condition(column=c.name, op="=" if self.row.get(c.name) is not None else "IS_NULL",
                                       value=format_value(self.row.get(c.name), dfmt)))
                if i >= 5:
                    break
        if conds:
            self.cond_editor.set_conditions(conds)
        else:
            self.cond_editor.add_row()

    def generate(self):
        ok, msg = self.cond_editor.validate_all()
        if not ok:
            W.warning(self, msg)
            return
        conds = self.cond_editor.conditions()
        if not conds:
            W.warning(self, "必须有定位条件，不能一次性改整张表。\n如确需全表更新，请在 SQL 窗口手写语句。")
            return
        values: Dict[str, Any] = {}
        errors = []
        for name, (var, vi) in self.inputs.items():
            if not var.get():
                continue
            ctype = self.col_types().get(name.upper(), "VARCHAR2")
            if vi.is_null():
                values[name] = None
                continue
            text = vi.get_text()
            if text == "":
                values[name] = None
                continue
            ok, py, m = validate(ctype, text)
            if not ok:
                errors.append(m)
                continue
            values[name] = py
        if errors:
            W.warning(self, "\n".join(errors[:6]))
            return
        if not values:
            W.warning(self, "请至少勾选一个要修改的字段")
            return
        try:
            sql = SB.build_update(self.schema, self.table, values, conds, self.col_types(),
                                  self.session.dialect if self.session else "oracle")
        except ValueError as e:
            W.warning(self, str(e))
            return
        self.preview.set_sql(sql)
        self._pending_conditions = conds
        return sql

    def execute(self):
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "请先生成语句")
            return
        if not sql.endswith(";"):
            sql += ";"
        capture = build_capture_sql(self.session, self.schema, self.table, sql,
                                    getattr(self, "_pending_conditions", None), self.col_types())
        ok, msg = confirm_and_execute_dml(
            self.app, self, self.session, sql, self.schema, self.table, "UPDATE",
            capture_sql=capture, pk_columns=self.pk,
        )
        if ok:
            W.toast(self, msg, "success", 4000)
            self.ok = True
            self.destroy()
        else:
            W.warning(self, msg)


class DeleteDialog(_DmlDialogBase):
    def __init__(self, parent, app, session_name: str, schema: str, table: str,
                 columns: Sequence[ColumnMeta], row: Optional[Dict[str, Any]] = None):
        super().__init__(parent, app, session_name, schema, table, "DELETE", "删除数据（向导）", columns)
        self.row = row or {}
        self.pk = [c.name for c in columns if c.is_pk]
        self._build_form()
        if self.row:
            self.generate()
        self._fit()

    def _hint_text(self) -> str:
        return "删除不可大意：请填好定位条件，执行前会自动备份被删数据。"

    def _build_form(self):
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        condbox = tb.Labelframe(self.form, text="删除哪些行（定位条件，必填）", padding=10)
        condbox.pack(fill=tk.BOTH, expand=True)
        self.cond_editor = ConditionEditor(condbox, self.columns,
                                           title="删除条件（建议先用“条件预览”看看会删掉哪些行）")
        self.cond_editor.pack(fill=tk.X)
        conds = []
        if self.row and self.pk:
            for pk in self.pk:
                if pk in self.row:
                    conds.append(Condition(column=pk, op="=", value=format_value(self.row[pk], dfmt)))
        elif self.row:
            for i, c in enumerate(self.columns):
                conds.append(Condition(column=c.name, op="=" if self.row.get(c.name) is not None else "IS_NULL",
                                       value=format_value(self.row.get(c.name), dfmt)))
                if i >= 7:
                    break
        if conds:
            self.cond_editor.set_conditions(conds)
        else:
            self.cond_editor.add_row()

        bar = tb.Frame(self.form)
        bar.pack(fill=tk.X, pady=(10, 0))
        tb.Button(bar, text="先看看会删掉哪些行", bootstyle="info-outline",
                  command=self._preview_rows).pack(side=tk.LEFT)
        self.preview_btn_note = tb.Label(bar, text="", bootstyle="secondary")
        self.preview_btn_note.pack(side=tk.LEFT, padx=(10, 0))

        self.detail_box = tb.Labelframe(self.form, text="匹配到的行（最多显示 20 行）", padding=8)
        self.detail_box.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.detail_text = tk.Text(self.detail_box, height=8, wrap="none", font=("Consolas", 10))
        sb = tkttk.Scrollbar(self.detail_box, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=sb.set)
        self.detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _preview_rows(self):
        ok, msg = self.cond_editor.validate_all()
        if not ok:
            W.warning(self, msg)
            return
        conds = self.cond_editor.conditions()
        if not conds:
            W.warning(self, "请填写条件")
            return
        capture = build_capture_sql(self.session, self.schema, self.table, "", conds, self.col_types())
        try:
            res = self.session.query(capture.rstrip(";"), max_rows=21)
        except Exception as e:  # noqa: BLE001
            W.error(self, f"预览失败：{e}")
            return
        dfmt = self.app.settings.get("date_format", "%Y-%m-%d %H:%M:%S")
        lines = ["\t".join(res.columns)]
        for r in res.rows[:20]:
            lines.append("\t".join(format_value(v, dfmt) for v in r))
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")
        self.preview_btn_note.configure(text=f"共匹配 {U.human_number(len(res.rows))} 行")

    def generate(self):
        ok, msg = self.cond_editor.validate_all()
        if not ok:
            W.warning(self, msg)
            return
        conds = self.cond_editor.conditions()
        if not conds:
            W.warning(self, "必须有定位条件，不能删除整张表的数据。\n如需清空整表，请在 SQL 窗口手写语句。")
            return
        try:
            sql = SB.build_delete(self.schema, self.table, conds, self.col_types(),
                                  self.session.dialect if self.session else "oracle")
        except ValueError as e:
            W.warning(self, str(e))
            return
        self.preview.set_sql(sql)
        self._pending_conditions = conds
        return sql

    def execute(self):
        sql = self.preview.get_sql().strip()
        if not sql:
            W.warning(self, "请先生成语句")
            return
        if not sql.endswith(";"):
            sql += ";"
        capture = build_capture_sql(self.session, self.schema, self.table, sql,
                                    getattr(self, "_pending_conditions", None), self.col_types())
        if not W.ask_yes(self, "删除操作会真正删除数据库中的数据。\n确认要继续吗？", "再次确认"):
            return
        ok, msg = confirm_and_execute_dml(
            self.app, self, self.session, sql, self.schema, self.table, "DELETE",
            capture_sql=capture, pk_columns=self.pk,
        )
        if ok:
            W.toast(self, msg, "success", 4000)
            self.ok = True
            self.destroy()
        else:
            W.warning(self, msg)
