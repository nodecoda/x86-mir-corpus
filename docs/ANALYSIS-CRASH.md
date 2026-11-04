# 全量语料 crash 分析（2026-11-04）

数据：`corpora/full/upfull-C2.json`（208,851 atom）× baseline 报告
`corpora/full/mir-full-C2-v2.json`，其中 **3,265 个 atom 的 outcome 是
`crash`**（1.6%）。本文把这 3,265 个逐个归类，给出每个家族的断言点、调用路径、
触发类型组合和根因。

复现环境：`builds/explore`（main + `X86LegalizerInfo.td` 修复移植），
`x86-mir-run` 以 `-UNDEBUG` 构建，断言全部开启。

产物：

- `crash-families-detail.json`：12 个家族的断言、类型分布、代表 atom；
- `crash-atoms.json`：3,265 个 atom 的逐条 `{id, opcode, family, types, expected}`。

## 结论先行

1. **12 个家族，全部需要"类型槽不合法"的查询才会触发。** 语料按 generic type
   index 独立赋值（见 `sidecar/mir/MIRRunner.cpp::assignTypes`），因此能造出
   "ICMP 结果是指针"、"进位是 s128"、"select 条件是指针"这类真实 MIR 不会出现的
   指令。
2. **23 个合法类型组合（含向量 carry、i100、i1 compare）反测全部不 crash**，
   只有 `failed`/`legalized`。所以这些断言不是"合法输入上的 bug"。
3. **MachineVerifier 拦不住它们**：driver 在合法化前已经 `MF.verify()`，这
   3,265 个全部通过。verifier 对 generic opcode 的类型检查很粗（例如 G_ICMP
   只查 lane 数，不查结果为 i1、不查指针操作数），所以断言发生在 legalizer。
4. 上游价值低：都是"畸形 MIR 上 assert 而不是 graceful fail"，真实 IR 走不到。
   对工具价值高：每次 crash 会杀死 driver，shard 从断点重启并重放尾部，
   3,265 次 crash 是全量跑的主要耗时来源。

## 家族总表

| # | 断言点 | n | opcode | 代表查询（type0, type1） | 直接原因 |
|---|---|---|---|---|---|
| 1 | `LegalizerHelper.cpp:5367` | 1544 | UADDE/UADDO/USUBE/USUBO | `v16s16, v64s8` / `v16s16, p0:64` | carry 槽不是"与结果同 lane 数的 i1 向量/标量" |
| 2 | `MachineIRBuilder.cpp:618` | 652 | CTLZ/CTTZ/CTPOP/CTTZ_ZERO_POISON | `v16s16, s1` | 结果与源形状不一致（vector dst, scalar src） |
| 3 | `MachineIRBuilder.cpp:1406` | 535 | ICMP + UADDO/USUBE/USUBO | `s1, v16s16` / `p0:64, s100` | 比较结果不是 i1 标量/向量（ICMP dst 是 int/ptr 或向量） |
| 4 | `MachineIRBuilder.cpp:1591` | 218 | UADDE/UADDO | `s1, v16s16` | carry_out 非标量（carry 槽给了向量） |
| 5 | `MachineIRBuilder.cpp:780` | 129 | ICMP | `s100, s128` | 拆分后不足 2 段，`buildUnmerge` 收到 1 个寄存器 |
| 6 | `MachineIRBuilder.cpp:1592` | 118 | UADDE/UADDO | `s128, s100` | carry_out 与 carry_in 类型不一致（carry 不是 i1） |
| 7 | `MachineIRBuilder.cpp:218` | 33 | SHL/ASHR/LSHR/ADD/… | `v2p0:64` / `s100, p0:64` | 操作数是指针（算术/移位不支持指针） |
| 8 | `LegalizerHelper.cpp:1939` | 12 | ICMP | `s100, s1` | equality 拆分得到 <2 个 XOR 片段 |
| 9 | `MachineIRBuilder.cpp:1293` | 7 | SELECT | `s100, p0:64` | select 条件是指针，不是 i1 |
| 10 | `MachineIRBuilder.cpp:1278` | 7 | USUBE | `s100, s8` | 扩展方向与断言要求相反（carry 非 i1） |
| 11 | `MachineIRBuilder.cpp:1587` | 7 | UADDE | `v2p0:64, v2p0:64` | 结果是"指针向量"，标量路径要求标量 |
| 12 | `MachineIRBuilder.cpp:224` | 3 | G_SEXT_INREG | `p0:64` | 对指针做 sext_inreg（会被 lower 成移位） |

