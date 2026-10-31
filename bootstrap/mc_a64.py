#!/usr/bin/env python3
"""AArch64 (ARM64) machine code encoder for the Mandela native backend (v0.6).

Emits raw AAPCS64 instruction words for the subset the native backend needs:
integer ALU ops, floating-point (VFP/ASIMD scalar) ops, branches and calls
with fixups, svc stubs. Every 4-byte word is produced here — no external
assembler, linker or libc is involved.

Register numbering: 64-bit integer registers x0..x30 (31 = zero register
xzr/wzr, 30 = lr). Floating point: v0..v31 (used as 64-bit `d` registers).
"""

X0, X1, X2, X3 = 0, 1, 2, 3
X4, X5, X6, X7 = 4, 5, 6, 7
X8, X9, X10, X11 = 8, 9, 10, 11
X12, X13, X14, X15 = 12, 13, 14, 15
X16, X17, X18, X19 = 16, 17, 18, 19
X20, X21, X22, X23 = 20, 21, 22, 23
X24, X25, X26, X27 = 24, 25, 26, 27
X28, X29, X30, XZR = 28, 29, 30, 31
FP = X29
LR = X30

V0, V1, V2, V3 = 0, 1, 2, 3
V4, V5, V6, V7 = 4, 5, 6, 7

ARG_REGS = [X0, X1, X2, X3, X4, X5, X6, X7]   # integer-class args (AAPCS64)
RET_REG = X0
RET_REG2 = X1

SF = 1 << 31          # sf bit: 64-bit operation

# condition codes
EQ, NE, CS, CC, MI, PL, VS, VC = 0, 1, 2, 3, 4, 5, 6, 7
HI, LS, GE, LT, GT, LE, ALNV = 8, 9, 10, 11, 12, 13, 14
# NOTE: unsigned comparisons on floats use CS/CC ("higher or same"/"carry"),
# which is what fcmp produces for > / >= etc. handled via dedicated methods.


