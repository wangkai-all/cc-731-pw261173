"""Sparse matrix repository: labels, logical triples, CSR/CSC layouts, queries.

Standard library only.  See ``docs/DESIGN.md`` for the invariants this file is
built around.  Physical layouts are stored as real binary arrays:

    <root>/layouts/v<version>-<layout>/
        majors.bin   int64   bookkeeping: major axis label id per stored slot
        indices.bin  int64   minor axis label id
        kinds.bin    uint8   value | zero | missing
        data.bin     float64 numeric value (0.0 for zero, NaN for missing)
        indptr.bin   int64   offset array, written when conversion finishes

Conversion appends chunk by chunk (chunk size derived from the memory
budget), records progress after every chunk, and can be paused / resumed /
recovered after a process restart.  A layout is readable only after a full
verification pass compares every stored slot against the logical triples.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import os
import struct
import threading
import time
import uuid
from typing import Any, Iterable

MAGIC = b"SPMATRIX"
FORMAT_VERSION = 1

KIND_VALUE = "value"
KIND_ZERO = "zero"
KIND_MISSING = "missing"
KINDS = (KIND_VALUE, KIND_ZERO, KIND_MISSING)
KIND_CODE = {KIND_VALUE: 0, KIND_ZERO: 1, KIND_MISSING: 2}
CODE_KIND = {v: k for k, v in KIND_CODE.items()}
AXES = ("row", "col")
LAYOUTS = ("csr", "csc")

MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "missing", "缺测", "空"}

VERIFY_SLICE = 4096


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class MatrixError(Exception):
    status = 400


class UnknownLabel(MatrixError):
    status = 404


class Conflict(MatrixError):
    status = 409


class LayoutNotReady(MatrixError):
    status = 409


class VerificationFailed(MatrixError):
    status = 409


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _json_dump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def float_or_nan(value: float | None) -> float:
    return math.nan if value is None else float(value)


# ---------------------------------------------------------------------------
# binary array directory
# ---------------------------------------------------------------------------

_ARRAY_META = {
    # name: (struct format, item size)
    "majors": ("q", 8),
    "counts": ("q", 8),
    "indptr": ("q", 8),
    "indices": ("q", 8),
    "kinds": ("B", 1),
    "data": ("d", 8),
}


class LayoutFiles:
    """Binary arrays for one version/layout, appended chunk by chunk."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.bin")

    def count(self, name: str) -> int:
        p = self.path(name)
        if not os.path.exists(p):
            return 0
        size = os.path.getsize(p)
        return size // _ARRAY_META[name][1]

    def reset(self) -> None:
        for name in _ARRAY_META:
            p = self.path(name)
            if os.path.exists(p):
                os.unlink(p)

    def truncate_to(self, name: str, count: int) -> None:
        p = self.path(name)
        if not os.path.exists(p):
            open(p, "wb").close()
            return
        with open(p, "r+b") as fh:
            fh.truncate(max(0, count) * _ARRAY_META[name][1])

    def append(self, name: str, payload: bytes, *, sync: bool = True) -> None:
        if not payload:
            return
        with open(self.path(name), "ab") as fh:
            fh.write(payload)
            fh.flush()
            if sync:
                os.fsync(fh.fileno())

    def write_all(self, name: str, payload: bytes) -> None:
        tmp = self.path(name) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path(name))

    def read_raw(self, name: str) -> bytes:
        p = self.path(name)
        if not os.path.exists(p):
            return b""
        with open(p, "rb") as fh:
            return fh.read()

    def read_slice(self, name: str, start: int, count: int) -> list:
        fmt, size = _ARRAY_META[name]
        if count <= 0:
            return []
        p = self.path(name)
        if not os.path.exists(p):
            return []
        with open(p, "rb") as fh:
            fh.seek(start * size)
            raw = fh.read(count * size)
        usable = len(raw) // size
        if not usable:
            return []
        return list(struct.unpack(f"<{usable}{fmt}", raw[: usable * size]))

    def read_kinds(self, start: int, count: int) -> list[str]:
        raw = self.read_raw("kinds")
        item = raw[start:start + count]
        return [CODE_KIND.get(b, f"code{b}") for b in item]

    def read_all(self, name: str) -> list:
        return self.read_slice(name, 0, self.count(name))

    def bytes(self) -> int:
        total = 0
        for name in _ARRAY_META:
            p = self.path(name)
            if os.path.exists(p):
                total += os.path.getsize(p)
        return total

    def digest(self) -> str:
        h = hashlib.sha256()
        for name in ("indptr", "indices", "kinds", "data"):
            p = self.path(name)
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
        return h.hexdigest()

    def delete_bookkeeping(self) -> None:
        for name in ("majors", "counts"):
            p = self.path(name)
            if os.path.exists(p):
                os.unlink(p)


def _pack(name: str, values: Iterable) -> bytes:
    fmt, _ = _ARRAY_META[name]
    values = list(values)
    if not values:
        return b""
    if name == "kinds":
        return bytes(KIND_CODE[v] for v in values)
    return struct.pack(f"<{len(values)}{fmt}", *values)


# ---------------------------------------------------------------------------
# triples parsing
# ---------------------------------------------------------------------------


def parse_kind_value(kind: str | None, raw: Any) -> tuple[str, float | None]:
    """Normalise an explicit kind/value pair.

    A numeric zero is an explicit zero; blank/NA/missing tokens are explicit
    missing values.  They are never collapsed into each other or into
    "no element".
    """
    if kind is not None:
        text = str(kind).strip().lower()
        aliases = {"val": KIND_VALUE, "number": KIND_VALUE, "缺失": KIND_MISSING,
                   "空值": KIND_MISSING, "0": KIND_ZERO}
        text = aliases.get(text, text)
        if text not in KINDS:
            raise MatrixError(f"未知的元素类型 {kind!r}")
        if text == KIND_VALUE:
            if raw is None or str(raw).strip().lower() in MISSING_TOKENS:
                raise MatrixError("kind=value 需要数值")
            return KIND_VALUE, float(raw)
        if text == KIND_ZERO:
            return KIND_ZERO, None
        return KIND_MISSING, None
    if raw is None:
        return KIND_MISSING, None
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in MISSING_TOKENS:
            return KIND_MISSING, None
        value = float(text)
    else:
        value = float(raw)
    if not math.isfinite(value):
        return KIND_MISSING, None
    if value == 0.0:
        return KIND_ZERO, None
    return KIND_VALUE, value


def parse_triples_text(text: str) -> list[dict[str, Any]]:
    """Parse CSV/TSV triples: row, col, value[, source_version[, source[, kind]]]."""
    text = text.strip("﻿\n\r ")
    if not text:
        raise MatrixError("没有提供三元组")
    sample = text.splitlines()[0]
    delimiter = "\t" if "\t" in sample else (";" if ";" in sample and "," not in sample else ",")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise MatrixError("没有提供三元组")
    first = [c.strip().lower() for c in rows[0]]
    has_header = "row" in first and "col" in first and any(
        h in first for h in ("value", "数值", "kind", "类型"))
    if has_header:
        index = {name: i for i, name in enumerate(first)}

        def col(*names: str, default: int | None = None) -> int | None:
            for name in names:
                if name in index:
                    return index[name]
            return default

        i_row = col("row", "行", default=0)
        i_col = col("col", "列", default=1)
        i_val = col("value", "数值", default=2)
        i_ver = col("source_version", "版本", default=3)
        i_src = col("source", "来源", default=4)
        i_kind = col("kind", "类型", default=5)
        body = rows[1:]
    else:
        i_row, i_col, i_val, i_ver, i_src, i_kind = 0, 1, 2, 3, 4, 5
        body = rows

    def cell(r: list[str], i: int | None) -> str | None:
        if i is None or i >= len(r):
            return None
        return r[i].strip()

    out: list[dict[str, Any]] = []
    for n, r in enumerate(body, start=2 if has_header else 1):
        row_name = cell(r, i_row) or ""
        col_name = cell(r, i_col) or ""
        if not row_name or not col_name:
            raise MatrixError(f"第 {n} 行：行标识与列标识不能为空")
        kind = cell(r, i_kind) or None
        raw_value = cell(r, i_val)
        try:
            if kind is None and (raw_value is None or raw_value == ""):
                kind, value = KIND_MISSING, None
            else:
                kind, value = parse_kind_value(kind, raw_value)
        except ValueError as exc:
            raise MatrixError(f"第 {n} 行：数值无效 {raw_value!r}") from exc
        rec: dict[str, Any] = {"row": row_name, "col": col_name, "kind": kind, "value": value}
        ver = cell(r, i_ver)
        if ver not in (None, ""):
            try:
                rec["source_version"] = int(float(ver))
            except ValueError as exc:
                raise MatrixError(f"第 {n} 行：来源版本号必须是整数") from exc
        src = cell(r, i_src)
        if src:
            rec["source"] = src
        out.append(rec)
    if not out:
        raise MatrixError("没有提供三元组")
    return out


