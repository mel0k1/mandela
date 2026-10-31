#!/usr/bin/env python3

import mc_x64
import mc_a64

X64_R = {"R0": mc_x64.RAX, "R1": mc_x64.RCX, "R2": mc_x64.RDX, "R3": mc_x64.RSI,
         "R4": mc_x64.RDI, "R5": mc_x64.R8, "R6": mc_x64.R9, "R7": mc_x64.R10,
         "R8": mc_x64.R11}
X64_F = {"F%d" % i: i for i in range(8)}

A64_R = {"R%d" % i: i for i in range(14)}
A64_F = {"F%d" % i: i for i in range(8)}

X64_ABI = {"A0": "R4", "A1": "R3", "A2": "R2", "A3": "R1", "A4": "R5",
           "A5": "R6", "RET": "R0", "RET2": "R2", "SYSNO": "R0"}
A64_ABI = {"A%d" % i: "R%d" % i for i in range(6)}
A64_ABI.update({"RET": "R0", "RET2": "R1", "SYSNO": "R8"})

X64_ARG_I = ["A0", "A1", "A2", "A3", "A4", "A5"]
A64_ARG_I = ["A%d" % i for i in range(8)]
X64_ARG_F = ["F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7"]
A64_ARG_F = ["F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7"]

X64_SETC = {"e": mc_x64.SE_E, "ne": mc_x64.SE_NE, "l": mc_x64.SE_L,
            "le": mc_x64.SE_LE, "g": mc_x64.SE_G, "ge": mc_x64.SE_GE,
            "b": mc_x64.SE_B, "be": mc_x64.SE_BE, "a": mc_x64.SE_A,
            "ae": mc_x64.SE_AE, "p": mc_x64.SE_P, "np": mc_x64.SE_NP}
X64_JC = {"e": mc_x64.JB_E, "ne": mc_x64.JB_NE, "l": mc_x64.JB_L,
          "le": mc_x64.JB_LE, "g": mc_x64.JB_G, "ge": mc_x64.JB_GE,
          "b": mc_x64.JB_B, "be": mc_x64.JB_BE, "a": mc_x64.JB_A,
          "ae": mc_x64.JB_AE, "p": mc_x64.JB_P, "np": mc_x64.JB_NP}

A64_CC = {"e": mc_a64.EQ, "ne": mc_a64.NE, "l": mc_a64.LT, "le": mc_a64.LE,
          "g": mc_a64.GT, "ge": mc_a64.GE, "b": mc_a64.CC, "be": mc_a64.LS,
          "a": mc_a64.HI, "ae": mc_a64.CS, "vs": mc_a64.VS, "vc": mc_a64.VC,
          "mi": mc_a64.MI, "pl": mc_a64.PL}

class LoweringError(Exception):
    pass

def _xreg(name):
    name = X64_ABI.get(name, name)
    if name == "SP":
        return mc_x64.RSP
    if name == "FREG":
        return mc_x64.RBP
    if name in X64_R:
        return X64_R[name]
    raise LoweringError("unknown x86-64 register %r" % name)

def _areg(name):
    name = A64_ABI.get(name, name)
    if name == "SP":
        return 31
    if name == "FREG":
        return mc_a64.FP
    if name in A64_R:
        return A64_R[name]
    raise LoweringError("unknown ARM64 register %r" % name)

def _uniq(a):
    n = getattr(a, "_uniq", 0) + 1
    a._uniq = n
    return "..u%d" % n

