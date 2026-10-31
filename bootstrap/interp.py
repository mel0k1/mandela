#!/usr/bin/env python3
"""DSL-level interpreter with x86-64 physical register semantics.

Executes the op lists produced by mc_rt.gen_runtime() + native.Lowering the
same way the emitted machine code would: abstract registers are mapped onto
physical x86-64 registers first (so aliasing bugs surface), and ops model the
sequential mutation of real instructions. Syscalls are emulated.

Usage: python3 interp.py program.mnd [args...]
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mandela
import native
import mc_rt
import mc_isa
import mc_x64

_BY_CONST = {mc_x64.RAX: "rax", mc_x64.RCX: "rcx", mc_x64.RDX: "rdx",
             mc_x64.RSI: "rsi", mc_x64.RDI: "rdi", mc_x64.R8: "r8",
             mc_x64.R9: "r9", mc_x64.R10: "r10", mc_x64.R11: "r11"}
PHYS = {num: _BY_CONST[num] for num in set(mc_isa.X64_R.values())}
NAME2NUM = {v: k for k, v in PHYS.items()}


def canon(name):
    """abstract reg name -> physical name (after ABI aliasing)"""
    if name in mc_isa.X64_ABI:
        name = mc_isa.X64_ABI[name]
    if name == "FREG":
        return "rbp"
    if name == "SP":
        return "rsp"
    if name in mc_isa.X64_R:
        return PHYS[mc_isa.X64_R[name]]
    raise RuntimeError("bad reg %r" % name)


M64 = (1 << 64) - 1


def s64(v):
    v &= M64
    return v - (1 << 64) if v >= (1 << 63) else v


def sx32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= (1 << 31) else v


CMP = {"e": lambda a, b: a == b, "ne": lambda a, b: a != b,
       "l": lambda a, b: a < b, "le": lambda a, b: a <= b,
       "g": lambda a, b: a > b, "ge": lambda a, b: a >= b,
       "b": lambda a, b: (a & M64) < (b & M64),
       "be": lambda a, b: (a & M64) <= (b & M64),
       "a": lambda a, b: (a & M64) > (b & M64),
       "ae": lambda a, b: (a & M64) >= (b & M64)}


class Machine:
    def __init__(self, ops):
        self.ops = ops
        self.labels = {}
        for i, op in enumerate(ops):
            if op[0] == "label":
                self.labels[op[1]] = i
        self.r = {n: 0 for n in NAME2NUM.keys()}
        self.r["rsp"] = 0x7000000
        self.r["rbp"] = 0x7000000
        self.f = {i: 0.0 for i in range(8)}
        self.mem = {}
        self.exit_code = None
        self.bump = 0x500000
        self.sysno = 0
        self.cur_pc = 0

    def rd8(self, addr):
        v = 0
        for i in range(8):
            v |= self.mem.get((addr + i) & M64, 0) << (8 * i)
        return v

    def wr8(self, addr, val):
        for i in range(8):
            self.mem[(addr + i) & M64] = (val >> (8 * i)) & 0xFF

    def rd1(self, addr):
        return self.mem.get(addr & M64, 0)

    def wr1(self, addr, val):
        self.mem[addr & M64] = val & 0xFF

    def wr_str(self, addr, data):
        for i, b in enumerate(data):
            self.mem[(addr + i) & M64] = b

    def run(self, max_steps=None):
        if max_steps is None:
            max_steps = int(os.environ.get("MND_MAXSTEPS", 2000000))
        pc = self.labels.get("_start", 0)
        self.calls = []
        steps = 0
        loop_pc = -1
        loop_ct = 0
        while pc < len(self.ops):
            steps += 1
            if steps > max_steps:
                raise RuntimeError("step limit at op %d %r; calls=%r"
                                   % (pc, self.ops[pc], self.calls[-4:]))
            if pc == loop_pc:
                loop_ct += 1
                if loop_ct == 300000:
                    raise RuntimeError(
                        "loop stuck at op %d %r rdi=%x rsi=%x rdx=%x "
                        "calls=%r" % (pc, self.ops[pc], self.r["rdi"],
                                      self.r["rsi"], self.r["rdx"],
                                      self.calls[-6:]))
            else:
                loop_pc = pc
                loop_ct = 0
            op = self.ops[pc]
            self.cur_pc = pc
            npc = pc + 1
            k = op[0]
            if k == "label" and os.environ.get("MND_LBL") in (None, "")                     and False:
                pass
            if k == "label" and op[1] in ("._sf_call", "._sf_found",
                                          "._sf_search", "._u2_can",
                                          "._u2_noexp", "._sf_pathA",
                                          "._sf_fallback", "._sf_norm",
                                          "._sf_bshld", "._sf_pfrac") \
                    and os.environ.get("MND_LBL"):
                sys.stderr.write("[%s] rax=%x rdi=%x rsi=%x rdx=%x r8=%x "
                                 "r9=%x r10=%x r11=%x\n"
                                 % (op[1], self.r["rax"], self.r["rdi"],
                                    self.r["rsi"], self.r["rdx"],
                                    self.r["r8"], self.r["r9"],
                                    self.r["r10"], self.r["r11"]))
            if os.environ.get("MND_OPS"):
                sys.stderr.write(
                    "op[%d] %r | rax=%x rcx=%x rdx=%x rsi=%x rdi=%x r8=%x "
                    "r9=%x r10=%x rsp=%x\n"
                    % (pc, op, self.r["rax"], self.r["rcx"], self.r["rdx"],
                       self.r["rsi"], self.r["rdi"], self.r["r8"],
                       self.r["r9"], self.r["r10"], self.r["rsp"]))
            if k == "label":
                pass
            elif k in ("set", "set64"):
                v = op[2]
                if isinstance(v, str):
                    d, sv = canon(op[1]), canon(v)
                    self.r[d] = self.r[sv]
                else:
                    self.r[canon(op[1])] = v & M64
            elif k == "mov":
                self.r[canon(op[1])] = self.r[canon(op[2])]
            elif k == "addr":
                self.r[canon(op[1])] = self.label_addrs[op[2]]
            elif k == "load":
                base = self.r[canon(op[2])]
                self.r[canon(op[1])] = self.rd8(base + sx32(op[3]))
            elif k == "store":
                base = self.r[canon(op[2])]
                self.wr8(base + sx32(op[3]), self.r[canon(op[1])])
                if (op[2] == "FREG" and op[3] == 40
                        and os.environ.get("MND_WATCH40")):
                    sys.stderr.write("W40 %r pc=%d val=%x\n"
                                     % (op, pc, self.r[canon(op[1])]))
            elif False:
                pass
            elif k == "load8":
                base = self.r[canon(op[2])]
                self.r[canon(op[1])] = self.rd1(base + sx32(op[3]))
            elif k == "store8":
                base = self.r[canon(op[2])]
                self.wr1(base + sx32(op[3]), self.r[canon(op[1])])
            elif k in ("add", "sub", "and", "or", "xor", "mul"):
                d, s1 = canon(op[1]), canon(op[2])
                s1v = self.r[s1]
                if isinstance(op[3], int):
                    s2v = op[3] & M64
                else:
                    s2v = self.r[canon(op[3])]
                if k == "add":
                    self.r[d] = (s1v + s2v) & M64
                elif k == "sub":
                    self.r[d] = (s1v - s2v) & M64
                elif k == "and":
                    self.r[d] = s1v & s2v
                elif k == "or":
                    self.r[d] = s1v | s2v
                elif k == "xor":
                    self.r[d] = s1v ^ s2v
                else:
                    self.r[d] = (s64(s1v) * s64(s2v)) & M64
            elif k in ("shl", "shr", "sar"):
                d, s1 = canon(op[1]), canon(op[2])
                v = self.r[s1]
                n = op[3] & 63
                if k == "shl":
                    self.r[d] = (v << n) & M64
                elif k == "shr":
                    self.r[d] = v >> n
                else:
                    self.r[d] = (s64(v) >> n) & M64
            elif k in ("shlv", "shrv", "sarv"):
                d, s1 = canon(op[1]), canon(op[2])
                v = self.r[s1]
                n = self.r[canon(op[3])] & 63
                if k == "shlv":
                    self.r[d] = (v << n) & M64
                elif k == "shrv":
                    self.r[d] = v >> n
                else:
                    self.r[d] = (s64(v) >> n) & M64
            elif k == "neg":
                d = canon(op[1])
                self.r[d] = (-self.r[d]) & M64
            elif k in ("div", "rem"):
                a = s64(self.r[canon(op[2])])
                b = s64(self.r[canon(op[3])])
                if b == 0:
                    raise RuntimeError("hardware div by zero")
                q = abs(a) // abs(b)
                if (a < 0) != (b < 0):
                    q = -q
                r = a - q * b
                d = canon(op[1])
                self.r[d] = (q if k == "div" else r) & M64
            elif k == "udivmod":
                a = self.r[canon(op[3])]
                b = self.r[canon(op[4])]
                if b == 0:
                    raise RuntimeError("hardware div by zero")
                q, r = a // b, a % b
                rq, rr = canon(op[1]), canon(op[2])
                self.r["rax"] = q
                self.r["rdx"] = r
                if rq != "rax":
                    self.r[rq] = q
                if rr != "rdx":
                    self.r[rr] = r
                if canon(op[4]) == "rdx" and rq != "rdx" and rr != "rdx":
                    self.r["rdx"] = b     # translator restores the divisor
            elif k == "msb":
                v = self.r[canon(op[2])]
                self.r[canon(op[1])] = v.bit_length() - 1
            elif k == "mulu128":
                p = self.r[canon(op[3])] * self.r[canon(op[4])]
                self.r[canon(op[1])] = (p >> 64) & M64
                self.r[canon(op[2])] = p & M64
            elif k in ("cset", "cseti"):
                if k == "cset":
                    a = s64(self.r[canon(op[2])])
                    b = s64(self.r[canon(op[3])])
                else:
                    a = s64(self.r[canon(op[2])])
                    b = sx32(op[3])
                self.r[canon(op[1])] = 1 if CMP[op[4]](a, b) else 0
            elif k == "jmp":
                npc = self.labels[op[1]]
            elif k in ("cj", "cji"):
                if k == "cj":
                    a = s64(self.r[canon(op[1])])
                    b = s64(self.r[canon(op[2])])
                    cc, lbl = op[3], op[4]
                else:
                    a = s64(self.r[canon(op[1])])
                    b = sx32(op[2])
                    cc, lbl = op[3], op[4]
                if CMP[cc](a, b):
                    npc = self.labels[lbl]
            elif k == "tj":
                if self.r[canon(op[1])] != 0:
                    npc = self.labels[op[2]]
            elif k == "call":
                if op[1] == "rt_memcpy" and os.environ.get("MND_TRACE"):
                    sys.stderr.write("memcpy dst=%x src=%x n=%d from op %d\n"
                                     % (self.r["rdi"], self.r["rsi"],
                                        self.r["rdx"], pc))
                self.calls.append(pc + 1)
                npc = self.labels[op[1]]
            elif k == "ret":
                if self.exit_code is not None:
                    return
                if not self.calls:
                    return
                npc = self.calls.pop()
            elif k == "push_rbp":
                self.r["rsp"] = (self.r["rsp"] - 8) & M64
                self.wr8(self.r["rsp"], self.r["rbp"])
            elif k == "pop_rbp":
                self.r["rbp"] = self.rd8(self.r["rsp"])
                self.r["rsp"] = (self.r["rsp"] + 8) & M64
            elif k in ("store_lr", "load_lr"):
                pass
            elif k == "subsp":
                self.r["rsp"] = (self.r["rsp"] - op[1]) & M64
            elif k == "addsp":
                self.r["rsp"] = (self.r["rsp"] + op[1]) & M64
            elif k == "spaddr":
                self.r[canon(op[1])] = (self.r["rsp"] + op[2]) & M64
            elif k == "sysno":
                self.sysno = {"write": 1, "exit": 231, "mmap": 9}[op[1]]
            elif k == "syscall":
                n = self.sysno
                if n == 1:
                    fd, buf, ln = self.r["rdi"], self.r["rsi"], self.r["rdx"]
                    if ln > 0x100000:
                        raise RuntimeError("write with bogus len=%d buf=0x%x"
                                           % (ln, buf))
                    data = bytes(self.mem.get((buf + i) & M64, 0)
                                 for i in range(ln))
                    if fd == 1:
                        sys.stdout.write(data.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    else:
                        sys.stderr.write(data.decode("utf-8", "replace"))
                elif n == 231:
                    self.exit_code = self.r["rdi"]
                    return
                elif n == 9:
                    self.r["rax"] = self.bump
                else:
                    raise RuntimeError("syscall %d" % n)
            elif k == "fset":
                self.f[int(op[1][1:])] = struct.unpack(
                    "<d", self.const_bytes[op[2]])[0]
            elif k == "fmov":
                self.f[int(op[1][1:])] = self.f[int(op[2][1:])]
            elif k in ("fadd", "fsub", "fmul", "fdiv"):
                import math
                a = self.f[int(op[2][1:])]
                b = self.f[int(op[3][1:])]
                if k == "fadd":
                    v = a + b
                elif k == "fsub":
                    v = a - b
                elif k == "fmul":
                    v = a * b
                else:
                    if a == 0 and b == 0:
                        v = math.nan
                    elif b == 0:
                        v = math.copysign(math.inf,
                                          math.copysign(1.0, a)
                                          * math.copysign(1.0, b))
                    else:
                        v = a / b
                self.f[int(op[1][1:])] = v
            elif k in ("fneg", "fabs", "fneg_f", "fabs_f"):
                v = self.f[int(op[2][1:])]
                self.f[int(op[1][1:])] = (-v if k in ("fneg", "fneg_f")
                                          else abs(v))
            elif k in ("ffloor", "fceil", "ftrunc", "fround"):
                import math
                v = self.f[int(op[2][1:])]
                fn = {"ffloor": math.floor, "fceil": math.ceil,
                      "ftrunc": math.trunc,
                      "fround": lambda x: math.floor(x + 0.5) if x >= 0
                      else math.ceil(x - 0.5)}[k]
                self.f[int(op[1][1:])] = fn(v)
            elif k == "fsqrt":
                import math
                self.f[int(op[1][1:])] = math.sqrt(self.f[int(op[2][1:])])
            elif k == "ftoi":
                self.r[canon(op[1])] = int(self.f[int(op[2][1:])]) & M64
            elif k == "itof":
                self.f[int(op[1][1:])] = float(s64(self.r[canon(op[2])]))
            elif k == "fgpr":
                self.r[canon(op[1])] = struct.unpack(
                    "<Q", struct.pack("<d", self.f[int(op[2][1:])]))[0]
            elif k == "gprf":
                self.f[int(op[1][1:])] = struct.unpack(
                    "<d", struct.pack("<Q", self.r[canon(op[2])]))[0]
            elif k == "fload":
                base = self.r[canon(op[2])]
                bits = self.rd8(base + sx32(op[3]))
                self.f[int(op[1][1:])] = struct.unpack(
                    "<d", struct.pack("<Q", bits))[0]
            elif k == "fstore":
                base = self.r[canon(op[2])]
                bits = struct.unpack(
                    "<Q", struct.pack("<d", self.f[int(op[1][1:])]))[0]
                self.wr8(base + sx32(op[3]), bits)
            elif k in ("fj", "fcset", "fjisnan"):
                if k == "fjisnan":
                    a = self.f[int(op[1][1:])]
                    res = a != a
                    if res:
                        npc = self.labels[op[2]]
                elif k == "fj":
                    a = self.f[int(op[1][1:])]
                    b = self.f[int(op[2][1:])]
                    cc = op[3]
                    if a != a or b != b:
                        res = cc == "ne"
                    else:
                        res = {"lt": a < b, "le": a <= b, "gt": a > b,
                               "ge": a >= b, "eq": a == b,
                               "ne": a != b}[cc]
                    if res:
                        npc = self.labels[op[4]]
                else:
                    a = self.f[int(op[2][1:])]
                    b = self.f[int(op[3][1:])]
                    cc = op[4]
                    if a != a or b != b:
                        res = cc == "ne"
                    else:
                        res = {"lt": a < b, "le": a <= b, "gt": a > b,
                               "ge": a >= b, "eq": a == b,
                               "ne": a != b}[cc]
                    self.r[canon(op[1])] = 1 if res else 0
            elif k == "int3":
                raise RuntimeError("int3 hit at op %d %r" % (pc, op))
            else:
                raise RuntimeError("interp: unknown op %r" % (op,))
            pc = npc
            if self.exit_code is not None:
                return

    def label_addr(self, name):
        return self.label_addrs[name]

    def do_syscall(self, pc):
        pass


def load_program(path):
    text = open(path, encoding="utf-8").read()
    toks = mandela.tokenize(text)
    ast = mandela.Parser(toks).parse()
    lw = native.Lowering("x86-64")
    lw.program(ast)
    rt_ops, rt_ro = mc_rt.gen_runtime()
    ops = rt_ops + lw.ops
    ro = dict(rt_ro)
    ro.update(lw.ro)
    m = Machine(ops)
    addr = 0x10000
    m.label_addrs = {}
    m.const_bytes = {}
    for name in m.labels:
        m.label_addrs[name] = addr
        addr += 8
    for name, data in ro.items():
        m.label_addrs[name] = addr
        m.const_bytes[name] = data
        m.wr_str(addr, data)
        addr += len(data) + 1
    m.label_addrs["rt_data"] = 0x20000
    m.label_addrs["mnd_g"] = 0x20100
    return m


if __name__ == "__main__":
    m = load_program(sys.argv[1])
    try:
        m.run()
    except RuntimeError as e:
        sys.stderr.write("interp: %s\n" % e)
        sys.exit(70)
    sys.exit(m.exit_code if m.exit_code is not None else 0)