每个家族的类型分布见 `crash-families-detail.json` 的 `top_types`。

## 逐个说明

### 1. LegalizerHelper.cpp:5367 — 1,544（48%）

```
assert(hasSameNumEltsOnAllVectorOperands(MI, MRI, NonVecOpIndices) &&
       "Non-compatible opcode or not specified non-vector operands");
```

路径：规则要求 `FewerElements` → `LegalizerHelper::fewerElementsVector`
（`LegalizerHelper.cpp:5945`）→ `fewerElementsVectorMultiEltType(GMI, NumElts)`。
注意 UADDE/UADDO/USUBE/USUBO 这个分支**没有传 NonVecOpIndices**，所以函数要求
所有操作数都是"同 lane 数的向量"。

语料给的 carry 槽（type1）分布：`vec-int 994`、`int 248`、`vec-ptr 116+`、
`ptr 68`。例如 `<16 x i16> + carry <64 x i8>`（lane 数不同），或
`<16 x i16> + carry p0:64`（carry 是标量指针）。合法 MIR 里 carry 必须是 i1，
向量运算时是与结果同 lane 数的 `<N x i1>`。

判定：**查询非法**；helper 的断言是在验证这个前置条件。

### 2. MachineIRBuilder.cpp:618 — 652

```
assert(Res.isScalar() == Op.isScalar());
```

路径：`LegalizerHelper::widenScalar` → `buildExtOrTrunc`。

查询固定为 `vector dst, scalar src`：`v16s16` 的 CTLZ 配 `s1`/`s8` 源。
`widenScalar` 试图把标量源扩成结果形状，`buildExtOrTrunc` 拒绝标量/向量混用。

判定：**查询非法**（结果与源形状必须一致）。

### 3. MachineIRBuilder.cpp:1406 — 535

`buildInstr` 里 G_ICMP 的类型检查：

```cpp
if (Op0Ty.isScalar() || Op0Ty.isPointer())
  return DstTy.isScalar();
else
  return DstTy.isVector() && DstTy.getElementCount() == Op0Ty.getElementCount();
```

G_ICMP 的 type0 是**结果**、type1 是比较操作数。语料组合出
`dst=s1/s80, src=<vector>` 或 `dst=p0:64, src=s100`（结果是指针）。
另外 UADDO/USUBE/USUBO 的 528 例是在溢出 lowering 里构造比较时撞到同一断言。

判定：**查询非法**（ICMP 结果必须是 i1 或 `<N x i1>`）。

### 4. MachineIRBuilder.cpp:1591 — 218

```
assert(DstOps[1].getLLTTy(*getMRI()).isScalar() && "Invalid operand");
```

路径：`widenScalar` → `widenScalarAddSubOverflow`。该函数把
`CarryOutTy = MRI.getType(operand 1)`（即 carry 槽）直接传回
`buildInstr(G_UADDE/G_UADDO, {WideTy, CarryOutTy}, ...)`。
carry 槽是向量（`v16s16`、`v64s8`、`v2p0:64`）时断言失败。

判定：**查询非法**（标量路径要求 carry 是标量 i1）。

### 5. MachineIRBuilder.cpp:780 — 129

```
assert(TmpVec.size() > 1);   // buildUnmerge
```

路径：`narrowScalar`（G_ICMP equality）→ `extractParts` → `buildUnmerge`。
查询是 `dst=s100, operands=s128/s16/s32/p0:64`。ICMP 结果被赋成 i100，
`narrowScalar` 按 `NarrowTy` 拆分时只得到 1 段，`buildUnmerge` 拒绝 1 个结果。

判定：**查询非法**（ICMP 结果应为 i1）。附带暴露一个 helper 健壮性缺口：
`extractParts` 在"源比目标窄"时应返回 `UnableToLegalize`，而不是构造 1 段的 unmerge。

### 6. MachineIRBuilder.cpp:1592 — 118

```
assert(DstOps[1].getLLTTy(*getMRI()) == SrcOps[2].getLLTTy(*getMRI()) &&
       "type mismatch");
```

路径：`narrowScalar` → `narrowScalarAddSub`。该函数把中间 carry 建成
`i1`（`LLT::integer(1)`），最后一段却把**原始 carry_in** 接到
`G_UADDE` 上。查询把 carry 槽设成 `s100/s128/s129/s200` 等非 i1 类型，
于是最后的 `carry_out`（原始类型）与上一段产生的 `i1` carry 不相等。

判定：**查询非法**（carry 必须是 i1）；helper 隐含假设 carry 宽度为 1。