def emit_x64(a, op):
    k = op[0]
    if k == "label":
        a.label(op[1])
    elif k in ("set", "set64"):
        if isinstance(op[2], str):
            d, sv = _xreg(op[1]), _xreg(op[2])
            if d != sv:
                a.mov_rr(d, sv)
        else:
            a.mov_ir(_xreg(op[1]), op[2])
    elif k == "mov":
        d, s = _xreg(op[1]), _xreg(op[2])
        if d != s:
            a.mov_rr(d, s)
    elif k == "addr":
        a.mov_addr(_xreg(op[1]), op[2])
    elif k == "load":
        d, b = _xreg(op[1]), _xreg(op[2])
        if b == mc_x64.RSP:

            a.mov_rr(d, mc_x64.RSP)
            a.mov_rm(d, d, op[3])
        else:
            a.mov_rm(d, b, op[3])
    elif k == "store":
        b = _xreg(op[2])
        if b == mc_x64.RSP:
            scr = "R7" if op[1] == "R8" else "R8"
            a.mov_rr(_xreg(scr), mc_x64.RSP)
            a.mov_mr(_xreg(scr), op[3], _xreg(op[1]))
        else:
            a.mov_mr(b, op[3], _xreg(op[1]))
    elif k == "load8":
        r, b = _xreg(op[1]), _xreg(op[2])
        rex = 0x48 | (mc_x64.REX_R if r >= 8 else 0) | (mc_x64.REX_B if b >= 8 else 0)
        a.byte(rex, 0x0F, 0xB6)
        a.modrm(2, r, b)
        a.imm32(op[3] & 0xFFFFFFFF)
    elif k == "store8":
        r, b = _xreg(op[1]), _xreg(op[2])
        if r >= 8 or b >= 8:
            a.byte(0x40 | (mc_x64.REX_R if r >= 8 else 0) |
                   (mc_x64.REX_B if b >= 8 else 0))
        a.byte(0x88)
        a.modrm(2, r, b)
        a.imm32(op[3] & 0xFFFFFFFF)
    elif k in ("add", "sub", "and", "or", "xor", "mul"):
        d, s1 = _xreg(op[1]), _xreg(op[2])
        comm = k in ("add", "and", "or", "xor", "mul")
        if isinstance(op[3], int):
            if d != s1:
                a.mov_rr(d, s1)
            if k == "mul":
                a.imul_ir(d, op[3])
            else:
                {"add": a.add_ir, "sub": a.sub_ir, "and": a.and_ir,
                 "or": a.or_ir, "xor": a.xor_ir}[k](d, op[3])
        else:
            s2 = _xreg(op[3])
            if d == s2 and d != s1:

                if comm:
                    {"add": a.add_rr, "and": a.and_rr, "or": a.or_rr,
                     "xor": a.xor_rr, "mul": a.imul_rr}[k](d, s1)
                else:
                    a.neg_r(d)
                    a.add_rr(d, s1)
            else:
                if d != s1:
                    a.mov_rr(d, s1)
                {"add": a.add_rr, "sub": a.sub_rr, "and": a.and_rr,
                 "or": a.or_rr, "xor": a.xor_rr, "mul": a.imul_rr}[k](d, s2)
    elif k in ("shl", "shr", "sar"):
        d = _xreg(op[1])
        if d != _xreg(op[2]):
            a.mov_rr(d, _xreg(op[2]))
        (a.shl_ir if k == "shl" else a.shr_ir if k == "shr" else a.sar_ir)(d, op[3])
    elif k in ("shlv", "shrv", "sarv"):
        d, ra, ramt = _xreg(op[1]), _xreg(op[2]), _xreg(op[3])
        if d != ra:
            a.mov_rr(d, ra)
        if ramt != mc_x64.RCX:
            a.mov_rr(mc_x64.RCX, ramt)
        (a.shl_cl if k == "shlv" else a.shr_cl if k == "shrv" else a.sar_cl)(d)
    elif k == "neg":
        d = _xreg(op[1])
        if d != _xreg(op[2]):
            a.mov_rr(d, _xreg(op[2]))
        a.neg_r(d)
    elif k in ("div", "rem"):
        ra, rb = _xreg(op[2]), _xreg(op[3])
        if rb == mc_x64.RDX:
            a.mov_rr(mc_x64.R11, rb)
            rb = mc_x64.R11
        if ra != mc_x64.RAX:
            a.mov_rr(mc_x64.RAX, ra)
        a.cqo()
        a.idiv_r(rb)
        d = _xreg(op[1])
        src = mc_x64.RAX if k == "div" else mc_x64.RDX
        if d != src:
            a.mov_rr(d, src)
    elif k in ("cset", "cseti"):
        d = _xreg(op[1])
        if k == "cset":
            a.cmp_rr(_xreg(op[2]), _xreg(op[3]))
        else:
            a.cmp_ir(_xreg(op[2]), op[3])
        a.setcc(X64_SETC[op[4]], mc_x64.RCX)
        a.movzx_r8(d, mc_x64.RCX)
    elif k == "jmp":
        a.jmp(op[1])
    elif k == "cj":
        a.cmp_rr(_xreg(op[1]), _xreg(op[2]))
        a.jcc(X64_JC[op[3]], op[4])
    elif k == "cji":
        a.cmp_ir(_xreg(op[1]), op[2])
        a.jcc(X64_JC[op[3]], op[4])
    elif k == "tj":
        a.test_rr(_xreg(op[1]), _xreg(op[1]))
        a.jcc(X64_JC["ne"], op[2])
    elif k == "call":
        a.call(op[1])
    elif k == "ret":
        a.ret()
    elif k == "udivmod":

        ra, rb_orig = _xreg(op[3]), _xreg(op[4])
        rb = rb_orig
        if rb == mc_x64.RDX:
            a.mov_rr(mc_x64.R11, rb)
            rb = mc_x64.R11
        if ra != mc_x64.RAX:
            a.mov_rr(mc_x64.RAX, ra)
        a.xor_rr(mc_x64.RDX, mc_x64.RDX)
        a.udiv_r(rb)
        rq, rr = _xreg(op[1]), _xreg(op[2])
        rr_done = False
        if rr != mc_x64.RDX and rq == mc_x64.RDX:
            a.mov_rr(rr, mc_x64.RDX)
            rr_done = True
        if rq != mc_x64.RAX:
            a.mov_rr(rq, mc_x64.RAX)
        if rr_done:
            pass
        elif rr != mc_x64.RDX:
            a.mov_rr(rr, mc_x64.RDX)
        else:
            a.mov_rr(mc_x64.R11, mc_x64.RDX)
        if rb_orig == mc_x64.RDX and rq != mc_x64.RDX and rr != mc_x64.RDX:
            a.mov_rr(mc_x64.RDX, mc_x64.R11)
    elif k == "msb":

        a.bsr(_xreg(op[1]), _xreg(op[2]))
    elif k == "mulu128":

        ra, rb = _xreg(op[3]), _xreg(op[4])
        if ra != mc_x64.RAX:
            a.mov_rr(mc_x64.RAX, ra)
        a.mul_r(rb)
        hi, lo = _xreg(op[1]), _xreg(op[2])
        if hi != mc_x64.RDX:
            a.mov_rr(hi, mc_x64.RDX)
        if lo != mc_x64.RAX:
            a.mov_rr(lo, mc_x64.RAX)
    elif k == "fset":
        a.mov_addr(mc_x64.R10, op[2])
        a.movsd_rm(X64_F[op[1]], mc_x64.R10, 0)
    elif k == "fmov":
        a.movsd_rr(X64_F[op[1]], X64_F[op[2]])
    elif k == "fload":
        b = _xreg(op[2])
        if b == mc_x64.RSP:
            a.mov_rr(mc_x64.R10, mc_x64.RSP)
            a.movsd_rm(X64_F[op[1]], mc_x64.R10, op[3])
        else:
            a.movsd_rm(X64_F[op[1]], b, op[3])
    elif k == "fstore":
        b = _xreg(op[2])
        if b == mc_x64.RSP:
            a.mov_rr(mc_x64.R10, mc_x64.RSP)
            a.movsd_mr(mc_x64.R10, op[3], X64_F[op[1]])
        else:
            a.movsd_mr(b, op[3], X64_F[op[1]])
    elif k in ("fadd", "fsub", "fmul", "fdiv"):
        d, s2 = X64_F[op[1]], X64_F[op[3]]
        if d != X64_F[op[2]]:
            a.movsd_rr(d, X64_F[op[2]])
        {"fadd": a.addsd, "fsub": a.subsd,
         "fmul": a.mulsd, "fdiv": a.divsd}[k](d, s2)
    elif k in ("fneg", "fabs", "fneg_f", "fabs_f"):
        d, s = X64_F[op[1]], X64_F[op[2]]
        neg = k in ("fneg", "fneg_f")
        o_ = "fcm_sign" if neg else "fcm_abs"
        a.mov_addr(mc_x64.R10, o_)
        a.movsd_rm(X64_F["F7"], mc_x64.R10, 0)
        if d != s:
            a.movsd_rr(d, s)
        (a.xorpd if neg else a.andpd)(d, X64_F["F7"])
    elif k in ("ffloor", "fceil", "ftrunc"):
        d, s = X64_F[op[1]], X64_F[op[2]]
        if d != s:
            a.movsd_rr(d, s)
        a.roundsd(d, d, 1 if k == "ffloor" else 2 if k == "fceil" else 3)
    elif k == "fround":

        d, s = X64_F[op[1]], X64_F[op[2]]
        done = _uniq(a) + "d"
        addit = _uniq(a) + "a"
        a.movsd_rr(X64_F[5], s)
        if d != s:
            a.movsd_rr(d, s)
        a.roundsd(d, d, 3)
        a.movsd_rr(X64_F[6], d)
        a.subsd(X64_F[5], X64_F[6])
        a.mov_addr(mc_x64.R10, "fcm_abs")
        a.movsd_rm(X64_F[7], mc_x64.R10, 0)
        a.andpd(X64_F[5], X64_F[7])
        a.mov_addr(mc_x64.R10, "fcm_half")
        a.movsd_rm(X64_F[7], mc_x64.R10, 0)
        a.ucomisd(X64_F[5], X64_F[7])
        a.jcc(X64_JC["be"], done)
        a.movq_gx(mc_x64.R10, s)
        a.shr_ir(mc_x64.R10, 63)
        a.test_rr(mc_x64.R10, mc_x64.R10)
        a.mov_addr(mc_x64.RCX, "fcm_none")
        a.movsd_rm(X64_F[5], mc_x64.RCX, 0)
        a.jcc(X64_JC["e"], addit)
        a.mov_addr(mc_x64.RCX, "fcm_one")
        a.movsd_rm(X64_F[5], mc_x64.RCX, 0)
        a.label(addit)
        a.addsd(d, X64_F[5])
        a.label(done)
    elif k == "fsqrt":
        d, s = X64_F[op[1]], X64_F[op[2]]
        if d != s:
            a.movsd_rr(d, s)
        a.sqrtsd(d, d)
    elif k == "ftoi":
        a.cvttsd2si(_xreg(op[1]), X64_F[op[2]])
    elif k == "itof":
        a.cvtsi2sd(X64_F[op[1]], _xreg(op[2]))
    elif k == "fgpr":
        a.movq_gx(_xreg(op[1]), X64_F[op[2]])
    elif k == "gprf":
        a.movq_xg(X64_F[op[1]], _xreg(op[2]))
    elif k == "fj":
        fa, fb, cc, lbl = X64_F[op[1]], X64_F[op[2]], op[3], op[4]
        if cc == "eq":
            a.ucomisd(fa, fb)
            a.jcc(X64_JC["p"], _uniq_done(a, lbl, "eq"))
        elif cc == "ne":
            a.ucomisd(fa, fb)
            a.jcc(X64_JC["p"], lbl)
            a.jcc(X64_JC["ne"], lbl)
        elif cc == "lt":
            a.ucomisd(fb, fa)
            a.jcc(X64_JC["a"], lbl)
        elif cc == "le":
            a.ucomisd(fb, fa)
            a.jcc(X64_JC["ae"], lbl)
        elif cc == "gt":
            a.ucomisd(fa, fb)
            a.jcc(X64_JC["a"], lbl)
        else:
            a.ucomisd(fa, fb)
            a.jcc(X64_JC["ae"], lbl)
    elif k == "fcset":
        d = _xreg(op[1])
        fa, fb, cc = X64_F[op[2]], X64_F[op[3]], op[4]
        if cc == "eq":
            a.ucomisd(fa, fb)
            a.setcc(X64_SETC["e"], mc_x64.RCX)
            a.movzx_r8(mc_x64.R8, mc_x64.RCX)
            a.setcc(X64_SETC["np"], mc_x64.RCX)
            a.movzx_r8(mc_x64.RCX, mc_x64.RCX)
            a.and_rr(mc_x64.RCX, mc_x64.R8)
            a.mov_rr(d, mc_x64.RCX)
        elif cc == "ne":
            a.ucomisd(fa, fb)
            a.setcc(X64_SETC["ne"], mc_x64.RCX)
            a.movzx_r8(mc_x64.R8, mc_x64.RCX)
            a.setcc(X64_SETC["p"], mc_x64.RCX)
            a.movzx_r8(mc_x64.RCX, mc_x64.RCX)
            a.or_rr(mc_x64.RCX, mc_x64.R8)
            a.mov_rr(d, mc_x64.RCX)
        else:
            if cc == "lt":
                a.ucomisd(fb, fa)
                sop = X64_SETC["a"]
            elif cc == "le":
                a.ucomisd(fb, fa)
                sop = X64_SETC["ae"]
            elif cc == "gt":
                a.ucomisd(fa, fb)
                sop = X64_SETC["a"]
            else:
                a.ucomisd(fa, fb)
                sop = X64_SETC["ae"]
            a.setcc(sop, mc_x64.RCX)
            a.movzx_r8(d, mc_x64.RCX)
    elif k == "fjisnan":
        a.ucomisd(X64_F[op[1]], X64_F[op[1]])
        a.jcc(X64_JC["p"], op[2])
    elif k == "push_rbp":
        a.push_r(mc_x64.RBP)
    elif k == "pop_rbp":
        a.pop_r(mc_x64.RBP)
    elif k == "store_lr" or k == "load_lr":
        pass
    elif k == "subsp":
        a.sub_ir(mc_x64.RSP, op[1])
    elif k == "addsp":
        a.add_ir(mc_x64.RSP, op[1])
    elif k == "spaddr":

        a.mov_rr(_xreg(op[1]), mc_x64.RSP)
        if op[2]:
            a.add_ir(_xreg(op[1]), op[2])
    elif k == "sysno":
        a.mov_ir(mc_x64.RAX, {"write": 1, "exit": 231, "mmap": 9}[op[1]])
    elif k == "syscall":
        a.mov_rr(mc_x64.R10, mc_x64.RCX)
        a.syscall()
    elif k == "int3":
        a.byte(0xCC)
    else:
        raise LoweringError("unknown x86-64 op %r" % (op,))

