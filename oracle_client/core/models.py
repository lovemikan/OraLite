"""核心数据模型。

只描述数据结构，不依赖任何界面库，方便单独测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- 连接配置
@dataclass
class ConnProfile:
    """一个数据库连接配置。"""

    name: str = "新建连接"
    host: str = "127.0.0.1"
    port: int = 1521
    service: str = ""           # 服务名（Service Name）
    sid: str = ""               # SID，与服务名二选一
    user: str = ""
    password: str = ""          # 落盘时为加密文本
    schema: str = ""            # 默认 schema（对象浏览器默认展开）
    role: str = "Normal"        # Normal / SYSDBA
    conn_string: str = ""       # 完整连接串（TNS 别名 / Easy Connect），填了优先用
    timeout: int = 10           # 连接超时（秒）
    thick: bool = False         # 使用 Oracle Instant Client（厚模式）
    lib_dir: str = ""           # Instant Client 目录
    demo: bool = False          # 内置演示库（无需 Oracle 即可体验）
    env: str = ""               # 环境标识：生产 / 预发 / 测试 / 开发，空=未标注
    note: str = ""

    ENV_CHOICES = ("未标注", "开发", "测试", "预发", "生产")

    @property
    def env_label(self) -> str:
        return (self.env or "").strip() or "未标注"

    @property
    def is_prod(self) -> bool:
        return (self.env or "").strip() == "生产"

    # ---------- 序列化 ----------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ConnProfile":
        known = {k: v for k, v in (d or {}).items() if k in ConnProfile.__dataclass_fields__}
        p = ConnProfile(**known)
        # 兼容旧版本的字段类型
        try:
            p.port = int(p.port or 1521)
        except (TypeError, ValueError):
            p.port = 1521
        try:
            p.timeout = int(p.timeout or 10)
        except (TypeError, ValueError):
            p.timeout = 10
        return p

    # ---------- 辅助 ----------
    def display_dsn(self) -> str:
        if self.demo:
            return "内置演示数据库（SQLite 内存库，仅供试用）"
        if self.conn_string.strip():
            return self.conn_string.strip()
        target = self.service.strip() or self.sid.strip()
        if not target:
            return f"{self.host}:{self.port}"
        kind = "服务名" if self.service.strip() else "SID"
        return f"{self.host}:{self.port}/{target}（{kind}）"

    def summary(self) -> str:
        return f"{self.name}  ·  {self.user or '(无用户)'}@{self.display_dsn()}"

    def make_dsn(self) -> str:
        """生成 oracledb 可识别的 DSN。"""
        if self.conn_string.strip():
            return self.conn_string.strip()
        host = (self.host or "127.0.0.1").strip()
        port = int(self.port or 1521)
        if self.service.strip():
            return f"{host}:{port}/{self.service.strip()}"
        if self.sid.strip():
            return f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port}))(CONNECT_DATA=(SID={self.sid.strip()})))"
        return f"{host}:{port}"


# ---------------------------------------------------------------- 元数据
@dataclass
class ColumnMeta:
    name: str
    data_type: str = "VARCHAR2"
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    nullable: bool = True
    default: Optional[str] = None
    comment: str = ""
    is_pk: bool = False
    position: int = 0

    @property
    def type_label(self) -> str:
        t = (self.data_type or "").upper()
        if t in ("NUMBER", "FLOAT", "DECIMAL", "NUMERIC"):
            if self.precision:
                if self.scale:
                    return f"{t}({self.precision},{self.scale})"
                return f"{t}({self.precision})"
            return t
        if t in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR"):
            return f"{t}({self.length})" if self.length else t
        return t

    @property
    def is_numeric(self) -> bool:
        return (self.data_type or "").upper() in (
            "NUMBER", "FLOAT", "INTEGER", "INT", "DECIMAL", "NUMERIC", "REAL", "DOUBLE PRECISION", "BINARY_FLOAT", "BINARY_DOUBLE"
        )

    @property
    def is_date(self) -> bool:
        t = (self.data_type or "").upper()
        return t.startswith("DATE") or t.startswith("TIMESTAMP")

    @property
    def is_text(self) -> bool:
        t = (self.data_type or "").upper()
        return t in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR", "CLOB", "NCLOB", "LONG", "TEXT")


@dataclass
class TableMeta:
    owner: str
    name: str
    object_type: str = "TABLE"          # TABLE / VIEW / SYNONYM
    num_rows: Optional[int] = None
    comment: str = ""
    last_analyzed: Optional[str] = None
    columns: Optional[List[ColumnMeta]] = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.name

    @property
    def quoted_full_name(self) -> str:
        if self.owner:
            return f'"{self.owner}"."{self.name}"'
        return f'"{self.name}"'

    @property
    def pk_columns(self) -> List[str]:
        if not self.columns:
            return []
        return [c.name for c in sorted(self.columns, key=lambda x: x.position) if c.is_pk]


# ---------------------------------------------------------------- 结果集
@dataclass
class QueryResult:
    columns: List[str] = field(default_factory=list)
    rows: List[tuple] = field(default_factory=list)
    elapsed: float = 0.0
    message: str = ""
    truncated: bool = False
    statement: str = ""
    kind: str = "SELECT"        # SELECT / DML / PLSQL / OTHER

    @property
    def rowcount(self) -> int:
        return len(self.rows)


# ---------------------------------------------------------------- SQL 构建
@dataclass
class Condition:
    """一个查询条件。"""

    column: str = ""
    op: str = "="               # = <> > >= < <= LIKE NOT_LIKE IN NOT_IN IS_NULL IS_NOT_NULL BETWEEN
    value: str = ""
    value2: str = ""
    logic: str = "AND"          # 与上一条件的关系

    def is_empty(self) -> bool:
        if self.op in ("IS_NULL", "IS_NOT_NULL"):
            return not self.column
        if self.op == "BETWEEN":
            return not self.column or (not self.value and not self.value2)
        if self.op in ("IN", "NOT_IN"):
            return not self.column or not self.value.strip()
        return not self.column or not str(self.value).strip()


@dataclass
class OrderTerm:
    column: str = ""
    direction: str = "ASC"


@dataclass
class QuerySpec:
    """引导式生成 SELECT 语句所需的全部信息。"""

    schema: str = ""
    table: str = ""
    columns: List[str] = field(default_factory=list)     # 空表示 *
    distinct: bool = False
    conditions: List[Condition] = field(default_factory=list)
    order_by: List[OrderTerm] = field(default_factory=list)
    limit: Optional[int] = 200
    group_by: List[str] = field(default_factory=list)
    having: str = ""


@dataclass
class BackupRecord:
    id: str = ""
    created_at: str = ""
    connection: str = ""
    schema: str = ""
    table: str = ""
    operation: str = ""      # UPDATE / DELETE / INSERT / 自定义
    rowcount: int = 0
    sql: str = ""            # 触发备份的语句
    capture_sql: str = ""    # 抓取数据的 SELECT
    columns: List[str] = field(default_factory=list)
    pk_columns: List[str] = field(default_factory=list)
    has_rowid: bool = False
    rows: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    restored_at: str = ""
    size_bytes: int = 0
