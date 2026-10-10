"""数据库访问层。

对外只暴露 DbSession，内部按连接类型选择不同的适配器：
  * OracleAdapter —— 通过 python-oracledb 的瘦模式直连（无需安装 Oracle 客户端）
  * DemoAdapter   —— 内置 SQLite 演示库，没装 Oracle 也能完整体验本软件
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .models import ColumnMeta, ConnProfile, QueryResult, TableMeta

try:  # oracledb 可能尚未安装，缺失时给出清晰提示
    import oracledb  # type: ignore
except Exception:  # pragma: no cover
    oracledb = None


class DbError(Exception):
    """数据库相关错误的统一封装。"""


# ================================================================== 基类
class DbAdapter:
    dialect = "oracle"

    def __init__(self, profile: ConnProfile):
        self.profile = profile
        self.conn = None
        self.server_version = ""
        self.current_user = ""
        self.current_schema = ""

    # ---- 生命周期 ----
    def open(self):  # pragma: no cover
        raise NotImplementedError

    def close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        finally:
            self.conn = None

    @property
    def is_open(self) -> bool:
        return self.conn is not None

    def cancel(self):
        """尝试中断正在执行的语句（Oracle 支持，SQLite 会直接中断）。"""
        try:
            fn = getattr(self.conn, "cancel", None)
            if fn:
                fn()
        except Exception:
            pass

    # ---- SQL ----
    def query(self, sql: str, max_rows: Optional[int] = None) -> QueryResult:  # pragma: no cover
        raise NotImplementedError

    def execute(self, sql: str, params: Optional[dict] = None) -> int:  # pragma: no cover
        raise NotImplementedError

    def stream(self, sql: str, chunk: int = 1000):  # pragma: no cover
        raise NotImplementedError

    def commit(self):
        if self.conn is not None:
            self.conn.commit()

    def rollback(self):
        if self.conn is not None:
            self.conn.rollback()

    def query_value(self, sql: str, default: Any = None) -> Any:
        r = self.query(sql, max_rows=1)
        if r.rows:
            return r.rows[0][0]
        return default

    # ---- 元数据 ----
    def list_schemas(self) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def list_objects(self, schema: str, kinds=("TABLE", "VIEW", "SYNONYM")) -> List[TableMeta]:  # pragma: no cover
        raise NotImplementedError

    def describe_table(self, schema: str, table: str) -> List[ColumnMeta]:  # pragma: no cover
        raise NotImplementedError

    def table_rowcount(self, schema: str, table: str) -> Optional[int]:
        return None

    def table_ddl(self, schema: str, table: str) -> str:
        return ""

    def table_indexes(self, schema: str, table: str) -> List[dict]:
        return []

    def explain(self, sql: str) -> QueryResult:
        raise DbError("当前连接不支持查看执行计划")

    # ---- 方言 ----
    def paginate(self, sql: str, offset: int, limit: int) -> str:
        base = (sql or "").strip().rstrip(";")
        if self.dialect == "oracle":
            return (
                "SELECT * FROM (\n"
                "  SELECT Q.*, ROWNUM AS RN__\n"
                "  FROM (\n" + base + "\n  ) Q\n"
                f"  WHERE ROWNUM <= {int(offset) + int(limit)}\n"
                f") WHERE RN__ > {int(offset)}"
            )
        return f"SELECT * FROM (\n{base}\n) LIMIT {int(limit)} OFFSET {int(offset)}"

    def literal(self, value: Any, data_type: str = "VARCHAR2") -> str:
        from .sqlbuilder import literal

        return literal(value, data_type, self.dialect)

    @staticmethod
    def quote(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'


# ================================================================== Oracle
class OracleAdapter(DbAdapter):
    dialect = "oracle"

    def open(self):
        if oracledb is None:
            raise DbError("缺少依赖包 python-oracledb，请先运行“安装依赖.bat”。")
        try:
            oracledb.defaults.fetch_lobs = False  # CLOB/BLOB 直接返回字符串/字节
        except Exception:
            pass

        p = self.profile
        if not p.thick:
            # thin 模式强依赖 cryptography 做口令加密/网络加密。
            # 这里显式 import：既能提前给出人话提示，也让打包工具能静态发现该依赖。
            try:
                import cryptography  # noqa: F401
            except Exception:
                raise DbError(
                    "缺少加密库 cryptography，thin（瘦）模式无法连接 Oracle。\n"
                    "· 源码运行：pip install cryptography\n"
                    "· 便携 exe：该版本打包时漏了这个库，请重新打包\n"
                    "· 临时办法：在连接设置里勾选“使用 Oracle Instant Client（thick 模式）”"
                )
        if p.thick:
            try:
                oracledb.init_oracle_client(lib_dir=(p.lib_dir or None))
            except Exception as e:
                # 已经初始化过会抛异常，忽略即可
                if "has already been initialized" not in str(e):
                    raise DbError(f"初始化 Oracle Instant Client 失败：{e}")

        mode = None
        if (p.role or "").upper() == "SYSDBA" and hasattr(oracledb, "AUTH_MODE_SYSDBA"):
            mode = oracledb.AUTH_MODE_SYSDBA

        kwargs: Dict[str, Any] = dict(user=p.user, password=p.password, dsn=p.make_dsn(), mode=mode)
        try:
            kwargs["tcp_connect_timeout"] = max(1, int(p.timeout or 10))
        except Exception:
            pass

        try:
            self.conn = oracledb.connect(**kwargs)
        except Exception as e:
            raise DbError(_friendly_oracle_error(e))

        try:
            self.server_version = str(self.conn.version)
        except Exception:
            self.server_version = ""
        try:
            self.current_user = self.query_value("SELECT USER FROM DUAL", "") or ""
            self.current_schema = (
                self.query_value("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL", "") or self.current_user
            )
        except Exception:
            pass
        self._init_session()

    def _init_session(self):
        stmts = [
            "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'",
            "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
            "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'",
        ]
        if (self.profile.schema or "").strip():
            stmts.append(f'ALTER SESSION SET CURRENT_SCHEMA = "{self.profile.schema.strip()}"')
            self.current_schema = self.profile.schema.strip().upper()
        for s in stmts:
            try:
                cur = self.conn.cursor()
                cur.execute(s)
                cur.close()
            except Exception:
                pass

    # ---- 执行 ----
    @staticmethod
    def _prepare(sql: str) -> str:
        from .sqlbuilder import prepare_statement

        return prepare_statement(sql)

    def query(self, sql: str, max_rows: Optional[int] = None) -> QueryResult:
        started = time.time()
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        try:
            cur.arraysize = 1000
            cur.execute(stmt)
        except Exception as e:
            cur.close()
            raise DbError(_friendly_oracle_error(e))

        if cur.description is None:
            rc = cur.rowcount
            kind = "PLSQL" if stmt.upper().startswith(("BEGIN", "DECLARE")) else "DML"
            cur.close()
            return QueryResult(columns=[], rows=[], elapsed=time.time() - started,
                               message=f"语句执行成功，影响行数：{rc}", kind=kind, statement=stmt)

        cols = [d[0] for d in cur.description]
        rows: List[tuple] = []
        truncated = False
        if max_rows:
            while len(rows) < max_rows:
                batch = cur.fetchmany(min(1000, max_rows - len(rows)))
                if not batch:
                    break
                rows.extend(batch)
            if len(rows) >= max_rows:
                try:
                    if cur.fetchone() is not None:
                        truncated = True
                except Exception:
                    pass
        else:
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                rows.extend(batch)
        cur.close()
        return QueryResult(columns=cols, rows=rows, elapsed=time.time() - started,
                           truncated=truncated, kind="SELECT", statement=stmt)

    def execute(self, sql: str, params: Optional[dict] = None) -> int:
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        try:
            if params:
                cur.execute(stmt, params)
            else:
                cur.execute(stmt)
            rc = cur.rowcount
            cur.close()
            return rc
        except Exception as e:
            try:
                cur.close()
            except Exception:
                pass
            raise DbError(_friendly_oracle_error(e))

    def execute_many(self, sql: str, seq: List[dict]) -> int:
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        try:
            cur.executemany(stmt, seq)
            rc = cur.rowcount
            cur.close()
            return rc
        except Exception as e:
            raise DbError(_friendly_oracle_error(e))

    def stream(self, sql: str, chunk: int = 1000):
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        cur.arraysize = chunk
        try:
            cur.execute(stmt)
        except Exception as e:
            raise DbError(_friendly_oracle_error(e))
        cols = [d[0] for d in (cur.description or [])]
        try:
            while True:
                batch = cur.fetchmany(chunk)
                if not batch:
                    break
                yield cols, batch
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # ---- 元数据 ----
    def list_schemas(self) -> List[str]:
        sql = "SELECT USERNAME FROM ALL_USERS ORDER BY USERNAME"
        try:
            r = self.query(sql)
            return [x[0] for x in r.rows]
        except Exception:
            return [self.current_schema or self.current_user]

    def list_objects(self, schema: str, kinds=("TABLE", "VIEW", "SYNONYM")) -> List[TableMeta]:
        wanted = tuple(k.upper() for k in kinds)
        frags: List[str] = []
        if "TABLE" in wanted:
            frags.append(
                "SELECT OWNER, TABLE_NAME, 'TABLE' AS OBJ_TYPE, NUM_ROWS, LAST_ANALYZED "
                "FROM ALL_TABLES WHERE OWNER = :o"
            )
        if "VIEW" in wanted:
            frags.append("SELECT OWNER, VIEW_NAME, 'VIEW', NULL, NULL FROM ALL_VIEWS WHERE OWNER = :o")
        if "SYNONYM" in wanted:
            frags.append(
                "SELECT OWNER, SYNONYM_NAME, 'SYNONYM', NULL, NULL FROM ALL_SYNONYMS "
                "WHERE OWNER = :o AND TABLE_OWNER IS NOT NULL"
            )
        if not frags:
            return []
        sql = " UNION ALL ".join(frags) + " ORDER BY 2"
        out: List[TableMeta] = []
        cur = self.conn.cursor()
        try:
            cur.execute(sql, o=schema)
            for owner, name, typ, num, analyzed in cur.fetchall():
                out.append(
                    TableMeta(owner=owner, name=name, object_type=typ, num_rows=num,
                              last_analyzed=str(analyzed)[:19] if analyzed else None)
                )
        except Exception as e:
            raise DbError(_friendly_oracle_error(e))
        finally:
            cur.close()
        return out

    def describe_table(self, schema: str, table: str) -> List[ColumnMeta]:
        sql = """
            SELECT C.COLUMN_ID, C.COLUMN_NAME, C.DATA_TYPE, C.DATA_LENGTH,
                   C.DATA_PRECISION, C.DATA_SCALE, C.NULLABLE, C.DATA_DEFAULT, CC.COMMENTS,
                   (SELECT MAX(1) FROM ALL_CONS_COLUMNS K, ALL_CONSTRAINTS S
                     WHERE S.CONSTRAINT_TYPE = 'P' AND S.OWNER = K.OWNER
                       AND S.CONSTRAINT_NAME = K.CONSTRAINT_NAME
                       AND K.OWNER = C.OWNER AND K.TABLE_NAME = C.TABLE_NAME
                       AND K.COLUMN_NAME = C.COLUMN_NAME) AS IS_PK
            FROM ALL_TAB_COLUMNS C
            LEFT JOIN ALL_COL_COMMENTS CC
                   ON CC.OWNER = C.OWNER AND CC.TABLE_NAME = C.TABLE_NAME AND CC.COLUMN_NAME = C.COLUMN_NAME
            WHERE C.OWNER = :o AND C.TABLE_NAME = :t
            ORDER BY C.COLUMN_ID
        """
        cols: List[ColumnMeta] = []
        cur = self.conn.cursor()
        try:
            cur.execute(sql, o=schema, t=table)
            for row in cur.fetchall():
                cid, name, dtype, length, prec, scale, nullable, dflt, comment, is_pk = row
                if isinstance(dflt, str):
                    dflt = dflt.strip()
                cols.append(
                    ColumnMeta(name=name, data_type=dtype, length=length, precision=prec, scale=scale,
                               nullable=(str(nullable).upper() == "Y"), default=dflt,
                               comment=(comment or "").strip(), is_pk=bool(is_pk), position=cid or 0)
                )
        finally:
            cur.close()
        if cols:
            return cols
        # 同义词 / 无权限查字典时退化：用空结果集的列描述
        return self._describe_by_probe(schema, table)

    def _describe_by_probe(self, schema: str, table: str) -> List[ColumnMeta]:
        full = f'{self.quote(schema)}.{self.quote(table)}' if schema else self.quote(table)
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {full} WHERE 1 = 0")
            cols = [
                ColumnMeta(name=d[0], data_type=_from_oracle_type_code(d[1]), length=d[2] or None,
                           precision=d[4] or None, scale=d[5] or None, position=i + 1)
                for i, d in enumerate(cur.description or [])
            ]
            return cols
        except Exception:
            return []
        finally:
            cur.close()

    def table_rowcount(self, schema: str, table: str) -> Optional[int]:
        full = f'{self.quote(schema)}.{self.quote(table)}' if schema else self.quote(table)
        try:
            v = self.query_value(f"SELECT COUNT(*) FROM {full}")
            return int(v) if v is not None else None
        except Exception:
            return None

    def table_ddl(self, schema: str, table: str) -> str:
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT DBMS_METADATA.GET_DDL(:typ, :t, :o) FROM DUAL", typ="TABLE", t=table, o=schema
            )
            row = cur.fetchone()
            cur.close()
            if row:
                v = row[0]
                return v.read() if hasattr(v, "read") else str(v)
        except Exception:
            pass
        return ""

    def table_indexes(self, schema: str, table: str) -> List[dict]:
        sql = """
            SELECT I.INDEX_NAME, I.UNIQUENESS, LISTAGG(IC.COLUMN_NAME, ', ')
                   WITHIN GROUP (ORDER BY IC.COLUMN_POSITION) AS COLS, I.STATUS
            FROM ALL_INDEXES I
            JOIN ALL_IND_COLUMNS IC ON IC.INDEX_NAME = I.INDEX_NAME AND IC.INDEX_OWNER = I.OWNER
            WHERE I.TABLE_OWNER = :o AND I.TABLE_NAME = :t
            GROUP BY I.INDEX_NAME, I.UNIQUENESS, I.STATUS
            ORDER BY I.INDEX_NAME
        """
        out = []
        cur = self.conn.cursor()
        try:
            cur.execute(sql, o=schema, t=table)
            for name, uniq, cols, status in cur.fetchall():
                out.append({"name": name, "unique": uniq, "columns": cols, "status": status})
        except Exception:
            pass
        finally:
            cur.close()
        return out

    def explain(self, sql: str) -> QueryResult:
        """取执行计划。

        两点关键：
        1. 绝不能 rollback —— 用户可能还有未提交的改动，一回滚就全没了。
           EXPLAIN PLAN 写的是会话级的计划表（GTT），不占用用户事务。
        2. 用专属 STATEMENT_ID，避免和上一次的计划混在一起（因此不需要靠
           rollback 来“清空”）。
        """
        import uuid

        stmt = self._prepare(sql)
        sid = "E" + uuid.uuid4().hex[:20].upper()
        cur = self.conn.cursor()
        try:
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' FOR " + stmt)
            cur.execute(
                "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', :sid))",
                sid=sid,
            )
            rows = cur.fetchall()
            return QueryResult(columns=["执行计划"], rows=[(r[0],) for r in rows], kind="SELECT")
        except Exception as e:
            raise DbError("获取执行计划失败：" + str(e))
        finally:
            try:
                cur.close()
            except Exception:
                pass


def _from_oracle_type_code(code: Any) -> str:
    mapping = {
        "DB_TYPE_VARCHAR": "VARCHAR2", "DB_TYPE_NVARCHAR": "NVARCHAR2", "DB_TYPE_CHAR": "CHAR",
        "DB_TYPE_NCHAR": "NCHAR", "DB_TYPE_NUMBER": "NUMBER", "DB_TYPE_DATE": "DATE",
        "DB_TYPE_TIMESTAMP": "TIMESTAMP", "DB_TYPE_CLOB": "CLOB", "DB_TYPE_NCLOB": "NCLOB",
        "DB_TYPE_BLOB": "BLOB", "DB_TYPE_RAW": "RAW", "DB_TYPE_ROWID": "ROWID",
    }
    if hasattr(code, "name"):
        return mapping.get(code.name, code.name)
    return str(code)


# python-oracledb 驱动自身错误（DPY-xxxx）的中文解释
DPY_HINTS = {
    "DPY-3016": (
        "缺少 cryptography 加密库，thin 模式无法建立连接。\n"
        "· 源码运行：pip install cryptography\n"
        "· 便携 exe：请重新打包（打包脚本需带上 cryptography）"
    ),
    "DPY-3010": "数据库版本过低，python-oracledb 只支持 Oracle 12.1 及以上。",
    "DPY-3015": (
        "口令验证方式不被支持。常见于很老的 10g/11g 数据库（只用 10G 验证器）。\n"
        "· 让 DBA 重置一次口令，会生成新的 11G/12C 验证器\n"
        "· 或改用 thick 模式（勾选使用 Instant Client）"
    ),
    "DPY-4005": "用户名或口令错误（也可能是账户被锁）。",
    "DPY-4026": "服务器要求的网络加密/校验算法不支持，请改用 thick 模式。",
    "DPY-6000": "无法连接到监听器：请检查主机、端口、服务名是否正确。",
    "DPY-6005": "连接失败：目标主机端口不通，检查防火墙与监听是否启动。",
    "DPY-1000": "无法解析连接串，请检查主机/端口/服务名格式。",
}


def _friendly_oracle_error(e: Exception) -> str:
    text = str(e)
    m = re.search(r"(ORA-\d+):\s*(.*)", text)
    if m:
        return f"{m.group(1)}: {m.group(2).strip()}"
    dm = re.search(r"(DPY-\d+)", text)
    if dm:
        code = dm.group(1)
        hint = DPY_HINTS.get(code)
        if hint:
            return f"{text}\n\n原因：{hint}"
        return text
    return text


# ================================================================== 演示库
DEMO_SCHEMA = "DEMO"

DEMO_DDL = """
CREATE TABLE DEMO.DEPARTMENTS (
  DEPT_ID     NUMBER(4) PRIMARY KEY,
  DEPT_NAME   VARCHAR2(60),
  CITY        VARCHAR2(40),
  CREATE_DATE DATE
);
CREATE TABLE DEMO.EMPLOYEES (
  EMP_ID     NUMBER(6) PRIMARY KEY,
  EMP_NAME   VARCHAR2(60),
  DEPT_ID    NUMBER(4),
  JOB        VARCHAR2(40),
  SALARY     NUMBER(10,2),
  HIRE_DATE  DATE,
  EMAIL      VARCHAR2(80),
  IS_ACTIVE  NUMBER(1)
);
CREATE TABLE DEMO.ORDERS (
  ORDER_ID   NUMBER(10) PRIMARY KEY,
  CUSTOMER   VARCHAR2(60),
  EMP_ID     NUMBER(6),
  AMOUNT     NUMBER(12,2),
  ORDER_DATE DATE,
  STATUS     VARCHAR2(20)
);
CREATE VIEW DEMO.V_EMP_DETAIL AS
  SELECT E.EMP_ID, E.EMP_NAME, D.DEPT_NAME, E.JOB, E.SALARY, E.HIRE_DATE
  FROM DEMO.EMPLOYEES E LEFT JOIN DEMO.DEPARTMENTS D ON D.DEPT_ID = E.DEPT_ID;
