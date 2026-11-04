# X86 MIR corpus 流水线：功能与原理

- 日期：2026-11-04
- 范围：`llvm/utils/x86-legalizer-corpus/` + `llvm/unittests/Target/X86/X86LegalizerMIRTest.cpp`
- 基线：`e5408d231441`（assertions 构建 `/home/ubuntu/llvm/builds/explore`）
- 关联证据：`reports/mir-full-C2.json`、`triage/`（SUMMARY、a2-budget-sweep、issues 等）

## 0. 一句话

**把“规则表”变成“可执行的查询集合”，再用真实的
`legalizeMachineFunction` 跑每个查询，最后输出“每个 atom 为什么终止/
为什么没终止”的可归因证据。**

它不是普通单元测试，而是“规则表 → 查询 → 真实 legalizer → 报告”的
可重跑流水线：

```
X86LegalizerInfo.cpp (源 revision)
        │ parse.py
        ▼
   rule ledger（rule set / opcode / 规则链 / 类型类 / mem / align）
        │ domain.build_points
        ▼
   points（只有形状，没有 expected）
        │ X86Tests: *GenerateCorpusFromCppPoints*
        ▼
   corpus JSON（id, opcode, types, mem, expected）
        │ X86Tests: *RunCorpusThroughLegalizer* + mirrun.py
        ▼
   report JSON（合法化/失败/不可构造/verifier 拒绝/crash/budget/hang）
```

## 1. 为什么要造这个测试集

Legalizer 的输入不是一个“指令”，而是一个 **query**：

```
(opcode, type tuple, memory descriptor) -> action
```

X86 有 115 个 generic opcode、275 条规则、4 套 subtarget config。手写 lit
测试只能覆盖人想到的形状。我们真正想知道三件事：

1. **覆盖**：每条规则、每个 opcode 是否至少被问过一次？
2. **终止性**：对任意合法/半合法形状，legalizer 会不会不收敛？
3. **归因**：崩了/循环了，是规则选错动作、helper 实现错、还是 verifier
   没挡住非法 MIR？

这三件事只有让**真实 legalizer** 跑起来才能回答。模拟
“query → action”的状态机不够——artifact 层的 livelock 就在模拟层下面。

## 2. 查询是怎么生成的（`parse.py` + `domain.py`）

### 2.1 先解析“人写的规则”，不是解析生成表

工具刻意解析 **`X86LegalizerInfo.cpp`**（手写链），而不是 TableGen 生成的
`.inc`。原因：

- 生成表是**被测对象**，不能拿它自己当测试输入的唯一来源；
- 手写链表达了“意图”，`.inc` 只在 `verify` 里做交叉检查；
- 这样 corpus 可以独立于当前树的 TableGen 实现，跨 revision 生成。

解析结果是一份 **rule ledger**：每个 rule set 的 opcode、规则方法
（`clampScalar`、`widenScalarToNextPow2`、`scalarizeIf`…）、参数、
subtarget 条件、类型类、内存描述和 align 阈值。

### 2.2 四个“形状来源”（origin）

`domain.build_points()` 把 ledger 展开成 query 形状，实际语料里是这四类：

| origin | 做法 | 作用 |
|---|---|---|
| `rule` | 对每条规则做**谓词定向搜索**：找一组 types 让它匹配这条规则；非目标槽位用 filler 类型补 | 保证每条规则至少被问一次（白盒覆盖） |
| `boundary` | 审过的边界面板（12 点 witness / 60 点 deep）：`s3, s7, s24, s33, s65, s100, s129, s200, s256`、`v3s32, v16s16, v64s8` | 打非 2 次幂、刚过阈值、窄向量这些最容易触发 widen/clamp 的点 |
| `classpair` | 对每个类型槽做**类笛卡尔积**（full/deep tier） | 覆盖两个槽位之间的交互，是语料的主体 |
| `as` | 地址空间指针组合（`p1/p2`，mem 的 addr space） | 覆盖指针宽度/地址空间相关规则 |
| `opcode` | 多 opcode rule set 的每个额外 opcode 各来一发 | 防止只测到第一个 opcode |