### 7. MachineIRBuilder.cpp:218 — 33

```
assert((Res.isScalar() || Res.isVector()) && "invalid operand type");
```

路径：`narrowScalarShift` → `buildSub`，以及 `lower` → `buildShl`（SEXT_INREG）。
查询是 `<2 x ptr>` 的向量运算，或 `s100` 配 `p0:64` 的移位量。
指针不是合法的算术/移位操作数。

判定：**查询非法**。

### 8. LegalizerHelper.cpp:1939 — 12

```
assert(Xors.size() >= 2 && "Should have gotten at least two Xors?");
```

路径：`narrowScalar` 的 G_ICMP equality 分支。查询 `dst=s100, operands=s1`：
源只有 1 位，按 `NarrowTy`（8/16/32）拆分得到 0 个 part（全部落进 leftover），
`Xors` 为空。

判定：**查询非法**（ICMP 结果应是 i1）。同样是 helper 健壮性缺口：
parts 少于 2 时应返回 `UnableToLegalize`。

### 9. MachineIRBuilder.cpp:1293 — 7

```
assert(TstTy.isScalar() && "type mismatch");
```

路径：`narrowScalar` → `narrowScalarSelect` → `buildSelect`。
查询 `s100, p0:64`：select 的条件槽（type1）是 `p0:64`。

判定：**查询非法**（条件是 i1 或 `<N x i1>`）。

### 10. MachineIRBuilder.cpp:1278 — 7

```
assert(TypeSize::isKnownGT(DstTy.getSizeInBits(), SrcTy.getSizeInBits()) &&
       "invalid narrowing extend");
```

路径：`widenScalar` → `widenScalarAddSubOverflow` → `widenScalarSrc`
（对 carry 做 bool ext）。查询 `s100, s8`：结果 i100、carry 是 i8。
扩展方向与 helper 的假设不符，`validateTruncExt` 拒绝。

判定：**查询非法**（carry 必须是 i1）。

### 11. MachineIRBuilder.cpp:1587 — 7

```
assert(DstOps[0].getLLTTy(*getMRI()).isScalar() && "Invalid operand");
```

路径：`fewerElementsVector` → `fewerElementsVectorMultiEltType` →
`buildInstr(G_UADDE, ...)`。查询 `v2p0:64, v2p0:64`：结果是"指针向量"，
标量 UADDE 路径要求结果标量。

判定：**查询非法**（对指针向量做带进位加法）。

### 12. MachineIRBuilder.cpp:224 — 3

```
assert((Res.isScalar() || Res.isVector()) && "invalid operand type");
```

路径：`lower` → `buildShl`（G_SEXT_INREG lowering）。查询 type0=`p1:64`
（指针）。

判定：**查询非法**（SEXT_INREG 只接受整数类型）。

## 反证：合法组合不 crash

用同样 driver 构造 23 个"合法槽位"的单 atom 反测，全部正常返回：

| 查询 | 结果 |
|---|---|
| UADDE `s32,s1` / `s100,s1` / `v4s32,v4s1` | legalized / failed / failed |
| UADDO / USUBE / USUBO `v4s32,v4s1` | failed（能力不足，不 crash） |
| CTLZ `v4s32,v4s32` / `s100,s100` / `s1,s1` | failed / failed / legalized |
| CTPOP `v4s32,v4s32` | legalized |
| ICMP `v4s1,v4s32` / `s1,s100` / `s1,p0:64` / `s1,s1` / `s1,s8` | failed / legalized / legalized / legalized / legalized |
| SELECT `v4s32,v4s1` / `s32,s1` | failed / legalized |
| SEXT_INREG `s32`、SHL `s32,s8`、ASHR `v4s32,v4s32`、ADD `s100`、UADDE `s1,s1` | legalized/legalized/failed/failed/legalized |

结论：crash 与"i100/向量/指针"本身无关，只与**槽位组合违反 opcode 类型契约**有关。

## MachineVerifier 为什么没拦住

- `MachineVerifier.cpp:1622` 对 G_ICMP/G_FCMP 只检查
  `DstTy.isVector() == SrcTy.isVector()` 和 lane 数，不检查结果是 i1、
  也不禁止指针操作数。
- 对 UADDE/UADDO/USUBE/USUBO、SELECT、SEXT_INREG 没有对应的槽位契约检查。
- 因此 `MF.verify()` 通过，断言推迟到 legalizer 内部。

## 上游可达性：分三类，不要一概而论

