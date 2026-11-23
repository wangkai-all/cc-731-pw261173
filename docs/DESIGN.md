# 存储设计与不变量

## 1. 三层结构

```
逻辑层   (row_label_id, col_label_id) -> {kind, value, source_id, source_version}
            │ 每次导入 / 更正修改逻辑层
            ▼
物理层   CSR 或 CSC 的真实压缩数组（二进制文件，按批追加）
            │ 转换完成 → 全量核对 → 发布
            ▼
容器层   .sparse 下载文件（头 + 数组 + 校验和）
```

逻辑层是**唯一事实来源**。物理层与容器层都是可再生的派生结果，任何不一致都以
逻辑层为准，并通过重新转换修正。

## 2. 标签与坐标

- `label_id` 是每轴独立、单调递增、**永不复用**的整数。行 `#0` 与列 `#0`
  互不相干。
- `position` 是显示顺序。重排只改 `position`，因此已发布数组可以继续使用：
  CS(R|C) 的 `indptr` 下标就是该轴的 `label_id`，不是位置。
- 矩阵形状 = `(行标签数, 列标签数)`。新增标签会改变形状并触发失效；
  重命名与重排不会。
- 坐标只由 `(row_label_id, col_label_id)` 决定。标签改名后坐标不变；
  标签重排后坐标不变；新增其他标签后坐标也不变。

## 3. 元素的三态

| 状态 | `kinds` 字节 | `data` 值 | 是否占槽位 |
| --- | --- | --- | --- |
| 数值 | `0` (value) | 任意有限 float64 | 是 |
| 显式零 | `1` (zero) | `0.0` | **是** |
| 显式缺测 | `2` (missing) | `NaN` | **是** |
| 没有元素 | — | — | 否 |

稀疏压缩只去掉「没有元素」，从不删除显式零或缺测。因此 `nnz` 表示
**存储槽位数**，不是「非零个数」。

## 4. 重复坐标的两条规则

输入允许同一坐标出现多次，导入时二选一：

- **sum**：数值相加；`missing` 视为「没有数据」而不是 0；两项相消为 `0.0` 时
  记作**显式零**（不是删除），并写入版本提示。
- **latest**：比较 `(source_version, 输入顺序)`，取最大者；来源版本号可逐行给出。

同一份三元组用两种规则导入会得到两个数据版，编号、布局、导出文件都独立。

## 5. CSR / CSC 数组

CSR（按行压缩）：

```
indptr[i] .. indptr[i+1]   -> 第 i 行（label_id = i）的槽位区间
indices[s]                 -> 该槽位的列 label_id
kinds[s] / data[s]         -> 类型与数值
```

CSC 对称：主轴是列，`indices` 存行 `label_id`。

不变量（发布前逐项核对）：

1. `indptr` 长度为 `主维 + 1`，`indptr[0] == 0`，`indptr[-1] == nnz`；
2. `indptr` 单调不减；
3. `0 <= indices[s] < 次维`；
4. `indices / kinds / data` 长度一致；
5. `kind == value` 时 `data` 有限；`kind == zero` 时 `data == 0.0`；
   `kind == missing` 时 `data` 为 NaN；
6. 每个坐标只出现一次；
7. 物理顺序在每个主轴块内按次轴 `label_id` 递增（规范顺序）；
8. 存储坐标集合与逻辑坐标集合**完全相等**；
9. 逐坐标比较 `kind` 与 `value`（浮点按相对误差 `1e-12`）；
10. `nnz` 等于逻辑坐标数。

核对按 4096 槽位一片流式进行，内存占用与矩阵规模无关（不复制整份数组）。

## 6. 转换、检查点与中断

- 批大小 `chunk = max(1, budget_bytes // 32)`（每个元素约 32 字节：两个 int64
  加 kind 与 float64 的均摊）。
- 每批追加 `majors`（主轴 `label_id`，仅转换期使用的簿记）、`indices`、
  `kinds`、`data` 四个二进制文件，随后才把 `progress.next_index` 落盘。
- 因此任一瞬间磁盘上可能出现「数组比进度长」。恢复时以 `next_index` 为准，
  截掉未确认的尾部；若数组比进度短（极端崩溃），则以数组长度为准回退进度。
- 进程被杀：`repository.json` 里状态仍是 `building`，下次打开时自动降级为
  `paused`，`pause_reason` 记为「进程重启」。
- 完成最后一批后，用 `majors` 流式累计出 `indptr` 写入 `indptr.bin`，
  删除 `majors` / `counts` 簿记文件，然后执行 §5 的全量核对。
- 只有核对通过才会把状态置为 `ready`。读取路径对非 `ready` 一律抛错；
  另有一条基于 `(logical_rev, shape_rev)` 的指纹，逻辑或维度一变就拒绝读取。

## 7. `.sparse` 容器

```
offset 0    "SPMATRIX"                 8 bytes
offset 8    uint32 format_version = 1
offset 12   uint32 header_len
offset 16   header (UTF-8 JSON)
            int64  shape[2]
            int64  indptr [master+1]
            int64  indices[nnz]
            uint8  kinds[nnz]     0=value 1=zero 2=missing
            float64 data[nnz]     little-endian
末尾 64     sha256(前面所有字节) 的 ASCII 十六进制
```

头部包含：`matrix_id`、`version_id`、重复坐标规则、布局名、形状、nnz、
行列标签表（`id / name / position`）、各段偏移与元素大小、kind 编码、
核对报告与数组摘要。文件自包含：只凭它就能还原标签、坐标、三态与数值。

## 8. 失效传播

| 操作 | 已发布布局 | 原因 |
| --- | --- | --- |
| 重排标签 | **保持可用** | 数组按 `label_id` 索引，位置与编号解耦 |
| 重命名标签 | **保持可用** | 数组只存编号 |
| 新增标签 | 失效 | 形状变化，`indptr` 长度必须改变 |
| 导入含新标签 | 失效 | 同上 |
| 更正元素 | 该版本失效 | 逻辑值与数组不一致 |
| 删除来源记录 | **保持可用** | 只改来源归属，不改值与坐标 |

失效布局的 `state` 变为 `stale` 并记录原因；读取、对照与导出都会拒绝，
直到重新转换并通过核对。