def _uniq_done(a, lbl, tag):
    notl = _uniq(a) + tag
    a.jcc(X64_JC["p"], notl)
    a.jcc(X64_JC["e"], lbl)
    a.label(notl)
    return notl

def emit_a64(a, op):
    k = op[0]
    if k == "label":
        a.label(op[1])
    elif k in ("set", "set64"):
        if isinstance(op[2], str):
            d, sv = _areg(op[1]), _areg(op[2])
            if d != sv:
                a.mov_rr(d, sv)
        else:
            a.mov_ir(_areg(op[1]), op[2])
    elif k == "mov":
        d, sv = _areg(op[1]), _areg(op[2])
        if op[2] == "SP" and op[1] != "SP":
            a.add_imm(d, 31, 0)
        elif op[1] == "SP" and op[2] != "SP":
            a.add_imm(31, sv, 0)
        elif d != sv:
            a.mov_rr(d, sv)
    elif k == "addr":

        a.adr(_areg(op[1]), op[2])
    elif k == "load":
        a.mov_rm(_areg(op[1]), _areg(op[2]), op[3])
    elif k == "store":
        a.mov_mr(_areg(op[2]), op[3], _areg(op[1]))
    elif k == "load8":
        a.ldrb(_areg(op[1]) & 31, _areg(op[2]) & 31, op[3])
    elif k == "store8":
        a.strb(_areg(op[1]) & 31, _areg(op[2]) & 31, op[3])
    elif k in ("add", "sub", "and", "or", "xor", "mul"):
        d, ra = _areg(op[1]), _areg(op[2])
        if isinstance(op[3], int):
            v = op[3]
            if k in ("add", "sub") and 0 <= v < 0x1000:
                (a.add_imm if k == "add" else a.sub_imm)(d, ra, v)
            else:
                a.mov_ir(13, v)
                rb = 13
                {"add": a.add_rr, "sub": a.sub_rr, "and": a.and_rr,
                 "or": a.orr_rr, "xor": a.eor_rr, "mul": a.mul_rr}[k](d, ra, rb)
        else:
            rb = _areg(op[3])
            {"add": a.add_rr, "sub": a.sub_rr, "and": a.and_rr,
             "or": a.orr_rr, "xor": a.eor_rr, "mul": a.mul_rr}[k](d, ra, rb)
    elif k in ("shl", "shr", "sar"):
        d, rn = _areg(op[1]), _areg(op[2])
        (a.shl_ir if k == "shl" else a.shr_ir if k == "shr" else a.sar_ir)(
            d, rn, op[3])
    elif k in ("shlv", "shrv", "sarv"):
        d, ra, ramt = _areg(op[1]), _areg(op[2]), _areg(op[3])
        (a.lslv if k == "shlv" else a.lsrv if k == "shrv" else a.asrv)(
            d, ra, ramt)
    elif k == "neg":
        a.neg_r(_areg(op[1]), _areg(op[2]))
    elif k == "div":
        a.sdiv(_areg(op[1]), _areg(op[2]), _areg(op[3]))
    elif k == "rem":
        ra, rb, d = _areg(op[2]), _areg(op[3]), _areg(op[1])
        a.sdiv(12, ra, rb)
        a.msub(d, 12, rb, ra)
    elif k in ("cset", "cseti"):
        d = _areg(op[1])
        if k == "cset":
            a.cmp_rr(_areg(op[2]), _areg(op[3]))
        elif 0 <= op[3] < 0x1000:
            a.cmp_imm(_areg(op[2]), op[3])
        else:
            a.mov_ir(13, op[3])
            a.cmp_rr(_areg(op[2]), 13)
        a.cset(d, A64_CC[op[4]])
    elif k == "jmp":
        a.b(op[1])
    elif k == "cj":
        a.cmp_rr(_areg(op[1]), _areg(op[2]))
        a.bcond(A64_CC[op[3]], op[4])
    elif k == "cji":
        if 0 <= op[2] < 0x1000:
            a.cmp_imm(_areg(op[1]), op[2])
        else:
            a.mov_ir(13, op[2])
            a.cmp_rr(_areg(op[1]), 13)
        a.bcond(A64_CC[op[3]], op[4])
    elif k == "tj":
        a.cmp_imm(_areg(op[1]), 0)
        a.bcond(A64_CC["ne"], op[2])
    elif k == "call":
        a.bl(op[1])
    elif k == "ret":
        a.ret()
    elif k == "udivmod":
        ra, rb = _areg(op[3]), _areg(op[4])
        rq, rr = _areg(op[1]), _areg(op[2])
        a.udiv(rq, ra, rb)
        a.msub(rr, rq, rb, ra)
    elif k == "msb":
        a.clz(_areg(op[1]), _areg(op[2]))
        a.mov_ir(_areg("R9"), 63)
        a.sub_rr(_areg(op[1]), _areg("R9"), _areg(op[1]))
    elif k == "mulu128":
        ra, rb = _areg(op[3]), _areg(op[4])
        a.mul_rr(_areg(op[2]), ra, rb)
        a.umulh(_areg(op[1]), ra, rb)
    elif k == "fset":
        a.adr(_areg("R9"), op[2])
        a.fmov_rm(A64_F[op[1]], _areg("R9"), 0)
    elif k == "fmov":
        a.fmov_rr(A64_F[op[1]], A64_F[op[2]])
    elif k in ("fadd", "fsub", "fmul", "fdiv"):
        d, fa, fb = A64_F[op[1]], A64_F[op[2]], A64_F[op[3]]
        {"fadd": a.fadd, "fsub": a.fsub,
         "fmul": a.fmul, "fdiv": a.fdiv}[k](d, fa, fb)
    elif k in ("fneg", "fabs", "fneg_f", "fabs_f"):
        (a.fneg_f if k in ("fneg", "fneg_f") else a.fabs_f)(A64_F[op[1]], A64_F[op[2]])
    elif k == "ffloor":
        a.frintm(A64_F[op[1]], A64_F[op[2]])
    elif k == "fceil":
        a.frintp(A64_F[op[1]], A64_F[op[2]])
    elif k == "ftrunc":
        a.frintz(A64_F[op[1]], A64_F[op[2]])
    elif k == "fround":
        a.frinta(A64_F[op[1]], A64_F[op[2]])
    elif k == "fsqrt":
        a.fsqrt(A64_F[op[1]], A64_F[op[2]])
    elif k == "ftoi":
        a.fcvtzs(_areg(op[1]), A64_F[op[2]])
    elif k == "itof":
        a.scvtf(A64_F[op[1]], _areg(op[2]))
    elif k == "fgpr":
        a.fmov_xg(_areg(op[1]), A64_F[op[2]])
    elif k == "gprf":
        a.fmov_gx(A64_F[op[1]], _areg(op[2]))
    elif k == "fj":
        fa, fb, cc, lbl = A64_F[op[1]], A64_F[op[2]], op[3], op[4]

        if cc == "eq":
            a.fcmp(fa, fb)
            okl = _uniq(a) + "e"
            a.bcond(A64_CC["eq"], okl)
            a.bcond(A64_CC["vc"], lbl)
            a.label(okl)
        elif cc == "ne":
            a.fcmp(fa, fb)
            a.bcond(A64_CC["ne"], lbl)
            a.bcond(A64_CC["vs"], lbl)
        elif cc == "lt":
            a.fcmp(fa, fb)
            a.bcond(mc_a64.MI, lbl)
        elif cc == "le":
            a.fcmp(fb, fa)
            a.bcond(A64_CC["ge"], lbl)
        elif cc == "gt":
            a.fcmp(fb, fa)
            a.bcond(A64_CC["gt"], lbl)
        else:
            a.fcmp(fa, fb)
            a.bcond(A64_CC["ge"], lbl)
    elif k == "fcset":
        d = _areg(op[1])
        fa, fb, cc = A64_F[op[2]], A64_F[op[3]], op[4]
        if cc == "eq":
            a.fcmp(fa, fb)
            a.cset(d, A64_CC["eq"])
            a.cset(13, mc_a64.VC)
            a.and_rr(d, d, 13)
        elif cc == "ne":
            a.fcmp(fa, fb)
            a.cset(d, A64_CC["ne"])
            a.cset(13, mc_a64.VS)
            a.orr_rr(d, d, 13)
        elif cc == "lt":
            a.fcmp(fa, fb)
            a.cset(d, mc_a64.MI)
        elif cc == "le":
            a.fcmp(fb, fa)
            a.cset(d, A64_CC["ge"])
        elif cc == "gt":
            a.fcmp(fb, fa)
            a.cset(d, A64_CC["gt"])
        else:
            a.fcmp(fa, fb)
            a.cset(d, A64_CC["ge"])
    elif k == "fjisnan":
        a.fcmp(A64_F[op[1]], A64_F[op[1]])
        a.bcond(A64_CC["vs"], op[2])
    elif k == "push_rbp" or k == "pop_rbp":
        pass
    elif k == "store_lr":
        a.mov_mr(mc_a64.FP, op[1], mc_a64.LR)
    elif k == "load_lr":
        a.mov_rm(mc_a64.LR, mc_a64.FP, op[1])
    elif k == "subsp":
        if op[1] <= 4095:
            a.sub_imm(31, 31, op[1])
        else:
            a.mov_ir(13, op[1])
            a.sub_sp_reg(13)
    elif k == "addsp":
        if op[1] <= 4095:
            a.add_imm(31, 31, op[1])
        else:
            a.mov_ir(13, op[1])
            a.add_sp_reg(13)
    elif k == "spaddr":

        if op[2]:
            a.add_imm(_areg(op[1]), 31, op[2])
        else:
            a.mov_rr(_areg(op[1]), 31)
    elif k == "sysno":
        a.mov_ir(_areg("SYSNO"), {"write": 64, "exit": 93, "mmap": 222}[op[1]])
    elif k == "syscall":
        a.svc(0)
    elif k == "int3":
        a.brk()
    else:
        raise LoweringError("unknown ARM64 op %r" % (op,))
