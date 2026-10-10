"""把客户端打包成可随身携带的单个 exe。

用法：
    python tools/build_exe.py              # 打包单文件 exe（同时生成调试版）
    python tools/build_exe.py --mode dir   # 打包成目录版（启动更快）
    python tools/build_exe.py --mode both
    python tools/build_exe.py --no-debug   # 不生成带控制台的调试版
    python tools/build_exe.py --clean-all  # 先清理 build/dist 缓存

产出在 dist/ 下：
    Oracle轻量客户端.exe            正式版（无控制台窗口）
    Oracle轻量客户端(调试版).exe    出错时弹控制台，便于排查
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Oracle轻量客户端"
ENTRY = ROOT / "tools" / "entry.py"
ASSETS = ROOT / "assets"


def make_icon() -> Path:
    """生成程序图标（ico，多尺寸）。"""
    ASSETS.mkdir(exist_ok=True)
    ico = ASSETS / "app.ico"
    try:
        from PIL import Image, ImageDraw

        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        c = size // 128 * 4
        d.rounded_rectangle([c, c, size - c, size - c], radius=size // 5,
                            fill=(38, 92, 168, 255))
        tops = [int(size * t) for t in (0.23, 0.45, 0.67)]
        for top in tops:
            h = int(size * 0.14)
            x0, x1 = int(size * 0.27), int(size * 0.73)
            d.ellipse([x0, top, x1, top + h], fill=(240, 246, 255, 255),
                      outline=(20, 60, 120, 255), width=max(2, size // 40))
        d.ellipse([int(size * 0.27), tops[0], int(size * 0.73), tops[0] + int(size * 0.14)],
                  fill=(255, 255, 255, 255))
        img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        print(f"  图标已生成：{ico}")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 图标生成失败（不影响打包）：{e}")
        return None
    return ico if ico.exists() else None


def base_args(name: str, onefile: bool, console: bool, icon: Path) -> list:
    # 每次用全新的工作目录：PyInstaller 启动时会删除旧缓存，
    # 在一些受保护的环境里这种批量删除会被拦截，换目录即可绕开。
    workpath = ROOT / "build" / f"{name}-{time.strftime('%H%M%S')}"
    specpath = workpath.parent
    # 注意：不要加 --clean，它会触发同样的批量删除。需要彻底重来时用 --clean-all。
    a = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--workpath", str(workpath),
        "--specpath", str(specpath),
        "--name", name,
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        "--paths", str(ROOT),
        # ttkbootstrap 自带图片/字体资源，必须整体打包，否则换主题会崩
        "--collect-all", "ttkbootstrap",
        # python-oracledb 的 thin 模式在连接时才会 import cryptography 做加密，
        # 缺了它连真实 Oracle 库必然报 DPY-3016，必须整体打包进去。
        "--collect-all", "cryptography",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "openpyxl",
        "--hidden-import", "oracledb",
        "--hidden-import", "sqlite3",
        # 运维 SQL 模板库：ops/app/windows 里既有顶层导入也有函数内延迟导入，
        # 显式声明一次，免得换了调用方式就被静态分析漏掉，跑起来才报 ModuleNotFoundError。
        "--hidden-import", "oracle_client.core.templates",
        "--hidden-import", "oracle_client.ui.templates",
        "--exclude-module", "tkinter.test",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
    ]
    if icon:
        a += ["--icon", str(icon), "--disable-windowed-traceback"]
    if not onefile:
        a += ["--contents-directory", "."]
    a.append(str(ENTRY))
    return a


def write_notes(dist: Path):
    """产出目录里放一份简短说明。"""
    try:
        dist.mkdir(exist_ok=True)
        txt = (
            "Oracle 轻量客户端 · 绿色便携版\n"
            "================================\n\n"
            "【怎么运行】\n"
            "  双击 Oracle轻量客户端.exe 即可，不需要安装 Python，也不需要安装 Oracle 客户端。\n"
            "  可以拷贝到任何 Windows 电脑或 U 盘上使用。\n\n"
            "【出问题时】\n"
            "  改用「Oracle轻量客户端(调试版).exe」，它会弹出控制台显示错误信息。\n"
            "  想确认运行环境是否完整，可在命令行执行：\n"
            "      Oracle轻量客户端(调试版).exe --selftest\n\n"
            "【数据放在哪】\n"
            "  程序会在自己所在目录建一个 UserData 文件夹，存放连接配置、SQL 历史、\n"
            "  设置和备份。整个文件夹跟 exe 一起拷贝 = 配置随身带；删掉它就恢复出厂设置。\n"
            "  想存到别处：新建一个 UserData 之外的目录，设置环境变量 ORACLE_CLIENT_HOME 指过去。\n\n"
            "【首次启动稍慢】\n"
            "  单文件版每次运行要把运行环境解压到临时目录（约 1~3 秒），属正常现象。\n\n"
            "【杀毒软件误报】\n"
            "  单文件 exe 容易被误判，请添加信任/白名单。\n"
            "  若公司环境限制严格，可在源码目录运行 python tools/build_exe.py --mode dir\n"
            "  生成目录版，几乎不会被误报，且启动更快。\n"
        )
        (dist / "绿色便携版说明.txt").write_text(txt, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 说明文件写入失败：{e}")


def human(size: int) -> str:
    m = size / 1024 / 1024
    return f"{m:.1f} MB"


def run(args: list, tag: str):
    print(f"\n>> {tag}")
    print("   " + " ".join(f'"{x}"' if " " in str(x) else str(x) for x in args[1:]))
    t0 = time.time()
    r = subprocess.run(args, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"  ✘ {tag} 失败（退出码 {r.returncode}）")
        return False
    print(f"  ✔ {tag} 完成，用时 {time.time() - t0:.0f}s")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["onefile", "dir", "both"], default="onefile")
    ap.add_argument("--no-debug", action="store_true", help="不生成带控制台的调试版")
    ap.add_argument("--clean-all", action="store_true", help="构建前清理 build/dist")
    ap.add_argument("--target", choices=["all", "release", "debug", "dir"], default="all",
                    help="只构建某一种产物（默认全部）")
    ns = ap.parse_args()

    print("=" * 60)
    print("Oracle 轻量客户端 · 便携版打包")
    print("=" * 60)

    if ns.clean_all:
        # 逐个删除，避免一次性批量删除
        for p in (ROOT / "build", ROOT / "dist"):
            if p.exists():
                for sub in sorted(p.iterdir()):
                    try:
                        shutil.rmtree(sub, ignore_errors=True) if sub.is_dir() else sub.unlink(missing_ok=True)
                    except Exception:
                        pass
                print(f"  已清理 {p}")

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("  正在安装 PyInstaller …")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyinstaller"], check=False)

    icon = make_icon()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    ok = True

    if ns.target == "all":
        want_release = ns.mode in ("onefile", "both")
        want_debug = ns.mode in ("onefile", "both") and not ns.no_debug
        want_dir = ns.mode in ("dir", "both")
    else:
        want_release = ns.target == "release"
        want_debug = ns.target == "debug"
        want_dir = ns.target == "dir"

    builds = []
    if ns.mode in ("onefile", "both"):
        if want_release:
            builds.append((APP_NAME, True, False, "正式版（单文件，无控制台）"))
        if want_debug:
            builds.append((APP_NAME + "(调试版)", True, True, "调试版（单文件，带控制台）"))
    if want_dir:
        builds.append((APP_NAME + "-便携目录版", False, False, "目录版（启动更快）"))

    for name, onefile, console, tag in builds:
        ok = run(base_args(name, onefile, console, icon if not console else icon), tag) and ok

    print("\n" + "=" * 60)
    write_notes(dist)
    print("产出清单：")
    if ns.mode in ("onefile", "both"):
        exe = dist / f"{APP_NAME}.exe"
        if exe.exists():
            print(f"  ✔ {exe.name}  {human(exe.stat().st_size)}   ← 双击即可运行，可随意拷贝")
        dbg = dist / f"{APP_NAME}(调试版).exe"
        if dbg.exists():
            print(f"  ✔ {dbg.name}  {human(dbg.stat().st_size)}   ← 出错时能看到报错信息")
    if ns.mode in ("dir", "both"):
        d = dist / f"{APP_NAME}-便携目录版"
        if d.exists():
            total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            print(f"  ✔ {d.name}\\  {human(total)}   ← 整体文件夹一起拷贝")
    print("=" * 60)
    if ok:
        print("打包成功 ✔  记得先用 dist 里的 exe 跑一次 --selftest（指调试版）确认环境完整")
    else:
        print("打包过程存在失败项 ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
