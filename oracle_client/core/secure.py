"""口令加密存储。

Windows 下使用系统级 DPAPI（CryptProtectData），密文只能被同一 Windows 账户解密；
非 Windows 或调用失败时退化为可逆的简单编码，并在文本中标注前缀以便区分。
"""
from __future__ import annotations

import base64
import os
import sys

DPAPI_PREFIX = "DPAPI:"
PLAIN_PREFIX = "PLAIN:"


def _win_available() -> bool:
    return sys.platform.startswith("win")


def _to_blob(data: bytes):
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, max(len(data), 1))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_to_bytes(blob) -> bytes:
    import ctypes

    ptr = ctypes.cast(blob.pbData, ctypes.POINTER(ctypes.c_char))
    try:
        return bytes(ptr[: blob.cbData])
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(plain: str) -> str:
    """加密字符串，返回可安全写入配置文件的文本。"""
    if not plain:
        return ""
    raw = plain.encode("utf-8")
    if _win_available():
        try:
            import ctypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            blob_in = _to_blob(raw)
            blob_out = DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            )
            if ok:
                return DPAPI_PREFIX + base64.b64encode(_blob_to_bytes(blob_out)).decode("ascii")
        except Exception:
            pass
    # 退化方案：仅做编码混淆，明确标注，避免误以为安全
    return PLAIN_PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect(text: str) -> str:
    """还原 protect() 生成的文本。"""
    if not text:
        return ""
    if text.startswith(DPAPI_PREFIX):
        if not _win_available():
            return ""
        try:
            import ctypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            raw = base64.b64decode(text[len(DPAPI_PREFIX):])
            blob_in = _to_blob(raw)
            blob_out = DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            )
            if ok:
                return _blob_to_bytes(blob_out).decode("utf-8", errors="replace")
        except Exception:
            return ""
        return ""
    if text.startswith(PLAIN_PREFIX):
        try:
            return base64.b64decode(text[len(PLAIN_PREFIX):]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def is_encrypted(text: str) -> bool:
    return bool(text) and (text.startswith(DPAPI_PREFIX) or text.startswith(PLAIN_PREFIX))


def encryption_note() -> str:
    if _win_available():
        return "口令使用 Windows 账户级加密（DPAPI）保存在本机，仅当前 Windows 用户可读。"
    return "当前系统不支持系统级加密，口令仅做编码混淆保存，请注意本机安全。"
