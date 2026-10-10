"""按 Oracle 数据类型校验并转换用户输入的值。

目标用户熟悉 Excel，所以对日期、数字做了比较宽容的解析。
"""
from __future__ import annotations

import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

DATE_PATTERNS = [
    ("%Y-%m-%d %H:%M:%S", "2024-01-15 08:30:00"),
    ("%Y-%m-%d %H:%M", "2024-01-15 08:30"),
    ("%Y-%m-%d", "2024-01-15"),
    ("%Y/%m/%d %H:%M:%S", "2024/01/15 08:30:00"),
    ("%Y/%m/%d", "2024/01/15"),
    ("%Y%m%d", "20240115"),
    ("%Y%m%d%H%M%S", "20240115083000"),
    ("%Y-%m-%d %H:%M:%S.%f", "2024-01-15 08:30:00.123"),
    ("%d-%m-%Y", "15-01-2024"),
]

# 用户输入里代表“空值”的写法（不含空串：空串由 validate 单独给出更明确的提示）
NULL_TOKENS = {"null", "(null)", "none", "(none)"}


def is_null_token(text: str) -> bool:
    return (text or "").strip().lower() in NULL_TOKENS


def hint_for(data_type: str) -> str:
    t = (data_type or "").upper()
    if t.startswith("DATE") or t.startswith("TIMESTAMP"):
        return "日期示例：2024-01-15 或 2024-01-15 08:30:00"
    if t in ("NUMBER", "FLOAT", "INTEGER", "INT", "DECIMAL", "NUMERIC", "REAL", "DOUBLE PRECISION",
             "BINARY_FLOAT", "BINARY_DOUBLE"):
        return "数字示例：1024、3.14、-5"
    if t in ("CHAR", "NCHAR"):
        return "文本会原样写入，注意定长字段会自动补空格"
    return "文本示例：SMITH；支持 % 通配（LIKE 时）"


def parse_date(text: str) -> Optional[datetime]:
    s = (text or "").strip().strip("'\"")
    if not s:
        return None
    for pattern, _ in DATE_PATTERNS:
        try:
            return datetime.strptime(s, pattern)
        except ValueError:
            continue
    # 尝试一些常见变体：2024年1月15日
    m = re.match(r"^(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_number(text: str) -> Optional[Decimal]:
    s = (text or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def validate(data_type: str, text: str) -> Tuple[bool, Any, str]:
    """校验用户输入。

    返回 (是否通过, 转换后的 Python 值, 提示信息)。
    """
    raw = "" if text is None else str(text)
    t = (data_type or "VARCHAR2").upper()

    if is_null_token(raw):
        if raw == "":
            return False, None, "值不能为空（如需判断空值，请选择“为空 / 不为空”运算符）"
        return True, None, ""

    if t.startswith("DATE") or t.startswith("TIMESTAMP"):
        if raw.strip().upper() in ("SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE"):
            return True, raw.strip().upper(), ""
        dt = parse_date(raw)
        if dt is None:
            return False, None, f"“{raw}”不是有效的日期时间\n{hint_for(t)}"
        return True, dt, ""

    if t in ("NUMBER", "FLOAT", "INTEGER", "INT", "DECIMAL", "NUMERIC", "REAL",
             "DOUBLE PRECISION", "BINARY_FLOAT", "BINARY_DOUBLE"):
        num = parse_number(raw)
        if num is None:
            return False, None, f"“{raw}”不是有效的数字\n{hint_for(t)}"
        return True, num, ""

    if t in ("CLOB", "NCLOB", "LONG", "BLOB", "RAW"):
        return True, raw, ""

    return True, raw, ""


# ---------------------------------------------------------------- 值的格式化
def format_value(v: Any, date_format: str = "%Y-%m-%d %H:%M:%S", null_display: str = "") -> str:
    if v is None:
        return null_display
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (datetime, date)):
        try:
            if isinstance(v, datetime) and v.microsecond:
                return v.strftime(date_format + ".%f")[:23]
            return v.strftime(date_format)
        except Exception:
            return str(v)
    if isinstance(v, Decimal):
        return _format_decimal(v)
    if isinstance(v, bytes):
        return f"<二进制 {len(v)} 字节>"
    return str(v)


def _format_decimal(v: Decimal) -> str:
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def to_excel_value(v: Any):
    """转换成 Excel 友好的类型。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        try:
            return float(v)
        except Exception:
            return str(v)
    if isinstance(v, (datetime, date)):
        return v
    if isinstance(v, bytes):
        return f"<二进制 {len(v)} 字节>"
    if isinstance(v, str) and len(v) > 32000:
        return v[:32000] + "…(已截断)"
    return v


def compare_values(left: Any, right: Any) -> int:
    """用于网格排序的比较函数，兼容 None / 数字 / 字符串 / 日期的混合。"""
    ln, rn = left is None, right is None
    if ln and rn:
        return 0
    if ln:
        return -1
    if rn:
        return 1
    try:
        if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
            a, b = float(left), float(right)
        elif isinstance(left, str) and isinstance(right, str):
            la, ra = parse_number(left), parse_number(right)
            if la is not None and ra is not None:
                a, b = float(la), float(ra)
            else:
                return (left > right) - (left < right)
        else:
            return (str(left) > str(right)) - (str(left) < str(right))
        return (a > b) - (a < b)
    except Exception:
        return (str(left) > str(right)) - (str(left) < str(right))
