"""引导式 SQL 生成器。

把用户的“表名 + 列 + 条件 + 排序 + 行数”翻译成可读的 SQL 语句，
用户可以先看语句、再修改、最后执行。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .models import Condition, ColumnMeta, OrderTerm, QuerySpec
from .validator import validate, is_null_token

# 运算符显示名 -> SQL 片段
OPERATORS = {
    "=": "等于",
    "<>": "不等于",
    ">": "大于",
    ">=": "大于等于",
    "<": "小于",
    "<=": "小于等于",
    "LIKE": "包含（模糊匹配）",
    "NOT_LIKE": "不包含",
    "STARTS": "开头是",
    "ENDS": "结尾是",
    "IN": "属于列表",
    "NOT_IN": "不属于列表",
    "IS_NULL": "为空 (NULL)",
    "IS_NOT_NULL": "不为空 (NOT NULL)",
    "BETWEEN": "介于两者之间",
}

# 不需要输入值的运算符
NO_VALUE_OPS = {"IS_NULL", "IS_NOT_NULL"}
# 需要两个值的运算符
TWO_VALUE_OPS = {"BETWEEN"}

TEXT_LIKE_TYPES = ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR", "CLOB", "NCLOB", "LONG")


def operator_label(op: str) -> str:
    return OPERATORS.get(op, op)


def operator_keys() -> List[str]:
    return list(OPERATORS.keys())


# ---------------------------------------------------------------- 字面量
def escape_string(s: str) -> str:
    return str(s).replace("'", "''")


def quote_ident(name: str, always: bool = False) -> str:
    """标识符加引号。默认只在必要时加，保证生成的 SQL 易读。"""
    name = (name or "").strip()
    if not name:
        return name
    if not always and re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", name) and name.upper() == name:
        return name
    if not always and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_$#]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def full_table_name(schema: str, table: str, always: bool = False) -> str:
    schema = (schema or "").strip()
    table = (table or "").strip()
    if schema:
        return f"{quote_ident(schema, always)}.{quote_ident(table, always)}"
    return quote_ident(table, always)


def literal(value: Any, data_type: str = "VARCHAR2", dialect: str = "oracle") -> str:
    """把 Python 值渲染成 SQL 字面量。"""
    if value is None:
        return "NULL"
    t = (data_type or "VARCHAR2").upper()

    if isinstance(value, str) and value.strip().upper() in ("SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE"):
        return value.strip().upper()

    if hasattr(value, "strftime"):
        if dialect == "oracle":
            if getattr(value, "microsecond", 0):
                return f"TO_TIMESTAMP('{value.strftime('%Y-%m-%d %H:%M:%S.%f')}','YYYY-MM-DD HH24:MI:SS.FF')"
            return f"TO_DATE('{value.strftime('%Y-%m-%d %H:%M:%S')}','YYYY-MM-DD HH24:MI:SS')"
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, (int, float)):
        return str(value)

    from decimal import Decimal

    if isinstance(value, Decimal):
        return _format_decimal(value)

    s = str(value)
    if t in ("NUMBER", "FLOAT", "INTEGER", "INT", "DECIMAL", "NUMERIC", "REAL",
             "DOUBLE PRECISION", "BINARY_FLOAT", "BINARY_DOUBLE"):
        if re.fullmatch(r"-?\d+(\.\d+)?", s.strip()):
            return s.strip()
    return "'" + escape_string(s) + "'"


def _format_decimal(v) -> str:
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ---------------------------------------------------------------- 条件
def render_condition(cond: Condition, col_type: str = "VARCHAR2", dialect: str = "oracle") -> Optional[str]:
    if cond is None or not cond.column:
        return None
    col = quote_ident(cond.column)
    op = cond.op.upper().replace(" ", "_")

    if op in NO_VALUE_OPS:
        return f"{col} IS {'NULL' if op == 'IS_NULL' else 'NOT NULL'}"

    if op in ("IN", "NOT_IN"):
        items = [x.strip() for x in str(cond.value).split(",") if x.strip() != ""]
        if not items:
            return None
        rendered = ", ".join(literal(_typed(items[0], col_type), col_type, dialect) for _ in [0])
        vals = []
        for it in items:
            ok, py, _ = validate(col_type, it)
            vals.append(literal(py if ok else it, col_type, dialect))
        rendered = ", ".join(vals)
        return f"{col} {'IN' if op == 'IN' else 'NOT IN'} ({rendered})"

    if op == "BETWEEN":
        ok1, v1, _ = validate(col_type, cond.value)
        ok2, v2, _ = validate(col_type, cond.value2)
        a = literal(v1 if ok1 else cond.value, col_type, dialect)
        b = literal(v2 if ok2 else cond.value2, col_type, dialect)
        return f"{col} BETWEEN {a} AND {b}"

    if op in ("LIKE", "NOT_LIKE"):
        ok, py, _ = validate("VARCHAR2", cond.value)
        txt = py if ok else cond.value
        return f"{col} {'LIKE' if op == 'LIKE' else 'NOT LIKE'} '%{escape_string(str(txt))}%'"

    if op == "STARTS":
        ok, py, _ = validate("VARCHAR2", cond.value)
        return f"{col} LIKE '{escape_string(str(py if ok else cond.value))}%'"

    if op == "ENDS":
        ok, py, _ = validate("VARCHAR2", cond.value)
        return f"{col} LIKE '%{escape_string(str(py if ok else cond.value))}'"

    # 比较运算符
    ok, py, _ = validate(col_type, cond.value)
    if ok and py is None and op in ("=", "<>"):
        return f"{col} IS {'NULL' if op == '=' else 'NOT NULL'}"
    val = literal(py if ok else cond.value, col_type, dialect)
    return f"{col} {op} {val}"


def _typed(x, col_type):
    ok, py, _ = validate(col_type, x)
    return py if ok else x


def render_where(conditions: List[Condition], col_types: Dict[str, str], dialect: str = "oracle") -> str:
    parts: List[str] = []
    for idx, c in enumerate(conditions or []):
        if not c.column:
            continue
        seg = render_condition(c, col_types.get(c.column.upper(), "VARCHAR2"), dialect)
        if not seg:
            continue
        logic = (c.logic or "AND").upper()
        if idx == 0 or logic not in ("AND", "OR"):
            parts.append(seg)
        else:
            parts.append(f"{logic} {seg}")
    return " ".join(parts) if parts else ""


# ---------------------------------------------------------------- SELECT
def build_select(spec: QuerySpec, col_types: Optional[Dict[str, str]] = None,
                 dialect: str = "oracle") -> str:
    col_types = col_types or {}
    table = full_table_name(spec.schema, spec.table)
    sel = "*"
    if spec.columns:
        sel = ", ".join(quote_ident(c) for c in spec.columns)
    if spec.distinct:
        sel = "DISTINCT " + sel

    where = render_where(spec.conditions, col_types, dialect)
    group = ""
    if spec.group_by:
        group = "\nGROUP BY " + ", ".join(quote_ident(c) for c in spec.group_by)
    having = ""
    if spec.having and spec.group_by:
        having = "\nHAVING " + spec.having

    order = ""
    if spec.order_by:
        terms = []
        for t in spec.order_by:
            if not t.column:
                continue
            terms.append(f"{quote_ident(t.column)} {t.direction}")
        if terms:
            order = "\nORDER BY " + ", ".join(terms)

    sql = f"SELECT {sel}\nFROM   {table}"
    if where:
        sql += f"\nWHERE  {where}"
    if group:
        sql += group
    if having:
        sql += having
    if order:
        sql += order
    return sql + ";"


def strip_row_limit(sql: str, dialect: str = "oracle") -> str:
    """去掉已有的行数限制（ROWNUM 包装 / FETCH FIRST / LIMIT）。

    探查与“导出全部”必须基于完整的筛选结果，否则只会在前 N 行里查，
    查重复、值分布、跨表钻取都会得出错误结论。
    """
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return s
    if dialect == "oracle":
        m = re.search(r"^\s*SELECT\s+\*\s*FROM\s*\((?P<inner>.*)\)\s*WHERE\s+ROWNUM\s*<=\s*\d+\s*$",
                      s, re.IGNORECASE | re.DOTALL)
        if m:
            return strip_row_limit(m.group("inner"), dialect)
        s = re.sub(r"\s*FETCH\s+FIRST\s+\d+\s+ROWS?\s+ONLY\s*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*OFFSET\s+\d+\s+ROWS\s*$", "", s, flags=re.IGNORECASE)
        return s.strip().rstrip(";").strip()
    s = re.sub(r"\s*LIMIT\s+\d+(\s*,\s*\d+|\s+OFFSET\s+\d+)?\s*$", "", s, flags=re.IGNORECASE)
    return s.strip().rstrip(";").strip()


def apply_row_limit(sql: str, limit: Optional[int], dialect: str = "oracle") -> str:
    """给 SQL 加行数限制（Oracle 用 ROWNUM 包装，兼容 11g 及以上）。

    先剥掉已有限制再重新套，保证重复调用不会层层嵌套。
    """
    base = strip_row_limit(sql, dialect)
    if not limit or limit <= 0:
        return base + ";"
    if dialect == "oracle":
        return f"SELECT *\nFROM   (\n{_indent(base)}\n)\nWHERE  ROWNUM <= {int(limit)};"
    return f"{base}\nLIMIT {int(limit)};"


def _indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def build_count_sql(spec: QuerySpec, col_types: Optional[Dict[str, str]] = None,
                    dialect: str = "oracle") -> str:
    table = full_table_name(spec.schema, spec.table)
    where = render_where(spec.conditions, col_types or {}, dialect)
    sql = f"SELECT COUNT(*) AS 行数\nFROM   {table}"
    if where:
        sql += f"\nWHERE  {where}"
    return sql + ";"


# ---------------------------------------------------------------- DML
def build_insert(schema: str, table: str, values: Dict[str, Any],
                 col_types: Dict[str, str], dialect: str = "oracle",
                 include_nulls: bool = True) -> str:
    cols = []
    vals = []
    for name, v in values.items():
        if v is None and not include_nulls:
            continue
        if isinstance(v, str) and v.strip() == "" and v != "":
            pass
        cols.append(quote_ident(name))
        vals.append(literal(v, col_types.get(name.upper(), "VARCHAR2"), dialect))
    if not cols:
        return "-- 没有需要插入的字段"
    sql = f"INSERT INTO {full_table_name(schema, table)} (\n  " + ",\n  ".join(cols) + "\n) VALUES (\n  "
    sql += ",\n  ".join(vals) + "\n);"
    return sql


def build_update(schema: str, table: str, values: Dict[str, Any], conditions: List[Condition],
                 col_types: Dict[str, str], dialect: str = "oracle") -> str:
    if not conditions:
        raise ValueError("更新语句必须有筛选条件，拒绝生成会修改整表的语句。")
    sets = []
    for name, v in values.items():
        if isinstance(v, str) and v.startswith("__UNCHANGED__"):
            continue
        sets.append(f"{quote_ident(name)} = {literal(v, col_types.get(name.upper(), 'VARCHAR2'), dialect)}")
    if not sets:
        raise ValueError("没有要修改的字段。")
    where = render_where(conditions, col_types, dialect)
    if not where:
        raise ValueError("更新语句必须有筛选条件。")
    sql = f"UPDATE {full_table_name(schema, table)}\nSET    " + ",\n       ".join(sets)
    sql += f"\nWHERE  {where};"
    return sql


def build_delete(schema: str, table: str, conditions: List[Condition],
                 col_types: Dict[str, str], dialect: str = "oracle") -> str:
    where = render_where(conditions, col_types, dialect)
    if not where:
        raise ValueError("删除语句必须有筛选条件，拒绝生成会清空整表的语句。")
    return f"DELETE FROM {full_table_name(schema, table)}\nWHERE  {where};"


def build_select_for_conditions(schema: str, table: str, conditions: List[Condition],
                                col_types: Dict[str, str], dialect: str = "oracle",
                                with_rowid: bool = True, alias: str = "t") -> str:
    """生成用于预览/备份的 SELECT（可带 ROWID，便于精确还原）。"""
    table_sql = full_table_name(schema, table)
    if with_rowid and dialect == "oracle":
        table_sql += f" {alias}"
        cols = f"ROWID AS ROWID__, {alias}.*"
    else:
        cols = "*"
    where = render_where(conditions, col_types, dialect)
    sql = f"SELECT {cols}\nFROM   {table_sql}"
    if where:
        sql += f"\nWHERE  {where}"
    return sql + ";"


# ---------------------------------------------------------------- 简单解析
_IDENT = r'(?:"[^"]+"|[A-Za-z][A-Za-z0-9_$#]*)'
_TABLE_RE = re.compile(
    r"\bFROM\s+(" + _IDENT + r"(?:\s*\.\s*" + _IDENT + r")?)", re.IGNORECASE
)
_UPDATE_RE = re.compile(r"\bUPDATE\s+(" + _IDENT + r"(?:\s*\.\s*" + _IDENT + r")?)", re.IGNORECASE)
_INTO_RE = re.compile(r"\bINSERT\s+INTO\s+(" + _IDENT + r"(?:\s*\.\s*" + _IDENT + r")?)", re.IGNORECASE)
_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+(" + _IDENT + r"(?:\s*\.\s*" + _IDENT + r")?)", re.IGNORECASE)


def strip_quotes(name: str) -> str:
    n = (name or "").strip()
    if n.startswith('"') and n.endswith('"') and len(n) > 1:
        return n[1:-1]
    return n


def split_table_name(full: str) -> Tuple[str, str]:
    """'SCOTT.EMP' / '"SCOTT"."EMP"' -> ('SCOTT','EMP')"""
    full = (full or "").strip()
    if "." in full:
        parts = re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", full)
        if len(parts) >= 2:
            return strip_quotes(parts[0]), strip_quotes(parts[-1])
    return "", strip_quotes(full)


def detect_table(sql: str) -> Tuple[str, str]:
    """从简单语句中识别 (schema, table)。"""
    s = (sql or "").strip()
    m = _UPDATE_RE.search(s) or _INTO_RE.search(s) or _DELETE_RE.search(s) or _TABLE_RE.search(s)
    if m:
        return split_table_name(m.group(1))
    return "", ""


def extract_where(sql: str) -> str:
    """抽取简单语句的 WHERE 子句（不含分号）。

    必须跳过字符串字面量与括号内部，否则
    ``WHERE ID IN (SELECT ID FROM X ORDER BY ID)``
    会被截断成 ``ID IN (SELECT ID FROM X`` —— 括号不闭合，拼进备份 SQL 必报 ORA-00936。
    """
    s = (sql or "").strip().rstrip(";").strip()
    start = re.search(r"\bWHERE\b", s, re.IGNORECASE)
    if not start:
        return ""
    i = start.end()
    n = len(s)
    depth = 0
    j = i
    while j < n:
        ch = s[j]
        if ch == "'":                      # 字符串字面量：整段跳过
            j += 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            continue
        if ch == "(":
            depth += 1
            j += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            j += 1
            continue
        if depth == 0:
            # 只在顶层认 ORDER BY / GROUP BY / HAVING / CONNECT BY / MODEL 这些收尾子句
            m = re.match(r"\b(ORDER\s+BY|GROUP\s+BY|HAVING|CONNECT\s+BY|START\s+WITH|MODEL|FOR\s+UPDATE)\b",
                         s[j:], re.IGNORECASE)
            if m:
                break
        j += 1
    return s[i:j].strip()


_ALIAS_RE_TEMPLATE = r"\b(?:UPDATE|DELETE\s+FROM|FROM)\s+" + _IDENT + \
                     r"(?:\s*\.\s*" + _IDENT + r")?\s+(?:AS\s+)?" \
                     r"(?P<alias>" + _IDENT + r")\b"

# 表名后紧跟的标识符若是这些关键字，说明它是子句而不是别名
_ALIAS_STOPWORDS = {
    "SET", "WHERE", "ORDER", "GROUP", "HAVING", "RETURNING", "INTO", "VALUES",
    "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "JOIN", "ON", "USING", "PARTITION",
    "SAMPLE", "CONNECT", "START", "MODEL", "PIVOT", "UNPIVOT", "AS", "AND", "OR",
}


def extract_table_alias(sql: str) -> str:
    """抽取 DML / 简单 SELECT 中主表的别名（没有别名则返回空串）。

    备份用的 capture SQL 必须沿用原语句的别名，否则
    ``UPDATE EMP E SET ... WHERE E.DEPTNO=10`` 抽出的条件引用了 ``E``，
    而 capture 写 ``FROM EMP t`` 就会 ORA-00904。
    """
    s = (sql or "").strip()
    m = re.search(_ALIAS_RE_TEMPLATE, s, re.IGNORECASE)
    if not m:
        return ""
    alias = strip_quotes(m.group("alias"))
    if alias.upper() in _ALIAS_STOPWORDS:
        return ""
    return alias


def is_query_sql(sql: str) -> bool:
    s = (sql or "").strip().lstrip("(").upper()
    return s.startswith("SELECT") or s.startswith("WITH")


def statement_kind(sql: str) -> str:
    s = (sql or "").strip().upper()
    if s.startswith("SELECT") or s.startswith("WITH"):
        return "SELECT"
    if s.startswith("INSERT"):
        return "INSERT"
    if s.startswith("UPDATE"):
        return "UPDATE"
    if s.startswith("DELETE"):
        return "DELETE"
    if s.startswith("MERGE"):
        return "MERGE"
    if s.startswith("BEGIN") or s.startswith("DECLARE"):
        return "PLSQL"
    if re.match(r"^(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|RENAME|COMMENT)\b", s):
        return "DDL"
    return "OTHER"


def is_dml(kind: str) -> bool:
    return kind in ("INSERT", "UPDATE", "DELETE", "MERGE")


def split_statements(sql: str) -> List[str]:
    """按分号拆分多条语句，忽略字符串与注释中的分号。

    PL/SQL 块（BEGIN / DECLARE 开头）内部的分号不参与拆分：
    否则一个 ``BEGIN ...; END;`` 块会被切成好几段，每段单独拿去执行必报错。
    块以 ``END;``（或 SQL*Plus 风格的独占一行 ``/``）结束。
    """
    out: List[str] = []
    buf: List[str] = []
    i, n = 0, len(sql or "")
    in_str = False
    in_line_comment = False
    in_block_comment = False
    plsql_depth = 0          # >0 表示正在 PL/SQL 块内部
    block_started = False    # 当前语句是否已确认为 PL/SQL 块
    expect_begin = False     # DECLARE 块：后续那个 BEGIN 不算嵌套

    def end_statement():
        """收尾当前语句，并吞掉其后 SQL*Plus 风格的独占一行 “/”。"""
        nonlocal block_started, plsql_depth, expect_begin
        piece = "".join(buf).strip()
        buf.clear()
        block_started = False
        plsql_depth = 0
        expect_begin = False
        if piece:
            out.append(_normalize_plsql(piece) if _is_plsql(piece) else piece)
        j = i
        while j < n and sql[j] in " \t\r\n":
            j += 1
        if j < n and sql[j] == "/":
            j += 1
            while j < n and sql[j] not in "\n":
                j += 1
        return j

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append("/")
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
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
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue

        # ---- PL/SQL 块识别（只在语句开头判定一次） ----
        if not block_started and not "".join(buf).strip():
            m_head = re.match(r"\s*(BEGIN|DECLARE)\b", sql[i:], re.IGNORECASE)
            if m_head:
                block_started = True
                plsql_depth = 1
                # DECLARE 块里那个 BEGIN 只是块的正式开始，不算嵌套
                expect_begin = m_head.group(1).upper() == "DECLARE"
                # 头部关键字在这里吃掉，否则下面又会被当成一层嵌套 BEGIN
                buf.append(m_head.group(0))
                i += len(m_head.group(0))
                continue
        if plsql_depth:
            # END IF / END LOOP / END CASE 不是块结束，不能减深度
            m_end = re.match(r"\bEND\b(?!\s+(?:IF|LOOP|CASE)\b)", sql[i:], re.IGNORECASE)
            m_begin = re.match(r"\bBEGIN\b", sql[i:], re.IGNORECASE)
            if m_end:
                plsql_depth -= 1
                buf.append(m_end.group(0))
                i += len(m_end.group(0))
                continue
            if m_begin:
                if expect_begin:
                    expect_begin = False
                else:
                    plsql_depth += 1
                buf.append(m_begin.group(0))
                i += len(m_begin.group(0))
                continue
            if ch == ";" and plsql_depth <= 0:
                # 块的 END; 到此结束，后面可能还跟着 SQL*Plus 的 "/"
                buf.append(";")
                i += 1
                i = end_statement()
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == ";":
            i += 1
            i = end_statement()
            continue
        buf.append(ch)
        i += 1
    if buf:
        end_statement()
    return out


def _is_plsql(stmt: str) -> bool:
    return bool(re.match(r"\s*(BEGIN|DECLARE)\b", stmt or "", re.IGNORECASE))


def _normalize_plsql(stmt: str) -> str:
    """PL/SQL 块规范化：去掉结尾的 "/"，并保证以 END; 收尾。

    ``BEGIN ... END`` 少了分号，Oracle 会报 PLS-00113（END 缺少分号）。
    """
    s = (stmt or "").strip()
    while s.endswith("/"):
        s = s[:-1].rstrip()
    s = s.rstrip().rstrip(";").strip()
    return s + ";" if s else s


def prepare_statement(sql: str) -> str:
    """把单条语句整理成可以直接交给驱动执行的样子。

    · 普通语句：去掉结尾分号（驱动不允许带分号）
    · PL/SQL 块：去掉 SQL*Plus 的 "/"，并补上 END 后的分号
    """
    s = (sql or "").strip()
    if not s:
        return s
    if _is_plsql(s):
        return _normalize_plsql(s)
    return s.rstrip(";").strip()
