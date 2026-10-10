"""数据网格：排序、筛选、右键菜单、双击查看详情。"""
from __future__ import annotations

import tkinter as tk
from functools import cmp_to_key
from tkinter import ttk as tkttk
from typing import Any, Callable, Dict, List, Optional, Sequence

import ttkbootstrap as tb

from ..core.utils import copy_to_clipboard, truncate_middle
from ..core.validator import compare_values, format_value
from . import widgets as W


class DataTable(tb.Frame):
    """可排序、可筛选的结果表格。"""

    def __init__(self, master, callbacks: Optional[Dict[str, Callable]] = None,
                 date_format: str = "%Y-%m-%d %H:%M:%S", null_display: str = "",
                 show_rowno: bool = True, max_rows: int = 20000,
                 even_color: str = "#f6f8fa", odd_color: str = "#ffffff",
                 heading_bg: str = "#eef2f7", heading_fg: str = "#1f2d3d",
                 page_size: int = 0):
        super().__init__(master)
        self.callbacks = callbacks or {}
        self.date_format = date_format
        self.null_display = null_display
        self.show_rowno = show_rowno
        self.max_rows = max_rows
        self.even_color = even_color
        self.odd_color = odd_color
        # 分页显示：page_size>0 时每次只渲染一页，几千行数据也不会把界面卡死；
        # 0 = 不分页（一次渲染全部），SQL 查询窗口等其它调用方保持原行为。
        self.page_size = max(0, int(page_size))
        self.page = 0  # 0 起始页码

        self.columns: List[str] = []
        self.raw: List[tuple] = []
        self.order: List[int] = []
        self.hidden: set = set()
        self._sort: Dict[int, str] = {}
        self._filter_fn: Optional[Callable[[tuple], bool]] = None
        # 按行内容返回额外标签（用于把“表空间使用率 90%”这类行标红）
        self.row_tag_fn: Optional[Callable[[tuple], str]] = None
        self.truncated = False
        # 右键所在的数据列（列级探查入口用），None 表示点在了行号列或空白处
        self.menu_column: Optional[str] = None

        self.tree = tb.Treeview(self, columns=(), show="headings", selectmode="extended",
                                bootstyle="primary")
        sb_y = tkttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sb_x = tkttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.tree.tag_configure("even", background=even_color)
        self.tree.tag_configure("odd", background=odd_color)
        self.tree.tag_configure("changed", background="#fff3cd")

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Control-c>", self._on_copy_key)
        self.tree.bind("<Control-C>", self._on_copy_key)
        self._build_menu()

    # ---------------------------------------------------------------- 数据
    def set_data(self, columns: Sequence[str], rows: Sequence[tuple], truncated: bool = False):
        self.columns = list(columns or [])
        total = len(rows or [])
        self.truncated = truncated or total > self.max_rows
        self.raw = list(rows or [])[: self.max_rows]
        self.order = list(range(len(self.raw)))
        self.hidden = set()
        self._sort = {}
        self._filter_fn = None
        self.page = 0
        self._build_columns()
        self.refresh()

    def clear(self):
        self.set_data([], [])

    def append_data(self, rows: Sequence[tuple], truncated: bool = False):
        start = len(self.raw)
        self.raw.extend(list(rows or [])[: max(0, self.max_rows - len(self.raw))])
        self.order.extend(range(start, len(self.raw)))
        self.truncated = truncated
        self.refresh()

    def _build_columns(self):
        cols = (["#"] if self.show_rowno else []) + self.columns
        self.tree.configure(columns=cols)
        width_no = max(5, len(str(len(self.raw))) + 2)
        if self.show_rowno:
            self.tree.heading("#", text="#", anchor="center")
            self.tree.column("#", width=38, anchor="center", stretch=False)
        for i, c in enumerate(self.columns):
            key = self.columns[i]
            self.tree.heading(key, text=c, anchor="w", command=lambda k=key: self._sort_by_name(k))
            w = self._estimate_width(i)
            # stretch=False 保证字段宽度不被压扁、列数多时横向滚动条生效，
            # 避免出现“字段名和内容挤在一起、显示不全”的情况。
            self.tree.column(key, width=w, minwidth=24, anchor="w", stretch=False)

    def _estimate_width(self, idx: int) -> int:
        text_len = len(str(self.columns[idx])) + 2
        sample = self.raw[:80]
        maxlen = text_len
        for r in sample:
            if idx < len(r):
                maxlen = max(maxlen, len(format_value(r[idx], self.date_format, self.null_display)) + 2)
        return min(max(maxlen * 8, 70), 320)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        idxs = [i for i in self.order if i not in self.hidden]
        start = 0
        render = idxs
        if self.page_size > 0:
            total_pages = self.page_count()
            if self.page >= total_pages:
                self.page = total_pages - 1
            if self.page < 0:
                self.page = 0
            start = self.page * self.page_size
            render = idxs[start:start + self.page_size]
        n = 0
        for idx in render:
            row = self.raw[idx]
            vals = [format_value(v, self.date_format, self.null_display) for v in row]
            if self.show_rowno:
                # 行号按“可见顺序中的位置”连续编号，翻页后接着上一页计数
                vals = [start + n + 1] + vals
            tag = "even" if n % 2 == 0 else "odd"
            extra = ""
            if self.row_tag_fn:
                try:
                    extra = self.row_tag_fn(row) or ""
                except Exception:  # noqa: BLE001
                    extra = ""
            tags = (tag, extra) if extra else (tag,)
            self.tree.insert("", "end", iid=f"r{idx}", values=vals, tags=tags)
            n += 1
        self._update_headings()
        if hasattr(self, "on_refresh"):
            try:
                self.on_refresh(n, len(self.raw))
            except Exception:
                pass

    def _update_headings(self):
        for key in self.columns:
            base = key
            mark = ""
            if key in self._sort:
                mark = " ▲" if self._sort[key] == "asc" else " ▼"
            self.tree.heading(key, text=base + mark)

    # ---------------------------------------------------------------- 排序
    def _sort_by_name(self, key: str):
        idx = self.columns.index(key) if key in self.columns else -1
        if idx < 0:
            return
        cur = self._sort.get(key)
        direction = "asc" if cur != "asc" else "desc"
        self._sort = {key: direction}
        reverse = direction == "desc"

        def cmpf(a, b):
            return compare_values(a[idx] if idx < len(a) else None,
                                  b[idx] if idx < len(b) else None)

        try:
            self.order.sort(key=cmp_to_key(lambda i, j: cmpf(self.raw[i], self.raw[j])), reverse=reverse)
        except Exception:
            pass
        self.refresh()

    def _on_click(self, event):
        """这里只负责“点行号列 → 恢复原始顺序”。

        其它列的排序由 heading 自带的 command 触发。两边都排的话，
        一次点击会切换两次方向（None→asc→desc），结果永远只能排降序。
        """
        if self.tree.identify_region(event.x, event.y) != "heading":
            return
        col = self.tree.identify_column(event.x)
        if not self.show_rowno or col != "#1":
            return
        self.order = list(range(len(self.raw)))
        self._sort = {}
        self.refresh()

    # ---------------------------------------------------------------- 筛选
    def set_filter(self, fn: Optional[Callable[[tuple], bool]]):
        """fn(原始行) -> True 表示显示。"""
        self._filter_fn = fn
        self.hidden = set()
        if fn:
            for i, row in enumerate(self.raw):
                try:
                    if not fn(row):
                        self.hidden.add(i)
                except Exception:
                    pass
        self.refresh()

    def set_row_tag_fn(self, fn: Optional[Callable[[tuple], str]]):
        """fn(原始行) -> 标签名（返回空串表示不加标签）。

        标签需要先用 grid.tree.tag_configure(...) 定义好配色。
        """
        self.row_tag_fn = fn
        self.refresh()

    def clear_filter(self):
        self.set_filter(None)

    # ---------------------------------------------------------------- 分页
    def page_count(self) -> int:
        if self.page_size <= 0:
            return 1
        vis = sum(1 for i in self.order if i not in self.hidden)
        return max(1, (vis + self.page_size - 1) // self.page_size)

    def current_page(self) -> int:
        """当前页码（1 起始）。"""
        return self.page + 1

    def goto_page(self, page: int):
        """跳到第 page 页（1 起始，越界自动收敛到有效范围）。"""
        if self.page_size <= 0:
            return
        self.page = int(page) - 1
        self.refresh()

    def next_page(self):
        self.goto_page(self.page + 2)

    def prev_page(self):
        self.goto_page(self.page)

    def first_page(self):
        self.goto_page(1)

    def last_page(self):
        self.goto_page(self.page_count())

    def set_page_size(self, n: int):
        self.page_size = max(0, int(n))
        self.page = 0
        self.refresh()

    def visible_rows(self) -> List[tuple]:
        return [self.raw[i] for i in self.order if i not in self.hidden]

    def visible_count(self) -> int:
        return len(self.order) - len(self.hidden)

    # ---------------------------------------------------------------- 选择
    def selected_rows(self) -> List[tuple]:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(self.raw[int(iid[1:])])
            except Exception:
                pass
        return out

    def selected_indexes(self) -> List[int]:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(iid[1:]))
            except Exception:
                pass
        return out

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    # ---------------------------------------------------------------- 交互
    def _on_double(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        self._emit("view")

    def _emit(self, key: str, *args):
        fn = self.callbacks.get(key)
        if fn:
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                W.error(self, f"操作失败：{e}")

    def _on_copy_key(self, event=None):
        self.copy_rows()
        return "break"

    def _build_menu(self):
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="复制单元格", command=self.copy_cell)
        self.menu.add_command(label="复制选中行（可直接粘贴到 Excel）", command=self.copy_rows)
        self.menu.add_command(label="复制列名", command=self.copy_headers)
        self.menu.add_separator()
        self.menu.add_command(label="全选", command=self.select_all)
        self.menu.add_command(label="查看整行详情", command=lambda: self._emit("view"))
        self.menu.add_separator()
        self.menu.add_command(label="修改此行…", command=lambda: self._emit("edit"))
        self.menu.add_command(label="删除此行…", command=lambda: self._emit("delete"))
        self.menu.add_command(label="新增一行…", command=lambda: self._emit("insert"))
        self.menu.add_separator()
        self.menu.add_command(label="导出当前视图到 Excel…", command=lambda: self._emit("export"))
        self.menu.add_separator()
        # 列级探查入口（数据探查组）
        self._col_menu_idx = {}
        for key, label in (("dup", "查此列重复值…"),
                           ("distinct", "此列值分布…"),
                           ("drill", "用此列的值去查…")):
            self.menu.add_command(label=label, command=lambda k=key: self._emit_col(k))
            self._col_menu_idx[key] = self.menu.index("end")

    def _column_at(self, x: int) -> Optional[str]:
        """右键横坐标对应的数据列名（扣除行号列的偏移）。"""
        try:
            col = self.tree.identify_column(x)
            if not col or not col.startswith("#"):
                return None
            pos = int(col[1:]) - 1
            if self.show_rowno:
                pos -= 1
            return self.columns[pos] if 0 <= pos < len(self.columns) else None
        except Exception:
            return None

    def _emit_col(self, key: str):
        if not self.menu_column:
            return
        self._emit(key, self.menu_column)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
        self.menu_column = self._column_at(event.x)
        col = self.menu_column
        tag = f"「{col}」" if col else "此列"
        try:
            state = "normal"
            has_edit = bool(self.callbacks.get("edit"))
            self.menu.entryconfigure("修改此行…", state=state if has_edit else "disabled")
            self.menu.entryconfigure("删除此行…", state=state if self.callbacks.get("delete") else "disabled")
            self.menu.entryconfigure("新增一行…", state=state if self.callbacks.get("insert") else "disabled")
            self.menu.entryconfigure("导出当前视图到 Excel…", state=state if self.callbacks.get("export") else "disabled")
            for key, tmpl in (("dup", "查{}重复值…"),
                              ("distinct", "{}值分布…"),
                              ("drill", "用{}的值去查…")):
                idx = self._col_menu_idx.get(key)
                if idx is None:
                    continue
                ok = bool(col) and bool(self.callbacks.get(key))
                self.menu.entryconfigure(idx, label=tmpl.format(tag),
                                         state="normal" if ok else "disabled")
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def copy_headers(self):
        copy_to_clipboard(self, "\t".join(self.columns))
        W.toast(self, "列名已复制", "success")

    def copy_cell(self):
        iid = self.tree.focus()
        if not iid:
            return
        try:
            row = self.raw[int(iid[1:])]
            col = self.tree.identify_column(self.tree.winfo_pointerx() - self.tree.winfo_rootx())
        except Exception:
            col = None
        idx = 0
        try:
            if col:
                idx = int(col[1:]) - 1 - (1 if self.show_rowno else 0)
        except Exception:
            idx = 0
        if 0 <= idx < len(row):
            copy_to_clipboard(self, format_value(row[idx], self.date_format))
            W.toast(self, "单元格已复制", "success")

    def copy_rows(self, with_header: bool = True):
        rows = self.selected_rows() or self.visible_rows()
        if not rows:
            W.toast(self, "没有可复制的数据", "warning")
            return
        lines = []
        if with_header:
            lines.append("\t".join(self.columns))
        for r in rows:
            lines.append("\t".join(format_value(v, self.date_format) for v in r))
        copy_to_clipboard(self, "\n".join(lines))
        W.toast(self, f"已复制 {len(rows)} 行，可直接粘贴到 Excel", "success")

    def apply_colors(self, even: str, odd: str):
        self.even_color = even
        self.odd_color = odd
        self.tree.tag_configure("even", background=even)
        self.tree.tag_configure("odd", background=odd)
        self.refresh()


