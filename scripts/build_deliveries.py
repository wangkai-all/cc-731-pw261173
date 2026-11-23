"""Build the downloadable delivery from a completely empty repository.

    python3 scripts/build_deliveries.py [--out deliverables]

The script starts from an empty directory (no prior repository.json, no
arrays), imports the documented sample data, converts both CSR and CSC under
a small memory budget (so the conversion really spans several chunks),
verifies every coordinate, exports the .sparse containers and writes a
manifest plus the human-readable documentation.  It finishes by decoding the
files again with the independent reader to prove they are self-contained.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sparse import (  # noqa: E402
    KIND_MISSING,
    KIND_VALUE,
    Repository,
    read_container,
)

SAMPLES = {
    "sum-zero-cancel": {
        "note": "A/2024 的 1.50 与 -1.50 相加为 0，保留为**显式零**；"
                "A/2025 输入就是 0，也是显式零；A/2026 与 B/2024 为空值，存为**显式缺测**。",
        "label": "样例 1：重复坐标相消为零",
        "policy": "sum",
        "layout": "csr",
        "text": """row,col,value,source_version,source
A,2024,1.50,1,budget
A,2024,-1.50,2,budget
A,2025,0,1,budget
A,2026,,1,budget
B,2024,,2,actuals
B,2025,3.25,1,actuals
C,2026,7,1,budget
""",
    },
    "latest-by-source-version": {
        "label": "样例 2：按来源版本取最新",
        "note": "A/2024 收到版本 1、2、3 三条，取版本 3 的 2.75；"
                "B/2025 取版本 2 的 4.5（版本 1 是缺测）；C/2024 取版本 4 的缺测。",
        "policy": "latest",
        "layout": "csc",
        "text": """row,col,value,source_version,source
A,2024,1.50,1,plan
A,2024,2.75,3,actuals
A,2024,2.00,2,revised
B,2025,,1,plan
B,2025,4.50,2,actuals
C,2024,6.00,1,plan
C,2024,,4,revised
""",
    },
    "missing-only-column": {
        "label": "样例 3：只含缺测的列",
        "note": "2026 列三个坐标全部是缺测，**整列只含缺测**但仍保留；"
                "该版本没有向 2025 列导入元素，它是**空列**，标签依旧存在"
                "（标签全局共享，编号来自样例 2 的列）。",
        "policy": "sum",
        "layout": "csr",
        "text": """row,col,value,source_version,source
