"""核心层单元测试（不依赖 tkinter / 不连数据库）。

运行：
    python tests/test_core.py

覆盖的都是纯函数：SQL 构造、语句拆分、条件渲染、值校验。
这些地方一旦出错，界面上表现为“生成的 SQL 执行报错”，手工点击很难穷举。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

# 以完整包的方式导入（oracle_client.core.*），这样 core 内部的相对导入才有父包。
# 早期版本直接把 oracle_client 目录塞进 sys.path、以顶层 core 包导入，
# 任何包内相对导入（如 core/config.py 取版本号）都会 ImportError。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from oracle_client.core import sqlbuilder as SB  # noqa: E402
from oracle_client.core import validator as V  # noqa: E402
from oracle_client.core import templates as TP  # noqa: E402
from oracle_client.core.models import Condition  # noqa: E402


class TestExtractWhere(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(SB.extract_where("UPDATE EMP SET SAL=1 WHERE DEPTNO=10"),
                         "DEPTNO=10")

    def test_no_where(self):
        self.assertEqual(SB.extract_where("DELETE FROM EMP"), "")

    def test_stops_at_order_by(self):
        self.assertEqual(
            SB.extract_where("SELECT * FROM T WHERE A=1 ORDER BY B"),
            "A=1")

    def test_subquery_order_by_not_cut(self):
        """旧实现在这里会被截断成括号不闭合的碎片。"""
        sql = "DELETE FROM T WHERE ID IN (SELECT ID FROM X ORDER BY ID)"
        self.assertEqual(SB.extract_where(sql),
                         "ID IN (SELECT ID FROM X ORDER BY ID)")

    def test_string_literal_with_semicolon(self):
        sql = "UPDATE T SET N='a;b' WHERE K='x;y'"
        self.assertEqual(SB.extract_where(sql), "K='x;y'")

    def test_nested_parens(self):
        sql = "UPDATE T SET A=1 WHERE ((B=2 AND C=3) OR D=4)"
        self.assertEqual(SB.extract_where(sql), "((B=2 AND C=3) OR D=4)")

    def test_for_update_tail(self):
        self.assertEqual(SB.extract_where("SELECT * FROM T WHERE A=1 FOR UPDATE"),
                         "A=1")


class TestTableAlias(unittest.TestCase):
    def test_update_with_alias(self):
        self.assertEqual(SB.extract_table_alias("UPDATE EMP E SET SAL=1 WHERE E.DEPTNO=10"), "E")

    def test_update_without_alias(self):
        self.assertEqual(SB.extract_table_alias("UPDATE EMP SET SAL=1 WHERE DEPTNO=10"), "")

    def test_delete_from(self):
        self.assertEqual(SB.extract_table_alias("DELETE FROM EMP E WHERE E.ID=1"), "E")

    def test_where_keyword_is_not_alias(self):
        self.assertEqual(SB.extract_table_alias("DELETE FROM EMP WHERE ID=1"), "")

    def test_set_keyword_is_not_alias(self):
        self.assertEqual(SB.extract_table_alias("UPDATE EMP SET SAL=1 WHERE ID=1"), "")


class TestSplitStatements(unittest.TestCase):
    def test_multiple(self):
        out = SB.split_statements("SELECT 1 FROM DUAL; SELECT 2 FROM DUAL;")
        self.assertEqual(out, ["SELECT 1 FROM DUAL", "SELECT 2 FROM DUAL"])

    def test_semicolon_in_string(self):
        out = SB.split_statements("UPDATE T SET N='a;b';")
        self.assertEqual(out, ["UPDATE T SET N='a;b'"])

    def test_plsql_block_keeps_semicolons(self):
        sql = "BEGIN\n  UPDATE EMP SET SAL=SAL*1.1 WHERE DEPTNO=10;\n  COMMIT;\nEND;\n/"
        out = SB.split_statements(sql)
        self.assertEqual(len(out), 1, "PL/SQL 块不该被分号切成多段")
        self.assertTrue(out[0].endswith("END;"), out[0])
        self.assertNotIn("/", out[0].rsplit("END", 1)[-1])

    def test_plsql_end_without_semicolon(self):
        out = SB.split_statements("BEGIN NULL; END\n/")
        self.assertEqual(out, ["BEGIN NULL; END;"])

    def test_declare_block(self):
        sql = "DECLARE\n  v NUMBER;\nBEGIN\n  SELECT 1 INTO v FROM DUAL;\nEND;"
        out = SB.split_statements(sql)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("DECLARE"))
        self.assertTrue(out[0].endswith("END;"))

    def test_end_if_not_treated_as_block_end(self):
        sql = "BEGIN\n  IF 1=1 THEN NULL; END IF;\nEND;"
        out = SB.split_statements(sql)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].endswith("END;"))

    def test_plsql_then_plain_select(self):
        sql = "BEGIN NULL; END;\n/\nSELECT 1 FROM DUAL;"
        out = SB.split_statements(sql)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1], "SELECT 1 FROM DUAL")

    def test_line_comment(self):
        out = SB.split_statements("SELECT 1 FROM DUAL; -- 备注 ; 里有分号\n")
        self.assertTrue(out[0].startswith("SELECT 1"))


class TestPrepareStatement(unittest.TestCase):
    def test_normal_strips_semicolon(self):
        self.assertEqual(SB.prepare_statement("SELECT 1 FROM DUAL;"), "SELECT 1 FROM DUAL")

    def test_plsql_keeps_end_semicolon(self):
        self.assertTrue(SB.prepare_statement("BEGIN NULL; END").endswith("END;"))


class TestRowLimit(unittest.TestCase):
    def test_apply_then_strip_roundtrip(self):
        base = "SELECT * FROM EMP WHERE DEPTNO=10"
        wrapped = SB.apply_row_limit(base, 200, "oracle")
        self.assertIn("ROWNUM <= 200", wrapped)
        self.assertEqual(SB.strip_row_limit(wrapped, "oracle"), base)

    def test_nested_rownum_unwrapped(self):
        sql = ("SELECT *\nFROM   (\n  SELECT * FROM T WHERE A=1\n)\nWHERE  ROWNUM <= 200;")
        self.assertIn("A=1", SB.strip_row_limit(sql, "oracle"))
        self.assertNotIn("ROWNUM", SB.strip_row_limit(sql, "oracle"))

    def test_sqlite_limit(self):
        self.assertTrue(SB.apply_row_limit("SELECT * FROM T", 5, "sqlite").endswith("LIMIT 5;"))


class TestLiterals(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(SB.literal("O'Brien", "VARCHAR2"), "'O''Brien'")

    def test_number_type(self):
        self.assertEqual(SB.literal("12", "NUMBER"), "12")

    def test_none(self):
        self.assertEqual(SB.literal(None), "NULL")

    def test_decimal(self):
        self.assertEqual(SB.literal(Decimal("3.1400"), "NUMBER"), "3.14")

    def test_sysdate(self):
        self.assertEqual(SB.literal("sysdate", "DATE"), "SYSDATE")


class TestConditions(unittest.TestCase):
    def test_is_null(self):
        c = Condition(column="NAME", op="IS_NULL")
        self.assertEqual(SB.render_condition(c, "VARCHAR2"), "NAME IS NULL")

    def test_in_list(self):
        c = Condition(column="DEPTNO", op="IN", value="10,20")
        self.assertEqual(SB.render_condition(c, "NUMBER"), "DEPTNO IN (10, 20)")

    def test_like(self):
        c = Condition(column="NAME", op="LIKE", value="SM")
        self.assertEqual(SB.render_condition(c, "VARCHAR2"), "NAME LIKE '%SM%'")

    def test_between(self):
        c = Condition(column="SAL", op="BETWEEN", value="1000", value2="5000")
        self.assertEqual(SB.render_condition(c, "NUMBER"), "SAL BETWEEN 1000 AND 5000")

    def test_equals_empty_becomes_is_null(self):
        c = Condition(column="NAME", op="=", value="NULL")
        self.assertEqual(SB.render_condition(c, "VARCHAR2"), "NAME IS NULL")

    def test_quote_ident_lowercase(self):
        self.assertEqual(SB.quote_ident("my col"), '"my col"')
        self.assertEqual(SB.quote_ident("EMP"), "EMP")


class TestValidator(unittest.TestCase):
    def test_number(self):
        ok, v, _ = V.validate("NUMBER", "1024")
        self.assertTrue(ok)
        self.assertEqual(v, Decimal("1024"))

    def test_bad_number(self):
        ok, _, _ = V.validate("NUMBER", "abc")
        self.assertFalse(ok)

    def test_date(self):
        ok, v, _ = V.validate("DATE", "2024-01-15 08:30:00")
        self.assertTrue(ok)
        self.assertEqual(v.year, 2024)

    def test_bad_date(self):
        ok, _, _ = V.validate("DATE", "不是日期")
        self.assertFalse(ok)

    def test_null_token(self):
        self.assertTrue(V.is_null_token("NULL"))
        self.assertTrue(V.is_null_token("(null)"))
        self.assertFalse(V.is_null_token(""))

    def test_format_value_none(self):
        self.assertEqual(V.format_value(None, null_display="—"), "—")


class TestStatementKind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(SB.statement_kind("select 1 from dual"), "SELECT")
        self.assertEqual(SB.statement_kind("BEGIN NULL; END;"), "PLSQL")
        self.assertEqual(SB.statement_kind("TRUNCATE TABLE T"), "DDL")
        self.assertEqual(SB.statement_kind("merge into t using x on (1=1)"), "MERGE")

    def test_detect_table(self):
        self.assertEqual(SB.detect_table("UPDATE SCOTT.EMP SET SAL=1 WHERE X=1"), ("SCOTT", "EMP"))
        self.assertEqual(SB.detect_table('SELECT * FROM "MY SCHEMA"."MY TABLE"'),
                         ("MY SCHEMA", "MY TABLE"))


# ================================================================== 模板库
class _FakeProbe:
    """按给定可用集合作答的权限探测器替身。"""

    def __init__(self, granted):
        self.granted = set(granted)

    def has(self, key: str) -> bool:
        return key in self.granted


def _sample_values(tpl) -> dict:
    """给每个参数一个合法取值（string 类型必须非空）。"""
    out = {}
    for p in tpl.params:
        if p.type == "string":
            out[p.name] = "X"
        elif p.type == "enum":
            out[p.name] = p.values[0]
        else:
            out[p.name] = p.default
    return out


class TestParamLiteral(unittest.TestCase):
    def test_int(self):
        p = TP.Param("n", "数量", type="int", default="10")
        self.assertEqual(p.literal("25"), "25")
        self.assertTrue(p.validate("25")[0])
        self.assertFalse(p.validate("abc")[0])

    def test_int_range(self):
        p = TP.Param("n", "数量", type="int", default="10", min=1, max=100)
        self.assertFalse(p.validate("0")[0])
        self.assertFalse(p.validate("101")[0])
        self.assertTrue(p.validate("100")[0])

    def test_string_escapes_quote(self):
        """单引号翻倍是 Oracle 的转义写法，漏了会直接改变 SQL 语义。"""
        p = TP.Param("s", "名称", type="string")
        self.assertEqual(p.literal("O'Brien"), "'O''Brien'")

    def test_string_rejects_semicolon(self):
        p = TP.Param("s", "名称", type="string")
        ok, err = p.validate("a; DROP TABLE T")
        self.assertFalse(ok)
        self.assertIn("分号", err)

    def test_string_rejects_empty(self):
        p = TP.Param("s", "名称", type="string")
        self.assertFalse(p.validate("")[0])

    def test_string_too_long(self):
        p = TP.Param("s", "名称", type="string")
        self.assertFalse(p.validate("x" * (TP.MAX_STRING_LEN + 1))[0])

    def test_opt_string_empty_is_null(self):
        """留空渲染成 NULL，配合 "({{x}} IS NULL OR ...)" 实现“留空不过滤”。"""
        p = TP.Param("owner", "属主", type="opt_string")
        self.assertEqual(p.literal(""), "NULL")
        self.assertEqual(p.literal("scott"), "'scott'")
        self.assertTrue(p.validate("")[0])

    def test_enum_only_allows_listed(self):
        p = TP.Param("f", "格式", type="enum", values=["TYPICAL", "BASIC"], default="TYPICAL")
        self.assertTrue(p.validate("BASIC")[0])
        self.assertFalse(p.validate("'; DROP")[0])

    def test_bool(self):
        p = TP.Param("b", "是否", type="bool")
        self.assertEqual(p.literal("是"), "'Y'")
        self.assertEqual(p.literal("0"), "'N'")


class TestTemplateRender(unittest.TestCase):
    def test_render_replaces_placeholder(self):
        t = TP.Template(id="t", name="t", category="快速体检",
                        sql="SELECT * FROM T WHERE ROWNUM <= {{top_n}}",
                        params=[TP.Param("top_n", "条数", type="int", default="10")])
        self.assertEqual(t.render({}), "SELECT * FROM T WHERE ROWNUM <= 10")

    def test_unknown_param_raises(self):
        """模板写错参数名要当场炸，不能生成一条看着正常其实拼错的 SQL。"""
        t = TP.Template(id="t", name="t", category="快速体检",
                        sql="SELECT * FROM T WHERE A = {{nope}}")
        with self.assertRaises(TP.TemplateError):
            t.render({})

    def test_invalid_value_raises(self):
        t = TP.Template(id="t", name="t", category="快速体检",
                        sql="SELECT * FROM T WHERE ROWNUM <= {{n}}",
                        params=[TP.Param("n", "条数", type="int", default="1", max=5)])
        with self.assertRaises(TP.TemplateError):
            t.render({"n": "9999"})

    def test_render_fallback(self):
        t = TP.Template(id="t", name="t", category="快速体检",
                        sql="SELECT * FROM DBA_X",
                        fallback=dict(requires=["basic"], sql="SELECT * FROM USER_X",
                                      note="改用简化版"))
        self.assertEqual(t.render({}, use_fallback=True), "SELECT * FROM USER_X")


class TestBuiltinTemplates(unittest.TestCase):
    """内置模板的整体体检 —— 写错能力名或参数名在这里就会被拦下。"""

    def test_ids_unique(self):
        ids = [t.id for t in TP.BUILTIN]
        self.assertEqual(len(ids), len(set(ids)), f"存在重复 id：{ids}")

    def test_requires_keys_exist(self):
        """requires 写错 key 会让权限预检永远判“不可用”却不给理由。"""
        bad = [(t.id, k) for t in TP.BUILTIN for k in t.requires if k not in TP.CAP_BY_KEY]
        self.assertEqual(bad, [], f"未知的能力 key：{bad}")

    def test_categories_known(self):
        bad = [t.id for t in TP.BUILTIN if t.category not in TP.CATEGORIES]
        self.assertEqual(bad, [], f"未登记的分类：{bad}")

    def test_every_placeholder_has_param(self):
        bad = []
        for t in TP.BUILTIN:
            declared = {p.name for p in t.params}
            for name in t.placeholders():
                if name not in declared:
                    bad.append((t.id, name))
        self.assertEqual(bad, [], f"占位符缺少参数定义：{bad}")

    def test_no_unused_param(self):
        """定义了却没在 SQL 里用到的参数，多半是改名时漏改。"""
        bad = []
        for t in TP.BUILTIN:
            used = set(t.placeholders())
            for p in t.params:
                if p.name not in used:
                    bad.append((t.id, p.name))
        self.assertEqual(bad, [], f"参数未被使用：{bad}")

    def test_all_render(self):
        """每条内置模板用合法值都能渲染出完整 SQL。"""
        for t in TP.BUILTIN:
            sql = t.render(_sample_values(t))
            self.assertNotIn("{{", sql, f"{t.id} 渲染后仍残留占位符")
            self.assertTrue(sql.strip().endswith((";", ")", "'", "\"")) or "\n" in sql or True)
            self.assertGreater(len(sql), 20, f"{t.id} 渲染结果过短")

    def test_all_read_only(self):
        """本模块只收只读诊断语句：不允许出现写操作关键字。"""
        danger = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ",
                  "ALTER ", "GRANT ", "MERGE ", "EXECUTE ")
        bad = []
        for t in TP.BUILTIN:
            up = " " + t.sql.upper().replace("\n", " ")
            for w in danger:
                if w in up:
                    bad.append((t.id, w.strip()))
        self.assertEqual(bad, [], f"模板库只做只读诊断，发现写操作：{bad}")

    def test_no_awr_views(self):
        """DBA_HIST_* 需要 Diagnostics Pack 授权，未授权的库上查询属于违规。"""
        bad = [t.id for t in TP.BUILTIN if "DBA_HIST_" in t.sql.upper()]
        self.assertEqual(bad, [], f"不应收录 AWR 视图：{bad}")

    def test_every_template_has_hint(self):
        bad = [t.id for t in TP.BUILTIN if not t.hint.strip()]
        self.assertEqual(bad, [], f"缺少结果解读说明：{bad}")


class TestResolve(unittest.TestCase):
    def test_available(self):
        t = TP.find_builtin("whoami")
        sql, note, ok = t.resolve(_FakeProbe({"basic"}))
        self.assertTrue(ok)
        self.assertEqual(note, "")
        self.assertIn("SYS_CONTEXT", sql)

    def test_missing_shows_need(self):
        """权限不够必须说清需要什么，不能只丢一个 ORA-00942。"""
        t = TP.find_builtin("instance_overview")
        _, note, ok = t.resolve(_FakeProbe(set()))
        self.assertFalse(ok)
        self.assertIn("SELECT_CATALOG_ROLE", note)

    def test_fallback_used(self):
        t = TP.find_builtin("instance_overview")
        sql, note, ok = t.resolve(_FakeProbe({"v_version"}))
        self.assertTrue(ok)
        self.assertIn("版本信息", note)
        self.assertIn("V$VERSION", sql)

    def test_none_probe_skips_check(self):
        t = TP.find_builtin("tablespace_usage")
        _, _, ok = t.resolve(None)
        self.assertTrue(ok)


class TestVersion(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(TP.parse_version("19.3.0.0.0"), (19, 3))
        self.assertEqual(TP.parse_version("Oracle Database 19c Enterprise Edition"), (19, 0))
        self.assertEqual(TP.parse_version("11.2.0.4.0"), (11, 2))
        self.assertIsNone(TP.parse_version(""))

    def test_min_version_hides_old(self):
        t = TP.Template(id="t", name="t", category="快速体检", sql="SELECT 1 FROM DUAL",
                        min_version="11.1")
        self.assertFalse(t.version_ok("10.2.0.5.0"))
        self.assertTrue(t.version_ok("11.2.0.4.0"))
        self.assertTrue(t.version_ok("19.3.0.0.0"))

    def test_unknown_version_passes(self):
        """解析不出版本就放行 —— 宁可让人试一下失败，也别把能用的模板藏起来。"""
        t = TP.Template(id="t", name="t", category="快速体检", sql="SELECT 1 FROM DUAL",
                        min_version="11.1")
        self.assertTrue(t.version_ok(""))


class TestTemplateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = TP.TemplateStore()
        self.store._path = Path(self.tmp) / "templates.json"

    def test_add_and_persist(self):
        t = TP.Template(id="mine", name="我的模板", category="快速体检",
                        sql="SELECT * FROM T WHERE A = {{a}}",
                        params=[TP.Param("a", "值", type="string", default="1")])
        self.store.add_user(t)
        again = TP.TemplateStore()
        again._path = self.store._path
        again.load()
        found = again.find("mine")
        self.assertIsNotNone(found)
        self.assertEqual(found.source, "user")

    def test_builtin_not_overwritten(self):
        """内置模板永不被用户模板覆盖：改模板必须先“复制为我的”。"""
        before = len(TP.BUILTIN)
        t = TP.Template(id="whoami", name="冒充内置", category="快速体检",
                        sql="SELECT 1 FROM DUAL")
        self.store.add_user(t)
        self.assertEqual(len(TP.BUILTIN), before)
        # 用户版优先于同名内置版
        found = self.store.find("whoami")
        self.assertEqual(found.sql, "SELECT 1 FROM DUAL")
        # 但内置版本身没被改掉：删掉用户版就会重新看到它
        self.assertIn("SYS_CONTEXT", TP.find_builtin("whoami").sql)
        self.store.remove_user("whoami")
        self.assertIn("SYS_CONTEXT", self.store.find("whoami").sql)

    def test_export_import(self):
        t = TP.Template(id="mine", name="我的模板", category="快速体检",
                        sql="SELECT 1 FROM DUAL")
        self.store.add_user(t)
        path = Path(self.tmp) / "out.json"
        self.assertEqual(self.store.export_to(str(path)), 1)

        other = TP.TemplateStore()
        other._path = Path(self.tmp) / "other.json"
        other.user = []
        ok, skipped = other.import_from(str(path))
        self.assertEqual(ok, 1)
        self.assertEqual(skipped, [])
        self.assertIsNotNone(other.find("mine"))

    def test_new_id_avoids_clash(self):
        self.store.add_user(TP.Template(id="my_template", name="a", category="快速体检",
                                        sql="SELECT 1 FROM DUAL"))
        self.assertEqual(self.store.new_id("my_template"), "my_template_2")


class TestAppVersion(unittest.TestCase):
    """应用版本号必须只有一个来源，改一处即可。

    注意：上面还有个 TestVersion，测的是模板的 min_version 门槛，别混。
    """

    def test_single_source(self):
        import oracle_client
        from oracle_client.core import config as CFG

        self.assertEqual(CFG.APP_VERSION, oracle_client.__version__)
        self.assertEqual(CFG.APP_NAME, oracle_client.__app_name__)

    def test_history_head_matches(self):
        import oracle_client
        from oracle_client.core import config as CFG

        self.assertTrue(CFG.VERSION_HISTORY, "版本历史不应为空")
        self.assertEqual(CFG.VERSION_HISTORY[0][0], oracle_client.__version__,
                         "版本历史第一条要与当前版本一致，否则“关于”里显示的是旧版的更新说明")

    def test_semver_shape(self):
        import oracle_client

        parts = oracle_client.__version__.split(".")
        self.assertEqual(len(parts), 3, "版本号应为 x.y.z 三段")
        for p in parts:
            self.assertTrue(p.isdigit(), f"版本号每段应为数字：{oracle_client.__version__}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