def show_row_detail(parent, columns: Sequence[str], row: Sequence[Any],
                    date_format: str = "%Y-%m-%d %H:%M:%S", title: str = "行详情"):
    """宽表场景下竖着看一条记录。"""
    top = tb.Toplevel(parent)
    top.title(title)
    top.geometry("640x560")
    frm = tb.Frame(top, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)

    tree = tb.Treeview(frm, columns=("列名", "值"), show="headings", bootstyle="primary")
    tree.heading("列名", text="列名")
    tree.heading("值", text="值")
    tree.column("列名", width=180, anchor="w")
    tree.column("值", width=400, anchor="w")
    for c, v in zip(columns, row):
        tree.insert("", "end", values=(c, format_value(v, date_format)))
    sb = tkttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    bar = tb.Frame(top, padding=(10, 0, 10, 10))
    bar.pack(fill=tk.X)

    def copy_all():
        text = "\n".join(f"{c}\t{format_value(v, date_format)}" for c, v in zip(columns, row))
        copy_to_clipboard(top, text)
        W.toast(top, "已复制", "success")

    tb.Button(bar, text="复制", bootstyle="primary", command=copy_all).pack(side=tk.LEFT)
    tb.Button(bar, text="关闭", bootstyle="secondary", command=top.destroy).pack(side=tk.RIGHT)
    W._attach_tooltip(tree, "双击任意行可复制该字段")
    tree.bind("<Double-1>", lambda e: _copy_focused(tree, top))
    from ..core.utils import center_on_parent

    center_on_parent(top, parent)
    return top


def _copy_focused(tree, parent):
    iid = tree.focus()
    if not iid:
        return
    vals = tree.item(iid, "values")
    if vals:
        copy_to_clipboard(parent, str(vals[-1]))
        W.toast(parent, "已复制", "success")
