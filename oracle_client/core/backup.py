"""写操作备份与还原。

在执行 UPDATE / DELETE / INSERT 之前，把“受影响的数据”原样抓下来存成 JSON 文件；
需要反悔时，按 ROWID / 主键 / 全列匹配把数据还原回去。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import BackupRecord
from .config import default_backup_dir

ROWID_COL = "ROWID__"
MAX_BACKUP_ROWS = 200000


# ---------------------------------------------------------------- 序列化
def _encode_value(v: Any) -> Any:
    if v is None:
        return {"__t": "null"}
    if isinstance(v, bool):
        return {"__t": "bool", "v": 1 if v else 0}
    if isinstance(v, datetime):
        return {"__t": "datetime", "v": v.strftime("%Y-%m-%d %H:%M:%S.%f")}
    if isinstance(v, Decimal):
        return {"__t": "decimal", "v": str(v)}
    if isinstance(v, (int, float)):
        return {"__t": "number", "v": v}
    if isinstance(v, bytes):
        return {"__t": "bytes", "v": "<二进制 %d 字节>" % len(v)}
    return {"__t": "str", "v": str(v)}


def _decode_value(o: Any) -> Any:
    if isinstance(o, dict) and "__t" in o:
        t = o.get("__t")
        v = o.get("v")
        if t == "null":
            return None
        if t == "bool":
            return bool(v)
        if t == "datetime":
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(v, fmt)
                except Exception:
                    continue
            return v
        if t == "decimal":
            try:
                return Decimal(str(v))
            except Exception:
                return v
        if t in ("number", "str", "bytes"):
            return v
        return v
    return o


def encode_rows(columns: List[str], rows: List[tuple]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        out.append({c: _encode_value(v) for c, v in zip(columns, r)})
    return out


def decode_rows(columns: List[str], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        out.append({c: _decode_value(r.get(c)) for c in columns})
    return out


# ---------------------------------------------------------------- 管理器
class BackupManager:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.dir = default_backup_dir()
            self.dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.dir / "_index.json"
        self._index: Optional[List[dict]] = None

    # ---------- 索引 ----------
    def _load_index(self) -> List[dict]:
        if self._index is not None:
            return self._index
        try:
            if self._index_path.exists():
                with open(self._index_path, "r", encoding="utf-8") as f:
                    self._index = list(json.load(f) or [])
            else:
                self._index = self._rebuild_index()
        except Exception:
            self._index = self._rebuild_index()
        return self._index

    def _rebuild_index(self) -> List[dict]:
        items = []
        for f in self.dir.glob("*.json"):
            if f.name.startswith("_"):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                items.append(_summary_of(data, f))
            except Exception:
                continue
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        self._index = items
        self._save_index()
        return items

    def _save_index(self):
        try:
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(self._index or [], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 创建 ----------
    def create(self, *, connection: str, schema: str, table: str, operation: str, sql: str,
               capture_sql: str, columns: List[str], rows: List[tuple], pk_columns: List[str],
               note: str = "") -> Optional[BackupRecord]:
        if not rows:
            return None
        if len(rows) > MAX_BACKUP_ROWS:
            rows = rows[:MAX_BACKUP_ROWS]
            note = (note + f" 仅备份前 {MAX_BACKUP_ROWS} 行").strip()
        bid = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _rand_suffix()
        has_rowid = ROWID_COL in (columns or [])
        rec = BackupRecord(
            id=bid, created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            connection=connection, schema=schema, table=table, operation=operation.upper(),
            rowcount=len(rows), sql=(sql or "")[:8000], capture_sql=(capture_sql or "")[:8000],
            columns=list(columns), pk_columns=list(pk_columns or []), has_rowid=has_rowid,
            rows=encode_rows(columns, rows), note=note,
        )
        path = self.dir / f"{bid}.json"
        payload = _record_to_dict(rec)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        rec.size_bytes = path.stat().st_size
        idx = self._load_index()
        idx.insert(0, _summary_of(payload, path))
        self._save_index()
        return rec

    # ---------- 读取 ----------
    def list(self) -> List[dict]:
        return list(self._load_index())

    def load(self, backup_id: str) -> Optional[BackupRecord]:
        path = self.dir / f"{backup_id}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _dict_to_record(data)

    def mark_restored(self, backup_id: str) -> bool:
        """记下“这份备份已经还原过”，否则列表里那段“已于 X 还原过”永远不会出现。"""
        path = self.dir / f"{backup_id}.json"
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["restored_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            self._index = None      # 让索引下次重新读，别留在旧状态
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, backup_id: str) -> bool:
        path = self.dir / f"{backup_id}.json"
        try:
            if path.exists():
                path.unlink()
        except Exception:
            return False
        self._index = [i for i in self._load_index() if i.get("id") != backup_id]
        self._save_index()
        return True

    def clear_all(self) -> int:
        n = 0
        for item in self.list():
            if self.delete(item.get("id", "")):
                n += 1
        return n

    def total_size(self) -> int:
        return sum(int(i.get("size_bytes") or 0) for i in self._load_index())

    def open_folder(self):
        try:
            import os
            import subprocess

            subprocess.Popen(["explorer", str(self.dir)])
        except Exception:
            pass


def _rand_suffix() -> str:
    import random
    import string

    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))


def _record_to_dict(rec: BackupRecord) -> dict:
    d = {
        "id": rec.id, "created_at": rec.created_at, "connection": rec.connection,
        "schema": rec.schema, "table": rec.table, "operation": rec.operation,
        "rowcount": rec.rowcount, "sql": rec.sql, "capture_sql": rec.capture_sql,
        "columns": rec.columns, "pk_columns": rec.pk_columns, "has_rowid": rec.has_rowid,
        "rows": rec.rows, "note": rec.note, "restored_at": rec.restored_at,
    }
    return d


def _summary_of(data: dict, path: Path) -> dict:
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    return {
        "id": data.get("id", path.stem),
        "created_at": data.get("created_at", ""),
        "connection": data.get("connection", ""),
        "schema": data.get("schema", ""),
        "table": data.get("table", ""),
        "operation": data.get("operation", ""),
        "rowcount": int(data.get("rowcount") or 0),
        "has_rowid": bool(data.get("has_rowid")),
        "note": data.get("note", ""),
        "restored_at": data.get("restored_at", ""),
        "size_bytes": size,
    }


def _dict_to_record(data: dict) -> BackupRecord:
    return BackupRecord(
        id=data.get("id", ""), created_at=data.get("created_at", ""),
        connection=data.get("connection", ""), schema=data.get("schema", ""),
        table=data.get("table", ""), operation=data.get("operation", ""),
        rowcount=int(data.get("rowcount") or 0), sql=data.get("sql", ""),
        capture_sql=data.get("capture_sql", ""), columns=list(data.get("columns") or []),
        pk_columns=list(data.get("pk_columns") or []), has_rowid=bool(data.get("has_rowid")),
        rows=list(data.get("rows") or []), note=data.get("note", ""),
        restored_at=data.get("restored_at", ""), size_bytes=int(data.get("size_bytes") or 0),
    )


# ---------------------------------------------------------------- 还原
def quote_ident(name: str) -> str:
    n = (name or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", n) and n.upper() == n:
        return n
    return '"' + n.replace('"', '""') + '"'


def full_table(schema: str, table: str) -> str:
    if schema:
        return f"{quote_ident(schema)}.{quote_ident(table)}"
    return quote_ident(table)


def build_restore_statements(rec: BackupRecord, dialect: str = "oracle") -> List[Tuple[str, dict]]:
    """根据备份内容生成还原语句，返回 [(sql, params)]。"""
    cols_all = list(rec.columns or [])
    has_rowid = rec.has_rowid and ROWID_COL in cols_all
    data_cols = [c for c in cols_all if c != ROWID_COL]
    rows = decode_rows(cols_all, rec.rows)

    stmts: List[Tuple[str, dict]] = []
    table = full_table(rec.schema, rec.table)
    op = (rec.operation or "").upper()

    if op in ("DELETE", "MERGE"):
        # 删掉的行 -> 插回去
        if not data_cols:
            return stmts
        for r in rows:
            names = [quote_ident(c) for c in data_cols]
            params = {}
            ph = []
            for i, c in enumerate(data_cols):
                params[f"p{i}"] = r.get(c)
                ph.append(f":p{i}")
            sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(ph)})"
            stmts.append((sql, params))
        return stmts

    if op == "UPDATE":
        # 被改过的行 -> 用旧值覆盖回去
        key_cols: List[str] = []
        if has_rowid:
            key_cols = [ROWID_COL]
        elif rec.pk_columns:
            key_cols = [c for c in rec.pk_columns if c in data_cols]
        if not key_cols:
            key_cols = [c for c in data_cols]
        for r in rows:
            sets, params = [], {}
            idx = 0
            for c in data_cols:
                if c in key_cols and not (has_rowid and c == ROWID_COL):
                    continue
                params[f"s{idx}"] = r.get(c)
                sets.append(f"{quote_ident(c)} = :s{idx}")
                idx += 1
            if not sets:
                continue
            where, wparams = [], {}
            if has_rowid:
                where.append("ROWID = :rid")
                wparams["rid"] = r.get(ROWID_COL)
            else:
                for i, c in enumerate(key_cols):
                    v = r.get(c)
                    if v is None:
                        where.append(f"{quote_ident(c)} IS NULL")
                    else:
                        wparams[f"w{i}"] = v
                        where.append(f"{quote_ident(c)} = :w{i}")
            params.update(wparams)
            sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {' AND '.join(where)}"
            stmts.append((sql, params))
        return stmts

    if op == "INSERT":
        # 插进去的行 -> 删掉
        key_cols = [c for c in (rec.pk_columns or []) if c in data_cols]
        if has_rowid:
            key_cols = [ROWID_COL]
        if not key_cols:
            key_cols = [c for c in data_cols]
        for r in rows:
            where, params = [], {}
            if has_rowid:
                where.append("ROWID = :rid")
                params["rid"] = r.get(ROWID_COL)
            else:
                for i, c in enumerate(key_cols):
                    v = r.get(c)
                    if v is None:
                        where.append(f"{quote_ident(c)} IS NULL")
                    else:
                        params[f"w{i}"] = v
                        where.append(f"{quote_ident(c)} = :w{i}")
            sql = f"DELETE FROM {table} WHERE {' AND '.join(where)}"
            stmts.append((sql, params))
        return stmts

    return stmts


def restore_preview(rec: BackupRecord, dialect: str = "oracle", sample: int = 3) -> str:
    stmts = build_restore_statements(rec, dialect)
    if not stmts:
        return "-- 没有可生成的还原语句"
    lines = [f"-- 共 {len(stmts)} 条还原语句（预览前 {min(sample, len(stmts))} 条）"]
    for sql, params in stmts[:sample]:
        lines.append(_inline(sql, params) + ";")
    if len(stmts) > sample:
        lines.append(f"-- …… 其余 {len(stmts) - sample} 条省略")
    return "\n".join(lines)


def _inline(sql: str, params: dict) -> str:
    out = sql
    for k, v in sorted(params.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(f":{k}", _sql_literal(v))
    return out


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, datetime):
        return f"TO_DATE('{v.strftime('%Y-%m-%d %H:%M:%S')}','YYYY-MM-DD HH24:MI:SS')"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"