def _combine_sum(a: dict, b: dict) -> dict:
    """Sum duplicate coordinates.  missing = no data, zero = real data."""
    if a["kind"] == KIND_MISSING and b["kind"] == KIND_MISSING:
        kind, value = KIND_MISSING, None
    elif a["kind"] == KIND_MISSING:
        kind, value = b["kind"], b["value"]
    elif b["kind"] == KIND_MISSING:
        kind, value = a["kind"], a["value"]
    else:
        total = (a["value"] if a["kind"] == KIND_VALUE else 0.0) + (
            b["value"] if b["kind"] == KIND_VALUE else 0.0)
        if total == 0.0:
            kind, value = KIND_ZERO, None
        else:
            kind, value = KIND_VALUE, total
    out = dict(b)
    out.update(kind=kind, value=value,
               source_version=max(a["source_version"], b["source_version"]))
    return out


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


class Repository:
    def __init__(self, root: str, *, recover_jobs: bool = True):
        self.root = os.path.abspath(root)
        self.state_path = os.path.join(self.root, "repository.json")
        self.export_dir = os.path.join(self.root, "exports")
        self.layout_root = os.path.join(self.root, "layouts")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.export_dir, exist_ok=True)
        os.makedirs(self.layout_root, exist_ok=True)
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._io_locks: dict[tuple[int, str], threading.Lock] = {}
        self._orders: dict[tuple, list] = {}
        self._array_cache: dict[tuple, dict] = {}
        self.state = self._empty_state()
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                self.state = json.load(fh)
            if self.state.get("schema") != 2:
                self.state = self._migrate_old_state(self.state)
            self._restore_state()
        if recover_jobs:
            self._recover_interrupted_jobs()

    # -- state -------------------------------------------------------------
    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "revision": 0,
            "shape_rev": 0,
            "next": {"row": 0, "col": 0, "source": 1, "version": 1},
            "labels": {"row": {}, "col": {}},
            "sources": {},
            "versions": {},
            "layouts": {},
            "exports": [],
            "audit": [],
        }

    def _migrate_old_state(self, old: dict) -> dict:
        """Discard layout records from an incompatible container format.

        Logical data (labels, sources, versions) is preserved; published
        arrays are dropped because their on-disk format changed, so they
        would fail verification anyway.
        """
        state = self._empty_state()
        state["revision"] = int(old.get("revision", 0))
        state["next"] = old.get("next", state["next"])
        for axis in AXES:
            state["labels"][axis] = old.get("labels", {}).get(axis, {})
        state["sources"] = old.get("sources", {})
        for vid, v in (old.get("versions") or {}).items():
            v = dict(v)
            v.setdefault("logical_rev", 0)
            v.pop("state", None)
            state["versions"][vid] = v
        state["shape_rev"] = len(state["labels"]["row"]) + len(state["labels"]["col"])
        state["audit"].append({
            "at": _now(), "action": "repository.migrate",
            "detail": {"from_schema": old.get("schema"), "to_schema": 2,
                       "说明": "旧版布局数组已丢弃，请从逻辑三元组重新转换"},
        })
        return state

    def _restore_state(self) -> None:
        state = self.state
        state["versions"] = {int(k): v for k, v in state.get("versions", {}).items()}
        state["layouts"] = {int(k): dict(v) for k, v in state.get("layouts", {}).items()}
        state["sources"] = {int(k): v for k, v in state.get("sources", {}).items()}
        for axis in AXES:
            state["labels"][axis] = {int(k): v for k, v in state["labels"][axis].items()}
        state.setdefault("shape_rev", 0)
        for v in state["versions"].values():
            v["elements"] = {
                tuple(int(x) for x in key.split(",")): rec
                for key, rec in v["elements"].items()
            }
            v.setdefault("logical_rev", 0)
        for layouts in state["layouts"].values():
            for lay in layouts.values():
                if lay.get("state") == "building":
                    lay["state"] = "paused"
                    lay["token"] = None
                    lay["pause_reason"] = "进程重启"
                    if lay.get("progress"):
                        lay["progress"]["interrupted"] = True

    def _save(self) -> None:
        payload = dict(self.state)
        versions = {}
        for vid, v in self.state["versions"].items():
            vv = dict(v)
            vv["elements"] = {f"{r},{c}": rec for (r, c), rec in v["elements"].items()}
            versions[str(vid)] = vv
        payload["versions"] = versions
        payload["layouts"] = {str(k): v for k, v in self.state["layouts"].items()}
        _atomic_write(self.state_path, _json_dump(payload))

    def _bump(self) -> None:
        self.state["revision"] += 1

    def _record(self, action: str, **detail: Any) -> None:
        self.state["audit"].append({"at": _now(), "action": action, "detail": detail})
        if len(self.state["audit"]) > 3000:
            del self.state["audit"][:-3000]

    # -- labels ------------------------------------------------------------
    def _labels(self, axis: str) -> dict[int, dict]:
        if axis not in AXES:
            raise MatrixError("轴只能是 row 或 col")
        return self.state["labels"][axis]

    def labels(self, axis: str) -> list[dict]:
        return sorted(self._labels(axis).values(), key=lambda x: (x["position"], x["id"]))

    def _label_by_name(self, axis: str, name: str) -> dict | None:
        for lab in self._labels(axis).values():
            if lab["name"] == name:
                return lab
        return None

    def _ensure_label(self, axis: str, name: str) -> tuple[dict, bool]:
        name = (name or "").strip()
        if not name:
            raise MatrixError("标签名称不能为空")
        found = self._label_by_name(axis, name)
        if found:
            return found, False
        labels = self._labels(axis)
        lid = self.state["next"][axis]
        position = max((l["position"] for l in labels.values()), default=-1) + 1
        lab = {"id": lid, "name": name, "position": position, "created_at": _now()}
        labels[lid] = lab
        self.state["next"][axis] = lid + 1
        self.state["shape_rev"] += 1
        return lab, True

    def add_label(self, axis: str, name: str) -> dict:
        with self._lock:
            if self._label_by_name(axis, name.strip()):
                raise Conflict(f"{axis} 轴上已存在标签 {name!r}")
            lab, _ = self._ensure_label(axis, name)
            invalidated = self._mark_stale_layouts("new label changed the matrix shape")
            self._bump()
            self._record("label.add", axis=axis, label_id=lab["id"], name=lab["name"],
                         invalidated=invalidated)
            self._save()
            return {"label": lab, "invalidated_layouts": invalidated}

    def rename_label(self, axis: str, label_id: int, new_name: str) -> dict:
        with self._lock:
            lab = self._labels(axis).get(label_id)
            if not lab:
                raise UnknownLabel(f"没有 {axis} 标签 #{label_id}")
            new_name = (new_name or "").strip()
            if not new_name:
                raise MatrixError("标签名称不能为空")
            other = self._label_by_name(axis, new_name)
            if other and other["id"] != label_id:
                raise Conflict(f"{axis} 轴上已存在标签 {new_name!r}")
            old = lab["name"]
            lab["name"] = new_name
            self._bump()
            self._record("label.rename", axis=axis, label_id=label_id, old=old, name=new_name,
                         note="label numbering unchanged; layouts stay valid")
            self._save()
            return {"label": lab}

    def reorder_labels(self, axis: str, order: list[int]) -> dict:
        with self._lock:
            labels = self._labels(axis)
            try:
                order = [int(x) for x in order]
            except (TypeError, ValueError) as exc:
                raise MatrixError("排序必须是标签编号列表") from exc
            if len(order) != len(set(order)) or set(order) != set(labels):
                raise MatrixError("排序必须恰好包含该轴上的每个标签一次")
            for pos, lid in enumerate(order):
                labels[lid]["position"] = pos
            self._bump()
            self._record("label.reorder", axis=axis, order=order,
                         note="positions changed; label ids and stored arrays unchanged")
            self._save()
            return {"labels": self.labels(axis), "layouts_unchanged": True}

    # -- sources -----------------------------------------------------------
    def add_source(self, name: str) -> dict:
        with self._lock:
            for src in self.state["sources"].values():
                if src["name"] == name and not src["deleted"]:
                    return src
            sid = self.state["next"]["source"]
            self.state["next"]["source"] = sid + 1
            src = {"id": sid, "name": name.strip() or f"source-{sid}",
                   "created_at": _now(), "deleted": False}
            self.state["sources"][sid] = src
            self._bump()
            self._record("source.add", source_id=sid, name=src["name"])
            self._save()
            return src

    def delete_source(self, source_id: int) -> dict:
        with self._lock:
            src = self.state["sources"].get(source_id)
            if not src or src["deleted"]:
                raise UnknownLabel(f"没有来源 #{source_id}")
            src["deleted"] = True
            affected = 0
            for v in self.state["versions"].values():
                for el in v["elements"].values():
                    if el.get("source_id") == source_id:
                        el["source_id"] = None
                        affected += 1
            self._bump()
            self._record("source.delete", source_id=source_id, name=src["name"],
                         orphaned_elements=affected,
                         note="element values kept; only provenance was removed")
            self._save()
            return {"source_id": source_id, "orphaned_elements": affected,
                    "layouts_unchanged": True}

    # -- versions ----------------------------------------------------------
    def ingest(self, records: list[dict], *, policy: str = "sum", label: str = "",
               source: str | None = None, default_source_version: int = 1,
               layout_hint: str = "csr") -> dict:
        if policy not in ("sum", "latest"):
            raise MatrixError("重复坐标规则只能是 sum 或 latest")
        if layout_hint not in LAYOUTS:
            raise MatrixError("初始布局只能是 csr 或 csc")
        if not records:
            raise MatrixError("没有提供三元组")
        with self._lock:
            vid = self.state["next"]["version"]
            self.state["next"]["version"] = vid + 1
            v = {
                "version_id": vid,
                "label": (label or f"数据版 {vid}").strip(),
                "policy": policy,
                "layout_hint": layout_hint,
                "matrix_id": uuid.uuid4().hex,
                "created_at": _now(),
                "logical_rev": 0,
                "elements": {},
                "notices": [],
                "import": {"records": len(records), "duplicates": 0,
                           "cancelled_to_zero": 0, "explicit_missing": 0,
                           "explicit_zero": 0, "sources": []},
            }
            source_ids: set[int] = set()
            for index, rec in enumerate(records):
                row_name = str(rec.get("row", "")).strip()
                col_name = str(rec.get("col", "")).strip()
                if not row_name or not col_name:
                    raise MatrixError(f"第 {index + 1} 条：行标识与列标识不能为空")
                try:
                    kind, value = parse_kind_value(rec.get("kind"), rec.get("value"))
                except ValueError as exc:
                    raise MatrixError(f"第 {index + 1} 条：数值无效") from exc
                src_name = (rec.get("source") or source or "").strip()
                src_id = None
                if src_name:
                    src = self.add_source(src_name)
                    src_id = src["id"]
                    source_ids.add(src_id)
                try:
                    source_version = int(rec.get("source_version", default_source_version))
                except (TypeError, ValueError) as exc:
                    raise MatrixError(
                        f"第 {index + 1} 条：来源版本号必须是整数") from exc
                r, _ = self._ensure_label("row", row_name)
                c, _ = self._ensure_label("col", col_name)
                key = (r["id"], c["id"])
                el = {
                    "row": r["id"], "col": c["id"], "kind": kind, "value": value,
                    "source_id": src_id, "source_version": source_version,
                    "import_order": index, "updated_at": _now(),
                }
                old = v["elements"].get(key)
                if old is None:
                    v["elements"][key] = el
                else:
                    v["import"]["duplicates"] += 1
                    if policy == "latest":
                        if (el["source_version"], el["import_order"]) >= (
                                old["source_version"], old["import_order"]):
                            v["elements"][key] = el
                    else:
                        merged = _combine_sum(old, el)
                        if merged["kind"] == KIND_ZERO and old["kind"] != KIND_ZERO:
                            v["import"]["cancelled_to_zero"] += 1
                            if len(v["notices"]) < 12:
                                v["notices"].append(
                                    f"坐标 ({row_name}, {col_name}) 的重复值求和为 0，保留为显式零")
                        v["elements"][key] = merged
            counts = {KIND_VALUE: 0, KIND_ZERO: 0, KIND_MISSING: 0}
            for el in v["elements"].values():
                counts[el["kind"]] += 1
            v["import"]["explicit_missing"] = counts[KIND_MISSING]
            v["import"]["explicit_zero"] = counts[KIND_ZERO]
            v["import"]["stored"] = len(v["elements"])
            v["import"]["sources"] = sorted(source_ids)
            self.state["versions"][vid] = v
            self.state["layouts"][vid] = {}
            # New labels change the matrix shape; only layouts that no longer
            # match the current version/shape are invalidated.
            self._mark_stale_layouts("new labels imported; matrix shape changed")
            self._bump()
            self._record("version.import", version_id=vid, policy=policy,
                         records=len(records), duplicates=v["import"]["duplicates"],
                         stored=len(v["elements"]), label=v["label"])
            self._save()
            return self.version_view(vid)

    # -- views -------------------------------------------------------------
    def _layout_view(self, lay: dict) -> dict:
        files = LayoutFiles(lay["dir"])
        prog = lay.get("progress") or {}
        nnz = lay.get("nnz", 0)
        return {
            "layout": lay["layout"],
            "state": lay["state"],
            "shape": lay["shape"],
            "nnz": nnz,
            "indptr_len": lay["shape"][0] + 1 if lay["layout"] == "csr" else lay["shape"][1] + 1,
            "arrays_bytes": files.bytes(),
            "verified_at": lay.get("verified_at"),
            "verification": lay.get("verification"),
            "error": lay.get("error"),
            "stale_reason": lay.get("stale_reason"),
            "pause_reason": lay.get("pause_reason"),
            "digest": lay.get("digest"),
            "chunk": lay.get("chunk", 0),
            "budget_bytes": lay.get("budget_bytes", 0),
            "progress": {
                "next_index": int(prog.get("next_index", 0)),
                "total": int(prog.get("total", 0)),
                "chunk": int(prog.get("chunk", 0)),
                "budget": int(prog.get("budget", 0)),
                "chunks_done": int(prog.get("chunks_done", 0)),
                "updated_at": prog.get("updated_at"),
                "interrupted": bool(prog.get("interrupted", False)),
                "percent": round(
                    100.0 * int(prog.get("next_index", 0)) / max(1, int(prog.get("total", 0))), 1)
                if lay["state"] != "ready" else 100.0,
            },
        }

    def version_view(self, version_id: int) -> dict:
        v = self.state["versions"].get(version_id)
        if not v:
            raise UnknownLabel(f"没有数据版 #{version_id}")
        counts = {KIND_VALUE: 0, KIND_ZERO: 0, KIND_MISSING: 0}
        present_rows, present_cols = set(), set()
        for (r, c), el in v["elements"].items():
            counts[el["kind"]] += 1
            present_rows.add(r)
            present_cols.add(c)
        rows = self.labels("row")
        cols = self.labels("col")
        layouts = {name: self._layout_view(lay)
                   for name, lay in sorted(self.state["layouts"].get(version_id, {}).items())}
        ready = [n for n, l in layouts.items() if l["state"] == "ready"]
        if any(l["state"] == "building" for l in layouts.values()):
            state = "building"
        elif any(l["state"] in ("paused", "failed", "stale") for l in layouts.values()):
            state = "attention"
        elif ready:
            state = "published"
        else:
            state = "logical"
        return {
            "version_id": version_id,
            "label": v["label"],
            "policy": v["policy"],
            "matrix_id": v["matrix_id"],
            "created_at": v["created_at"],
            "shape": [len(rows), len(cols)],
            "element_count": len(v["elements"]),
            "counts": {"value": counts[KIND_VALUE], "zero": counts[KIND_ZERO],
                       "missing": counts[KIND_MISSING]},
            "empty_rows": [l["name"] for l in rows if l["id"] not in present_rows],
            "empty_cols": [l["name"] for l in cols if l["id"] not in present_cols],
            "notices": v.get("notices", []),
            "import": v.get("import", {}),
            "layouts": layouts,
            "state": state,
            "readable_layouts": ready,
            "logical_rev": v.get("logical_rev", 0),
            "shape_rev": self.state["shape_rev"],
        }

    def list_versions(self) -> list[dict]:
        with self._lock:
            return [self.version_view(vid) for vid in sorted(self.state["versions"], reverse=True)]

    def logical_triples(self, version_id: int, *, limit: int | None = None,
                        row_names: list[str] | None = None,
                        col_names: list[str] | None = None,
                        row_positions: tuple[int, int] | None = None,
                        col_positions: tuple[int, int] | None = None,
                        kinds: set[str] | None = None) -> list[dict]:
        v = self.state["versions"].get(version_id)
        if not v:
            raise UnknownLabel(f"没有数据版 #{version_id}")
        row_labels = self._labels("row")
        col_labels = self._labels("col")
        row_ids = None
        col_ids = None
        if row_names:
            row_ids = {self._resolve(row_labels, n)["id"] for n in row_names}
        if col_names:
            col_ids = {self._resolve(col_labels, n)["id"] for n in col_names}
        row_pos = {lid: l["position"] for lid, l in row_labels.items()}
        col_pos = {lid: l["position"] for lid, l in col_labels.items()}
        if row_positions:
            lo, hi = row_positions
            row_ids = (row_ids if row_ids is not None else set(row_labels)) & {
                lid for lid, p in row_pos.items() if lo <= p <= hi}
        if col_positions:
            lo, hi = col_positions
            col_ids = (col_ids if col_ids is not None else set(col_labels)) & {
                lid for lid, p in col_pos.items() if lo <= p <= hi}
        items = []
        for (r, c), el in v["elements"].items():
            if row_ids is not None and r not in row_ids:
                continue
            if col_ids is not None and c not in col_ids:
                continue
            if kinds is not None and el["kind"] not in kinds:
                continue
            items.append((row_pos.get(r, 10 ** 9), col_pos.get(c, 10 ** 9), r, c, el))
        items.sort(key=lambda t: t[:4])
        out = []
        for _, _, r, c, el in items[:limit] if limit else items:
            src = self.state["sources"].get(el.get("source_id") or -1)
            out.append({
                "row": row_labels[r]["name"],
                "col": col_labels[c]["name"],
                "row_id": r,
                "col_id": c,
                "kind": el["kind"],
                "value": el["value"],
                "source": src["name"] if src and not src["deleted"] else None,
                "source_version": el.get("source_version"),
            })
        return out

    @staticmethod
    def _resolve(labels: dict[int, dict], name: str) -> dict:
        for lab in labels.values():
            if lab["name"] == name:
                return lab
        raise UnknownLabel(f"未知标签 {name!r}")

    # -- invalidation ------------------------------------------------------
    def _invalidate_all_layouts(self, reason: str) -> list[str]:
        changed = []
        for vid, layouts in self.state["layouts"].items():
            for name, lay in layouts.items():
                if lay["state"] in ("ready", "building", "paused", "failed"):
                    lay["state"] = "stale"
                    lay["stale_reason"] = reason
                    lay["token"] = None
                    lay["error"] = None
                    changed.append(f"{vid}:{name}")
        return changed

    def _mark_stale_layouts(self, reason: str) -> list[str]:
        """Mark only layouts that no longer match their version as stale."""
        changed = []
        for vid, layouts in self.state["layouts"].items():
            v = self.state["versions"].get(vid)
            if not v:
                continue
            for name, lay in layouts.items():
                if lay["state"] in ("ready", "building", "paused", "failed"):
                    if not self._layout_is_current(lay, vid):
                        lay["state"] = "stale"
                        lay["stale_reason"] = reason
                        lay["token"] = None
                        lay["error"] = None
                        changed.append(f"{vid}:{name}")
        return changed

    def _invalidate_version_layouts(self, version_id: int, reason: str) -> list[str]:
        changed = []
        for name, lay in self.state["layouts"].get(version_id, {}).items():
            lay["state"] = "stale"
            lay["stale_reason"] = reason
            lay["token"] = None
            lay["error"] = None
            changed.append(name)
        return changed

    def _layout_is_current(self, lay: dict, version_id: int) -> bool:
        v = self.state["versions"][version_id]
        return (lay.get("version_rev") == v.get("logical_rev", 0)
                and lay.get("shape_rev") == self.state["shape_rev"])

    # -- layout files / conversion -----------------------------------------
    def _layout_dir(self, version_id: int, layout: str) -> str:
        return os.path.join(self.layout_root, f"v{version_id}-{layout}")

    def _io_lock(self, version_id: int, layout: str) -> threading.Lock:
        key = (version_id, layout)
        with self._lock:
            lock = self._io_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._io_locks[key] = lock
            return lock

    def _new_layout(self, version_id: int, layout: str) -> dict:
        directory = self._layout_dir(version_id, layout)
        files = LayoutFiles(directory)
        files.reset()
        shape = [len(self._labels("row")), len(self._labels("col"))]
        n_major = shape[0] if layout == "csr" else shape[1]
        files.write_all("counts", _pack("counts", [0] * n_major))
        return {
            "layout": layout,
            "version_id": version_id,
            "state": "idle",
            "dir": directory,
            "shape": shape,
            "nnz": 0,
            "digest": "",
            "version_rev": self.state["versions"][version_id].get("logical_rev", 0),
            "shape_rev": self.state["shape_rev"],
            "token": None,
            "verified_at": None,
            "verification": None,
            "error": None,
            "stale_reason": None,
            "pause_reason": None,
            "budget_bytes": 4096,
            "pace_ms": 0,
            "chunk": 128,
            "progress": {
                "next_index": 0,
                "total": 0,
                "chunk": 128,
                "budget": 4096,
                "chunks_done": 0,
                "updated_at": None,
                "interrupted": False,
                "started_at": None,
            },
        }

    def start_conversion(self, version_id: int, layout: str, *, budget_bytes: int = 4096,
                         pace_ms: int = 0, restart: bool = False) -> dict:
        if layout not in LAYOUTS:
            raise MatrixError("布局只能是 csr 或 csc")
        with self._lock:
            v = self.state["versions"].get(version_id)
            if not v:
                raise UnknownLabel(f"没有数据版 #{version_id}")
            layouts = self.state["layouts"].setdefault(version_id, {})
            lay = layouts.get(layout)
            current = lay is not None and self._layout_is_current(lay, version_id)
            if lay and lay["state"] == "ready" and current and not restart:
                return {"started": False, "reason": "already ready",
                        "layout": self._layout_view(lay)}
            if lay and lay["state"] == "building":
                return {"started": False, "reason": "already building",
                        "layout": self._layout_view(lay)}
            resume_ok = (
                lay is not None and lay["state"] == "paused" and current
                and int((lay.get("progress") or {}).get("next_index", 0)) > 0)
            if lay is None or restart or lay["state"] in ("stale", "failed") or not resume_ok:
                lay = self._new_layout(version_id, layout)
                layouts[layout] = lay
            lay["state"] = "building"
            lay["token"] = uuid.uuid4().hex
            lay["error"] = None
            lay["stale_reason"] = None
            lay["pause_reason"] = None
            lay["budget_bytes"] = max(256, int(budget_bytes))
            lay["pace_ms"] = max(0, min(2000, int(pace_ms)))
            lay["chunk"] = max(1, lay["budget_bytes"] // 32)
            prog = lay["progress"]
            prog["total"] = len(v["elements"])
            prog["chunk"] = lay["chunk"]
            prog["budget"] = lay["budget_bytes"]
            prog["interrupted"] = False
            prog["updated_at"] = _now()
            prog["started_at"] = prog.get("started_at") or _now()
            prog["next_index"] = int(prog.get("next_index", 0))
            prog["chunks_done"] = int(prog.get("chunks_done", 0))
            self._bump()
            self._record("layout.start", version_id=version_id, layout=layout,
                         budget=lay["budget_bytes"], chunk=lay["chunk"],
                         resume_from=prog["next_index"], total=prog["total"],
                         restart=restart or lay["state"] == "idle")
            self._save()
            token = lay["token"]
            result = self._layout_view(lay)
        thread = threading.Thread(target=self._work, args=(version_id, layout, token),
                                  daemon=True, name=f"convert-{version_id}-{layout}")
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()
        return {"started": True, "layout": result}

    def pause_conversion(self, version_id: int, layout: str) -> dict:
        lock = self._io_lock(version_id, layout)
        with lock:
            with self._lock:
                lay = self.state["layouts"].get(version_id, {}).get(layout)
                if not lay:
                    raise UnknownLabel(f"数据版 #{version_id} 没有 {layout} 布局")
                if lay["state"] != "building":
                    return {"paused": False, "layout": self._layout_view(lay)}
                lay["state"] = "paused"
                lay["token"] = None
                lay["pause_reason"] = "手动暂停"
                prog = lay.get("progress") or {}
                prog["interrupted"] = True
                prog["updated_at"] = _now()
                self._bump()
                self._record("layout.pause", version_id=version_id, layout=layout,
                             next_index=prog.get("next_index"))
                self._save()
                return {"paused": True, "layout": self._layout_view(lay)}

    def resume_conversion(self, version_id: int, layout: str) -> dict:
        with self._lock:
            lay = self.state["layouts"].get(version_id, {}).get(layout)
            if not lay:
                raise UnknownLabel(f"数据版 #{version_id} 没有 {layout} 布局")
            if lay["state"] == "ready":
                return {"resumed": False, "layout": self._layout_view(lay)}
            if lay["state"] != "paused":
                raise Conflict(f"布局当前为 {lay['state']}，不是暂停状态")
        started = self.start_conversion(version_id, layout,
                                        budget_bytes=lay.get("budget_bytes", 4096),
                                        pace_ms=lay.get("pace_ms", 0))
        started["resumed"] = bool(started.get("started"))
        return started

    # -- ordering / counts -------------------------------------------------
    def _physical_order(self, version_id: int, layout: str) -> list[tuple[int, int]]:
        v = self.state["versions"][version_id]
        key = (version_id, layout, v.get("logical_rev", 0))
        cached = self._orders.get(key)
        if cached is not None:
            return cached
        if layout == "csr":
            order = sorted(v["elements"])
        else:
            order = sorted(v["elements"], key=lambda rc: (rc[1], rc[0]))
        if len(self._orders) > 8:
            self._orders.clear()
        self._orders[key] = order
        return order

    def _recover_arrays(self, lay: dict, version_id: int) -> int:
        """Make stored arrays consistent with progress after a crash."""
        files = LayoutFiles(lay["dir"])
        prog = lay.get("progress") or {}
        next_index = int(prog.get("next_index", 0))
        lengths = {name: files.count(name) for name in ("majors", "indices", "kinds", "data")}
        # If the process died after appending but before saving progress, the
        # arrays are longer than next_index; drop the unacknowledged tail.
        if any(n > next_index for n in lengths.values()):
            for name in lengths:
                files.truncate_to(name, min(lengths[name], next_index))
            lay.setdefault("recovery", {})["truncated_tail"] = True
        elif any(n < next_index for n in lengths.values()):
            # Progress claims more than exists: trust the arrays.
            next_index = min(lengths.values())
            prog["next_index"] = next_index
            lay["nnz"] = next_index
        return next_index

    def _counts_from_majors(self, files: LayoutFiles, upto: int, n_major: int) -> list[int]:
        counts = [0] * n_major
        for start in range(0, upto, VERIFY_SLICE):
            majors = files.read_slice("majors", start, min(VERIFY_SLICE, upto - start))
            for m in majors:
                if 0 <= m < n_major:
                    counts[m] += 1
                else:
                    raise MatrixError("存储的主轴索引超出矩阵维度")
        return counts

    def _work(self, version_id: int, layout: str, token: str) -> None:
        lock = self._io_lock(version_id, layout)
        try:
            while True:
                if self._stopping.is_set():
                    return
                with lock:
                    with self._lock:
                        lay = self.state["layouts"].get(version_id, {}).get(layout)
                        if not lay or lay["state"] != "building" or lay["token"] != token:
                            return
                        v = self.state["versions"][version_id]
                        if not self._layout_is_current(lay, version_id):
                            lay["state"] = "stale"
                            lay["stale_reason"] = "logical content or shape changed during conversion"
                            lay["token"] = None
                            self._save()
                            return
                        files = LayoutFiles(lay["dir"])
                        prog = lay["progress"]
                        order = self._physical_order(version_id, layout)
                        total = len(order)
                        prog["total"] = total
                        start = self._recover_arrays(lay, version_id)
                        chunk = max(1, int(prog.get("chunk", lay["chunk"])))
                        if start >= total:
                            if start > total:
                                raise MatrixError("转换进度超过元素总数")
                            break
                        end = min(total, start + chunk)
                        batch = order[start:end]
                        shape = lay["shape"]
                        elements = v["elements"]
                        payload_indices = [0] * len(batch)
                        payload_kinds = [KIND_VALUE] * len(batch)
                        payload_data = [0.0] * len(batch)
                        payload_majors = [0] * len(batch)
                        for i, (r, c) in enumerate(batch):
                            el = elements[(r, c)]
                            major, minor = (r, c) if layout == "csr" else (c, r)
                            payload_majors[i] = major
                            payload_indices[i] = minor
                            payload_kinds[i] = el["kind"]
                            if el["kind"] == KIND_VALUE:
                                payload_data[i] = float(el["value"])
                            elif el["kind"] == KIND_ZERO:
                                payload_data[i] = 0.0
                            else:
                                payload_data[i] = math.nan
                    # Binary append happens outside the state lock, holding the
                    # per-layout IO lock so tail writes can never interleave.
                    files.append("majors", _pack("majors", payload_majors))
                    files.append("indices", _pack("indices", payload_indices))
                    files.append("kinds", _pack("kinds", payload_kinds))
                    files.append("data", _pack("data", payload_data))
                    with self._lock:
                        lay = self.state["layouts"].get(version_id, {}).get(layout)
                        # Arrays are already longer than progress; record it so
                        # a crash between these steps is recoverable either way.
                        prog = lay["progress"]
                        prog["next_index"] = end
                        prog["chunks_done"] = int(prog.get("chunks_done", 0)) + 1
                        prog["updated_at"] = _now()
                        lay["nnz"] = end
                        self._save()
                pace = lay.get("pace_ms", 0)
                if pace:
                    time.sleep(pace / 1000.0)
            with lock:
                with self._lock:
                    lay = self.state["layouts"].get(version_id, {}).get(layout)
                    if not lay or lay["state"] != "building" or lay["token"] != token:
                        return
                    self._finalize_locked(version_id, layout)
        except Exception as exc:  # defensive: thread boundary
            with self._lock:
                lay = self.state["layouts"].get(version_id, {}).get(layout)
                if lay and lay.get("token") == token:
                    lay["state"] = "failed"
                    lay["error"] = f"conversion error: {exc}"
                    lay["token"] = None
                    self._save()

    def _finalize_locked(self, version_id: int, layout: str) -> None:
        lay = self.state["layouts"][version_id][layout]
        files = LayoutFiles(lay["dir"])
        n_major = lay["shape"][0] if layout == "csr" else lay["shape"][1]
        nnz = files.count("indices")
        if files.count("majors") != nnz or files.count("kinds") != nnz or files.count("data") != nnz:
            lay["state"] = "failed"
            lay["error"] = "array lengths disagree after conversion"
            lay["token"] = None
            self._save()
            return
        # Build indptr by streaming the major indices; memory stays bounded.
        indptr_parts: list[int] = []
        running = 0
        counts = self._counts_from_majors(files, nnz, n_major)
        for value in counts:
            running += value
            indptr_parts.append(running)
        indptr = [0] + indptr_parts
        files.write_all("indptr", _pack("indptr", indptr))
        lay["nnz"] = nnz
        report = self._verify_locked(version_id, layout)
        lay["verification"] = report
        if not report["ok"]:
            lay["state"] = "failed"
            lay["error"] = "; ".join(report["errors"][:4])
            lay["token"] = None
            self._record("layout.failed", version_id=version_id, layout=layout,
                         errors=report["errors"][:4])
            self._save()
            return
        lay["state"] = "ready"
        lay["verified_at"] = _now()
        lay["digest"] = report["digest"]
        lay["token"] = None
        lay["error"] = None
        lay["nnz"] = report["nnz"]
        lay["progress"]["next_index"] = report["nnz"]
        lay["progress"]["updated_at"] = _now()
        files.delete_bookkeeping()
        self._array_cache.clear()
        self._bump()
        self._record("layout.publish", version_id=version_id, layout=layout,
                     nnz=report["nnz"], shape=report["shape"], digest=report["digest"][:16])
        self._save()

    # -- verification ------------------------------------------------------
    def verify_layout(self, version_id: int, layout: str) -> dict:
        with self._lock:
            lay = self.state["layouts"].get(version_id, {}).get(layout)
            if not lay:
                raise UnknownLabel(f"数据版 #{version_id} 没有 {layout} 布局")
            if lay["state"] == "stale":
                raise LayoutNotReady(f"布局已失效：{lay.get('stale_reason')}；请重新转换")
            if lay["state"] not in ("ready", "failed"):
                raise LayoutNotReady(f"布局当前为 {lay['state']}；请先完成转换与发布")
            if not self._layout_is_current(lay, version_id):
                lay["state"] = "stale"
                lay["stale_reason"] = "logical content changed"
                self._save()
                raise LayoutNotReady("布局已失效，请重新转换后再核对")
            report = self._verify_locked(version_id, layout)
            lay["verification"] = report
            if report["ok"]:
                lay["state"] = "ready"
                lay["digest"] = report["digest"]
                lay["verified_at"] = _now()
                lay["error"] = None
            else:
                lay["state"] = "failed"
                lay["error"] = "; ".join(report["errors"][:4])
            self._array_cache.clear()
            self._record("layout.verify", version_id=version_id, layout=layout, ok=report["ok"])
            self._save()
            return report

    def _verify_locked(self, version_id: int, layout: str) -> dict:
        lay = self.state["layouts"][version_id][layout]
        v = self.state["versions"][version_id]
        files = LayoutFiles(lay["dir"])
        if layout == "csr":
            n_major, n_minor = lay["shape"][0], lay["shape"][1]
        else:
            n_major, n_minor = lay["shape"][1], lay["shape"][0]
        errors: list[str] = []
        checks: dict[str, bool] = {}

        def check(name: str, ok: bool, message: str) -> None:
            checks[name] = ok
            if not ok and len(errors) < 20:
                errors.append(message)

        nnz = files.count("indices")
        check("arrays_aligned",
              files.count("kinds") == nnz and files.count("data") == nnz
              and files.count("indptr") == n_major + 1,
              "indptr / indices / kinds / data 的长度与预期不符")
        indptr = files.read_all("indptr")
        check("indptr_length", len(indptr) == n_major + 1,
              f"indptr 有 {len(indptr)} 项，预期 {n_major + 1} 项")
        check("indptr_zero_base", bool(indptr) and indptr[0] == 0, "indptr[0] 不是 0")
        check("indptr_final_nnz", bool(indptr) and indptr[-1] == nnz,
              f"indptr[-1] 与 nnz（{nnz}）不相等")
        monotonic = all(indptr[i] <= indptr[i + 1] for i in range(len(indptr) - 1)) if indptr else False
        check("indptr_monotonic", monotonic, "indptr 偏移不是单调不减")

        if not errors:
            logical = v["elements"]
            seen: set[tuple[int, int]] = set()
            duplicate = None
            mismatch = None
            out_of_range = None
            bad_kind = None
            bad_value = None
            unordered = None
            extra_slots = 0
            # Walk one major block at a time in bounded slices.  indptr already
            # passed the monotonicity check, so blocks partition all slots.
            for major in range(n_major):
                block_start = indptr[major]
                block_end = indptr[major + 1]
                if block_end < block_start or block_end > nnz:
                    break
                previous_minor = -1
                for offset in range(block_start, block_end, VERIFY_SLICE):
                    count = min(VERIFY_SLICE, block_end - offset)
                    indices = files.read_slice("indices", offset, count)
                    kinds = files.read_kinds(offset, count)
                    data = files.read_slice("data", offset, count)
                    if not (len(indices) == len(kinds) == len(data) == count):
                        mismatch = "存储数组比 indptr 声明的更短"
                        break
                    for i in range(count):
                        idx = indices[i]
                        kind = kinds[i]
                        if not (0 <= idx < n_minor) and out_of_range is None:
                            out_of_range = (major, idx)
                        if kind not in KINDS:
                            if bad_kind is None:
                                bad_kind = (major, idx)
                        elif kind == KIND_VALUE:
                            if data[i] is None or not math.isfinite(data[i]):
                                if bad_value is None:
                                    bad_value = (major, idx)
                        elif kind == KIND_ZERO:
                            if data[i] is None or data[i] != 0.0:
                                if bad_value is None:
                                    bad_value = (major, idx)
                        elif data[i] is None or not math.isnan(data[i]):
                            if bad_value is None:
                                bad_value = (major, idx)
                        if idx <= previous_minor and unordered is None:
                            unordered = (major, idx)
                        previous_minor = idx
                        coord = (major, idx) if layout == "csr" else (idx, major)
                        if coord in seen and duplicate is None:
                            duplicate = coord
                        seen.add(coord)
                        el = logical.get(coord)
                        if el is None:
                            if mismatch is None:
                                mismatch = f"存储坐标 {coord} 不在逻辑三元组中"
                        elif el["kind"] != kind:
                            if mismatch is None:
                                mismatch = f"坐标 {coord} 类型不一致：存储 {kind}，逻辑 {el['kind']}"
                        elif kind == KIND_VALUE:
                            logical_value = float(el["value"])
                            if abs(float(data[i]) - logical_value) > 1e-12 * max(1.0, abs(logical_value)):
                                if mismatch is None:
                                    mismatch = (f"坐标 {coord} 数值不一致："
                                                f"{data[i]} vs {logical_value}")
                    if out_of_range or bad_kind or bad_value or duplicate or mismatch or unordered:
                        break
                if out_of_range or bad_kind or bad_value or duplicate or mismatch or unordered:
                    break
            check("indices_in_range", out_of_range is None,
                  f"槽位 {out_of_range} 的索引超出 [0, {n_minor})" if out_of_range else "")
            check("kind_codes_valid", bad_kind is None,
                  f"{bad_kind} 处的类型码未知" if bad_kind else "")
            check("kinds_values_consistent", bad_value is None,
                  f"{bad_value} 处的数值与类型不一致" if bad_value else "")
            check("coordinates_unique", duplicate is None,
                  f"坐标 {duplicate} 出现了两次" if duplicate else "")
            check("coordinates_match_logical",
                  mismatch is None or "not in the logical" not in mismatch, mismatch or "")
            check("values_match_logical", mismatch is None, mismatch or "")
            check("canonical_order", unordered is None,
                  f"槽位顺序在 {unordered} 处不再规范" if unordered else "")
            if not errors:
                check("coordinates_complete", len(seen) == len(logical),
                      f"{len(logical) - len(seen)} 个逻辑坐标没有存储槽位")
                check("counts_match", nnz == len(logical),
                      f"存储条目 {nnz} 与逻辑坐标数 {len(logical)} 不一致")
        else:
            seen = set()
            for name in ("indices_in_range", "kind_codes_valid", "kinds_values_consistent",
                         "coordinates_unique", "coordinates_match_logical",
                         "values_match_logical", "canonical_order", "coordinates_complete",
                         "counts_match"):
                checks[name] = False

        digest = ""
        if not errors:
            digest = files.digest()
        return {
            "ok": not errors,
            "errors": errors,
            "checks": checks,
            "nnz": nnz,
            "logical_count": len(v["elements"]),
            "shape": [n_major, n_minor],
            "digest": digest,
            "checked_at": _now(),
            "coordinate_checks": len(seen) if not errors else 0,
        }

    # -- reading -----------------------------------------------------------
    def _require_ready(self, version_id: int, layout: str) -> dict:
        v = self.state["versions"].get(version_id)
        if not v:
            raise UnknownLabel(f"没有数据版 #{version_id}")
        lay = self.state["layouts"].get(version_id, {}).get(layout)
        if not lay:
            raise LayoutNotReady(f"{layout.upper()} 布局尚未构建")
        if lay["state"] != "ready":
            raise LayoutNotReady(
                f"{layout.upper()} 布局当前为 {lay['state']}；未完成验证的布局不能参与读取")
        if not self._layout_is_current(lay, version_id):
            lay["state"] = "stale"
            lay["stale_reason"] = "logical content or shape changed"
            self._save()
            raise LayoutNotReady("布局已失效（逻辑数据或维度已变化），请重新转换并核对后再读取")
        return lay

    def _arrays(self, version_id: int, layout: str) -> dict:
        lay = self._require_ready(version_id, layout)
        key = (version_id, layout, lay.get("version_rev"), lay.get("shape_rev"),
               lay.get("digest"))
        cached = self._array_cache.get(key)
        if cached is not None:
            return cached
        files = LayoutFiles(lay["dir"])
        arrays = {
            "indptr": files.read_all("indptr"),
            "indices": files.read_all("indices"),
            "kinds": files.read_kinds(0, files.count("kinds")),
            "data": files.read_all("data"),
            "layout": layout,
            "shape": list(lay["shape"]),
            "digest": lay.get("digest"),
        }
        if len(arrays["indptr"]) != lay["shape"][0] + 1 and layout == "csr":
            raise MatrixError("存储的 indptr 与记录的形状不一致")
        if len(arrays["indptr"]) != lay["shape"][1] + 1 and layout == "csc":
            raise MatrixError("存储的 indptr 与记录的形状不一致")
        if not (len(arrays["indices"]) == len(arrays["kinds"]) == len(arrays["data"])):
            raise MatrixError("存储数组长度不一致")
        if len(self._array_cache) > 6:
            self._array_cache.clear()
        self._array_cache[key] = arrays
        return arrays

    def _filter_sets(self, row_names, col_names, row_positions, col_positions):
        row_labels = self._labels("row")
        col_labels = self._labels("col")
        row_ids = None
        col_ids = None
        if row_names:
            row_ids = {self._resolve(row_labels, n)["id"] for n in row_names}
        if col_names:
            col_ids = {self._resolve(col_labels, n)["id"] for n in col_names}
        if row_positions:
            lo, hi = row_positions
            ids = {lid for lid, l in row_labels.items() if lo <= l["position"] <= hi}
            row_ids = ids if row_ids is None else (row_ids & ids)
        if col_positions:
            lo, hi = col_positions
            ids = {lid for lid, l in col_labels.items() if lo <= l["position"] <= hi}
            col_ids = ids if col_ids is None else (col_ids & ids)
        return row_ids, col_ids

    def query(self, version_id: int, layout: str, *,
              row_names: list[str] | None = None,
              col_names: list[str] | None = None,
              row_positions: tuple[int, int] | None = None,
              col_positions: tuple[int, int] | None = None,
              kinds: set[str] | None = None,
              limit: int = 5000) -> dict:
        with self._lock:
            arrays = self._arrays(version_id, layout)
            lay = self._require_ready(version_id, layout)
            row_ids, col_ids = self._filter_sets(row_names, col_names,
                                                 row_positions, col_positions)
            label_rows = self._labels("row")
            label_cols = self._labels("col")
            indptr, indices = arrays["indptr"], arrays["indices"]
            kinds_arr, data = arrays["kinds"], arrays["data"]
            n_major = lay["shape"][0] if layout == "csr" else lay["shape"][1]
            if len(indptr) != n_major + 1:
                raise MatrixError("存储的 indptr 与记录的形状不一致")
            matches = []
            total = 0
            scanned = 0
            for major in range(n_major):
                if layout == "csr":
                    if row_ids is not None and major not in row_ids:
                        continue
                else:
                    if col_ids is not None and major not in col_ids:
                        continue
                start, end = indptr[major], indptr[major + 1]
                scanned += end - start
                for slot in range(start, end):
                    minor = indices[slot]
                    if layout == "csr":
                        r, c = major, minor
                        if col_ids is not None and c not in col_ids:
                            continue
                    else:
                        r, c = minor, major
                        if row_ids is not None and r not in row_ids:
                            continue
                    if kinds is not None and kinds_arr[slot] not in kinds:
                        continue
                    total += 1
                    if len(matches) < limit:
                        matches.append({
                            "row": label_rows[r]["name"],
                            "col": label_cols[c]["name"],
                            "row_id": r,
                            "col_id": c,
                            "kind": kinds_arr[slot],
                            "value": data[slot] if kinds_arr[slot] == KIND_VALUE else None,
                            "slot": slot,
                        })
            return {
                "version_id": version_id,
                "layout": layout,
                "shape": lay["shape"],
                "indptr": indptr,
                "matches": matches,
                "returned": len(matches),
                "total_matches": total,
                "slots_scanned": scanned,
                "nnz_total": len(indices),
                "digest": lay["digest"],
            }

    def point(self, version_id: int, layout: str, row: str, col: str) -> dict:
        with self._lock:
            arrays = self._arrays(version_id, layout)
            row_lab = self._resolve(self._labels("row"), row)
            col_lab = self._resolve(self._labels("col"), col)
            major, minor = ((row_lab["id"], col_lab["id"]) if layout == "csr"
                            else (col_lab["id"], row_lab["id"]))
            indptr, indices = arrays["indptr"], arrays["indices"]
            if major + 1 >= len(indptr):
                return {"present": False, "row": row, "col": col, "kind": None,
                        "value": None, "layout": layout,
                        "note": "coordinate outside the stored shape"}
            start, end = indptr[major], indptr[major + 1]
            pos = bisect.bisect_left(indices, minor, start, end)
            if pos < end and indices[pos] == minor:
                kind = arrays["kinds"][pos]
                return {"present": True, "row": row, "col": col, "slot": pos,
                        "kind": kind,
                        "value": arrays["data"][pos] if kind == KIND_VALUE else None,
                        "layout": layout}
            return {"present": False, "row": row, "col": col, "kind": None,
                    "value": None, "layout": layout,
                    "note": "coordinate has no element (distinct from explicit zero/missing)"}

    def arrays_preview(self, version_id: int, layout: str, limit: int = 200) -> dict:
        with self._lock:
            arrays = self._arrays(version_id, layout)
            lay = self._require_ready(version_id, layout)
            limit = max(0, int(limit))
            indptr, indices = arrays["indptr"], arrays["indices"]
            n = min(len(indices), limit)
            return {
                "version_id": version_id,
                "layout": layout,
                "shape": lay["shape"],
                "nnz": len(indices),
                "indptr": indptr[:limit],
                "indptr_len": len(indptr),
                "indices": indices[:n],
                "kinds": arrays["kinds"][:n],
                "data": arrays["data"][:n],
                "truncated": n < len(indices),
                "arrays_bytes": LayoutFiles(lay["dir"]).bytes(),
                "digest": lay["digest"],
                "verification": lay.get("verification"),
            }

    def compare(self, version_id: int, layout: str) -> dict:
        with self._lock:
            arrays = self._arrays(version_id, layout)
            lay = self._require_ready(version_id, layout)
            logical = self.logical_triples(version_id)
            logical_map = {(t["row_id"], t["col_id"]): t for t in logical}
            stored: dict[tuple[int, int], dict] = {}
            duplicates = []
            indptr, indices = arrays["indptr"], arrays["indices"]
            major = 0
            for slot in range(len(indices)):
                while major + 1 < len(indptr) and slot >= indptr[major + 1]:
                    major += 1
                coord = (major, indices[slot]) if layout == "csr" else (indices[slot], major)
                if coord in stored:
                    duplicates.append(coord)
                stored[coord] = {"kind": arrays["kinds"][slot], "value": arrays["data"][slot],
                                 "slot": slot}
            missing = [k for k in logical_map if k not in stored]
            extra = [k for k in stored if k not in logical_map]
            kind_mismatch = []
            value_mismatch = []
            for coord, el in stored.items():
                log = logical_map.get(coord)
                if not log:
                    continue
                if el["kind"] != log["kind"]:
                    kind_mismatch.append({"coord": coord, "stored": el["kind"],
                                          "logical": log["kind"]})
                elif el["kind"] == KIND_VALUE:
                    a, b = float(el["value"]), float(log["value"])
                    if abs(a - b) > 1e-12 * max(1.0, abs(b)):
                        value_mismatch.append({"coord": coord, "stored": a, "logical": b})
            row_labels = self._labels("row")
            col_labels = self._labels("col")

            def label_name(coord):
                r, c = coord
                return {"row": row_labels[r]["name"], "col": col_labels[c]["name"]}

            return {
                "version_id": version_id,
                "layout": layout,
                "all_match": not (duplicates or missing or extra
                                  or kind_mismatch or value_mismatch),
                "logical_count": len(logical_map),
                "stored_count": len(stored),
                "counter": {
                    "duplicates": len(duplicates),
                    "missing_in_layout": len(missing),
                    "extra_in_layout": len(extra),
                    "kind_mismatches": len(kind_mismatch),
                    "value_mismatches": len(value_mismatch),
                },
                "missing_in_layout": [{**label_name(k), "row_id": k[0], "col_id": k[1]}
                                      for k in missing[:100]],
                "extra_in_layout": [{**label_name(k), "row_id": k[0], "col_id": k[1]}
                                    for k in extra[:100]],
                "kind_mismatches": kind_mismatch[:100],
                "value_mismatches": value_mismatch[:100],
                "verification": lay.get("verification"),
                "logical": logical[:1000],
                "stored": [
                    {"row_id": r, "col_id": c, "kind": e["kind"],
                     "value": e["value"] if e["kind"] == KIND_VALUE else None}
                    for (r, c), e in sorted(stored.items())
                ][:1000],
            }

    # -- corrections -------------------------------------------------------
    def correct_element(self, version_id: int, row: str, col: str, *,
                        kind: str, value: float | None = None) -> dict:
        with self._lock:
            v = self.state["versions"].get(version_id)
            if not v:
                raise UnknownLabel(f"没有数据版 #{version_id}")
            kind, value = parse_kind_value(kind, value)
            shape_before = (len(self._labels("row")), len(self._labels("col")))
            r, _ = self._ensure_label("row", row)
            c, _ = self._ensure_label("col", col)
            shape_after = (len(self._labels("row")), len(self._labels("col")))
            key = (r["id"], c["id"])
            old = v["elements"].get(key)
            v["elements"][key] = {
                "row": r["id"], "col": c["id"], "kind": kind, "value": value,
                "source_id": None,
                "source_version": (old["source_version"] + 1) if old else 0,
                "import_order": (old["import_order"] + 1) if old else 10 ** 9,
                "updated_at": _now(), "corrected": True,
            }
            v["logical_rev"] = v.get("logical_rev", 0) + 1
            invalidated = self._invalidate_version_layouts(version_id, "element corrected")
            if shape_before != shape_after:
                invalidated += self._mark_stale_layouts(
                    "correction added a label and changed the matrix shape")
            self._bump()
            self._record("element.correct", version_id=version_id, row=row, col=col,
                         kind=kind, value=value, invalidated=invalidated)
            self._save()
            return {"element": {"row": row, "col": col, "kind": kind, "value": value},
                    "invalidated_layouts": invalidated,
                    "shape_changed": shape_before != shape_after}

    # -- export ------------------------------------------------------------
    def build_container(self, version_id: int, layout: str) -> tuple[bytes, dict]:
        with self._lock:
            arrays = self._arrays(version_id, layout)
            lay = self._require_ready(version_id, layout)
            v = self.state["versions"][version_id]
            rows = self.labels("row")
            cols = self.labels("col")
            indptr = arrays["indptr"]
            indices = arrays["indices"]
            kind_bytes = bytes(KIND_CODE[k] for k in arrays["kinds"])
            data_bytes = _pack("data", [float_or_nan(val) for val in arrays["data"]])
            indptr_bytes = _pack("indptr", indptr)
            indices_bytes = _pack("indices", indices)
            sections = [
                ("indptr", "int64", "little", len(indptr_bytes)),
                ("indices", "int64", "little", len(indices_bytes)),
                ("kinds", "uint8", "not-applicable", len(kind_bytes)),
                ("data", "float64", "little", len(data_bytes)),
            ]
            payload = indptr_bytes + indices_bytes + kind_bytes + data_bytes
            header = {
                "magic": MAGIC.decode(),
                "format_version": FORMAT_VERSION,
                "matrix_id": v["matrix_id"],
                "version_id": version_id,
                "version_label": v["label"],
                "duplicate_policy": v["policy"],
                "layout": layout,
                "shape": lay["shape"],
                "nnz": len(indices),
                "row_labels": [{"id": l["id"], "name": l["name"], "position": l["position"]}
                               for l in rows],
                "col_labels": [{"id": l["id"], "name": l["name"], "position": l["position"]}
                               for l in cols],
                "sections": sections,
                "kind_codes": KIND_CODE,
                "zero_policy": "explicit zero: kind=zero, data=0.0",
                "missing_policy": "explicit missing: kind=missing, data=NaN",
                "absent_policy": "no element: no slot at all",
                "verification": lay.get("verification"),
                "digest": lay.get("digest"),
                "created_at": _now(),
            }
            header_bytes = _json_dump(header)
            prefix = MAGIC + struct.pack("<II", FORMAT_VERSION, len(header_bytes)) + header_bytes
            blob = prefix + payload
            blob += hashlib.sha256(blob).hexdigest().encode("ascii")
            return blob, header

    def export(self, version_id: int, layout: str) -> dict:
        blob, header = self.build_container(version_id, layout)
        with self._lock:
            name = f"v{version_id}-{header['matrix_id'][:8]}-{layout}.sparse"
            path = os.path.join(self.export_dir, name)
            _atomic_write(path, blob)
            digest = hashlib.sha256(blob).hexdigest()
            entry = {
                "name": name,
                "version_id": version_id,
                "layout": layout,
                "bytes": len(blob),
                "sha256": digest,
                "created_at": _now(),
                "nnz": header["nnz"],
                "shape": header["shape"],
            }
            self.state["exports"] = [e for e in self.state["exports"] if e["name"] != name]
            self.state["exports"].append(entry)
            self._record("layout.export", version_id=version_id, layout=layout,
                         name=name, bytes=len(blob), sha256=digest[:16])
            self._save()
            return entry

    def exports(self) -> list[dict]:
        with self._lock:
            return sorted(self.state["exports"], key=lambda e: e["created_at"], reverse=True)

    def export_manifest(self) -> dict:
        with self._lock:
            files = []
            for name in sorted(os.listdir(self.export_dir)):
                path = os.path.join(self.export_dir, name)
                if os.path.isfile(path):
                    files.append({"name": name, "bytes": os.path.getsize(path),
                                  "sha256": _sha256_file(path)})
            return {"generated_at": _now(), "files": files}

    # -- aggregate ---------------------------------------------------------
    def summary(self) -> dict:
        with self._lock:
            versions = self.list_versions()
            sources = sorted(self.state["sources"].values(), key=lambda s: s["id"])
            return {
                "matrix_id": versions[0]["matrix_id"] if versions else None,
                "revision": self.state["revision"],
                "shape_rev": self.state["shape_rev"],
                "shape": [len(self._labels("row")), len(self._labels("col"))],
                "labels": {"row": self.labels("row"), "col": self.labels("col")},
                "sources": sources,
                "versions": versions,
                "exports": self.exports(),
                "audit": list(reversed(self.state["audit"][-120:])),
                "stats": {
                    "versions": len(versions),
                    "elements": sum(v["element_count"] for v in versions),
                    "sources": sum(1 for s in sources if not s["deleted"]),
                    "layouts_ready": sum(len(v["readable_layouts"]) for v in versions),
                },
            }

    # -- jobs --------------------------------------------------------------
    def _recover_interrupted_jobs(self) -> None:
        for layouts in self.state["layouts"].values():
            for lay in layouts.values():
                if lay.get("state") == "paused" and lay.get("pause_reason") == "进程重启":
                    self._record("layout.recovered_as_paused",
                                 version_id=lay["version_id"], layout=lay["layout"],
                                 next_index=(lay.get("progress") or {}).get("next_index"))

    def wait_for_layout(self, version_id: int, layout: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                lay = self.state["layouts"].get(version_id, {}).get(layout)
                if not lay or lay["state"] != "building":
                    return self._layout_view(lay) if lay else {}
            time.sleep(0.01)
        with self._lock:
            lay = self.state["layouts"].get(version_id, {}).get(layout)
            return self._layout_view(lay) if lay else {}

    def close(self, *, crash: bool = False, timeout: float = 5.0) -> None:
        """Stop background writers.

        ``crash=True`` leaves the on-disk state mid-conversion (the layout is
        still recorded as building), which is what a process kill looks like.
        """
        self._stopping.set()
        for thread in list(self._threads):
            thread.join(timeout=timeout)
        if crash:
            return
        with self._lock:
            for layouts in self.state["layouts"].values():
                for lay in layouts.values():
                    if lay["state"] == "building":
                        lay["state"] = "paused"
                        lay["token"] = None
                        lay["pause_reason"] = "服务关闭"
                        if lay.get("progress"):
                            lay["progress"]["interrupted"] = True
            self._save()

    def clear(self) -> None:
        with self._lock:
            for vid, layouts in self.state["layouts"].items():
                for lay in layouts.values():
                    files = LayoutFiles(lay["dir"])
                    files.reset()
            for name in os.listdir(self.export_dir):
                path = os.path.join(self.export_dir, name)
                if os.path.isfile(path):
                    os.unlink(path)
            self.state = self._empty_state()
            self._orders.clear()
            self._array_cache.clear()
            self._save()


# ---------------------------------------------------------------------------
# container reader
# ---------------------------------------------------------------------------


def read_container(blob: bytes) -> dict[str, Any]:
    """Decode a .sparse file (used by tests / external inspection)."""
    if len(blob) < 16 + 64 or not blob.startswith(MAGIC):
        raise MatrixError("不是有效的 SPMATRIX 存储文件")
    fmt, header_len = struct.unpack("<II", blob[8:16])
    if fmt != FORMAT_VERSION:
        raise MatrixError(f"不支持的存储文件版本 {fmt}")
    body = blob[:-64]
    checksum = blob[-64:].decode("ascii", "replace")
    if hashlib.sha256(body).hexdigest() != checksum:
        raise MatrixError("存储文件校验和不对，文件可能已损坏")
    header = json.loads(blob[16:16 + header_len].decode("utf-8"))
    offset = 16 + header_len
    sections = {s[0]: s for s in header["sections"]}
    out: dict[str, Any] = {"header": header}
    for name in ("indptr", "indices", "kinds", "data"):
        size = sections[name][3]
        raw = body[offset:offset + size]
        offset += size
        if name in ("indptr", "indices"):
            out[name] = list(struct.unpack(f"<{size // 8}q", raw))
        elif name == "kinds":
            out[name] = [CODE_KIND[b] for b in raw]
        else:
            out[name] = list(struct.unpack(f"<{size // 8}d", raw))
    out["payload_bytes"] = len(body)
    return out
