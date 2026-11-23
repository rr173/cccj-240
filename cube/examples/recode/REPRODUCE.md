# 离线转仓小样例（复跑方法）

## 一、主样例（现有 4 个账套）

输入（均未修改）：

- `../data/dims.json`、`../data/books.json`：现有交接维度与账套。
- `../data/mapping_sample.json`：粗粒度映射。`X` 一条 `X -> N-WEST` 覆盖整棵树，
  `A -> N-ALPHA` 覆盖 A 子树（B/Z 继承根规则）。
- `../data/mapping_exception.json`：与 sample 只差一条 `A1 -> N-BETA`。

复跑（任意目录，离线）：

```bash
python3 -I src/cube/cli.py recode \
  --dims examples/data/dims.json \
  --books examples/data/books.json \
  --mapping examples/data/mapping_sample.json \
  --out examples/recode/batch_sample.json --audit
# 把 mapping_sample.json 换成 mapping_exception.json，输出 batch_exception.json
```

加 `--audit` 会在 stdout 打印发布/扣留、金额合计与对账结果；对账不平时
命令以退出码 3 结束，且不覆盖目标文件。

## 二、例外映射拆分控制额（同一个账套）

输入：

- `../data/recode_dims.json`、`../data/recode_books.json`：单一账套 `E`，
  `A|Y` 上有一笔控制额 100；`a2|P` 缺报，所以它属于现有引擎判定的
  “暂不可核验、整格可信任”的控制额。
- `../data/mapping_recode_base.json`：`a1/a2` 同码、`p1/p2` 同码，
  `A|Y` 的适用范围只落一个新码对。
- `../data/mapping_recode_exception.json`：仅把 `a1` 改成 `NX1`、
  `a2` 改成 `NX2`，使 `A|Y` 跨新码对。

```bash
for m in base exception; do
  python3 -I src/cube/cli.py recode \
    --dims examples/data/recode_dims.json \
    --books examples/data/recode_books.json \
    --mapping examples/data/mapping_recode_$m.json \
    --out examples/recode/batch_recode_$m.json --audit
done
```

对比（旧账合计恒为 100，`reconcile.delta` 均为 0）：

| 映射 | 入仓 | 暂挂 | 说明 |
| --- | --- | --- | --- |
| base | `A|Y` 控制额 100 | 无 | 控制额只落一个新码对，整笔入仓；缺报被吸收，不另生成记录 |
| exception | `a1|P` 40（NX1/MY） | `A|Y` 差额 60；`a2|P` 缺报无金额 | 下层可确认的 40 入仓，差额 100-40=60 对回控制格；差额没有补出缺报格的值 |

两份产出的 `totals.all_sources` 都等于 100；`parked` 项 `new_pair` 均为
`null`，不猜目标。

## 三、测试

```bash
python3 -I -m unittest discover -s tests -v
```

大输入（两轴各 2 万叶子、少量源格）在测试内生成，不落库。
