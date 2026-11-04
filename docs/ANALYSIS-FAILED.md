# 失败语料统计与分析（full C2）

日期：2026-11-04
语料：`corpora/full/upfull-C2.json`（208,851 atoms，源 rev `fed20e0e6832`）
基线报告：`corpora/full/mir-full-C2-v2.json`（`rules=production`）
复核驱动：`/home/ubuntu/xmc-main/x86-mir-run` → upstream/main `05c41f574638`

## 0. 结论先行

`failed` 这 17,891 个 atom 里，只有一个桶有行动价值：**1,486 个在 upstream main 上其实是崩溃**，
全部落在向量位计数（`llvm.ctpop/ctlz/cttz`），并且**已有公开 issue #220802**。
其余部分是预期 fallback、装置限制和合成形状。

同时暴露一个方法学问题：基线报告不是上游基线——它由 explore 树的 **TableGen 版 legalizer**
产出，在 1,486 个查询上与上游手写规则语义分叉（上游断言，TD port 返回 `failed`）。

## 1. 基线构成（17,891 failed / 208,851 = 8.6%）

按参考模型（`query.expected.action`）拆分：

| 类别 | 数量 | 占比 | 性质 |
|---|---:|---:|---|
| 参考模型也判 `Unsupported` | 7,525 | 42.1% | 正确 fallback，不是缺口 |
| `libcall_lowering_disabled` | 1,446 | 8.1% | driver 关闭 libcall，装置限制 |
| 与参考模型不一致 | 8,920 | 49.9% | 需分类 |
| — 合成类型（mem 宽度不符 / 向量标量混搭 / 非 2 幂） | 6,672 | 37.3% | IR 不可达，噪音 |
| — 类型看似合理 | 2,248 | 12.6% | 需实测 |

按 `origin`：`boundary` 8,854、`classpair` 8,748、`as` 174、`rule` 92、`opcode` 23。
按 atom opcode 前几名：`G_FPTOUI` 1,149、`G_FPTOSI` 1,144、`G_UITOFP` 1,140、`G_SITOFP` 1,131、
`G_CTLZ` 826、`G_CTTZ` 800、`G_CTTZ_ZERO_POISON` 800、`G_CTPOP` 794。

注意 `failed_on_opcode` 与 atom 自身 opcode 常不一致：合法化过程中新生指令（如 `G_TRUNC`）失败时，
归因落在新生指令上，例如 `G_ADD s100` 的失败记为 `G_TRUNC`。

## 2. 把 17,891 个 failed 原样在 upstream/main 上重跑

```
PYTHONPATH=src python3 -m x86_mir_corpus mir-run --corpus /home/ubuntu/xmc-work/failed-all.json \
  --driver /home/ubuntu/xmc-main/x86-mir-run --rules production \
  --max-steps 10000 --jobs 8 --out /home/ubuntu/xmc-work/failed-all-upstream.json
```

结果：

| outcome | 数量 |
|---|---:|
| crash | **1,486** |
| failed | 16,403 |
| legalized | 2 |

1,486 个崩溃全部是同一条断言：

```
LegalizerInfo.cpp:200: Assertion `mutationIsSane(Rule, Query, Mutation) &&
                        "legality mutation invalid for match"' failed.
```

分布：`X86_CTPOP` 379、`X86_CTLZ` 369、`X86_CTTZ` 369、`X86_CTTZ_ZERO_POISON` 369；
`origin=classpair` 1,079、`boundary` 407；所有 atom 的 type0 都是向量。

其中包含 `<16 x s16>, <16 x s16>` 这类**完全自洽**的类型，说明不是构造噪音。

## 3. 真实 IR 复现（upstream main `05c41f574638`，assertions on）

```llvm
declare <16 x i16> @llvm.ctpop.v16i16(<16 x i16>)
define <16 x i16> @f(<16 x i16> %a) {
  %r = call <16 x i16> @llvm.ctpop.v16i16(<16 x i16> %a)
  ret <16 x i16> %r
}
```

```
$ llc -O0 -mtriple=x86_64-unknown-linux-gnu -global-isel repro.ll -o /dev/null
Assertion `mutationIsSane(...)` failed
```

实测 `<16 x i16>`、`<8 x i16>`、`<4 x i32>`、`<2 x i64>`、`<16 x i8>`、`<32 x i8>`、`<8 x i32>`
全部崩溃；`ctlz`/`ctpop`/`cttz` 三个 intrinsic 全部崩溃。

根因（`X86LegalizerInfo.cpp:289-317` 的规则链尾部）：

```cpp
getActionDefinitionsBuilder(G_CTPOP)
    ...
    .widenScalarToNextPow2(1, /*Min=*/16)
    .clampScalar(1, s16, sMaxScalar)
    .scalarSameSizeAs(0, 1);
