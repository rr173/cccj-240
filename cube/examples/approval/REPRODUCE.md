# 责任交接审批小样例（复跑方法）

不连数仓、不改原交接文件。所有输入都在本目录，命令从仓库根目录离线运行；
建议始终用 `python3 -I`，避免工作目录里的同名模块被导入。

## 输入

| 文件 | 角色 |
| --- | --- |
| `dims_chain.json` | 两版共用的维度树（X 两叶、Y 三叶，6 个叶对的网格） |
| `mapping_chain.json` | 与现有 `recode` 相同格式的新旧编码映射 |
| `books_current.json` | 当前已过账的完整账套清单（H1..H5，均可发布） |
| `books_proposed.json` | 拟发布的完整账套清单（H1..H4 沿网格各走一格） |
| `books_proposed_withheld.json` | 拟发布版的扣留变体：H2 终结格控制额 99 ≠ 值 21 |
| `approvals_round1.json` | 第一轮批准：H1..H3、H5，**不含链末 H4** |
| `approvals_full.json` | 全部批准（含已无变化的 H5） |

交接链（每个箭头都在一棵树上保持叶节点不变，共三次交接关系）：

```
H1: x1|y1 -> x2|y1
H2: x2|y1 -> x2|y2      与 H1 新责任相交（共享 x2）
H3: x2|y2 -> x1|y2      与 H2 新责任相交（共享 y2）
H4: x1|y2 -> x1|y3      与 H3 新责任相交（共享 x1）
```

H1–H2、H2–H3、H3–H4 三条边使四个 ID 成为一个不可再拆的更正组；
H5 在 `x2|y3`，与链上任何新旧责任都不相交，是独立组。相交判断只走
维度树节点子树，不展开叶对（输出 `stats.leaf_pairs_materialized` 恒为 0）。

## 第一轮：只差链末 H4 的批准

```bash
mkdir -p examples/approval/out
python3 -I src/cube/cli.py approve \
  --dims examples/approval/dims_chain.json \
  --mapping examples/approval/mapping_chain.json \
  --current examples/approval/books_current.json \
  --proposed examples/approval/books_proposed.json \
  --approvals examples/approval/approvals_round1.json \
  --out examples/approval/out/round1.json --audit
```

预期：链组 `effective=false`、`missing_approvals=["H4"]`，整组一条分录都不
产生；H5 组照常生效（冲销 50、登记 55）。包里的 `effective_handover` 仍
保留 H1..H4 的旧责任范围，可直接作为下一轮的当前清单。

## 第二轮：从生效交接继续，补齐批准

```bash
python3 -I -c "import json;json.dump(json.load(open('examples/approval/out/round1.json'))['effective_handover'], open('examples/approval/out/current_after_round1.json','w'), ensure_ascii=False, indent=2)"

python3 -I src/cube/cli.py approve \
  --dims examples/approval/dims_chain.json \
  --mapping examples/approval/mapping_chain.json \
  --current examples/approval/out/current_after_round1.json \
  --proposed examples/approval/books_proposed.json \
  --approvals examples/approval/approvals_full.json \
  --out examples/approval/out/round2.json --audit
```

第二轮 H5 已与拟发布版相同，不再是候选，只剩链上四个 ID；链组整体生效，
旧 H1..H4 的已入仓记录逐条冲销（H4 原值 -7，冲销为 +7），新记录逐条登记
（H3 新值 0 照样登记），冲销与登记分开保留、不按新码对轧差。

### 按新码对累计核对

把「当前版重新转仓的入仓金额」加上两轮更正包的全部冲销/登记，按新码对
累计，应等于「最终生效交接重新转仓」的金额：

| 新码对 | 旧账 | 两轮更正后累计 | 最终重转仓 |
| --- | --- | --- | --- |
| (NX, NY12) | 53 | 32 | 32 |
| (NX, NY3) | 50 | 49 | 49 |

`verify_approval.py` 会自动完成这条核对（以及下列全部断言）：

```bash
python3 -I examples/approval/verify_approval.py
```

## 扣留变体：批准齐全也不得冲销同组旧账

```bash
python3 -I src/cube/cli.py approve \
  --dims examples/approval/dims_chain.json \
  --mapping examples/approval/mapping_chain.json \
  --current examples/approval/books_current.json \
  --proposed examples/approval/books_proposed_withheld.json \
  --approvals examples/approval/approvals_full.json \
  --out examples/approval/out/withheld.json --audit
```

H2 因控制额冲突被现有构建扣留，这不是输入损坏：链组
`effective=false` 且在 `withheld_books` 里列明 H2 与原因，H1..H4 的旧账
一条都不冲销；无关的 H5 组仍正常过账。

## 错误不产生半包

未知批准 ID、结构或映射错误一律以退出码 2 结束；输出先写临时文件再原子
替换，失败时既不会留下半个包，也不会覆盖此前的包（测试
`test_bad_input_never_overwrites_existing_package` 固定此行为）。

## 测试

```bash
python3 -I -m unittest discover -s tests -v
```

`tests/test_approval.py` 的 21 个用例覆盖：三链接链分组、链末缺批整组
冻结、扣留账套整组拦截且不冲旧账、两轮新码对累计闭环、冲销保号翻号
（含负数与零）、冲销/登记不轧差、同一 ID 撤旧上新不可拆、新增/退场/
不变/纯换序、空清单、未知批准报错、当前版必须可发布、确定性与 CLI 原子写。