类型词汇是 `E.scalar(bits)`、`E.vec(lanes, elem)`、`E.ptr(addrspace)`，
以及 pointer vector。生成后会：

- 排序、去重，分配稳定 ID：`cpp/<rule_set>/<opcode>/<origin>/<n>`；
- 剪掉 pointer 参与 `changeElementSize` 的非法 query
  （`pruned_pointer_element_size`）；
- 做 **coverage gate**：每条 active rule 要么有 probe，要么在
  `coverage_exceptions.json` 里有审过的例外，否则 corpus 直接失败。

我们这批 C2 full 语料的实际数字：

```
208,851 atoms
  classpair 180,342
  boundary   27,672
  rule          448
  as            336
  opcode         53
rules: 275 total / 271 active / 261 probed / 10 reviewed exceptions / 0 unprobed
opcodes: 115 / 115 covered
```

所以“测试集”不是随手采样，而是**规则覆盖率驱动的枚举**。

## 3. expected 是怎么来的（`oracle.py` + 冻结 reference）

关键原则：**Python 侧永远不写 `expected`。**

- `--oracle=none`：只产出 shapes，没有答案，适合 review 和 diff；
- `--oracle=reference`：调用 `X86Tests` 的
  `*GenerateCorpusFromCppPoints*`，由冻结的
  **`X86LegalizerInfoReference::getAction()`** 逐条填入 expected；
- 如果 shapes 源 revision 和 reference revision 不同，envelope 标记
  `cross_revision: true`，默认拒绝，除非显式 `--allow-cross-revision`；
  同时记录 rule delta（按 rule 文本，不按 index）。

我们的语料正是跨 revision 的：

```
shapes:     fed20e0e…   (X86LegalizerInfo.cpp)
oracle:     ebac177…    (frozen C++ reference)
production: 当前树的生成表
```

这带来一个重要区分：`expected` 是**单次 query 的规则答案**，不是“整个
legalizer 的最终结果”。所以 runner 不用它判定通过/失败，只在 triage 里
用它区分已知家族（A3）。

## 4. 一个 atom 是怎么变成真实实验的（`X86LegalizerMIRTest.cpp`）

每个 atom 都独立构造一个最小 `MachineFunction`：

1. **fresh state**：新 `Module`、`Function`、`MachineModuleInfo`、
   `MachineFunction`。不能复用 MF，否则 MMI 按 `Function*` 缓存，
   寄存器和指令会串。
2. **一个 MBB、一条 generic 指令**：按 `MCInstrDesc` 的
   `getGenericTypeIndex()` 把 corpus 的 types 分配到 def/use 槽位。
3. **操作数补齐**：
   - use 用 `G_IMPLICIT_DEF` 产生（这是 producer 归因的来源）；
   - 立即数、predicate、shuffle mask 按审过的规则补；
   - 变参形状（BUILD_VECTOR/CONCAT/MERGE/UNMERGE）单独处理。
4. **liveness anchor**：如果指令的 def 是 trivially dead，补一条 `COPY`
   把它钉住，否则 legalizer 会直接删掉、根本不测。
5. **构造门禁**：
   - `unbuildable`：控制流、intrinsic、缺 fill 规则、未审立即数等，
     **显式拒绝并给稳定 reason**；
   - `construction_mismatch`：类型 readback 不一致，或 `MF.verify()`
     拒绝——说明这个形状本来就不是合法 MIR；
   - 只有 verifier 通过，才进入 legalizer。
6. **真实执行**：`legalizeMachineFunction(MF, LI, {&Budget}, …)`。
7. **预算观察器**：`StepBudgetObserver` 统计
   `createdInstr + changingInstr + erasingInstr`，超过 `max_steps` 就
   `report_fatal_error`，并在 stderr 里带上 atom id。
8. **markers**：每个 atom 在 `start`、`constructed`、`legalize` 时写
   JSONL marker。这样 supervisor 能区分：构造阶段崩了、legalize 阶段
   崩了、进程在两个 atom 之间被杀了（不能伪造成 crash）。