"""

_CITIES = ["北京", "上海", "广州", "深圳", "成都", "杭州", "西安", "武汉"]
_JOBS = ["销售经理", "开发工程师", "测试工程师", "财务专员", "人事专员", "产品经理", "运维工程师", "数据分析师"]
_SURNAME = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张"
_GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇", "军", "杰", "涛", "明", "霞", "平"]
_STATUS = ["已付款", "待发货", "已发货", "已完成", "已取消"]


class DemoAdapter(DbAdapter):
    """内置演示库：SQLite 内存数据库，语法尽量贴近 Oracle 以便演练。"""

    dialect = "sqlite"

    def __init__(self, profile: ConnProfile):
        super().__init__(profile)
        import sqlite3

        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.current_user = "DEMO"
        self.current_schema = "DEMO"
        self.server_version = "演示库 1.0"
        self._bootstrap()

    def _bootstrap(self):
        import random
        from datetime import datetime, timedelta

        random.seed(20240)
        cur = self.conn.cursor()
        cur.execute(f'ATTACH DATABASE ":memory:" AS "{DEMO_SCHEMA}"')
        cur.executescript(DEMO_DDL)
        base = datetime(2024, 1, 1)

        depts = []
        for i in range(1, 9):
            depts.append((i, f"{_CITIES[i-1]}分公司", _CITIES[i-1], (base + timedelta(days=i * 7)).strftime("%Y-%m-%d %H:%M:%S")))
        cur.executemany("INSERT INTO DEMO.DEPARTMENTS VALUES (?,?,?,?)", depts)

        emps = []
        for i in range(1, 121):
            name = random.choice(_SURNAME) + random.choice(_GIVEN)
            dept = random.randint(1, 8)
            job = random.choice(_JOBS)
            salary = round(random.uniform(6000, 45000), 2)
            hire = (base + timedelta(days=random.randint(0, 900))).strftime("%Y-%m-%d %H:%M:%S")
            emps.append((i, name, dept, job, salary, hire, f"user{i:03d}@example.com", random.randint(0, 1)))
        cur.executemany("INSERT INTO DEMO.EMPLOYEES VALUES (?,?,?,?,?,?,?,?)", emps)

        orders = []
        for i in range(1, 301):
            orders.append((
                i, f"客户{random.randint(1, 60):03d}", random.randint(1, 120),
                round(random.uniform(100, 200000), 2),
                (base + timedelta(days=random.randint(0, 700))).strftime("%Y-%m-%d %H:%M:%S"),
                random.choice(_STATUS),
            ))
        cur.executemany("INSERT INTO DEMO.ORDERS VALUES (?,?,?,?,?,?)", orders)
        self.conn.commit()

    def open(self):
        pass

    # ---- 执行 ----
    @staticmethod
    def _prepare(sql: str) -> str:
        s = (sql or "").strip().rstrip(";").strip()
        # 兼容常见 Oracle 写法，方便演示环境里也能跑
        s = re.sub(r"\bNVL\(", "IFNULL(", s, flags=re.IGNORECASE)
        s = re.sub(r"\bSYSDATE\b", "datetime('now')", s, flags=re.IGNORECASE)
        # 只替换“整条语句末尾”的 ROWNUM：嵌套子查询里的 ROWNUM 也替换会拼出非法 SQL
        s = re.sub(r"\bROWNUM\s*<=\s*(\d+)\s*$", r"1=1 LIMIT \1", s, flags=re.IGNORECASE)
        return s

    def query(self, sql: str, max_rows: Optional[int] = None) -> QueryResult:
        started = time.time()
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        try:
            cur.execute(stmt)
        except Exception as e:
            raise DbError(f"SQL 错误：{e}")
        if cur.description is None:
            rc = cur.rowcount
            self.conn.commit()
            return QueryResult(columns=[], rows=[], elapsed=time.time() - started,
                               message=f"语句执行成功，影响行数：{rc}", kind="DML", statement=stmt)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
        truncated = False
        if max_rows and len(rows) >= max_rows:
            if cur.fetchone() is not None:
                truncated = True
        return QueryResult(columns=cols, rows=rows, elapsed=time.time() - started,
                           truncated=truncated, kind="SELECT", statement=stmt)

    def execute(self, sql: str, params: Optional[dict] = None) -> int:
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        try:
            if params:
                cur.execute(stmt, {k.lstrip(":"): v for k, v in params.items()})
            else:
                cur.execute(stmt)
            rc = cur.rowcount
            self.conn.commit()
            return rc
        except Exception as e:
            raise DbError(f"SQL 错误：{e}")

    def execute_many(self, sql: str, seq: List[dict]) -> int:
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        total = 0
        for p in seq:
            cur.execute(stmt, {k.lstrip(":"): v for k, v in p.items()})
            total += cur.rowcount
        self.conn.commit()
        return total

    def stream(self, sql: str, chunk: int = 1000):
        stmt = self._prepare(sql)
        cur = self.conn.cursor()
        cur.execute(stmt)
        cols = [d[0] for d in (cur.description or [])]
        while True:
            batch = cur.fetchmany(chunk)
            if not batch:
                break
            yield cols, batch

    def cancel(self):
        try:
            self.conn.interrupt()
        except Exception:
            pass

    # ---- 元数据 ----
    def list_schemas(self) -> List[str]:
        return ["DEMO"]

    def list_objects(self, schema: str, kinds=("TABLE", "VIEW", "SYNONYM")) -> List[TableMeta]:
        wanted = tuple(k.upper() for k in kinds)
        cur = self.conn.cursor()
        cur.execute(f'SELECT NAME, TYPE FROM {DEMO_SCHEMA}.sqlite_master '
                    f"WHERE NAME NOT LIKE 'sqlite_%' ORDER BY NAME")
        out = []
        for name, typ in cur.fetchall():
            obj_type = "TABLE" if typ == "table" else "VIEW"
            if obj_type not in wanted:
                continue
            cnt = None
            try:
                cnt = self.query_value(f'SELECT COUNT(*) FROM "{DEMO_SCHEMA}"."{name}"')
            except Exception:
                pass
            out.append(TableMeta(owner=DEMO_SCHEMA, name=name, object_type=obj_type, num_rows=cnt))
        return out

    def describe_table(self, schema: str, table: str) -> List[ColumnMeta]:
        cur = self.conn.cursor()
        cur.execute(f'PRAGMA "{DEMO_SCHEMA}".table_info("{table}")')
        cols = []
        for row in cur.fetchall():
            cid, name, ctype, notnull, dflt, pk = row
            dtype, length, prec, scale = _parse_sqlite_type(ctype or "TEXT")
            cols.append(
                ColumnMeta(name=name, data_type=dtype, length=length, precision=prec, scale=scale,
                           nullable=not notnull, default=dflt, is_pk=bool(pk), position=cid + 1)
            )
        return cols

    def table_rowcount(self, schema: str, table: str) -> Optional[int]:
        try:
            return int(self.query_value(f'SELECT COUNT(*) FROM "{DEMO_SCHEMA}"."{table}"'))
        except Exception:
            return None

    def table_ddl(self, schema: str, table: str) -> str:
        cur = self.conn.cursor()
        cur.execute(f"SELECT SQL FROM {DEMO_SCHEMA}.sqlite_master WHERE NAME = ?", (table,))
        row = cur.fetchone()
        return row[0] if row else ""

    def table_indexes(self, schema: str, table: str) -> List[dict]:
        cur = self.conn.cursor()
        cur.execute(f'PRAGMA "{DEMO_SCHEMA}".index_list("{table}")')
        out = []
        for name, uniq, origin, partial in cur.fetchall():
            cur2 = self.conn.cursor()
            cur2.execute(f'PRAGMA "{DEMO_SCHEMA}".index_info("{name}")')
            cols = ",".join(r[2] for r in cur2.fetchall() if r[2])
            out.append({"name": name, "unique": "UNIQUE" if uniq else "NONUNIQUE", "columns": cols, "status": "VALID"})
        return out

    def literal(self, value: Any, data_type: str = "VARCHAR2") -> str:
        from .sqlbuilder import literal

        return literal(value, data_type, "sqlite")


def _parse_sqlite_type(t: str) -> Tuple[str, Optional[int], Optional[int], Optional[int]]:
    m = re.match(r"^\s*([A-Za-z0-9_ ]+?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$", t or "")
    if not m:
        return (t or "TEXT").upper(), None, None, None
    base = m.group(1).strip().upper()
    if m.group(3):
        return base, None, int(m.group(2)), int(m.group(3))
    if m.group(2):
        return base, int(m.group(2)), None, None
    return base, None, None, None


# ================================================================== 会话
class DbSession:
    """一个已打开的数据库连接，附带元数据缓存。"""

    def __init__(self, profile: ConnProfile):
        self.profile = profile
        self.adapter: DbAdapter = DemoAdapter(profile) if profile.demo else OracleAdapter(profile)
        self._schema_cache: Optional[List[str]] = None
        self._obj_cache: Dict[str, List[TableMeta]] = {}
        self._col_cache: Dict[Tuple[str, str], List[ColumnMeta]] = {}
        self._fk_cache: Dict[Tuple[str, str], List[dict]] = {}
        self._cache_ts: Dict[Any, float] = {}
        # 元数据缓存有效期：连着看几个小时时，别人新建的表/改过的字段要能自己冒出来
        self.cache_ttl: float = 300.0
        self.last_error = ""

    def _expired(self, key: Any) -> bool:
        if self.cache_ttl <= 0:
            return True
        ts = self._cache_ts.get(key)
        return ts is None or (time.time() - ts) > self.cache_ttl

    # ---- 生命周期 ----
    def open(self) -> None:
        self.adapter.open()

    def close(self) -> None:
        self.adapter.close()
        self._obj_cache.clear()
        self._col_cache.clear()
        self._cache_ts.clear()

    @property
    def is_open(self) -> bool:
        return self.adapter.is_open or isinstance(self.adapter, DemoAdapter)

    @property
    def label(self) -> str:
        p = self.profile
        tail = f"@{p.host}" if not p.demo else ""
        return f"{p.name}（{p.user}{tail}）"

    @property
    def dialect(self) -> str:
        return self.adapter.dialect

    # ---- 透传 ----
    def query(self, sql: str, max_rows: Optional[int] = None) -> QueryResult:
        return self.adapter.query(sql, max_rows)

    def execute(self, sql: str, params: Optional[dict] = None) -> int:
        return self.adapter.execute(sql, params)

    def execute_many(self, sql: str, seq: List[dict]) -> int:
        return self.adapter.execute_many(sql, seq)

    def stream(self, sql: str, chunk: int = 1000):
        return self.adapter.stream(sql, chunk)

    def commit(self):
        self.adapter.commit()

    def rollback(self):
        self.adapter.rollback()

    def cancel(self):
        self.adapter.cancel()

    def explain(self, sql: str) -> QueryResult:
        return self.adapter.explain(sql)

    def query_value(self, sql: str, default: Any = None) -> Any:
        return self.adapter.query_value(sql, default)

    def literal(self, value: Any, data_type: str = "VARCHAR2") -> str:
        return self.adapter.literal(value, data_type)

    def paginate(self, sql: str, offset: int, limit: int) -> str:
        return self.adapter.paginate(sql, offset, limit)

    # ---- 元数据（带缓存） ----
    def schemas(self, refresh: bool = False) -> List[str]:
        if self._schema_cache is None or refresh:
            try:
                self._schema_cache = self.adapter.list_schemas()
            except Exception as e:
                self._schema_cache = [self.adapter.current_schema or ""] if (self.adapter.current_schema or "") else []
        return list(self._schema_cache)

    @property
    def default_schema(self) -> str:
        return self.profile.schema.strip().upper() or self.adapter.current_schema or self.adapter.current_user

    def objects(self, schema: str, refresh: bool = False) -> List[TableMeta]:
        key = (schema or "").upper()
        if refresh or key not in self._obj_cache or self._expired(key):
            try:
                self._obj_cache[key] = self.adapter.list_objects(key)
                self._cache_ts[key] = time.time()
            except Exception as e:
                raise DbError(str(e))
        return self._obj_cache[key]

    def columns(self, schema: str, table: str, refresh: bool = False) -> List[ColumnMeta]:
        key = ((schema or "").upper(), (table or "").upper())
        if refresh or key not in self._col_cache or self._expired(key):
            self._col_cache[key] = self.adapter.describe_table(key[0], key[1])
            self._cache_ts[key] = time.time()
        return self._col_cache[key]

    def column_types(self, schema: str, table: str) -> Dict[str, str]:
        return {c.name.upper(): c.data_type for c in self.columns(schema, table)}

    def col_type(self, schema: str, table: str, column: str) -> str:
        return self.column_types(schema, table).get((column or "").upper(), "VARCHAR2")

    def rowcount(self, schema: str, table: str) -> Optional[int]:
        return self.adapter.table_rowcount(schema, table)

    def ddl(self, schema: str, table: str) -> str:
        return self.adapter.table_ddl(schema, table)

    def indexes(self, schema: str, table: str) -> List[dict]:
        return self.adapter.table_indexes(schema, table)

    def foreign_keys(self, schema: str, table: str) -> List[dict]:
        """该表指向别的表的外键关系。

        用于跨表钻取时自动推荐目标字段。权限不足（没有 dba_/all_ 视图权限）
        或查询失败时返回空列表，由调用方退化成“按字段名猜测”，绝不抛异常。
        """
        schema = (schema or "").strip()
        table = (table or "").strip()
        if not table:
            return []
        key = ((schema or "").upper(), table.upper())
        if key in self._fk_cache:
            return self._fk_cache[key]
        out: List[dict] = []
        esc = lambda s: str(s).replace("'", "''")  # noqa: E731
        for prefix in ("dba", "all", "user"):
            where_owner = f" AND c.owner = '{esc(schema)}'" if prefix != "user" else ""
            sql = (
                f"SELECT cc.column_name, rc.table_name AS r_table, "
                f"rc.owner AS r_owner, rcol.column_name AS r_column "
                f"FROM {prefix}_constraints c "
                f"JOIN {prefix}_cons_columns cc "
                f"  ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
                f"JOIN {prefix}_constraints rc "
                f"  ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name "
                f"JOIN {prefix}_cons_columns rcol "
                f"  ON rcol.owner = rc.owner AND rcol.constraint_name = rc.constraint_name "
                f"     AND rcol.position = cc.position "
                f"WHERE c.constraint_type = 'R' AND c.table_name = '{esc(table)}'{where_owner}"
            )
            try:
                res = self.query(sql, max_rows=200)
            except Exception:  # noqa: BLE001
                continue
            for r in res.rows or []:
                out.append({"column": r[0], "ref_table": r[1],
                            "ref_schema": r[2], "ref_column": r[3]})
            if out:
                break
        self._fk_cache[key] = out
        return out

    def invalidate(self, schema: str = ""):
        if schema:
            self._obj_cache.pop((schema or "").upper(), None)
            self._schema_cache = None
            for k in [k for k in self._col_cache if k[0] == (schema or "").upper()]:
                self._col_cache.pop(k, None)
                self._cache_ts.pop(k, None)
            for k in [k for k in self._fk_cache if k[0] == (schema or "").upper()]:
                self._fk_cache.pop(k, None)
            self._cache_ts.pop((schema or "").upper(), None)
        else:
            self._obj_cache.clear()
            self._col_cache.clear()
            self._fk_cache.clear()
            self._cache_ts.clear()
            self._schema_cache = None


def create_session(profile: ConnProfile) -> DbSession:
    s = DbSession(profile)
    s.open()
    return s