class Asm:
    def __init__(self):
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []            # (pos, label, kind)

    # -- labels ---------------------------------------------------------

    def label(self, name):
        if name in self.labels:
            raise RuntimeError("duplicate label %s" % name)
        # words are 4-aligned by construction
        self.labels[name] = len(self.buf)

    # -- low-level --------------------------------------------------------

    def word(self, insn):
        self.buf.extend((insn & 0xFFFFFFFF).to_bytes(4, "little"))

    def byte(self, *bs):
        self.buf.extend(bs)

    def _chk(self, r):
        if not 0 <= r <= 31:
            raise RuntimeError("bad register %r" % r)

    # -- moves --------------------------------------------------------------

    def movz(self, rd, imm16, shift):
        """movz x(rd), imm16 lsl shift (shift in {0,16,32,48})."""
        self._chk(rd)
        assert 0 <= imm16 < (1 << 16) and shift in (0, 16, 32, 48)
        self.word(SF | (0xA5 << 23) | (shift // 16) << 21 | (imm16 << 5) | rd)

    def movk(self, rd, imm16, shift):
        self._chk(rd)
        assert 0 <= imm16 < (1 << 16) and shift in (0, 16, 32, 48)
        self.word(SF | (0xE5 << 23) | (shift // 16) << 21 | (imm16 << 5) | rd)

    def mov_ir(self, rd, imm):
        """Load a 64-bit constant into rd (movz + movk pairs)."""
        self._chk(rd)
        v = imm & 0xFFFFFFFFFFFFFFFF
        if -(1 << 15) <= v < (1 << 15) or 0 <= v < (1 << 16):
            self.movz(rd, v & 0xFFFF, 0)
        else:
            need = [v & 0xFFFF, (v >> 16) & 0xFFFF, (v >> 32) & 0xFFFF, (v >> 48) & 0xFFFF]
            first = next(i for i, h in enumerate(need) if h != 0)
            self.movz(rd, need[first], first * 16)
            for i in range(first + 1, 4):
                if need[i]:
                    self.movk(rd, need[i], i * 16)

    def mov_rr(self, rd, rm):
        """rd = rm (mov x rd, x rm  ==  orr rd, xzr, rm)."""
        self._chk(rd)
        self._chk(rm)
        self.word(SF | (0x150 << 21) | (rm << 16) | (31 << 5) | rd)

    def mov_mr(self, base, disp, rt):
        """str rt, [base, #disp] (disp byte offset, multiple of 8, < 32768)."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x8000 and disp % 8 == 0
        self.word(0xF9 << 24 | (disp // 8) << 10 | base << 5 | rt)

    def mov_rm(self, rt, base, disp):
        """ldr rt, [base, #disp]."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x8000 and disp % 8 == 0
        self.word(0xF9 << 24 | (1 << 22) | (disp // 8) << 10 | base << 5 | rt)

    def mov_mr32(self, base, disp, rt):
        """str w(rt), [base, #disp] (4-byte store, disp multiple of 4)."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x10000 and disp % 4 == 0
        self.word(0xB9 << 24 | (disp // 4) << 10 | base << 5 | rt)

    def mov_rm32(self, rt, base, disp):
        """ldr w(rt), [base, #disp]."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x10000 and disp % 4 == 0
        self.word(0xB9 << 24 | (1 << 22) | (disp // 4) << 10 | base << 5 | rt)

    def strb(self, rt, base, disp=0):
        """strb w(rt), [base, #disp] (byte store, disp 0..4095)."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x1000
        self.word(0x39 << 24 | disp << 10 | base << 5 | rt)

    def ldrb(self, rt, base, disp=0):
        """ldrb w(rt), [base, #disp]."""
        self._chk(base)
        self._chk(rt)
        assert 0 <= disp < 0x1000
        self.word(0x39 << 24 | (1 << 22) | disp << 10 | base << 5 | rt)

    def stp_pre(self, rt1, rt2, base, disp):
        """stp rt1, rt2, [base, #disp]! (pre-index writeback)."""
        self._chk(base)
        assert -512 <= disp < 512 and disp % 8 == 0
        imm7 = disp // 8 & 0x7F
        self.word((0xA9 << 24) | (1 << 23) | ((imm7 & 0x7F) << 15) | (rt2 << 10) |
                  (base << 5) | rt1)

    def stp(self, rt1, rt2, base, disp):
        """stp rt1, rt2, [base, #disp] (64-bit offset form)."""
        self._chk(base)
        assert -512 <= disp < 512 and disp % 8 == 0
        imm7 = disp // 8 & 0x7F
        self.word((0xA9 << 24) | ((imm7 & 0x7F) << 15) | (rt2 << 10) |
                  (base << 5) | rt1)

    def ldp_post(self, rt1, rt2, base, disp):
        """ldp rt1, rt2, [base], #disp (post-index writeback)."""
        self._chk(base)
        assert -512 <= disp < 512 and disp % 8 == 0
        imm7 = disp // 8 & 0x7F
        self.word((0xA8 << 24) | (1 << 23) | (1 << 22) | ((imm7 & 0x7F) << 15) |
                  (rt2 << 10) | (base << 5) | rt1)

    def ldp(self, rt1, rt2, base, disp):
        self._chk(base)
        assert -512 <= disp < 512 and disp % 8 == 0
        imm7 = disp // 8 & 0x7F
        self.word((0xA9 << 24) | (1 << 22) | ((imm7 & 0x7F) << 15) | (rt2 << 10) |
                  (base << 5) | rt1)

    def add_imm(self, rd, rn, imm12):
        """add rd, rn, #imm12 (0..4095)."""
        self._chk(rd)
        self._chk(rn)
        assert 0 <= imm12 < 0x1000
        self.word(SF | (0x22 << 23) | (imm12 << 10) | (rn << 5) | rd)

    def sub_imm(self, rd, rn, imm12):
        self._chk(rd)
        self._chk(rn)
        assert 0 <= imm12 < 0x1000
        self.word(SF | (0xA2 << 23) | (imm12 << 10) | (rn << 5) | rd)

    def add_rr(self, rd, rn, rm):
        """rd = rn + rm."""
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x58 << 21) | (rm << 16) | (rn << 5) | rd)

    def sub_rr(self, rd, rn, rm):
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x258 << 21) | (rm << 16) | (rn << 5) | rd)

    def orr_rr(self, rd, rn, rm):
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x150 << 21) | (rm << 16) | (rn << 5) | rd)

    def and_rr(self, rd, rn, rm):
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x50 << 21) | (rm << 16) | (rn << 5) | rd)

    def eor_rr(self, rd, rn, rm):
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x250 << 21) | (rm << 16) | (rn << 5) | rd)

    def shl_ir(self, rd, rn, n):
        """rd = rn << n (UBFM alias)."""
        assert 0 <= n < 64
        self.word(SF | (0xA6 << 23) | (1 << 22) | (((64 - n) & 63) << 16) |
                  ((63 - n) << 10) | (rn << 5) | rd)

    def shr_ir(self, rd, rn, n):
        """rd = rn >> n logical (UBFM alias)."""
        assert 0 <= n < 64
        self.word(SF | (0xA6 << 23) | (1 << 22) | (n << 16) | (63 << 10) |
                  (rn << 5) | rd)

    def sar_ir(self, rd, rn, n):
        """rd = rn >> n arithmetic (SBFM alias)."""
        assert 0 <= n < 64
        self.word(SF | (0x26 << 23) | (1 << 22) | (n << 16) | (63 << 10) |
                  (rn << 5) | rd)

    def adr(self, rd, label):
        """adr rd, label (PC-relative, ±1MB)."""
        self.fixups.append((len(self.buf), label, "adr"))
        self.word(0x10000000 | rd)

    def and_imm(self, rd, rn, immr, imms):
        """and rd, rn, #bitmask encoded as (immr, imms) for N=1 64-bit masks."""
        self._chk(rd)
        self._chk(rn)
        self.word(SF | (0x24 << 23) | (1 << 22) | (immr << 16) | (imms << 10) |
                  (rn << 5) | rd)

    def lslv(self, rd, rn, rm):
        """rd = rn << rm (variable)."""
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0xD6 << 21) | (rm << 16) | (0x08 << 10) | (rn << 5) | rd)

    def lsrv(self, rd, rn, rm):
        """rd = rn >> rm (logical, variable)."""
        self.word(SF | (0xD6 << 21) | (rm << 16) | (0x09 << 10) | (rn << 5) | rd)

    def asrv(self, rd, rn, rm):
        """rd = rn >> rm (arithmetic, variable)."""
        self.word(SF | (0xD6 << 21) | (rm << 16) | (0x0A << 10) | (rn << 5) | rd)

    def mul_rr(self, rd, rn, rm):
        """rd = rn * rm (madd with xzr)."""
        self._chk(rd)
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x1B << 24) | (rm << 16) | (31 << 10) | (rn << 5) | rd)

    def madd(self, rd, rn, rm, ra):
        """rd = ra + rn * rm."""
        self.word(SF | (0x1B << 24) | (rm << 16) | (ra << 10) | (rn << 5) | rd)

    def msub(self, rd, rn, rm, ra):
        """rd = ra - rn * rm."""
        self.word(SF | (0x1B << 24) | (1 << 15) | (rm << 16) | (ra << 10) |
                  (rn << 5) | rd)

    def add_sp_reg(self, rm):
        """add sp, sp, x(rm) (extended-register form)."""
        self.word(0x8B2063FF | (rm << 16))

    def sub_sp_reg(self, rm):
        """sub sp, sp, x(rm) (extended-register form)."""
        self.word(0xCB2063FF | (rm << 16))

    def umulh(self, rd, rn, rm):
        """rd = high 64 bits of rn * rm (unsigned)."""
        self.word(SF | (0x1B << 24) | (1 << 21) | (rm << 16) | (31 << 10) |
                  (rn << 5) | rd)

    def clz(self, rd, rn):
        """rd = count of leading zero bits of rn."""
        self.word(0xDAC01000 | (rn << 5) | rd)

    def sdiv(self, rd, rn, rm):
        """rd = rn / rm (truncated; rm==0 or overflow -> rn==min/-1 gives rn)."""
        self.word(SF | (0xD6 << 21) | (rm << 16) | (3 << 10) | (rn << 5) | rd)

    def udiv(self, rd, rn, rm):
        self.word(SF | (0xD6 << 21) | (rm << 16) | (2 << 10) | (rn << 5) | rd)

    def cset(self, rd, cond):
        """rd = (cond ? 1 : 0)  ==  csinc rd, xzr, xzr, invert(cond)."""
        self.word(SF | (0xD4 << 21) | (31 << 16) | ((cond ^ 1) << 12) | 0x400 |
                  (31 << 5) | rd)

    def cmp_rr(self, rn, rm):
        """cmp rn, rm (flags from rn - rm) == subs xzr, rn, rm."""
        self._chk(rn)
        self._chk(rm)
        self.word(SF | (0x358 << 21) | (rm << 16) | (rn << 5) | 31)

    def cmp_imm(self, rn, imm12):
        """cmp rn, #imm12."""
        self._chk(rn)
        assert 0 <= imm12 < 0x1000
        self.word(SF | (0xE2 << 23) | (imm12 << 10) | (rn << 5) | 31)

    def tst_rr(self, rn, rm):
        self.word(SF | (0x350 << 21) | (rm << 16) | (rn << 5) | 31)

    def neg_r(self, rd, rm):
        """rd = -rm (sub rd, xzr, rm)."""
        self._chk(rm)
        self.word(SF | (0x258 << 21) | (rm << 16) | (31 << 5) | rd)

    # -- control flow ---------------------------------------------------------

    def b(self, label):
        self.fixups.append((len(self.buf), label, "b"))
        self.word(0x14000000)

    def bl(self, label):
        self.fixups.append((len(self.buf), label, "b"))
        self.word(0x94000000)

    def bcond(self, cond, label):
        self.fixups.append((len(self.buf), label, "bcond"))
        self.word((0x54 << 24) | cond)

    def blr(self, rn):
        self.word(0xD63F0000 | (rn << 5))

    def br(self, rn):
        self.word(0xD61F0000 | (rn << 5))

    def ret_r(self, rn):
        self.word(0xD65F0000 | (rn << 5))

    def ret(self):
        self.word(0xD65F03C0)

    def svc(self, imm):
        assert 0 <= imm < (1 << 16)
        self.word((0xD4 << 24) | (imm << 5) | 1)

    def nop(self):
        self.word(0xD503201F)

    def brk(self):
        self.word(0xD4200000)

    # -- floating point --------------------------------------------------------

    def fmov_rr(self, vd, vn):
        """fmov d(vd), d(vn)."""
        self.word(0x1E604000 | (vn << 5) | vd)

    def fmov_ir(self, vd, imm8):
        """fmov d(vd), #imm8 (IEEE double immediate: 3-bit exp, 4-bit mantissa)."""
        assert 0 <= imm8 < 0x100
        self.word(0x1E601000 | (imm8 << 13) | vd)

    def fmov_gx(self, vd, xn):
        """fmov d(vd), x(n) (bit copy)."""
        self.word(0x9E670000 | (xn << 5) | vd)

    def fmov_xg(self, xd, vn):
        """fmov x(xd), d(vn)."""
        self.word(0x9E660000 | (vn << 5) | xd)

    def fadd(self, vd, vn, vm):
        self.word(0x1E600800 | (2 << 12) | (vm << 16) | (vn << 5) | vd)

    def fsub(self, vd, vn, vm):
        self.word(0x1E600800 | (3 << 12) | (vm << 16) | (vn << 5) | vd)

    def fmul(self, vd, vn, vm):
        self.word(0x1E600800 | (0 << 12) | (vm << 16) | (vn << 5) | vd)

    def fdiv(self, vd, vn, vm):
        self.word(0x1E600800 | (1 << 12) | (vm << 16) | (vn << 5) | vd)

    def fsqrt(self, vd, vn):
        self.word(0x1E604000 | (3 << 15) | (1 << 16) | (vn << 5) | vd)

    def fabs_f(self, vd, vn):
        self.word(0x1E604000 | (1 << 15) | (vn << 5) | vd)

    def fneg_f(self, vd, vn):
        self.word(0x1E604000 | (1 << 16) | (vn << 5) | vd)

    def frintm(self, vd, vn):
        """floor."""
        self.word(0x1E604000 | 0x50000 | (vn << 5) | vd)

    def frintp(self, vd, vn):
        """ceil."""
        self.word(0x1E604000 | 0x48000 | (vn << 5) | vd)

    def frinta(self, vd, vn):
        """round half away from zero (C round())."""
        self.word(0x1E604000 | 0x60000 | (vn << 5) | vd)

    def frintz(self, vd, vn):
        """truncate toward zero."""
        self.word(0x1E604000 | 0x58000 | (vn << 5) | vd)

    def fcmp(self, vn, vm):
        """fcmp d(vn), d(vm) -> flags (N=lt, Z=eq, C=ge, V=unordered)."""
        self.word(0x1E602000 | (vm << 16) | (vn << 5))

    def fcmp_zero(self, vn):
        """fcmp d(vn), #0.0."""
        self.word(0x1E602000 | 0x8 | (vn << 5))

    def scvtf(self, vd, xn):
        """scvtf d(vd), x(n)."""
        self.word(0x9E620000 | (xn << 5) | vd)

    def fcvtzs(self, xd, vn):
        """fcvtzs x(d), d(vn) (truncate)."""
        self.word(0x9E780000 | (vn << 5) | xd)

    def fmov_mr(self, base, disp, vt):
        """str d(vt), [base, #disp] (disp multiple of 8)."""
        self._chk(base)
        assert 0 <= disp < 0x8000 and disp % 8 == 0
        self.word(0xFD << 24 | (disp // 8) << 10 | base << 5 | vt)

    def fmov_rm(self, vt, base, disp):
        """ldr d(vt), [base, #disp]."""
        self._chk(base)
        assert 0 <= disp < 0x8000 and disp % 8 == 0
        self.word(0xFD << 24 | (1 << 22) | (disp // 8) << 10 | base << 5 | vt)

    # -- fixups ------------------------------------------------------------------

    def resolve(self, base_addr=0):
        for pos, label, kind in self.fixups:
            target = self.labels[label]
            if target is None:
                raise RuntimeError("label %s never bound" % label)
            insn_pos = pos
            rel = (base_addr + target) - (base_addr + insn_pos)
            insn = int.from_bytes(self.buf[insn_pos:insn_pos + 4], "little")
            if kind == "b":
                assert rel % 4 == 0 and -(1 << 27) <= rel < (1 << 27)
                insn |= ((rel >> 2) & 0x3FFFFFF)
            elif kind == "bcond":
                assert rel % 4 == 0 and -1048576 <= rel < 1048576
                insn |= ((rel >> 2) & 0x7FFFF) << 5
            elif kind == "adr":
                assert rel % 4 == 0 and -(1 << 20) <= rel < (1 << 20)
                insn |= ((rel >> 2) & 0x7FFFF) << 5
            else:
                raise RuntimeError("bad fixup kind %s" % kind)
            self.buf[insn_pos:insn_pos + 4] = insn.to_bytes(4, "little")
        return bytes(self.buf)
