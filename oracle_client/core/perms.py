"""权限探测：每个人的账号权限都不一样，功能要能自己说清“缺什么”。

设计原则（运维/DBA 现场用）：
  * 不假设任何视图一定可查，全部靠“探针”实测，结果缓存；
  * 探测失败绝不当成致命错误，只把相关功能降级；
  * 界面上永远写清楚“需要 xx 权限”，而不是悄悄把功能藏起来。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ================================================================== 能力清单
# key            : 内部标识
# label          : 权限自检里显示的名字
# view           : 用于探测的对象（空表示用 probe_sql）
# probe_sql      : 自定义探测语句
# need           : 缺少时提示需要什么
CAPABILITIES: List[dict] = [
    dict(key="basic", label="基本连接与查询", view="DUAL",
         need="只要有 CREATE SESSION 权限即可"),
    dict(key="dict_all", label="字典视图 ALL_*（看得到所有有权限的表）", view="ALL_TABLES",
         need="一般默认就有；没有说明账号被回收了 SELECT 字典权限"),
    dict(key="dict_dba", label="字典视图 DBA_*（看全库对象）", view="DBA_TABLES",
         need="SELECT ANY DICTIONARY 权限，或 SELECT_CATALOG_ROLE 角色"),
    dict(key="v_database", label="V$DATABASE（库名/归档/字符集）", view="V$DATABASE",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$DATABASE"),
    dict(key="v_instance", label="V$INSTANCE（实例名/启动时间）", view="V$INSTANCE",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$INSTANCE"),
    dict(key="v_version", label="V$VERSION（数据库版本）", view="V$VERSION",
         need="一般默认可查"),
    dict(key="v_session", label="V$SESSION（会话/连接数）", view="V$SESSION",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SESSION"),
    dict(key="v_sqlarea", label="V$SQLAREA（慢 SQL 汇总）", view="V$SQLAREA",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SQLAREA"),
    dict(key="v_sql", label="V$SQL（SQL 明细）", view="V$SQL",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SQL"),
    dict(key="v_locked_object", label="V$LOCKED_OBJECT（被锁对象）", view="V$LOCKED_OBJECT",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$LOCKED_OBJECT"),
    dict(key="v_lock", label="V$LOCK（锁信息）", view="V$LOCK",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$LOCK"),
    dict(key="v_transaction", label="V$TRANSACTION（未提交事务）", view="V$TRANSACTION",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$TRANSACTION"),
    dict(key="v_session_longops", label="V$SESSION_LONGOPS（长操作进度）", view="V$SESSION_LONGOPS",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SESSION_LONGOPS"),
    dict(key="v_sql_plan", label="V$SQL_PLAN（真实执行计划）", view="V$SQL_PLAN",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SQL_PLAN"),
    dict(key="v_system_event", label="V$SYSTEM_EVENT（等待事件）", view="V$SYSTEM_EVENT",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$SYSTEM_EVENT"),
    dict(key="v_resource_limit", label="V$RESOURCE_LIMIT（连接数等资源上限）", view="V$RESOURCE_LIMIT",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$RESOURCE_LIMIT"),
    dict(key="v_archived_log", label="V$ARCHIVED_LOG（归档日志）", view="V$ARCHIVED_LOG",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$ARCHIVED_LOG"),
    dict(key="v_temp_space_header", label="V$TEMP_SPACE_HEADER（临时表空间用量）",
         view="V$TEMP_SPACE_HEADER",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$TEMP_SPACE_HEADER"),
    dict(key="v_diag_alert_ext", label="V$DIAG_ALERT_EXT（告警日志，11g+）", view="V$DIAG_ALERT_EXT",
         need="SELECT_CATALOG_ROLE 或 SELECT ON V_$DIAG_ALERT_EXT（且数据库需 11g 及以上）"),
    dict(key="dba_data_files", label="DBA_DATA_FILES（数据文件）", view="DBA_DATA_FILES",
         need="SELECT ANY DICTIONARY / SELECT_CATALOG_ROLE"),
    dict(key="dba_free_space", label="DBA_FREE_SPACE（空闲空间）", view="DBA_FREE_SPACE",
         need="SELECT ANY DICTIONARY / SELECT_CATALOG_ROLE"),
    dict(key="dba_ts_metrics", label="DBA_TABLESPACE_USAGE_METRICS（表空间用量）",
         view="DBA_TABLESPACE_USAGE_METRICS",
         need="SELECT ANY DICTIONARY / SELECT_CATALOG_ROLE"),
    dict(key="dba_temp_files", label="DBA_TEMP_FILES（临时文件）", view="DBA_TEMP_FILES",
         need="SELECT ANY DICTIONARY / SELECT_CATALOG_ROLE"),
    dict(key="dba_segments", label="DBA_SEGMENTS（全库段占用）", view="DBA_SEGMENTS",
         need="SELECT ANY DICTIONARY / SELECT_CATALOG_ROLE"),
    dict(key="user_segments", label="USER_SEGMENTS（自己 schema 的段占用）", view="USER_SEGMENTS",
         need="默认就有"),
    dict(key="user_ts_quotas", label="USER_TS_QUOTAS（自己在各表空间的配额）", view="USER_TS_QUOTAS",
         need="默认就有"),
]

CAP_BY_KEY: Dict[str, dict] = {c["key"]: c for c in CAPABILITIES}

# 角色/系统权限探测（比逐个视图探测更能解释“为什么会缺”）
ROLE_PROBES: List[dict] = [
    dict(key="role_catalog", label="角色 SELECT_CATALOG_ROLE",
         sql="SELECT COUNT(*) FROM SESSION_ROLES WHERE ROLE = 'SELECT_CATALOG_ROLE'",
         need="让 DBA 授予：GRANT SELECT_CATALOG_ROLE TO <用户>"),
    dict(key="priv_any_dict", label="系统权限 SELECT ANY DICTIONARY",
         sql="SELECT COUNT(*) FROM SESSION_PRIVS WHERE PRIVILEGE = 'SELECT ANY DICTIONARY'",
         need="让 DBA 授予：GRANT SELECT ANY DICTIONARY TO <用户>"),
    dict(key="priv_any_table", label="系统权限 SELECT ANY TABLE",
         sql="SELECT COUNT(*) FROM SESSION_PRIVS WHERE PRIVILEGE = 'SELECT ANY TABLE'",
         need="让 DBA 授予：GRANT SELECT ANY TABLE TO <用户>"),
]


def _probe_sql(cap: dict) -> str:
    if cap.get("probe_sql"):
        return cap["probe_sql"]
    view = cap.get("view") or "DUAL"
    # ROWNUM <= 1：只要能解析并取到一行就算有权限，避免全表计数带来的开销
    return f"SELECT COUNT(*) FROM {view} WHERE ROWNUM <= 1"


# ================================================================== 探测器
class PrivilegeProbe:
    """对某个连接做一次“能看什么”的体检，结果按连接缓存。"""

    def __init__(self, session):
        self.session = session
        self._cache: Dict[str, bool] = {}
        self._err: Dict[str, str] = {}

    # ---- 基础探测 ----
    @property
    def is_demo(self) -> bool:
        return bool(getattr(getattr(self.session, "profile", None), "demo", False))

    def has(self, key: str, refresh: bool = False) -> bool:
        key = key or ""
        if not refresh and key in self._cache:
            return self._cache[key]
        if self.is_demo:
            # 演示库没有 Oracle 字典视图，只有“基本查询”可用
            ok = (key == "basic")
            self._cache[key] = ok
            self._err[key] = "" if ok else "内置演示库没有 Oracle 字典/动态性能视图"
            return ok

        cap = CAP_BY_KEY.get(key)
        if cap is None:  # 未知能力直接按“没有”处理，避免误判
            self._cache[key] = False
            return False
        try:
            self.session.query(_probe_sql(cap), max_rows=1)
            ok = True
            err = ""
        except Exception as e:  # noqa: BLE001
            ok = False
            err = str(e).strip().splitlines()[0][:160] if str(e).strip() else "无法访问"
        self._cache[key] = ok
        self._err[key] = err
        return ok

    def has_any(self, *keys: str) -> bool:
        return any(self.has(k) for k in keys)

    def has_all(self, *keys: str) -> bool:
        return all(self.has(k) for k in keys)

    def error(self, key: str) -> str:
        self.has(key)
        return self._err.get(key, "")

    # ---- 角色 / 系统权限 ----
    def role_states(self) -> List[dict]:
        out = []
        for p in ROLE_PROBES:
            if self.is_demo:
                out.append(dict(p, ok=(p["key"] == "priv_any_table"), error="演示库不适用"))
                continue
            try:
                v = self.session.query_value(p["sql"], 0)
                ok = bool(int(v or 0))
                err = ""
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e).strip().splitlines()[0][:120]
            out.append(dict(p, ok=ok, error=err))
        return out

    # ---- 功能级别 ----
    def level(self, feature: str) -> str:
        """返回某功能可用的数据层级：full / 二级 / 三级 / none。"""
        if feature == "instance":
            if self.has_all("v_instance", "v_database"):
                return "full"
            if self.has("v_instance"):
                return "instance"
            if self.has("v_version"):
                return "version"
            return "none"
        if feature == "sessions":
            return "full" if self.has("v_session") else "none"
        if feature == "slow_sql":
            if self.has("v_sqlarea"):
                return "sqlarea"
            if self.has("v_sql"):
                return "sql"
            return "none"
        if feature == "tablespace":
            if self.has_all("dba_data_files", "dba_free_space"):
                return "full"
            if self.has("dba_ts_metrics"):
                return "metrics"
            if self.has("user_segments"):
                return "own"
            return "none"
        if feature == "segments":
            if self.has("dba_segments"):
                return "full"
            if self.has("user_segments"):
                return "own"
            return "none"
        if feature == "locks":
            if self.has_all("v_locked_object", "v_session"):
                return "full"
            if self.has("v_lock"):
                return "lock"
            return "none"
        return "none"

    def missing_hint(self, feature: str) -> str:
        """功能不可用 / 被降级时，界面上要显示给用户的那句话。"""
        lv = self.level(feature)
        if feature == "slow_sql":
            if lv == "none":
                return "当前账号看不到 V$SQLAREA / V$SQL（需要 SELECT_CATALOG_ROLE 或 SELECT ON V_$SQLAREA）。"
            return ""
        if feature == "tablespace":
            if lv == "none":
                return "当前账号既看不到 DBA_* 表空间视图，也没有 USER_SEGMENTS，无法估算空间用量。"
            if lv == "own":
                return "没有 DBA_* 权限，只能统计当前用户自己的段占用（不是整库用量）。"
            if lv == "metrics":
                return "看不到 DBA_FREE_SPACE，改用 DBA_TABLESPACE_USAGE_METRICS 估算（结果为近似值）。"
            return ""
        if feature == "instance":
            if lv == "none":
                return "当前账号看不到 V$DATABASE / V$INSTANCE（需要 SELECT_CATALOG_ROLE）。"
            return ""
        if feature == "sessions":
            if lv == "none":
                return "当前账号看不到 V$SESSION（需要 SELECT_CATALOG_ROLE 或 SELECT ON V_$SESSION）。"
            return ""
        if feature == "locks":
            if lv == "none":
                return "当前账号看不到 V$LOCKED_OBJECT / V$LOCK，无法查锁。"
            return ""
        return ""

    # ---- 体检报告 ----
    def report(self) -> List[dict]:
        out = []
        for cap in CAPABILITIES:
            ok = self.has(cap["key"])
            out.append(dict(key=cap["key"], label=cap["label"], ok=ok,
                            need=cap["need"] if not ok else "",
                            error=self._err.get(cap["key"], "")))
        return out

    def feature_report(self) -> List[Tuple[str, str, str]]:
        """(功能名, 可用级别, 说明)。"""
        names = [("instance", "实例概览"), ("sessions", "会话统计"), ("slow_sql", "慢 SQL"),
                 ("tablespace", "表空间用量"), ("segments", "段占用排行"), ("locks", "锁与阻塞")]
        out = []
        for key, label in names:
            lv = self.level(key)
            if lv == "full":
                desc = "完整可用"
            elif lv == "none":
                desc = self.missing_hint(key) or "不可用"
            else:
                desc = {
                    "instance": "只能看到版本（看不到实例名/启动时间）",
                    "sqlarea": "使用 V$SQLAREA", "sql": "改用 V$SQL（按子游标累计，可能有重复）",
                    "metrics": self.missing_hint("tablespace") or "近似值",
                    "own": self.missing_hint("tablespace") or "仅当前用户",
                    "lock": "只能看 V$LOCK，无法直接定位被锁对象",
                }.get(lv, "部分可用")
            out.append((label, lv, desc))
        return out

    def summary_text(self, user: str = "", version: str = "") -> str:
        """一键复制的诊断信息：贴给 DBA 让他决定要不要授权。"""
        lines = ["Oracle 轻量客户端 —— 权限自检结果"]
        if user:
            lines.append(f"当前用户：{user}")
        if version:
            lines.append(f"数据库版本：{version}")
        if self.is_demo:
            lines.append("连接类型：内置演示库（无 Oracle 字典视图）")
        lines.append("")
        lines.append("【可用功能】")
        for label, lv, desc in self.feature_report():
            flag = "✔" if lv != "none" else "✘"
            lines.append(f"  {flag} {label}：{desc}")
        lines.append("")
        lines.append("【视图可用性】")
        for item in self.report():
            flag = "✔" if item["ok"] else "✘"
            tail = "" if item["ok"] else f"  —— 需要：{item['need']}"
            lines.append(f"  {flag} {item['label']}{tail}")
        lines.append("")
        lines.append("【角色与系统权限】")
        for p in self.role_states():
            flag = "✔" if p["ok"] else "✘"
            lines.append(f"  {flag} {p['label']}")
        return "\n".join(lines)

    def refresh(self):
        self._cache.clear()
        self._err.clear()


# 会话级缓存：同一个连接只体检一次，避免每次开窗口都去查字典
_PROBES: Dict[int, PrivilegeProbe] = {}


def probe_for(session) -> PrivilegeProbe:
    if session is None:
        return PrivilegeProbe(None)
    key = id(session)
    p = _PROBES.get(key)
    if p is None:
        p = PrivilegeProbe(session)
        _PROBES[key] = p
    return p


def drop(session) -> None:
    if session is not None:
        _PROBES.pop(id(session), None)
