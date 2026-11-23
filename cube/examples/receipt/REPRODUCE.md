# 签收核验小样例（复跑方法）

月结前，审计员手里有两样东西：**计划侧**——按审批先后排列的更正包清单
（每轮一个 `correction_package/v1` 文件），和**实际侧**——数仓导出的
过账日志切片（NDJSON，可乱序、可交叠）。`verify` 命令在不过账、不生成
修复分录的前提下，核验截至某个提交号，哪些更正组能凭实际事务签收。所有
输入都在本目录，命令从仓库根目录离线运行；建议始终用 `python3 -I`。

## 输入

| 文件 | 角色 |
| --- | --- |
| `plan.json` | 核验计划：两轮，按审批先后排列，各指向一个更正包 |
| `packages/round1.json` | 第 2024-05 轮：组 `{BK-A, BK-B}`（4 条分录）与独立组 `{BK-D}` 生效 |
| `packages/round2.json` | 第 2024-06 轮：组 `{BK-B, BK-C}` 生效（BK-B 的前置组即上一轮 `{BK-A, BK-B}`）；`{BK-E}` 未生效；`{BK-F}` 生效 |
| `logs/slice_a.ndjson` / `logs/slice_b.ndjson` | 过账日志导出切片，乱序且交叠（两片都含 1 号提交第 1 行的相同记录） |

实际日志里：组 `{BK-A, BK-B}` 的 4 条分录被**拆在 1、2 两笔事务**；3 号
事务一起过账了 2024-06 轮的 `{BK-B, BK-C}` 与 `{BK-F}`；4 号事务是独立
组 `{BK-D}`。

## 运行

```bash
python3 -I src/cube/cli.py verify \
  --plan examples/receipt/plan.json \
  --logs examples/receipt/logs/slice_a.ndjson examples/receipt/logs/slice_b.ndjson \
  --cutoff 4 --out examples/receipt/out/conclusion.json
```

预期结论（已生成在 `out/conclusion.json`）：

- `{BK-A, BK-B}`：**收到回执却不能签收**——金额与分录逐条都对得上，但拆在
  两笔事务里（`group_entries_in_multiple_commits`，commits [1, 2]）；
- `{BK-B, BK-C}`：前置组未签收而被拦（`prerequisite_not_signable`）；
- `{BK-F}`：自身分录无问题，但与 `{BK-B, BK-C}` 同在 3 号事务，不得单独
  签收（`co_transaction_group_prerequisite_unmet`）；
- `{BK-D}`：与上述组无交集，**可签收**（commit 4）；
- 1、2、3 号事务列入 `failed_transactions`，各带可定位依据；导出重叠的
  那一份只计一次（`duplicate_export_lines = 1`）。

## 更多场景（自动复跑）

```bash
python3 -I examples/receipt/verify_receipt.py
```

脚本在临时目录生成变体日志并断言：

1. 上述拆分场景（金额对齐也不签收）；
2. 把前轮组的实际提交号改到后轮事务之后：后轮事务（含同事务的 `{BK-F}`）
   随之不能签收，独立对照组 `{BK-D}` 不受影响；恢复先后次序后全部签收；
3. 同位置相同记录是导出重叠、只计一次；同内容写在新行号是第二次过账，
   签收失败（`unexpected_entry`）；
4. 前缀缺一行：退出码 2，且此前写出的结论文件不被替换。

## 输入格式

### 计划（`verification_plan/v1`）

```json
{"format": "verification_plan/v1",
 "rounds": [{"round": "2024-05", "package": "packages/round1.json"}, ...]}
```

`round` 为唯一轮次 ID；`package` 相对计划文件所在目录解析（也接受绝对
路径，或直接给一轮对象组成的 JSON 数组）。只读取包中 `effective=true`
组的分录（`reversals`/`postings`，按账套成员归组）；生效但无分录的组不
要求回执、也不构成前置组；未生效组不应收到分录。轮次重复、组成员交叠、
分录归不到唯一生效组，都按输入无效处理（退出码 2）。

### 过账日志（NDJSON）

每行一个 JSON 对象，字段恰好为：

| 字段 | 含义 |
| --- | --- |
| `commit` | 事务提交号，日志中从 1 连续递增 |
| `line` / `line_count` | 事务内行号（从 1）/ 该事务总行数 |
| `round` | 轮次 ID |
| `entry` / `book` / `rect` / `kind` / `new_pair` / `amount` | 分录本体：冲销或登记、账套、原格、来源、两轴新码、带符号金额 |

`--cutoff N`（非负）只取截至 N 号的提交为证据。同提交号同行号的相同记录
是导出重叠（计一次）；同位置内容冲突、同事务总行数不一致、缺提交、缺行，
都使前缀无法核验（退出码 2，不写结论）。超出截止号的记录不参与判定。

## 结论（`receipt_verification/v1`）

- `groups`：按计划顺序列出所有有应到分录的组（轮次 + 成员），状态为
  `signable`（附 `commit`）/ `no_receipt` / `receipt_not_signable`
  （附 `reasons`）；
- `failed_transactions`：逐笔未通过的实际事务（含没有任何可归属组的
  事务），每笔至少一条可定位依据（行号及不匹配内容、同组出现的其它
  提交号、未满足的前置组等）；
- `stats`：前缀提交数、回执行数、导出重叠行数、各状态组数与事务数。

签收判定（一笔事务及其各组同时成立才可签收）：事务每行都能归到有应到
分录的生效组；每个涉及的组全部应到分录原样、按条数落在这一笔事务里，
且前缀中其它事务没有该组分录（逐条比较，不按合计轧差）；事务涉及的
每个组，其每位成员在更早轮次中最近的、有应到分录的生效组都已签收且
实际提交号更小（同一事务写两轮不算先后）。条件不成立时，同事务的其它
组不单独签收；无交集的组不受拖累。

相同证据拆成不同文件、调整书写顺序或增加同位置重叠副本，结论内容不变
（`stats.duplicate_export_lines` 如实记录实际看到的重叠份数）。

## 资源口径

内存只随计划侧（组数与应到分录数）增长，外加一个定长排序缓冲；日志经
一次外部排序落在临时磁盘上，之后常数遍扫描，不随回执总行数、事务个数
或单笔事务行数占用额外内存，也不按事务回扫原始切片。规模数据在本地
生成即可（见 `tests/test_receipt.py` 的 `TestScale`）。
