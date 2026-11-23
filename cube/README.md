# 离线稀疏汇总立方格（sparse handover cube）

把多账套交接的稀疏明细、父组汇总与控制总额，构建成可按**任意父组（两棵维度树的任意子树对）**读取的离线立方格。只做构建与按格取值，不含账套录入界面。

## 必须遵守的口径（实现逐条对应）

| 交接规则 | 实现位置与行为 |
| --- | --- |
| 两棵有限、无环维度树，叶子互斥 | `model.py`：解析时校验单根、可达、无环、无重复父；用 DFS 出入时间做 O(1) 祖先/相交判断 |
| 账套责任矩形 = 两树各选一个子树；账套间叶对互斥 | `loader.py`：加载后两两校验矩形在某一维子树不相交 |
| 非终结格只沿一维拆为该维**全部直接子项**，另一维固定 | `loader.py::_validate_split`：子格矩形集合必须等于直接子项枚举，多一个少一个都拒收 |
| 终结格仅 exact（含 0、可为负）/ missing / not_applicable | `loader.py`：仅 exact 可带整数；非终结与 state 不得混用 |
| 控制总额仅在全部适用子项可精确取到时核验；不等则整账套不发布 | `engine.py::build_cube`：后序计算 `precise / has_applicable / exact_sum`，可核验且不等即扣留 |
| 全 not_applicable 却有精确控制额 = 矛盾 | 同上，扣留并写明原因 |
| 暂不可核验的控制额可用于**整格读取**，但差额不得下推给不可见子格 | 查询时：整格包含则直接取控制额且**不再下钻**（不重复计数）；窗口切开格则忽略控制额、继续下钻 |
| 被部分切开的终结 exact 片段 → 切开区域报未知 | `reason = cut_exact_terminal`，绝不按比例分摊 |
| 没交明细 ≠ 0；父汇总与明细不重复相加 | missing 列入未知不计值；祖先额命中即停止下钻，只计一次 |
| 责任外叶对不可计收入 | 只在责任矩形内求值；目标格其余部分按矩形切分 |
| 无账套声明但属整报表域 → 单列“未交接”，不是不适用 | `_carve_uncovered` 产出 `source=unhanded` 片段；被扣留账套区域单列为 `unpublished:<id>` |
| 不得展开叶笛卡尔积 | 全部几何都是“节点子树对”矩形；`stats.leaf_pairs_materialized` 恒为 0 |

### 结果四态

- `fully_exact`：目标区域全部精确（不适用格不算未知、不计值）。
- `partial`：有精确片段，也有未知片段；附 `sum_known`（只含已精确部分，适用负数）。
- `all_unknown`：没有任何精确片段。
- `not_applicable`：覆盖到的区域全部不适用。

每个结果都附带：实际用到的来源格（`exact_fragments` / `not_applicable_fragments`，含账套、来源矩形、取值原因）和作为未知未计的集合（`unknown_fragments`，区分 missing、切开、扣留、未交接）。

## 目录

```
src/cube/        引擎（model 几何 / loader 校验 / engine 构建与查询 / cli 命令行）
examples/data/   维度树、账套、顺序镜像夹具、批量目标
examples/output_ 已生成的可查结果
tests/           26 个 unittest，无第三方依赖
```

## 命令行

所有命令均离线运行；建议用 `python3 -I`，避免工作目录里的同名模块被导入。

```bash
# 1. 构建并查看发布/扣留审计（含每条控制总额的核验说明）
python3 -I src/cube/cli.py build \
  --dims examples/data/dims.json --books examples/data/books.json

# 2. 按任意父组取一格（人类可读）
python3 -I src/cube/cli.py query \
  --dims examples/data/dims.json --books examples/data/books.json \
  --target A P
# 加 --json 得到带全部来源格与未知集合的机读结果

# 3. 批量取格，结果落盘
python3 -I src/cube/cli.py batch \
  --dims examples/data/dims.json --books examples/data/books.json \
  --targets examples/data/targets.txt --out examples/output_batch.json
```

## 已验证场景（tests/test_cube.py，26 项全过）

1. **拆分顺序相反、结果相同**：`books_b1_only.json`（先 Y 后 X）与 `books_b1_mirror.json`（先 X 后 Y）终结事实完全一致，对 8 个不同切窗的规范化结果逐项相同。
2. **父额精确、一个子额 missing**：B1 根格控制额 110，而 `A2|P2` 未交。
   - 整格 `A × P` → `fully_exact = 110`，只引用根控制额一个来源（不与子额重复相加）；
   - 切窗 `A × P2` → `partial`，已知 40，缺失格列未知，控制额**不能**让切窗变精确。
3. **不适用**：`B1|Q2` 整格 `not_applicable`，与 0、未知严格区分；父级求和时不适用格不计值。
4. **负数**：`A2|P1 = -30` 正常计入。
5. **控制额冲突**：B3 控制额 20 ≠ 子项和 15，整账套扣留；其责任区在查询中列为 `unpublished:B3` 未知，而非未交接或零。全 NA 挂控制额的 B4 同样扣留。
6. **巨大稀疏区**：每维 20 万个叶子（400 亿隐含叶对）、仅一个精确源格，构建约 1.8s、全域查询约 0.7s，`leaf_pairs_materialized = 0`，未交接区以 O(维度规模) 的节点兄弟带矩形表示。
7. **结构拒收**：拆分漏子项、固定维被改、责任矩形重叠、未知节点、非整数、维度成环、state 与 children 混用均报错退出。

运行测试：

```bash
python3 -I -m unittest discover -s tests -v
```

## 输入格式要点

- 维度树：`{"dimensions": {"X": {"root": ..., "nodes": {节点: [直接子项...]}}, "Y": ...}}`，叶子给空数组。
- 账套：`id` + 责任矩形 `rect` + 分解树 `root`；非终结格给 `split`（"X"/"Y"）与全部子格；终结格给 `state`，exact 再给整数 `value`；任意格可挂 `control`。
- 输入文件是外部数据，请放在独立目录并用路径传入；本引擎不执行其中任何代码。
