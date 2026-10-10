"""应用配置与连接档案持久化。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .models import ConnProfile
from .secure import protect, unprotect, is_encrypted, encryption_note

# 版本号唯一来源是包的 __init__，这里只做转发，避免两处各自维护。
from .. import __app_name__ as APP_NAME  # noqa: E402
from .. import __version__ as APP_VERSION  # noqa: E402
from .. import __version_history__ as VERSION_HISTORY  # noqa: E402


def app_base_dir() -> Path:
    """安装/运行基准目录：打包成 exe 后为 exe 所在目录，开发时为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_portable() -> bool:
    """便携模式：数据随程序走，不写 %APPDATA%。

    - 打包后的单文件 exe：默认便携（随身 U 盘场景）
    - 源码运行：项目根目录存在 portable.ini 时便携
    - 始终可用环境变量 ORACLE_CLIENT_HOME 强制指定数据目录
    """
    if getattr(sys, "frozen", False):
        return True
    try:
        return (app_base_dir() / "portable.ini").exists()
    except Exception:
        return False


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """用户数据目录（设置、连接、历史、备份）。

    优先级：环境变量 ORACLE_CLIENT_HOME > 便携目录 > %APPDATA%
    """
    override = os.environ.get("ORACLE_CLIENT_HOME")
    candidates: List[Path] = []
    if override:
        candidates.append(Path(override))
    if is_portable():
        candidates.append(app_base_dir() / "UserData")
        candidates.append(app_base_dir())
    appdata = os.environ.get("APPDATA") or str(Path.home())
    candidates.append(Path(appdata) / "OracleClient")
    candidates.append(Path(tempfile.gettempdir()) / "OracleClientData")

    for base in candidates:
        if _writable(base):
            return base
    return candidates[-1]


def default_backup_dir() -> Path:
    base = data_dir()
    p = base / "备份" if is_portable() else Path.home() / "Oracle客户端备份"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = base / "backups"
    return p


# ---------------------------------------------------------------- 设置
DEFAULTS = {
    "theme": "flatly",               # ttkbootstrap 主题名
    "font_family": "Microsoft YaHei UI",
    "font_size": 10,
    "fetch_rows": 200,               # 单次取数上限
    "max_grid_rows": 20000,          # 网格最多渲染行数（性能保护）
    "auto_commit": False,
    "confirm_dml": True,             # 写操作前弹出确认
    "read_only": False,              # 只读模式：禁止一切写操作
    "env_read_only": True,           # 标记为“生产”的连接自动进入只读
    "auto_backup": True,             # 写操作默认勾选备份
    "backup_dir": "",                # 空 = 默认目录
    "editor_wrap": False,
    "show_row_number": True,
    "null_display": "",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "recent_files": [],
    "window_geometry": "",
    "schema_panel": True,
    "schemas_loaded": [],
    "demo_seen": False,
}


class Settings:
    def __init__(self):
        self._path = data_dir() / "settings.json"
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data.update(json.load(f) or {})
        except Exception:
            pass

    def save(self):
        with self._lock:
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def get(self, key: str, default=None):
        if key in self._data:
            return self._data[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self.save()

    def update(self, mapping: dict):
        self._data.update(mapping)
        self.save()

    @property
    def backup_dir(self) -> Path:
        d = self.get("backup_dir") or ""
        p = Path(d) if d else default_backup_dir()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            p = data_dir() / "backups"
            p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def as_dict(self) -> dict:
        return dict(self._data)


# ---------------------------------------------------------------- 连接档案
class ConnectionStore:
    def __init__(self):
        self._path = data_dir() / "connections.json"
        self.profiles: List[ConnProfile] = []
        self.load()

    def load(self):
        self.profiles = []
        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or []
                for item in raw:
                    p = ConnProfile.from_dict(item)
                    if is_encrypted(p.password):
                        p.password = unprotect(p.password)
                    self.profiles.append(p)
        except Exception:
            pass
        if not self.profiles:
            self.profiles.append(_demo_profile())

    def save(self):
        data = []
        for p in self.profiles:
            d = p.to_dict()
            d["password"] = protect(p.password or "")
            data.append(d)
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, profile: ConnProfile):
        self.profiles.append(profile)
        self.save()

    def remove(self, name: str):
        self.profiles = [p for p in self.profiles if p.name != name]
        self.save()

    def find(self, name: str) -> Optional[ConnProfile]:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def names(self) -> List[str]:
        return [p.name for p in self.profiles]


def _demo_profile() -> ConnProfile:
    p = ConnProfile()
    p.name = "演示数据库（无需 Oracle）"
    p.demo = True
    p.user = "DEMO"
    p.note = "内置 SQLite 演示库，用于在没有 Oracle 环境时体验本软件的所有功能。"
    return p


# ---------------------------------------------------------------- 历史 / 收藏
class SqlStore:
    """SQL 历史与收藏。"""

    def __init__(self, filename: str, limit: int = 300):
        self._path = data_dir() / filename
        self.limit = limit
        self.items: List[dict] = []
        self.load()

    def load(self):
        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    self.items = list(json.load(f) or [])
        except Exception:
            self.items = []

    def save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self.items[: self.limit], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, sql: str, tag: str = "", connection: str = ""):
        sql = (sql or "").strip()
        if not sql:
            return
        # 去掉重复项
        self.items = [i for i in self.items if i.get("sql") != sql]
        from datetime import datetime

        self.items.insert(
            0,
            {
                "sql": sql,
                "tag": tag,
                "connection": connection,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.save()

    def clear(self):
        self.items = []
        self.save()

    def remove(self, sql: str):
        self.items = [i for i in self.items if i.get("sql") != sql]
        self.save()


def encryption_hint() -> str:
    return encryption_note()
