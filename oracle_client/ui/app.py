"""主程序：主窗口、菜单、会话与子窗口管理。"""
from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import ttk as tkttk
from typing import Dict, List, Optional, Tuple

import ttkbootstrap as tb

from ..core.config import APP_NAME, APP_VERSION, ConnectionStore, Settings, SqlStore, data_dir
from ..core.templates import TemplateStore
from ..core.db import DbSession, create_session
from ..core.models import ConnProfile
from ..core import utils as U
from . import theme as TH
from . import widgets as W
from .schema import SchemaPanel
from .windows import BaseSubWindow, QueryWindow, TableWindow


class Application:
    def __init__(self):
        self.settings = Settings()
        self.conn_store = ConnectionStore()
        self.history = SqlStore("history.json", limit=400)
        self.favorites = SqlStore("favorites.json", limit=200)
        self.templates = TemplateStore()
        self.backup_manager = _make_backup_manager(self.settings)

        self.sessions: Dict[str, DbSession] = {}
        self.current_session_name = ""
        self.windows: List[BaseSubWindow] = []
        self._template_dialog = None      # 模板库对话框（非子窗口，单独记引用）

        theme_name = self.settings.get("theme", TH.DEFAULT_THEME) or TH.DEFAULT_THEME
        self.root = tb.Window(themename=theme_name, title=f"{APP_NAME}  v{APP_VERSION}",
                              size=(1400, 900), minsize=(1080, 680))
        TH.apply_font(self.root, self.settings.get("font_family", "Microsoft YaHei UI"),
                      int(self.settings.get("font_size", 10)))
        self._set_icon()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            geom = self.settings.get("window_geometry")
            if geom:
                self.root.geometry(geom)
        except Exception:
            pass
        self.root.after(200, self._startup)

    # ================================================================ 图标
    def _set_icon(self):
        try:
            path = data_dir() / "app_icon.png"
            if not path.exists():
                _make_icon(path)
            if path.exists():
                photo = tk.PhotoImage(file=str(path))
                self.root.iconphoto(True, photo)
                self._icon = photo  # 防止被回收
        except Exception:
            pass

    # ================================================================ 菜单
    def _build_menu(self):
        c = TH.grid_colors(self.root.style)
        style_kwargs = dict(tearoff=0, bg=c["bg"], fg=c["fg"], activebackground=c["selected"],
                            activeforeground=c["fg"], relief="flat", borderwidth=0)
        self.menu_kwargs = style_kwargs
        menubar = tk.Menu(self.root, **style_kwargs)
        self.root.configure(menu=menubar)
        self.menubar = menubar

        file_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="新建查询窗口  Ctrl+N", command=lambda: self.new_query_window())
        file_menu.add_command(label="新建数据窗口  Ctrl+T", command=lambda: self.new_table_window())
        file_menu.add_separator()
        file_menu.add_command(label="打开 SQL 脚本…", command=self._open_script_global)
        file_menu.add_command(label="导出数据…", command=self._export_global)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)

        conn_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="连接", menu=conn_menu)
        conn_menu.add_command(label="连接管理…", command=self.open_connection_manager)
        conn_menu.add_command(label="新建连接…", command=self.new_connection)
        conn_menu.add_separator()
        conn_menu.add_command(label="提交 COMMIT", command=self.do_commit)
        conn_menu.add_command(label="回滚 ROLLBACK", command=self.do_rollback)
        conn_menu.add_separator()
        conn_menu.add_command(label="断开当前连接", command=self.disconnect_current)

        tool_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="工具", menu=tool_menu)
        tool_menu.add_command(label="查询向导  Ctrl+G", command=self.open_wizard)
        tool_menu.add_command(label="运维 SQL 模板库  Ctrl+Shift+L", command=self.open_template_library)
        tool_menu.add_command(label="运维概览（实例/慢SQL/表空间）  Ctrl+Shift+O", command=self.open_ops)
        tool_menu.add_command(label="备份与还原  Ctrl+B", command=self.open_backup_center)
        tool_menu.add_command(label="查看表结构…", command=self.open_table_info)
        tool_menu.add_separator()
        tool_menu.add_command(label="SQL 历史与收藏", command=self.open_history)
        tool_menu.add_command(label="设置…", command=self.open_settings)

        view_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="视图", menu=view_menu)
        self.theme_menu = tk.Menu(view_menu, **style_kwargs)
        view_menu.add_cascade(label="界面主题", menu=self.theme_menu)
        self._build_theme_menu()
        view_menu.add_separator()
        view_menu.add_command(label="刷新对象浏览器  Ctrl+R", command=self.refresh_schema)
        view_menu.add_command(label="显示/隐藏对象浏览器", command=self.toggle_schema_panel)

        self.win_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="窗口", menu=self.win_menu)
        self._build_window_menu()

        help_menu = tk.Menu(menubar, **style_kwargs)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="使用说明", command=self.show_help)
        help_menu.add_command(label="关于", command=self.show_about)

    def _build_theme_menu(self):
        self.theme_menu.delete(0, "end")
        self.theme_var = tk.StringVar(value=self.settings.get("theme", TH.DEFAULT_THEME))
        for key, label in TH.all_themes().items():
            self.theme_menu.add_radiobutton(label=label, variable=self.theme_var, value=key,
                                            command=lambda k=key: self.switch_theme(k))

    def _build_window_menu(self):
        if not getattr(self, "win_menu", None):
            return
        # 清理已被销毁却仍留在列表里的窗口（例如代码直接 destroy、或被父窗口连带销毁）
        alive = []
        for w in self.windows:
            try:
                if w.winfo_exists():
                    alive.append(w)
            except Exception:
                continue
        if len(alive) != len(self.windows):
            self.windows = alive
        try:
            self.win_menu.delete(0, "end")
            self.win_menu.add_command(label="层叠排列", command=self.cascade_windows)
            self.win_menu.add_command(label="平铺排列", command=self.tile_windows)
            self.win_menu.add_command(label="关闭所有子窗口", command=self.close_all_windows)
            self.win_menu.add_separator()
            if not self.windows:
                self.win_menu.add_command(label="（没有打开的子窗口）", state="disabled")
            for w in self.windows:
                try:
                    title = w.title()[:36]
                except Exception:
                    continue
                self.win_menu.add_command(label=title, command=lambda x=w: self.focus_window(x))
        except Exception:
            pass

    # ================================================================ 工具栏
    def _build_toolbar(self):
        tb_ = W.Toolbar(self.root, padding=(8, 6))
        tb_.pack(fill=tk.X)
        tb_.add_button("🔌 连接管理", self.open_connection_manager, "primary")
        self.conn_var = tk.StringVar(value="(未连接)")
        self.conn_combo = tb_.add_combo(self.conn_var, ["(未连接)"], width=30,
                                        command=self._on_conn_combo)
        tb_.add_sep()
        tb_.add_button("＋ 查询窗口", lambda: self.new_query_window(), "success")
        tb_.add_button("＋ 数据窗口", lambda: self.new_table_window(), "success-outline")
        tb_.add_button("🪄 查询向导", self.open_wizard, "info")
        tb_.add_button("📈 运维概览", self.open_ops, "info-outline")
        tb_.add_sep()
        tb_.add_button("备份与还原", self.open_backup_center, "warning")
        tb_.add_sep()
        tb_.add_button("提交", self.do_commit, "warning-outline")
        tb_.add_button("回滚", self.do_rollback, "secondary-outline")
        self.auto_commit_var = tk.BooleanVar(value=bool(self.settings.get("auto_commit", False)))
        tb_.add_check("自动提交", self.auto_commit_var, command=self._on_auto_commit)
        self.read_only_var = tk.BooleanVar(value=bool(self.settings.get("read_only", False)))
        ro = tb_.add_check("只读", self.read_only_var, command=self._on_read_only,
                           bootstyle="danger-round-toggle")
        W._attach_tooltip(ro, "开启后所有写操作都会被拦下；连到标记为“生产”的库会自动开启")
        tb_.add_spacer()
        tb_.add_button("使用说明", self.show_help, "secondary-link")
        self.toolbar = tb_

    def _on_auto_commit(self):
        self.settings.set("auto_commit", bool(self.auto_commit_var.get()))

    def _on_read_only(self):
        self.settings.set("read_only", bool(self.read_only_var.get()))
        self._sync_read_only()
        self._update_status()

    def _sync_read_only(self):
        """把“只读”状态同步到所有子窗口（生产库自动只读也算）。"""
        try:
            self.read_only_var.set(bool(self.settings.get("read_only", False)))
        except Exception:
            pass
        for w in list(self.windows):
            try:
                if hasattr(w, "sync_read_only"):
                    w.sync_read_only(self.read_only_active)
            except Exception:
                pass

    # ---------------- 只读保护 ----------------
    @property
    def read_only_active(self) -> bool:
        """当前是否处于只读状态（手动开关 或 连接被标记为“生产”）。"""
        if bool(self.settings.get("read_only", False)):
            return True
        if bool(self.settings.get("env_read_only", True)):
            s = self.current_session
            if s is not None and getattr(getattr(s, "profile", None), "is_prod", False):
                return True
        return False

    def read_only_reason(self) -> str:
        if bool(self.settings.get("read_only", False)):
            return "已开启“只读模式”"
        s = self.current_session
        if s is not None and getattr(getattr(s, "profile", None), "is_prod", False):
            return f"连接“{s.profile.name}”被标记为生产环境，已自动只读"
        return ""

    def guard_write(self, parent=None, what: str = "写操作") -> bool:
        """写操作统一闸门：只读时拦下来并说清楚原因，返回 True 才允许继续。"""
        if not self.read_only_active:
            return True
        W.warning(parent or self.root,
                  f"{self.read_only_reason()}，{what}已被阻止。\n\n"
                  "确实需要改数据时：\n"
                  "· 取消工具栏的“只读”勾选；\n"
                  "· 若是因为生产环境自动只读，请在连接设置里改环境标识，\n"
                  "　或在设置里关闭“生产环境自动只读”（不建议）。")
        return False

    def _on_conn_combo(self):
        name = self.conn_var.get()
        if name and name in self.sessions:
            self.current_session_name = name
            self._sync_read_only()
            self.refresh_schema()
            self._update_status()

    # ================================================================ 主体
    def _build_body(self):
        self.paned = tkttk.PanedWindow(self.root, orient="horizontal")
        self.paned.pack(fill=tk.BOTH, expand=True)
        self.schema_panel = SchemaPanel(self.paned, self)
        self.paned.add(self.schema_panel, weight=0)
        self.home = tb.Frame(self.paned, padding=24)
        self.paned.add(self.home, weight=1)
        self._build_home()
        if not bool(self.settings.get("schema_panel", True)):
            self.paned.forget(self.schema_panel)

    def _build_home(self):
        tb.Label(self.home, text=f"欢迎使用 {APP_NAME}",
                 font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        self.conn_card = tb.Label(self.home, text="当前状态：未连接数据库",
                                  bootstyle="secondary", wraplength=900, justify="left")
        self.conn_card.pack(anchor="w", pady=(6, 14))

        grid = tb.Frame(self.home)
        grid.pack(anchor="w")
        buttons = [
            ("🔌 连接数据库", self.open_connection_manager, "primary"),
            ("📝 新建查询窗口", lambda: self.new_query_window(), "success"),
            ("🪄 查询向导（不用写 SQL）", self.open_wizard, "info"),
            ("📊 浏览数据表", lambda: self.new_table_window(), "success-outline"),
            ("🛟 备份与还原", self.open_backup_center, "warning"),
            ("📖 使用说明", self.show_help, "secondary-outline"),
        ]
        for i, (text, cmd, style) in enumerate(buttons):
            b = tb.Button(grid, text=text, command=cmd, bootstyle=style, width=26)
            b.grid(row=i // 3, column=i % 3, padx=(0, 12), pady=6, sticky="w")

        tb.Label(self.home, text="最近执行过的 SQL（双击在查询窗口打开）",
                 bootstyle="primary").pack(anchor="w", pady=(26, 6))
        self.recent = tb.Treeview(self.home, columns=("时间", "类型", "语句"), show="headings",
                                  height=8, bootstyle="primary")
        for c, w in zip(("时间", "类型", "语句"), (150, 90, 700)):
            self.recent.heading(c, text=c)
            self.recent.column(c, width=w, anchor="w")
        self.recent.pack(fill=tk.BOTH, expand=True)
        self.recent.bind("<Double-1>", lambda e: self._open_recent())

    def _open_recent(self):
        sel = self.recent.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.history.items):
            self.new_query_window(sql=self.history.items[idx].get("sql", ""))

    def refresh_home_recent(self):
        self.recent.delete(*self.recent.get_children())
        for i, item in enumerate(self.history.items[:30]):
            self.recent.insert("", "end", iid=str(i), values=(
                item.get("time", ""), item.get("tag", ""), U.clip_text(item.get("sql", ""), 160)))

    # ================================================================ 状态栏
    def _build_status(self):
        self.statusbar = W.StatusBar(self.root, parts=3, padding=(10, 4))
        self.statusbar.pack(fill=tk.X, side=tk.BOTTOM)
        self.statusbar.set(2, f"{APP_NAME} v{APP_VERSION}")
        self._update_status()

    def _update_status(self):
        if self.current_session_name:
            self.statusbar.set(1, f"已连接：{self.current_session_name}")
        else:
            self.statusbar.set(1, "未连接")
        # 状态栏左侧：环境标识 + 只读状态，连到生产库时一眼能看出来
        env = ""
        s = self.current_session
        if s is not None:
            label = getattr(s.profile, "env_label", "未标注")
            if label != "未标注":
                env = ("🔴 " if getattr(s.profile, "is_prod", False) else "🟢 ") + label + "　"
        self.statusbar.set(0, ("🔒 只读　" if self.read_only_active else "") + env + "就绪")
        try:
            self.root.title(
                f"{APP_NAME}  v{APP_VERSION}"
                + (f"　【{env.strip()}】" if env else "")
                + ("　🔒只读" if self.read_only_active else "")
            )
        except Exception:
            pass
        try:
            self.conn_card.configure(
                text=f"当前连接：{self.current_session_name or '（无）'}"
                     + (f"　·　{self.current_session.adapter.server_version}"
                        if self.current_session and getattr(self.current_session.adapter, "server_version", "") else "")
            )
        except Exception:
            pass

    # ================================================================ 快捷键
    def _bind_keys(self):
        r = self.root
        r.bind_all("<Control-n>", lambda e: self.new_query_window())
        r.bind_all("<Control-N>", lambda e: self.new_query_window())
        r.bind_all("<Control-t>", lambda e: self.new_table_window())
        r.bind_all("<Control-T>", lambda e: self.new_table_window())
        r.bind_all("<Control-g>", lambda e: self.open_wizard())
        r.bind_all("<Control-G>", lambda e: self.open_wizard())
        r.bind_all("<Control-b>", lambda e: self.open_backup_center())
        r.bind_all("<Control-B>", lambda e: self.open_backup_center())
        r.bind_all("<Control-r>", lambda e: self.refresh_schema())
        r.bind_all("<Control-R>", lambda e: self.refresh_schema())
        # 说明：Ctrl+O 留给编辑器“打开脚本”（各窗口自己绑定并 return "break"），
        # 全局不再占用，否则在编辑器里按 Ctrl+O 会同时弹出“打开脚本”和“运维概览”。
        # 运维概览 / 数据库档案统一用 Ctrl+Shift+O / Ctrl+Shift+D（Tk 里大写字母即 Shift）
        for seq in ("<Control-Shift-O>", "<Control-Shift-o>"):
            r.bind_all(seq, lambda e: self.open_ops())
        for seq in ("<Control-Shift-D>", "<Control-Shift-d>"):
            r.bind_all(seq, lambda e: self.open_snapshot())
        for seq in ("<Control-Shift-L>", "<Control-Shift-l>"):
            r.bind_all(seq, lambda e: self.open_template_library())

    def open_snapshot(self):
        """数据库档案：把库的结构写成一段文本，供内网离线求助时复制出去。"""
        if not self.current_session:
            W.warning(self.root, "请先连接数据库")
            return
        from .snapshot import SnapshotWindow

        for w in list(self.windows):
            if isinstance(w, SnapshotWindow):
                self.focus_window(w)
                return
        w = SnapshotWindow(self, self.current_session_name)
        self.focus_window(w)
        return w

    def open_ops(self):
        """运维概览：实例概览 / 慢 SQL / 表空间 / 权限自检（只读页面）。"""
        if not self.current_session:
            W.warning(self.root, "请先连接数据库")
            return
        from .ops import OpsWindow

        for w in list(self.windows):
            if isinstance(w, OpsWindow):
                self.focus_window(w)
                return
        w = OpsWindow(self, self.current_session_name)
        self.focus_window(w)
        return w

    # ================================================================ 会话
    @property
    def current_session(self) -> Optional[DbSession]:
        return self.sessions.get(self.current_session_name)

    def session_names(self) -> List[str]:
        return list(self.sessions.keys())

    def get_session(self, name: str = "") -> Optional[DbSession]:
        if not name:
            return self.current_session
        return self.sessions.get(name)

    def get_session_by_label(self, label: str) -> Optional[DbSession]:
        for s in self.sessions.values():
            if s.label == label:
                return s
        return None

    def test_connection(self, profile: ConnProfile) -> Tuple[bool, str]:
        def work(ev):
            s = create_session(profile)
            ver = getattr(s.adapter, "server_version", "") or ""
            user = getattr(s.adapter, "current_user", "") or ""
            s.close()
            return f"服务器版本 {ver}，当前用户 {user}"

        ok, res = W.run_async(self.root, work, title="正在连接", message=f"正在连接 {profile.name} …",
                              cancellable=False)
        return ok, (res if ok else str(res))

    def open_connection(self, name: str, parent=None, silent: bool = False) -> bool:
        parent = parent or self.root
        profile = self.conn_store.find(name)
        if not profile:
            if not silent:
                W.error(parent, f"找不到连接配置：{name}")
            return False
        if name in self.sessions:
            self.current_session_name = name
            self._sync_conn_combo()
            self.refresh_schema()
            self._update_status()
            return True

        def work(ev):
            return create_session(profile)

        ok, res = W.run_async(parent, work, title="正在连接",
                              message=f"正在连接 {profile.display_dsn()} …", cancellable=False)
        if not ok:
            if not silent:
                W.error(parent, f"连接失败：\n{res}")
            return False
        self.sessions[name] = res
        self.current_session_name = name
        self._sync_conn_combo()
        self._sync_read_only()
        self.refresh_schema()
        self._update_status()
        if not silent:
            if getattr(profile, "is_prod", False) and bool(self.settings.get("env_read_only", True)):
                W.toast(self.root, f"已连接：{name}　·　生产环境，已自动开启只读保护", "warning", 4000)
            else:
                W.toast(self.root, f"已连接：{name}", "success")
        return True

    def _sync_conn_combo(self):
        names = self.session_names()
        self.conn_combo.configure(values=names or ["(未连接)"])
        if self.current_session_name:
            self.conn_var.set(self.current_session_name)
        else:
            self.conn_var.set("(未连接)")
        # 只更新“可选连接列表”，绝不替子窗口改当前连接：
        # 多库并行时，在主窗口切换连接不应该把别人正在用的库一起切走
        for w in self.windows:
            try:
                w.session_combo.configure(values=names or ["(未连接)"])
                if w.session_name and w.session_name not in names:
                    # 该窗口用的连接已断开，退化成显示当前连接，避免下拉框空着
                    w.session_var.set(self.current_session_name or "")
                    w.session_name = self.current_session_name or ""
            except Exception:
                pass

    def disconnect_current(self):
        from ..core import perms as PM

        name = self.current_session_name
        if not name:
            return
        s = self.sessions.pop(name, None)
        if s:
            PM.drop(s)   # 清掉这个连接的权限探测缓存，下次重连重新体检
            try:
                s.close()
            except Exception:
                pass
        self.current_session_name = self.session_names()[0] if self.sessions else ""
        self._sync_conn_combo()
        self._sync_read_only()
        self.refresh_schema()
        self._update_status()
        W.toast(self.root, f"已断开 {name}", "info")

    def do_commit(self):
        s = self.current_session
        if not s:
            W.warning(self.root, "当前没有连接")
            return
        if not W.ask_yes(self.root, "提交（COMMIT）后改动永久生效，无法回滚。是否继续？", "提交"):
            return
        try:
            s.commit()
            W.toast(self.root, "已提交 COMMIT", "success")
        except Exception as e:  # noqa: BLE001
            W.error(self.root, f"提交失败：{e}")

    def do_rollback(self):
        s = self.current_session
        if not s:
            W.warning(self.root, "当前没有连接")
            return
        if not W.ask_yes(self.root, "回滚（ROLLBACK）会撤销所有未提交的改动。是否继续？", "回滚"):
            return
        try:
            s.rollback()
            W.toast(self.root, "已回滚 ROLLBACK", "success")
        except Exception as e:  # noqa: BLE001
            W.error(self.root, f"回滚失败：{e}")

    # ================================================================ 子窗口
    def register_window(self, win: BaseSubWindow):
        if win not in self.windows:
            self.windows.append(win)
        self._build_window_menu()

    def unregister_window(self, win: BaseSubWindow):
        if win in self.windows:
            self.windows.remove(win)
        self._build_window_menu()

    def focus_window(self, win: BaseSubWindow):
        try:
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            pass

    def cascade_windows(self):
        x, y = 60, 60
        for w in self.windows:
            try:
                w.geometry(f"+{x}+{y}")
                w.lift()
            except Exception:
                pass
            x += 32
            y += 32

    def tile_windows(self):
        wins = [w for w in self.windows if w.winfo_exists()]
        if not wins:
            return
        sw = self.root.winfo_screenwidth() - 60
        sh = self.root.winfo_screenheight() - 140
        cols = max(1, int(math.ceil(math.sqrt(len(wins)))))
        rows = max(1, int(math.ceil(len(wins) / cols)))
        w_w, w_h = max(360, sw // cols), max(260, sh // rows)
        for i, w in enumerate(wins):
            x = 30 + (i % cols) * w_w
            y = 40 + (i // cols) * w_h
            try:
                w.geometry(f"{w_w}x{w_h}+{x}+{y}")
                w.lift()
            except Exception:
                pass

    def close_all_windows(self):
        for w in list(self.windows):
            try:
                w._on_close()
            except Exception:
                pass

    def new_query_window(self, sql: str = "", session_name: str = "") -> QueryWindow:
        w = QueryWindow(self, sql=sql, session_name=session_name or self.current_session_name)
        self.focus_window(w)
        return w

    def new_table_window(self, schema: str = "", table: str = "", session_name: str = "") -> TableWindow:
        if not self.current_session and not session_name:
            W.warning(self.root, "请先连接数据库")
            return None
        w = TableWindow(self, schema=schema, table=table,
                        session_name=session_name or self.current_session_name)
        self.focus_window(w)
        return w

    # ================================================================ 对话框入口
    def open_connection_manager(self):
        from .dialogs import ConnectionManager

        ConnectionManager(self.root, self)

    def new_connection(self):
        from .dialogs import ConnectionDialog

        dlg = ConnectionDialog(self.root, self, None)
        self.root.wait_window(dlg)
        if dlg.saved:
            self.conn_store.add(dlg.saved)
            if dlg.connect_after_save:
                self.open_connection(dlg.saved.name)

    def open_wizard(self):
        from .builders import QueryWizard

        dlg = QueryWizard(self.root, self, session_name=self.current_session_name)
        self.root.wait_window(dlg)
        res = dlg.result or {}
        if res.get("sql"):
            w = self.new_query_window(sql=res["sql"], session_name=res.get("session", ""))
            if res.get("execute"):
                w.run_all()

    def open_backup_center(self):
        from .dialogs import BackupCenter

        BackupCenter(self.root, self)

    def open_template_library(self):
        """运维 SQL 模板库：只读诊断语句，选完填参数再看要不要跑。"""
        if not self.current_session:
            W.warning(self.root, "请先连接数据库")
            return
        from .templates import TemplateDialog

        # 对话框不是子窗口，不进 self.windows，单独记一个引用防重复打开
        old = getattr(self, "_template_dialog", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    old.focus_set()
                    return
            except Exception:
                pass
        self._template_dialog = TemplateDialog(self.root, self,
                                                session_name=self.current_session_name)

    def open_history(self):
        from .dialogs import HistoryDialog

        HistoryDialog(self.root, self, self.favorites,
                      on_pick=lambda sql: self.new_query_window(sql=sql))

    def open_settings(self):
        from .dialogs import SettingsDialog

        SettingsDialog(self.root, self)

    def open_table_info(self):
        from .dialogs import TableInfoDialog

        session = self.current_session
        if not session:
            W.warning(self.root, "请先连接数据库")
            return
        schema = self.schema_panel.schema_var.get() or session.default_schema
        sel = self.schema_panel.tree.selection()
        table = sel[0] if sel else ""
        if not table:
            W.warning(self.root, "请先在左侧选中一张表")
            return
        TableInfoDialog(self.root, self, self.current_session_name, schema, table)

    def show_help(self):
        from .dialogs import HELP_TEXT

        W.show_text(self.root, "使用说明", HELP_TEXT, width=900, height=660, mono=False)

    def show_about(self):
        from .dialogs import AboutDialog

        AboutDialog(self.root, self)

    def _open_script_global(self):
        from tkinter import filedialog

        path = filedialog.askopenfilename(parent=self.root, filetypes=[("SQL 脚本", "*.sql"),
                                                                       ("文本文件", "*.txt")])
        if path:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                self.new_query_window(sql=f.read())

    def _export_global(self):
        W.info(self.root, "请在查询窗口或数据窗口中执行查询后，点“导出 Excel”按钮。")

    # ================================================================ 主题 / 设置
    def editor_colors(self) -> dict:
        return TH.editor_colors(self.root.style)

    def grid_colors(self) -> dict:
        return TH.grid_colors(self.root.style)

    def switch_theme(self, name: str):
        self.settings.set("theme", name)
        self.apply_theme()

    def apply_theme(self):
        TH.set_theme(self.root, self.settings.get("theme", TH.DEFAULT_THEME))
        TH.apply_font(self.root, self.settings.get("font_family", "Microsoft YaHei UI"),
                      int(self.settings.get("font_size", 10)))
        c = TH.grid_colors(self.root.style)
        self._rebuild_menu_colors(c)
        for w in self.windows:
            try:
                w.apply_theme()
            except Exception:
                pass

    def _rebuild_menu_colors(self, c: dict):
        try:
            self._build_menu()
        except Exception:
            pass

    def apply_settings(self, values: dict):
        old_dir = str(self.settings.backup_dir)
        self.settings.update(values)
        if str(self.settings.backup_dir) != old_dir:
            self.backup_manager = _make_backup_manager(self.settings)
        self.apply_theme()
        self.auto_commit_var.set(bool(self.settings.get("auto_commit", False)))
        if hasattr(self, "read_only_var"):
            self.read_only_var.set(bool(self.settings.get("read_only", False)))
        self.refresh_schema()

    def toggle_schema_panel(self):
        try:
            tabs = self.paned.panes()
        except Exception:
            tabs = []
        if self.schema_panel.winfo_ismapped():
            self.paned.forget(self.schema_panel)
            self.settings.set("schema_panel", False)
        else:
            self.paned.insert(0, self.schema_panel, weight=0)
            self.settings.set("schema_panel", True)
            self.refresh_schema()

    def refresh_schema(self):
        try:
            self.schema_panel.refresh()
        except Exception:
            pass
        self.refresh_home_recent()
        self._update_status()
        self._build_window_menu()

    # ================================================================ 启动 / 关闭
    def _startup(self):
        self._sync_conn_combo()
        self.refresh_home_recent()
        demo = next((p for p in self.conn_store.profiles if p.demo), None)
        if demo and not self.settings.get("demo_seen", False):
            if self.open_connection(demo.name, parent=self.root, silent=True):
                self.settings.set("demo_seen", True)
                W.toast(self.root, "已连接内置演示数据库，可立即体验；点“连接管理”连真实 Oracle。",
                        "info", 6000)
        self._update_status()

    def on_close(self):
        try:
            self.settings.set("window_geometry", self.root.geometry())
        except Exception:
            pass
        if self.windows:
            if not W.ask_yes(self.root, f"还有 {len(self.windows)} 个子窗口打开，确定退出吗？", "退出"):
                return
        for s in self.sessions.values():
            try:
                s.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def _make_backup_manager(settings: Settings):
    from ..core.backup import BackupManager

    return BackupManager(settings.backup_dir)


def _make_icon(path: Path):
    """用 PIL 生成一个简单的程序图标（蓝色圆角方块 + 数据库图形）。"""
    try:
        from PIL import Image, ImageDraw

        size = 128
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, size - 4, size - 4], radius=26, fill=(38, 92, 168, 255))
        # 三个“数据盘”
        for i, top in enumerate((30, 58, 86)):
            d.ellipse([34, top, 94, top + 18], fill=(240, 246, 255, 255), outline=(20, 60, 120, 255), width=3)
        d.ellipse([34, 26, 94, 44], fill=(255, 255, 255, 255))
        img.save(path)
    except Exception:
        pass
