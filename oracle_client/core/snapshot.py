"""数据库文本档案：把"这个库长什么样"写成一段可复制的文本。

为什么要有这个模块（内网场景）：
    现场人员在内网，无法把库暴露给外部，也无法让远程工程师远程连进来。
    于是把"结构 + 权限 + 报错"写成一段 Markdown，由人复制出去（聊天/邮件/贴给 AI），
    对方在没有库访问权限的情况下也能判断问题；结论再被人带回来。

三条硬规则：
  1. **只出元数据，绝不出业务数据行**。文本要离开内网，不能带真实业务数据，也不带口令；
  2. **权限不足要如实写进档案**，而不是留空白——否则对方会误判为"这项没问题"；
  3. **体积可控**。表多的时候按规则截断，并写明"共 N 项，此处收录 M 项"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from . import ops as OPS
from . import utils as U

LEVEL_LABELS = {"brief": "精简", "standard": "标准"}

PRIVACY_NOTE = (
    "本档案只包含数据库结构元数据（有哪些对象、字段、索引、约束、行数），"
    "不包含任何业务数据行，也不包含连接口令。"
)

CELL_LIMIT = 80


# ---------------------------------------------------------------- 选项
@dataclass
class SnapshotOptions:
    """一次档案生成的选项。

    level: brief=只要"有哪些东西"；standard=再加表结构明细与运行状况。
    tables: 指定要展开的表名；为空时按规则自动挑选（标准档=按行数倒序）。
    """

    level: str = "standard"
    schema: str = ""
    tables: List[str] = field(default_factory=list)
    max_tables: int = 30
    max_columns: int = 40
    keyword: str = ""
    exact_counts: bool = False          # 精确行数要跑 COUNT(*)，大表很慢，默认关
    include_ddl: bool = False
    include_ops: bool = True
    detail_tables: bool = True          # 是否展开表结构明细
    problem: str = ""
    error_text: str = ""
    sql_text: str = ""


# ---------------------------------------------------------------- 小工具
def _cell(v: Any, limit: int = CELL_LIMIT) -> str:
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    s = s.replace("|", "\\|")
    return U.clip_text(s, limit)


def _md_table(cols: Sequence[str], rows: Sequence[tuple], max_rows: Optional[int] = None) -> List[str]:
    if not cols:
        return ["（无数据）"]
    head = "| " + " | ".join(_cell(c) for c in cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    out = [head, sep]
    n = 0
    for r in rows or []:
        if max_rows is not None and n >= max_rows:
            break
        out.append("| " + " | ".join(_cell(x) for x in r) + " |")
        n += 1
    if not rows:
        out.append("| " + " | ".join(["（空）"] * len(cols)) + " |")
    return out


def _fence(text: str, lang: str = "") -> List[str]:
    body = (text or "").strip().replace("```", "'''")
    return ["```" + lang, body, "```"]


def _rows_text(n: Optional[int], exact: bool) -> str:
    if n is None:
        return "未知"
    return f"{U.human_number(n)}（{'精确统计' if exact else '统计值，非实时'}）"


# ---------------------------------------------------------------- 各章节
def _section_conn(session, opts: SnapshotOptions) -> List[str]:
    p = getattr(session, "profile", None)
    ad = getattr(session, "adapter", None)
    user = getattr(ad, "current_user", "") or getattr(p, "user", "") or ""
    schema = (opts.schema or "").strip() or (getattr(session, "default_schema", "") or "")
    lines = ["- 连接名：" + _cell(getattr(p, "name", "") or "（未命名）")]
    env = getattr(p, "env_label", "未标注") if p else "未标注"
    if p is not None and getattr(p, "is_prod", False):
        env += "（生产环境，操作请谨慎）"
    lines.append("- 环境标识：" + env)
    lines.append("- 账号：" + _cell(user))
    lines.append("- 当前 schema：" + _cell(schema))
    if p is not None:
        lines.append("- 目标：" + _cell(p.display_dsn()))
    ver = getattr(ad, "server_version", "") or ""
    lines.append("- 数据库版本：" + (_cell(ver) if ver else "未知"))
    lines.append("- 连接类型：" + ("内置演示库（不是真实 Oracle）" if getattr(p, "demo", False) else "Oracle"))
    return lines


def _section_problem(opts: SnapshotOptions) -> List[str]:
    lines: List[str] = []
    if (opts.problem or "").strip():
        lines.append((opts.problem or "").strip())
        lines.append("")
    if (opts.error_text or "").strip():
        lines.append("报错原文：")
        lines.extend(_fence(opts.error_text))
        lines.append("")
    if (opts.sql_text or "").strip():
        lines.append("相关 SQL：")
        lines.extend(_fence(opts.sql_text, "sql"))
        lines.append("")
    return lines


def _section_perms(probe) -> List[str]:
    if probe is None:
        return ["（本次未做权限探测）"]
    lines = ["【功能可用性】"]
    for label, lv, desc in probe.feature_report():
        flag = "可用" if lv != "none" else "不可用"
        tail = "" if str(desc).strip() == flag else f" —— {desc}"
        lines.append(f"- {label}：{flag}{tail}")
    try:
        missing = [it for it in probe.report() if not it.get("ok")]
    except Exception:  # noqa: BLE001
        missing = []
    if missing:
        lines.append("")
        lines.append("【缺失的字典视图】以下视图当前账号看不到，相关章节会缺失或降级，请对照理解：")
        for it in missing:
            lines.append(f"- {it.get('label','')} —— 需要：{it.get('need','')}")
    if getattr(probe, "is_demo", False):
        lines.append("")
        lines.append("（当前是内置演示库，没有 Oracle 字典视图，以上结果不代表真实环境。）")
    return lines


def _pick_tables(session, schema: str, opts: SnapshotOptions) -> Tuple[List[Any], int]:
    """返回 (选中的 TableMeta 列表, 总对象数)。绝不抛异常。"""
    try:
        objs = list(session.objects(schema))
    except Exception:  # noqa: BLE001
        return [], 0
    total = len(objs)
    wanted = [str(t).strip().upper() for t in (opts.tables or []) if str(t).strip()]
    kw = (opts.keyword or "").strip().upper()
    if wanted:
        sel = [o for o in objs if (o.name or "").upper() in wanted]
    elif kw:
        sel = [o for o in objs if kw in (o.name or "").upper()]
    else:
        sel = list(objs)
    sel.sort(key=lambda o: (-(o.num_rows or -1), (o.name or "")))
    return sel[: max(1, int(opts.max_tables))], total


def _section_objects(session, schema: str, opts: SnapshotOptions, detail: bool):
    sel, total = _pick_tables(session, schema, opts)
    lines: List[str] = []
    picked: List[Any] = []
    if not sel:
        lines.append(f"当前 schema 下没有读到对象（可能是权限不足，或 schema 名不对：{schema}）。")
        return lines, picked
    by_type: dict = {}
    try:
        for o in session.objects(schema):
            by_type[o.object_type] = by_type.get(o.object_type, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    summary = " · ".join(f"{k} {v}" for k, v in sorted(by_type.items()))
    lines.append(f"按类型：{summary}" if summary else "")
    shown = len(sel)
    tail = f"；此处收录 {shown} 个" if shown < total else ""
    lines.append(f"schema {schema} 下共 {total} 个对象{tail}。")
    lines.append("")
    if detail:
        cols = ["对象", "类型", "列数", "行数", "主键"]
        rows = []
        for o in sel:
            ncol, pk = "?", ""
            try:
                cs = list(session.columns(schema, o.name))
                ncol = str(len(cs))
                pk = ", ".join(c.name for c in cs if c.is_pk)
            except Exception:  # noqa: BLE001
                pass
            rows.append((o.name, o.object_type, ncol, _rows_text(o.num_rows, False), pk))
            picked.append(o)
    else:
        cols = ["对象", "类型", "行数"]
        rows = [(o.name, o.object_type, _rows_text(o.num_rows, False)) for o in sel]
    lines.extend(_md_table(cols, rows))
    return lines, picked


def _section_table(session, schema: str, o: Any, opts: SnapshotOptions) -> List[str]:
    name = o.name
    lines = [f"### {schema}.{name}"]
    meta = []
    meta.append(f"类型：{o.object_type}")
    n = o.num_rows
    if opts.exact_counts:
        try:
            n = session.rowcount(schema, name)
        except Exception:  # noqa: BLE001
            n = o.num_rows
    meta.append("行数：" + _rows_text(n, opts.exact_counts))
    lines.append("　".join(meta))
    if (o.comment or "").strip():
        lines.append("注释：" + _cell(o.comment))
    lines.append("")

    try:
        cols = list(session.columns(schema, name))
    except Exception as e:  # noqa: BLE001
        lines.append(f"读不到字段信息：{e}")
        return lines
    if not cols:
        lines.append("（没有读到字段，可能无权限或对象类型特殊）")
        return lines
    shown = cols[: max(1, int(opts.max_columns))]
    rows = []
    for c in shown:
        rows.append((c.position, c.name, c.type_label,
                     "是" if c.nullable else "否（必填）",
                     (c.default or ""), (c.comment or ""), "主键" if c.is_pk else ""))
    lines.extend(_md_table(["#", "列名", "类型", "可空", "默认值", "说明", "键"], rows))
    if len(cols) > len(shown):
        lines.append(f"（共 {len(cols)} 列，此处收录前 {len(shown)} 列）")
    pk = sorted([c.name for c in cols if c.is_pk])
    lines.append("")
    lines.append("主键：" + (", ".join(pk) if pk else "无"))

    try:
        idx = session.indexes(schema, name) or []
    except Exception:  # noqa: BLE001
        idx = []
    if idx:
        lines.append("索引：")
        for i in idx:
            uniq = "唯一" if str(i.get("unique", "")).upper().startswith("UNIQ") else "非唯一"
            lines.append(f"- {i.get('name','')}（{', '.join(str(i.get('columns') or '').split(','))}）{uniq}"
                         + (f"，状态 {i.get('status')}" if i.get("status") else ""))
    else:
        lines.append("索引：（读不到或没有）")

    try:
        fks = session.foreign_keys(schema, name) or []
    except Exception:  # noqa: BLE001
        fks = []
    if fks:
        lines.append("外键：")
        for f in fks:
            lines.append(f"- {f.get('column','')} → {f.get('ref_schema','')}.{f.get('ref_table','')}"
                         f"（{f.get('ref_column','')}）")
    if opts.include_ddl:
        try:
            ddl = session.ddl(schema, name) or ""
        except Exception:  # noqa: BLE001
            ddl = ""
        if ddl.strip():
            lines.append("")
            lines.append("建表语句：")
            lines.extend(_fence(ddl, "sql"))
    lines.append("")
    return lines


def _section_ops(session, probe, opts: SnapshotOptions) -> List[str]:
    lines: List[str] = []
    if not opts.include_ops:
        return lines
    for kind, title, kw in (
        ("slow", "慢 SQL（Top 10，按平均耗时）", {"top_n": 10, "order_by": "平均耗时"}),
        ("tablespace", "表空间使用率", {}),
    ):
        try:
            cols, rows, note, level = OPS.fetch(session, probe, kind, **kw)
        except Exception as e:  # noqa: BLE001
            cols, rows, note, level = [], [], f"读取失败：{e}", "error"
        lines.append(f"### {title}")
        if not rows:
            lines.append(f"无法获取：{note or '当前账号权限不足，或该库没有这类信息。'}")
            lines.append("")
            continue
        if note:
            lines.append("说明：" + _cell(note, 200))
        if level == "demo":
            lines.append("（演示数据）")
        lines.extend(_md_table(cols, rows))
        lines.append("")
    return lines


# ---------------------------------------------------------------- 主入口
def build(session, probe=None, opts: Optional[SnapshotOptions] = None) -> str:
    """生成档案文本。任何一步取不到数据都降级成一句说明，绝不抛异常。"""
    if session is None:
        return "# 数据库档案（离线求助用）\n\n当前没有连接数据库，无法生成档案。\n"
    opts = opts or SnapshotOptions()
    out: List[str] = ["# 数据库档案（离线求助用）", ""]
    out.append(f"生成时间：{U.now_str()}　　篇幅：{LEVEL_LABELS.get(opts.level, opts.level)}")
    out.append(PRIVACY_NOTE)
    out.append("")

    schema = (opts.schema or "").strip()
    if not schema:
        try:
            schema = session.default_schema or ""
        except Exception:  # noqa: BLE001
            schema = ""

    no = 0

    no += 1
    out.append(f"## {no}. 连接与环境")
    out.extend(_section_conn(session, opts))
    out.append("")

    if (opts.problem or "").strip() or (opts.error_text or "").strip() or (opts.sql_text or "").strip():
        no += 1
        out.append(f"## {no}. 我遇到的问题")
        out.extend(_section_problem(opts))

    no += 1
    out.append(f"## {no}. 权限自检（当前账号能看什么）")
    out.extend(_section_perms(probe))
    out.append("")

    no += 1
    out.append(f"## {no}. 对象清单（schema {schema or '未知'}）")
    obj_lines, picked = _section_objects(session, schema, opts,
                                         detail=opts.detail_tables and opts.level != "brief")
    out.extend(obj_lines)
    out.append("")

    if opts.detail_tables and picked:
        no += 1
        out.append(f"## {no}. 表结构明细")
        for o in picked:
            out.extend(_section_table(session, schema, o, opts))

    if opts.level == "standard":
        no += 1
        out.append(f"## {no}. 运行状况快照")
        ops_lines = _section_ops(session, probe, opts)
        out.extend(ops_lines or ["（本档未包含）", ""])

    out.append("## 附：给协助者的话")
    out.append("- 以上由客户端自动生成，只含结构元数据，不含任何业务数据行；")
    out.append("- 标注“无法获取 / 不可用”的项，是当前账号权限不足，**不代表该项没有问题**；")
    out.append("- 行数默认来自统计信息，可能不是实时值。")
    return "\n".join(out).rstrip() + "\n"


def problem_context(session, probe=None, problem: str = "", error_text: str = "",
                    sql_text: str = "", schema: str = "", tables: Optional[List[str]] = None,
                    max_tables: int = 6) -> str:
    """报错场景用：生成一段"问题上下文"。

    与完整档案的区别：只围绕出问题的那几张表展开，文本短一些，方便直接贴出去。
    """
    tables = [t for t in (tables or []) if t]
    opts = SnapshotOptions(
        level="standard",
        schema=schema,
        tables=tables,
        max_tables=max(len(tables), 1) if tables else max_tables,
        max_columns=30,
        include_ops=True,
        detail_tables=True,
        problem=problem,
        error_text=error_text,
        sql_text=sql_text,
    )
    return build(session, probe, opts)
