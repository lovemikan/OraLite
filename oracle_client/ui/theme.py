"""主题与字体管理（基于 ttkbootstrap）。"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk as tkttk

import ttkbootstrap as tb

LIGHT_THEMES = {
    "flatly": "浅色 · 简洁蓝",
    "cosmo": "浅色 · 现代蓝",
    "litera": "浅色 · 素雅",
    "minty": "浅色 · 清新绿",
    "sandstone": "浅色 · 沙石",
    "pulse": "浅色 · 紫调",
    "united": "浅色 · 橙调",
    "yeti": "浅色 · 冷青",
    "journal": "浅色 · 纸感",
    "simplex": "浅色 · 极简",
}

DARK_THEMES = {
    "darkly": "深色 · 经典",
    "superhero": "深色 · 蓝调",
    "solar": "深色 · 暖橙",
    "cyborg": "深色 · 墨绿",
    "vapor": "深色 · 霓虹",
}

DEFAULT_THEME = "flatly"


def all_themes() -> dict:
    out = {}
    out.update(LIGHT_THEMES)
    out.update(DARK_THEMES)
    return out


def is_dark(style) -> bool:
    try:
        return str(style.theme.type).lower() == "dark"
    except Exception:
        try:
            return str(style.theme.name).lower() in DARK_THEMES
        except Exception:
            return False


def apply_font(root, family: str = "Microsoft YaHei UI", size: int = 10):
    """统一字体，保证中文显示正常。"""
    for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                  "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(fname).configure(family=family, size=size)
        except Exception:
            pass
    try:
        style = root.style if hasattr(root, "style") else tkttk.Style()
        style.configure(".", font=(family, size))
        style.configure("Treeview", rowheight=int(size * 2.4))
        style.configure("Heading", font=(family, size, "bold"))
        style.configure("TButton", font=(family, size))
        style.configure("TLabel", font=(family, size))
        style.configure("TEntry", font=(family, size))
        style.configure("TCombobox", font=(family, size))
        style.configure("TCheckbutton", font=(family, size))
        style.configure("TNotebook.Tab", font=(family, size))
    except Exception:
        pass


def mono_font(family: str = "Consolas", size: int = 11):
    try:
        f = tkfont.Font(family=family, size=size)
        return f
    except Exception:
        return ("Consolas", size)


def row_colors(style) -> tuple:
    """返回 (偶数行背景, 奇数行背景)。"""
    dark = is_dark(style)
    if dark:
        return ("#2a2f3a", "#23272f")
    return ("#f6f8fa", "#ffffff")


def grid_colors(style) -> dict:
    even, odd = row_colors(style)
    dark = is_dark(style)
    if dark:
        return {"even": even, "odd": odd, "bg": "#23272f", "fg": "#e8eaf0", "heading_bg": "#2f3542",
                "heading_fg": "#ffffff", "border": "#3a3f4b", "selected": "#3a5f8a"}
    return {"even": even, "odd": odd, "bg": "#ffffff", "fg": "#22303f", "heading_bg": "#eef2f7",
            "heading_fg": "#1f2d3d", "border": "#d6dee8", "selected": "#cfe3fb"}


def editor_colors(style) -> dict:
    dark = is_dark(style)
    if dark:
        return {"bg": "#1e2229", "fg": "#e6e9ef", "insert": "#ffffff", "gutter_bg": "#23272f",
                "gutter_fg": "#6b7280", "keyword": "#7cc4ff", "string": "#c3e88d",
                "comment": "#7a8699", "number": "#f7a072", "func": "#c792ea"}
    return {"bg": "#ffffff", "fg": "#1f2933", "insert": "#1f2933", "gutter_bg": "#f3f5f8",
            "gutter_fg": "#98a2b3", "keyword": "#1a56b8", "string": "#0f7b53",
            "comment": "#8a94a6", "number": "#b45309", "func": "#7c3aed"}


def status_colors(style) -> dict:
    dark = is_dark(style)
    return {"ok": "#5ec98a" if dark else "#1a7f52",
            "warn": "#e5b567" if dark else "#9a6700",
            "err": "#ef7b7b" if dark else "#b4232c",
            "info": "#9db4d0" if dark else "#425466"}


def set_theme(root, name: str):
    try:
        style = root.style if hasattr(root, "style") else tkttk.Style()
        style.theme_use(name)
    except Exception:
        pass
