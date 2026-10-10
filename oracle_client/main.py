"""程序入口。"""
from __future__ import annotations

import os
import sys
import traceback
from typing import List, Tuple


def _show_startup_error(title: str, detail: str):
    """依赖缺失等问题用弹窗提示，避免双击 bat 后一闪而过。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, detail)
        root.destroy()
    except Exception:
        print(detail, file=sys.stderr)


def _self_test() -> int:
    """打包后的 exe 自检：逐个加载关键依赖与核心逻辑，确认打包没丢东西。"""
    checks: List[Tuple[bool, str, str]] = []

    def check(name: str, fn):
        try:
            msg = fn()
            checks.append((True, name, msg if isinstance(msg, str) else ""))
        except Exception as e:  # noqa: BLE001
            checks.append((False, name, f"{type(e).__name__}: {e}"))

    def _tk():
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()

    def _bootstrap():
        import tkinter

        import ttkbootstrap as tb

        root = tkinter.Tk()
        root.withdraw()
        try:
            # 触发 ttbootstrap 资源与主题加载（最容易在打包后缺失的一步）
            style = tb.Style("flatly")
            tb.Label(None, text="x")
        finally:
            root.destroy()

    def _oracledb():
        import oracledb

        _ = oracledb.__version__

    def _crypto():
        """thin 模式连真实 Oracle 必需；缺了会报 DPY-3016。"""
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401

        from cryptography.hazmat.primitives import hashes  # noqa: F401

    def _excel():
        import tempfile
        from pathlib import Path

        from .core.exporter import export_excel

        out = Path(tempfile.mkdtemp()) / "t.xlsx"
        export_excel(str(out), ["A", "B"], [[1, "中文"]], sheet_name="S")
        assert out.exists() and out.stat().st_size > 0

    def _demo_db():
        from .core.db import DbSession
        from .core.models import ConnProfile

        p = ConnProfile()
        p.demo = True
        s = DbSession(p)
        rows = s.query("SELECT COUNT(*) FROM DEMO.EMPLOYEES", max_rows=10).rows
        assert rows and int(rows[0][0]) > 0
        s.close()

    def _templates():
        """模板库：内容（core）与界面（ui）缺一都会在打开时崩，两个都要验。"""
        from .core import templates as TP
        from .ui import templates as UIT

        n = len(TP.BUILTIN)
        assert n > 0, "内置模板为空"
        assert hasattr(UIT, "TemplatePicker"), "界面模块没打进包"
        # 渲染一遍：顺带确认模板里的 {{占位}} 与参数声明没有写错。
        # 必填参数没有默认值（比如 SQL_ID），要像用户那样先填个占位值再渲染。
        for t in TP.BUILTIN:
            values = {}
            for p in t.params:
                v = p.default
                if v in (None, ""):
                    if p.type == "enum" and p.values:
                        v = p.values[0]
                    elif p.type in ("int", "float"):
                        v = p.min if p.min is not None else 1
                    else:
                        v = "X"
                values[p.name] = v
            t.render(values)
        return f"{n} 条内置模板"

    check("tkinter", _tk)
    check("ttkbootstrap + 主题资源", _bootstrap)
    check("python-oracledb", _oracledb)
    check("cryptography（连真实库必需，缺则 DPY-3016）", _crypto)
    check("openpyxl 导出 xlsx", _excel)
    check("sqlite3（演示库）", lambda: __import__("sqlite3").connect(":memory:").close())
    check("演示数据库", _demo_db)
    check("运维 SQL 模板库", _templates)
    check("PIL", lambda: __import__("PIL.Image"))
    try:
        from .core.config import data_dir, is_portable

        checks.append((True, "数据目录", f"{data_dir()}{'（便携模式）' if is_portable() else ''}"))
    except Exception as e:  # noqa: BLE001
        checks.append((False, "数据目录", str(e)))

    failed = 0
    lines = ["=" * 56, "Oracle 轻量客户端 · 运行环境自检", "=" * 56]
    for ok, name, msg in checks:
        lines.append(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  {msg}" if msg else ""))
        failed += 0 if ok else 1
    lines.append("=" * 56)
    lines.append("全部通过 ✔" if not failed else f"存在 {failed} 项失败 ✘")
    report = "\n".join(lines)

    # 同时写一份到 exe 同目录，方便没有控制台时查看
    try:
        from .core.config import app_base_dir

        (app_base_dir() / "_自检结果.txt").write_text(report, encoding="utf-8")
    except Exception:
        pass

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            print(report)
        except UnicodeEncodeError:  # GBK 控制台等窄编码环境
            print(report.replace("✔", "OK").replace("✘", "FAIL").encode("gbk", "replace").decode("gbk"))
    else:  # 无控制台的 windowed exe：用弹窗展示
        _show_startup_error("运行环境自检" + ("" if failed else " · 全部通过"), report)
    return 0 if not failed else 1


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    if "--version" in argv:
        from . import __version__

        print(__version__)
        return 0
    if "--selftest" in argv:
        return _self_test()

    # 让 pythonw 下也有控制台输出（便于排查）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        from .ui.app import Application
    except Exception as e:  # noqa: BLE001
        missing = [m for m in ("ttkbootstrap", "oracledb", "openpyxl", "PIL") if m in str(e)]
        if missing or "No module named" in str(e):
            _show_startup_error(
                "缺少运行依赖",
                f"启动失败：{e}\n\n请先双击运行“安装依赖.bat”，安装完成后再启动程序。",
            )
        else:
            _show_startup_error("启动失败", f"{e}\n\n{traceback.format_exc()}")
        return 1

    try:
        app = Application()
        app.run()
        return 0
    except Exception as e:  # noqa: BLE001
        _show_startup_error("程序异常退出", f"{e}\n\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
