"""SQL 编辑框：行号 + 关键字高亮 + 右键菜单。"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk as tkttk
from typing import Callable, Optional

import ttkbootstrap as tb

KEYWORDS = [
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN",
    "ORDER", "BY", "GROUP", "HAVING", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
    "AS", "JOIN", "ON", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "UNION", "ALL",
    "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END", "CREATE", "TABLE", "VIEW", "INDEX",
    "DROP", "ALTER", "TRUNCATE", "COMMIT", "ROLLBACK", "GRANT", "REVOKE", "MERGE", "USING",
    "WITH", "EXISTS", "ASC", "DESC", "FETCH", "OFFSET", "ROWS", "ONLY", "ROWID", "ROWNUM",
    "DUAL", "PRIMARY", "KEY", "FOREIGN", "REFERENCES", "DEFAULT", "CHECK", "UNIQUE",
    "BEGIN", "DECLARE", "EXCEPTION", "IF", "THEN", "LOOP", "END", "CURSOR", "TYPE", "IS",
]
FUNCTIONS = [
    "COUNT", "SUM", "AVG", "MIN", "MAX", "NVL", "COALESCE", "DECODE", "TO_DATE", "TO_CHAR",
    "TO_NUMBER", "SYSDATE", "SYSTIMESTAMP", "TRUNC", "ROUND", "SUBSTR", "LENGTH", "UPPER",
    "LOWER", "TRIM", "REPLACE", "INSTR", "LPAD", "RPAD", "MOD", "ABS", "CEIL", "FLOOR",
    "LISTAGG", "RANK", "ROW_NUMBER", "OVER", "PARTITION", "CAST", "NULLIF", "GREATEST", "LEAST",
]

_KW_RE = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)
_FN_RE = re.compile(r"\b(" + "|".join(FUNCTIONS) + r")\b", re.IGNORECASE)
_COMMENT_RE = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


class SqlEditor(tb.Frame):
    def __init__(self, master, height: int = 12, font=("Consolas", 11), wrap: bool = True,
                 colors: Optional[dict] = None, on_change: Optional[Callable] = None,
                 read_only: bool = False):
        super().__init__(master)
        c = colors or {}
        self.colors = c
        self.on_change = on_change
        self._after_id = None

        bg = c.get("bg", "#ffffff")
        fg = c.get("fg", "#1f2933")
        gut_bg = c.get("gutter_bg", "#f3f5f8")
        gut_fg = c.get("gutter_fg", "#98a2b3")

        self.gutter = tk.Text(self, width=5, height=height, font=font, bg=gut_bg, fg=gut_fg,
                              relief="flat", borderwidth=0, takefocus=0, highlightthickness=0,
                              state="disabled", wrap="none")
        self.text = tk.Text(self, height=height, font=font, bg=bg, fg=fg,
                            insertbackground=c.get("insert", fg), relief="flat", borderwidth=0,
                            highlightthickness=1, highlightbackground="#d6dee8",
                            highlightcolor="#c3d4e6", wrap="word" if wrap else "none",
                            undo=True, maxundo=200, padx=6, pady=4, spacing1=1, spacing3=1)
        sb_y = tkttk.Scrollbar(self, orient="vertical")
        sb_x = tkttk.Scrollbar(self, orient="horizontal")

        self.text.configure(yscrollcommand=self._on_text_scroll(sb_y))
        self.text.configure(xscrollcommand=sb_x.set)
        sb_y.configure(command=self._sync_scroll)
        sb_x.configure(command=self.text.xview)

        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        sb_y.grid(row=0, column=2, sticky="ns")
        sb_x.grid(row=1, column=1, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        for tag, color, extra in (
            ("keyword", c.get("keyword", "#1a56b8"), ("bold",)),
            ("func", c.get("func", "#7c3aed"), ()),
            ("string", c.get("string", "#0f7b53"), ()),
            ("comment", c.get("comment", "#8a94a6"), ("italic",)),
            ("number", c.get("number", "#b45309"), ()),
        ):
            self.text.tag_configure(tag, foreground=color, font=(font[0], font[1]) + extra)

        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", lambda e: self._schedule())
        self.text.bind("<ButtonRelease-1>", lambda e: self._update_gutter())
        self.text.bind("<Configure>", lambda e: self._update_gutter())
        self._build_menu()
        if read_only:
            self.text.configure(state="disabled")
        self._update_gutter()
        self._highlight()

    # ---------------- 滚动同步 ----------------
    def _on_text_scroll(self, sb):
        def handler(*args):
            sb.set(*args)
            self._update_gutter()
        return handler

    def _sync_scroll(self, *args):
        self.text.yview(*args)
        self.gutter.yview(*args)

    # ---------------- 行号 ----------------
    def _update_gutter(self):
        try:
            lines = int(self.text.index("end-1c").split(".")[0])
        except Exception:
            lines = 1
        width = max(4, len(str(lines)) + 1)
        if self.gutter.cget("width") != width:
            self.gutter.configure(width=width)
        self.gutter.configure(state="normal")
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", "\n".join(str(i) for i in range(1, lines + 1)))
        self.gutter.configure(state="disabled")
        try:
            self.gutter.yview_moveto(self.text.yview()[0])
        except Exception:
            pass

    # ---------------- 高亮 ----------------
    def _schedule(self):
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(220, self._tick_change)

    def _tick_change(self):
        self._update_gutter()
        self._highlight()
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _on_modified(self, event=None):
        try:
            self.text.edit_modified(False)
        except Exception:
            pass
        self._schedule()

    def _highlight(self):
        txt = self.text
        try:
            content = txt.get("1.0", "end-1c")
        except Exception:
            return
        for tag in ("keyword", "func", "string", "comment", "number"):
            txt.tag_remove(tag, "1.0", "end")
        if len(content) > 200000:  # 超大脚本只高亮前一部分，避免卡顿
            content = content[:200000]
        for m in _COMMENT_RE.finditer(content):
            self._tag_range("comment", m.start(), m.end())
        for m in _STRING_RE.finditer(content):
            self._tag_range("string", m.start(), m.end())
        for m in _KW_RE.finditer(content):
            self._tag_range("keyword", m.start(), m.end())
        for m in _FN_RE.finditer(content):
            self._tag_range("func", m.start(), m.end())
        for m in _NUMBER_RE.finditer(content):
            self._tag_range("number", m.start(), m.end())

    def _tag_range(self, tag, start, end):
        try:
            self.text.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
        except Exception:
            pass

    # ---------------- 内容 ----------------
    def get_sql(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except Exception:
            return ""

    def set_sql(self, sql: str):
        try:
            state = self.text.cget("state")
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", sql or "")
            self.text.configure(state=state)
            self._update_gutter()
            self._highlight()
        except Exception:
            pass

    def get_selected(self) -> str:
        try:
            sel = self.text.tag_ranges("sel")
            if sel:
                return self.text.get(sel[0], sel[1])
        except Exception:
            pass
        return ""

    def insert_text(self, text: str, at_cursor: bool = True):
        try:
            self.text.insert("insert" if at_cursor else "end", text)
        except Exception:
            pass

    def clear(self):
        self.set_sql("")

    def focus_text(self):
        try:
            self.text.focus_set()
        except Exception:
            pass

    # ---------------- 右键菜单 ----------------
    def _build_menu(self):
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="撤销  Ctrl+Z", command=self._undo)
        self.menu.add_command(label="重做  Ctrl+Y", command=self._redo)
        self.menu.add_separator()
        self.menu.add_command(label="剪切  Ctrl+X", command=self._cut)
        self.menu.add_command(label="复制  Ctrl+C", command=self._copy)
        self.menu.add_command(label="粘贴  Ctrl+V", command=self._paste)
        self.menu.add_separator()
        self.menu.add_command(label="全选  Ctrl+A", command=self._select_all)
        self.menu.add_command(label="清空", command=self.clear)
        self.text.bind("<Button-3>", self._popup)
        # 让 Ctrl+A 在中文输入法下也能用
        self.text.bind("<Control-a>", lambda e: self._select_all())
        self.text.bind("<Control-A>", lambda e: self._select_all())

    def _popup(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _undo(self):
        try:
            self.text.edit_undo()
        except Exception:
            pass

    def _redo(self):
        try:
            self.text.edit_redo()
        except Exception:
            pass

    def _cut(self):
        try:
            self.text.event_generate("<<Cut>>")
        except Exception:
            pass

    def _copy(self):
        try:
            self.text.event_generate("<<Copy>>")
        except Exception:
            pass

    def _paste(self):
        try:
            self.text.event_generate("<<Paste>>")
        except Exception:
            pass

    def _select_all(self):
        try:
            self.text.tag_add("sel", "1.0", "end")
            return "break"
        except Exception:
            return None

    def apply_colors(self, colors: dict):
        self.colors = colors
        try:
            self.text.configure(bg=colors.get("bg"), fg=colors.get("fg"),
                                insertbackground=colors.get("insert"))
            self.gutter.configure(bg=colors.get("gutter_bg"), fg=colors.get("gutter_fg"))
        except Exception:
            pass
        font = self.text.cget("font")
        for tag, key in (("keyword", "keyword"), ("func", "func"), ("string", "string"),
                         ("comment", "comment"), ("number", "number")):
            try:
                self.text.tag_configure(tag, foreground=colors.get(key), font=font)
            except Exception:
                pass
