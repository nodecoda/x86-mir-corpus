//===-- X86LegalizerInfoReference.cpp ---------------------------*- C++ -*-==//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Frozen migration reference; see X86LegalizerInfoReference.h.
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerInfoReference.h"
#include "X86Subtarget.h"
#include "X86TargetMachine.h"
#include "llvm/CodeGen/TargetOpcodes.h"

using namespace llvm;
using namespace TargetOpcode;
using namespace LegalizeActions;
using namespace LegalityPredicates;

X86LegalizerInfoReference::X86LegalizerInfoReference(const X86Subtarget &STI,
                                                     const X86TargetMachine &TM)
    : Subtarget(STI) {
  (void)TM;

  bool Is64Bit = Subtarget.is64Bit();
  bool HasCMOV = Subtarget.canUseCMOV();
  bool HasSSE1 = Subtarget.hasSSE1();
  bool HasSSE2 = Subtarget.hasSSE2();
  bool HasSSE41 = Subtarget.hasSSE41();
  bool HasAVX = Subtarget.hasAVX();
  bool HasAVX2 = Subtarget.hasAVX2();
  bool HasAVX512 = Subtarget.hasAVX512();
  bool HasVLX = Subtarget.hasVLX();
  bool HasDQI = Subtarget.hasAVX512() && Subtarget.hasDQI();
  bool HasBWI = Subtarget.hasAVX512() && Subtarget.hasBWI();
  bool UseX87 = !Subtarget.useSoftFloat() && Subtarget.hasX87();
  bool HasPOPCNT = Subtarget.hasPOPCNT();
  bool HasLZCNT = Subtarget.hasLZCNT();
  bool HasBMI = Subtarget.hasBMI();
  bool HasVPOPCNTDQ = Subtarget.hasVPOPCNTDQ();
  bool HasVPOPCNTDQVL = HasVPOPCNTDQ && Subtarget.hasVLX();

  const LLT p0 = LLT::pointer(0, Subtarget.isTarget64BitLP64() ? 64 : 32);
  const LLT s1 = LLT::scalar(1);
  const LLT s8 = LLT::scalar(8);
  const LLT s16 = LLT::scalar(16);
  const LLT s32 = LLT::scalar(32);
  const LLT s64 = LLT::scalar(64);
  const LLT s80 = LLT::scalar(80);
  const LLT s128 = LLT::scalar(128);
  const LLT sMaxScalar = Subtarget.is64Bit() ? s64 : s32;
  const LLT v2s32 = LLT::fixed_vector(2, 32);
  const LLT v4s8 = LLT::fixed_vector(4, 8);

  const LLT v16s8 = LLT::fixed_vector(16, 8);
  const LLT v8s16 = LLT::fixed_vector(8, 16);
  const LLT v4s32 = LLT::fixed_vector(4, 32);
  const LLT v2s64 = LLT::fixed_vector(2, 64);
  const LLT v2p0 = LLT::fixed_vector(2, p0);

  const LLT v32s8 = LLT::fixed_vector(32, 8);
  const LLT v16s16 = LLT::fixed_vector(16, 16);
  const LLT v8s32 = LLT::fixed_vector(8, 32);
  const LLT v4s64 = LLT::fixed_vector(4, 64);
  const LLT v4p0 = LLT::fixed_vector(4, p0);

  const LLT v64s8 = LLT::fixed_vector(64, 8);
  const LLT v32s16 = LLT::fixed_vector(32, 16);
  const LLT v16s32 = LLT::fixed_vector(16, 32);
  const LLT v8s64 = LLT::fixed_vector(8, 64);

  const LLT s8MaxVector = HasAVX512 ? v64s8 : HasAVX ? v32s8 : v16s8;
  const LLT s16MaxVector = HasAVX512 ? v32s16 : HasAVX ? v16s16 : v8s16;
  const LLT s32MaxVector = HasAVX512 ? v16s32 : HasAVX ? v8s32 : v4s32;
  const LLT s64MaxVector = HasAVX512 ? v8s64 : HasAVX ? v4s64 : v2s64;

  // todo: AVX512 bool vector predicate types

  // implicit/constants
  // 32/64-bits needs support for s64/s128 to handle cases:
  // s64 = EXTEND (G_IMPLICIT_DEF s32) -> s64 = G_IMPLICIT_DEF
  // s128 = EXTEND (G_IMPLICIT_DEF s32/s64) -> s128 = G_IMPLICIT_DEF
  getActionDefinitionsBuilder(
      {G_IMPLICIT_DEF, G_PHI, G_FREEZE, G_CONSTANT_FOLD_BARRIER})
      .legalFor({p0, s1, s8, s16, s32, s64})
      .legalFor(UseX87, {s80})
      .legalFor(Is64Bit, {s128})
      .legalFor(HasSSE2, {v16s8, v8s16, v4s32, v2s64})
      .legalFor(HasAVX, {v32s8, v16s16, v8s32, v4s64})
      .legalFor(HasAVX512, {v64s8, v32s16, v16s32, v8s64})
      .widenScalarOrEltToNextPow2(0, /*Min=*/8)
      .clampScalarOrElt(0, s8, sMaxScalar)
      .moreElementsToNextPow2(0)
      .clampNumElements(0, v16s8, s8MaxVector)
      .clampNumElements(0, v8s16, s16MaxVector)
      .clampNumElements(0, v4s32, s32MaxVector)
      .clampNumElements(0, v2s64, s64MaxVector)
      .clampMaxNumElements(0, p0,
                           Is64Bit ? s64MaxVector.getNumElements()
                                   : s32MaxVector.getNumElements())
      .scalarizeIf(scalarOrEltWiderThan(0, 64), 0);

  getActionDefinitionsBuilder(G_CONSTANT)
      .legalFor({p0, s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .widenScalarToNextPow2(0, /*Min=*/8)
      .clampScalar(0, s8, sMaxScalar);

  getActionDefinitionsBuilder({G_LROUND, G_LLROUND})
      .widenScalarIf(typeIs(1, s16),
                     [=](const LegalityQuery &) {
                       return std::pair<unsigned, LLT>(1, s32);
                     })
      .libcall();

  getActionDefinitionsBuilder(
      {G_FCOS,  G_FCOSH, G_FACOS,  G_FSIN,  G_FSINH,   G_FASIN, G_FTAN,
       G_FTANH, G_FATAN, G_FATAN2, G_FPOW,  G_FEXP,    G_FEXP2, G_FEXP10,
       G_FLOG,  G_FLOG2, G_FLOG10, G_FPOWI, G_FSINCOS, G_FCEIL, G_FFLOOR})
      .libcall();

  getActionDefinitionsBuilder(G_FNEG)
      .legalFor(UseX87 && !HasSSE1, {s32})
      .legalFor(UseX87 && !HasSSE2, {s64})
      .legalFor(UseX87, {s80})
      .lower();

  getActionDefinitionsBuilder(G_FSQRT)
      .legalFor(HasSSE1 || UseX87, {s32})
      .legalFor(HasSSE2 || UseX87, {s64})
      .legalFor(UseX87, {s80});

  getActionDefinitionsBuilder({G_GET_ROUNDING, G_SET_ROUNDING})
      .customFor({s32});

  // merge/unmerge
  for (unsigned Op : {G_MERGE_VALUES, G_UNMERGE_VALUES}) {
    unsigned BigTyIdx = Op == G_MERGE_VALUES ? 0 : 1;
    unsigned LitTyIdx = Op == G_MERGE_VALUES ? 1 : 0;
    getActionDefinitionsBuilder(Op)
        .widenScalarToNextPow2(LitTyIdx, /*Min=*/8)
        .widenScalarToNextPow2(BigTyIdx, /*Min=*/16)
        .minScalar(LitTyIdx, s8)
        .minScalar(BigTyIdx, s32)
        .legalIf([=](const LegalityQuery &Q) {
          switch (Q.Types[BigTyIdx].getSizeInBits()) {
          case 16:
          case 32:
          case 64:
          case 128:
          case 256:
          case 512:
            break;
          default:
            return false;
          }
          switch (Q.Types[LitTyIdx].getSizeInBits()) {
          case 8:
          case 16:
          case 32:
          case 64:
          case 128:
          case 256:
            return true;
          default:
            return false;
          }
        });
  }

  getActionDefinitionsBuilder({G_UMIN, G_UMAX, G_SMIN, G_SMAX})
      .widenScalarToNextPow2(0, /*Min=*/32)
      .lower();

  // integer addition/subtraction
  getActionDefinitionsBuilder({G_ADD, G_SUB})
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .legalFor(HasSSE2, {v16s8, v8s16, v4s32, v2s64})
      .legalFor(HasAVX2, {v32s8, v16s16, v8s32, v4s64})
      .legalFor(HasAVX512, {v16s32, v8s64})
      .legalFor(HasBWI, {v64s8, v32s16})
      .clampMinNumElements(0, s8, 16)
      .clampMinNumElements(0, s16, 8)
      .clampMinNumElements(0, s32, 4)
      .clampMinNumElements(0, s64, 2)
      .clampMaxNumElements(0, s8, HasBWI ? 64 : (HasAVX2 ? 32 : 16))
      .clampMaxNumElements(0, s16, HasBWI ? 32 : (HasAVX2 ? 16 : 8))
      .clampMaxNumElements(0, s32, HasAVX512 ? 16 : (HasAVX2 ? 8 : 4))
      .clampMaxNumElements(0, s64, HasAVX512 ? 8 : (HasAVX2 ? 4 : 2))
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s8, sMaxScalar)
      .scalarize(0);

  getActionDefinitionsBuilder({G_UADDE, G_UADDO, G_USUBE, G_USUBO})
      .legalFor({{s8, s8}, {s16, s8}, {s32, s8}})
      .legalFor(Is64Bit, {{s64, s8}})
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s8, sMaxScalar)
      .clampScalar(1, s8, s8)
      .scalarize(0);

  // integer multiply
  getActionDefinitionsBuilder(G_MUL)
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .legalFor(HasSSE2, {v8s16})
      .legalFor(HasSSE41, {v4s32})
      .legalFor(HasAVX2, {v16s16, v8s32})
      .legalFor(HasAVX512, {v16s32})
      .legalFor(HasDQI, {v8s64})
      .legalFor(HasDQI && HasVLX, {v2s64, v4s64})
      .legalFor(HasBWI, {v32s16})
      .clampMinNumElements(0, s16, 8)
      .clampMinNumElements(0, s32, 4)
      .clampMinNumElements(0, s64, HasVLX ? 2 : 8)
      .clampMaxNumElements(0, s16, HasBWI ? 32 : (HasAVX2 ? 16 : 8))
      .clampMaxNumElements(0, s32, HasAVX512 ? 16 : (HasAVX2 ? 8 : 4))
      .clampMaxNumElements(0, s64, 8)
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s8, sMaxScalar)
      .scalarize(0);

  getActionDefinitionsBuilder({G_SMULH, G_UMULH})
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s8, sMaxScalar)
      .scalarize(0);

  // integer divisions
  getActionDefinitionsBuilder({G_SDIV, G_SREM, G_UDIV, G_UREM})
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .libcallFor({s64})
      .clampScalar(0, s8, sMaxScalar);

  // integer shifts
  getActionDefinitionsBuilder({G_SHL, G_LSHR, G_ASHR})
      .legalFor({{s8, s8}, {s16, s8}, {s32, s8}})
      .legalFor(Is64Bit, {{s64, s8}})
      .clampScalar(0, s8, sMaxScalar)
      .clampScalar(1, s8, s8);

  // integer logic
  getActionDefinitionsBuilder({G_AND, G_OR, G_XOR})
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .legalFor(HasSSE2, {v16s8, v8s16, v4s32, v2s64})
      .legalFor(HasAVX, {v32s8, v16s16, v8s32, v4s64})
      .legalFor(HasAVX512, {v64s8, v32s16, v16s32, v8s64})
      .clampNumElements(0, v16s8, s8MaxVector)
      .clampNumElements(0, v8s16, s16MaxVector)
      .clampNumElements(0, v4s32, s32MaxVector)
      .clampNumElements(0, v2s64, s64MaxVector)
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s8, sMaxScalar)
      .scalarize(0);

  // integer comparison
  const std::initializer_list<LLT> IntTypes32 = {s8, s16, s32, p0};
  const std::initializer_list<LLT> IntTypes64 = {s8, s16, s32, s64, p0};

  getActionDefinitionsBuilder(G_ICMP)
      .legalForCartesianProduct({s8}, Is64Bit ? IntTypes64 : IntTypes32)
      .clampScalar(0, s8, s8)
      .widenScalarToNextPow2(1, /*Min=*/8)
      .clampScalar(1, s8, sMaxScalar);

  // bswap
  getActionDefinitionsBuilder(G_BSWAP)
      .legalFor({s32})
      .legalFor(Is64Bit, {s64})
      .widenScalarToNextPow2(0, /*Min=*/32)
      .clampScalar(0, s32, sMaxScalar);

  // popcount
  // Vector forms are only legal when a VPOPCNT instruction is available.
  // 512-bit forms only need AVX512VPOPCNTDQ; 128-bit and 256-bit forms also
  // need VLX. Other vector queries intentionally match no rule here and are
  // reported as unsupported rather than falling through to the scalar-only
  // rules below.
  getActionDefinitionsBuilder(G_CTPOP)
      .legalFor(HasPOPCNT, {{s16, s16}, {s32, s32}})
      .legalFor(HasPOPCNT && Is64Bit, {{s64, s64}})
      .legalFor(HasVPOPCNTDQ, {{v16s32, v16s32}, {v8s64, v8s64}})
      .legalFor(
          HasVPOPCNTDQVL,
          {{v4s32, v4s32}, {v8s32, v8s32}, {v2s64, v2s64}, {v4s64, v4s64}})
      .widenScalarToNextPow2(1, /*Min=*/16)
      .clampScalar(1, s16, sMaxScalar)
      .scalarSameSizeAs(0, 1);

  // count leading zeros (LZCNT)
  getActionDefinitionsBuilder(G_CTLZ)
      .legalFor(HasLZCNT, {{s16, s16}, {s32, s32}})
      .legalFor(HasLZCNT && Is64Bit, {{s64, s64}})
      .widenScalarToNextPow2(1, /*Min=*/16)
      .clampScalar(1, s16, sMaxScalar)
      .scalarSameSizeAs(0, 1);

  // count trailing zeros
  getActionDefinitionsBuilder(G_CTTZ_ZERO_POISON)
      .legalFor({{s16, s16}, {s32, s32}})
      .legalFor(Is64Bit, {{s64, s64}})
      .widenScalarToNextPow2(1, /*Min=*/16)
      .clampScalar(1, s16, sMaxScalar)
      .scalarSameSizeAs(0, 1);

  getActionDefinitionsBuilder(G_CTTZ)
      .legalFor(HasBMI, {{s16, s16}, {s32, s32}})
      .legalFor(HasBMI && Is64Bit, {{s64, s64}})
      .widenScalarToNextPow2(1, /*Min=*/16)
      .clampScalar(1, s16, sMaxScalar)
      .scalarSameSizeAs(0, 1);

  getActionDefinitionsBuilder(G_BR).alwaysLegal();
  getActionDefinitionsBuilder(G_BRCOND).legalFor({s1});

  // pointer handling
  const std::initializer_list<LLT> PtrTypes32 = {s1, s8, s16, s32};
  const std::initializer_list<LLT> PtrTypes64 = {s1, s8, s16, s32, s64};

  getActionDefinitionsBuilder(G_PTRTOINT)
      .legalForCartesianProduct(Is64Bit ? PtrTypes64 : PtrTypes32, {p0})
      .maxScalar(0, sMaxScalar)
      .widenScalarToNextPow2(0, /*Min*/ 8);

  getActionDefinitionsBuilder(G_INTTOPTR).legalFor({{p0, sMaxScalar}});

  getActionDefinitionsBuilder(G_CONSTANT_POOL).legalFor({p0});

  getActionDefinitionsBuilder(G_PTR_ADD)
      .legalFor({{p0, s32}})
      .legalFor(Is64Bit, {{p0, s64}})
      .widenScalarToNextPow2(1, /*Min*/ 32)
      .clampScalar(1, s32, sMaxScalar);

  getActionDefinitionsBuilder(G_FRAME_INDEX).legalFor({p0});

  getActionDefinitionsBuilder(G_GLOBAL_VALUE).customFor({p0});

  // load/store: add more corner cases
  for (unsigned Op : {G_LOAD, G_STORE}) {
    auto &Action = getActionDefinitionsBuilder(Op);
    Action.legalForTypesWithMemDesc({{s8, p0, s8, 1},
                                     {s16, p0, s16, 1},
                                     {s32, p0, s32, 1},
                                     {s80, p0, s80, 1},
                                     {p0, p0, p0, 1},
                                     {v4s8, p0, v4s8, 1}});
    if (Is64Bit)
      Action.legalForTypesWithMemDesc(
          {{s64, p0, s64, 1}, {v2s32, p0, v2s32, 1}});

    if (HasSSE1)
      Action.legalForTypesWithMemDesc({{v4s32, p0, v4s32, 1}});
    if (HasSSE2)
      Action.legalForTypesWithMemDesc({{v16s8, p0, v16s8, 1},
                                       {v8s16, p0, v8s16, 1},
                                       {v2s64, p0, v2s64, 1},
                                       {v2p0, p0, v2p0, 1}});
    if (HasAVX)
      Action.legalForTypesWithMemDesc({{v32s8, p0, v32s8, 1},
                                       {v16s16, p0, v16s16, 1},
                                       {v8s32, p0, v8s32, 1},
                                       {v4s64, p0, v4s64, 1},
                                       {v4p0, p0, v4p0, 1}});
    if (HasAVX512)
      Action.legalForTypesWithMemDesc({{v64s8, p0, v64s8, 1},
                                       {v32s16, p0, v32s16, 1},
                                       {v16s32, p0, v16s32, 1},
                                       {v8s64, p0, v8s64, 1}});

    // X86 supports extending loads but not stores for GPRs
    if (Op == G_LOAD) {
      Action.legalForTypesWithMemDesc({{s8, p0, s1, 1},
                                       {s16, p0, s8, 1},
                                       {s32, p0, s8, 1},
                                       {s32, p0, s16, 1}});
      if (Is64Bit)
        Action.legalForTypesWithMemDesc(
            {{s64, p0, s8, 1}, {s64, p0, s16, 1}, {s64, p0, s32, 1}});
    } else {
      Action.customIf([=](const LegalityQuery &Query) {
        return Query.Types[0] != Query.MMODescrs[0].MemoryTy;
      });
    }
    Action.widenScalarToNextPow2(0, /*Min=*/8)
        .clampScalar(0, s8, sMaxScalar)
        .scalarize(0);
  }

  for (unsigned Op : {G_SEXTLOAD, G_ZEXTLOAD}) {
    auto &Action = getActionDefinitionsBuilder(Op);
    Action.legalForTypesWithMemDesc(
        {{s16, p0, s8, 1}, {s32, p0, s8, 1}, {s32, p0, s16, 1}});
    if (Is64Bit)
      Action.legalForTypesWithMemDesc(
          {{s64, p0, s8, 1}, {s64, p0, s16, 1}, {s64, p0, s32, 1}});
    // TODO - SSE41/AVX2/AVX512F/AVX512BW vector extensions
  }

  for (unsigned Op : {G_FPEXTLOAD, G_FPTRUNCSTORE}) {
    auto &Action = getActionDefinitionsBuilder(Op);
    Action.legalForTypesWithMemDesc(
        UseX87, {{s80, p0, s32, 1}, {s80, p0, s64, 1}, {s64, p0, s32, 1}});
  }

  // sext, zext, and anyext
  getActionDefinitionsBuilder(G_ANYEXT)
      .legalFor({s8, s16, s32, s128})
      .legalFor(Is64Bit, {s64})
      .widenScalarToNextPow2(0, /*Min=*/8)
      .clampScalar(0, s8, sMaxScalar)
      .widenScalarToNextPow2(1, /*Min=*/8)
      .clampScalar(1, s8, sMaxScalar)
      .scalarize(0);

  getActionDefinitionsBuilder({G_SEXT, G_ZEXT})
      .legalFor({s8, s16, s32})
      .legalFor(Is64Bit, {s64})
      .widenScalarToNextPow2(0, /*Min=*/8)
      .clampScalar(0, s8, sMaxScalar)
      .widenScalarToNextPow2(1, /*Min=*/8)
      .clampScalar(1, s8, sMaxScalar)
      .scalarize(0);

  getActionDefinitionsBuilder(G_TRUNC).legalForCartesianProduct(
      {s1, s8, s16, s32, s64}, {s8, s16, s32, s64, s128});

  getActionDefinitionsBuilder(G_SEXT_INREG).lower();

  // fp constants
  getActionDefinitionsBuilder(G_FCONSTANT)
      .legalFor({s32, s64})
      .legalFor(UseX87, {s80});

  // fp arithmetic
  getActionDefinitionsBuilder({G_FADD, G_FSUB, G_FMUL, G_FDIV})
      .legalFor({s32, s64})
      .legalFor(HasSSE1, {v4s32})
      .legalFor(HasSSE2, {v2s64})
      .legalFor(HasAVX, {v8s32, v4s64})
      .legalFor(HasAVX512, {v16s32, v8s64})
      .legalFor(UseX87, {s80});

  getActionDefinitionsBuilder(G_FABS)
      .legalFor(UseX87, {s80})
      .legalFor(UseX87 && !Is64Bit, {s64})
      .lower();

  // fp comparison
  getActionDefinitionsBuilder(G_FCMP)
      .legalFor(HasSSE1 || UseX87, {s8, s32})
      .legalFor(HasSSE2 || UseX87, {s8, s64})
      .legalFor(UseX87, {s8, s80})
      .clampScalar(0, s8, s8)
      .clampScalar(1, s32, HasSSE2 ? s64 : s32)
      .widenScalarToNextPow2(1);

  // fp conversions
  getActionDefinitionsBuilder(G_FPEXT)
      .legalFor(HasSSE2, {{s64, s32}})
      .legalFor(HasAVX, {{v4s64, v4s32}})
      .legalFor(HasAVX512, {{v8s64, v8s32}})
      .lowerFor(UseX87, {{s64, s32}, {s80, s32}, {s80, s64}})
      .libcall();

  getActionDefinitionsBuilder(G_FPTRUNC)
      .legalFor(HasSSE2, {{s32, s64}})
      .legalFor(HasAVX, {{v4s32, v4s64}})
      .legalFor(HasAVX512, {{v8s32, v8s64}})
      .lowerFor(UseX87, {{s32, s64}, {s32, s80}, {s64, s80}});

  getActionDefinitionsBuilder(G_SITOFP)
      .legalFor(HasSSE1, {{s32, s32}})
      .legalFor(HasSSE1 && Is64Bit, {{s32, s64}})
      .legalFor(HasSSE2, {{s64, s32}})
      .legalFor(HasSSE2 && Is64Bit, {{s64, s64}})
      .clampScalar(1, (UseX87 && !HasSSE1) ? s16 : s32, sMaxScalar)
      .widenScalarToNextPow2(1)
      .customForCartesianProduct(UseX87, {s32, s64, s80}, {s16, s32, s64})
      .clampScalar(0, s32, HasSSE2 ? s64 : s32)
      .widenScalarToNextPow2(0);

  getActionDefinitionsBuilder(G_FPTOSI)
      .legalFor(HasSSE1, {{s32, s32}})
      .legalFor(HasSSE1 && Is64Bit, {{s64, s32}})
      .legalFor(HasSSE2, {{s32, s64}})
      .legalFor(HasSSE2 && Is64Bit, {{s64, s64}})
      .clampScalar(0, (UseX87 && !HasSSE1) ? s16 : s32, sMaxScalar)
      .widenScalarToNextPow2(0)
      .customForCartesianProduct(UseX87, {s16, s32, s64}, {s32, s64, s80})
      .clampScalar(1, s32, HasSSE2 ? s64 : s32)
      .widenScalarToNextPow2(1);

  // For G_UITOFP and G_FPTOUI without AVX512, we have to custom legalize types
  // <= s32 manually. Otherwise, in custom handler there is no way to
  // understand whether s32 is an original type and we need to promote it to
  // s64 or s32 is obtained after widening and we shouldn't widen it to s64.
  //
  // For AVX512 we simply widen types as there is direct mapping from opcodes
  // to asm instructions.
  getActionDefinitionsBuilder(G_UITOFP)
      .legalFor(HasAVX512, {{s32, s32}, {s32, s64}, {s64, s32}, {s64, s64}})
      .customIf([=](const LegalityQuery &Query) {
        return !HasAVX512 &&
               ((HasSSE1 && typeIs(0, s32)(Query)) ||
                (HasSSE2 && typeIs(0, s64)(Query))) &&
               scalarNarrowerThan(1, Is64Bit ? 64 : 32)(Query);
      })
      .lowerIf([=](const LegalityQuery &Query) {
        // Lower conversions from s64
        return !HasAVX512 &&
               ((HasSSE1 && typeIs(0, s32)(Query)) ||
                (HasSSE2 && typeIs(0, s64)(Query))) &&
               (Is64Bit && typeIs(1, s64)(Query));
      })
      .clampScalar(0, s32, HasSSE2 ? s64 : s32)
      .widenScalarToNextPow2(0)
      .clampScalar(1, s32, sMaxScalar)
      .widenScalarToNextPow2(1);

  getActionDefinitionsBuilder(G_FPTOUI)
      .legalFor(HasAVX512, {{s32, s32}, {s32, s64}, {s64, s32}, {s64, s64}})
      .customIf([=](const LegalityQuery &Query) {
        return !HasAVX512 &&
               ((HasSSE1 && typeIs(1, s32)(Query)) ||
                (HasSSE2 && typeIs(1, s64)(Query))) &&
               scalarNarrowerThan(0, Is64Bit ? 64 : 32)(Query);
      })
      // TODO: replace with customized legalization using
      // specifics of cvttsd2si. The selection of this node requires
      // a vector type. Either G_SCALAR_TO_VECTOR is needed or more advanced
      // support of G_BUILD_VECTOR/G_INSERT_VECTOR_ELT is required beforehand.
      .lowerIf([=](const LegalityQuery &Query) {
        return !HasAVX512 &&
               ((HasSSE1 && typeIs(1, s32)(Query)) ||
                (HasSSE2 && typeIs(1, s64)(Query))) &&
               (Is64Bit && typeIs(0, s64)(Query));
      })
      .clampScalar(0, s32, sMaxScalar)
      .widenScalarToNextPow2(0)
      .clampScalar(1, s32, HasSSE2 ? s64 : s32)
      .widenScalarToNextPow2(1);

  // vector ops
  getActionDefinitionsBuilder(G_BUILD_VECTOR)
      .customIf([=](const LegalityQuery &Query) {
        return (HasSSE1 && typeInSet(0, {v4s32})(Query)) ||
               (HasSSE2 && typeInSet(0, {v2s64, v8s16, v16s8})(Query)) ||
               (HasAVX && typeInSet(0, {v4s64, v8s32, v16s16, v32s8})(Query)) ||
               (HasAVX512 &&
                typeInSet(0, {v8s64, v16s32, v32s16, v64s8})(Query));
      })
      .clampNumElements(0, v16s8, s8MaxVector)
      .clampNumElements(0, v8s16, s16MaxVector)
      .clampNumElements(0, v4s32, s32MaxVector)
      .clampNumElements(0, v2s64, s64MaxVector)
      .moreElementsToNextPow2(0);

  getActionDefinitionsBuilder({G_EXTRACT, G_INSERT})
      .legalIf([=](const LegalityQuery &Query) {
        unsigned SubIdx = Query.Opcode == G_EXTRACT ? 0 : 1;
        unsigned FullIdx = Query.Opcode == G_EXTRACT ? 1 : 0;
        return (HasAVX && typePairInSet(SubIdx, FullIdx,
                                        {{v16s8, v32s8},
                                         {v8s16, v16s16},
                                         {v4s32, v8s32},
                                         {v2s64, v4s64}})(Query)) ||
               (HasAVX512 && typePairInSet(SubIdx, FullIdx,
                                           {{v16s8, v64s8},
                                            {v32s8, v64s8},
                                            {v8s16, v32s16},
                                            {v16s16, v32s16},
                                            {v4s32, v16s32},
                                            {v8s32, v16s32},
                                            {v2s64, v8s64},
                                            {v4s64, v8s64}})(Query));
      });

  // todo: only permit dst types up to max legal vector register size?
  getActionDefinitionsBuilder(G_CONCAT_VECTORS)
      .legalFor(
          HasSSE1,
          {{v32s8, v16s8}, {v16s16, v8s16}, {v8s32, v4s32}, {v4s64, v2s64}})
      .legalFor(HasAVX, {{v64s8, v16s8},
                         {v64s8, v32s8},
                         {v32s16, v8s16},
                         {v32s16, v16s16},
                         {v16s32, v4s32},
                         {v16s32, v8s32},
                         {v8s64, v2s64},
                         {v8s64, v4s64}});

  // todo: vectors and address spaces
  getActionDefinitionsBuilder(G_SELECT)
      .legalFor({{s16, s32}, {s32, s32}, {p0, s32}})
      .legalFor(!HasCMOV, {{s8, s32}})
      .legalFor(Is64Bit, {{s64, s32}})
      .legalFor(UseX87, {{s80, s32}})
      .clampScalar(1, s32, s32)
      .widenScalarToNextPow2(0, /*Min=*/8)
      .clampScalar(0, HasCMOV ? s16 : s8, sMaxScalar);

  // memory intrinsics
  getActionDefinitionsBuilder({G_MEMCPY, G_MEMMOVE, G_MEMSET}).libcall();

  getActionDefinitionsBuilder({G_DYN_STACKALLOC, G_STACKSAVE, G_STACKRESTORE})
      .lower();

  // fp intrinsics
  // fpclass for i686 is disabled for llvm issue #171992
  getActionDefinitionsBuilder(G_IS_FPCLASS)
      .lowerFor(Is64Bit, {{s1, s32}, {s1, s64}, {s1, s80}});

  getActionDefinitionsBuilder({G_INTRINSIC_ROUNDEVEN, G_INTRINSIC_TRUNC})
      .scalarize(0)
      .minScalar(0, LLT::scalar(32))
      .libcall();

  getActionDefinitionsBuilder({G_INTRINSIC, G_INTRINSIC_W_SIDE_EFFECTS})
      .alwaysLegal();
  getActionDefinitionsBuilder({G_TRAP, G_DEBUGTRAP, G_UBSANTRAP}).alwaysLegal();
  getActionDefinitionsBuilder(G_INVOKE_REGION_START).alwaysLegal();
}