| 类别 | 定义 | 数量（本语料） | 上游价值 |
|---|---|---|---|
| A | IR 可表达 + 槽位契约合法 | 205,586 中的大部分（含全部 225 个 livelock atom） | 高：真实 bug（如 `<3 x i32>` 的 G_BUILD_VECTOR 死循环） |
| B | 槽位契约合法，但 clang 基本不会生成（`i100`、`i1` 算术、怪 lane 数） | 少量 | 低：健壮性/覆盖率，仍属合法 MIR |
| C | 槽位契约不合法（本文 12 个家族） | 3,265 | 仅模糊测试：不能作为 "LLVM bug" 证据 |

判据来自 IR 自身的类型规则，而不是我们的猜测：

- `icmp` 的结果**按定义为 i1 / `<N x i1>`**；本文 535 例把结果赋成指针或 i100；
- `select` 的条件必须是 i1 / `<N x i1>`；本文 7 例用了指针；
- `llvm.*add/sub.with.overflow` 的 carry 是 i1 或与结果同 lane 数的 `<N x i1>`；
  本文 1,544 + 218 + 118 + 7 例把 carry 赋成向量、指针或 i100；
- `llvm.ctlz/ctpop/cttz` 的返回类型与参数类型相同；本文 652 例是 `v16s16 ← s1`；
- `sext_inreg` 只作用于整数；本文 3 例作用在指针；
- IR 的 `add` 不接受指针（指针算术只有 `getelementptr`/`G_PTR_ADD`）；
  本文 33 例对 `<2 x ptr>` 或 `s100 + p0` 做算术/移位。

所以 C 类不是"上游暂时没测到"，而是**任何 IR（包括手写 IR）都无法生成**：
语料按 descriptor 的 generic type index 独立赋值，绕过了 IR 的类型系统。
把它们当作 LLVM 缺陷去报，上游只会回复 "invalid MIR"。

反面对照：225 个 livelock atom 是 `v3s32` 等形状（`<3 x i32>`），
`vector_size(12)` 的 C 代码就能生成，槽位完全合法——这是 A 类，也是真正
值得修的部分。全量复跑已确认修复效果：`budget 225 → 0`，且只有
`budget → failed` 这一种状态迁移，其余 208,626 个 atom 结果不变。

## 对工具的影响与建议

1. **成本**：3,265 次 crash 意味着 3,265 次进程重启。`run_shard_with_resume`
   从崩溃点的下一个 atom 重放剩余尾部；crash 按 opcode 连续聚集，重放接近平方级。
   这是全量跑从"几十分钟"变成"数小时"的主要原因，也是 16 并发 OOM 的诱因
   （每个 driver 峰值 RSS 约 4.3GB）。
2. **已实现（工具侧，P0）**：`MIRRunner` 在 `assignTypes` 之后、构造指令之前
   做 per-opcode 槽位契约检查（`checkTypeContract`，表驱动），违规 atom 直接
   `construction_mismatch` + `detail_code: type_contract`，不再让进程 abort。
   回归测试：`tests/fixtures/type-contract.json`（12 个家族代表 + 23 个合法对照）
   + `tests/test_type_contract.py`。

   全量复跑（208,851 atom，8 并发，shard 300）：

   | outcome | 旧 driver | 契约检查后 |
   |---|---|---|
   | crash | 3,265 | **0** |
   | construction_mismatch | 97,605 | 106,849 |
   | failed | 18,116 | 12,375 |
   | legalized | 3,109 | 2,871 |
   | unbuildable | 86,756 | 86,756 |
   | budget | 0 | 0 |

   新增的 `construction_mismatch` 不止 3,265 个 crash：另有 5,737 个原本
   `failed`、238 个原本 `legalized`、4 个原本 `budget` 的 atom 同样是槽位非法
   （例如 `fcmp` 结果给 i256、`select` 条件给 i32、`uadde` carry 给 i8 向量）。
   这些 atom 的旧 outcome 本身没有意义——legalizer 只是没有走到会断言的 helper。
   契约检查按 IR 类型规则统一拒绝它们。另有 2,674 个原本已由 verifier 拒绝的
   atom 现在在更早的契约检查处得到更具体的 `type_contract` 原因（outcome 不变）。
   耗时从数小时降到约 10 分钟，峰值内存 ~27G（8 并发），不再需要为 crash
   重启降并发。
3. **建议（上游）**：不建议按 crash 提 issue——都是畸形 MIR 触发 assert，真实 IR
   不可达；但家族 5、8 暴露的 `extractParts`/`Xors` 假设可以合并成一个小的
   "legalizer 应 graceful fail" 补丁，等有真实复现再提。
