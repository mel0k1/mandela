#!/usr/bin/env python3
"""Mandela native backend runtime, written in the architecture-neutral DSL.

Every routine here is emitted as real machine code (via the mc_isa
translators) into the produced executable: there is no libc, no assembler
and no intermediate C anywhere in the pipeline. The runtime talks to the
Linux kernel directly (write / exit_group / mmap syscalls) and owns a bump
allocator carved out of one anonymous mmap.

Calling convention inside the runtime: arguments arrive in A0..A5 (and F0..
for doubles); results come back in RET (or RET,RET2 for {ptr,len} pairs).

The float machinery represents decimals on a 192-bit scratch integer kept
in .bss (rt_data): N (value), Q (division workspace). Shortest round-trip
printing works like the C backend: generate the exact decimal expansion,
then take the first prefix of p = 1..17 correctly rounded digits that
parses back to the same double.
"""

# bss layout inside the single 64-byte rt_data block
B_BUMP = 0
B_END = 8
B_NLO = 16
B_NMID = 24
B_NHI = 32
B_QLO = 40
B_QMID = 48
B_QHI = 56
B_FD = 64

import os
import struct as _struct


def _f64bits(x):
    return _struct.pack("<d", x)


def gen_runtime(debug=False):
    o = []
    ro = {}

    def rd(text):
        name = "s_%d" % len(ro)
        ro[name] = text.encode("utf-8")
        return name, len(ro[name])

    def frd(bits):
        name = "f_%d" % len(ro)
        ro[name] = bits
        return name

    def L(name):
        o.append(("label", name))

    def tick():
        if debug:
            o.append(("set", "A0", 2))
            o.append(("addr", "A1", S_TICK))
            o.append(("set", "A2", 1))
            o.append(("call", "rt_write"))

    S_NL, _ = rd("\n")
    S_TICK, _ = rd(".")
    S_HDR, L_HDR = rd("panic: ")
    S_TRUE, L_TRUE = rd("true")
    S_FALSE, L_FALSE = rd("false")
    S_NIL, L_NIL = rd("nil")
    S_OOM, L_OOM = rd("out of memory")
    S_IDXPRE, L_IDXPRE = rd("index out of bounds: the len is ")
    S_IDXMID, L_IDXMID = rd(" but the index is ")
    S_EMPTY, L_EMPTY = rd("")
    S_CP1, L_CP1 = rd("invalid code point in chr(): ")
    S_EINT, L_EINT = rd("cannot parse integer from empty string")
    S_DDIG, L_DDIG = rd("invalid digit found in string")
    S_DBIG, L_DBIG = rd("number too large to fit in target type")
    S_ORD, L_ORD = rd("expected a single-character string in ord()")
    S_EF2, L_EF2 = rd("cannot parse float from empty string")
    S_FBAD2, L_FBAD2 = rd("invalid float literal")
    S_NAN, L_NAN = rd("nan")
    S_INF, L_INF = rd("inf")
    S_NINF, L_NINF = rd("-inf")
    S_DIV0, L_DIV0 = rd("attempt to divide by zero")
    S_REM0, L_REM0 = rd("attempt to calculate the remainder with a divisor of zero")
    S_RNG0, L_RNG0 = rd("range() arg 3 must not be zero")
    S_NAN2I, L_NAN2I = rd("cannot convert `nan` to `int`")
    S_BIG2I, L_BIG2I = rd("number too large to fit in target type")

    F_SIGN = "fcm_sign"
    F_ABS = "fcm_abs"
    ro["fcm_sign"] = _f64bits(-0.0)
    ro["fcm_abs"] = (0x7FFFFFFFFFFFFFFF).to_bytes(8, "little")
    F_E22 = frd(_f64bits(1e22))
    F_TEN = frd(_f64bits(10.0))
    F_SMALL = frd(_f64bits(1e-22))
    F_HALF = "fcm_half"
    ro["fcm_half"] = _f64bits(0.5)
    F_P2_63 = frd(_f64bits(9223372036854775808.0))
    F_N2_63 = frd(_f64bits(-9223372036854775808.0))

    def call_write_reg_fd(buf, ln):
        o.append(("addr" if isinstance(buf, str) else "mov", "A1", buf))
        o.append(("set" if isinstance(ln, int) else "mov", "A2", ln))
        o.append(("call", "rt_write"))

    def call_write(fd, buf, ln):
        o.append(("set", "A0", fd))
        if isinstance(buf, str):
            o.append(("addr", "A1", buf))
        else:
            o.append(("mov", "A1", buf))
        if isinstance(ln, int):
            o.append(("set", "A2", ln))
        else:
            o.append(("mov", "A2", ln))
        o.append(("call", "rt_write"))

    def panic(label, ln):
        o.append(("addr", "A0", label))
        o.append(("set", "A1", ln))
        o.append(("jmp", "rt_panic"))

    def sticky_nz(reg):
        """if reg != 0: R5 |= 1"""
        j = "._sn%d" % len(o)
        o.append(("cji", reg, 0, "e", j + "z"))
        o.append(("or", "R5", "R5", 1))
        o.append(("label", j + "z"))

    def _load_N(dlo="R2", dmid="R3", dhi="R4"):
        o.append(("addr", "R1", "rt_data"))
        o.append(("load", dlo, "R1", B_NLO))
        o.append(("load", dmid, "R1", B_NMID))
        o.append(("load", dhi, "R1", B_NHI))

    def _store_N(slo="R2", smid="R3", shi="R4"):
        o.append(("addr", "R1", "rt_data"))
        o.append(("store", slo, "R1", B_NLO))
        o.append(("store", smid, "R1", B_NMID))
        o.append(("store", shi, "R1", B_NHI))

    # ======================================================================
    # rt_write(fd=A0, buf=A1, len=A2)
    # ======================================================================
    L("rt_write")
    o.append(("sysno", "write"))
    o.append(("syscall",))
    o.append(("ret",))

    # ======================================================================
    # rt_exit(code=A0); rt_hang
    # ======================================================================
    L("rt_exit")
    o.append(("sysno", "exit"))
    o.append(("syscall",))
    L("rt_hang")
    o.append(("jmp", "rt_hang"))

    # ======================================================================
    # rt_panic(msg=A0, len=A1)
    # ======================================================================
    L("rt_panic")
    o.append(("mov", "A4", "A0"))
    o.append(("mov", "A5", "A1"))
    call_write(2, S_HDR, L_HDR)
    o.append(("set", "A0", 2))
    o.append(("mov", "A1", "A4"))
    o.append(("mov", "A2", "A5"))
    o.append(("call", "rt_write"))
    call_write(2, S_NL, 1)
    o.append(("set", "A0", 101))
    o.append(("jmp", "rt_exit"))

    # ======================================================================
    # rt_fatal(msg=A0, len=A1): "panic: msg\n" then exit 1
    # ======================================================================
    L("rt_fatal")
    o.append(("mov", "A4", "A0"))
    o.append(("mov", "A5", "A1"))
    call_write(2, S_HDR, L_HDR)
    o.append(("set", "A0", 2))
    o.append(("mov", "A1", "A4"))
    o.append(("mov", "A2", "A5"))
    o.append(("call", "rt_write"))
    call_write(2, S_NL, 1)
    o.append(("set", "A0", 1))
    o.append(("jmp", "rt_exit"))

    # ======================================================================
    # rt_init: mmap 64 MiB arena
    # ======================================================================
    L("rt_init")
    o.append(("set", "A0", 0))
    o.append(("set64", "A1", int(os.environ.get("MND_ARENA_MB", "64")) * 1024 * 1024))
    o.append(("set", "A2", 3))
    o.append(("set", "A3", 0x22))
    o.append(("set", "A4", -1))
    o.append(("set", "A5", 0))
    o.append(("sysno", "mmap"))
    o.append(("syscall",))
    o.append(("cji", "RET", 4096, "ae", "._iok"))
    # debug: dump the raw mmap error code to stderr
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "RET", "R1", 72))
    o.append(("mov", "A1", "R1"))
    o.append(("add", "A1", "A1", 72))
    o.append(("set", "A0", 2))
    o.append(("set", "A2", 8))
    o.append(("call", "rt_write"))
    panic(S_OOM, L_OOM)
    L("._iok")
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "RET", "R1", B_BUMP))
    o.append(("add", "R2", "RET", "A1"))
    o.append(("store", "R2", "R1", B_END))
    o.append(("ret",))

    # ======================================================================
    # rt_alloc(n=A0) -> RET
    # ======================================================================
    L("rt_alloc")
    o.append(("add", "A0", "A0", 7))
    o.append(("and", "A0", "A0", -8))
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "R2", "R1", B_BUMP))
    o.append(("add", "R3", "R2", "A0"))
    o.append(("load", "R4", "R1", B_END))
    o.append(("cj", "R3", "R4", "be", "._aok"))
    o.append(("int3",))                    # debug: OOM site trap
    panic(S_OOM, L_OOM)
    L("._aok")
    o.append(("store", "R3", "R1", B_BUMP))
    o.append(("mov", "RET", "R2"))
    o.append(("ret",))

    # ======================================================================
    # rt_memcpy(dst=A0, src=A1, n=A2)
    # ======================================================================
    L("rt_memcpy")
    L("._mc")
    o.append(("cji", "A2", 0, "e", "._mcd"))
    o.append(("load8", "R0", "A1", 0))
    o.append(("store8", "R0", "A0", 0))
    o.append(("add", "A0", "A0", 1))
    o.append(("add", "A1", "A1", 1))
    o.append(("sub", "A2", "A2", 1))
    o.append(("jmp", "._mc"))
    L("._mcd")
    o.append(("ret",))

    # ======================================================================
    # rt_append_int(buf=A0, pos=A1, v=A2) -> RET = pos after digits
    # ======================================================================
    L("rt_append_int")
    # frame: [SP+0..19] digit buffer, [SP+24] buf, [SP+32] pos
    o.append(("subsp", 48))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "A0", "R1", 24))    # buf
    o.append(("store", "A1", "R1", 32))    # pos
    o.append(("mov", "R5", "A2"))          # v (r8, call-safe)
    o.append(("cji", "R5", 0, "ge", "._ai_pos"))
    o.append(("neg", "R5", "R5"))
    o.append(("set", "R7", 1))             # neg flag (r10)
    o.append(("jmp", "._ai_go"))
    L("._ai_pos")
    o.append(("set", "R7", 0))
    L("._ai_go")
    o.append(("set", "R2", 10))
    o.append(("set", "R4", 20))            # fill index (rdi, no calls yet)
    L("._ai_loop")
    o.append(("udivmod", "R0", "R1", "R5", "R2"))
    o.append(("sub", "R4", "R4", 1))
    o.append(("add", "R1", "R1", 48))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R4"))
    o.append(("store8", "R1", "R3", 0))
    o.append(("mov", "R5", "R0"))
    o.append(("cji", "R5", 0, "ne", "._ai_loop"))
    o.append(("cji", "R7", 0, "e", "._ai_ns"))
    o.append(("sub", "R4", "R4", 1))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R4"))
    o.append(("set", "R1", 45))
    o.append(("store8", "R1", "R3", 0))
    L("._ai_ns")
    # memcpy(dst = buf + pos, src = SP + R4, n = 20 - R4)
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R6", "R1", 24))     # buf (r9)
    o.append(("load", "R8", "R1", 32))     # pos (r11)
    o.append(("add", "A3", "R6", "R8"))    # dst (rcx; memcpy keeps rcx)
    o.append(("spaddr", "A4", 0))
    o.append(("add", "A4", "A4", "R4"))
    o.append(("set", "R7", 20))
    o.append(("sub", "R7", "R7", "R4"))    # n (r10)
    o.append(("mov", "A0", "A3"))
    o.append(("mov", "A1", "A4"))
    o.append(("mov", "A2", "R7"))
    o.append(("call", "rt_memcpy"))
    o.append(("add", "RET", "A3", "R7"))
    o.append(("addsp", 48))
    o.append(("ret",))

    # ======================================================================
    # rt_str_from_int(v=A0) -> RET ptr, RET2 len
    # ======================================================================
    L("rt_str_from_int")
    o.append(("subsp", 16))
    o.append(("mov", "R6", "A0"))
    o.append(("set", "A0", 21))
    o.append(("call", "rt_alloc"))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "RET", "R1", 0))    # buf
    o.append(("mov", "A0", "RET"))
    o.append(("set", "A1", 0))
    o.append(("mov", "A2", "R6"))
    o.append(("call", "rt_append_int"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R5", "R1", 0))      # buf
    o.append(("sub", "RET2", "RET", "R5"))
    o.append(("mov", "RET", "R5"))
    o.append(("addsp", 16))
    o.append(("ret",))

    # ======================================================================
    # rt_print_int(v=A0)
    # ======================================================================
    L("rt_print_int")
    o.append(("subsp", 16))
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "A0", "R1", B_FD))  # fd
    o.append(("mov", "R6", "A1"))          # v
    o.append(("set", "A0", 24))
    o.append(("call", "rt_alloc"))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "RET", "R1", 0))    # buf
    o.append(("mov", "A0", "RET"))
    o.append(("set", "A1", 0))
    o.append(("mov", "A2", "R6"))
    o.append(("call", "rt_append_int"))
    o.append(("set", "R1", 10))
    o.append(("store8", "R1", "RET", 0))
    o.append(("add", "RET", "RET", 1))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R5", "R1", 0))      # buf
    o.append(("sub", "R6", "RET", "R5"))
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    o.append(("mov", "A1", "R5"))
    o.append(("mov", "A2", "R6"))
    o.append(("call", "rt_write"))
    o.append(("addsp", 16))
    o.append(("ret",))

    # ======================================================================
    # rt_print_str(p=A0, len=A1); rt_print_bool(b=A0); rt_print_nil()
    # ======================================================================
    L("rt_print_str")
    o.append(("call", "rt_write"))         # fd=A0, buf=A1, len=A2
    call_write_reg_fd(S_NL, 1)
    o.append(("ret",))

    L("rt_print_bool")
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "A0", "R1", B_FD))
    o.append(("cji", "A1", 0, "e", "._pb_f"))
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    call_write_reg_fd(S_TRUE, L_TRUE)
    o.append(("jmp", "._pb_nl"))
    L("._pb_f")
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    call_write_reg_fd(S_FALSE, L_FALSE)
    L("._pb_nl")
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    call_write_reg_fd(S_NL, 1)
    o.append(("ret",))

    L("rt_print_nil")
    call_write_reg_fd(S_NIL, L_NIL)
    call_write_reg_fd(S_NL, 1)
    o.append(("ret",))

    # ======================================================================
    # rt_str_cmp(a_p=A0, a_l=A1, b_p=A2, b_l=A3) -> RET -1/0/1
    # ======================================================================
    L("rt_str_cmp")
    o.append(("mov", "R5", "A1"))
    o.append(("cj", "A1", "A3", "be", "._sc_min"))
    o.append(("mov", "R5", "A3"))
    L("._sc_min")
    o.append(("set", "R6", 0))
    L("._sc_loop")
    o.append(("cj", "R6", "R5", "ge", "._sc_tail"))
    o.append(("add", "R7", "A0", "R6"))
    o.append(("load8", "R0", "R7", 0))
    o.append(("add", "R7", "A2", "R6"))
    o.append(("load8", "R1", "R7", 0))
    o.append(("cj", "R0", "R1", "e", "._sc_next"))
    o.append(("cj", "R0", "R1", "l", "._sc_lt"))
    o.append(("set", "RET", 1))
    o.append(("ret",))
    L("._sc_lt")
    o.append(("set", "RET", -1))
    o.append(("ret",))
    L("._sc_next")
    o.append(("add", "R6", "R6", 1))
    o.append(("jmp", "._sc_loop"))
    L("._sc_tail")
    o.append(("cj", "A1", "A3", "l", "._sc_slt"))
    o.append(("cj", "A1", "A3", "g", "._sc_sgt"))
    o.append(("set", "RET", 0))
    o.append(("ret",))
    L("._sc_slt")
    o.append(("set", "RET", -1))
    o.append(("ret",))
    L("._sc_sgt")
    o.append(("set", "RET", 1))
    o.append(("ret",))

    # ======================================================================
    # rt_str_concat(a_p=A0, a_l=A1, b_p=A2, b_l=A3) -> RET, RET2
    # ======================================================================
    L("rt_str_concat")
    o.append(("subsp", 16))
    o.append(("add", "R5", "A1", "A3"))
    o.append(("mov", "R6", "A0"))
    o.append(("mov", "R7", "A2"))
    o.append(("mov", "R8", "A1"))
    o.append(("mov", "A0", "R5"))
    o.append(("call", "rt_alloc"))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "RET", "R1", 0))    # buf
    o.append(("mov", "A0", "RET"))
    o.append(("mov", "A1", "R6"))
    o.append(("mov", "A2", "R8"))
    o.append(("call", "rt_memcpy"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R4", "R1", 0))      # buf
    o.append(("add", "A0", "R4", "R8"))
    o.append(("mov", "A1", "R7"))
    o.append(("sub", "A2", "R5", "R8"))
    o.append(("call", "rt_memcpy"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "RET", "R1", 0))     # buf
    o.append(("mov", "RET2", "R5"))
    o.append(("addsp", 16))
    o.append(("ret",))

    # ======================================================================
    # rt_str_index(p=A0, len=A1, i=A2) -> RET ptr, RET2 = 1
    # ======================================================================
    L("rt_str_index")
    o.append(("cji", "A2", 0, "l", "._si_bad"))
    o.append(("cj", "A2", "A1", "ge", "._si_bad"))
    o.append(("mov", "R5", "A0"))
    o.append(("mov", "R6", "A2"))
    o.append(("set", "A0", 1))
    o.append(("call", "rt_alloc"))
    o.append(("add", "R6", "R5", "R6"))
    o.append(("load8", "R1", "R6", 0))
    o.append(("store8", "R1", "RET", 0))
    o.append(("set", "RET2", 1))
    o.append(("ret",))
    L("._si_bad")
    o.append(("mov", "A0", "A1"))
    o.append(("mov", "A1", "A2"))
    o.append(("jmp", "rt_panic_index"))

    # ======================================================================
    # rt_substr(p=A0, len=A1, start=A2, n=A3) -> RET, RET2
    # ======================================================================
    L("rt_substr")
    o.append(("cji", "A2", 0, "ge", "._su_s"))
    o.append(("set", "A2", 0))
    L("._su_s")
    o.append(("cji", "A3", 0, "ge", "._su_n"))
    o.append(("set", "A3", 0))
    L("._su_n")
    o.append(("cj", "A2", "A1", "g", "._su_empty"))
    o.append(("sub", "R5", "A1", "A2"))
    o.append(("cj", "A3", "R5", "le", "._su_ok"))
    o.append(("mov", "A3", "R5"))
    L("._su_ok")
    o.append(("mov", "R6", "A0"))
    o.append(("mov", "R7", "A2"))
    o.append(("mov", "R8", "A3"))
    o.append(("mov", "A0", "R8"))
    o.append(("call", "rt_alloc"))
    o.append(("add", "A1", "R6", "R7"))
    o.append(("mov", "A2", "R8"))
    o.append(("mov", "R4", "RET"))
    o.append(("mov", "A0", "R4"))
    o.append(("call", "rt_memcpy"))
    o.append(("mov", "RET", "R4"))
    o.append(("mov", "RET2", "R8"))
    o.append(("ret",))
    L("._su_empty")
    o.append(("addr", "RET", S_EMPTY))
    o.append(("set", "RET2", 0))
    o.append(("ret",))

    # ======================================================================
    # rt_ord(p=A0, len=A1) -> RET
    # ======================================================================
    L("rt_ord")
    o.append(("cji", "A1", 1, "e", "._or_ok"))
    panic(S_ORD, L_ORD)
    L("._or_ok")
    o.append(("load8", "RET", "A0", 0))
    o.append(("ret",))

    # ======================================================================
    # rt_chr(cp=A0) -> RET ptr, RET2 len
    # ======================================================================
    L("rt_chr")
    o.append(("cji", "A0", 0, "l", "._ch_bad"))
    o.append(("set64", "R5", 0x10FFFF))
    o.append(("cj", "A0", "R5", "g", "._ch_bad"))
    o.append(("cji", "A0", 0xD800, "l", "._ch_ok"))
    o.append(("cji", "A0", 0xDFFF, "g", "._ch_ok"))
    L("._ch_bad")
    o.append(("mov", "A4", "A0"))
    o.append(("subsp", 160))
    o.append(("spaddr", "A0", 0))
    o.append(("addr", "A1", S_CP1))
    o.append(("set", "A2", L_CP1))
    o.append(("call", "rt_memcpy"))
    o.append(("spaddr", "A0", 0))
    o.append(("set", "A1", L_CP1))
    o.append(("mov", "A2", "A4"))
    o.append(("call", "rt_append_int"))
    o.append(("mov", "A1", "RET"))
    o.append(("spaddr", "A0", 0))
    o.append(("addsp", 160))
    o.append(("jmp", "rt_panic"))
    L("._ch_ok")
    o.append(("set", "A0", 4))
    o.append(("call", "rt_alloc"))
    o.append(("mov", "R4", "RET"))
    o.append(("cji", "A0", 0x80, "l", "._ch_1"))
    o.append(("cji", "A0", 0x800, "l", "._ch_2"))
    o.append(("cji", "A0", 0x10000, "l", "._ch_3"))
    o.append(("shr", "R0", "A0", 18))
    o.append(("or", "R0", "R0", 0xF0))
    o.append(("store8", "R0", "R4", 0))
    o.append(("shr", "R0", "A0", 12))
    o.append(("and", "R0", "R0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 1))
    o.append(("shr", "R0", "A0", 6))
    o.append(("and", "R0", "R0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 2))
    o.append(("and", "R0", "A0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 3))
    o.append(("set", "RET2", 4))
    o.append(("jmp", "._ch_ret"))
    L("._ch_3")
    o.append(("shr", "R0", "A0", 12))
    o.append(("or", "R0", "R0", 0xE0))
    o.append(("store8", "R0", "R4", 0))
    o.append(("shr", "R0", "A0", 6))
    o.append(("and", "R0", "R0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 1))
    o.append(("and", "R0", "A0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 2))
    o.append(("set", "RET2", 3))
    o.append(("jmp", "._ch_ret"))
    L("._ch_2")
    o.append(("shr", "R0", "A0", 6))
    o.append(("or", "R0", "R0", 0xC0))
    o.append(("store8", "R0", "R4", 0))
    o.append(("and", "R0", "A0", 0x3F))
    o.append(("or", "R0", "R0", 0x80))
    o.append(("store8", "R0", "R4", 1))
    o.append(("set", "RET2", 2))
    o.append(("jmp", "._ch_ret"))
    L("._ch_1")
    o.append(("store8", "A0", "R4", 0))
    o.append(("set", "RET2", 1))
    L("._ch_ret")
    o.append(("mov", "RET", "R4"))
    o.append(("ret",))

    # ======================================================================
    # rt_panic_index(len=A0, idx=A1)
    # ======================================================================
    L("rt_panic_index")
    o.append(("subsp", 160))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "A1", "R1", 152))   # idx survives calls
    o.append(("mov", "R5", "A0"))
    o.append(("spaddr", "A0", 0))
    o.append(("addr", "A1", S_IDXPRE))
    o.append(("set", "A2", L_IDXPRE))
    o.append(("call", "rt_memcpy"))
    o.append(("spaddr", "A0", 0))
    o.append(("set", "A1", L_IDXPRE))
    o.append(("mov", "A2", "R5"))
    o.append(("call", "rt_append_int"))
    o.append(("mov", "R5", "RET"))
    o.append(("spaddr", "A0", 0))
    o.append(("add", "A0", "A0", "R5"))
    o.append(("addr", "A1", S_IDXMID))
    o.append(("set", "A2", L_IDXMID))
    o.append(("call", "rt_memcpy"))
    o.append(("add", "R5", "R5", L_IDXMID))
    o.append(("spaddr", "A0", 0))
    o.append(("mov", "A1", "R5"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "A2", "R1", 152))
    o.append(("call", "rt_append_int"))
    o.append(("mov", "A1", "RET"))
    o.append(("spaddr", "A0", 0))
    o.append(("addsp", 160))
    o.append(("jmp", "rt_panic"))

    # ======================================================================
    # rt_parse_int(p=A0, len=A1) -> RET
    # ======================================================================
    L("rt_parse_int")
    # frame: [SP+0] p, [SP+8] len
    o.append(("subsp", 16))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "A0", "R1", 0))
    o.append(("store", "A1", "R1", 8))
    o.append(("set", "R5", 0))             # i (r8)
    o.append(("set", "R7", 0))             # neg (r10)
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R8", "R1", 8))      # len (r11)
    L("._pi_sign")
    o.append(("cj", "R5", "R8", "ge", "._pi_empty"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R6", "R1", 0))      # p (r9)
    o.append(("add", "R3", "R6", "R5"))
    o.append(("load8", "R0", "R3", 0))
    o.append(("cji", "R0", 43, "e", "._pi_plus"))
    o.append(("cji", "R0", 45, "e", "._pi_minus"))
    o.append(("jmp", "._pi_digits"))
    L("._pi_plus")
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._pi_digits"))
    L("._pi_minus")
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R7", 1))
    L("._pi_digits")
    o.append(("cj", "R5", "R8", "ge", "._pi_empty"))
    o.append(("set", "R6", 0))             # acc (r9)
    o.append(("set64", "R4", 0x8000000000000000))   # limit (rdi)
    o.append(("cji", "R7", 0, "ne", "._pi_lim"))
    o.append(("set64", "R4", 0x7FFFFFFFFFFFFFFF))
    L("._pi_lim")
    o.append(("set", "R2", 10))
    L("._pi_loop")
    o.append(("cj", "R5", "R8", "ge", "._pi_done"))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R6", "R1", 0))      # p (r9)
    o.append(("add", "R3", "R6", "R5"))
    o.append(("load8", "R0", "R3", 0))
    o.append(("cji", "R0", 48, "l", "._pi_dig"))
    o.append(("cji", "R0", 57, "g", "._pi_dig"))
    o.append(("sub", "R0", "R0", 48))      # d (rax)
    o.append(("sub", "R3", "R4", "R0"))
    o.append(("udivmod", "R3", "R1", "R3", "R2"))
    o.append(("cj", "R6", "R3", "g", "._pi_big"))
    o.append(("mul", "R6", "R6", 10))
    o.append(("add", "R6", "R6", "R0"))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._pi_loop"))
    L("._pi_dig")
    panic(S_DDIG, L_DDIG)
    L("._pi_big")
    panic(S_DBIG, L_DBIG)
    L("._pi_empty")
    panic(S_EINT, L_EINT)
    L("._pi_done")
    o.append(("cji", "R7", 0, "e", "._pi_ret"))
    o.append(("neg", "RET", "R6"))
    o.append(("addsp", 16))
    o.append(("ret",))
    L("._pi_ret")
    o.append(("mov", "RET", "R6"))
    o.append(("addsp", 16))
    o.append(("ret",))

    # ======================================================================
    # rt_shl192(k=A0): N <<= k, k in [1, 127]
    # ======================================================================
    L("rt_shl192")
    o.append(("cji", "A0", 64, "l", "._sl_s"))
    o.append(("cji", "A0", 64, "e", "._sl_64"))
    o.append(("sub", "R6", "A0", 64))      # k2 in [1, 63]
    _load_N("R2", "R3", "R4")
    o.append(("set", "R0", 64))
    o.append(("sub", "R0", "R0", "R6"))    # 64 - k2
    o.append(("shlv", "R5", "R3", "R6"))   # mid << k2
    o.append(("shrv", "R7", "R2", "R0"))   # lo >> (64-k2)
    o.append(("or", "R5", "R5", "R7"))     # new hi
    o.append(("shlv", "R7", "R2", "R6"))   # lo << k2 = new mid
    o.append(("set", "R8", 0))
    _store_N("R8", "R7", "R5")
    o.append(("ret",))
    L("._sl_64")
    _load_N("R2", "R3", "R4")
    o.append(("set", "R0", 0))
    _store_N("R0", "R2", "R3")             # lo<-0, mid<-lo, hi<-mid
    o.append(("ret",))
    L("._sl_s")                            # k in [1, 63]
    o.append(("mov", "R6", "A0"))
    _load_N("R2", "R3", "R4")
    o.append(("set", "R0", 64))
    o.append(("sub", "R0", "R0", "R6"))
    o.append(("shlv", "R5", "R4", "R6"))
    o.append(("shrv", "R7", "R3", "R0"))
    o.append(("or", "R5", "R5", "R7"))     # new hi
    o.append(("shlv", "R7", "R3", "R6"))
    o.append(("shrv", "R8", "R2", "R0"))
    o.append(("or", "R7", "R7", "R8"))     # new mid
    o.append(("shlv", "R8", "R2", "R6"))   # new lo
    _store_N("R8", "R7", "R5")
    o.append(("ret",))

    # ======================================================================
    # rt_shr192s(k=A0) -> RET sticky: N >>= k, dropped bits folded into bit0
    # ======================================================================
    L("rt_shr192s")
    o.append(("set", "R5", 0))
    o.append(("cji", "A0", 128, "l", "._sr2"))
    o.append(("cji", "A0", 128, "e", "._sr128"))
    o.append(("sub", "R6", "A0", 128))     # k2 in [1, 63]
    _load_N("R2", "R3", "R4")
    o.append(("or", "R2", "R2", "R3"))
    sticky_nz("R2")                        # lo+mid dropped
    o.append(("set", "R0", 64))
    o.append(("sub", "R0", "R0", "R6"))
    o.append(("shlv", "R7", "R4", "R0"))
    sticky_nz("R7")                        # low k2 bits of hi
    o.append(("shrv", "R7", "R4", "R6"))
    o.append(("set", "R8", 0))
    _store_N("R7", "R8", "R8")
    o.append(("mov", "RET", "R5"))
    o.append(("ret",))
    L("._sr128")
    _load_N("R2", "R3", "R4")
    o.append(("or", "R2", "R2", "R3"))
    sticky_nz("R2")
    _store_N("R4", "R2", "R2")             # lo<-hi (mid/hi zeroed below)
    o.append(("addr", "R1", "rt_data"))
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("store", "R0", "R1", B_NHI))
    o.append(("mov", "RET", "R5"))
    o.append(("ret",))
    L("._sr2")
    o.append(("cji", "A0", 64, "l", "._sr3"))
    o.append(("cji", "A0", 64, "e", "._sr64"))
    o.append(("sub", "R6", "A0", 64))      # k2 in [1, 63]
    _load_N("R2", "R3", "R4")
    sticky_nz("R2")                        # lo dropped
    o.append(("set", "R0", 64))
    o.append(("sub", "R0", "R0", "R6"))
    o.append(("shlv", "R7", "R3", "R0"))
    sticky_nz("R7")                        # low k2 bits of mid
    o.append(("shrv", "R8", "R3", "R6"))
    o.append(("shlv", "R7", "R4", "R0"))
    o.append(("or", "R8", "R8", "R7"))     # new lo
    o.append(("shrv", "R7", "R4", "R6"))   # new mid
    o.append(("addr", "R1", "rt_data"))
    o.append(("set", "R2", 0))
    o.append(("store", "R8", "R1", B_NLO))
    o.append(("store", "R7", "R1", B_NMID))
    o.append(("store", "R2", "R1", B_NHI))
    o.append(("mov", "RET", "R5"))
    o.append(("ret",))
    L("._sr64")
    _load_N("R2", "R3", "R4")
    sticky_nz("R2")
    _store_N("R3", "R4", "R4")
    o.append(("addr", "R1", "rt_data"))
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "R1", B_NHI))
    o.append(("mov", "RET", "R5"))
    o.append(("ret",))
    L("._sr3")                             # k in [1, 63]
    o.append(("mov", "R6", "A0"))
    _load_N("R2", "R3", "R4")
    o.append(("set", "R0", 64))
    o.append(("sub", "R0", "R0", "R6"))
    o.append(("shlv", "R7", "R2", "R0"))
    sticky_nz("R7")                        # low k bits of lo
    o.append(("shrv", "R8", "R2", "R6"))
    o.append(("shlv", "R7", "R3", "R0"))
    o.append(("or", "R8", "R8", "R7"))     # new lo
    o.append(("shrv", "R2", "R3", "R6"))   # mid >> k (lo value consumed)
    o.append(("shlv", "R7", "R4", "R0"))
    o.append(("or", "R2", "R2", "R7"))     # new mid
    o.append(("shrv", "R7", "R4", "R6"))   # new hi
    _store_N("R8", "R2", "R7")
    o.append(("mov", "RET", "R5"))
    o.append(("ret",))

    # ======================================================================
    # rt_mul192(m=A0): N *= m (m < 2^64)
    # ======================================================================
    L("rt_mul192")
    # m must be spilled first: _load_N uses R4 == A0 on x86-64.  R1 (rcx)
    # is free between the N load/store helpers, so it carries m.
    # Register plan (physical clobbers of mulu128: rdx/rax = R2/R0):
    #   P1 = lo*m -> R5:R6 (P1_lo kept in R8)
    #   P3 = hi*m -> R6:R4 (old hi consumed; issued before the mid sum so
    #        the internal rdx clobber cannot kill it)
    #   P2 = mid*m -> R7:R2 (issued last; R2 becomes the new mid)
    o.append(("subsp", 8))
    o.append(("store", "A0", "SP", 0))              # m
    _load_N("R2", "R3", "R4")
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R1", "R1", 0))               # m
    o.append(("mulu128", "R5", "R6", "R2", "R1"))   # P1 = Nlo*m
    o.append(("mov", "R8", "R6"))                   # keep P1_lo
    o.append(("mulu128", "R6", "R4", "R4", "R1"))   # P3 = Nhi*m -> R6:R4
    o.append(("mulu128", "R7", "R2", "R3", "R1"))   # P2 = Nmid*m -> R7:R2
    o.append(("add", "R2", "R2", "R5"))             # mid = P2_lo + P1_hi
    o.append(("cj", "R2", "R5", "b", "._mu_c1"))
    o.append(("jmp", "._mu_n1"))
    L("._mu_c1")
    o.append(("add", "R4", "R4", 1))                # carry into new hi
    L("._mu_n1")
    o.append(("add", "R4", "R4", "R7"))             # hi = P3_lo + P2_hi
    o.append(("cj", "R4", "R7", "b", "._mu_c2"))
    o.append(("jmp", "._mu_n2"))
    L("._mu_c2")
    o.append(("add", "R6", "R6", 1))     # overflow beyond 192 bits (dropped)
    L("._mu_n2")
    _store_N("R8", "R2", "R4")     # lo <- P1_lo, mid, hi
    o.append(("addsp", 8))
    o.append(("ret",))

    # ======================================================================
    # rt_divmod192(d=A0) -> RET remainder; N -> Q (quotient in N slots)
    # d must be < 2^63
    # ======================================================================
    L("rt_divmod192")
    # NOTE: d must be spilled first -- _load_N uses R4, which is the same
    # physical register as A0 on x86-64.
    o.append(("subsp", 8))
    o.append(("store", "A0", "SP", 0))     # d
    _load_N("R2", "R3", "R4")
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "R2", "R1", B_QLO))
    o.append(("store", "R3", "R1", B_QMID))
    o.append(("store", "R4", "R1", B_QHI))
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "R1", B_NLO))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("store", "R0", "R1", B_NHI))
    o.append(("set", "R5", 0))
    o.append(("set", "R6", 192))
    L("._dm_loop")
    o.append(("load", "R2", "R1", B_QHI))
    o.append(("shr", "R7", "R2", 63))
    o.append(("shl", "R2", "R2", 1))
    o.append(("load", "R3", "R1", B_QMID))
    o.append(("shr", "R0", "R3", 63))
    o.append(("or", "R2", "R2", "R0"))
    o.append(("shl", "R3", "R3", 1))
    o.append(("load", "R4", "R1", B_QLO))
    o.append(("shr", "R0", "R4", 63))
    o.append(("or", "R3", "R3", "R0"))
    o.append(("shl", "R4", "R4", 1))
    o.append(("store", "R2", "R1", B_QHI))
    o.append(("store", "R3", "R1", B_QMID))
    o.append(("store", "R4", "R1", B_QLO))
    o.append(("shl", "R5", "R5", 1))
    o.append(("or", "R5", "R5", "R7"))
    o.append(("spaddr", "R7", 0))
    o.append(("load", "R7", "R7", 0))      # d (A0 was clobbered by _load_N)
    o.append(("cj", "R5", "R7", "l", "._dm_noq"))
    o.append(("sub", "R5", "R5", "R7"))
    o.append(("load", "R2", "R1", B_NHI))
    o.append(("shl", "R2", "R2", 1))
    o.append(("load", "R3", "R1", B_NMID))
    o.append(("shr", "R0", "R3", 63))
    o.append(("or", "R2", "R2", "R0"))
    o.append(("shl", "R3", "R3", 1))
    o.append(("load", "R4", "R1", B_NLO))
    o.append(("shr", "R0", "R4", 63))
    o.append(("or", "R3", "R3", "R0"))
    o.append(("shl", "R4", "R4", 1))
    o.append(("or", "R4", "R4", 1))
    o.append(("store", "R2", "R1", B_NHI))
    o.append(("store", "R3", "R1", B_NMID))
    o.append(("store", "R4", "R1", B_NLO))
    o.append(("jmp", "._dm_next"))
    L("._dm_noq")
    o.append(("load", "R2", "R1", B_NHI))
    o.append(("shl", "R2", "R2", 1))
    o.append(("load", "R3", "R1", B_NMID))
    o.append(("shr", "R0", "R3", 63))
    o.append(("or", "R2", "R2", "R0"))
    o.append(("shl", "R3", "R3", 1))
    o.append(("load", "R4", "R1", B_NLO))
    o.append(("shr", "R0", "R4", 63))
    o.append(("or", "R3", "R3", "R0"))
    o.append(("shl", "R4", "R4", 1))
    o.append(("store", "R2", "R1", B_NHI))
    o.append(("store", "R3", "R1", B_NMID))
    o.append(("store", "R4", "R1", B_NLO))
    L("._dm_next")
    o.append(("sub", "R6", "R6", 1))
    o.append(("cji", "R6", 0, "ne", "._dm_loop"))
    o.append(("mov", "RET", "R5"))
    o.append(("addsp", 8))
    o.append(("ret",))

    # ======================================================================
    # rt_u192_to_f64(rshift=A0, sticky=A1) -> RET double bits
    # value = N / 2^rshift (+ sticky fraction); must be a normal double
    # ======================================================================
    L("rt_u192_to_f64")
    o.append(("subsp", 32))
    o.append(("set", "R0", 0))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "R0", "R1", 16))    # s (original msb), init 0
    o.append(("store", "R0", "R1", 24))    # exp_adj = 0
    o.append(("store", "A0", "SP", 0))     # rshift
    o.append(("store", "A1", "SP", 8))     # sticky
    _load_N("R2", "R3", "R4")
    o.append(("or", "R5", "R2", "R3"))
    o.append(("or", "R5", "R5", "R4"))
    o.append(("cji", "R5", 0, "ne", "._u2_nz"))
    o.append(("set", "RET", 0))
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._u2_nz")
    o.append(("cji", "R4", 0, "e", "._u2_h"))
    o.append(("msb", "R5", "R4"))
    o.append(("add", "R5", "R5", 128))
    o.append(("jmp", "._u2_s"))
    L("._u2_h")
    o.append(("cji", "R3", 0, "e", "._u2_m"))
    o.append(("msb", "R5", "R3"))
    o.append(("add", "R5", "R5", 64))
    o.append(("jmp", "._u2_s"))
    L("._u2_m")
    o.append(("msb", "R5", "R2"))
    L("._u2_s")
    o.append(("spaddr", "R1", 0))
    o.append(("store", "R5", "R1", 16))    # remember original msb position
    o.append(("set", "R6", 107))
    o.append(("sub", "R6", "R6", "R5"))    # k = 107 - s
    o.append(("cji", "R6", 0, "le", "._u2_kd"))
    o.append(("mov", "A0", "R6"))
    o.append(("call", "rt_shl192"))
    o.append(("jmp", "._u2_can"))
    L("._u2_kd")
    o.append(("cji", "R6", 0, "e", "._u2_can"))
    o.append(("neg", "A0", "R6"))
    o.append(("load", "A1", "SP", 8))
    o.append(("call", "rt_shr192s"))       # RET = sticky
    o.append(("load", "A1", "SP", 8))
    o.append(("or", "A1", "A1", "RET"))
    o.append(("store", "A1", "SP", 8))
    L("._u2_can")
    _load_N("R2", "R3", "R4")              # hi == 0 now
    o.append(("shl", "R7", "R3", 10))      # mid << 10
    o.append(("shr", "R8", "R2", 54))      # lo >> 54
    o.append(("or", "R7", "R7", "R8"))     # m54 = Nn bits 107..54
    # m53 (the packed field) = m54 >> 1, so the ROUND bit is Nn bit 54
    # (m54 & 1) and everything below it (Nn bits 53..0) plus the external
    # sticky forms the dropped tail. Round-to-nearest-even:
    #   rup iff round && (tail != 0 || sticky || m53 odd)
    L("._u2_nst")
    o.append(("set", "R8", 0))             # rup flag
    o.append(("and", "R5", "R7", 1))       # round bit
    o.append(("cji", "R5", 0, "e", "._u2_nor"))
    o.append(("set64", "R6", 0x3FFFFFFFFFFFFF))
    o.append(("and", "R6", "R2", "R6"))    # Nn bits 53..0
    o.append(("cji", "R6", 0, "ne", "._u2_rup"))
    o.append(("load", "R5", "SP", 8))      # external sticky
    o.append(("cji", "R5", 0, "ne", "._u2_rup"))
    o.append(("mov", "R5", "R7"))
    o.append(("shr", "R5", "R5", 1))
    o.append(("and", "R5", "R5", 1))       # m53 odd -> round up on tie
    o.append(("cji", "R5", 0, "ne", "._u2_rup"))
    o.append(("jmp", "._u2_nor"))
    L("._u2_rup")
    o.append(("add", "R7", "R7", 1))
    L("._u2_nor")
    o.append(("set64", "R6", 0x40000000000000))   # 2^54
    o.append(("cj", "R7", "R6", "ne", "._u2_noexp"))
    o.append(("shr", "R7", "R7", 1))
    o.append(("spaddr", "R1", 0))
    o.append(("set", "R5", 1))
    o.append(("store", "R5", "R1", 24))    # exp_adj = 1
    L("._u2_noexp")
    o.append(("load", "R2", "SP", 0))      # rshift
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R6", "R1", 16))     # original s
    o.append(("sub", "R3", "R6", "R2"))
    o.append(("add", "R3", "R3", 1023))    # exp_field = s - rshift + 1023
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R5", "R1", 24))
    o.append(("add", "R3", "R3", "R5"))
    o.append(("cji", "R3", 2047, "ge", "._u2_inf"))
    o.append(("cji", "R3", 0, "le", "._u2_zero"))
    o.append(("shl", "R5", "R3", 52))
    o.append(("set64", "R6", 0x0020000000000000))  # 2^53
    o.append(("sub", "R7", "R7", "R6"))
    o.append(("shr", "R7", "R7", 1))       # field = (m54 - 2^53) / 2
    o.append(("or", "R5", "R5", "R7"))
    o.append(("mov", "RET", "R5"))
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._u2_inf")
    o.append(("set64", "RET", 0x7FF0000000000000))
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._u2_zero")
    # subnormal (exp_field <= 0): field = RNE(m54 / 2^(2 - exp_field));
    # a result of 2^52 lands on the smallest normal's bit pattern.
    o.append(("neg", "R5", "R3"))
    o.append(("add", "R5", "R5", 2))       # shift = 2 - exp_field
    o.append(("cji", "R5", 55, "l", "._u2_szok"))
    o.append(("set", "RET", 0))            # below half of the min subnormal
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._u2_szok")
    o.append(("shrv", "R6", "R7", "R5"))   # q = m54 >> shift
    o.append(("shlv", "R8", "R6", "R5"))
    o.append(("sub", "R8", "R7", "R8"))    # rem = m54 - (q << shift)
    o.append(("set", "R2", 1))
    o.append(("sub", "R3", "R5", 1))
    o.append(("shlv", "R4", "R2", "R3"))   # half = 2^(shift-1)
    o.append(("cj", "R8", "R4", "a", "._u2_sup"))
    o.append(("cj", "R8", "R4", "ne", "._u2_snd"))
    o.append(("and", "R2", "R6", 1))       # tie: round to even
    o.append(("cji", "R2", 0, "ne", "._u2_sup"))
    o.append(("jmp", "._u2_snd"))
    L("._u2_sup")
    o.append(("add", "R6", "R6", 1))
    L("._u2_snd")
    o.append(("mov", "RET", "R6"))
    o.append(("addsp", 32))
    o.append(("ret",))

    L("rt_f64_from_decimal")
    # frame: 0 D, 8 q, 16 s remaining, 24 S (total left shifts),
    #        32 k scratch, 40 sticky
    o.append(("subsp", 48))
    o.append(("store", "A0", "SP", 0))     # D
    o.append(("store", "A1", "SP", 8))     # q (or s)
    o.append(("cji", "A1", 0, "ge", "._fd_pos"))
    # ---------------- q < 0 ----------------
    # value = D / 10^s, s = -q. Interleave: normalize N to msb 107, divide
    # by 5 once, repeat. Every division then happens on an N >= 2^104, so
    # at least 54 significant bits survive into the final result.
    # rshift = S + s (S = sum of left shifts): value = D*2^S/5^s / 2^(S+s)
    o.append(("load", "R2", "SP", 0))
    o.append(("cji", "R2", 0, "ne", "._fd_nzq"))
    o.append(("set", "RET", 0))            # D == 0 -> 0.0
    o.append(("addsp", 48))
    o.append(("ret",))
    L("._fd_nzq")
    o.append(("neg", "R4", "A1"))
    o.append(("store", "R4", "SP", 16))    # s remaining
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "SP", 24))    # S = 0
    o.append(("store", "R0", "SP", 40))    # sticky = 0
    # N = D
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "R2", "R1", B_NLO))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("store", "R0", "R1", B_NHI))
    L("._fd_step")
    o.append(("load", "R4", "SP", 16))
    o.append(("cji", "R4", 0, "le", "._fd_done"))
    # normalize: shift N so its msb sits at 115 (not 107!). rt_u192_to_f64
    # then always takes its RIGHT-shift path, where the sub-bit-0 fraction
    # is < 1 and the 1-bit sticky is enough for correct rounding. With msb
    # 107 the left-shift path would amplify the fraction by up to 2^3 and
    # flip the guard decision (0.1 case).
    _load_N("R2", "R3", "R4")
    o.append(("cji", "R4", 0, "e", "._fd_m2"))
    o.append(("msb", "R5", "R4"))
    o.append(("add", "R5", "R5", 128))
    o.append(("jmp", "._fd_m3"))
    L("._fd_m2")
    o.append(("cji", "R3", 0, "e", "._fd_m1"))
    o.append(("msb", "R5", "R3"))
    o.append(("add", "R5", "R5", 64))
    o.append(("jmp", "._fd_m3"))
    L("._fd_m1")
    o.append(("msb", "R5", "R2"))
    L("._fd_m3")
    o.append(("set", "R6", 115))
    o.append(("sub", "R6", "R6", "R5"))    # k = 115 - msb(N)
    o.append(("cji", "R6", 0, "le", "._fd_nosh"))
    o.append(("store", "R6", "SP", 32))
    o.append(("mov", "A0", "R6"))
    o.append(("call", "rt_shl192"))
    o.append(("load", "R6", "SP", 32))
    o.append(("load", "R0", "SP", 24))
    o.append(("add", "R0", "R0", "R6"))    # S += k
    o.append(("store", "R0", "SP", 24))
    L("._fd_nosh")
    # divide by 5, fold remainder into sticky
    o.append(("set", "A0", 5))
    o.append(("call", "rt_divmod192"))
    o.append(("cji", "RET", 0, "e", "._fd_dnz"))
    o.append(("spaddr", "R1", 40))
    o.append(("load", "R5", "R1", 0))
    o.append(("or", "R5", "R5", 1))
    o.append(("store", "R5", "R1", 0))
    L("._fd_dnz")
    o.append(("load", "R4", "SP", 16))
    o.append(("sub", "R4", "R4", 1))
    o.append(("store", "R4", "SP", 16))
    o.append(("jmp", "._fd_step"))
    L("._fd_done")
    # rshift = S + s_orig; N / 2^rshift = D / 10^s (with sticky fraction)
    o.append(("load", "R4", "SP", 8))
    o.append(("neg", "R4", "R4"))          # s_orig
    o.append(("load", "R5", "SP", 24))     # S
    o.append(("add", "R5", "R5", "R4"))
    o.append(("spaddr", "R1", 40))
    o.append(("load", "A1", "R1", 0))      # sticky
    o.append(("mov", "A0", "R5"))
    o.append(("call", "rt_u192_to_f64"))
    o.append(("addsp", 48))
    o.append(("ret",))
    # ---------------- q >= 0 ----------------
    # value = D * 10^q = D * 5^q * 2^q.  Multiply N by 5 q times, pulling
    # the msb back down to 140 after every step (dropped low bits go into
    # the sticky fraction).  N stays ~2^140, so q may reach ~310 without
    # overflowing the 192-bit N.  The value equals N / 2^rshift with
    # rshift = -(S + q_orig) (negative: u2f adds the exponent back).
    L("._fd_pos")
    o.append(("load", "R2", "SP", 0))      # D
    o.append(("cji", "R2", 0, "ne", "._fd_pnz"))
    o.append(("set", "RET", 0))            # D == 0 -> 0.0
    o.append(("addsp", 48))
    o.append(("ret",))
    L("._fd_pnz")
    o.append(("load", "R4", "SP", 8))
    o.append(("store", "R4", "SP", 32))    # q_orig
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "SP", 24))    # S = 0
    o.append(("store", "R0", "SP", 40))    # sticky = 0
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "R2", "R1", B_NLO))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("store", "R0", "R1", B_NHI))
    L("._fd_mul")
    o.append(("load", "R4", "SP", 8))
    o.append(("cji", "R4", 0, "le", "._fd_muld"))
    o.append(("set", "A0", 5))
    o.append(("call", "rt_mul192"))
    L("._fd_mr")
    # renormalize while msb(N) > 140
    _load_N("R2", "R3", "R4")
    o.append(("cji", "R4", 0, "e", "._fd_pm2"))
    o.append(("msb", "R5", "R4"))
    o.append(("add", "R5", "R5", 128))
    o.append(("jmp", "._fd_pm3"))
    L("._fd_pm2")
    o.append(("cji", "R3", 0, "e", "._fd_pm1"))
    o.append(("msb", "R5", "R3"))
    o.append(("add", "R5", "R5", 64))
    o.append(("jmp", "._fd_pm3"))
    L("._fd_pm1")
    o.append(("msb", "R5", "R2"))
    L("._fd_pm3")
    o.append(("set", "R6", 140))
    o.append(("sub", "R6", "R6", "R5"))    # k = 140 - msb
    o.append(("cji", "R6", 0, "ge", "._fd_mrok"))
    o.append(("neg", "R6", "R6"))          # msb - 140
    o.append(("store", "R6", "SP", 16))
    o.append(("mov", "A0", "R6"))
    o.append(("call", "rt_shr192s"))       # N >>= k, RET = sticky
    o.append(("spaddr", "R1", 40))
    o.append(("load", "R5", "R1", 0))
    o.append(("or", "R5", "R5", "RET"))
    o.append(("store", "R5", "R1", 0))
    o.append(("load", "R6", "SP", 16))
    o.append(("load", "R0", "SP", 24))
    o.append(("add", "R0", "R0", "R6"))    # S += k
    o.append(("store", "R0", "SP", 24))
    o.append(("jmp", "._fd_mr"))
    L("._fd_mrok")
    o.append(("load", "R4", "SP", 8))
    o.append(("sub", "R4", "R4", 1))
    o.append(("store", "R4", "SP", 8))
    o.append(("jmp", "._fd_mul"))
    L("._fd_muld")
    # rshift = -(S + q_orig)
    o.append(("load", "R0", "SP", 24))     # S
    o.append(("load", "R4", "SP", 32))     # q_orig
    o.append(("add", "R0", "R0", "R4"))
    o.append(("neg", "R0", "R0"))
    o.append(("spaddr", "R1", 40))
    o.append(("load", "A1", "R1", 0))      # sticky
    o.append(("mov", "A0", "R0"))
    o.append(("call", "rt_u192_to_f64"))
    o.append(("addsp", 48))
    o.append(("ret",))

    # ======================================================================
    # rt_str_from_f64(x=F0) -> RET ptr, RET2 len (shortest repr, no newline)
    # frame: 0-63 digits, 64 nd, 72 decpt, 80 xbits, 88 sign, 96 m, 104 e,
    # 112 p, 120 q, 128 decpt_eff, 136 nd_eff, 144 frac, 152 tmp,
    # 160..231 out buffer
    # ======================================================================
    def sld(reg, slot):
        o.append(("spaddr", "R1", slot))
        o.append(("load", reg, "R1", 0))

    def sst(reg, slot):
        o.append(("spaddr", "R1", slot))
        o.append(("store", reg, "R1", 0))

    L("rt_str_from_f64")
    o.append(("subsp", 232))
    o.append(("fgpr", "R0", "F0"))         # bits
    o.append(("fjisnan", "F0", "._sf_nan"))
    o.append(("set64", "R2", 0x7FF0000000000000))
    o.append(("mov", "R1", "R0"))
    o.append(("and", "R1", "R1", "R2"))
    o.append(("cj", "R1", "R2", "e", "._sf_inf"))
    o.append(("shr", "R3", "R0", 63))
    sst("R3", 88)                          # sign
    o.append(("set64", "R2", 0x7FFFFFFFFFFFFFFF))
    o.append(("and", "R0", "R0", "R2"))
    sst("R0", 80)                          # xbits (abs)
    o.append(("cji", "R0", 0, "ne", "._sf_nz"))
    # ---- zero: [sign] "0.0" ----
    o.append(("set", "R6", 0))
    sld("R3", 88)
    o.append(("cji", "R3", 0, "e", "._sf_z1"))
    o.append(("spaddr", "R5", 160))
    o.append(("set", "R7", 45))
    o.append(("store8", "R7", "R5", 0))
    o.append(("set", "R6", 1))
    L("._sf_z1")
    o.append(("spaddr", "R5", 160))
    o.append(("add", "R5", "R5", "R6"))
    o.append(("set", "R7", 48))
    o.append(("store8", "R7", "R5", 0))
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R7", 46))
    o.append(("store8", "R7", "R5", 0))
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R7", 48))
    o.append(("store8", "R7", "R5", 0))
    o.append(("add", "R6", "R6", 3))
    o.append(("spaddr", "RET", 160))
    o.append(("mov", "RET2", "R6"))        # 3, or 4 with the sign
    o.append(("addsp", 232))
    o.append(("ret",))
    L("._sf_nz")
    o.append(("shr", "R1", "R0", 52))
    o.append(("and", "R1", "R1", 0x7FF))   # exp_field
    o.append(("cji", "R1", 0, "ne", "._sf_norm"))
    o.append(("jmp", "._sf_fallback"))
    L("._sf_norm")
    o.append(("set64", "R2", 0xFFFFFFFFFFFFF))
    o.append(("and", "R3", "R0", "R2"))
    o.append(("set64", "R2", 0x10000000000000))
    o.append(("or", "R3", "R3", "R2"))     # m
    o.append(("sub", "R4", "R1", 1075))    # e (before sst: sst clobbers R1)
    sst("R3", 96)
    sst("R4", 104)
    o.append(("cji", "R4", -64, "l", "._sf_fallback"))
    o.append(("cji", "R4", 74, "g", "._sf_fallback"))
    o.append(("cji", "R4", 10, "le", "._sf_pathA"))
    # ---- path B: N = m << e is an integer ----
    sld("R2", 96)
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "R2", "R1", B_NLO))
    o.append(("set", "R0", 0))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("store", "R0", "R1", B_NHI))
    L("._sf_bshl")
    sld("R5", 104)
    o.append(("cji", "R5", 0, "le", "._sf_bshld"))
    o.append(("set", "R0", 63))
    o.append(("cj", "R5", "R0", "le", "._sf_bshl1"))
    o.append(("set", "A0", 63))
    o.append(("call", "rt_shl192"))
    sld("R5", 104)
    o.append(("sub", "R5", "R5", 63))
    sst("R5", 104)
    o.append(("jmp", "._sf_bshl"))
    L("._sf_bshl1")
    o.append(("mov", "A0", "R5"))
    o.append(("call", "rt_shl192"))
    o.append(("set", "R0", 0))
    sst("R0", 104)
    L("._sf_bshld")
    o.append(("set", "R6", 0))
    sst("R6", 64)                          # nd = 0
    L("._sf_bdig")
    _load_N("R2", "R3", "R4")
    o.append(("or", "R5", "R2", "R3"))
    o.append(("or", "R5", "R5", "R4"))
    o.append(("cji", "R5", 0, "e", "._sf_bdrev"))
    o.append(("set", "A0", 10))
    o.append(("call", "rt_divmod192"))     # RET = digit (LSB first)
    sld("R6", 64)
    o.append(("add", "R1", "RET", 48))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R6"))
    o.append(("store8", "R1", "R3", 0))
    o.append(("add", "R6", "R6", 1))
    sst("R6", 64)
    o.append(("jmp", "._sf_bdig"))
    L("._sf_bdrev")                        # reverse [0..nd)
    o.append(("set", "R7", 0))
    L("._sf_brev")
    sld("R6", 64)
    o.append(("sub", "R8", "R6", 1))
    o.append(("sub", "R8", "R8", "R7"))    # j = nd-1-i
    o.append(("cj", "R7", "R8", "ge", "._sf_brevd"))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R5", "R3", "R7"))
    o.append(("add", "R4", "R3", "R8"))
    o.append(("load8", "R1", "R5", 0))
    o.append(("load8", "R2", "R4", 0))
    o.append(("store8", "R1", "R4", 0))
    o.append(("store8", "R2", "R5", 0))
    o.append(("add", "R7", "R7", 1))
    o.append(("jmp", "._sf_brev"))
    L("._sf_brevd")
    sld("R6", 64)
    sst("R6", 72)                          # decpt = nd
    o.append(("jmp", "._sf_search"))
    # ---- path A: exact fixed point (e in [-64, 10]) ----
    L("._sf_pathA")
    sld("R3", 96)                          # m
    sld("R4", 104)                         # e
    o.append(("add", "R5", "R4", 64))      # shift in [0, 74]
    o.append(("cji", "R5", 0, "ne", "._sf_pa1"))
    o.append(("mov", "R6", "R3"))          # lo = m
    o.append(("set", "R7", 0))             # hi = 0
    o.append(("jmp", "._sf_pa3"))
    L("._sf_pa1")
    o.append(("cji", "R5", 64, "l", "._sf_pa2"))
    o.append(("sub", "R6", "R5", 64))      # shift - 64 in [0, 10]
    o.append(("shlv", "R7", "R3", "R6"))   # hi = m << (shift-64)
    o.append(("set", "R6", 0))             # lo = 0
    o.append(("jmp", "._sf_pa3"))
    L("._sf_pa2")                          # shift in [1, 63]
    o.append(("shlv", "R6", "R3", "R5"))   # lo = m << shift
    o.append(("set", "R8", 64))
    o.append(("sub", "R8", "R8", "R5"))
    o.append(("shrv", "R7", "R3", "R8"))   # hi = m >> (64-shift)
    L("._sf_pa3")
    sst("R6", 144)                         # frac slot
    o.append(("set", "R8", 0))
    sst("R8", 64)                          # nd = 0
    # integer digits (LSB first, then reversed)
    o.append(("cji", "R7", 0, "e", "._sf_pfrac"))
    L("._sf_pint")
    o.append(("set", "R2", 10))
    o.append(("udivmod", "R0", "R5", "R7", "R2"))    # R5 = digit (R1 unsafe: sld)
    sld("R6", 64)                          # nd
    o.append(("add", "R5", "R5", 48))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R6"))
    o.append(("store8", "R5", "R3", 0))
    o.append(("add", "R6", "R6", 1))
    sst("R6", 64)
    o.append(("mov", "R7", "R0"))
    o.append(("cji", "R7", 0, "ne", "._sf_pint"))
    o.append(("set", "R7", 0))
    L("._sf_prev")
    sld("R6", 64)
    o.append(("sub", "R8", "R6", 1))
    o.append(("sub", "R8", "R8", "R7"))
    o.append(("cj", "R7", "R8", "ge", "._sf_prevd"))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R5", "R3", "R7"))
    o.append(("add", "R4", "R3", "R8"))
    o.append(("load8", "R1", "R5", 0))
    o.append(("load8", "R2", "R4", 0))
    o.append(("store8", "R1", "R4", 0))
    o.append(("store8", "R2", "R5", 0))
    o.append(("add", "R7", "R7", 1))
    o.append(("jmp", "._sf_prev"))
    L("._sf_prevd")
    L("._sf_pfrac")
    sld("R6", 64)
    sst("R6", 72)                          # decpt = nd
    # fraction digits (MSB first, natural order)
    L("._sf_pfr")
    sld("R6", 144)                         # frac
    o.append(("cji", "R6", 0, "ne", "._sf_pfr1"))
    o.append(("jmp", "._sf_search"))       # frac == 0: no more digits
    L("._sf_pfr1")
    sld("R8", 64)
    o.append(("cji", "R8", 64, "l", "._sf_pfr2"))
    o.append(("jmp", "._sf_search"))
    L("._sf_pfr2")
    o.append(("set", "R2", 10))
    o.append(("mulu128", "R5", "R6", "R6", "R2"))  # digit=R5, frac=R6
    o.append(("add", "R5", "R5", 48))
    sld("R8", 64)
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R8"))
    o.append(("store8", "R5", "R3", 0))
    o.append(("add", "R8", "R8", 1))
    sst("R8", 64)
    sst("R6", 144)
    o.append(("jmp", "._sf_pfr"))
    # ---- shortest round-trip search ----
    L("._sf_search")
    o.append(("set", "R6", 1))
    sst("R6", 112)                         # p = 1
    L("._sf_sr")
    sld("R6", 112)                         # p
    sld("R7", 64)                          # nd
    o.append(("cj", "R6", "R7", "ge", "._sf_ex"))
    # ---- candidate for p < nd ----
    # D = digits_as_int(p) -> R4
    o.append(("set", "R4", 0))
    o.append(("set", "R5", 0))
    o.append(("spaddr", "R3", 0))
    L("._sf_dai")
    o.append(("cj", "R5", "R6", "ge", "._sf_daid"))
    o.append(("load8", "R1", "R3", 0))
    o.append(("sub", "R1", "R1", 48))
    o.append(("mul", "R4", "R4", 10))
    o.append(("add", "R4", "R4", "R1"))
    o.append(("add", "R3", "R3", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_dai"))
    L("._sf_daid")
    # dnext = digits[p]
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R6"))
    o.append(("load8", "R5", "R3", 0))     # dnext
    o.append(("set", "R8", 0))             # up flag
    o.append(("cji", "R5", 53, "le", "._sf_u1"))
    o.append(("set", "R8", 1))             # dnext > '5'
    o.append(("jmp", "._sf_u3"))
    L("._sf_u1")
    o.append(("cji", "R5", 53, "ne", "._sf_u3"))   # dnext < '5'
    # tie: scan digits[p+1..nd) for nonzero; else round to even
    o.append(("set", "R5", 0))             # scan index
    L("._sf_scan")
    sld("R1", 64)
    o.append(("cj", "R5", "R1", "ge", "._sf_scd"))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R5"))
    o.append(("load8", "R2", "R3", 0))
    o.append(("cji", "R2", 48, "ne", "._sf_scnz"))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_scan"))
    L("._sf_scnz")
    o.append(("set", "R8", 1))
    o.append(("jmp", "._sf_u3"))
    L("._sf_scd")
    o.append(("and", "R5", "R4", 1))       # D odd?
    o.append(("cji", "R5", 0, "e", "._sf_u3"))
    o.append(("set", "R8", 1))
    L("._sf_u3")
    sld("R5", 112)
    o.append(("cji", "R8", 0, "e", "._sf_u4"))
    o.append(("add", "R4", "R4", 1))       # D += 1
    L("._sf_u4")
    # carry: if D == 10^p: D = 1, q = decpt
    o.append(("set", "R5", 1))
    o.append(("set", "R2", 0))
    L("._sf_pw")
    sld("R1", 112)
    o.append(("cj", "R2", "R1", "ge", "._sf_pwd"))
    o.append(("mul", "R5", "R5", 10))
    o.append(("add", "R2", "R2", 1))
    o.append(("jmp", "._sf_pw"))
    L("._sf_pwd")
    o.append(("cj", "R4", "R5", "ne", "._sf_nc"))
    o.append(("set", "R4", 1))
    sld("R5", 72)
    sst("R5", 120)                         # q = decpt
    
    o.append(("jmp", "._sf_dc"))
    L("._sf_nc")
    sld("R5", 72)
    o.append(("sub", "R5", "R5", "R6"))
    sst("R5", 120)                         # q = decpt - p
    L("._sf_dc")
    sst("R4", 152)                         # D -> tmp slot? no: D slot is 152? use 152
    o.append(("jmp", "._sf_call"))
    # ---- exact candidate (p >= nd) ----
    L("._sf_ex")
    o.append(("set", "R4", 0))
    o.append(("set", "R5", 0))
    o.append(("spaddr", "R3", 0))
    L("._sf_dai2")
    o.append(("cj", "R5", "R7", "ge", "._sf_dai2d"))
    o.append(("load8", "R1", "R3", 0))
    o.append(("sub", "R1", "R1", 48))
    o.append(("mul", "R4", "R4", 10))
    o.append(("add", "R4", "R4", "R1"))
    o.append(("add", "R3", "R3", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_dai2"))
    L("._sf_dai2d")
    sst("R4", 152)                         # D
    sld("R5", 72)
    o.append(("sub", "R5", "R5", "R7"))    # q = decpt - nd
    sst("R5", 120)
    L("._sf_call")
    sld("A0", 152)
    sld("A1", 120)
    o.append(("call", "rt_f64_from_decimal"))
    sld("R2", 80)                          # xbits
    o.append(("cj", "RET", "R2", "e", "._sf_found"))
    sld("R6", 112)
    o.append(("add", "R6", "R6", 1))
    sst("R6", 112)
    o.append(("cji", "R6", 17, "le", "._sf_sr"))
    # exhausted (should not happen): use last candidate
    L("._sf_found")
    # nd_eff = decimal length of D; trim trailing zeros
    sld("R2", 152)                         # D
    o.append(("set", "R3", 0))             # nd_eff
    L("._sf_len")
    o.append(("cji", "R2", 0, "e", "._sf_lend"))
    o.append(("set", "R6", 10))
    o.append(("udivmod", "R2", "R7", "R2", "R6"))
    o.append(("add", "R3", "R3", 1))
    o.append(("jmp", "._sf_len"))
    L("._sf_lend")
    L("._sf_trim")
    o.append(("cji", "R3", 1, "le", "._sf_trimd"))
    sld("R2", 152)
    o.append(("set", "R6", 10))
    o.append(("udivmod", "R2", "R7", "R2", "R6"))
    o.append(("cji", "R7", 0, "ne", "._sf_trimd"))
    o.append(("sub", "R3", "R3", 1))
    sst("R2", 152)
    # one decimal place moved right: q must follow nd_eff down
    sld("R5", 120)
    o.append(("add", "R5", "R5", 1))
    sst("R5", 120)
    o.append(("jmp", "._sf_trim"))
    L("._sf_trimd")
    sst("R3", 136)                         # nd_eff
    sld("R5", 120)                         # q
    o.append(("add", "R5", "R5", "R3"))
    sst("R5", 128)                         # decpt_eff = q + nd_eff
    # ---- format ----
    # rebuild digit chars from D
    sld("R2", 152)
    sld("R3", 136)
    o.append(("sub", "R4", "R3", 1))       # idx
    L("._sf_dgs")
    o.append(("cji", "R4", 0, "l", "._sf_dgsd"))
    o.append(("set", "R6", 10))
    o.append(("udivmod", "R2", "R7", "R2", "R6"))
    o.append(("spaddr", "R5", 0))
    o.append(("add", "R5", "R5", "R4"))
    o.append(("add", "R7", "R7", 48))
    o.append(("store8", "R7", "R5", 0))
    o.append(("sub", "R4", "R4", 1))
    o.append(("jmp", "._sf_dgs"))
    L("._sf_dgsd")
    o.append(("set", "R6", 0))             # pos
    sld("R5", 88)                          # sign
    o.append(("cji", "R5", 0, "e", "._sf_nsg"))
    o.append(("spaddr", "R7", 160))
    o.append(("set", "R5", 45))
    o.append(("store8", "R5", "R7", 0))
    o.append(("set", "R6", 1))
    L("._sf_nsg")
    sld("R4", 128)                         # decpt_eff
    sld("R3", 136)                         # nd_eff
    o.append(("cji", "R4", -4, "le", "._sf_enot"))
    o.append(("cji", "R4", 16, "g", "._sf_enot"))
    o.append(("cji", "R4", 0, "le", "._sf_fxs"))
    o.append(("cj", "R4", "R3", "ge", "._sf_fxb"))
    # 0 < decpt < nd: int part '.' frac part
    o.append(("set", "R5", 0))             # i
    L("._sf_c1")
    o.append(("cj", "R5", "R4", "ge", "._sf_c1d"))
    o.append(("spaddr", "R2", 0))
    o.append(("add", "R2", "R2", "R5"))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_c1"))
    L("._sf_c1d")
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 46))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("mov", "R5", "R4"))
    L("._sf_c2")
    o.append(("cj", "R5", "R3", "ge", "._sf_c2d"))
    o.append(("spaddr", "R2", 0))
    o.append(("add", "R2", "R2", "R5"))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_c2"))
    L("._sf_c2d")
    o.append(("jmp", "._sf_out"))
    L("._sf_fxb")                          # decpt >= nd: digits zeros '.' '0'
    o.append(("set", "R5", 0))
    L("._sf_c3")
    o.append(("cj", "R5", "R3", "ge", "._sf_c3d"))
    o.append(("spaddr", "R2", 0))
    o.append(("add", "R2", "R2", "R5"))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_c3"))
    L("._sf_c3d")
    o.append(("mov", "R5", "R3"))
    L("._sf_c4")                           # zeros to decpt
    o.append(("cj", "R5", "R4", "ge", "._sf_c4d"))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 48))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_c4"))
    L("._sf_c4d")
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 46))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("set", "R1", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("jmp", "._sf_out"))
    L("._sf_fxs")                          # decpt <= 0: '0' '.' zeros digits
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 48))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 46))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("neg", "R5", "R4"))          # -decpt zeros
    o.append(("set", "R2", 0))
    L("._sf_c5")
    o.append(("cj", "R2", "R5", "ge", "._sf_c5d"))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 48))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R2", "R2", 1))
    o.append(("jmp", "._sf_c5"))
    L("._sf_c5d")
    o.append(("set", "R5", 0))
    L("._sf_c6")
    o.append(("cj", "R5", "R3", "ge", "._sf_c6d"))
    o.append(("spaddr", "R2", 0))
    o.append(("add", "R2", "R2", "R5"))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_c6"))
    L("._sf_c6d")
    o.append(("jmp", "._sf_out"))
    L("._sf_enot")
    # digits[0]; ['.' digits[1..]] 'e' [+-] exp
    o.append(("spaddr", "R2", 0))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("cji", "R3", 1, "le", "._sf_e1"))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 46))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("set", "R5", 1))
    L("._sf_e0")
    o.append(("cj", "R5", "R3", "ge", "._sf_e1"))
    o.append(("spaddr", "R2", 0))
    o.append(("add", "R2", "R2", "R5"))
    o.append(("load8", "R1", "R2", 0))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_e0"))
    L("._sf_e1")
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("set", "R1", 101))           # 'e'
    o.append(("store8", "R1", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("sub", "R5", "R4", 1))       # exp = decpt_eff - 1
    o.append(("cji", "R5", 0, "ge", "._sf_e2"))
    o.append(("neg", "R5", "R5"))
    o.append(("set", "R2", 45))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R2", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("jmp", "._sf_e3"))
    L("._sf_e2")
    o.append(("set", "R2", 43))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R2", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    L("._sf_e3")
    o.append(("cji", "R5", 100, "l", "._sf_e4"))
    # 3 digits
    o.append(("set", "R1", 100))
    o.append(("set", "R1", 100))
    o.append(("udivmod", "R2", "R8", "R5", "R1"))   # q=exp/100, r
    o.append(("add", "R2", "R2", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R2", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("set", "R1", 10))
    o.append(("udivmod", "R2", "R8", "R8", "R1"))
    o.append(("add", "R2", "R2", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R2", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R8", "R8", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R8", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("jmp", "._sf_out"))
    L("._sf_e4")
    # 2 digits, zero padded
    o.append(("set", "R1", 10))
    o.append(("udivmod", "R2", "R8", "R5", "R1"))
    o.append(("add", "R2", "R2", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R2", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    o.append(("add", "R8", "R8", 48))
    o.append(("spaddr", "R7", 160))
    o.append(("add", "R7", "R7", "R6"))
    o.append(("store8", "R8", "R7", 0))
    o.append(("add", "R6", "R6", 1))
    L("._sf_out")
    o.append(("spaddr", "RET", 160))
    o.append(("mov", "RET2", "R6"))
    o.append(("addsp", 232))
    o.append(("ret",))
    # ---- special values ----
    L("._sf_nan")
    o.append(("addr", "RET", S_NAN))
    o.append(("set", "RET2", 3))
    o.append(("addsp", 232))
    o.append(("ret",))
    L("._sf_inf")
    
    o.append(("shr", "R3", "R0", 63))
    o.append(("cji", "R3", 0, "e", "._sf_inf1"))
    o.append(("addr", "RET", S_NINF))
    o.append(("set", "RET2", 4))
    o.append(("addsp", 232))
    o.append(("ret",))
    L("._sf_inf1")
    o.append(("addr", "RET", S_INF))
    o.append(("set", "RET2", 3))
    o.append(("addsp", 232))
    o.append(("ret",))
    # ---- approximate fallback (outside exact window) ----
    L("._sf_fallback")
    o.append(("fabs_f", "F1", "F0"))
    o.append(("fset", "F2", F_E22))
    o.append(("fset", "F3", F_TEN))
    o.append(("fset", "F4", F_SMALL))
    o.append(("set", "R6", 0))
    sst("R6", 152)                         # cnt
    L("._sf_fb1")
    o.append(("fj", "F1", "F2", "lt", "._sf_fb1d"))
    o.append(("fdiv", "F1", "F1", "F2"))
    sld("R6", 152)
    o.append(("add", "R6", "R6", 22))
    sst("R6", 152)
    o.append(("jmp", "._sf_fb1"))
    L("._sf_fb1d")
    L("._sf_fb2")
    o.append(("fj", "F1", "F3", "lt", "._sf_fb2d"))
    o.append(("fdiv", "F1", "F1", "F3"))
    sld("R6", 152)
    o.append(("add", "R6", "R6", 1))
    sst("R6", 152)
    o.append(("jmp", "._sf_fb2"))
    L("._sf_fb2d")
    L("._sf_fb3")
    o.append(("fj", "F1", "F4", "ge", "._sf_fb3d"))
    o.append(("fmul", "F1", "F1", "F2"))
    sld("R6", 152)
    o.append(("sub", "R6", "R6", 22))
    sst("R6", 152)
    o.append(("jmp", "._sf_fb3"))
    L("._sf_fb3d")
    o.append(("fset", "F5", F_HALF))
    
    # while F1 < 1: F1 *= 10; cnt -= 1
    o.append(("set64", "R5", 0x3FF0000000000000))
    o.append(("gprf", "F5", "R5"))         # F5 = 1.0
    L("._sf_fb4")
    o.append(("fj", "F1", "F5", "ge", "._sf_fb4d"))
    o.append(("fmul", "F1", "F1", "F3"))
    sld("R6", 152)
    o.append(("sub", "R6", "R6", 1))
    sst("R6", 152)
    o.append(("jmp", "._sf_fb4"))
    L("._sf_fb4d")
    # 17 digits
    o.append(("set", "R7", 0))
    L("._sf_fbd")
    o.append(("cji", "R7", 17, "ge", "._sf_fbdd"))
    o.append(("ftrunc", "F6", "F1"))
    o.append(("ftoi", "R5", "F6"))
    o.append(("add", "R5", "R5", 48))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R7"))
    o.append(("store8", "R5", "R3", 0))
    o.append(("itof", "F6", "R5"))
    o.append(("set", "R5", 48))
    o.append(("itof", "F7", "R5"))
    o.append(("fsub", "F6", "F6", "F7"))
    o.append(("fsub", "F1", "F1", "F6"))
    o.append(("fmul", "F1", "F1", "F3"))
    o.append(("add", "R7", "R7", 1))
    o.append(("jmp", "._sf_fbd"))
    L("._sf_fbdd")
    # D = digits as int (17)
    o.append(("set", "R4", 0))
    o.append(("set", "R5", 0))
    o.append(("spaddr", "R3", 0))
    L("._sf_fbai")
    o.append(("cji", "R5", 17, "ge", "._sf_fbaid"))
    o.append(("load8", "R1", "R3", 0))
    o.append(("sub", "R1", "R1", 48))
    o.append(("mul", "R4", "R4", 10))
    o.append(("add", "R4", "R4", "R1"))
    o.append(("add", "R3", "R3", 1))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._sf_fbai"))
    L("._sf_fbaid")
    # round: if F1 >= 0.5: D += 1
    o.append(("fset", "F5", F_HALF))
    o.append(("fcset", "R5", "F1", "F5", "ge"))
    o.append(("cji", "R5", 0, "e", "._sf_fbr"))
    o.append(("add", "R4", "R4", 1))
    L("._sf_fbr")
    # decpt = cnt + 1 (read cnt BEFORE D lands in the same tmp slot)
    sld("R5", 152)
    o.append(("add", "R5", "R5", 1))
    sst("R5", 72)                          # decpt
    o.append(("sub", "R5", "R5", 17))
    sst("R5", 120)                         # q = decpt - 17
    sst("R4", 152)                         # D (initial guess)
    # ---- correction loop: step D until D*10^q round-trips to |x| ----
    # The float-scaling above (x1e22 / x10 chains) is exact only to a few
    # ulps, so the extracted 17 digits can be off in the last place.  Each
    # D step moves D*10^q by ~0.45 ulp of x, so the walk is short and it
    # cannot skip over x.
    o.append(("set", "R6", 0))
    sst("R6", 136)                         # guard counter
    L("._sf_fbc")
    sld("A0", 152)                         # D
    sld("A1", 120)                         # q
    o.append(("call", "rt_f64_from_decimal"))
    sld("R2", 80)                          # xbits (reloaded: call clobbers R2)
    o.append(("cj", "RET", "R2", "e", "._sf_fbcd"))
    o.append(("cj", "RET", "R2", "b", "._sf_fbcu"))
    # y > x: D -= 1
    sld("R4", 152)
    o.append(("sub", "R4", "R4", 1))
    sst("R4", 152)
    o.append(("jmp", "._sf_fbcs"))
    L("._sf_fbcu")
    # y < x: D += 1
    sld("R4", 152)
    o.append(("add", "R4", "R4", 1))
    sst("R4", 152)
    L("._sf_fbcs")
    sld("R6", 136)
    o.append(("add", "R6", "R6", 1))
    sst("R6", 136)
    o.append(("cji", "R6", 5000, "l", "._sf_fbc"))
    L("._sf_fbcd")
    sld("R4", 152)                         # D (reloaded: R4 may be stale)
    o.append(("set64", "R6", 100000000000000000))
    o.append(("cj", "R4", "R6", "ne", "._sf_fbn"))
    # ---- carry: 17 nines rounded up -> digits "1", nd = 1, decpt + 1 ----
    o.append(("set", "R4", 1))
    sld("R5", 72)
    o.append(("add", "R5", "R5", 1))
    sst("R5", 72)
    o.append(("spaddr", "R3", 0))
    o.append(("set", "R5", 49))
    o.append(("store8", "R5", "R3", 0))
    o.append(("set", "R6", 1))
    sst("R6", 64)                          # nd = 1
    o.append(("jmp", "._sf_fbw"))
    L("._sf_fbn")
    # ---- write D as 17 zero-padded digits, nd = 17 ----
    sst("R4", 152)                         # D
    o.append(("set", "R6", 17))
    sst("R6", 64)                          # nd
    o.append(("set", "R7", 16))
    L("._sf_fbw")
    o.append(("cji", "R7", 0, "l", "._sf_fbwd"))
    o.append(("set", "R6", 10))
    o.append(("udivmod", "R4", "R5", "R4", "R6"))
    o.append(("add", "R5", "R5", 48))
    o.append(("spaddr", "R3", 0))
    o.append(("add", "R3", "R3", "R7"))
    o.append(("store8", "R5", "R3", 0))
    o.append(("sub", "R7", "R7", 1))
    o.append(("jmp", "._sf_fbw"))
    L("._sf_fbwd")
    # hand the exact digits to the shared shortest-round-trip search
    o.append(("jmp", "._sf_search"))

    # ======================================================================
    # rt_print_f64(x=F0)
    # ======================================================================
    L("rt_print_f64")
    o.append(("addr", "R1", "rt_data"))
    o.append(("store", "A0", "R1", B_FD))  # fd
    o.append(("call", "rt_str_from_f64"))
    o.append(("mov", "A1", "RET"))
    o.append(("mov", "A2", "RET2"))
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    o.append(("call", "rt_write"))
    o.append(("addr", "R1", "rt_data"))
    o.append(("load", "A0", "R1", B_FD))
    call_write_reg_fd(S_NL, 1)
    o.append(("ret",))

    # ======================================================================
    # rt_fmod(x=F0, y=F1) -> F0
    # ======================================================================
    L("rt_fmod")
    o.append(("fjisnan", "F0", "._fm_nan"))
    o.append(("fjisnan", "F1", "._fm_nan"))
    o.append(("fgpr", "R0", "F0"))
    o.append(("fgpr", "R1", "F1"))
    o.append(("set64", "R2", 0x7FFFFFFFFFFFFFFF))
    o.append(("mov", "R3", "R1"))
    o.append(("and", "R3", "R3", "R2"))
    o.append(("cji", "R3", 0, "e", "._fm_nan"))       # y == 0
    o.append(("set64", "R4", 0x7FF0000000000000))
    o.append(("mov", "R5", "R0"))
    o.append(("and", "R5", "R5", "R4"))
    o.append(("cj", "R5", "R4", "e", "._fm_nan"))     # x inf
    o.append(("mov", "R5", "R1"))
    o.append(("and", "R5", "R5", "R4"))
    o.append(("cj", "R5", "R4", "e", "._fm_retx"))    # y inf
    o.append(("mov", "R3", "R0"))
    o.append(("and", "R3", "R3", "R2"))
    o.append(("cji", "R3", 0, "e", "._fm_retx"))      # x == 0
    # extract (m, e) for x and y
    o.append(("set64", "R6", 0xFFFFFFFFFFFFF))
    o.append(("mov", "R3", "R0"))
    o.append(("and", "R3", "R3", "R6"))               # x frac
    o.append(("shr", "R7", "R0", 52))
    o.append(("and", "R7", "R7", 0x7FF))              # x exp field
    o.append(("cji", "R7", 0, "ne", "._fm_xn"))
    o.append(("set", "R8", -1074))                    # x denormal
    o.append(("jmp", "._fm_xg"))
    L("._fm_xn")
    o.append(("sub", "R8", "R7", 1075))
    o.append(("set64", "R5", 0x10000000000000))
    o.append(("or", "R3", "R3", "R5"))
    L("._fm_xg")                           # R3 = mx, R8 = ex
    o.append(("mov", "R4", "R1"))
    o.append(("and", "R4", "R4", "R6"))               # y frac
    o.append(("shr", "R7", "R1", 52))
    o.append(("and", "R7", "R7", 0x7FF))
    o.append(("cji", "R7", 0, "ne", "._fm_yn"))
    o.append(("set", "R5", -1074))
    o.append(("jmp", "._fm_yg"))
    L("._fm_yn")
    o.append(("sub", "R5", "R7", 1075))
    o.append(("set64", "R2", 0x10000000000000))
    o.append(("or", "R4", "R4", "R2"))
    L("._fm_yg")                           # R4 = my, R5 = ey
    # if |x| < |y| -> return x
    o.append(("cj", "R8", "R5", "l", "._fm_retx"))
    o.append(("cj", "R8", "R5", "g", "._fm_k"))
    o.append(("cj", "R3", "R4", "l", "._fm_retx"))
    L("._fm_k")
    # M = mx; k = ex - ey; while k: M = (M*2) % my
    o.append(("sub", "R6", "R8", "R5"))    # k
    L("._fm_loop")
    o.append(("cji", "R6", 0, "le", "._fm_loopd"))
    o.append(("shl", "R3", "R3", 1))
    o.append(("cj", "R3", "R4", "l", "._fm_nom"))
    o.append(("sub", "R3", "R3", "R4"))
    L("._fm_nom")
    o.append(("sub", "R6", "R6", 1))
    o.append(("jmp", "._fm_loop"))
    L("._fm_loopd")
    # M == 0 -> signed zero
    o.append(("cji", "R3", 0, "ne", "._fm_nz"))
    o.append(("set64", "R5", 0x8000000000000000))
    o.append(("and", "RET", "R0", "R5"))
    o.append(("gprf", "F0", "RET"))
    o.append(("ret",))
    L("._fm_nz")
    # result = M * 2^ey with sign of x
    o.append(("msb", "R6", "R3"))          # mz in [0, 52]
    o.append(("set", "R7", 52))
    o.append(("sub", "R7", "R7", "R6"))
    o.append(("shlv", "R3", "R3", "R7"))   # mant = M << (52 - mz)
    o.append(("add", "R5", "R5", "R6"))    # ey + mz
    o.append(("add", "R5", "R5", 1023))    # exp_field
    o.append(("cji", "R5", 0, "g", "._fm_nrm"))
    # denormal result (truncating; documented)
    o.append(("neg", "R7", "R5"))
    o.append(("add", "R7", "R7", 1))
    o.append(("shrv", "R3", "R3", "R7"))
    o.append(("set64", "R5", 0x8000000000000000))
    o.append(("and", "R5", "R0", "R5"))
    o.append(("or", "R5", "R5", "R3"))
    o.append(("gprf", "F0", "R5"))
    o.append(("ret",))
    L("._fm_nrm")
    o.append(("set64", "R6", 0x0020000000000000))
    o.append(("sub", "R3", "R3", "R6"))
    o.append(("shl", "R5", "R5", 52))
    o.append(("or", "R5", "R5", "R3"))
    o.append(("set64", "R6", 0x8000000000000000))
    o.append(("and", "R6", "R0", "R6"))
    o.append(("or", "R5", "R5", "R6"))
    o.append(("gprf", "F0", "R5"))
    o.append(("ret",))
    L("._fm_nan")
    o.append(("fadd", "F0", "F0", "F1"))   # NaN propagation
    o.append(("ret",))
    L("._fm_retx")
    o.append(("ret",))

    # ======================================================================
    # rt_parse_f64(p=A0, len=A1) -> RET double bits
    # ======================================================================
    L("rt_parse_f64")
    o.append(("subsp", 32))
    o.append(("cji", "A1", 0, "ne", "._pf_go"))
    panic(S_EF2, L_EF2)
    L("._pf_go")
    o.append(("set", "R5", 0))             # i
    o.append(("set", "R7", 0))             # neg
    o.append(("add", "R3", "A0", "R5"))
    o.append(("load8", "R0", "R3", 0))
    o.append(("cji", "R0", 43, "e", "._pf_pl"))
    o.append(("cji", "R0", 45, "e", "._pf_mn"))
    o.append(("jmp", "._pf_mloop"))
    L("._pf_pl")
    o.append(("set", "R5", 1))
    o.append(("jmp", "._pf_mloop"))
    L("._pf_mn")
    o.append(("set", "R5", 1))
    o.append(("set", "R7", 1))
    L("._pf_mloop")
    o.append(("set", "R6", 0))             # D
    o.append(("set", "R8", 0))             # nd
    o.append(("set", "R4", 0))             # q
    o.append(("set", "R0", 0))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "R0", "R1", 0))     # seen_dot = 0
    L("._pf_loop")
    o.append(("cj", "R5", "A1", "ge", "._pf_mdone"))
    o.append(("add", "R3", "A0", "R5"))
    o.append(("load8", "R0", "R3", 0))
    # '.'?
    o.append(("cji", "R0", 46, "e", "._pf_dot"))
    # digit?
    o.append(("cji", "R0", 48, "l", "._pf_exp"))
    o.append(("cji", "R0", 57, "g", "._pf_exp"))
    o.append(("sub", "R0", "R0", 48))
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R2", "R1", 0))      # seen_dot
    o.append(("cji", "R8", 18, "l", "._pf_keep"))
    # extra digits: scale the exponent instead (documented approximation)
    o.append(("cji", "R2", 0, "ne", "._pf_skip"))
    o.append(("add", "R4", "R4", 1))
    L("._pf_skip")
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._pf_loop"))
    L("._pf_keep")
    o.append(("mul", "R6", "R6", 10))
    o.append(("add", "R6", "R6", "R0"))
    o.append(("add", "R8", "R8", 1))
    o.append(("cji", "R2", 0, "e", "._pf_kd"))
    o.append(("sub", "R4", "R4", 1))
    L("._pf_kd")
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._pf_loop"))
    L("._pf_dot")
    o.append(("spaddr", "R1", 0))
    o.append(("load", "R2", "R1", 0))
    o.append(("cji", "R2", 0, "ne", "._pf_bad"))
    o.append(("set", "R0", 1))
    o.append(("spaddr", "R1", 0))
    o.append(("store", "R0", "R1", 0))
    o.append(("add", "R5", "R5", 1))
    o.append(("jmp", "._pf_loop"))
    L("._pf_exp")
    o.append(("cji", "R0", 101, "e", "._pf_e1"))
    o.append(("cji", "R0", 69, "e", "._pf_e1"))
    o.append(("jmp", "._pf_bad"))
    L("._pf_e1")
    o.append(("add", "R5", "R5", 1))
    o.append(("cj", "R5", "A1", "ge", "._pf_bad"))
    o.append(("add", "R3", "A0", "R5"))
    o.append(("load8", "R0", "R3", 0))
    o.append(("cji", "R0", 43, "e", "._pf_esp"))
    o.append(("cji", "R0", 45, "e", "._pf_esm"))
    o.append(("set", "R2", 0))
    o.append(("jmp", "._pf_ed_init"))
    L("._pf_esp")
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R2", 0))
    o.append(("jmp", "._pf_ed_init"))
    L("._pf_esm")
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R2", 1))
    L("._pf_ed_init")
    o.append(("set", "R1", 0))             # exp value
    o.append(("set", "R0", 0))
    o.append(("spaddr", "R3", 0))
    o.append(("store", "R0", "R3", 8))     # any exp digit = 0
    L("._pf_edl")
    o.append(("cj", "R5", "A1", "ge", "._pf_edld"))
    o.append(("add", "R3", "A0", "R5"))
    o.append(("load8", "R0", "R3", 0))
    o.append(("cji", "R0", 48, "l", "._pf_edld"))
    o.append(("cji", "R0", 57, "g", "._pf_edld"))
    o.append(("sub", "R0", "R0", 48))
    o.append(("mul", "R1", "R1", 10))
    o.append(("add", "R1", "R1", "R0"))
    o.append(("add", "R5", "R5", 1))
    o.append(("set", "R0", 1))
    o.append(("spaddr", "R3", 0))
    o.append(("store", "R0", "R3", 8))
    o.append(("jmp", "._pf_edl"))
    L("._pf_edld")
    o.append(("spaddr", "R3", 0))
    o.append(("load", "R2", "R3", 8))
    o.append(("cji", "R2", 0, "e", "._pf_bad"))
    o.append(("cji", "R2", 0, "e", "._pf_epos"))
    o.append(("neg", "R1", "R1"))
    L("._pf_epos")
    o.append(("add", "R4", "R4", "R1"))
    L("._pf_mdone")
    o.append(("cji", "R8", 0, "e", "._pf_bad"))
    # q range guard
    o.append(("cji", "R4", 38, "le", "._pf_q1"))
    o.append(("set64", "R5", 0x7FF0000000000000))
    o.append(("cji", "R7", 0, "e", "._pf_inf"))
    o.append(("set64", "R5", 0xFFF0000000000000))
    L("._pf_inf")
    o.append(("mov", "RET", "R5"))
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._pf_q1")
    o.append(("cji", "R4", -36, "ge", "._pf_q2"))
    o.append(("set", "RET", 0))
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._pf_q2")
    o.append(("mov", "A0", "R6"))
    o.append(("mov", "A1", "R4"))
    o.append(("call", "rt_f64_from_decimal"))
    o.append(("cji", "R7", 0, "e", "._pf_ret"))
    o.append(("set64", "R2", 0x8000000000000000))
    o.append(("or", "RET", "RET", "R2"))
    L("._pf_ret")
    o.append(("addsp", 32))
    o.append(("ret",))
    L("._pf_bad")
    panic(S_FBAD2, L_FBAD2)

    return o, ro
