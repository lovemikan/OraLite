"""开发自检脚本：核心逻辑 + 界面冒烟测试（不需要真实 Oracle）。

运行方式：
    .venv\\Scripts\\python.exe tools\\dev_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle_client.core import backup as BK  # noqa: E402
from oracle_client.core import explore as XP  # noqa: E402
from oracle_client.core import exporter as EX  # noqa: E402
from oracle_client.core import ops as OPS  # noqa: E402
from oracle_client.core import perms as PM  # noqa: E402
from oracle_client.core import snapshot as SNAP  # noqa: E402
from oracle_client.core import sqlbuilder as SB  # noqa: E402
from oracle_client.core.db import create_session  # noqa: E402
from oracle_client.core.models import Condition, ConnProfile, OrderTerm, QuerySpec  # noqa: E402

FAILED = []


def check(name: str, fn):
    try:
        fn()
        print(f"  ✔ {name}")
    except Exception as e:  # noqa: BLE001
        FAILED.append((name, e))
        print(f"  ✘ {name} → {e}")
        traceback.print_exc()


def core_tests():
    print("\n[1] 核心逻辑（内置演示库）")
    p = ConnProfile(name="演示数据库（无需 Oracle）", demo=True, user="DEMO")
    s = create_session(p)

    def t_schemas():
        assert "DEMO" in s.schemas(), s.schemas()

    check("列出模式", t_schemas)

    def t_objects():
        objs = s.objects("DEMO")
        names = [o.name for o in objs]
        assert "EMPLOYEES" in names and "ORDERS" in names, names
        assert any(o.object_type == "VIEW" for o in objs), [o.object_type for o in objs]

    check("列出表/视图", t_objects)

    cols = s.columns("DEMO", "EMPLOYEES")

    def t_columns():
        assert len(cols) >= 8, [c.name for c in cols]
        assert any(c.is_pk for c in cols), "没识别到主键"

    check("读取字段与主键", t_columns)

    types = s.column_types("DEMO", "EMPLOYEES")

    def t_select():
        spec = QuerySpec(schema="DEMO", table="EMPLOYEES",
                         columns=["EMP_ID", "EMP_NAME", "SALARY", "HIRE_DATE"],
                         conditions=[Condition(column="DEPT_ID", op="=", value="3"),
                                     Condition(column="SALARY", op=">", value="8000", logic="AND")],
                         order_by=[OrderTerm(column="SALARY", direction="DESC")], limit=10)
        sql = SB.build_select(spec, types, s.dialect)
        sql = SB.apply_row_limit(sql, 10, s.dialect)
        print("     生成的 SQL：\n" + "\n".join("       " + l for l in sql.splitlines()))
        res = s.query(sql.rstrip(";"))
        assert res.rows, "查询没有返回数据"
        for r in res.rows:
            assert r[0] is not None

    check("引导式生成 SELECT 并查询", t_select)

    def t_like():
        spec = QuerySpec(schema="DEMO", table="EMPLOYEES",
                         conditions=[Condition(column="EMP_NAME", op="LIKE", value="张")], limit=50)
        sql = SB.build_select(spec, types, s.dialect)
        assert "LIKE '%张%'" in sql, sql
        res = s.query(sql.rstrip(";"))
        print(f"     模糊匹配返回 {len(res.rows)} 行")

    check("模糊匹配（包含）", t_like)

    def t_between():
        spec = QuerySpec(schema="DEMO", table="EMPLOYEES",
                         conditions=[Condition(column="SALARY", op="BETWEEN", value="10000", value2="20000")],
                         limit=50)
        sql = SB.build_select(spec, types, s.dialect)
        res = s.query(sql.rstrip(";"))
        assert res.rows, sql

    check("区间条件 BETWEEN", t_between)

    def t_insert():
        sql = SB.build_insert("DEMO", "DEPARTMENTS",
                              {"DEPT_ID": 99, "DEPT_NAME": "测试部门", "CITY": "深圳",
                               "CREATE_DATE": "2024-05-01"},
                              s.column_types("DEMO", "DEPARTMENTS"), s.dialect)
        rc = s.execute(sql)
        assert rc == 1, rc
        assert s.query_value("SELECT COUNT(*) FROM DEMO.DEPARTMENTS WHERE DEPT_ID = 99") == 1
        s.execute("DELETE FROM DEMO.DEPARTMENTS WHERE DEPT_ID = 99")
        s.commit()

    check("生成并执行 INSERT", t_insert)

    # ---- 备份 / 还原 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="oracle_client_bk_"))
    mgr = BK.BackupManager(tmpdir)

    def t_backup_restore():
        capture = ('SELECT rowid AS ROWID__, t.* FROM "DEMO"."EMPLOYEES" t WHERE DEPT_ID = 1'
                   if s.dialect == "sqlite" else
                   "SELECT ROWID AS ROWID__, t.* FROM DEMO.EMPLOYEES t WHERE DEPT_ID = 1")
        res = s.query(capture)
        assert res.rows, "抓取不到数据"
        rec = mgr.create(connection="演示", schema="DEMO", table="EMPLOYEES", operation="UPDATE",
                         sql="UPDATE ...", capture_sql=capture, columns=res.columns, rows=res.rows,
                         pk_columns=["EMP_ID"])
        assert rec and rec.rowcount == len(res.rows)
        before = s.query_value("SELECT SUM(SALARY) FROM DEMO.EMPLOYEES WHERE DEPT_ID = 1")
        s.execute("UPDATE DEMO.EMPLOYEES SET SALARY = 1 WHERE DEPT_ID = 1")
        after = s.query_value("SELECT SUM(SALARY) FROM DEMO.EMPLOYEES WHERE DEPT_ID = 1")
        assert float(after) != float(before), f"{after} == {before}"
        stmts = BK.build_restore_statements(rec, s.dialect)
        assert stmts, "没有生成还原语句"
        for sql_i, params in stmts:
            s.execute(sql_i, params)
        s.commit()
        restored = s.query_value("SELECT SUM(SALARY) FROM DEMO.EMPLOYEES WHERE DEPT_ID = 1")
        old_sum = sum(float(r[list(res.columns).index("SALARY")]) for r in res.rows)
        assert abs(float(restored) - old_sum) < 0.01, f"{restored} != {old_sum}"
        assert mgr.list(), "备份列表为空"

    check("备份 → 改数据 → 一键还原", t_backup_restore)

    def t_export():
        res = s.query("SELECT * FROM DEMO.ORDERS", max_rows=500)
        xlsx = tmpdir / "导出测试.xlsx"
        n, sheets = EX.export_excel(str(xlsx), res.columns, iter(res.rows), sheet_name="订单")
        assert n == len(res.rows) and xlsx.exists()
        csv_path = tmpdir / "导出测试.csv"
        n2 = EX.export_csv(str(csv_path), res.columns, iter(res.rows))
        assert n2 == len(res.rows)
        sql_path = tmpdir / "导出测试.sql"
        n3 = EX.export_insert_sql(str(sql_path), "ORDERS", res.columns, iter(res.rows))
        assert n3 == len(res.rows)
        print(f"     导出 {n} 行 → {xlsx.name} / {csv_path.name} / {sql_path.name}")

    check("导出 Excel / CSV / SQL", t_export)

    def t_paginate():
        base = "SELECT * FROM DEMO.EMPLOYEES ORDER BY EMP_ID"
        p1 = s.query(s.paginate(base, 0, 10))
        p2 = s.query(s.paginate(base, 10, 10))
        assert len(p1.rows) == 10 and len(p2.rows) == 10
        assert p1.rows[0][0] != p2.rows[0][0]

    check("分页取数（加载更多）", t_paginate)

    def t_parse_insert():
        from oracle_client.ui.builders import parse_insert_values

        sql = ("INSERT INTO DEMO.DEPARTMENTS (DEPT_ID, DEPT_NAME, CITY) "
               "VALUES (98, '测试''部门', '深圳')")
        parsed = parse_insert_values(sql)
        assert parsed, "无法解析 INSERT"
        cols, row = parsed
        assert cols == ["DEPT_ID", "DEPT_NAME", "CITY"], cols
        assert row[1] == "测试'部门", row
        assert parse_insert_values("DELETE FROM X WHERE A=1") is None

    check("解析 INSERT 用于备份", t_parse_insert)

    def t_split():
        stmts = SB.split_statements("SELECT 1 FROM DUAL; SELECT ';' FROM DUAL;")
        assert len(stmts) == 2, stmts
        assert SB.statement_kind("update emp set x=1") == "UPDATE"
        assert SB.detect_table("UPDATE SCOTT.EMP SET X=1 WHERE ID=2") == ("SCOTT", "EMP")

    check("多语句拆分与语句识别", t_split)

    # ---------------------------------------------------------- 数据探查
    def t_strip_limit():
        limited = SB.apply_row_limit("SELECT * FROM DEMO.EMPLOYEES", 50, s.dialect)
        assert "50" in limited, limited
        assert "50" not in XP.normalize(limited, s.dialect), XP.normalize(limited, s.dialect)
        # 反复套限制不能层层嵌套
        again = SB.apply_row_limit(limited, 10, s.dialect)
        if s.dialect == "sqlite":
            assert again.upper().count("SELECT") == 1, again
        else:
            assert again.upper().count("ROWNUM") == 1, again

    check("探查：剥离行数限制", t_strip_limit)

    def t_dup():
        src = SB.apply_row_limit("SELECT * FROM DEMO.EMPLOYEES", 200, s.dialect)
        r = s.query(XP.dup_summary_sql(src, ["DEPT_ID"], s.dialect), max_rows=10)
        assert r.rows and len(r.rows[0]) == 2, r.rows[:2]
        r2 = s.query(XP.dup_detail_sql(src, ["DEPT_ID"], s.dialect), max_rows=10)
        assert r2.rows, "查重明细应该有行"
        r3 = s.query(XP.dup_summary_sql(src, ["DEPT_ID", "JOB"], s.dialect), max_rows=5)
        assert r3.rows and len(r3.rows[0]) == 3, r3.rows[:2]

    check("探查：查重汇总 / 明细 / 多列", t_dup)

    def t_distinct():
        src = "SELECT * FROM DEMO.EMPLOYEES"
        r = s.query(XP.distinct_sql(src, "JOB", s.dialect), max_rows=10)
        assert r.rows and len(r.rows[0]) == 2, r.rows[:2]
        d = XP.value_detail_sql(src, ["DEPT_ID"], [1], dialect=s.dialect)
        assert s.query(d, max_rows=5).rows, d

    check("探查：值分布与某值明细", t_distinct)

    def t_drill():
        src = "SELECT * FROM DEMO.EMPLOYEES"
        a = XP.drill_sql(src, "DEPT_ID", "DEMO", "DEPARTMENTS", "DEPT_ID", s.dialect)
        assert s.query(a, max_rows=10).rows, a
        b = XP.drill_sql(src, "DEPT_ID", "DEMO", "DEPARTMENTS", "DEPT_ID", s.dialect,
                         selected_values=[1, 2])
        assert s.query(b, max_rows=10).rows, b
        # 反向：拿员工号去订单表查
        c = XP.drill_sql(src, "EMP_ID", "DEMO", "ORDERS", "EMP_ID", s.dialect)
        assert s.query(c, max_rows=10).rows, c

    check("探查：跨表钻取（子查询 / 字面量）", t_drill)

    def t_in_chunk():
        # Oracle 的 IN 列表最多 1000 个值，超出必须分块，否则 ORA-01795
        pred = XP.in_predicate("ID", list(range(2500)), "NUMBER", s.dialect)
        assert pred.count(" IN (") == 3, pred.count(" IN (")

    check("探查：IN 列表超 1000 自动分块", t_in_chunk)

    def t_fk_fallback():
        # 演示库没有约束视图，应优雅返回空列表而不是抛异常
        assert s.foreign_keys("DEMO", "EMPLOYEES") == []

    check("探查：外键查询失败时优雅降级", t_fk_fallback)

    def t_perms_demo():
        pr = PM.PrivilegeProbe(s)
        assert pr.is_demo
        assert pr.has("basic") is True
        # 演示库没有 Oracle 字典视图，必须如实报告“没有”，不能假装能查
        assert pr.has("v_session") is False
        assert pr.level("slow_sql") == "none"
        assert pr.level("tablespace") == "none"
        assert "演示库" in pr.missing_hint("slow_sql") or "V$SQLAREA" in pr.missing_hint("slow_sql")
        text = pr.summary_text(user="DEMO", version="演示库 1.0")
        assert "权限自检结果" in text and "不可用" in text, text[:200]

    check("权限探测：演示库如实报告不可用", t_perms_demo)

    def t_perms_unknown():
        pr = PM.PrivilegeProbe(s)
        # 未知能力不能误判成“有”
        assert pr.has("no_such_capability") is False
        assert pr.has_any("v_session", "v_sql") is False

    check("权限探测：未知能力不误判", t_perms_unknown)

    def t_ops_sql():
        a = OPS.slow_sql_sql("sqlarea", top_n=30, min_avg_sec=1.5, order_by="总耗时", keyword="EMP")
        assert "V$SQLAREA" in a and "ROWNUM <= 30" in a and "EMP" in a
        b = OPS.slow_sql_sql("sql", top_n=10)
        assert "V$SQL" in b and "V$SQLAREA" not in b
        for lvl, token in (("full", "DBA_FREE_SPACE"), ("metrics", "DBA_TABLESPACE_USAGE_METRICS"),
                           ("own", "USER_SEGMENTS")):
            assert token in OPS.tablespace_sql(lvl), lvl
        assert "V$INSTANCE" in OPS.instance_sql("full")
        assert "V$VERSION" in OPS.instance_sql("version")
        assert "USER" in OPS.instance_sql("none")

    check("运维 SQL：按权限级别生成不同语句", t_ops_sql)

    def t_ops_fetch_demo():
        pr = PM.PrivilegeProbe(s)
        for kind in ("instance", "slow", "tablespace"):
            cols, rows, note, level = OPS.fetch(s, pr, kind)
            assert cols and rows, kind
            assert level == "demo", (kind, level)
            assert "演示数据" in note, note
        _, _, note, level = OPS.fetch(s, pr, "sessions")
        assert level == "demo" and rows is not None

    check("运维取数：演示库返回带标注的样例数据", t_ops_fetch_demo)

    def t_env_flag():
        p1 = ConnProfile(name="A", env="生产")
        p2 = ConnProfile(name="B")
        assert p1.is_prod and p1.env_label == "生产"
        assert not p2.is_prod and p2.env_label == "未标注"
        assert p1.to_dict()["env"] == "生产"
        assert ConnProfile.from_dict({"name": "C", "env": "测试"}).env == "测试"

    check("环境标识：生产标记与序列化", t_env_flag)

    # ---------------------------------------------------------- 数据库档案
    def t_snapshot_standard():
        pr = PM.PrivilegeProbe(s)
        text = SNAP.build(s, pr, SNAP.SnapshotOptions(level="standard", schema="DEMO", max_tables=10))
        assert "数据库档案" in text and "连接与环境" in text, text[:200]
        assert "对象清单" in text and "表结构明细" in text, text[:400]
        assert "EMPLOYEES" in text and "ORDERS" in text
        # 底线：档案要拿得出内网，绝不能夹带业务数据行
        r = s.query("SELECT EMP_NAME FROM DEMO.EMPLOYEES", max_rows=30)
        for (nm,) in r.rows:
            assert str(nm) not in text, f"档案里出现了业务数据：{nm}"
        # 权限不足必须写清楚，不能留白让人误判
        assert "不可用" in text or "缺失" in text, text[-400:]
        print(f"     标准档档案：{len(text)} 字符")

    check("档案：标准档含结构与权限说明，且不含业务数据", t_snapshot_standard)

    def t_snapshot_brief():
        text = SNAP.build(s, PM.PrivilegeProbe(s),
                          SNAP.SnapshotOptions(level="brief", schema="DEMO"))
        assert "对象清单" in text, text[:200]
        assert "表结构明细" not in text, "精简档不该展开表结构"

    check("档案：精简档只给对象清单", t_snapshot_brief)

    def t_snapshot_context():
        ctx = SNAP.problem_context(s, PM.PrivilegeProbe(s),
                                   problem="查 ORDERS 时报 ORA-00942",
                                   error_text="ORA-00942: table or view does not exist",
                                   sql_text="SELECT * FROM ORDERS WHERE STATUS = 'X'",
                                   schema="DEMO", tables=["ORDERS"])
        assert "ORA-00942" in ctx, ctx[:300]
        assert "SELECT * FROM ORDERS" in ctx, ctx[:600]
        assert "### DEMO.ORDERS" in ctx, "问题上下文没展开出问题的那张表"
        assert "### DEMO.EMPLOYEES" not in ctx, "指定表之后不该把整库表都塞进来"

    check("档案：问题上下文围绕出错的表展开", t_snapshot_context)

    def t_snapshot_no_session():
        assert "没有连接" in SNAP.build(None, None, None)

    check("档案：没连接时给出说明而不是崩溃", t_snapshot_no_session)


def dialog_content_overflow(win) -> str:
    """检查对话框是否装得下内容。可滚动主体只要能滚动到就不算问题。

    返回空字符串表示没问题，否则返回描述。
    """
    from oracle_client.ui.widgets import ScrolledBody

    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    bodies = []
    stack = list(win.winfo_children())

    def scan(wgt):
        if isinstance(wgt, ScrolledBody):
            bodies.append(wgt)
            return
        for c in wgt.winfo_children():
            scan(c)

    for c in win.winfo_children():
        scan(c)
    if bodies:  # 有可滚动主体：内容高一点没关系，能滚到即可
        return ""
    rw, rh = win.winfo_reqwidth(), win.winfo_reqheight()
    if rw > w + 4 or rh > h + 4:
        return f"需要 {rw}x{rh}，实际 {w}x{h}"
    return ""


def gui_tests():
    print("\n[2] 界面冒烟测试（会短暂弹出窗口）")

    import threading
    import time

    def _watchdog():
        time.sleep(120)
        print("!! 看门狗超时：界面流程卡住，强制退出", flush=True)
        os._exit(2)

    threading.Thread(target=_watchdog, daemon=True).start()
    from oracle_client.ui.app import Application
    from oracle_client.ui.builders import DeleteDialog, InsertDialog, QueryWizard, UpdateDialog
    from oracle_client.ui.dialogs import BackupCenter, ExportDialog, TableInfoDialog

    app = Application()
    errors = []
    app.root.report_callback_exception = lambda *a: errors.append(a)

    def actions():
        try:
            name = app.conn_store.profiles[0].name
            app.open_connection(name, parent=app.root, silent=True)
            print(f"     已连接：{app.current_session_name}")

            q = app.new_query_window(sql="SELECT * FROM DEMO.EMPLOYEES")
            q.run_all()
            q.run_selected()
            print(f"     查询窗口结果页：{len(q.result_nb.tabs())}")

            t = app.new_table_window("DEMO", "EMPLOYEES")
            t.refresh_data()
            print(f"     数据窗口行数：{len(t.data_grid.raw)}")
            t.generate_sql()

            # 引导式向导（只走界面，不点完成）
            wz = QueryWizard(app.root, app, session_name=app.current_session_name,
                             schema="DEMO", table="EMPLOYEES")
            wz._load_tables()
            wz.step = 4
            wz._render_step()
            sql = wz.preview.get_sql()
            assert "SELECT" in sql.upper(), sql
            print("     向导生成语句：", sql.replace("\n", " ")[:90])
            wz.destroy()

            cols = app.current_session.columns("DEMO", "EMPLOYEES")
            for cls, args in ((InsertDialog, (app.root, app, app.current_session_name, "DEMO",
                                              "EMPLOYEES", cols)),
                              (UpdateDialog, (app.root, app, app.current_session_name, "DEMO",
                                              "EMPLOYEES", cols, {"EMP_ID": 1, "EMP_NAME": "张三"})),
                              (DeleteDialog, (app.root, app, app.current_session_name, "DEMO",
                                              "EMPLOYEES", cols, {"EMP_ID": 1}))):
                d = cls(*args)
                if cls is UpdateDialog:
                    for name, (var, _vi) in d.inputs.items():
                        if name != "EMP_ID":
                            var.set(True)
                            break
                d.generate()
                sql_i = d.preview.get_sql().strip()
                assert sql_i, f"{cls.__name__} 没有生成语句"
                print(f"     {cls.__name__} 生成：{sql_i.splitlines()[0][:70]}")
                d.destroy()

            ti = TableInfoDialog(app.root, app, app.current_session_name, "DEMO", "EMPLOYEES")
            ti.destroy()
            bc = BackupCenter(app.root, app)
            bc.destroy()
            ex = ExportDialog(app.root, app, default_name="测试",
                              providers={"当前视图": lambda: (t.data_grid.columns, t.data_grid.visible_rows())},
                              table_name="EMPLOYEES")
            ex.destroy()
            from oracle_client.ui.dialogs import (AboutDialog, ConnectionDialog, ConnectionManager,
                                                  HistoryDialog, SettingsDialog)
            from oracle_client.ui import widgets as W2

            for factory in (lambda: ConnectionDialog(app.root, app, None),
                            lambda: ConnectionManager(app.root, app),
                            lambda: SettingsDialog(app.root, app),
                            lambda: HistoryDialog(app.root, app, app.favorites, on_pick=lambda s: None),
                            lambda: AboutDialog(app.root, app),
                            lambda: InsertDialog(app.root, app, app.current_session_name, "DEMO",
                                                 "EMPLOYEES", cols),
                            lambda: UpdateDialog(app.root, app, app.current_session_name, "DEMO",
                                                 "EMPLOYEES", cols, {"EMP_ID": 1}),
                            lambda: DeleteDialog(app.root, app, app.current_session_name, "DEMO",
                                                 "EMPLOYEES", cols, {"EMP_ID": 1})):
                d = factory()
                d.update_idletasks()
                d.update()
                bad = dialog_content_overflow(d)
                assert not bad, f"{type(d).__name__} 内容装不下：{bad}"
                print(f"     {type(d).__name__} 创建成功 ({d.winfo_width()}x{d.winfo_height()})")
                d.destroy()

            # ---- 数据探查 ----
            from oracle_client.ui.explore import DrillDialog, DupDialog, ExploreWindow

            dia = app.current_session.dialect
            dsrc = "SELECT * FROM DEMO.EMPLOYEES"
            dd = DupDialog(app.root, app, dsrc, ["EMP_ID", "DEPT_ID", "JOB"], "DEPT_ID", dia)
            sql_dup = dd.preview.get_sql().strip()
            assert sql_dup and "GROUP" in sql_dup.upper(), sql_dup
            print("     DupDialog 生成：", sql_dup.replace("\n", " ")[:70])
            dd.destroy()

            rd = DrillDialog(app.root, app, app.current_session_name, dsrc, "DEPT_ID",
                             selected_values=[1, 2], schema="DEMO",
                             source_table="EMPLOYEES", dialect=dia)
            print("     DrillDialog 推荐：", rd.hint_var.get()[:60])
            rd.destroy()

            ew = ExploreWindow(app, app.current_session_name, dsrc, title="探查测试",
                               dialect=dia, schema="DEMO", source_table="EMPLOYEES")
            assert ew.steps and ew.steps[0]["rows"], "探查窗口首步没有数据"
            print(f"     ExploreWindow 首步行数：{len(ew.steps[0]['rows'])}")
            ew.on_distinct("DEPT_ID")
            kids = ew.data_grid.tree.get_children()
            if kids:
                ew.data_grid.tree.selection_set(kids[0])
            ew.detail_of_value()
            print(f"     ExploreWindow 探查链步数：{len(ew.steps)}")
            assert len(ew.steps) >= 3, len(ew.steps)
            ew.destroy()

            # ---- 运维概览（实例 / 慢 SQL / 表空间 / 权限自检）----
            from oracle_client.ui.ops import OpsWindow

            ow = OpsWindow(app, app.current_session_name)
            pr = PM.probe_for(app.current_session)
            sess = app.current_session
            ow._fill({
                "instance": OPS.fetch(sess, pr, "instance"),
                "sessions": OPS.fetch(sess, pr, "sessions"),
                "slow": OPS.fetch(sess, pr, "slow", top_n=20, min_avg_sec=0, order_by="平均耗时",
                                  keyword="", min_execs=1),
                "tablespace": OPS.fetch(sess, pr, "tablespace"),
                "segments": OPS.fetch(sess, pr, "segments", top_n=20),
                "features": pr.feature_report(),
                "perms": pr.report(),
                "roles": pr.role_states(),
            })
            assert ow.ov_grid.raw and ow.slow_grid.raw and ow.ts_grid.raw, "运维概览没有数据"
            assert ow.perm_grid.raw and ow.role_grid.raw, "权限自检没有数据"
            print(f"     运维概览：实例 {len(ow.ov_grid.raw)} 项 / 慢SQL {len(ow.slow_grid.raw)} 行 / "
                  f"表空间 {len(ow.ts_grid.raw)} 行 / 权限 {len(ow.perm_grid.raw)} 项")
            ow.show_seg_var.set(True)
            ow._toggle_segments()
            ow.nb.select(1)
            ow.nb.select(2)
            ow.nb.select(3)

            # ---- 运维 SQL 模板库（内置在概览的第 4 个页签）----
            from oracle_client.core import templates as TP

            assert len(TP.BUILTIN) >= 20, "内置模板数量异常"
            picker = ow.tpl_picker
            assert picker.tree.get_children(), "模板列表为空"
            assert picker.current is not None, "未自动选中模板"
            # 每条模板都要能用默认参数渲染出 SQL（写错参数名会在这里炸）
            rendered = 0
            for tpl in TP.BUILTIN:
                values = {}
                for p in tpl.params:
                    values[p.name] = "X" if p.type == "string" else (
                        p.values[0] if p.type == "enum" else p.default)
                sql = tpl.render(values)
                assert "{{" not in sql, f"{tpl.id} 渲染后仍残留占位符"
                rendered += 1
            # 切换模板后参数表单要跟着重建
            picker.tree.selection_set("blocking_tree")
            picker.tree.event_generate("<<TreeviewSelect>>")
            picker.update_idletasks()
            assert picker.current.id == "blocking_tree", "切换模板失败"
            assert "seconds" in picker.param_vars, "参数表单未重建"
            # 演示库没有 V$SESSION —— 权限预检必须把这条拦下来，而不是让它跑出 ORA-00942
            ok, msg, note = picker.current_sql()
            assert not ok and msg and "权限" in msg, f"缺少权限的模板没有被拦下：{msg}"
            assert picker.perm_label.cget("text") == "当前账号跑不了", "权限徽标未标红"
            # 只依赖 basic 的模板必须能跑
            picker.tree.selection_set("whoami")
            picker.tree.event_generate("<<TreeviewSelect>>")
            picker.update_idletasks()
            ok, sql, note = picker.current_sql()
            assert ok and "SYS_CONTEXT" in sql, f"基础模板渲染异常：{sql[:80]}"
            # 搜索能过滤
            picker.search_var.set("阻塞")
            picker.refresh_list()
            assert picker.tree.get_children(), "搜索后列表为空"
            picker.search_var.set("")
            picker.refresh_list()
            ow.nb.select(ow.tpl_page)
            print(f"     模板库：{rendered} 条内置模板全部渲染通过，参数表单/搜索/切换正常")
            ow.destroy()

            # ---- 模板库独立对话框（查询窗口工具栏 / 工具菜单入口）----
            from oracle_client.ui.templates import TemplateDialog

            picked = []
            tdlg = TemplateDialog(app.root, app, app.current_session_name,
                                   on_insert=lambda sql: picked.append(sql))
            tdlg.update_idletasks()
            assert tdlg.picker.tree.get_children(), "模板对话框列表为空"
            tdlg.picker.tree.selection_set("whoami")
            tdlg.picker.tree.event_generate("<<TreeviewSelect>>")
            tdlg.picker.update_idletasks()
            tdlg.picker.insert_current()
            assert picked and "SYS_CONTEXT" in picked[0], "插入到编辑器没有得到 SQL"
            tdlg.destroy()

            # ---- 数据库档案（离线求助用）----
            from oracle_client.ui.snapshot import SnapshotWindow, show_error_with_context

            sw = SnapshotWindow(app, app.current_session_name)
            sw.schema_var.set("DEMO")
            sw.generate()
            assert sw.text.strip(), "档案窗口没有生成内容"
            assert "数据库档案" in sw.text and "DEMO.ORDERS" in sw.text, sw.text[:300]
            print(f"     档案窗口：{len(sw.text)} 字符")
            bad = dialog_content_overflow(sw)
            assert not bad, f"档案窗口内容装不下：{bad}"
            sw.destroy()

            ed = show_error_with_context(app.root, app, "ORA-00942: 表或视图不存在", "查询失败",
                                         sql="SELECT * FROM ORDERS", schema="DEMO", table="ORDERS")
            ed.destroy()
            print("     报错框：问题上下文入口可用")

            # ---- 只读模式拦截（不弹窗，避免卡住冒烟流程）----
            from oracle_client.ui import widgets as WT
            from oracle_client.ui.builders import confirm_and_execute_dml

            warned = []
            orig_warning, orig_error = WT.warning, WT.error
            WT.warning = lambda parent, message, title="注意": warned.append(str(message))
            WT.error = lambda parent, message, title="出错": warned.append(str(message))
            try:
                app.settings.set("read_only", True)
                assert app.read_only_active is True
                ok, msg = confirm_and_execute_dml(app, app.root, sess,
                                                  "DELETE FROM DEMO.EMPLOYEES WHERE EMP_ID = 1",
                                                  "DEMO", "EMPLOYEES", "DELETE")
                assert ok is False and "只读" in msg, (ok, msg)
                assert any("只读" in w for w in warned), warned
                print("     只读模式：写操作已被拦截")
                warned.clear()
                app.settings.set("read_only", False)
                app.current_session.profile.env = "生产"
                assert app.read_only_active is True, "生产环境应自动只读"
                print("     生产环境：自动进入只读")
            finally:
                WT.warning, WT.error = orig_warning, orig_error
                app.settings.set("read_only", False)
                app.current_session.profile.env = ""
            assert app.read_only_active is False

            # ---- 回归防线：这两个对话框曾经“点保存必崩”（取了没注册的表单字段）----
            cd = ConnectionDialog(app.root, app, None)
            cd.form.fields["name"].set("回归测试连接")
            cd.form.fields["env"].set("生产")
            p = cd._collect()
            assert p.env == "生产" and p.is_prod, f"连接对话框 env 字段异常：{p.env!r}"
            cd.destroy()
            print("     连接对话框：环境标识可保存（不再 KeyError）")

            sd = SettingsDialog(app.root, app)
            sd.form.fields["read_only"].set(True)
            sd.form.fields["env_read_only"].set(True)
            sd._save()
            assert app.settings.get("read_only") is True, "设置没有保存只读开关"
            assert app.settings.get("env_read_only") is True
            print("     设置对话框：只读相关开关可保存（不再 KeyError）")
            app.settings.set("read_only", False)

            # ---- 工具栏只读开关必须真实存在（否则 read_only_var 会 AttributeError）----
            assert hasattr(app, "read_only_var"), "工具栏缺少只读开关"
            app.read_only_var.set(True)
            app._on_read_only()
            assert app.read_only_active is True
            app.read_only_var.set(False)
            app._on_read_only()
            print("     工具栏只读开关可用")

            # ---- 排序：一次点击必须得到升序（曾因 heading + Button-1 双触发而永远是降序）----
            g = t.data_grid
            col = g.columns[0]
            g._sort_by_name(col)
            assert g._sort.get(col) == "asc", g._sort
            g._sort_by_name(col)
            assert g._sort.get(col) == "desc", g._sort

            class _Ev:
                x, y = 4, 4

            g._on_click(_Ev())
            assert g._sort == {}, "点行号列应恢复原始顺序"
            print("     结果表格排序：升序 → 降序 → 复位 正常")

            # ---- run_async：改成事件循环等待后行为必须与原来一致 ----
            ok_a, val_a = W2.run_async(app.root, lambda ev: 42, cancellable=False)
            assert (ok_a, val_a) == (True, 42), (ok_a, val_a)
            ok_b, val_b = W2.run_async(app.root, lambda ev: (_ for _ in ()).throw(RuntimeError("boom")),
                                       cancellable=False)
            assert ok_b is False and "boom" in str(val_b), (ok_b, val_b)
            ok_c, val_c = W2.run_async(app.root, lambda ev: time.sleep(2), cancellable=False, timeout=0.5)
            assert ok_c is False and "未完成" in str(val_c), (ok_c, val_c)
            print("     异步执行框：正常 / 异常 / 超时 三种路径均正常")

            # ---- 切换主窗口连接不应把子窗口的连接一起切走 ----
            before = {w: w.session_name for w in app.windows}
            app._sync_conn_combo()
            assert all(w.session_name == before[w] for w in app.windows if w.winfo_exists()), \
                "子窗口连接被主窗口切换动作改掉了"
            print("     子窗口连接独立性：未被主窗口切换影响")

            W2.show_text(app.root, "测试文本窗口", "hello\n世界")
            app.show_help()
            app.switch_theme("darkly")
            app.switch_theme("flatly")
            app.toggle_schema_panel()
            app.toggle_schema_panel()
            for w in list(app.windows):
                w.apply_theme()
            print("     主题切换 / 面板开关 / 帮助窗口 正常")

            app.cascade_windows()
            app.tile_windows()
            print(f"     子窗口数：{len(app.windows)}")
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())
        finally:
            app.root.after(200, app.root.destroy)

    app.root.after(400, actions)
    app.root.after(90000, app.root.destroy)
    app.run()
    if errors:
        for e in errors:
            print("  ✘ 界面异常：")
            print("".join(str(x) for x in e) if isinstance(e, tuple) else str(e))
        FAILED.append(("界面冒烟", errors[0]))
    else:
        print("  ✔ 界面冒烟测试通过")


if __name__ == "__main__":
    print("=" * 60)
    print("Oracle 轻量客户端 —— 自检")
    print("=" * 60)
    core_tests()
    if "--no-gui" not in sys.argv:
        gui_tests()
    print("\n" + "=" * 60)
    if FAILED:
        print(f"存在 {len(FAILED)} 项失败：")
        for n, e in FAILED:
            print(f"  · {n}: {e}")
        sys.exit(1)
    print("全部通过 ✔")
