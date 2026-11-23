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
src/cube/        引擎（model 几何 / loader 校验 / engine 构建与查询
                 / mapping 新旧编码规则 / recode 离线转仓 /
                 approval 交接审批更正包 / cli 命令行）
examples/data/   维度树、账套、顺序镜像夹具、批量目标、新旧编码映射
examples/recode/ 转仓小样例批次与复跑说明
examples/approval/ 交接审批（三次交接链、扣留拦截、两轮闭环）与复跑说明
examples/output_ 已生成的可查结果
tests/           unittest，无第三方依赖
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

# 4. 离线转仓：旧交接 -> 可直接导入新仓的 JSON 批次
python3 -I src/cube/cli.py recode \
  --dims examples/data/dims.json --books examples/data/books.json \
  --mapping examples/data/mapping_sample.json \
  --out examples/recode/batch_sample.json --audit

# 5. 交接审批：当前版 + 拟发布版 + 本次批准 ID -> 可过账更正包
python3 -I src/cube/cli.py approve \
  --dims examples/approval/dims_chain.json \
  --mapping examples/approval/mapping_chain.json \
  --current examples/approval/books_current.json \
  --proposed examples/approval/books_proposed.json \
  --approvals examples/approval/approvals_round1.json \
  --out examples/approval/out/round1.json --audit