```

`scalarSameSizeAs` = `minScalarSameAs` + `maxScalarSameAs`，两个谓词都在
`LegalizerInfo.h:1158-1179`：

```cpp
// minScalarSameAs
Query.Types[LargeTypeIdx].getScalarSizeInBits() > Query.Types[TypeIdx].getSizeInBits()
// maxScalarSameAs
Query.Types[NarrowTypeIdx].getScalarSizeInBits() < Query.Types[TypeIdx].getSizeInBits()
```

左边是**元素位宽**，右边是**总位宽**。当 `TypeIdx` 是向量时（如 v16s16 总宽 256），
`16 < 256` 为真 → 返回 `NarrowScalar` + 原类型（`changeElementSizeTo` 不变）→
`mutationIsSane` 拒绝（NarrowScalar 要求元素变小）→ 断言。

修法方向：两处都改用 `getScalarSizeInBits()`（向量时谓词为假，落到 `Unsupported` 正常 fallback），
或对向量 `TypeIdx` 禁用 `scalarSameSizeAs`。

已知 issue：**#220802** `[GlobalISel] legalizer aborts on a plain vector ctpop`（open，2026-09-03），
复现即 `<4 x i32>` ctpop。本条是其 X86 侧的规模化确认，并额外覆盖 `ctlz`/`cttz`/`cttz_zero_poison`。

## 4. 为什么基线报告没抓到这 1,486 个

`corpora/full/mir-full-C2-v2.json` 由 `/home/ubuntu/xmc-dbg-build/x86-mir-run` 产出，
链接 `builds/explore`，而 explore 树的 `X86LegalizerInfo.cpp` 已被替换为
**由 `X86LegalizerInfo.td` 生成的规则表**（`X86LegalizerTable.cpp`）。

同一查询 `(v16s16, s32)`：

| 构建 | 结果 |
|---|---|
| explore（TD 生成表） | `WidenScalar` 到 v16s32，随后 `Unsupported` → `failed` |
| pr-lane / upstream main（手写规则） | `NarrowScalar` + 原类型 → `mutationIsSane` 断言 |

用纯 `llc` 手写 MIR 可复现该分叉：

```
llc -run-pass=legalizer -verify-machineinstrs ctlz_bad.mir
builds/explore  → LLVM ERROR: unable to legalize instruction
builds/pr-lane  → Assertion `mutationIsSane(...)` failed
```

结论：**TD port 与手写规则在 1,486 个查询上语义不一致**。方向上 port 更安全（不崩），
但这也意味着 baseline 报告的 `failed` 数字不能当作上游行为。

## 5. 其余 16,403 个 failed

- 7,525 参考模型本就判 `Unsupported`：正确 fallback。
- 1,446 `libcall_lowering_disabled`：`G_LLROUND` 222、`G_LROUND` 221、`G_FPEXT` 204、
  其余为数学 intrinsic（`G_FACOS`/`G_FSIN`…）。driver 未开 libcall。
- 其余为合成形状。真实 IR 抽样均干净 fallback：

| 真实 IR | 结果 |
|---|---|
| `add i100` + `trunc` | GISel 直接完成 |
| `ret i100` / `ctlz i128` / `ctpop i128` | SDAG fallback warning |
| `uitofp i128` / `fptoui i128` / `sitofp i100` | SDAG fallback warning |
| `load/store <16 x i16>`、`<64 x i8>` | GISel 直接完成 |
| `usub.with.overflow.i128` | SDAG fallback warning |
| `uadd.with.overflow.v16i16` | 逐 lane 标量化为 G_UADDO，fallback |

## 6. 建议

1. **把 1,486 并入 #220802**：补 X86 规则链定位（`scalarSameSizeAs` 谓词用总位宽）、
   `ctlz/cttz/cttz_zero_poison` 同样受影响、7 种向量宽度的实测矩阵。
2. **baseline 重跑改用 upstream main 驱动**，否则 `failed` 与上游不可比；
   TD port 的差异应作为独立验收项记录，而不是混进基线。
3. **语料约束收紧**：目前每个类型索引独立扫描，产生大量 opcode 语义上不可能的查询
   （如 `G_CTLZ` 的 dst/src 类型不同、`G_LOAD` 的 mem 宽度与结果不符、
   `G_UADDE` 的 carry 用 `p0`）。建议在生成阶段加 opcode 级类型约束，
   预期的噪音削减量约为 `failed` 的 37% + `crash` 的一部分。
4. `failed_on_opcode` 已能区分"新生指令失败"，统计时应同时保留 atom opcode。