最终每个 atom 落到一个明确的 outcome：

| outcome | 含义 |
|---|---|
| `legalized` | legalizer 正常终止 |
| `failed` | 返回 `UnableToLegalize`（带 `failed_on_opcode`） |
| `unbuildable` | 合成器按策略拒绝构造 |
| `construction_mismatch` | 构造出来了，但 verifier 拒绝（非法 MIR） |
| `crash` | 通过 verifier 后 legalizer assert/abort |
| `budget` | 超过 MI 变更预算 |
| `hang` | 外部 timeout 时仍停在 legalize |
| `not_run` | 证据不完整（不是“通过”） |

## 5. supervisor 为什么能扛住 208k atom（`mirrun.py`）

- **分片**：`N = ceil(atoms / shard_size)`，shard `i` 处理
  `global_index % N == i`；`--jobs` 并行，最后按 corpus 顺序合并。
- **崩溃恢复**：某个 shard 在 atom k 处 crash/budget/hang，supervisor
  保留该结果，然后从 `k+1` 重新启动同一 shard
  （`X86_LEGALIZER_MIR_START_INDEX`）。
- **不伪造**：如果重启后没有任何进展，剩余 atom 记 `not_run` 并带原因
  （`timeout_before_start` / `unattributed_termination`），不会被算成
  crash。
- **完整性不变量**：`sum(counts) == corpus.atoms`；每个 `unbuildable`
  都有非空 reason；`not_run` 默认必须是 0。
- **A3 分类**：把异常分成 known family、new findings、coverage gaps；
  `--only-atom` 和 `--a2-probe` 会豁免 A3，避免单 atom 探针伪造
  “覆盖缺口”。

退出码也按证据语义设计：`0` = 全部终止；`1` = 有 crash/budget/hang；
`2` = 基础设施问题（不是“测试跳过”）。

## 6. 为什么必须跑“真实 legalizer”

README 里那个 `v2s32 G_BUILD_VECTOR` 的例子最能说明问题：

- 规则表说 `MoreElements`；
- 差分测试和 trace 都同意：下一步应该变成 `v4s32`；
- 但真实 helper 的 `moreElementsVector()` 先补 undef lane，
  `buildDeleteTrailingVectorElements()` 又把它们删掉，
  `buildMergeLikeInstr` 重建出的还是 `v2s32`；
- 下一次 query 完全相同 → 永远循环。

这个循环发生在 **query → helper → artifact** 层，任何“只模拟
query → action”的测试都看不见。只有把真实的
`legalizeMachineFunction` 跑起来、加上步数预算，才能把它变成
`budget` 证据。

这也是 A2 sweep 的价值：一条规则修复把 225/225 的 budget 全部变成
`failed`，并且 `failed_on_opcode` 证明失败都发生在辅助
`G_BUILD_VECTOR/G_INSERT/...` 上——测试集不仅发现了循环，还告诉我们
循环/失败到底发生在哪条指令上。

## 7. 这套设计的边界

- 它测的是**终止性、断言、verifier 门禁**，不测 legalization 的语义
  正确性；
- 合成操作数没有真实语义，不能替代 codegen 测试；
- `expected` 是单 query 的规则答案，不是端到端结果；producer 可能先
  失败（所以需要 `failed_on_opcode` / `blocked_by_producer` 分类）；
- assertions 构建下的 abort 与 release 行为不同，release 结论需要
  NDEBUG/ASan 复跑；
- 语料域本身会限制覆盖率：我们这批 208,851 个 atom 里只有 24,490 个
  真正进入 legalizer，但这是 **24,490 / 24,807 = 98.7% 的 MIR 可表达
  查询**，不是 synthesizer 只做到 11.7%。

## 8. 一句话总结

**atom = 一个 query + 一个最小真实 MF 实验；corpus = 规则覆盖驱动的
query 集合；report = 每个 atom 的终止性证据与归因。**

这就是“创建测试用例”这条线的功能与原理。
