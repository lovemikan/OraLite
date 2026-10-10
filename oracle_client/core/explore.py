"""数据探查：把“拿一组值去做什么”翻译成 SQL。

面向现场核查/对账场景的三个高频动作：

  1. 查重复值 —— 某（几）列里哪些值重复、重复几次、对应的明细行有哪些
  2. 值分布   —— 某列每个值各出现几次
  3. 跨表钻取 —— 拿当前结果的某列值，去另一张表的关联字段里查数据

设计要点：
  * 一律基于“当前筛选结果”而不是整表，先把源 SQL 剥掉行数限制再包成子查询，
    否则只会在前 N 行里查，结论是错的。
  * 明细一律用子查询表达（WHERE (c1,c2) IN (SELECT ...)），
    天然绕开 Oracle 的 IN 列表 1000 个值上限（ORA-01795）。
  * 只有在“用选中行的值”时才退化成字面量 IN，并按 1000 分块用 OR 连接。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import sqlbuilder as SB

# Oracle 硬限制：IN 列表最多 1000 个表达式，超过报 ORA-01795
MAX_IN_LIST = 1000


def normalize(sql: str, dialect: str = "oracle") -> str:
    """剥掉行数限制与分号，得到可用于嵌套的源语句。"""
    return SB.strip_row_limit(sql or "", dialect)


def _from_source(sql: str, dialect: str = "oracle", alias: str = "t") -> str:
    inner = SB._indent(normalize(sql, dialect))
    return f"FROM   (\n{inner}\n) {alias}"


def _col_list(columns: Sequence[str]) -> str:
    return ", ".join(SB.quote_ident(c) for c in columns)


# ---------------------------------------------------------------- 查重复值
def dup_summary_sql(sql: str, columns: Sequence[str], dialect: str = "oracle",
                    min_count: int = 2) -> str:
    """汇总：哪些值重复了、重复几次。按次数倒序。"""
    if not columns:
        raise ValueError("请至少选择一个字段。")
    cols = _col_list(columns)
    body = (f"SELECT {cols}, COUNT(*) AS 重复次数\n"
            f"{_from_source(sql, dialect)}\n"
            f"GROUP  BY {cols}\n"
            f"HAVING COUNT(*) >= {max(2, int(min_count))}\n"
            f"ORDER  BY 重复次数 DESC, {cols}")
    return body + ";"


def dup_detail_sql(sql: str, columns: Sequence[str], dialect: str = "oracle",
                   min_count: int = 2) -> str:
    """明细：所有参与重复的行（不只是“哪个值重复了”）。"""
    if not columns:
        raise ValueError("请至少选择一个字段。")
    cols = _col_list(columns)
    inner = SB._indent(normalize(sql, dialect))
    sub = (f"SELECT {cols}\n"
           f"FROM   (\n{inner}\n) t2\n"
           f"GROUP  BY {cols}\n"
           f"HAVING COUNT(*) >= {max(2, int(min_count))}")
    body = (f"SELECT *\n"
            f"{_from_source(sql, dialect)}\n"
            f"WHERE  ({cols}) IN (\n{SB._indent(sub)}\n)\n"
            f"ORDER  BY {cols}")
    return body + ";"


def value_detail_sql(sql: str, columns: Sequence[str], values: Sequence[Any],
                     col_types: Optional[Dict[str, str]] = None,
                     dialect: str = "oracle") -> str:
    """点某个具体的重复值，看它对应的所有行。"""
    if not columns:
        raise ValueError("请至少选择一个字段。")
    col_types = {k.upper(): v for k, v in (col_types or {}).items()}
    parts = []
    for c, v in zip(columns, values):
        ident = SB.quote_ident(c)
        if v is None:
            parts.append(f"{ident} IS NULL")
        else:
            parts.append(f"{ident} = {SB.literal(v, col_types.get(c.upper(), 'VARCHAR2'), dialect)}")
    body = (f"SELECT *\n"
            f"{_from_source(sql, dialect)}\n"
            f"WHERE  " + "\nAND    ".join(parts))
    return body + ";"


# ---------------------------------------------------------------- 值分布
def distinct_sql(sql: str, column: str, dialect: str = "oracle") -> str:
    """某列每个值出现几次（查重复是它的一个特例）。"""
    if not column:
        raise ValueError("请先指定要统计的字段。")
    c = SB.quote_ident(column)
    body = (f"SELECT {c}, COUNT(*) AS 行数\n"
            f"{_from_source(sql, dialect)}\n"
            f"GROUP  BY {c}\n"
            f"ORDER  BY 行数 DESC, {c}")
    return body + ";"


# ---------------------------------------------------------------- 跨表钻取
def in_predicate(col: str, values: Sequence[Any], col_type: str = "VARCHAR2",
                 dialect: str = "oracle", chunk: int = MAX_IN_LIST) -> str:
    """字面量 IN 谓词；超过 1000 个值自动分块用 OR 连接，规避 ORA-01795。"""
    vals = [v for v in (values or []) if v is not None]
    if not vals:
        return f"{col} IS NULL"
    groups = []
    for i in range(0, len(vals), max(1, chunk)):
        part = ", ".join(SB.literal(v, col_type, dialect) for v in vals[i:i + max(1, chunk)])
        groups.append(f"{col} IN ({part})")
    if len(groups) == 1:
        return groups[0]
    return "(\n    " + "\n OR ".join(groups) + "\n)"


def drill_sql(source_sql: str, source_column: str, target_schema: str, target_table: str,
              target_column: str, dialect: str = "oracle",
              selected_values: Optional[Sequence[Any]] = None,
              col_type: str = "VARCHAR2") -> str:
    """拿源结果里某列的值，去目标表的目标字段里查。

    selected_values 为空 -> 用子查询（推荐：不受 1000 限制、语句短、可读）
    否则                -> 用选中行的字面量 IN
    """
    if not target_table or not target_column:
        raise ValueError("请选择目标表和目标字段。")
    table = SB.full_table_name(target_schema, target_table)
    tcol = SB.quote_ident(target_column)

    if not selected_values:
        if not source_sql or not source_column:
            raise ValueError("缺少来源语句或来源字段。")
        sc = SB.quote_ident(source_column)
        inner = SB._indent(normalize(source_sql, dialect))
        sub = f"SELECT {sc}\nFROM   (\n{inner}\n) s\nWHERE  {sc} IS NOT NULL"
        return f"SELECT *\nFROM   {table}\nWHERE  {tcol} IN (\n{SB._indent(sub)}\n);"

    return f"SELECT *\nFROM   {table}\nWHERE  {in_predicate(tcol, selected_values, col_type, dialect)};"


def drill_title(source_table: str, source_column: str, target_table: str, target_column: str) -> str:
    """探查链上这一跳的可读描述。"""
    left = f"{source_table}.{source_column}" if source_table else str(source_column)
    return f"{left} → {target_table}.{target_column}"