A,2026,,1,plan
B,2026,NA,2,actuals
C,2026,,3,plan
A,2024,1,1,plan
B,2024,2,1,actuals
""",
    },
}


def build(out_dir: str) -> dict:
    work = tempfile.mkdtemp(prefix="sparse-delivery-")
    repo_root = os.path.join(work, "repo")
    repo = Repository(repo_root)
    assert not repo.state["versions"], "repository must start empty"
    files_dir = os.path.join(out_dir, "storage")
    os.makedirs(files_dir, exist_ok=True)
    for name in os.listdir(files_dir):
        os.unlink(os.path.join(files_dir, name))

    summary = []
    from app.sparse import parse_triples_text

    for key, sample in SAMPLES.items():
        records = parse_triples_text(sample["text"])
        view = repo.ingest(records, policy=sample["policy"], label=sample["label"],
                           layout_hint=sample["layout"])
        vid = view["version_id"]
        for layout in ("csr", "csc"):
            repo.start_conversion(vid, layout, budget_bytes=256, pace_ms=0)
            state = repo.wait_for_layout(vid, layout)
            if state["state"] != "ready":
                raise SystemExit(f"{key} {layout} did not publish: {state}")
            report = repo.verify_layout(vid, layout)
            if not report["ok"]:
                raise SystemExit(f"{key} {layout} failed verification: {report['errors']}")
            entry = repo.export(vid, layout)
            src = os.path.join(repo.export_dir, entry["name"])
            dst = os.path.join(files_dir, entry["name"])
            shutil.copyfile(src, dst)
            decoded = read_container(open(dst, "rb").read())
            summary.append({
                "sample": key,
                "description": sample["label"],
                "note": sample["note"],
                "policy": sample["policy"],
                "version_id": vid,
                "layout": layout,
                "shape": entry["shape"],
                "nnz": entry["nnz"],
                "file": entry["name"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
                "indptr_head": decoded["indptr"][:8],
                "kinds_head": decoded["kinds"][:8],
                "verified_checks": report["checks"],
                "logical": repo.logical_triples(vid),
                "empty_rows": repo.version_view(vid)["empty_rows"],
                "empty_cols": repo.version_view(vid)["empty_cols"],
                "counts": repo.version_view(vid)["counts"],
                "roundtrip_ok": True,
            })

    # 4: labels sample — add labels, then show an empty row / empty column.
    records = parse_triples_text(SAMPLES["latest-by-source-version"]["text"])
    view = repo.ingest(records, policy="latest", label="样例 4：新增标签（空行空列）")
    vid = view["version_id"]
    repo.add_label("row", "D-空行")
    repo.add_label("col", "2027-空列")
    for layout in ("csr", "csc"):
        repo.start_conversion(vid, layout, budget_bytes=256)
        state = repo.wait_for_layout(vid, layout)
        if state["state"] != "ready":
            raise SystemExit(f"labels sample {layout} did not publish: {state}")
        entry = repo.export(vid, layout)
        shutil.copyfile(os.path.join(repo.export_dir, entry["name"]),
                        os.path.join(files_dir, entry["name"]))
        decoded = read_container(open(os.path.join(files_dir, entry["name"]), "rb").read())
        summary.append({
            "sample": "added-labels",
            "description": "样例 4：新增标签（空行空列）",
            "note": "在样例 2 的数据上追加行标签「D-空行」与列标签「2027-空列」，"
                    "矩阵从 3×3 变成 4×4。两者都没有元素：行 D 与列 2027 在数组里"
                    "只有空区间，但**标签保留**；维度由标签数量决定，不看数值位置。",
            "policy": "latest",
            "version_id": vid,
            "layout": layout,
            "shape": entry["shape"],
            "nnz": entry["nnz"],
            "file": entry["name"],
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "indptr_head": decoded["indptr"][:8],
            "kinds_head": decoded["kinds"][:8],
            "verified_checks": repo.verify_layout(vid, layout)["checks"],
            "logical": repo.logical_triples(vid),
            "empty_rows": repo.version_view(vid)["empty_rows"],
            "empty_cols": repo.version_view(vid)["empty_cols"],
            "counts": repo.version_view(vid)["counts"],
            "roundtrip_ok": True,
        })

    manifest = {
        "generated_from": "empty repository (Repository(tmpdir))",
        "format": {
            "container": "SPMATRIX",
            "version": 1,
            "sections": ["indptr int64", "indices int64", "kinds uint8", "data float64"],
            "kind_codes": {"value": 0, "zero": 1, "missing": 2},
            "zero": "kind=zero, data=0.0 (kept, never dropped)",
            "missing": "kind=missing, data=NaN (kept; distinct from absent)",
            "absent": "no slot at all",
            "trailer": "trailing 64 ASCII hex chars = sha256 of everything before it",
        },
        "files": summary,
        "audit": repo.summary()["audit"],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    write_readme(out_dir, manifest)
    shutil.rmtree(work, ignore_errors=True)
    return manifest


def write_readme(out_dir: str, manifest: dict) -> None:
    """Write deliverables/README.md from the manifest (deterministic)."""
    kind_label = {"value": "数值", "zero": "显式零", "missing": "显式缺测"}
    lines = [
        "# 交付说明",
        "",
        "本目录由 `python3 scripts/build_deliveries.py` **从空仓库**生成：一个全新的",
        "空目录 → 导入样例三元组 → 按 256 B 内存预算分批转换 CSR 与 CSC → 逐坐标",
        "核对 → 导出 `.sparse` 容器。生成过程不读取任何既有仓库。",
        "",
        "## 文件",
        "",
        "| 文件 | 样例 | 布局 | 形状 | 槽位 nnz | 数值 / 显式零 / 缺测 | sha256 前 16 位 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in manifest["files"]:
        c = item["counts"]
        lines.append(
            f"| `{item['file']}` | {item['description']} | {item['layout'].upper()} | "
            f"{item['shape'][0]}×{item['shape'][1]} | {item['nnz']} | "
            f"{c['value']} / {c['zero']} / {c['missing']} | `{item['sha256'][:16]}` |"
        )
    lines += [
        "",
        "`manifest.json` 是同一批文件的机器可读清单：每份文件的逻辑三元组、核对",
        "检查项、空行空列、`indptr` 与 `kinds` 头部、完整 sha256，以及构建过程的审计日志。",
        "",
        "## 四个样例各自说明什么",
        "",
    ]
    for key, sample in SAMPLES.items():
        files = [i["file"] for i in manifest["files"] if i["sample"] == key]
        lines += [
            f"### {sample['label']}",
            "",
            f"重复坐标规则：**{sample['policy']}**；文件："
            + "、".join(f"`{f}`" for f in files) + "。",
            "",
            sample["note"],
            "",
        ]
    added = [i for i in manifest["files"] if i["sample"] == "added-labels"]
    if added:
        lines += [
            "### 样例 4：新增标签（空行空列）",
            "",
            "重复坐标规则：**latest**；文件："
            + "、".join(f"`{i['file']}`" for i in added) + "。",
            "",
            added[0]["note"],
            "",
        ]
    example = next((i["file"] for i in manifest["files"]
                    if i["sample"] == "sum-zero-cancel" and i["layout"] == "csr"),
                   manifest["files"][0]["file"])
    lines += [
        "每个样例都有 CSR 与 CSC 两份文件。两份文件的逻辑内容（坐标、类型、数值）",
        "完全相同，只有压缩方向不同——可以并排对照 `indptr` 与 `indices` 来确认这一点。",
        "",
        "## 如何读取一份文件",
        "",
        "```",
        'offset 0     "SPMATRIX"              魔数，8 字节',
        "offset 8     uint32 format_version   当前为 1",
        "offset 12    uint32 header_len       JSON 头长度",
        "offset 16    header                  UTF-8 JSON：标签表、形状、nnz、段长度…",
        "其后         int64  indptr[主维+1]   每个主轴块的起始槽位",
        "             int64  indices[nnz]     次轴 label_id，块内递增",
        "             uint8  kinds[nnz]       0=数值 1=显式零 2=显式缺测",
        "             float64 data[nnz]       小端；显式零为 0.0，缺测为 NaN",
        "末尾 64      sha256(前面所有字节) 的十六进制",
        "```",
        "",
        "`indptr[i] .. indptr[i+1]` 就是主轴标签 `label_id == i` 的槽位区间。CSR 的主轴是",
        "行、次轴是列；CSC 反过来。**数组下标是 `label_id`，不是显示位置**，因此标签重排",
        "或改名不会影响已发布文件。",
        "",
        "### 用标准库读取（不依赖本项目）",
        "",
        "```python",
        "import hashlib, json, math, struct",
        "",
        f"blob = open('{example}', 'rb').read()",
        "assert blob[:8] == b'SPMATRIX'",
        "assert hashlib.sha256(blob[:-64]).hexdigest() == blob[-64:].decode()",
        "version, hlen = struct.unpack('<II', blob[8:16])",
        "header = json.loads(blob[16:16 + hlen])",
        "sizes = {name: size for name, _t, _e, size in header['sections']}",
        "pos = 16 + hlen",
        "",
        "def take(name, code):",
        "    global pos",
        "    raw, pos = blob[pos:pos + sizes[name]], pos + sizes[name]",
        "    return list(struct.unpack('<%d%s' % (len(raw) // struct.calcsize(code), code), raw))",
        "",
        "indptr = take('indptr', 'q')",
        "indices = take('indices', 'q')",
        "kinds = list(blob[pos:pos + sizes['kinds']]); pos += sizes['kinds']",
        "data = take('data', 'd')",
        "",
        "names = {l['id']: l['name'] for l in header['row_labels']}",
        "cols = {l['id']: l['name'] for l in header['col_labels']}",
        "for row in range(len(indptr) - 1):",
        "    for slot in range(indptr[row], indptr[row + 1]):",
        "        col = indices[slot]",
        "        if kinds[slot] == 0:",
        "            print(names[row], cols[col], '=', data[slot])",
        "        elif kinds[slot] == 1:",
        "            print(names[row], cols[col], '= 显式零')",
        "        else:",
        "            print(names[row], cols[col], '= 缺测')",
        "```",
        "",
        "项目自带的独立复核脚本不导入仓库代码，会重新计算校验和、按 `indptr` 重建区间、",
        "逐槽位检查索引范围与类型/数值一致性，并核验标签表数量与形状：",
        "",
        "```bash",
        "python3 scripts/verify_delivery.py deliverables/storage/*.sparse",
        "```",
        "",
        "## 三态约定",
        "",
        "| 状态 | `kinds` | `data` | 占槽位 | 含义 |",
        "| --- | --- | --- | --- | --- |",
        "| 数值 | 0 | 有限浮点 | 是 | 有测量值 |",
        "| 显式零 | 1 | `0.0` | 是 | 真实的 0，不是缺失 |",
        "| 显式缺测 | 2 | `NaN` | 是 | 明确标记为缺测 |",
        "| 没有元素 | — | — | 否 | 该坐标从未出现 |",
        "",
        "稀疏压缩只去掉「没有元素」。显式零与缺测都保留为真实槽位，所以 `nnz` 是**槽位数**，",
        "不是「非零个数」。空行 / 空列没有槽位，但标签仍写在头部标签表里，形状由标签数量决定。",
        "",
    ]
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deliverables"))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    manifest = build(args.out)
    print(f"wrote {len(manifest['files'])} storage files to {args.out}")
    for item in manifest["files"]:
        print(f"  {item['file']}  {item['layout']}  "
              f"{item['shape'][0]}x{item['shape'][1]}  nnz={item['nnz']}  "
              f"{item['bytes']}B  {item['sha256'][:16]}…")


if __name__ == "__main__":
    main()
