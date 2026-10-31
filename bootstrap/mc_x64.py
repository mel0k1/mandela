#!/usr/bin/env python3
"""x86-64 machine code encoder for the Mandela native backend (v0.6).

Emits raw System V AMD64 instruction bytes for the instruction subset the
native backend needs: integer ALU ops, SSE2 float ops, calls/branches with
rel32 fixups, syscall stubs. No external assembler is involved: every byte
is produced here.

Registers are named by their encoding number (0=RAX .. 15=R15). Memory
operands are restricted to [reg + disp32] forms (ModRM mod=10), which is
enough for rbp-relative frames.
"""

REX_W = 0x48          # REX prefix: 64-bit operand size
REX_B = 0x01          # extension bit for rm/r
REX_X = 0x02          # extension bit for index
REX_R = 0x04          # extension bit for reg

RAX, RCX, RDX, RBX = 0, 1, 2, 3
RSP, RBP, RSI, RDI = 4, 5, 6, 7
R8, R9, R10, R11 = 8, 9, 10, 11
R12, R13, R14, R15 = 12, 13, 14, 15

ARG_REGS = [RDI, RSI, RDX, RCX, R8, R9]     # integer-class args (SysV AMD64)
RET_REG = RAX
RET_REG2 = RDX                              # second half of a {ptr, len} pair

# condition codes for setcc/jcc (full second byte for setcc)
SE_O = 0x90  # seto
SE_NO = 0x91
SE_B = 0x92
SE_AE = 0x93
SE_E = 0x94
SE_NE = 0x95
SE_BE = 0x96
SE_A = 0x97
SE_S = 0x98
SE_NS = 0x99
SE_P = 0x9A
SE_NP = 0x9B
SE_L = 0x9C
SE_GE = 0x9D
SE_LE = 0x9E
SE_G = 0x9F

# branch opcodes (low byte after 0x0F)
JB_O = 0x80
JB_B = 0x82
JB_AE = 0x83
JB_E = 0x84
JB_NE = 0x85
JB_BE = 0x86
JB_A = 0x87
JB_S = 0x88
JB_NS = 0x89
JB_P = 0x8A
JB_NP = 0x8B
JB_L = 0x8C
JB_GE = 0x8D
JB_LE = 0x8E
JB_G = 0x8F


