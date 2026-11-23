# 交付说明

本目录由 `python3 scripts/build_deliveries.py` **从空仓库**生成：一个全新的
空目录 → 导入样例三元组 → 按 256 B 内存预算分批转换 CSR 与 CSC → 逐坐标
核对 → 导出 `.sparse` 容器。生成过程不读取任何既有仓库。

## 文件

| 文件 | 样例 | 布局 | 形状 | 槽位 nnz | 数值 / 显式零 / 缺测 | sha256 前 16 位 |
| --- | --- | --- | --- | --- | --- | --- |
| `v1-22b99e92-csr.sparse` | 样例 1：重复坐标相消为零 | CSR | 3×3 | 6 | 2 / 2 / 2 | `72eb60a72160b91f` |
| `v1-22b99e92-csc.sparse` | 样例 1：重复坐标相消为零 | CSC | 3×3 | 6 | 2 / 2 / 2 | `50c23a1226d3ff10` |
| `v2-e5d5fd92-csr.sparse` | 样例 2：按来源版本取最新 | CSR | 3×3 | 3 | 2 / 0 / 1 | `1e4f122195fd251d` |
| `v2-e5d5fd92-csc.sparse` | 样例 2：按来源版本取最新 | CSC | 3×3 | 3 | 2 / 0 / 1 | `5a651f584de529c1` |
| `v3-f1fb6e5f-csr.sparse` | 样例 3：只含缺测的列 | CSR | 3×3 | 5 | 2 / 0 / 3 | `be5191a9301c9870` |
| `v3-f1fb6e5f-csc.sparse` | 样例 3：只含缺测的列 | CSC | 3×3 | 5 | 2 / 0 / 3 | `2e74c241e2a42e01` |
| `v4-7f237a33-csr.sparse` | 样例 4：新增标签（空行空列） | CSR | 4×4 | 3 | 2 / 0 / 1 | `2cf01941bad3182f` |
| `v4-7f237a33-csc.sparse` | 样例 4：新增标签（空行空列） | CSC | 4×4 | 3 | 2 / 0 / 1 | `9d93fc34e72f178d` |

`manifest.json` 是同一批文件的机器可读清单：每份文件的逻辑三元组、核对
检查项、空行空列、`indptr` 与 `kinds` 头部、完整 sha256，以及构建过程的审计日志。

## 四个样例各自说明什么

### 样例 1：重复坐标相消为零

重复坐标规则：**sum**；文件：`v1-22b99e92-csr.sparse`、`v1-22b99e92-csc.sparse`。

A/2024 的 1.50 与 -1.50 相加为 0，保留为**显式零**；A/2025 输入就是 0，也是显式零；A/2026 与 B/2024 为空值，存为**显式缺测**。

### 样例 2：按来源版本取最新

重复坐标规则：**latest**；文件：`v2-e5d5fd92-csr.sparse`、`v2-e5d5fd92-csc.sparse`。

A/2024 收到版本 1、2、3 三条，取版本 3 的 2.75；B/2025 取版本 2 的 4.5（版本 1 是缺测）；C/2024 取版本 4 的缺测。

### 样例 3：只含缺测的列

重复坐标规则：**sum**；文件：`v3-f1fb6e5f-csr.sparse`、`v3-f1fb6e5f-csc.sparse`。

2026 列三个坐标全部是缺测，**整列只含缺测**但仍保留；该版本没有向 2025 列导入元素，它是**空列**，标签依旧存在（标签全局共享，编号来自样例 2 的列）。

### 样例 4：新增标签（空行空列）

重复坐标规则：**latest**；文件：`v4-7f237a33-csr.sparse`、`v4-7f237a33-csc.sparse`。

在样例 2 的数据上追加行标签「D-空行」与列标签「2027-空列」，矩阵从 3×3 变成 4×4。两者都没有元素：行 D 与列 2027 在数组里只有空区间，但**标签保留**；维度由标签数量决定，不看数值位置。

每个样例都有 CSR 与 CSC 两份文件。两份文件的逻辑内容（坐标、类型、数值）
完全相同，只有压缩方向不同——可以并排对照 `indptr` 与 `indices` 来确认这一点。

## 如何读取一份文件

```
offset 0     "SPMATRIX"              魔数，8 字节
offset 8     uint32 format_version   当前为 1
offset 12    uint32 header_len       JSON 头长度
offset 16    header                  UTF-8 JSON：标签表、形状、nnz、段长度…
其后         int64  indptr[主维+1]   每个主轴块的起始槽位
             int64  indices[nnz]     次轴 label_id，块内递增
             uint8  kinds[nnz]       0=数值 1=显式零 2=显式缺测
             float64 data[nnz]       小端；显式零为 0.0，缺测为 NaN
末尾 64      sha256(前面所有字节) 的十六进制
```

`indptr[i] .. indptr[i+1]` 就是主轴标签 `label_id == i` 的槽位区间。CSR 的主轴是
行、次轴是列；CSC 反过来。**数组下标是 `label_id`，不是显示位置**，因此标签重排
或改名不会影响已发布文件。

### 用标准库读取（不依赖本项目）

```python
import hashlib, json, math, struct

blob = open('v1-22b99e92-csr.sparse', 'rb').read()
assert blob[:8] == b'SPMATRIX'
assert hashlib.sha256(blob[:-64]).hexdigest() == blob[-64:].decode()
version, hlen = struct.unpack('<II', blob[8:16])
header = json.loads(blob[16:16 + hlen])
sizes = {name: size for name, _t, _e, size in header['sections']}
pos = 16 + hlen

def take(name, code):
    global pos
    raw, pos = blob[pos:pos + sizes[name]], pos + sizes[name]
    return list(struct.unpack('<%d%s' % (len(raw) // struct.calcsize(code), code), raw))

indptr = take('indptr', 'q')
indices = take('indices', 'q')
kinds = list(blob[pos:pos + sizes['kinds']]); pos += sizes['kinds']
data = take('data', 'd')

names = {l['id']: l['name'] for l in header['row_labels']}
cols = {l['id']: l['name'] for l in header['col_labels']}
for row in range(len(indptr) - 1):
    for slot in range(indptr[row], indptr[row + 1]):
        col = indices[slot]
        if kinds[slot] == 0:
            print(names[row], cols[col], '=', data[slot])
        elif kinds[slot] == 1:
            print(names[row], cols[col], '= 显式零')
        else:
            print(names[row], cols[col], '= 缺测')
```

项目自带的独立复核脚本不导入仓库代码，会重新计算校验和、按 `indptr` 重建区间、
逐槽位检查索引范围与类型/数值一致性，并核验标签表数量与形状：

```bash
python3 scripts/verify_delivery.py deliverables/storage/*.sparse
```

## 三态约定

| 状态 | `kinds` | `data` | 占槽位 | 含义 |
| --- | --- | --- | --- | --- |
| 数值 | 0 | 有限浮点 | 是 | 有测量值 |
| 显式零 | 1 | `0.0` | 是 | 真实的 0，不是缺失 |
| 显式缺测 | 2 | `NaN` | 是 | 明确标记为缺测 |
| 没有元素 | — | — | 否 | 该坐标从未出现 |

稀疏压缩只去掉「没有元素」。显式零与缺测都保留为真实槽位，所以 `nnz` 是**槽位数**，
不是「非零个数」。空行 / 空列没有槽位，但标签仍写在头部标签表里，形状由标签数量决定。
