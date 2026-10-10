"""运维 SQL 模板库（只读诊断）。

设计的四条原则：
  1. **只做只读诊断** —— 本模块不收录任何会改数据的语句。需要“动手”的场景
     （杀会话、收缩表），只在结果里给出语句文本让人复制走，程序不代执行。
  2. **权限声明是一等公民** —— 每条模板声明自己需要哪些视图（`requires`），
     跑不了要明说“需要 SELECT_CATALOG_ROLE”，而不是丢一个 ORA-00942。
  3. **参数用文本占位 `{{name}}` 替换** —— 渲染出来的是可以直接复制到
     sqlplus / PL/SQL Developer 里跑的整段 SQL，不走绑定变量。
     代价是必须做严格的类型校验，这部分由 `Param.literal()` 兜住。
  4. **结果要带“怎么看”** —— 每条模板有 `hint`，说明哪些列超过什么值要警惕。
     没有解读的 SQL 片段跟网上随便搜一段没区别。

版本门槛说明：`min_version` 只用于**隐藏明确跑不通的模板**（如 11g 才有的
V$DIAG_ALERT_EXT）。版本号解析不出来时一律放行 —— 宁可让人试一下失败，
也不要因为猜错版本把能用的模板藏起来。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import data_dir
from .perms import CAP_BY_KEY

# 模板里的参数占位符：{{name}}
PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# 字符串参数长度上限：避免误填一整段文本进去
MAX_STRING_LEN = 200

# 分类顺序即界面上的展示顺序
CATEGORIES = ["快速体检", "会话与锁", "SQL 性能", "空间与存储", "对象与结构", "容量与增长"]


class TemplateError(Exception):
    """模板定义错误或参数校验失败。"""


# ================================================================== 参数
# 类型说明：
#   int/float  强校验数值，越界拒绝 —— 这是防止注入的主要手段
#   string     单引号转义 + 长度上限，渲染成 '字面量'
#   opt_string 可留空：空值渲染成 NULL，配合模板里
#              "({{owner}} IS NULL OR OWNER = UPPER({{owner}}))" 实现“留空不过滤”
#   enum       只能从 values 里选
#   bool       渲染成 'Y' / 'N'
@dataclass
class Param:
    name: str
    label: str
    type: str = "string"
    default: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    values: List[str] = field(default_factory=list)
    hint: str = ""

    def validate(self, raw: Any) -> Tuple[bool, str]:
        """返回 (是否通过, 错误说明)。"""
        text = "" if raw is None else str(raw).strip()
        if self.type == "int":
            try:
                v: float = int(text)
            except (TypeError, ValueError):
                return False, f"“{self.label}”必须是整数"
            return self._range(v)
        if self.type == "float":
            try:
                v = float(text)
            except (TypeError, ValueError):
                return False, f"“{self.label}”必须是数字"
            return self._range(v)
        if self.type == "enum":
            if text not in self.values:
                return False, f"“{self.label}”必须从给定选项中选择"
            return True, ""
        if self.type == "bool":
            return True, ""
        # string / opt_string
        if self.type != "opt_string" and not text:
            return False, f"“{self.label}”不能为空"
        if len(text) > MAX_STRING_LEN:
            return False, f"“{self.label}”过长（最多 {MAX_STRING_LEN} 个字符）"
        # 拒绝会破坏 SQL 结构的字符（分号提前结束语句，换行影响注释）
        if ";" in text:
            return False, f"“{self.label}”不能包含分号"
        return True, ""

    def _range(self, v: float) -> Tuple[bool, str]:
        if self.min is not None and v < self.min:
            return False, f"“{self.label}”不能小于 {self.min:g}"
        if self.max is not None and v > self.max:
            return False, f"“{self.label}”不能大于 {self.max:g}"
        return True, ""

    def literal(self, raw: Any) -> str:
        """渲染成 SQL 字面量（已完成校验，调用前必须先 validate）。"""
        text = "" if raw is None else str(raw).strip()
        if self.type == "int":
            return str(int(float(text)))
        if self.type == "float":
            return str(float(text))
        if self.type == "bool":
            return "'Y'" if text.lower() in ("1", "y", "yes", "true", "是") else "'N'"
        if self.type == "opt_string" and not text:
            return "NULL"
        # string / enum / 非空 opt_string：单引号翻倍转义是 Oracle 的标准做法
        return "'" + text.replace("'", "''") + "'"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("name", "label", "type", "default", "min", "max", "values", "hint")}

    @staticmethod
    def from_dict(d: dict) -> "Param":
        return Param(
            name=str(d.get("name", "")),
            label=str(d.get("label", "") or d.get("name", "")),
            type=str(d.get("type", "string")),
            default="" if d.get("default") is None else str(d.get("default")),
            min=d.get("min"),
            max=d.get("max"),
            values=list(d.get("values") or []),
            hint=str(d.get("hint", "")),
        )


# ================================================================== 模板
@dataclass
class Template:
    id: str
    name: str
    category: str
    sql: str
    params: List[Param] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    fallback: Optional[dict] = None      # {"requires": [...], "sql": "...", "note": "..."}
    min_version: str = ""                # 低于该版本隐藏，如 "11.2"
    hint: str = ""                       # 结果的解读说明
    keywords: str = ""                   # 搜索用补充关键词
    source: str = "builtin"              # builtin / user

    # ---------------- 参数 ----------------
    def param(self, name: str) -> Optional[Param]:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def placeholders(self) -> List[str]:
        """SQL 与降级 SQL 里出现过的全部占位符。"""
        names = set(PARAM_RE.findall(self.sql or ""))
        if self.fallback:
            names |= set(PARAM_RE.findall(self.fallback.get("sql", "") or ""))
        return sorted(names)

    def check_params(self, values: Dict[str, Any]) -> Tuple[bool, str]:
        """校验全部参数，返回 (是否通过, 第一条错误)。"""
        for p in self.params:
            ok, err = p.validate(values.get(p.name, p.default))
            if not ok:
                return False, err
        return True, ""

    def render(self, values: Optional[Dict[str, Any]] = None,
               use_fallback: bool = False) -> str:
        """渲染出可直接执行的 SQL。

        未知占位符会被原样保留并报错 —— 模板写错名字时宁可当场失败，
        也不要生成一条看起来正常、实际拼错的 SQL。
        """
        values = dict(values or {})
        base = (self.fallback or {}).get("sql", "") if use_fallback else self.sql
        ok, err = self.check_params(values)
        if not ok:
            raise TemplateError(err)

        def sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            p = self.param(name)
            if p is None:
                raise TemplateError(f"模板“{self.name}”使用了未定义的参数：{name}")
            return p.literal(values.get(name, p.default))

        out = PARAM_RE.sub(sub, base or "")
        unknown = PARAM_RE.findall(out)
        if unknown:
            raise TemplateError(f"模板“{self.name}”有未定义的参数：{', '.join(sorted(set(unknown)))}")
        return out.strip()

    # ---------------- 权限 ----------------
    def missing_caps(self, probe, requires: Optional[Sequence[str]] = None) -> List[str]:
        """当前账号缺哪些能力。probe 为空表示不检查。"""
        reqs = list(requires if requires is not None else self.requires)
        if probe is None:
            return []
        return [k for k in reqs if not probe.has(k)]

    def resolve(self, probe) -> Tuple[str, str, bool]:
        """挑一条当前账号能跑的语句。

        返回 (SQL, 说明, 是否可用)。主 SQL 用不了时退回 fallback，
        都没有则如实说明缺什么权限 —— 绝不静默失败。
        """
        missing = self.missing_caps(probe)
        if not missing:
            return self.sql, "", True
        if self.fallback:
            fb_missing = self.missing_caps(probe, self.fallback.get("requires", []))
            if not fb_missing:
                note = self.fallback.get("note", "") or "已改用简化版本"
                return self.fallback.get("sql", ""), note, True
        need = "；".join(CAP_BY_KEY[k]["need"] for k in missing if k in CAP_BY_KEY)
        lack = "、".join(CAP_BY_KEY[k]["label"] for k in missing if k in CAP_BY_KEY)
        detail = f"当前账号看不到{'（' + lack + '）' if lack else '所需视图'}"
        if need:
            detail += f"，需要：{need}"
        return self.sql, detail, False

    # ---------------- 版本 ----------------
    def version_ok(self, server_version: str = "") -> bool:
        if not self.min_version:
            return True
        actual = parse_version(server_version or "")
        if actual is None:      # 解析不出版本就放行，不猜
            return True
        need = parse_version(self.min_version) or (0, 0)
        return actual >= need

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category, "sql": self.sql,
            "params": [p.to_dict() for p in self.params],
            "requires": list(self.requires), "fallback": self.fallback,
            "min_version": self.min_version, "hint": self.hint,
            "keywords": self.keywords, "source": self.source,
        }

    @staticmethod
    def from_dict(d: dict) -> "Template":
        fb = d.get("fallback")
        return Template(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            category=str(d.get("category", "快速体检")),
            sql=str(d.get("sql", "")),
            params=[Param.from_dict(p) for p in (d.get("params") or [])],
            requires=list(d.get("requires") or []),
            fallback=dict(fb) if isinstance(fb, dict) else None,
            min_version=str(d.get("min_version", "")),
            hint=str(d.get("hint", "")),
            keywords=str(d.get("keywords", "")),
            source=str(d.get("source", "user")),
        )

    def search_text(self) -> str:
        return " ".join([self.name, self.category, self.keywords, self.hint,
                         self.id]).lower()


# ================================================================== 版本解析
def parse_version(text: str) -> Optional[Tuple[int, int]]:
    """从各种版本串里取出 (主版本, 次版本)，取不到返回 None。

    能识别：'19.3.0.0.0'、'Oracle Database 19c Enterprise Edition'、
    '11.2.0.4.0'、'Release 12.1.0.2.0'。
    """
    if not text:
        return None
    s = str(text)
    m = re.search(r"(\d+)\s*[cC]\b", s)          # 19c / 12c
    if m:
        return (int(m.group(1)), 0)
    m = re.search(r"(\d+)\.(\d+)", s)            # 19.3.0.0.0
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{2})\b", s)             # 只有 "19"
    if m:
        return (int(m.group(1)), 0)
    return None


# ================================================================== 内置模板
def _p(name: str, label: str, **kw) -> Param:
    return Param(name=name, label=label, **kw)


def _owner_param() -> Param:
    """属主过滤：留空 = 全库。模板里配合 "({{owner}} IS NULL OR ...)" 使用。"""
    return _p("owner", "属主（留空=全库）", type="opt_string", default="",
              hint="填 schema 名只查该用户下的对象")


def _top_param(default: int = 50) -> Param:
    return _p("top_n", "取前几条", type="int", default=str(default), min=1, max=5000)


BUILTIN: List[Template] = [
    # ============================================================ 快速体检
    Template(
        id="instance_overview",
        name="实例概览",
        category="快速体检",
        requires=["v_instance", "v_database"],
        fallback=dict(
            requires=["v_version"],
            sql="SELECT SUBSTR(BANNER, 1, 120) AS \"版本信息\"\n"
                "FROM   V$VERSION\n"
                "WHERE  ROWNUM <= 4",
            note="看不到 V$INSTANCE / V$DATABASE，只能显示版本信息",
        ),
        sql="SELECT di.INSTANCE_NAME AS \"实例名\", di.HOST_NAME AS \"主机名\",\n"
            "       db.NAME AS \"数据库名\", di.VERSION AS \"版本\",\n"
            "       di.STATUS AS \"实例状态\", db.OPEN_MODE AS \"打开模式\",\n"
            "       db.DATABASE_ROLE AS \"数据库角色\", db.LOG_MODE AS \"归档模式\",\n"
            "       TO_CHAR(di.STARTUP_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"启动时间\",\n"
            "       ROUND(SYSDATE - di.STARTUP_TIME, 2) AS \"已运行天数\"\n"
            "FROM   V$INSTANCE di, V$DATABASE db",
        hint="已运行天数突然变小说明实例被重启过；归档模式为 NOARCHIVELOG 时无法做时间点恢复。",
        keywords="实例 启动时间 归档 版本 实例状态",
    ),
    Template(
        id="whoami",
        name="当前连接身份",
        category="快速体检",
        requires=["basic"],
        sql="SELECT USER AS \"当前用户\",\n"
            "       SYS_CONTEXT('USERENV','CURRENT_SCHEMA') AS \"当前Schema\",\n"
            "       SYS_CONTEXT('USERENV','DB_NAME') AS \"数据库名\",\n"
            "       SYS_CONTEXT('USERENV','HOST') AS \"服务器主机\",\n"
            "       SYS_CONTEXT('USERENV','IP_ADDRESS') AS \"客户端IP\",\n"
            "       SYS_CONTEXT('USERENV','MODULE') AS \"客户端模块\",\n"
            "       TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') AS \"数据库当前时间\"\n"
            "FROM   DUAL",
        hint="连错库是真实风险：执行任何操作前先确认“数据库名 / 服务器主机”这两列。",
        keywords="我是谁 当前用户 schema 连错库 客户端IP",
    ),
    Template(
        id="nls_time",
        name="字符集与时区",
        category="快速体检",
        requires=["basic"],
        sql="SELECT '数据库时区' AS \"项\", DBTIMEZONE AS \"值\" FROM DUAL\n"
            "UNION ALL SELECT '会话时区', SESSIONTIMEZONE FROM DUAL\n"
            "UNION ALL SELECT '数据库当前时间', TO_CHAR(SYSTIMESTAMP,'YYYY-MM-DD HH24:MI:SS TZH:TZM') FROM DUAL\n"
            "UNION ALL SELECT '会话当前时间', TO_CHAR(CURRENT_TIMESTAMP,'YYYY-MM-DD HH24:MI:SS TZH:TZM') FROM DUAL\n"
            "UNION ALL SELECT PARAMETER, VALUE\n"
            "       FROM NLS_DATABASE_PARAMETERS\n"
            "       WHERE PARAMETER IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET',\n"
            "                           'NLS_DATE_FORMAT','NLS_TIMESTAMP_FORMAT',\n"
            "                           'NLS_LANGUAGE','NLS_TERRITORY')\n"
            "ORDER  BY 1",
        hint="数据库时区与会话时区不一致时，SYSDATE 和 CURRENT_TIMESTAMP 会差几个小时 —— "
             "按时间查数据出问题先来这里核对。",
        keywords="字符集 乱码 时区 NLS 日期格式",
    ),
    Template(
        id="alert_errors",
        name="最近告警日志里的 ORA- 错误",
        category="快速体检",
        requires=["v_diag_alert_ext"],
        min_version="11.1",
        params=[_p("days", "最近几天", type="int", default="2", min=1, max=30),
                _top_param(200)],
        sql="SELECT * FROM (\n"
            "  SELECT TO_CHAR(a.ORIGINATING_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') AS \"时间\",\n"
            "         TRIM(a.MESSAGE_TEXT) AS \"告警内容\"\n"
            "  FROM   V$DIAG_ALERT_EXT a\n"
            "  WHERE  a.ORIGINATING_TIMESTAMP >= SYSDATE - {{days}}\n"
            "    AND  a.MESSAGE_TEXT LIKE 'ORA-%'\n"
            "  ORDER  BY a.ORIGINATING_TIMESTAMP DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="需要 11g 及以上（V$DIAG_ALERT_EXT）。同一错误密集重复基本就是当前正在发生的问题；"
             "偶发一条可以忽略。",
        keywords="告警 alert ORA 报错 日志 11g",
    ),
    Template(
        id="resource_usage",
        name="资源与连接数使用率",
        category="快速体检",
        requires=["v_resource_limit"],
        sql="SELECT r.RESOURCE_NAME AS \"资源项\",\n"
            "       r.CURRENT_UTILIZATION AS \"当前用量\",\n"
            "       r.MAX_UTILIZATION AS \"历史峰值\",\n"
            "       r.LIMIT_VALUE AS \"上限\",\n"
            "       CASE WHEN REGEXP_LIKE(r.LIMIT_VALUE, '^[0-9]+$')\n"
            "            THEN ROUND(r.CURRENT_UTILIZATION / TO_NUMBER(r.LIMIT_VALUE) * 100, 1)\n"
            "       END AS \"使用率%\"\n"
            "FROM   V$RESOURCE_LIMIT r\n"
            "WHERE  r.RESOURCE_NAME IN ('processes','sessions','transactions',\n"
            "                           'enqueue_locks','enqueue_resources','sort_segment_locks')\n"
            "ORDER  BY 5 DESC NULLS LAST",
        hint=" processes / sessions 使用率超过 85% 就要警惕，到 100% 新连接会直接被拒"
             "（ORA-00020）。历史峰值比当前用量更能说明是否曾经打满过。",
        keywords="连接数 打满 sessions processes ORA-00020 资源",
    ),
    Template(
        id="archive_status",
        name="归档模式与最近归档量",
        category="快速体检",
        requires=["v_archived_log", "v_database"],
        params=[_p("days", "最近几天", type="int", default="3", min=1, max=90)],
        sql="SELECT (SELECT LOG_MODE FROM V$DATABASE) AS \"归档模式\",\n"
            "       COUNT(*) AS \"归档个数\",\n"
            "       TO_CHAR(MAX(FIRST_TIME), 'YYYY-MM-DD HH24:MI:SS') AS \"最近一次归档\",\n"
            "       ROUND(SUM(BLOCKS * BLOCK_SIZE) / 1048576, 1) AS \"归档总量MB\"\n"
            "FROM   V$ARCHIVED_LOG\n"
            "WHERE  DEST_ID = 1\n"
            "  AND  FIRST_TIME >= SYSDATE - {{days}}",
        hint="归档量突然暴涨，通常是有人跑了大批量 DML 或导入。归档目录写满会导致整个库挂起。",
        keywords="归档 archivelog 日志切换 归档量",
    ),

    # ============================================================ 会话与锁
    Template(
        id="session_by_program",
        name="会话分布（按用户/程序/机器）",
        category="会话与锁",
        requires=["v_session"],
        params=[_top_param(30)],
        sql="SELECT * FROM (\n"
            "  SELECT NVL(s.USERNAME, '(后台进程)') AS \"用户\",\n"
            "         NVL(s.PROGRAM, '(未知)') AS \"程序\",\n"
            "         NVL(s.MACHINE, '(未知)') AS \"机器\",\n"
            "         NVL(s.STATUS, '(空)') AS \"状态\",\n"
            "         COUNT(*) AS \"会话数\"\n"
            "  FROM   V$SESSION s\n"
            "  GROUP  BY s.USERNAME, s.PROGRAM, s.MACHINE, s.STATUS\n"
            "  ORDER  BY COUNT(*) DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="某个程序名下会话数异常多，多半是连接池配置过大或连接没释放。",
        keywords="会话 连接 分布 谁连着 连接池",
    ),
    Template(
        id="blocking_tree",
        name="谁堵了谁（阻塞链）",
        category="会话与锁",
        requires=["v_session"],
        params=[_p("seconds", "阻塞超过(秒)", type="int", default="30", min=0, max=86400)],
        sql="SELECT w.SID AS \"被阻塞SID\", w.SERIAL# AS \"被阻塞序号\",\n"
            "       w.USERNAME AS \"被阻塞用户\", w.PROGRAM AS \"被阻塞程序\",\n"
            "       w.MACHINE AS \"被阻塞机器\", w.EVENT AS \"等待事件\",\n"
            "       w.SECONDS_IN_WAIT AS \"已等待秒\", w.SQL_ID AS \"被阻塞SQL\",\n"
            "       b.SID AS \"阻塞者SID\", b.SERIAL# AS \"阻塞者序号\",\n"
            "       b.USERNAME AS \"阻塞者用户\", b.PROGRAM AS \"阻塞者程序\",\n"
            "       b.MACHINE AS \"阻塞者机器\", b.SQL_ID AS \"阻塞者SQL\",\n"
            "       CASE WHEN b.BLOCKING_SESSION IS NULL THEN '阻塞源头'\n"
            "            ELSE '它也被别人堵着' END AS \"说明\"\n"
            "FROM   V$SESSION w, V$SESSION b\n"
            "WHERE  w.BLOCKING_SESSION = b.SID\n"
            "  AND  w.SECONDS_IN_WAIT >= {{seconds}}\n"
            "ORDER  BY w.SECONDS_IN_WAIT DESC",
        hint="只有“说明=阻塞源头”的会话才是要处理的根源。杀中间节点只是把等待转移，"
             "问题会立刻复现 —— 顺着源头往上找，通常是某个忘了提交的事务。",
        keywords="阻塞 锁表 blocking 等待会话 卡住",
    ),
    Template(
        id="long_transaction",
        name="长事务（很久没提交）",
        category="会话与锁",
        requires=["v_transaction", "v_session"],
        params=[_p("minutes", "已运行超过(分钟)", type="int", default="10", min=1, max=100000)],
        sql="SELECT s.SID AS \"SID\", s.SERIAL# AS \"序号\", s.USERNAME AS \"用户\",\n"
            "       s.PROGRAM AS \"程序\", s.MACHINE AS \"机器\",\n"
            "       TO_CHAR(TO_DATE(t.START_TIME, 'MM/DD/YY HH24:MI:SS'),\n"
            "               'YYYY-MM-DD HH24:MI:SS') AS \"事务开始\",\n"
            "       ROUND((SYSDATE - TO_DATE(t.START_TIME, 'MM/DD/YY HH24:MI:SS')) * 24 * 60, 1) AS \"已运行分钟\",\n"
            "       t.USED_UBLK AS \"占用UNDO块\",\n"
            "       ROUND(t.USED_UBLK * 8 / 1024, 1) AS \"UNDO约MB(按8K块)\",\n"
            "       s.SQL_ID AS \"SQL_ID\", s.EVENT AS \"等待事件\", s.STATUS AS \"状态\"\n"
            "FROM   V$TRANSACTION t, V$SESSION s\n"
            "WHERE  t.SES_ADDR = s.SADDR\n"
            "  AND  (SYSDATE - TO_DATE(t.START_TIME, 'MM/DD/YY HH24:MI:SS')) * 24 * 60 >= {{minutes}}\n"
            "ORDER  BY 8 DESC",
        hint="未提交的长事务是锁表的头号根源，也会把 UNDO 表空间撑大导致 ORA-01555。"
             "找到它比杀会话更重要 —— 多半是应用没提交或批处理卡住了。",
        keywords="长事务 未提交 undo 锁根源 ORA-01555",
    ),
    Template(
        id="locked_objects",
        name="被锁的对象与持有者",
        category="会话与锁",
        requires=["v_locked_object", "v_session", "dict_dba"],
        params=[_owner_param()],
        sql="SELECT o.OWNER AS \"属主\", o.OBJECT_NAME AS \"对象名\", o.OBJECT_TYPE AS \"对象类型\",\n"
            "       s.SID AS \"SID\", s.SERIAL# AS \"序号\", s.USERNAME AS \"用户\",\n"
            "       s.PROGRAM AS \"程序\", s.MACHINE AS \"机器\",\n"
            "       DECODE(l.LOCKED_MODE, 0,'无', 1,'空', 2,'行共享(RS)', 3,'行独占(RX)',\n"
            "               4,'共享(S)', 5,'共享行独占(SRX)', 6,'独占(X)',\n"
            "               TO_CHAR(l.LOCKED_MODE)) AS \"锁模式\",\n"
            "       TO_CHAR(s.LOGON_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"登录时间\"\n"
            "FROM   V$LOCKED_OBJECT l, DBA_OBJECTS o, V$SESSION s\n"
            "WHERE  l.OBJECT_ID = o.OBJECT_ID\n"
            "  AND  l.SESSION_ID = s.SID\n"
            "  AND  ({{owner}} IS NULL OR o.OWNER = UPPER({{owner}}))\n"
            "ORDER  BY o.OWNER, o.OBJECT_NAME",
        hint="行独占(RX) 通常是正常 DML；出现 独占(X) 多半是 DDL 被堵或有人在改结构。"
             "这里只诊断，杀会话的语句请自行到查询窗口执行：ALTER SYSTEM KILL SESSION 'SID,序号';",
        keywords="锁表 被锁对象 locked 持有者 TM锁",
    ),
    Template(
        id="idle_sessions",
        name="长时间空闲会话（连接泄漏嫌疑）",
        category="会话与锁",
        requires=["v_session"],
        params=[_p("minutes", "空闲超过(分钟)", type="int", default="120", min=1, max=100000)],
        sql="SELECT s.SID AS \"SID\", s.SERIAL# AS \"序号\", s.USERNAME AS \"用户\",\n"
            "       s.PROGRAM AS \"程序\", s.MACHINE AS \"机器\", s.STATUS AS \"状态\",\n"
            "       ROUND(s.LAST_CALL_ET / 60, 1) AS \"空闲分钟\",\n"
            "       TO_CHAR(s.LOGON_TIME, 'YYYY-MM-DD HH24:MI') AS \"登录时间\",\n"
            "       s.SQL_ID AS \"最后一条SQL\"\n"
            "FROM   V$SESSION s\n"
            "WHERE  s.TYPE = 'USER'\n"
            "  AND  s.STATUS = 'INACTIVE'\n"
            "  AND  s.LAST_CALL_ET >= {{minutes}} * 60\n"
            "ORDER  BY s.LAST_CALL_ET DESC",
        hint="空闲会话本身不占资源，但数量持续增长说明应用没关连接，最终会打满 processes。"
             "看“登录时间”是否集中在某次发布之后。",
        keywords="空闲会话 连接泄漏 没关闭连接 inactive",
    ),

    # ============================================================ SQL 性能
    Template(
        id="slow_top",
        name="Top SQL（按耗时/读次数排序）",
        category="SQL 性能",
        requires=["v_sqlarea"],
        params=[_top_param(50),
                _p("min_avg_sec", "平均耗时≥(秒)", type="float", default="0", min=0, max=100000),
                _p("keyword", "SQL 包含（留空不限）", type="opt_string", default=""),
                _p("order_by", "排序依据", type="enum",
                   values=["平均耗时", "总耗时", "执行次数", "逻辑读", "物理读"],
                   default="平均耗时")],
        sql="SELECT * FROM (\n"
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
            "         SUBSTR(REPLACE(TRIM(s.SQL_TEXT), CHR(10), ' '), 1, 200) AS \"SQL片段\"\n"
            "  FROM   V$SQLAREA s\n"
            "  WHERE  s.ELAPSED_TIME > 0\n"
            "    AND  s.ELAPSED_TIME / 1000000 / GREATEST(s.EXECUTIONS, 1) >= {{min_avg_sec}}\n"
            "    AND  ({{keyword}} IS NULL OR UPPER(s.SQL_TEXT) LIKE UPPER('%{{keyword}}%'))\n"
            "  ORDER  BY CASE '{{order_by}}'\n"
            "              WHEN '平均耗时' THEN s.ELAPSED_TIME / 1000000 / GREATEST(s.EXECUTIONS, 1)\n"
            "              WHEN '总耗时'   THEN s.ELAPSED_TIME / 1000000\n"
            "              WHEN '执行次数' THEN s.EXECUTIONS\n"
            "              WHEN '逻辑读'   THEN s.BUFFER_GETS\n"
            "              WHEN '物理读'   THEN s.DISK_READS\n"
            "            END DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="先按“总耗时”排看整体影响，再按“平均耗时”排看单条体验。"
             "“每次逻辑读”远大于返回行数 = 全表扫描嫌疑。",
        keywords="慢SQL 耗时 性能 top sql 执行次数",
    ),
    Template(
        id="sql_text_by_id",
        name="按 SQL_ID 取完整文本",
        category="SQL 性能",
        requires=["v_sqlarea"],
        params=[_p("sql_id", "SQL_ID", type="string", default="",
                   hint="从 Top SQL 列表里复制")],
        sql="SELECT s.SQL_ID AS \"SQL_ID\",\n"
            "       s.SQL_FULLTEXT AS \"完整SQL文本\"\n"
            "FROM   V$SQLAREA s\n"
            "WHERE  s.SQL_ID = {{sql_id}}\n"
            "  AND  ROWNUM = 1",
        hint="拿到完整文本后再用“按 SQL_ID 看真实执行计划”继续分析。",
        keywords="SQL_ID 完整文本 sql全文 查看语句",
    ),
    Template(
        id="sql_plan_cursor",
        name="按 SQL_ID 看真实执行计划",
        category="SQL 性能",
        requires=["v_sql_plan"],
        params=[_p("sql_id", "SQL_ID", type="string", default=""),
                _p("format", "详细程度", type="enum",
                   values=["TYPICAL", "BASIC", "ALLSTATS LAST", "ADVANCED"],
                   default="TYPICAL")],
        sql="SELECT PLAN_TABLE_OUTPUT AS \"执行计划\"\n"
            "FROM   TABLE(DBMS_XPLAN.DISPLAY_CURSOR({{sql_id}}, NULL, '{{format}}'))",
        hint="这是共享池里真实用过的计划，比 EXPLAIN PLAN 准。ALLSTATS LAST 需要 "
             "statistics_level=ALL 才有实际行数；SQL 已被挤出共享池时会查不到。",
        keywords="执行计划 explain plan xplan 索引 全表扫描",
    ),
    Template(
        id="hard_parse_top",
        name="硬解析最多的 SQL",
        category="SQL 性能",
        requires=["v_sqlarea"],
        params=[_top_param(30)],
        sql="SELECT * FROM (\n"
            "  SELECT s.SQL_ID AS \"SQL_ID\",\n"
            "         s.PARSE_CALLS AS \"解析次数\",\n"
            "         s.EXECUTIONS AS \"执行次数\",\n"
            "         ROUND(s.PARSE_CALLS / GREATEST(s.EXECUTIONS, 1), 1) AS \"每次执行解析数\",\n"
            "         s.LOADS AS \"加载次数\",\n"
            "         s.INVALIDATIONS AS \"失效次数\",\n"
            "         NVL(s.PARSING_SCHEMA_NAME, '(空)') AS \"解析用户\",\n"
            "         SUBSTR(REPLACE(TRIM(s.SQL_TEXT), CHR(10), ' '), 1, 180) AS \"SQL片段\"\n"
            "  FROM   V$SQLAREA s\n"
            "  WHERE  s.PARSE_CALLS > 0\n"
            "  ORDER  BY s.PARSE_CALLS DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="每次执行解析数接近 1 且执行次数很高 = 没用绑定变量，会大量消耗 CPU 并引发 "
             "library cache 争用。看 SQL 片段里是不是拼了很多字面量。",
        keywords="硬解析 绑定变量 parse library cache 软解析",
    ),
    Template(
        id="disk_read_top",
        name="物理读最高的 SQL（全表扫描嫌疑）",
        category="SQL 性能",
        requires=["v_sqlarea"],
        params=[_top_param(30)],
        sql="SELECT * FROM (\n"
            "  SELECT s.SQL_ID AS \"SQL_ID\",\n"
            "         s.DISK_READS AS \"物理读\",\n"
            "         s.BUFFER_GETS AS \"逻辑读\",\n"
            "         s.EXECUTIONS AS \"执行次数\",\n"
            "         ROUND(s.DISK_READS / GREATEST(s.EXECUTIONS, 1)) AS \"每次物理读\",\n"
            "         ROUND(s.DISK_READS / GREATEST(s.BUFFER_GETS, 1) * 100, 1) AS \"物理读占比%\",\n"
            "         NVL(s.PARSING_SCHEMA_NAME, '(空)') AS \"解析用户\",\n"
            "         SUBSTR(REPLACE(TRIM(s.SQL_TEXT), CHR(10), ' '), 1, 180) AS \"SQL片段\"\n"
            "  FROM   V$SQLAREA s\n"
            "  WHERE  s.DISK_READS > 0\n"
            "  ORDER  BY s.DISK_READS DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="物理读占比高说明数据不在内存里反复读盘。先看是不是缺索引，再看统计信息是否过期。",
        keywords="物理读 全表扫描 IO 磁盘 索引缺失",
    ),
    Template(
        id="wait_events",
        name="等待事件汇总（库在等什么）",
        category="SQL 性能",
        requires=["v_system_event"],
        params=[_top_param(20)],
        sql="SELECT * FROM (\n"
            "  SELECT e.EVENT AS \"等待事件\",\n"
            "         e.WAIT_CLASS AS \"类别\",\n"
            "         e.TOTAL_WAITS AS \"总等待次数\",\n"
            "         ROUND(e.TIME_WAITED_MICRO / 1000000, 1) AS \"总等待秒\",\n"
            "         ROUND(e.TIME_WAITED_MICRO / GREATEST(e.TOTAL_WAITS, 1) / 1000, 2) AS \"平均等待毫秒\",\n"
            "         ROUND(e.TIME_WAITED_MICRO / SUM(e.TIME_WAITED_MICRO) OVER () * 100, 1) AS \"占比%\"\n"
            "  FROM   V$SYSTEM_EVENT e\n"
            "  WHERE  e.WAIT_CLASS <> 'Idle'\n"
            "  ORDER  BY e.TIME_WAITED_MICRO DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="db file sequential read 高=索引读多；db file scattered read 高=全表扫描多；"
             "log file sync 高=提交太频繁；enq: TX - row lock contention 高=有人在等锁。"
             "看占比最高的那一两个，不用管长尾。",
        keywords="等待事件 wait event AWR 瓶颈 系统在等什么",
    ),
    Template(
        id="longops",
        name="正在跑的长操作与剩余时间",
        category="SQL 性能",
        requires=["v_session_longops"],
        sql="SELECT o.SID AS \"SID\", o.SERIAL# AS \"序号\",\n"
            "       o.OPNAME AS \"操作\",\n"
            "       o.TARGET AS \"目标对象\",\n"
            "       o.SOFAR AS \"已完成量\", o.TOTALWORK AS \"总量\",\n"
            "       ROUND(o.SOFAR / GREATEST(o.TOTALWORK, 1) * 100, 1) AS \"完成%\",\n"
            "       ROUND(o.ELAPSED_SECONDS / 60, 1) AS \"已用分钟\",\n"
            "       ROUND(o.TIME_REMAINING / 60, 1) AS \"预计剩余分钟\",\n"
            "       TO_CHAR(o.START_TIME, 'MM-DD HH24:MI') AS \"开始时间\"\n"
            "FROM   V$SESSION_LONGOPS o\n"
            "WHERE  o.TOTALWORK > 0\n"
            "  AND  o.SOFAR < o.TOTALWORK\n"
            "ORDER  BY o.TIME_REMAINING DESC",
        hint="预计剩余分钟是线性外推的估算值，只用于判断“还要等多久”，别当承诺。"
             "跑批卡住时先看这里。",
        keywords="长操作 跑批 剩余时间 longops 进度 卡住",
    ),
    Template(
        id="active_sql",
        name="当前活跃会话正在跑什么",
        category="SQL 性能",
        requires=["v_session", "v_sqlarea"],
        sql="SELECT s.SID AS \"SID\", s.SERIAL# AS \"序号\", s.USERNAME AS \"用户\",\n"
            "       s.PROGRAM AS \"程序\", s.MACHINE AS \"机器\",\n"
            "       s.EVENT AS \"等待事件\", s.WAIT_CLASS AS \"等待类别\",\n"
            "       s.SECONDS_IN_WAIT AS \"已等待秒\",\n"
            "       s.SQL_ID AS \"SQL_ID\",\n"
            "       (SELECT SUBSTR(REPLACE(TRIM(q.SQL_TEXT), CHR(10), ' '), 1, 150)\n"
            "          FROM V$SQLAREA q WHERE q.SQL_ID = s.SQL_ID AND ROWNUM = 1) AS \"当前SQL\",\n"
            "       TO_CHAR(s.LOGON_TIME, 'MM-DD HH24:MI') AS \"登录时间\"\n"
            "FROM   V$SESSION s\n"
            "WHERE  s.TYPE = 'USER'\n"
            "  AND  s.STATUS = 'ACTIVE'\n"
            "ORDER  BY s.SECONDS_IN_WAIT DESC",
        hint="库“卡”的时候第一眼看这个：如果大量会话卡在同一个等待事件，问题就定位了。",
        keywords="活跃会话 正在执行 active 当前SQL 卡住",
    ),

    # ============================================================ 空间与存储
    Template(
        id="tablespace_usage",
        name="表空间用量",
        category="空间与存储",
        requires=["dict_dba"],
        sql="SELECT a.\"表空间\", t.CONTENTS AS \"类型\",\n"
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
            "ORDER BY \"使用率%\" DESC",
        hint="使用率 ≥90 要马上处理，≥75 提前扩容。注意“最大可扩MB”才是真正的天花板 —— "
             "空闲 MB 少但还能自动扩展的不算紧急。",
        keywords="表空间 满了 使用率 扩容 空间不足",
    ),
    Template(
        id="temp_usage",
        name="临时表空间用量",
        category="空间与存储",
        requires=["dba_temp_files", "v_temp_space_header"],
        sql="SELECT t.TABLESPACE_NAME AS \"临时表空间\",\n"
            "       ROUND(SUM(f.BYTES) / 1048576, 1) AS \"总大小MB\",\n"
            "       ROUND(NVL(u.used_mb, 0), 1) AS \"已用MB\",\n"
            "       ROUND(SUM(f.BYTES) / 1048576 - NVL(u.used_mb, 0), 1) AS \"空闲MB\",\n"
            "       ROUND(NVL(u.used_mb, 0) / GREATEST(SUM(f.BYTES) / 1048576, 1) * 100, 1) AS \"使用率%\"\n"
            "FROM   DBA_TEMP_FILES f\n"
            "JOIN   DBA_TABLESPACES t ON t.TABLESPACE_NAME = f.TABLESPACE_NAME\n"
            "LEFT JOIN (\n"
            "  SELECT TABLESPACE_NAME, SUM(BYTES_USED) / 1048576 AS used_mb\n"
            "  FROM   V$TEMP_SPACE_HEADER GROUP BY TABLESPACE_NAME\n"
            ") u ON u.TABLESPACE_NAME = f.TABLESPACE_NAME\n"
            "GROUP  BY t.TABLESPACE_NAME, NVL(u.used_mb, 0)\n"
            "ORDER  BY 1",
        hint="临时表空间被撑爆会导致 ORA-01652，报错的往往是一条大排序或笛卡尔积的 SQL —— "
             "去 Top SQL 里按物理读找它，别急着加数据文件。",
        keywords="临时表空间 temp ORA-01652 排序 磁盘排序",
    ),
    Template(
        id="datafile_autoextend",
        name="数据文件清单与扩展余量",
        category="空间与存储",
        requires=["dict_dba"],
        sql="SELECT f.TABLESPACE_NAME AS \"表空间\",\n"
            "       f.FILE_NAME AS \"数据文件\",\n"
            "       ROUND(f.BYTES / 1048576, 1) AS \"当前MB\",\n"
            "       ROUND(DECODE(f.AUTOEXTENSIBLE, 'YES', GREATEST(f.MAXBYTES, f.BYTES), f.BYTES) / 1048576, 1) AS \"最大MB\",\n"
            "       f.AUTOEXTENSIBLE AS \"可自动扩展\",\n"
            "       ROUND(DECODE(f.AUTOEXTENSIBLE, 'YES', GREATEST(f.MAXBYTES, f.BYTES), f.BYTES)\n"
            "             / GREATEST(f.BYTES, 1), 2) AS \"还能扩几倍\",\n"
            "       f.STATUS AS \"状态\"\n"
            "FROM   DBA_DATA_FILES f\n"
            "ORDER  BY f.TABLESPACE_NAME, f.FILE_NAME",
        hint="最危险的组合是：表空间快满 + 数据文件“可自动扩展=NO”。"
             "这种一旦写满会直接报错，而不是自动长大。",
        keywords="数据文件 自动扩展 autoextend 扩容 文件清单",
    ),
    Template(
        id="top_segments",
        name="占空间最大的段",
        category="空间与存储",
        requires=["dict_dba"],
        params=[_top_param(50), _owner_param()],
        sql="SELECT * FROM (\n"
            "  SELECT s.OWNER AS \"属主\", s.SEGMENT_NAME AS \"段名\",\n"
            "         s.SEGMENT_TYPE AS \"类型\", s.TABLESPACE_NAME AS \"表空间\",\n"
            "         ROUND(s.BYTES / 1048576, 1) AS \"大小MB\"\n"
            "  FROM   DBA_SEGMENTS s\n"
            "  WHERE  ({{owner}} IS NULL OR s.OWNER = UPPER({{owner}}))\n"
            "  ORDER  BY s.BYTES DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="按“大小MB”降序看，索引比表还大通常意味着索引建多了或者很久没重建。",
        keywords="最大的表 段占用 空间排行 大表",
    ),
    Template(
        id="table_bloat",
        name="表膨胀（高水位线远高于数据量）",
        category="空间与存储",
        requires=["dict_dba"],
        params=[_p("min_mb", "段大小≥(MB)", type="int", default="100", min=1, max=10000000),
                _p("min_ratio", "膨胀倍数≥", type="float", default="3", min=1, max=1000),
                _top_param(50)],
        sql="SELECT * FROM (\n"
            "  SELECT t.OWNER AS \"属主\", t.TABLE_NAME AS \"表名\",\n"
            "         t.NUM_ROWS AS \"统计行数\",\n"
            "         ROUND(NVL(s.bytes, 0) / 1048576, 1) AS \"段占用MB\",\n"
            "         ROUND(t.NUM_ROWS * NVL(t.AVG_ROW_LEN, 0) / 1048576, 1) AS \"数据理论MB\",\n"
            "         ROUND(NVL(s.bytes, 0) / GREATEST(t.NUM_ROWS * NVL(t.AVG_ROW_LEN, 0), 1), 1) AS \"膨胀倍数\",\n"
            "         TO_CHAR(t.LAST_ANALYZED, 'YYYY-MM-DD HH24:MI') AS \"统计时间\"\n"
            "  FROM   DBA_TABLES t\n"
            "  LEFT JOIN (\n"
            "    SELECT OWNER, SEGMENT_NAME, SUM(BYTES) AS bytes\n"
            "    FROM   DBA_SEGMENTS\n"
            "    WHERE  SEGMENT_TYPE IN ('TABLE', 'TABLE PARTITION')\n"
            "    GROUP  BY OWNER, SEGMENT_NAME\n"
            "  ) s ON s.OWNER = t.OWNER AND s.SEGMENT_NAME = t.TABLE_NAME\n"
            "  WHERE  t.NUM_ROWS > 0\n"
            "    AND  NVL(s.bytes, 0) >= {{min_mb}} * 1048576\n"
            "    AND  NVL(s.bytes, 0) >= {{min_ratio}} * GREATEST(t.NUM_ROWS * NVL(t.AVG_ROW_LEN, 0), 1)\n"
            "  ORDER  BY NVL(s.bytes, 0) DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="膨胀倍数 = 段占用 / 数据理论大小。DELETE 不会释放空间，大量删除后需要 "
             "SHRINK SPACE 或 MOVE 才能收回 —— 这里只诊断，不动手。统计行数过期时结果不可信。",
        keywords="碎片 高水位 膨胀 收缩 shrink 删除后没变小",
    ),

    # ============================================================ 对象与结构
    Template(
        id="invalid_objects",
        name="无效对象（编译失败）",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_owner_param()],
        sql="SELECT o.OWNER AS \"属主\", o.OBJECT_NAME AS \"对象名\", o.OBJECT_TYPE AS \"类型\",\n"
            "       o.STATUS AS \"状态\",\n"
            "       TO_CHAR(o.LAST_DDL_TIME, 'YYYY-MM-DD HH24:MI:SS') AS \"最后变更时间\"\n"
            "FROM   DBA_OBJECTS o\n"
            "WHERE  o.STATUS <> 'VALID'\n"
            "  AND  ({{owner}} IS NULL OR o.OWNER = UPPER({{owner}}))\n"
            "ORDER  BY o.OWNER, o.OBJECT_TYPE, o.OBJECT_NAME",
        hint="上线后必查这一项。INVALID 的包/视图在被调用时会自动重编译，失败才会报错；"
             "长期 INVALID 说明有依赖缺失（比如表被删了）。",
        keywords="失效 无效对象 INVALID 编译失败 上线检查",
    ),
    Template(
        id="stale_stats",
        name="统计信息过期",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_p("months", "超过几个月未收集", type="int", default="3", min=1, max=120),
                _owner_param()],
        sql="SELECT t.OWNER AS \"属主\", t.TABLE_NAME AS \"表名\",\n"
            "       t.NUM_ROWS AS \"统计行数\",\n"
            "       TO_CHAR(t.LAST_ANALYZED, 'YYYY-MM-DD HH24:MI') AS \"最后收集时间\",\n"
            "       ROUND(SYSDATE - NVL(t.LAST_ANALYZED, DATE '2000-01-01')) AS \"距今天数\",\n"
            "       CASE WHEN t.LAST_ANALYZED IS NULL THEN '从未收集'\n"
            "            WHEN MONTHS_BETWEEN(SYSDATE, t.LAST_ANALYZED) >= {{months}} THEN '已过期'\n"
            "            ELSE '正常' END AS \"状态\"\n"
            "FROM   DBA_TAB_STATISTICS t\n"
            "WHERE  t.PARTITION_NAME IS NULL\n"
            "  AND  (t.LAST_ANALYZED IS NULL\n"
            "        OR MONTHS_BETWEEN(SYSDATE, t.LAST_ANALYZED) >= {{months}})\n"
            "  AND  ({{owner}} IS NULL OR t.OWNER = UPPER({{owner}}))\n"
            "ORDER  BY 距今天数 DESC NULLS FIRST",
        hint="统计信息过期是“SQL 突然变慢”最常见的原因 —— 优化器拿到旧的行数估算，"
             "选了错误的执行计划。这条要和“Top SQL”对照着看。",
        keywords="统计信息 过期 analyze DBMS_STATS 突然变慢 执行计划变差",
    ),
    Template(
        id="fk_no_index",
        name="外键缺索引",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_owner_param()],
        sql="SELECT fk.OWNER AS \"属主\", fk.TABLE_NAME AS \"子表\",\n"
            "       fk.CONSTRAINT_NAME AS \"外键名\",\n"
            "       pk.TABLE_NAME AS \"父表\",\n"
            "       (SELECT LISTAGG(cc.COLUMN_NAME, ',') WITHIN GROUP (ORDER BY cc.POSITION)\n"
            "          FROM DBA_CONS_COLUMNS cc\n"
            "         WHERE cc.OWNER = fk.OWNER AND cc.CONSTRAINT_NAME = fk.CONSTRAINT_NAME) AS \"外键列\"\n"
            "FROM   DBA_CONSTRAINTS fk\n"
            "JOIN   DBA_CONSTRAINTS pk\n"
            "       ON pk.CONSTRAINT_NAME = fk.R_CONSTRAINT_NAME AND pk.OWNER = fk.R_OWNER\n"
            "WHERE  fk.CONSTRAINT_TYPE = 'R'\n"
            "  AND  ({{owner}} IS NULL OR fk.OWNER = UPPER({{owner}}))\n"
            "  AND  NOT EXISTS (\n"
            "        SELECT 1\n"
            "        FROM   DBA_IND_COLUMNS ic\n"
            "        WHERE  ic.TABLE_OWNER = fk.OWNER\n"
            "          AND  ic.TABLE_NAME = fk.TABLE_NAME\n"
            "          AND  ic.COLUMN_POSITION = 1\n"
            "          AND  ic.COLUMN_NAME IN (\n"
            "                SELECT cc.COLUMN_NAME FROM DBA_CONS_COLUMNS cc\n"
            "                 WHERE cc.OWNER = fk.OWNER AND cc.CONSTRAINT_NAME = fk.CONSTRAINT_NAME))\n"
            "ORDER  BY fk.OWNER, fk.TABLE_NAME",
        hint="外键列没有索引时，删除或更新父表记录会把整张子表锁住 —— 这是很多“莫名锁表”的"
             "根因。只检查索引首列，组合索引的后续列可能仍有覆盖，动手前再确认一下。",
        keywords="外键 缺索引 锁表 约束 级联删除",
    ),
    Template(
        id="index_problem",
        name="异常索引（失效 / 选择性差）",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_owner_param()],
        sql="SELECT i.OWNER AS \"属主\", i.INDEX_NAME AS \"索引名\", i.TABLE_NAME AS \"表名\",\n"
            "       i.STATUS AS \"状态\",\n"
            "       i.NUM_ROWS AS \"行数\",\n"
            "       i.DISTINCT_KEYS AS \"唯一值数\",\n"
            "       ROUND(i.DISTINCT_KEYS / GREATEST(i.NUM_ROWS, 1) * 100, 2) AS \"选择性%\",\n"
            "       i.BLEVEL AS \"层级\",\n"
            "       ROUND(i.LEAF_BLOCKS * 8 / 1024, 1) AS \"叶子块约MB(按8K块)\",\n"
            "       TO_CHAR(i.LAST_ANALYZED, 'YYYY-MM-DD') AS \"统计时间\"\n"
            "FROM   DBA_INDEXES i\n"
            "WHERE  (i.STATUS <> 'VALID' OR (i.NUM_ROWS > 0 AND i.DISTINCT_KEYS = 0))\n"
            "  AND  ({{owner}} IS NULL OR i.OWNER = UPPER({{owner}}))\n"
            "ORDER  BY i.OWNER, i.INDEX_NAME",
        hint="STATUS=UNUSABLE 的索引会让 DML 直接报错，需要重建。选择性低于 5% 的单列索引"
             "基本不会被用到，反而拖慢写入 —— 这里只列出，删不删你自己判断。",
        keywords="失效索引 UNUSABLE 重建 选择性 索引没用",
    ),
    Template(
        id="find_column",
        name="按字段名全库找表",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_p("keyword", "字段名包含", type="string", default="",
                   hint="如 EMP_ID，不区分大小写"),
                _owner_param(),
                _top_param(200)],
        sql="SELECT * FROM (\n"
            "  SELECT c.OWNER AS \"属主\", c.TABLE_NAME AS \"表名\", c.COLUMN_NAME AS \"字段名\",\n"
            "         c.DATA_TYPE AS \"类型\",\n"
            "         c.DATA_LENGTH AS \"长度\",\n"
            "         c.NULLABLE AS \"可空\",\n"
            "         c.DATA_DEFAULT AS \"默认值\",\n"
            "         (SELECT cc.COMMENTS FROM DBA_COL_COMMENTS cc\n"
            "           WHERE cc.OWNER = c.OWNER AND cc.TABLE_NAME = c.TABLE_NAME\n"
            "             AND cc.COLUMN_NAME = c.COLUMN_NAME) AS \"字段注释\"\n"
            "  FROM   DBA_TAB_COLUMNS c\n"
            "  WHERE  UPPER(c.COLUMN_NAME) LIKE UPPER('%{{keyword}}%')\n"
            "    AND  ({{owner}} IS NULL OR c.OWNER = UPPER({{owner}}))\n"
            "  ORDER  BY c.OWNER, c.TABLE_NAME\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="手上只有一个字段、想知道它在哪些表里 —— 用这条。找到表之后可以回到"
             "“数据窗口”直接查，或用“跨表钻取”顺着值追下去。",
        keywords="找字段 哪个表 字段搜索 列名 全库搜索",
    ),
    Template(
        id="find_comment",
        name="按注释找表或字段",
        category="对象与结构",
        requires=["dict_dba"],
        params=[_p("keyword", "注释包含", type="string", default="",
                   hint="如 订单、客户"),
                _top_param(200)],
        sql="SELECT * FROM (\n"
            "  SELECT '表' AS \"类型\", t.OWNER AS \"属主\", t.TABLE_NAME AS \"对象名\",\n"
            "         CAST(NULL AS VARCHAR2(30)) AS \"字段名\", t.COMMENTS AS \"注释\"\n"
            "  FROM   DBA_TAB_COMMENTS t\n"
            "  WHERE  UPPER(t.COMMENTS) LIKE UPPER('%{{keyword}}%')\n"
            "  UNION ALL\n"
            "  SELECT '字段', c.OWNER, c.TABLE_NAME, c.COLUMN_NAME, c.COMMENTS\n"
            "  FROM   DBA_COL_COMMENTS c\n"
            "  WHERE  UPPER(c.COMMENTS) LIKE UPPER('%{{keyword}}%')\n"
            "  ORDER  BY 2, 3\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="内网环境通常没有数据字典文档，注释就是唯一的说明书。"
             "字段名拼音缩写看不懂时，用它反查业务含义。",
        keywords="注释 数据字典 找表 业务含义 文档",
    ),

    # ============================================================ 容量与增长
    Template(
        id="big_tables",
        name="表行数排行（统计值估算）",
        category="容量与增长",
        requires=["dict_dba"],
        params=[_top_param(50), _owner_param()],
        sql="SELECT * FROM (\n"
            "  SELECT t.OWNER AS \"属主\", t.TABLE_NAME AS \"表名\",\n"
            "         t.NUM_ROWS AS \"行数\",\n"
            "         ROUND(t.NUM_ROWS * NVL(t.AVG_ROW_LEN, 0) / 1048576, 1) AS \"估算大小MB\",\n"
            "         t.PARTITIONED AS \"是否分区\",\n"
            "         TO_CHAR(t.LAST_ANALYZED, 'YYYY-MM-DD HH24:MI') AS \"统计时间\"\n"
            "  FROM   DBA_TABLES t\n"
            "  WHERE  t.NUM_ROWS > 0\n"
            "    AND  ({{owner}} IS NULL OR t.OWNER = UPPER({{owner}}))\n"
            "  ORDER  BY t.NUM_ROWS DESC\n"
            ") WHERE ROWNUM <= {{top_n}}",
        hint="行数取自统计信息，不是 COUNT(*) —— 大表数一次太慢，这里只要一个量级判断。"
             "要精确数字请到数据窗口自己查，并注意统计时间是否过期。",
        keywords="大表 行数 排行 数据量 估算",
    ),
    Template(
        id="archive_daily",
        name="每日归档产生量",
        category="容量与增长",
        requires=["v_archived_log"],
        params=[_p("days", "最近几天", type="int", default="7", min=1, max=90)],
        sql="SELECT TO_CHAR(a.FIRST_TIME, 'YYYY-MM-DD') AS \"日期\",\n"
            "       COUNT(*) AS \"归档个数\",\n"
            "       ROUND(SUM(a.BLOCKS * a.BLOCK_SIZE) / 1048576, 1) AS \"当天MB\"\n"
            "FROM   V$ARCHIVED_LOG a\n"
            "WHERE  a.DEST_ID = 1\n"
            "  AND  a.FIRST_TIME >= SYSDATE - {{days}}\n"
            "GROUP  BY TO_CHAR(a.FIRST_TIME, 'YYYY-MM-DD')\n"
            "ORDER  BY 1 DESC",
        hint="某天归档量突然是平时的几倍，那天一定跑过大批量作业。判断“谁干的”要配合"
             "Top SQL 的最后活动时间和日期对一下。",
        keywords="归档量 增长 大批量 日志生成 趋势",
    ),
    Template(
        id="segment_by_tablespace",
        name="各表空间的段占用构成",
        category="容量与增长",
        requires=["dict_dba"],
        sql="SELECT s.TABLESPACE_NAME AS \"表空间\",\n"
            "       s.SEGMENT_TYPE AS \"段类型\",\n"
            "       COUNT(*) AS \"段数\",\n"
            "       ROUND(SUM(s.BYTES) / 1048576, 1) AS \"总大小MB\"\n"
            "FROM   DBA_SEGMENTS s\n"
            "GROUP  BY s.TABLESPACE_NAME, s.SEGMENT_TYPE\n"
            "ORDER  BY SUM(s.BYTES) DESC",
        hint="用来回答“这个表空间的空间到底被什么占了”：索引占比过高、LOB 段异常增长，"
             "都是常见的空间黑洞。",
        keywords="表空间构成 段类型 空间被谁占了 LOB 索引",
    ),
]


def find_builtin(template_id: str) -> Optional[Template]:
    for t in BUILTIN:
        if t.id == template_id:
            return t
    return None


# ================================================================== 存储
class TemplateStore:
    """内置模板 + 用户模板。

    内置模板只增不改：用户想改必须先“复制为我的模板”，改动存在 templates.json。
    这样升级软件时既不会覆盖用户改过的东西，也不会丢内置模板的更新。
    """

    FILENAME = "templates.json"

    def __init__(self):
        self._path: Optional[Path] = None
        self.user: List[Template] = []
        self._lock = threading.Lock()
        self.load()

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = data_dir() / self.FILENAME
        return self._path

    def load(self):
        self.user = []
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or []
                for item in raw:
                    try:
                        t = Template.from_dict(item)
                        t.source = "user"
                        if t.id and t.sql:
                            self.user.append(t)
                    except Exception:
                        continue      # 单条坏数据不影响其余模板
        except Exception:
            self.user = []

    def save(self):
        with self._lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump([t.to_dict() for t in self.user], f,
                              ensure_ascii=False, indent=2)
            except Exception:
                pass

    # ---- 查询 ----
    def all(self) -> List[Template]:
        """用户模板排在前面：用户显式保存的东西优先于同名内置模板。

        注意 BUILTIN 列表本身从不被修改 —— “优先”只是展示顺序，
        内置模板的内容始终完好，删掉用户模板就会重新看到它。
        """
        return list(self.user) + list(BUILTIN)

    def find(self, template_id: str) -> Optional[Template]:
        for t in self.all():
            if t.id == template_id:
                return t
        return None

    def categories(self) -> List[str]:
        """有模板的分类，按固定顺序 + 用户自定义分类追加在后。"""
        out = [c for c in CATEGORIES if any(t.category == c for t in self.all())]
        for t in self.user:
            if t.category not in out:
                out.append(t.category)
        return out

    def by_category(self, category: str) -> List[Template]:
        return [t for t in self.all() if t.category == category]

    def search(self, text: str) -> List[Template]:
        kw = (text or "").strip().lower()
        if not kw:
            return self.all()
        return [t for t in self.all() if kw in t.search_text()]

    # ---- 用户模板增删（B3 界面尚未做，存储层先备好）----
    def add_user(self, template: Template) -> Template:
        template.source = "user"
        self.user = [t for t in self.user if t.id != template.id]
        self.user.append(template)
        self.save()
        return template

    def remove_user(self, template_id: str) -> bool:
        before = len(self.user)
        self.user = [t for t in self.user if t.id != template_id]
        if len(self.user) != before:
            self.save()
            return True
        return False

    def new_id(self, base: str = "my_template") -> str:
        """生成一个不与现有模板冲突的 id。"""
        used = {t.id for t in self.all()}
        if base not in used:
            return base
        i = 2
        while f"{base}_{i}" in used:
            i += 1
        return f"{base}_{i}"

    # ---- 导入导出（内网无外网，团队之间靠文件搬运）----
    def export_to(self, path: str, ids: Optional[Sequence[str]] = None) -> int:
        items = [t.to_dict() for t in self.user if not ids or t.id in set(ids)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return len(items)

    def import_from(self, path: str, replace: bool = False) -> Tuple[int, List[str]]:
        """导入模板文件，返回 (成功条数, 跳过/失败的名称列表)。"""
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or []
        if isinstance(raw, dict):
            raw = [raw]
        ok, skipped = 0, []
        for item in raw:
            try:
                t = Template.from_dict(item)
                if not t.id or not t.sql:
                    skipped.append(str(item.get("name", "(无名)")))
                    continue
                if any(u.id == t.id for u in self.user) and not replace:
                    skipped.append(t.name)
                    continue
                self.add_user(t)
                ok += 1
            except Exception:
                skipped.append(str(item.get("name", "(无法解析)")))
        return ok, skipped