class Asm:
    """Byte buffer + label fixups. `kind` of a fixup is 'rel32' or 'abs64'."""

    def __init__(self):
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []            # (buf pos, label, kind)

    # -- labels ---------------------------------------------------------

    def label(self, name):
        """Bind `name` to the current position."""
        if name in self.labels:
            raise RuntimeError("duplicate label %s" % name)
        self.labels[name] = len(self.buf)

    def here(self):
        return len(self.buf)

    # -- low-level emitters ----------------------------------------------

    def byte(self, *bs):
        self.buf.extend(bs)

    def imm32(self, v):
        v &= 0xFFFFFFFF
        self.byte(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF)

    def imm64(self, v):
        for i in range(8):
            self.byte((v >> (8 * i)) & 0xFF)

    def _rex(self, reg, rm, w=True):
        """REX prefix covering reg (modrm.reg) and rm (modrm.rm or index)."""
        b = (REX_W if w else 0x40)
        if reg >= 8:
            b |= REX_R
        if rm >= 8:
            b |= REX_B
        if b != 0x40:
            self.byte(b)

    def modrm(self, mod, reg, rm):
        self.byte((mod << 6) | ((reg & 7) << 3) | (rm & 7))

    def mem_rm(self, base, disp, reg):
        """modrm for [base + disp32] with given reg field; returns True if
        a SIB byte is required (rsp/rbp-free bases never need one here)."""
        self._rex(reg, base)
        self.modrm(2, reg, base)
        self.imm32(disp)

    # -- moves -------------------------------------------------------------

    def mov_rr(self, dst, src):
        """dst = src (64-bit)."""
        self._rex(src, dst)
        self.byte(0x89)
        self.modrm(3, src, dst)

    def mov_ir(self, dst, imm):
        """dst = imm (sign-extended imm32 when it fits, else full imm64)."""
        if -(1 << 31) <= imm < (1 << 31):
            self._rex(0, dst)
            self.byte(0xC7)
            self.modrm(3, 0, dst)
            self.imm32(imm)
        else:
            if dst >= 8:
                self.byte(REX_W | REX_B)
            else:
                self.byte(REX_W)
            self.byte(0xB8 + (dst & 7))
            self.imm64(imm & 0xFFFFFFFFFFFFFFFF)

    def _mem_rex(self, base, reg):
        b = REX_W
        if reg >= 8:
            b |= REX_R
        if base >= 8:
            b |= REX_B
        self.byte(b)

    def _mem_modrm(self, base, disp, reg):
        self.modrm(2, reg, base)
        self.imm32(disp & 0xFFFFFFFF)

    def _sse_mem_rex(self, base, xmm_reg):
        """REX for an SSE memory operand (no REX.W; extends reg/rm past 7)."""
        b = 0x40
        if xmm_reg >= 8:
            b |= REX_R
        if base >= 8:
            b |= REX_B
        if b != 0x40:
            self.byte(b)

    def mov_rm(self, dst, base, disp):
        """dst = [base + disp]."""
        self._mem_rex(base, dst)
        self.byte(0x8B)
        self._mem_modrm(base, disp, dst)

    def mov_mr(self, base, disp, src):
        """[base + disp] = src."""
        self._mem_rex(base, src)
        self.byte(0x89)
        self._mem_modrm(base, disp, src)

    def mov_addr(self, dst, label):
        """dst = absolute address of label (patched after layout)."""
        if dst >= 8:
            self.byte(REX_W | REX_B)
        else:
            self.byte(REX_W)
        self.byte(0xB8 + (dst & 7))
        self.fixups.append((len(self.buf), label, "abs64"))
        self.imm64(0)

    def lea_rbp(self, dst, disp):
        """dst = rbp + disp (disp is negative for locals)."""
        self._rex(0, dst)
        self.byte(0x8D)
        self.modrm(2, dst, RBP)
        self.imm32(disp & 0xFFFFFFFF)

    # -- integer ALU -----------------------------------------------------

    def _alu_rr(self, opbyte, dst, src):
        self._rex(src, dst)
        self.byte(opbyte)
        self.modrm(3, src, dst)

    def _alu_ir(self, digit, dst, imm):
        self._rex(0, dst)
        self.byte(0x81)
        self.modrm(3, digit, dst)
        self.imm32(imm)

    def add_rr(self, dst, src):
        self._alu_rr(0x01, dst, src)

    def sub_rr(self, dst, src):
        self._alu_rr(0x29, dst, src)

    def and_rr(self, dst, src):
        self._alu_rr(0x21, dst, src)

    def or_rr(self, dst, src):
        self._alu_rr(0x09, dst, src)

    def xor_rr(self, dst, src):
        self._alu_rr(0x31, dst, src)

    def cmp_rr(self, a, b):
        """compare a with b (flags from a - b)."""
        self._alu_rr(0x39, a, b)

    def add_ir(self, dst, imm):
        self._alu_ir(0, dst, imm)

    def or_ir(self, dst, imm):
        self._alu_ir(1, dst, imm)

    def and_ir(self, dst, imm):
        self._alu_ir(4, dst, imm)

    def sub_ir(self, dst, imm):
        self._alu_ir(5, dst, imm)

    def xor_ir(self, dst, imm):
        self._alu_ir(6, dst, imm)

    def cmp_ir(self, dst, imm):
        self._alu_ir(7, dst, imm)

    def imul_rr(self, dst, src):
        """dst *= src."""
        self._rex(dst, src)
        self.byte(0x0F, 0xAF)
        self.modrm(3, dst, src)

    def imul_ir(self, dst, imm):
        """dst *= imm (imul r64, r/m64, imm32)."""
        self._rex(dst, dst)
        self.byte(0x69)
        self.modrm(3, dst, dst)
        self.imm32(imm & 0xFFFFFFFF)

    def idiv_r(self, reg):
        """signed divide rdx:rax by reg (quotient rax, remainder rdx)."""
        self._rex(7, reg)
        self.byte(0xF7)
        self.modrm(3, 7, reg)

    def mul_r(self, reg):
        """unsigned multiply rax * reg -> rdx:rax."""
        self._rex(4, reg)
        self.byte(0xF7)
        self.modrm(3, 4, reg)

    def udiv_r(self, reg):
        """unsigned divide rdx:rax by reg (quotient rax, remainder rdx)."""
        self._rex(6, reg)
        self.byte(0xF7)
        self.modrm(3, 6, reg)

    def bsr(self, dst, src):
        """dst = index of highest set bit of src (src != 0)."""
        self._rex(dst, src)
        self.byte(0x0F, 0xBD)
        self.modrm(3, dst, src)

    def cqo(self):
        self.byte(REX_W, 0x99)

    def neg_r(self, reg):
        self._rex(3, reg)
        self.byte(0xF7)
        self.modrm(3, 3, reg)

    def not_r(self, reg):
        self._rex(2, reg)
        self.byte(0xF7)
        self.modrm(3, 2, reg)

    def shl_ir(self, reg, n):
        self._rex(4, reg)
        self.byte(0xC1)
        self.modrm(3, 4, reg)
        self.byte(n & 0x3F)

    def _shift_cl(self, digit, reg):
        self._rex(digit, reg)
        self.byte(0xD3)
        self.modrm(3, digit, reg)

    def shl_cl(self, reg):
        self._shift_cl(4, reg)

    def shr_cl(self, reg):
        self._shift_cl(5, reg)

    def sar_cl(self, reg):
        self._shift_cl(7, reg)

    def sar_ir(self, reg, n):
        self._rex(7, reg)
        self.byte(0xC1)
        self.modrm(3, 7, reg)
        self.byte(n & 0x3F)

    def shr_ir(self, reg, n):
        self._rex(5, reg)
        self.byte(0xC1)
        self.modrm(3, 5, reg)
        self.byte(n & 0x3F)

    def test_rr(self, a, b):
        self._alu_rr(0x85, a, b)

    # -- setcc / movzx -----------------------------------------------------

    def setcc(self, op, reg8dst):
        """set<cc> reg8; op is the full second byte (0x94 = sete, ...)."""
        if reg8dst >= 8:
            self.byte(0x41)
        self.byte(0x0F, op)
        self.modrm(3, 0, reg8dst)

    def movzx_r8(self, dst, src):
        self._rex(src, dst)
        self.byte(0x0F, 0xB6)
        self.modrm(3, dst, src)

    def set_bool(self, cc, tmp=RCX):
        """rax = (flags <cc> 0) as 0/1 in a full register. cc: full opcode."""
        self.setcc(cc, tmp)
        self.movzx_r8(RAX, tmp)

    # -- control flow -------------------------------------------------------

    def call(self, label):
        self.byte(0xE8)
        self.fixups.append((len(self.buf), label, "rel32"))
        self.imm32(0)

    def call_reg(self, reg):
        if reg >= 8:
            self.byte(0x41)
        self.byte(0xFF)
        self.modrm(3, 2, reg)

    def jmp(self, label):
        self.byte(0xE9)
        self.fixups.append((len(self.buf), label, "rel32"))
        self.imm32(0)

    def jcc(self, cc, label):
        """cc is the full low byte (0x84 = je, ...)."""
        self.byte(0x0F, cc)
        self.fixups.append((len(self.buf), label, "rel32"))
        self.imm32(0)

    def push_r(self, reg):
        if reg >= 8:
            self.byte(0x41)
        self.byte(0x50 + (reg & 7))

    def pop_r(self, reg):
        if reg >= 8:
            self.byte(0x41)
        self.byte(0x58 + (reg & 7))

    def ret(self):
        self.byte(0xC3)

    def syscall(self):
        self.byte(0x0F, 0x05)

    # -- SSE2 float --------------------------------------------------------

    def _sse(self, prefix, op, src_xmm, dst_xmm):
        rex = 0x40 | (REX_R if dst_xmm >= 8 else 0) | (REX_B if src_xmm >= 8 else 0)
        if rex != 0x40:
            self.byte(rex)
        self.byte(prefix, 0x0F, op)
        self.modrm(3, dst_xmm, src_xmm)

    def movsd_rr(self, dst, src):
        self._sse(0xF2, 0x10, src, dst)     # movsd dst, src (reg,reg form)

    def movsd_rm(self, dst, base, disp):
        self.byte(0xF2)                 # legacy prefix precedes REX
        self._sse_mem_rex(base, dst)
        self.byte(0x0F, 0x10)
        self._mem_modrm(base, disp, dst)

    def movsd_mr(self, base, disp, src):
        self.byte(0xF2)
        self._sse_mem_rex(base, src)
        self.byte(0x0F, 0x11)
        self._mem_modrm(base, disp, src)

    def movapd_rr(self, dst, src):
        self._sse(0x66, 0x28, src, dst)

    def addsd(self, dst, src):
        self._sse(0xF2, 0x58, src, dst)

    def subsd(self, dst, src):
        self._sse(0xF2, 0x5C, src, dst)

    def mulsd(self, dst, src):
        self._sse(0xF2, 0x59, src, dst)

    def divsd(self, dst, src):
        self._sse(0xF2, 0x5E, src, dst)

    def sqrtsd(self, dst, src):
        self._sse(0xF2, 0x51, src, dst)

    def ucomisd(self, a, b):
        self._sse(0x66, 0x2E, b, a)

    def andpd(self, dst, src):
        self._sse(0x66, 0x54, src, dst)

    def orpd(self, dst, src):
        self._sse(0x66, 0x56, src, dst)

    def xorpd(self, dst, src):
        self._sse(0x66, 0x57, src, dst)

    def cvtsi2sd(self, xmm_dst, gpr_src):
        self.mem_dummy_gpr = None
        rex = REX_W | (REX_R if xmm_dst >= 8 else 0) | (REX_B if gpr_src >= 8 else 0)
        self.byte(0xF2, rex, 0x0F, 0x2A)
        self.modrm(3, xmm_dst, gpr_src)

    def cvttsd2si(self, gpr_dst, xmm_src):
        rex = REX_W | (REX_R if gpr_dst >= 8 else 0) | (REX_B if xmm_src >= 8 else 0)
        self.byte(0xF2, rex, 0x0F, 0x2C)
        self.modrm(3, gpr_dst, xmm_src)

    def movq_gx(self, gpr_dst, xmm_src):
        """gpr = bits of xmm (movq r64, xmm)."""
        rex = REX_W | (REX_R if xmm_src >= 8 else 0) | (REX_B if gpr_dst >= 8 else 0)
        self.byte(0x66, rex, 0x0F, 0x7E)
        self.modrm(3, xmm_src, gpr_dst)

    def movq_xg(self, xmm_dst, gpr_src):
        """xmm = bits from gpr (movq xmm, r64)."""
        rex = REX_W | (REX_R if xmm_dst >= 8 else 0) | (REX_B if gpr_src >= 8 else 0)
        self.byte(0x66, rex, 0x0F, 0x6E)
        self.modrm(3, xmm_dst, gpr_src)

    def roundsd(self, dst, src, mode):
        """mode: 0=nearest-even 1=down 2=up 3=toward-zero (SSE4.1)."""
        rex = 0x40 | (REX_R if dst >= 8 else 0) | (REX_B if src >= 8 else 0)
        if rex != 0x40:
            self.byte(rex)
        self.byte(0x66, 0x0F, 0x3A, 0x0B)
        self.modrm(3, dst, src)
        self.byte(mode & 0x07)

    # -- fixup resolution ---------------------------------------------------

    def resolve(self, base_addr=0):
        """Patch every fixup. Returns final bytes."""
        for pos, label, kind in self.fixups:
            target = self.labels[label]
            if target is None:
                raise RuntimeError("label %s never bound" % label)
            if kind == "rel32":
                rel = (base_addr + target) - (base_addr + pos + 4)
                for i in range(4):
                    self.buf[pos + i] = (rel >> (8 * i)) & 0xFF
            else:  # abs64
                addr = base_addr + target
                for i in range(8):
                    self.buf[pos + i] = (addr >> (8 * i)) & 0xFF
        return bytes(self.buf)
