"""Independently re-check a downloaded .sparse file.

    python3 scripts/verify_delivery.py deliverables/storage/*.sparse

This script deliberately does not import the repository; it re-implements the
container layout and rebuilds every coordinate from the arrays to show the
file is self-contained.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys

MAGIC = b"SPMATRIX"
KINDS = {0: "value", 1: "zero", 2: "missing"}


def load(path: str) -> dict:
    with open(path, "rb") as fh:
        blob = fh.read()
    if not blob.startswith(MAGIC):
        raise SystemExit(f"{path}: 不是 SPMATRIX 文件")
    version, header_len = struct.unpack("<II", blob[8:16])
    if version != 1:
        raise SystemExit(f"{path}: 不支持的版本 {version}")
    body, trailer = blob[:-64], blob[-64:]
    actual = hashlib.sha256(body).hexdigest()
    if actual != trailer.decode("ascii"):
        raise SystemExit(f"{path}: 校验和不匹配")
    header = json.loads(blob[16:16 + header_len].decode("utf-8"))
    sizes = {name: size for name, _t, _e, size in header["sections"]}
    if sum(sizes.values()) != len(body) - 16 - header_len:
        raise SystemExit(f"{path}: 段长度之和与文件长度不符")
    offset = 16 + header_len
    arrays = {}
    for name in ("indptr", "indices", "kinds", "data"):
        raw = body[offset:offset + sizes[name]]
        offset += sizes[name]
        if name in ("indptr", "indices"):
            arrays[name] = list(struct.unpack(f"<{sizes[name] // 8}q", raw))
        elif name == "kinds":
            arrays[name] = list(raw)
        else:
            arrays[name] = list(struct.unpack(f"<{sizes[name] // 8}d", raw))
    return {"path": path, "header": header, "arrays": arrays, "sha256": actual}


def check(path: str) -> dict:
    doc = load(path)
    header, arrays = doc["header"], doc["arrays"]
    layout = header["layout"]
    rows, cols = header["shape"] if layout == "csr" else header["shape"][::-1]
    master, minor = rows, cols
    indptr, indices = arrays["indptr"], arrays["indices"]
    kinds, data = arrays["kinds"], arrays["data"]
    errors = []

    if len(indptr) != master + 1:
        errors.append(f"indptr 长度 {len(indptr)} != 主维 + 1 ({master + 1})")
    if not indptr or indptr[0] != 0:
        errors.append("indptr[0] != 0")
    if indptr and indptr[-1] != len(indices):
        errors.append(f"indptr[-1]={indptr[-1]} != nnz {len(indices)}")
    if any(indptr[i] > indptr[i + 1] for i in range(len(indptr) - 1)):
        errors.append("indptr 不单调")
    if not (len(kinds) == len(data) == len(indices)):
        errors.append("数组长度不一致")
    if header["nnz"] != len(indices):
        errors.append(f"头部 nnz={header['nnz']} 与数组 {len(indices)} 不符")

    row_names = {l["id"]: l["name"] for l in header["row_labels"]}
    col_names = {l["id"]: l["name"] for l in header["col_labels"]}
    if len(row_names) != header["shape"][0] or len(col_names) != header["shape"][1]:
        errors.append("标签表数量与形状不符")

    coords = {}
    zero = missing = numeric = 0
    for major in range(master):
        start, end = indptr[major], indptr[major + 1]
        previous = -1
        for slot in range(start, end):
            idx = indices[slot]
            if not (0 <= idx < minor):
                errors.append(f"索引越界 {idx}")
                continue
            if idx <= previous:
                errors.append(f"第 {major} 块内索引未递增：{idx} <= {previous}")
            previous = idx
            kind = KINDS.get(kinds[slot], f"code{kinds[slot]}")
            if kind == "value" and not math.isfinite(data[slot]):
                errors.append(f"数值槽位 data 非有限：slot {slot}")
            if kind == "zero" and data[slot] != 0.0:
                errors.append(f"显式零的 data 不是 0.0：slot {slot}")
            if kind == "missing" and not math.isnan(data[slot]):
                errors.append(f"显式缺测的 data 不是 NaN：slot {slot}")
            coord = (major, idx) if layout == "csr" else (idx, major)
            if coord in coords:
                errors.append(f"坐标 {coord} 重复")
            coords[coord] = (kind, data[slot])
            zero += kind == "zero"
            missing += kind == "missing"
            numeric += kind == "value"

    present_rows = {r for r, _ in coords}
    present_cols = {c for _, c in coords}
    empty_rows = [n for i, n in row_names.items() if i not in present_rows]
    empty_cols = [n for i, n in col_names.items() if i not in present_cols]

    return {
        "file": doc["path"],
        "layout": layout,
        "shape": header["shape"],
        "nnz": len(indices),
        "value": numeric,
        "zero": zero,
        "missing": missing,
        "empty_rows": empty_rows,
        "empty_cols": empty_cols,
        "sha256": doc["sha256"],
        "ok": not errors,
        "errors": errors[:10],
    }


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        raise SystemExit(2)
    failed = False
    for path in paths:
        report = check(path)
        status = "通过" if report["ok"] else "失败"
        print(f"{report['file']}\n  布局 {report['layout'].upper()} · "
              f"{report['shape'][0]}×{report['shape'][1]} · nnz {report['nnz']} "
              f"(数值 {report['value']} / 显式零 {report['zero']} / 缺测 {report['missing']})"
              f" · 校验 {status}")
        if report["empty_rows"] or report["empty_cols"]:
            print(f"  空行 {report['empty_rows']} 空列 {report['empty_cols']}")
        for err in report["errors"]:
            print(f"  ! {err}")
        failed = failed or not report["ok"]
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
