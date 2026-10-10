"""运维常用查询：实例概览、会话统计、慢 SQL、表空间。

要点：
  * 全部按“权限级别”生成不同语句——dba_* → all_* → user_* 逐级降级，
    没有权限就换一条能跑的，而不是报错了事；
  * 演示库（SQLite）没有 Oracle 字典视图，这里给出一份明确标注的样例数据，
    只为让人看到界面长什么样，不会被误认成真实数据。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

DEMO_NOTE = "以下为演示数据（内置演示库没有 Oracle 实例视图）"


# ================================================================== 实例概览
def instance_sql(level: str = "full", with_sessions: bool = True) -> str:
    """实例概览。level: full(v$instance+v$database) / instance / version。"""
    if level == "full":
        sql = (
            "SELECT di.INSTANCE_NAME AS \"实例名\", di.HOST_NAME AS \"主机名\",\n"
            "       db.NAME AS \"数据库名\", di.VERSION AS \"版本\",\n"
            "       di.STATUS AS \"实例状态\", db.OPEN_MODE AS \"打开模式\",\n"
            "       db.DATABASE_ROLE AS \"数据库角色\", db.LOG_MODE AS \"归档模式\",\n"
            "       TO_CHAR(di.STARTUP_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"启动时间\",\n"
            "       ROUND(SYSDATE - di.STARTUP_TIME, 2) AS \"已运行天数\",\n"
            "       db.PLATFORM_NAME AS \"平台\"\n"
            "FROM   V$INSTANCE di, V$DATABASE db"
        )
    elif level == "instance":
        sql = (
            "SELECT di.INSTANCE_NAME AS \"实例名\", di.HOST_NAME AS \"主机名\",\n"
            "       di.VERSION AS \"版本\", di.STATUS AS \"实例状态\",\n"
            "       TO_CHAR(di.STARTUP_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"启动时间\",\n"
            "       ROUND(SYSDATE - di.STARTUP_TIME, 2) AS \"已运行天数\",\n"
            "       TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') AS \"数据库当前时间\"\n"
            "FROM   V$INSTANCE di"
        )
    elif level == "version":
        sql = (
            "SELECT SUBSTR(banner, 1, 120) AS \"版本信息\"\n"
            "FROM   V$VERSION WHERE ROWNUM <= 4"
        )
    else:
        sql = (
            "SELECT USER AS \"当前用户\",\n"
            "       SYS_CONTEXT('USERENV','CURRENT_SCHEMA') AS \"当前 Schema\",\n"
            "       SYS_CONTEXT('USERENV','DB_NAME') AS \"数据库名\",\n"
            "       SYS_CONTEXT('USERENV','HOST') AS \"主机名\",\n"
            "       SYS_CONTEXT('USERENV','SESSION_USER') AS \"会话用户\"\n"
            "FROM   DUAL"
        )
    return sql


def basic_info_sql() -> str:
    """永远能查到的那部分信息（不需要任何额外权限）。"""
    return (
        "SELECT USER AS \"当前用户\",\n"
        "       SYS_CONTEXT('USERENV','CURRENT_SCHEMA') AS \"当前 Schema\",\n"
        "       SYS_CONTEXT('USERENV','DB_NAME') AS \"数据库名\",\n"
        "       SYS_CONTEXT('USERENV','HOST') AS \"主机名\",\n"
        "       TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') AS \"数据库当前时间\"\n"
        "FROM   DUAL"
    )


# ================================================================== 会话统计
def session_count_sql() -> str:
    """会话总数 / 活动数 / 被阻塞数，一行三列，供概览页直接拼在后面。"""
    return (
        "SELECT COUNT(*) AS \"会话数\",\n"
        "       SUM(CASE WHEN STATUS = 'ACTIVE' THEN 1 ELSE 0 END) AS \"活动会话\",\n"
        "       SUM(CASE WHEN BLOCKING_SESSION IS NOT NULL THEN 1 ELSE 0 END) AS \"被阻塞会话\"\n"
        "FROM   V$SESSION"
    )


def session_summary_sql(with_blocking: bool = True) -> str:
    blocked = (",\n         SUM(CASE WHEN s.BLOCKING_SESSION IS NOT NULL THEN 1 ELSE 0 END)"
               " AS \"被阻塞\"") if with_blocking else ""
    return (
        "SELECT NVL(s.STATUS, '(空)') AS \"状态\", NVL(s.TYPE, '(空)') AS \"类型\",\n"
        "       COUNT(*) AS \"会话数\"" + blocked + "\n"
        "FROM   V$SESSION s\n"
        "GROUP  BY s.STATUS, s.TYPE\n"
        "ORDER  BY \"会话数\" DESC"
    )


def session_top_sql(top_n: int = 20) -> str:
    return (
        "SELECT * FROM (\n"
        "  SELECT NVL(s.USERNAME, '(后台)') AS \"用户\",\n"
        "         NVL(s.PROGRAM, '(未知)') AS \"程序\",\n"
        "         NVL(s.MACHINE, '(未知)') AS \"机器\",\n"
        "         NVL(s.STATUS, '(空)') AS \"状态\",\n"
        "         COUNT(*) AS \"会话数\"\n"
        "  FROM   V$SESSION s\n"
        "  GROUP  BY s.USERNAME, s.PROGRAM, s.MACHINE, s.STATUS\n"
        "  ORDER  BY COUNT(*) DESC\n"
        ") WHERE ROWNUM <= " + str(int(top_n))
    )


# ================================================================== 慢 SQL
SLOW_ORDER = {
    "平均耗时": "平均耗时秒 DESC",
    "总耗时": "总耗时秒 DESC",
    "执行次数": "执行次数 DESC",
    "逻辑读": "逻辑读 DESC",
    "物理读": "物理读 DESC",
    "最后活动": "最后活动 DESC NULLS LAST",
}


def slow_sql_sql(level: str = "sqlarea", top_n: int = 50, min_avg_sec: float = 0.0,
                 order_by: str = "平均耗时", keyword: str = "",
                 min_execs: int = 1) -> str:
    """Top SQL。level: sqlarea（汇总，推荐）/ sql（子游标明细）。"""
    src = "V$SQLAREA" if level == "sqlarea" else "V$SQL"
    where = ["s.ELAPSED_TIME > 0"]
    try:
        if float(min_avg_sec) > 0:
            where.append(
                f"(s.ELAPSED_TIME / 1000000 / GREATEST(s.EXECUTIONS, 1)) >= {float(min_avg_sec)}")
    except (TypeError, ValueError):
        pass
    try:
        if int(min_execs) > 1:
            where.append(f"s.EXECUTIONS >= {int(min_execs)}")
    except (TypeError, ValueError):
        pass
    kw = (keyword or "").strip().replace("'", "''")
    if kw:
        where.append(
            f"(UPPER(s.SQL_TEXT) LIKE UPPER('%{kw}%') OR UPPER(NVL(s.MODULE,' ')) LIKE UPPER('%{kw}%'))")
    order = SLOW_ORDER.get(order_by or "平均耗时", SLOW_ORDER["平均耗时"])
    cols = (
        "  SELECT s.SQL_ID AS \"SQL_ID\",\n"
        "         ROUND(s.ELAPSED_TIME / 1000000 / GREATEST(s.EXECUTIONS, 1), 3) AS \"平均耗时秒\",\n"
        "         ROUND(s.ELAPSED_TIME / 1000000, 2) AS \"总耗时秒\",\n"
        "         s.EXECUTIONS AS \"执行次数\",\n"
        "         s.BUFFER_GETS AS \"逻辑读\",\n"
        "         s.DISK_READS AS \"物理读\",\n"
        "         ROUND(s.BUFFER_GETS / GREATEST(s.EXECUTIONS, 1)) AS \"每次逻辑读\",\n"
        "         s.PARSE_CALLS AS \"解析次数\",\n"
        "         TO_CHAR(s.LAST_ACTIVE_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"最后活动\",\n"
        "         NVL(s.PARSING_SCHEMA_NAME, '(空)') AS \"解析用户\",\n"
        "         NVL(s.MODULE, '(空)') AS \"模块\",\n"
        "         SUBSTR(REPLACE(TRIM(s.SQL_TEXT), CHR(10), ' '), 1, 220) AS \"SQL 文本（前 220 字符）\"\n"
    )
    return (
        "SELECT * FROM (\n"
        + cols +
        f"  FROM   {src} s\n"
        "  WHERE  " + "\n    AND ".join(where) + "\n"
        f"  ORDER  BY {order}\n"
        f") WHERE ROWNUM <= {max(1, int(top_n))}"
    )


def sql_text_by_id(sql_id: str, level: str = "sqlarea") -> str:
    """按 SQL_ID 取完整 SQL 文本。"""
    src = "V$SQLAREA" if level == "sqlarea" else "V$SQL"
    sid = (sql_id or "").replace("'", "''")
    return (
        f"SELECT SQL_FULLTEXT AS \"SQL 文本\" FROM {src} "
        f"WHERE SQL_ID = '{sid}' AND ROWNUM = 1"
    )


# ================================================================== 表空间
def tablespace_sql(level: str = "full") -> str:
    """level: full（dba_data_files + dba_free_space）/ metrics / own。"""
    if level == "full":
        return (
            "SELECT a.\"表空间\", t.CONTENTS AS \"类型\",\n"
            "       ROUND(a.total_mb, 1) AS \"总大小MB\",\n"
            "       ROUND(a.total_mb - NVL(f.free_mb, 0), 1) AS \"已用MB\",\n"
            "       ROUND(NVL(f.free_mb, 0), 1) AS \"空闲MB\",\n"
            "       ROUND((a.total_mb - NVL(f.free_mb, 0)) / GREATEST(a.total_mb, 1) * 100, 1) AS \"使用率%\",\n"
            "       ROUND(a.max_mb, 1) AS \"最大可扩MB\",\n"
            "       a.files AS \"数据文件数\"\n"
            "FROM (\n"
            "  SELECT TABLESPACE_NAME AS \"表空间\",\n"
            "         SUM(BYTES) / 1048576 AS total_mb,\n"
            "         SUM(DECODE(AUTOEXTENSIBLE, 'YES', GREATEST(MAXBYTES, BYTES), BYTES)) / 1048576 AS max_mb,\n"
            "         COUNT(*) AS files\n"
            "  FROM   DBA_DATA_FILES GROUP BY TABLESPACE_NAME\n"
            ") a\n"
            "LEFT JOIN (\n"
            "  SELECT TABLESPACE_NAME, SUM(BYTES) / 1048576 AS free_mb\n"
            "  FROM   DBA_FREE_SPACE GROUP BY TABLESPACE_NAME\n"
            ") f ON f.TABLESPACE_NAME = a.\"表空间\"\n"
            "JOIN DBA_TABLESPACES t ON t.TABLESPACE_NAME = a.\"表空间\"\n"
            "ORDER BY \"使用率%\" DESC"
        )
    if level == "metrics":
        return (
            "SELECT m.TABLESPACE_NAME AS \"表空间\", t.CONTENTS AS \"类型\",\n"
            "       ROUND(m.TABLESPACE_SIZE * t.BLOCK_SIZE / 1048576, 1) AS \"总大小MB\",\n"
            "       ROUND(m.USED_SPACE * t.BLOCK_SIZE / 1048576, 1) AS \"已用MB\",\n"
            "       ROUND((m.TABLESPACE_SIZE - m.USED_SPACE) * t.BLOCK_SIZE / 1048576, 1) AS \"空闲MB\",\n"
            "       ROUND(m.USED_PERCENT, 1) AS \"使用率%\"\n"
            "FROM   DBA_TABLESPACE_USAGE_METRICS m\n"
            "JOIN   DBA_TABLESPACES t ON t.TABLESPACE_NAME = m.TABLESPACE_NAME\n"
            "ORDER  BY m.USED_PERCENT DESC"
        )
    # own：只能看到自己 schema 的段占用 + 自己的配额
    return (
        "SELECT s.TABLESPACE_NAME AS \"表空间\",\n"
        "       ROUND(SUM(s.BYTES) / 1048576, 1) AS \"已用MB\",\n"
        "       ROUND(q.quota_mb, 1) AS \"配额MB\",\n"
        "       CASE WHEN q.quota_mb > 0\n"
        "            THEN ROUND(SUM(s.BYTES) / 1048576 / q.quota_mb * 100, 1) END AS \"占配额%\",\n"
        "       COUNT(*) AS \"段数\"\n"
        "FROM   USER_SEGMENTS s\n"
        "LEFT JOIN (\n"
        "  SELECT TABLESPACE_NAME,\n"
        "         DECODE(MAX(MAX_BYTES), -1, NULL, MAX(MAX_BYTES) / 1048576) AS quota_mb\n"
        "  FROM   USER_TS_QUOTAS GROUP BY TABLESPACE_NAME\n"
        ") q ON q.TABLESPACE_NAME = s.TABLESPACE_NAME\n"
        "GROUP  BY s.TABLESPACE_NAME, q.quota_mb\n"
        "ORDER  BY \"已用MB\" DESC"
    )


def temp_usage_sql() -> str:
    """临时表空间用量（需要 DBA_TEMP_FILES / V$TEMP_SPACE_HEADER）。"""
    return (
        "SELECT t.TABLESPACE_NAME AS \"临时表空间\",\n"
        "       ROUND(SUM(f.BYTES) / 1048576, 1) AS \"总大小MB\",\n"
        "       ROUND(NVL(u.used_mb, 0), 1) AS \"已用MB\",\n"
        "       ROUND((SUM(f.BYTES) / 1048576 - NVL(u.used_mb, 0)), 1) AS \"空闲MB\"\n"
        "FROM   DBA_TEMP_FILES f\n"
        "JOIN   DBA_TABLESPACES t ON t.TABLESPACE_NAME = f.TABLESPACE_NAME\n"
        "LEFT JOIN (\n"
        "  SELECT TABLESPACE_NAME, SUM(BYTES_USED) / 1048576 AS used_mb\n"
        "  FROM   V$TEMP_SPACE_HEADER GROUP BY TABLESPACE_NAME\n"
        ") u ON u.TABLESPACE_NAME = f.TABLESPACE_NAME\n"
        "GROUP  BY t.TABLESPACE_NAME, NVL(u.used_mb, 0)\n"
        "ORDER  BY 1"
    )


# ================================================================== 占空间最大的段
def top_segments_sql(level: str = "full", top_n: int = 30, schema: str = "") -> str:
    if level == "full":
        inner = (
            "  SELECT s.OWNER AS \"属主\", s.SEGMENT_NAME AS \"段名\", s.SEGMENT_TYPE AS \"类型\",\n"
            "         s.TABLESPACE_NAME AS \"表空间\", ROUND(s.BYTES / 1048576, 1) AS \"大小MB\"\n"
            "  FROM   DBA_SEGMENTS s\n"
        )
        where = ""
        if schema:
            where = f"  WHERE  s.OWNER = '{schema.replace(chr(39), chr(39) * 2)}'\n"
        return ("SELECT * FROM (\n" + inner + where +
                "  ORDER  BY s.BYTES DESC\n"
                f") WHERE ROWNUM <= {max(1, int(top_n))}")
    return (
        "SELECT * FROM (\n"
        "  SELECT s.SEGMENT_NAME AS \"段名\", s.SEGMENT_TYPE AS \"类型\",\n"
        "         s.TABLESPACE_NAME AS \"表空间\", ROUND(s.BYTES / 1048576, 1) AS \"大小MB\"\n"
        "  FROM   USER_SEGMENTS s\n"
        "  ORDER  BY s.BYTES DESC\n"
        f") WHERE ROWNUM <= {max(1, int(top_n))}"
    )


# ================================================================== 演示库样例数据
def _demo_instance() -> Tuple[List[str], List[tuple]]:
    cols = ["项", "值"]
    rows = [
        ("说明", DEMO_NOTE),
        ("实例名", "DEMO"),
        ("主机名", "localhost"),
        ("数据库名", "DEMO"),
        ("版本", "演示库 1.0（非真实 Oracle）"),
        ("实例状态", "OPEN"),
        ("打开模式", "READ WRITE"),
        ("归档模式", "NOARCHIVELOG"),
        ("启动时间", "2026-09-20 08:00:00"),
        ("已运行天数", 0.24),
        ("会话数", 12),
        ("活动会话", 3),
        ("当前用户", "DEMO"),
    ]
    return cols, rows


def _demo_sessions() -> Tuple[List[str], List[tuple]]:
    cols = ["状态", "类型", "会话数"]
    rows = [("ACTIVE", "USER", 3), ("INACTIVE", "USER", 7), ("ACTIVE", "BACKGROUND", 2)]
    return cols, rows


def _demo_slow() -> Tuple[List[str], List[tuple]]:
    cols = ["SQL_ID", "平均耗时秒", "总耗时秒", "执行次数", "逻辑读", "物理读",
            "每次逻辑读", "解析次数", "最后活动", "解析用户", "模块", "SQL 文本（前 220 字符）"]
    rows = [
        ("8h3kdm2xqz9ta", 12.531, 375.93, 30, 1823440, 91220, 60781, 30,
         "2026-09-20 13:20:11", "DEMO", "JDBC Thin Client",
         "SELECT * FROM DEMO.ORDERS o JOIN DEMO.EMPLOYEES e ON e.EMP_ID = o.EMP_ID WHERE o.STATUS = '已完成'"),
        ("b1p9vz0m4nc7s", 3.207, 96.21, 30, 512300, 12040, 17077, 30,
         "2026-09-20 12:58:02", "DEMO", "SQL*Plus",
         "SELECT DEPT_ID, COUNT(*) FROM DEMO.EMPLOYEES GROUP BY DEPT_ID ORDER BY 2 DESC"),
        ("c7t2xr8a1hb50", 0.884, 88.40, 100, 264100, 0, 2641, 100,
         "2026-09-20 13:31:47", "DEMO", "JDBC Thin Client",
         "UPDATE DEMO.EMPLOYEES SET SALARY = SALARY * 1.05 WHERE DEPT_ID = 3"),
        ("d4m6nq2ws8yk1", 0.412, 12.36, 30, 98400, 330, 3280, 30,
         "2026-09-20 11:02:19", "DEMO", "PL/SQL Developer",
         "SELECT * FROM DEMO.V_EMP_DETAIL WHERE SALARY > 20000"),
    ]
    return cols, rows


def _demo_tablespace() -> Tuple[List[str], List[tuple]]:
    cols = ["表空间", "类型", "总大小MB", "已用MB", "空闲MB", "使用率%", "最大可扩MB", "数据文件数"]
    rows = [
        ("SYSTEM", "PERMANENT", 1024.0, 965.2, 58.8, 94.3, 32767.0, 1),
        ("SYSAUX", "PERMANENT", 2048.0, 1712.6, 335.4, 83.6, 32767.0, 1),
        ("UNDOTBS1", "UNDO", 4096.0, 1204.8, 2891.2, 29.4, 32767.0, 1),
        ("USERS", "PERMANENT", 8192.0, 5880.1, 2311.9, 71.8, 32767.0, 2),
        ("TBS_ORDER", "PERMANENT", 16384.0, 15200.7, 1183.3, 92.8, 32767.0, 4),
    ]
    return cols, rows


def _demo_segments() -> Tuple[List[str], List[tuple]]:
    cols = ["属主", "段名", "类型", "表空间", "大小MB"]
    rows = [
        ("DEMO", "ORDERS", "TABLE", "TBS_ORDER", 512.0),
        ("DEMO", "PK_ORDERS", "INDEX", "TBS_ORDER", 96.0),
        ("DEMO", "EMPLOYEES", "TABLE", "USERS", 24.0),
        ("DEMO", "DEPARTMENTS", "TABLE", "USERS", 0.1),
    ]
    return cols, rows


DEMO_DATA = {
    "instance": _demo_instance,
    "sessions": _demo_sessions,
    "slow": _demo_slow,
    "tablespace": _demo_tablespace,
    "segments": _demo_segments,
}


def demo_rows(kind: str) -> Tuple[List[str], List[tuple]]:
    fn = DEMO_DATA.get(kind)
    return fn() if fn else ([], [])


# ================================================================== 统一取数
def fetch(session, probe, kind: str, **kw) -> Tuple[List[str], List[tuple], str, str]:
    """按权限级别取一组运维数据。

    返回 (列名, 行, 备注, 实际使用的级别)。
    备注用于在界面上说明“为什么会降级 / 这是演示数据”。
    """
    if session is None:
        return [], [], "当前没有连接数据库。", "none"
    if getattr(getattr(session, "profile", None), "demo", False):
        cols, rows = demo_rows(kind)
        return cols, rows, DEMO_NOTE, "demo"

    note = ""
    try:
        if kind == "instance":
            level = probe.level("instance")
            note = probe.missing_hint("instance")
            cols, rows = _run(session, instance_sql(level))
            if len(cols) > 1:  # 概览一律摊成“项 / 值”两列，界面上更好看
                cols, rows = _transpose(cols, rows)
            if probe.has("v_session"):
                c2, r2 = _run(session, session_count_sql())
                if r2:
                    for name, val in zip(c2, r2[0]):
                        rows.append((str(name), val))
            return cols, rows, note, level

        if kind == "sessions":
            level = probe.level("sessions")
            if level == "none":
                return [], [], probe.missing_hint("sessions"), "none"
            cols, rows = _run(session, session_summary_sql())
            return cols, rows, "", level

        if kind == "slow":
            level = probe.level("slow_sql")
            if level == "none":
                return [], [], probe.missing_hint("slow_sql"), "none"
            sql = slow_sql_sql("sqlarea" if level == "sqlarea" else "sql", **kw)
            cols, rows = _run(session, sql)
            extra = "" if level == "sqlarea" else "（看不到 V$SQLAREA，改用 V$SQL：同条 SQL 的多个子游标会分开展示）"
            return cols, rows, extra, level

        if kind == "tablespace":
            level = probe.level("tablespace")
            if level == "none":
                return [], [], probe.missing_hint("tablespace"), "none"
            cols, rows = _run(session, tablespace_sql(level))
            return cols, rows, probe.missing_hint("tablespace"), level

        if kind == "segments":
            level = probe.level("segments")
            if level == "none":
                return [], [], "当前账号看不到 DBA_SEGMENTS / USER_SEGMENTS，无法统计段占用。", "none"
            cols, rows = _run(session, top_segments_sql(level, kw.get("top_n", 30)))
            extra = "" if level == "full" else "（无 DBA_SEGMENTS 权限，只统计当前用户自己的段）"
            return cols, rows, extra, level
    except Exception as e:  # noqa: BLE001
        return [], [], f"查询失败：{e}", "error"
    return [], [], "暂不支持的类型：" + str(kind), "none"


def _run(session, sql: str) -> Tuple[List[str], List[tuple]]:
    res = session.query(sql, max_rows=2000)
    return list(res.columns or []), list(res.rows or [])


def _transpose(cols: Sequence[str], rows: Sequence[tuple]) -> Tuple[List[str], List[tuple]]:
    """把“宽表一行”摊成“项 / 值”两列，用于概览页。"""
    out: List[tuple] = []
    for r in rows:
        for c, v in zip(cols, r):
            out.append((str(c), v))
    return ["项", "值"], out