```

## 离线转仓（recode）

财务数仓更换了入仓维度编码，旧月份的交接文件无法重发。`recode` 只读取现有
的维度、账套和一份新旧编码映射，产出一份可导入的新仓批次；**不修改原交接
文件，也不提供按新口径查格的接口**。小样例与复跑步骤见
`examples/recode/REPRODUCE.md`。

映射文件每轴一组 `{"old": 旧节点, "new": 新码}`：

- 一条规则管整个子树，越靠近叶子的规则覆盖祖先规则；不同旧叶可归到同一新码。
- 两轴所有旧叶都必须能落到规则；未知节点、同轴重复规则、空新码、未映射叶
  任一条都使转换失败——不生成新批次，也不覆盖已有批次（先写临时文件再原子
  替换）。
- 校验只走两轴各自的树节点，不枚举旧叶对或可能的新码对；每轴数万叶子的
  场景也只需节点级工作量。

批次内容（`recode_batch/v1`）：

- `published_books` / `withheld_books`：沿用现有 `build_cube` 的发布判断。
  被扣留账套只给账套 ID 与原因，**不生成金额或缺失记录**；没有账套负责的
  区域同样不生成任何记录。
- `ingested`：可定归属的金额。每条带 `book`（原账套）、`rect`（原格坐标）、
  `kind`、`amount`、`new_pair`（完整新码对）。
- `parked`：暂挂清单，供财务对回旧账。`new_pair` 恒为 `null`，绝不猜目标。

判定口径：

- **适用**范围以格内适用的叶对为准：不适用部分不参与判定，缺报部分仍算
  适用，但不生成零值。一个精确终结格只落到一个新码对就整笔入仓，否则整笔
  暂挂；不比例拆分，也不放到第一个碰到的新码对。零金额同口径。
- **控制额**只落到一个新码对时直接整笔入仓，该格下的金额与缺报记录都不再
  生成。跨新码对时继续处理下层，可单独定归属的金额照常入仓；控制额另行
  对账：
  `差额 = 控制原额 - 下层第一层遇到的控制额 - 未被这些控制额覆盖的精确终结额`。
  下层金额无论入仓还是暂挂都只扣一次；扣减遇到控制额即停，不把控制额和
  它的明细一并扣两次。差额非零、或扣除范围外仍有缺报时，在控制格留一条
  `control_difference`（带符号；即使差额为零，只要范围外有缺报也保留）。
  差额只对回该控制格，不能据此补出某个缺报格的值。无控制额的中间格只组织
  下层记录，本身不生成金额。
- **来源**：终结额记为 `exact_terminal`，控制额记为 `control_total`，
  控制差额记为 `control_difference`（附 `control` 原额与 `deducted` 扣减
  合计，以及 `deducted_controls` / `deducted_exact` / `outside_missing`），
  未被吸收的缺报记为 `missing`。逐源保留，不会把恰好落在同一新码对的来源
  合并。
- **确定性**：记录按旧树几何顺序排序，打乱映射规则、账套顺序或兄弟格书写
  顺序后批次内容不变。

`--audit` 附带 `reconcile`：对每个已发布账套核对
`Σ入仓 + Σ暂挂金额（含控制差额）== 旧账来源合计`，不平则以退出码 3 结束。

## 交接审批更正包（approve）

同一个月先按旧交接过账、再出拟发布版：部分账套改了金额，部分把责任范围
转给别的账套。`approve` 读取同一套维度树与转仓映射、**当前已过账的完整
账套清单**、**拟发布的完整账套清单**（两份均允许空清单）和**本次批准的
账套 ID 列表**，交付一份真正可过账的更正包；不连数仓、不改原交接文件。
小样例与复跑步骤见 `examples/approval/REPRODUCE.md`。

候选与分组（全部在稀疏矩形上判断，不展开两棵树的叶对）：

- 只有新增、退场或内容有变化的 ID 是更正候选；仅换书写顺序不算变化，
  责任范围、格的分解、状态、精确值或控制额变化才算。批准列表里不变的
  ID 不产生分录；两版都没有的 ID 直接报错（退出码 2）。
- 审批对象是整次责任交接：一个更正的**旧责任**与另一个更正的**新责任**
  在某一处覆盖同一叶对（两维子树相交）即绑定，关系沿间接交接传递，连通
  分量就是不可再拆的更正组。同一 ID 的撤旧与上新也不可拆。判断只走节点
  子树相交，`leaf_pairs_materialized` 恒为 0。
- 仅当组内全部 ID 已批准、且组内所有存在于拟发布版的账套都可发布时整组
  生效；否则整组保持旧状，并在组上列明尚缺的批准与被扣留账套及原因。
  拟发布版因控制额冲突等被现有构建扣留的账套按此规则处理，不把整个请求
  报成输入损坏，也不拖住无交接关系的其它组。已退场无接手方的范围、无
  移交方的新责任都随各自更正生效。

更正包（`correction_package/v1`）内容：

- `groups`：逐组给出成员、本次是否生效、尚缺批准与扣留账套原因。
- `reversals` / `postings`：对生效组，以现有转仓命令的**可入仓记录**
  为准，把成员旧版已过账记录逐条冲销——按相反符号保留原账套、原格、
  来源类型和完整新码对；再把新版可入仓记录逐条登记。冲销与登记分列、
  不按新码对轧差；原金额为负或为零同样处理。暂挂与缺报不是过账分录，
  不会出现。
- `effective_handover`：生效组整体换成新版（含删除与新增），其它组及
  未变化账套保留旧版，即过账后实际生效的交接；格式即现有账套清单，可
  直接作为下一次 `--current` 输入。
- 同一业务输入仅打乱书写顺序，更正包内容完全相同；输出先写临时文件再
  原子替换，结构或映射错误不留下半个包，也不覆盖此前的包。

## 已验证场景

`tests/test_cube.py` 27 项覆盖原有构建/查询口径；`tests/test_recode.py` 24 项
覆盖离线转仓；`tests/test_approval.py` 21 项覆盖交接审批更正包：

- **三次交接链整组生效/冻结**：四个更正 ID 由三条旧责任↔新责任相交边连成
  一组；只差链末一个 ID 的批准时整条链不产生任何分录，无关组仍可过账。
- **扣留账套整组拦截**：拟发布账套因控制额冲突被现有构建扣留时，即使批准
  齐全，同组旧账一条都不冲销，原因写入组报告；其它组不受影响。
- **两轮新码对累计闭环**：从首个更正包的 `effective_handover` 继续审批，
  旧账入仓额加两轮冲销/登记，按新码对累计等于最终生效交接重新转仓的金额。
- **冲销口径**：冲销只翻符号、保留原账套/原格/来源类型/完整新码对，负数
  变正数、零照常出记录；冲销与登记分列不轧差；暂挂与缺报不产生分录。
- **新增、退场、不变、纯换序与空清单**：退场只冲销不登记、新增只登记不
  冲销；换书写顺序不是变化；两版都没有的批准 ID 报错；当前版必须可发布。
- **确定性与原子写**：打乱账套/批准书写顺序，更正包内容相同；结构或映射
  错误不留下半包、不覆盖旧包。

- **映射校验**：未知节点、同轴重复规则、空新码、未映射叶均拒收且不写批次；
  祖先/后代规则覆盖与多旧叶共享新码。
- **例外映射拆分控制额**（`mapping_recode_base` vs `mapping_recode_exception`，
  只差 `a1/a2` 的新码）：原先整笔入仓的控制额 100 变成「可确认的下层入仓 40
  + 控制差额 60」，旧账金额不丢不重（`reconcile` delta = 0）；也覆盖大样例
  `mapping_sample` vs `mapping_exception` 中控制额跨码对、差额为 0 仍因范围外
  缺报而保留的场景。
- **嵌套控制额 + 粗粒度终结额**：上层扣下层控制额一次、不再扣其明细；已暂挂
  的下层金额仍被扣减；粗粒度终结额整笔暂挂、不比例拆分。
- **大输入**：两轴各 2 万叶子、少量源格，构建/转换与映射校验只走节点；大输入
  在测试中生成，不入库。

`tests/test_cube.py` 的原有场景：

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
