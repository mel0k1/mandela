#!/usr/bin/env python3

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mandela
import mc_isa
import mc_rt
import mc_x64
import mc_a64
import mc_elf

class NativeError(Exception):
    def __init__(self, msg, line=0, col=0):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col

def where(n):
    if isinstance(n, list) and len(n) > 0 and isinstance(n[-1], int):
        return n[-1], n[-2] if isinstance(n[-2], int) else 0
    return 0, 0

STRRET2 = {"x86-64": "R2", "arm64": "R1"}
ARGI = {"x86-64": mc_isa.X64_ARG_I, "arm64": mc_a64 and mc_isa.A64_ARG_I}
ARGF = {"x86-64": mc_isa.X64_ARG_F, "arm64": mc_isa.A64_ARG_F}

INT_REGS_MAX = {"x86-64": 6, "arm64": 8}
FLT_REGS_MAX = 8

UNSUPPORTED_STMT = {
    "struct": "`struct` definitions",
    "unsafe": "`unsafe` blocks",
    "import": "module imports",
    "from": "module imports",
}
UNSUPPORTED_EXPR = {
    "list": "list literals",
    "dict": "dict literals",
    "slit": "struct literals",
    "field": "field access",
}

def type_of_ann(ann, ln):
    if ann in ("int", "f64", "bool", "str", "nil", ""):
        return ann if ann else "nil"
    raise NativeError("native backend: type `%s` is not supported yet "
                      "(lists, dicts, structs and pointers stay on the C "
                      "backend in v0.6)" % ann, ln, 0)

class Lowering:
    def __init__(self, arch):
        self.arch = arch
        self.ops = []
        self.ro = {}
        self._rl = 0
        self.strret2 = STRRET2[arch]
        self.argi = mc_isa.X64_ARG_I if arch == "x86-64" else mc_isa.A64_ARG_I
        self.argf = mc_isa.X64_ARG_F if arch == "x86-64" else mc_isa.A64_ARG_F
        self.ni = INT_REGS_MAX[arch]

        self.fns = {}
        self.globals = {}
        self.gsize = 0
        self.scopes = [{}]
        self.slot = 0
        self.maxslot = 0
        self.spill = 0
        self.maxspill = 0
        self.loopstack = []
        self.epilogue = None
        self.fret = "nil"
        self._uniq = 0
        self._fl = {}

    def uniq(self):
        self._uniq += 1
        return "..%d" % self._uniq

    def emit(self, *op):
        self.ops.append(op)

    def L(self, name):
        self.emit("label", name)

    def slit(self, text):
        name = "u_s%d" % self._rl
        self._rl += 1
        self.ro[name] = text.encode("utf-8")
        return name, len(self.ro[name])

    def fconst(self, value):
        key = struct.pack("<d", value)
        if key not in self._fl:
            name = "u_f%d" % self._rl
            self._rl += 1
            self.ro[name] = key
            self._fl[key] = name
        return self._fl[key]

    def newscope(self):
        self.scopes.append({})

    def endscope(self):
        self.scopes.pop()

    def declare(self, name, ty):
        n = 2 if ty == "str" else 1
        idx = self.slot
        self.slot += n
        self.maxslot = max(self.maxslot, self.slot)
        self.scopes[-1][name] = (idx, ty, False)
        if os.environ.get("MND_SLOTS"):
            sys.stderr.write("slot %s = %d (%s)\n" % (name, idx, ty))
        return idx

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        if name in self.globals:
            return self.globals[name]
        return None

    def ldslot(self, r, idx):
        self.emit("load", r, "FREG", 8 * idx)

    def stslot(self, r, idx):
        self.emit("store", r, "FREG", 8 * idx)

    def ldfslot(self, f, idx):
        self.emit("fload", f, "FREG", 8 * idx)

    def stfslot(self, f, idx):
        self.emit("fstore", f, "FREG", 8 * idx)

    def spill_alloc(self, n):
        base = self.maxslot + self.spill
        self.spill += n
        self.maxspill = max(self.maxspill, self.spill)
        return base

    def panic(self, label, ln_):
        self.emit("addr", "A0", label)
        self.emit("set", "A1", ln_)
        self.emit("jmp", "rt_panic")

    def expr_type(self, n):
        tag = n[0]
        if tag == "num":
            return "int"
        if tag == "fnum":
            return "f64"
        if tag == "str":
            return "str"
        if tag == "bool":
            return "bool"
        if tag == "nil":
            return "nil"
        if tag == "var":
            v = self.lookup(n[1])
            if v is None:
                raise NativeError("undefined variable `%s`" % n[1])
            return v[1]
        if tag == "bin":
            op = n[1]
            if op in ("==", "!=", "<", "<=", ">", ">=", "and", "or"):
                return "bool"
            lt = self.expr_type(n[2])
            if op == "+" and lt == "str":
                return "str"
            rt = self.expr_type(n[3])
            if lt == "f64" or rt == "f64":
                return "f64"
            return "int"
        if tag == "un":
            if n[1] == "!":
                return "bool"
            return self.expr_type(n[2])
        if tag == "call":
            return self.call_type(n)
        if tag == "index":
            bt = self.expr_type(n[1])
            if bt == "str":
                return "str"
            raise NativeError("native backend: indexing is only supported on "
                              "`str` in v0.6")
        raise NativeError("native backend: unsupported expression `%s`"
                          % UNSUPPORTED_EXPR.get(tag, tag))

    def call_type(self, n):
        f = n[1]
        if f[0] != "var":
            raise NativeError("native backend: method calls are not "
                              "supported yet")
        name = f[1]
        if name in ("print", "eprint", "exit", "fatal"):
            return "nil"
        if name == "len":
            return "int"
        if name == "str":
            return "str"
        if name in ("int", "f64", "chr", "ord", "abs", "sqrt", "floor",
                    "ceil", "round", "substr"):
            sig = {"int": None, "f64": None, "chr": "str", "ord": "int",
                   "abs": None, "sqrt": "f64", "floor": "f64", "ceil": "f64",
                   "round": "f64", "substr": "str"}
            if name in ("int",):
                return "int"
            if name in ("f64",):
                return "f64"
            if name == "abs":
                return self.expr_type(n[2][0])
            return sig[name]
        if name in self.fns:
            return self.fns[name][1]
        raise NativeError("native backend: unknown function `%s`" % name)

    def gate(self, n):
        tag = n[0]
        ln = n[-1] if isinstance(n[-1], int) else 0
        if tag in UNSUPPORTED_STMT:
            raise NativeError("native backend: %s are not supported yet"
                              % UNSUPPORTED_STMT[tag], ln)
        if tag in UNSUPPORTED_EXPR:
            raise NativeError("native backend: %s are not supported yet"
                              % UNSUPPORTED_EXPR[tag], ln)
        if tag == "def":
            fname = n[1]
            pts = [type_of_ann(p[1], ln) for p in n[2]]
            ret = type_of_ann(n[3], ln)
            self.fns[fname] = (pts, ret)
            self.newscope()
            for p, pt in zip(n[2], pts):
                self.scopes[-1][p[0]] = (0, pt, False)
            for s in n[4]:
                self.gate(s)
            self.endscope()
        elif tag == "let":
            self.expr_type_gate(n[3])
            ty = self.expr_type(n[3])
            self.scopes[-1][n[1]] = (0, ty, False)
        elif tag == "assign":
            if n[1][0] != "var":
                raise NativeError("native backend: only plain variables can "
                                  "be assigned in v0.6", ln)
            self.expr_type_gate(n[2])
        elif tag == "expr":
            self.expr_type_gate(n[1])
        elif tag == "return":
            if n[1] is not None:
                self.expr_type_gate(n[1])
        elif tag == "if":
            for cond, body in n[1]:
                self.expr_type_gate(cond)
                self.newscope()
                for s in body:
                    self.gate(s)
                self.endscope()
            if n[2] is not None:
                self.newscope()
                for s in n[2]:
                    self.gate(s)
                self.endscope()
        elif tag == "while":
            self.expr_type_gate(n[1])
            self.newscope()
            for s in n[2]:
                self.gate(s)
            self.endscope()
        elif tag == "for":
            it = n[2]
            if it[0] == "call" and it[1][0] == "var" and it[1][1] == "range":
                for a in it[2]:
                    self.expr_type_gate(a)
                self.newscope()
                self.scopes[-1][n[1]] = (0, "int", False)
            else:
                vt = self.expr_type(it)
                self.newscope()
                self.scopes[-1][n[1]] = (0, vt, False)
            for s in n[3]:
                self.gate(s)
            self.endscope()
        elif tag in ("break", "continue"):
            pass

    def expr_type_gate(self, n):
        tag = n[0]
        ln = n[-1] if isinstance(n[-1], int) else 0
        if tag in UNSUPPORTED_EXPR:
            raise NativeError("native backend: %s are not supported yet"
                              % UNSUPPORTED_EXPR[tag], ln)
        if tag == "call":
            f = n[1]
            if f[0] == "field":
                raise NativeError("native backend: method calls are not "
                                  "supported yet", ln)
            name = f[1]
            if name in ("push", "pop", "split", "read_file", "write_file",
                        "args", "pow", "alloc", "arena_new", "arena_alloc",
                        "arena_free", "ptr_len", "is_null"):
                raise NativeError("native backend: `%s` is not supported yet"
                                  % name, ln)
            for a in n[2]:
                self.expr_type_gate(a)
        elif tag == "bin":
            self.expr_type_gate(n[2])
            self.expr_type_gate(n[3])
        elif tag == "un":
            self.expr_type_gate(n[2])
        elif tag == "index":
            bt = self.expr_type(n[1])
            if bt != "str":
                raise NativeError("native backend: indexing is only "
                                  "supported on `str` in v0.6", ln)
            self.expr_type_gate(n[1])
            self.expr_type_gate(n[2])
        elif tag == "var":
            self.expr_type(n)

    def gen_expr(self, n):
        tag = n[0]
        if tag == "num":
            v = n[1]
            if v > (1 << 63) - 1:
                v = mandela.wrap64(v)
            if v == -(1 << 63):
                self.emit("set64", "R0", 0x8000000000000000)
            elif -(1 << 31) <= v < (1 << 31):
                self.emit("set", "R0", v)
            else:
                self.emit("set64", "R0", v & 0xFFFFFFFFFFFFFFFF)
            return "int"
        if tag == "fnum":
            lbl = self.fconst(n[1])
            self.emit("fset", "F0", lbl)
            return "f64"
        if tag == "str":
            lbl, ln_ = self.slit(n[1])
            self.emit("addr", "R0", lbl)
            self.emit("set", self.strret2, ln_)
            return "str"
        if tag == "bool":
            self.emit("set", "R0", 1 if n[1] else 0)
            return "bool"
        if tag == "nil":
            self.emit("set", "R0", 0)
            return "nil"
        if tag == "var":
            v = self.lookup(n[1])
            if v is None:
                raise NativeError("undefined variable `%s`" % n[1])
            idx, ty, isg = v
            if isg:
                self.emit("addr", "R8", "mnd_g")
                if ty == "f64":
                    self.emit("fload", "F0", "R8", idx)
                elif ty == "str":
                    self.emit("load", "R0", "R8", idx)
                    self.emit("load", self.strret2, "R8", idx + 8)
                else:
                    self.emit("load", "R0", "R8", idx)
                return ty
            if ty == "f64":
                self.ldfslot("F0", idx)
            elif ty == "str":
                self.ldslot("R0", idx)
                self.ldslot(self.strret2, idx + 1)
            else:
                self.ldslot("R0", idx)
            return ty
        if tag == "bin":
            return self.gen_bin(n)
        if tag == "un":
            ty = self.expr_type(n[2])
            self.gen_expr(n[2])
            if n[1] == "-":
                if ty == "f64":
                    self.emit("fneg", "F0", "F0")
                else:
                    self.emit("neg", "R0", "R0")
            else:
                self.emit("xor", "R0", "R0", 1)
            return ty
        if tag == "call":
            return self.gen_call(n)
        if tag == "index":
            entry = self.spill
            bt = self.expr_type(n[1])
            self.gen_expr(n[1])
            sb = self.spill_alloc(2)
            self.stslot("R0", sb)
            self.stslot(self.strret2, sb + 1)
            self.spill = sb + 2
            self.gen_expr(n[2])
            self.emit("mov", "A2", "R0")
            self.ldslot("A0", sb)
            self.ldslot("A1", sb + 1)
            self.spill = entry
            self.emit("call", "rt_str_index")
            self.emit("mov", self.strret2, "RET2")
            self.emit("mov", "R0", "RET")
            return "str"
        raise NativeError("native backend: unsupported expression `%s`" % tag)

    def gen_bin(self, n):
        op = n[1]
        ln = n[-1] if isinstance(n[-1], int) else 0
        if op in ("and", "or"):
            self.gen_expr(n[2])
            l1 = self.uniq() + "r"
            ld = self.uniq() + "d"
            if op == "and":
                self.emit("tj", "R0", l1)
                self.emit("set", "R0", 0)
                self.emit("jmp", ld)
                self.L(l1)
                self.gen_expr(n[3])
                self.L(ld)
            else:
                self.emit("tj", "R0", ld)
                self.gen_expr(n[3])
                self.L(ld)
            return "bool"
        lt = self.expr_type(n[2])
        rt = self.expr_type(n[3])
        if op == "+" and lt == "str" and rt == "str":
            return self.gen_str_concat(n)
        if lt == "f64" or rt == "f64":
            return self.gen_fbin(n, op, ln)
        return self.gen_ibin(n, op, ln)

    def gen_str_concat(self, n):
        entry = self.spill
        self.gen_expr(n[2])
        sb = self.spill_alloc(2)
        self.stslot("R0", sb)
        self.stslot(self.strret2, sb + 1)
        self.spill = sb + 2
        self.gen_expr(n[3])

        self.emit("mov", "A3", self.strret2)
        self.emit("mov", "A2", "R0")
        self.ldslot("A0", sb)
        self.ldslot("A1", sb + 1)
        self.spill = entry
        self.emit("call", "rt_str_concat")
        self.emit("mov", self.strret2, "RET2")
        self.emit("mov", "R0", "RET")
        return "str"

    def gen_ibin(self, n, op, ln):
        entry = self.spill
        self.gen_expr(n[2])
        sb = self.spill_alloc(1)
        self.stslot("R0", sb)
        self.spill = sb + 1
        self.gen_expr(n[3])
        self.emit("mov", "R1", "R0")
        self.ldslot("R0", sb)
        self.spill = entry
        if op in ("==", "!=", "<", "<=", ">", ">="):
            cc = {"==": "e", "!=": "ne", "<": "l", "<=": "le",
                  ">": "g", ">=": "ge"}[op]
            self.emit("cset", "R0", "R0", "R1", cc)
            return "bool"
        if op == "+":
            self.emit("add", "R0", "R0", "R1")
        elif op == "-":
            self.emit("sub", "R0", "R0", "R1")
        elif op == "*":
            self.emit("mul", "R0", "R0", "R1")
        elif op in ("/", "%"):
            n1 = self.uniq() + "n"
            self.emit("cji", "R1", 0, "ne", n1)
            msg, mlen = self.slit("attempt to divide by zero" if op == "/"
                                  else "attempt to calculate the remainder "
                                       "with a divisor of zero")
            self.emit("addr", "A0", msg)
            self.emit("set", "A1", mlen)
            self.emit("jmp", "rt_panic")
            self.L(n1)
            if op == "/":
                dodiv = self.uniq() + "dv"
                done = self.uniq() + "dn"
                self.emit("cji", "R1", -1, "ne", dodiv)
                self.emit("set64", "R2", 0x8000000000000000)
                self.emit("cj", "R0", "R2", "ne", dodiv)
                self.emit("neg", "R0", "R0")
                self.emit("jmp", done)
                self.L(dodiv)
                self.emit("div", "R0", "R0", "R1")
                self.L(done)
            else:
                rem1 = self.uniq() + "r1"
                done = self.uniq() + "dn"
                self.emit("cji", "R1", -1, "ne", rem1)
                self.emit("set", "R0", 0)
                self.emit("jmp", done)
                self.L(rem1)
                self.emit("rem", "R0", "R0", "R1")
                self.L(done)
        return "int"

    def gen_fbin(self, n, op, ln):
        entry = self.spill
        self.gen_expr(n[2])
        sb = self.spill_alloc(1)
        self.emit("fstore", "F0", "FREG", 8 * sb)
        self.spill = sb + 1
        self.gen_expr(n[3])
        self.emit("fload", "F1", "FREG", 8 * sb)
        self.spill = entry
        if op in ("==", "!=", "<", "<=", ">", ">="):
            cc = {"==": "eq", "!=": "ne", "<": "lt", "<=": "le",
                  ">": "gt", ">=": "ge"}[op]
            self.emit("fcset", "R0", "F1", "F0", cc)
            return "bool"
        if op == "%":
            self.emit("call", "rt_fmod")
            return "f64"
        self.emit({"+" : "fadd", "-": "fsub", "*": "fmul",
                   "/": "fdiv"}[op], "F0", "F1", "F0")
        return "f64"

    def gen_call(self, n):
        f = n[1]
        name = f[1]
        args = n[2]
        ln = n[-1] if isinstance(n[-1], int) else 0

        if name in ("print", "eprint"):
            if len(args) != 1:
                raise NativeError("%s() takes exactly 1 argument" % name, ln)
            fd = 1 if name == "print" else 2
            ty = self.gen_expr(args[0])
            if ty == "f64":
                raise NativeError("print of f64 is not supported by the "
                                  "native backend yet; use the C backend",
                                  ln)
            self.emit("set", "A0", fd)
            if ty == "f64":
                self.emit("call", "rt_print_f64")
            elif ty == "str":
                self.emit("mov", "A2", self.strret2)
                self.emit("mov", "A1", "R0")
                self.emit("call", "rt_print_str")

            elif ty == "bool":
                self.emit("mov", "A1", "R0")
                self.emit("call", "rt_print_bool")
            elif ty == "nil":
                self.emit("call", "rt_print_nil")
            else:
                self.emit("mov", "A1", "R0")
                self.emit("call", "rt_print_int")
            return "nil"
        if name == "len":
            ty = self.expr_type(args[0])
            self.gen_expr(args[0])
            if ty == "str":
                self.emit("mov", "R0", self.strret2)
            return "int"
        if name == "str":
            ty = self.expr_type(args[0])
            self.gen_expr(args[0])
            if ty == "int":
                self.emit("mov", "A0", "R0")
                self.emit("call", "rt_str_from_int")
                self.emit("mov", self.strret2, "RET2")
                self.emit("mov", "R0", "RET")
            elif ty == "bool":
                t_lbl, t_len = self.slit("true")
                f_lbl, f_len = self.slit("false")
                z = self.uniq() + "z"
                self.emit("cji", "R0", 0, "ne", z)
                self.emit("addr", "R0", f_lbl)
                self.emit("set", self.strret2, f_len)
                self.emit("jmp", z + "e")
                self.L(z)
                self.emit("addr", "R0", t_lbl)
                self.emit("set", self.strret2, t_len)
                self.L(z + "e")
            elif ty == "nil":
                lbl, llen = self.slit("nil")
                self.emit("addr", "R0", lbl)
                self.emit("set", self.strret2, llen)
            elif ty == "f64":
                self.emit("call", "rt_str_from_f64")
                self.emit("mov", self.strret2, "RET2")
                self.emit("mov", "R0", "RET")
            return "str"
        if name == "int":
            entry = self.spill
            ty = self.expr_type(args[0])
            self.gen_expr(args[0])
            self.spill = entry
            if ty == "str":
                self.emit("mov", "A0", "R0")
                self.emit("mov", "A1", self.strret2)
                self.emit("call", "rt_parse_int")
            elif ty == "f64":
                bad = self.uniq() + "b"
                ok = self.uniq() + "k"
                self.emit("fjisnan", "F0", bad)
                self.emit("fset", "F2", self.fconst(9223372036854775808.0))
                self.emit("fabs_f", "F3", "F0")
                self.emit("fj", "F3", "F2", "lt", ok)
                msg, mlen = self.slit("number too large to fit in target type")
                self.emit("addr", "A0", msg)
                self.emit("set", "A1", mlen)
                self.emit("jmp", "rt_panic")
                L_bad = bad
                self.L(L_bad)
                msg2, mlen2 = self.slit("cannot convert `nan` to `int`")
                self.emit("addr", "A0", msg2)
                self.emit("set", "A1", mlen2)
                self.emit("jmp", "rt_panic")
                self.L(ok)
                self.emit("ftoi", "R0", "F0")
            return "int"
        if name == "f64":
            ty = self.expr_type(args[0])
            self.gen_expr(args[0])
            if ty == "int":
                self.emit("itof", "F0", "R0")
            elif ty == "str":
                self.emit("mov", "A0", "R0")
                self.emit("mov", "A1", self.strret2)
                self.emit("call", "rt_parse_f64" if False else "rt_parse_f64")
            return "f64"
        if name == "chr":
            self.gen_expr(args[0])
            self.emit("mov", "A0", "R0")
            self.emit("call", "rt_chr")
            self.emit("mov", self.strret2, "RET2")
            self.emit("mov", "R0", "RET")
            return "str"
        if name == "ord":
            self.gen_expr(args[0])
            self.emit("mov", "A0", "R0")
            self.emit("mov", "A1", self.strret2)
            self.emit("call", "rt_ord")
            self.emit("mov", "R0", "RET")
            return "int"
        if name == "abs":
            ty = self.expr_type(args[0])
            self.gen_expr(args[0])
            if ty == "f64":
                self.emit("fabs_f", "F0", "F0")
            else:
                z = self.uniq() + "z"
                self.emit("cji", "R0", 0, "ge", z)
                self.emit("neg", "R0", "R0")
                self.L(z)
            return ty
        if name in ("sqrt", "floor", "ceil", "round"):
            self.gen_expr(args[0])
            self.emit({"sqrt": "fsqrt", "floor": "ffloor",
                       "ceil": "fceil", "round": "fround"}[name], "F0", "F0")
            return "f64"
        if name == "substr":
            entry = self.spill
            self.gen_expr(args[0])
            sb = self.spill_alloc(2)
            self.stslot("R0", sb)
            self.stslot(self.strret2, sb + 1)
            self.spill = sb + 2
            self.gen_expr(args[1])
            self.emit("mov", "A2", "R0")
            self.gen_expr(args[2])
            self.emit("mov", "A3", "R0")
            self.ldslot("A0", sb)
            self.ldslot("A1", sb + 1)
            self.spill = entry
            self.emit("call", "rt_substr")
            self.emit("mov", self.strret2, "RET2")
            self.emit("mov", "R0", "RET")
            return "str"
        if name == "exit":
            self.gen_expr(args[0])
            self.emit("mov", "A0", "R0")
            self.emit("call", "rt_exit")
            return "nil"
        if name == "fatal":
            self.gen_expr(args[0])
            self.emit("mov", "A1", self.strret2)
            self.emit("mov", "A0", "R0")
            self.emit("call", "rt_fatal")
            return "nil"
        if name == "range":
            raise NativeError("`range` is only allowed in `for` loops", ln)

        if name not in self.fns:
            raise NativeError("native backend: unknown function `%s`" % name,
                              ln)
        ptypes, ret = self.fns[name]
        if len(args) != len(ptypes):
            raise NativeError("native backend: `%s` takes %d arguments but "
                              "%d were given" % (name, len(ptypes),
                                                 len(args)), ln)

        entry = self.spill
        staging = []
        istk = 0
        fstk = 0
        for a, pt in zip(args, ptypes):
            ty = self.gen_expr(a)
            if ty == "f64":
                base = self.spill_alloc(1)
                self.emit("fstore", "F0", "FREG", 8 * base)
                staging.append(("f", base))
                fstk += 1
            elif ty == "str":
                base = self.spill_alloc(2)
                self.stslot("R0", base)
                self.stslot(self.strret2, base + 1)
                staging.append(("s", base))
                istk += 2
            else:
                base = self.spill_alloc(1)
                self.stslot("R0", base)
                staging.append(("i", base))
                istk += 1

        ireg = 0
        freg = 0
        stack_args = []
        for kind, base in staging:
            if kind == "f":
                if freg < 8:
                    self.ldfslot(self.argf[freg], base)
                    freg += 1
                else:
                    stack_args.append(("f", base))
            elif kind == "s":
                if ireg + 1 < self.ni:
                    self.ldslot(self.argi[ireg], base)
                    self.ldslot(self.argi[ireg + 1], base + 1)
                    ireg += 2
                else:
                    stack_args.append(("s", base))
            else:
                if ireg < self.ni:
                    self.ldslot(self.argi[ireg], base)
                    ireg += 1
                else:
                    stack_args.append(("i", base))
        self.spill = entry
        nstack = len(stack_args)
        if nstack:
            nbytes = ((nstack * 8 + 15) // 16) * 16
            self.emit("subsp", nbytes)
            for i, (kind, base) in enumerate(stack_args):
                if kind == "f":
                    self.ldfslot("F7", base)
                    self.emit("fstore", "F7", "SP", i * 8)
                else:
                    self.ldslot("R8", base)
                    if kind == "s":
                        self.ldslot("R2", base + 1)
                        self.emit("store", "R2", "SP", i * 8 + 8)

                    self.emit("store", "R8", "SP", i * 8)
        self.emit("call", "fn_" + name)
        if nstack:
            nbytes = ((nstack * 8 + 15) // 16) * 16
            self.emit("addsp", nbytes)
        if ret == "f64":
            return "f64"
        if ret == "str":
            self.emit("mov", self.strret2, "RET2")
            self.emit("mov", "R0", "RET")
            return "str"
        self.emit("mov", "R0", "RET")
        return ret

    def gen_stmt(self, n, top=False):
        tag = n[0]
        if tag == "let":
            ty = self.expr_type(n[3])
            idx = self.declare(n[1], ty)
            self.gen_expr(n[3])
            if ty == "f64":
                self.stfslot("F0", idx)
            elif ty == "str":
                self.stslot("R0", idx)
                self.stslot(self.strret2, idx + 1)
            else:
                self.stslot("R0", idx)
            return
        if tag == "assign":
            v = self.lookup(n[1][1])
            if v is None:
                raise NativeError("undefined variable `%s`" % n[1][1])
            idx, ty, isg = v
            self.gen_expr(n[2])
            if isg:
                self.emit("addr", "R8", "mnd_g")
                if ty == "f64":
                    self.emit("fstore", "F0", "R8", idx)
                elif ty == "str":
                    self.emit("store", "R0", "R8", idx)
                    self.emit("store", self.strret2, "R8", idx + 8)
                else:
                    self.emit("store", "R0", "R8", idx)
                return
            if ty == "f64":
                self.stfslot("F0", idx)
            elif ty == "str":
                self.stslot("R0", idx)
                self.stslot(self.strret2, idx + 1)
            else:
                self.stslot("R0", idx)
            return
        if tag == "expr":
            self.gen_expr(n[1])
            return
        if tag == "return":
            if n[1] is not None:
                self.gen_expr(n[1])
            else:
                self.emit("set", "R0", 0)
            self.emit("jmp", self.epilogue)
            return
        if tag == "if":
            end = self.uniq() + "end"
            first = True
            for cond, body in n[1]:
                nxt = self.uniq() + "nx"
                self.gen_expr(cond)
                self.emit("cji", "R0", 0, "e", nxt)
                self.newscope()
                for s in body:
                    self.gen_stmt(s)
                self.endscope()
                self.emit("jmp", end)
                self.L(nxt)
            if n[2] is not None:
                self.newscope()
                for s in n[2]:
                    self.gen_stmt(s)
                self.endscope()
            self.L(end)
            return
        if tag == "while":
            top_l = self.uniq() + "w"
            body_l = self.uniq() + "b"
            end_l = self.uniq() + "e"
            self.L(top_l)
            self.gen_expr(n[1])
            self.emit("cji", "R0", 0, "e", end_l)
            self.loopstack.append((end_l, top_l))
            self.newscope()
            for s in n[2]:
                self.gen_stmt(s)
            self.endscope()
            self.loopstack.pop()
            self.emit("jmp", top_l)
            self.L(end_l)
            return
        if tag == "for":
            self.gen_for(n)
            return
        if tag == "break":
            self.emit("jmp", self.loopstack[-1][0])
            return
        if tag == "continue":
            self.emit("jmp", self.loopstack[-1][1])
            return
        raise NativeError("native backend: unsupported statement `%s`" % tag)

    def gen_for(self, n):
        name, it, body = n[1], n[2], n[3]
        if it[0] == "call" and it[1][0] == "var" and it[1][1] == "range":
            args = it[2]
            self.newscope()
            idx = self.declare(name, "int")

            sb = self.slot
            self.slot += 3
            self.maxslot = max(self.maxslot, self.slot)
            if len(args) == 1:
                self.gen_expr(args[0])
                self.stslot("R0", sb + 1)
                self.emit("set", "R0", 0)
                self.stslot("R0", sb)
                self.emit("set", "R0", 1)
                self.stslot("R0", sb + 2)
            elif len(args) == 2:
                self.gen_expr(args[0])
                self.stslot("R0", sb)
                self.gen_expr(args[1])
                self.stslot("R0", sb + 1)
                self.emit("set", "R0", 1)
                self.stslot("R0", sb + 2)
            else:
                self.gen_expr(args[0])
                self.stslot("R0", sb)
                self.gen_expr(args[1])
                self.stslot("R0", sb + 1)
                self.gen_expr(args[2])
                self.stslot("R0", sb + 2)
                z = self.uniq() + "z"
                self.ldslot("R0", sb + 2)
                self.emit("cji", "R0", 0, "ne", z)
                msg, mlen = self.slit("range() arg 3 must not be zero")
                self.emit("addr", "A0", msg)
                self.emit("set", "A1", mlen)
                self.emit("jmp", "rt_panic")
                self.L(z)
            self.emit("set", "R0", 0)

            self.ldslot("R0", sb)
            self.stslot("R0", idx)
            cond_l = self.uniq() + "fc"
            body_l = self.uniq() + "fb"
            end_l = self.uniq() + "fe"
            self.L(cond_l)
            self.ldslot("R0", idx)
            self.ldslot("R1", sb + 1)
            self.ldslot("R2", sb + 2)
            sld = self.uniq() + "s"
            self.emit("cji", "R2", 0, "le", sld)
            self.emit("cj", "R0", "R1", "ge", end_l)
            self.emit("jmp", body_l)
            self.L(sld)
            self.emit("cj", "R0", "R1", "le", end_l)
            self.L(body_l)
            self.loopstack.append((end_l, cond_l))
            for s in body:
                self.gen_stmt(s)
            self.loopstack.pop()
            self.ldslot("R0", idx)
            self.ldslot("R1", sb + 2)
            self.emit("add", "R0", "R0", "R1")
            self.stslot("R0", idx)
            self.emit("jmp", cond_l)
            self.L(end_l)
            self.endscope()
            return

        ty = self.expr_type(it)
        if ty != "str":
            raise NativeError("native backend: `for` over `str` only in "
                              "v0.6 (use range() for numbers)")
        self.gen_expr(it)
        sb = self.slot
        self.slot += 3
        self.maxslot = max(self.maxslot, self.slot)
        self.stslot("R0", sb)
        self.stslot(self.strret2, sb + 1)
        self.emit("set", "R0", 0)
        self.stslot("R0", sb + 2)
        self.newscope()
        idx = self.declare(name, "str")
        cond_l = self.uniq() + "sc"
        body_l = self.uniq() + "sb"
        end_l = self.uniq() + "se"
        self.L(cond_l)
        self.ldslot("R0", sb + 2)
        self.ldslot("R1", sb + 1)
        self.emit("cj", "R0", "R1", "ge", end_l)
        self.L(body_l)
        self.ldslot("A0", sb)
        self.ldslot("A1", sb + 1)
        self.ldslot("A2", sb + 2)
        self.emit("call", "rt_str_index")
        self.stslot("RET", idx)
        self.stslot("RET2", idx + 1)
        self.loopstack.append((end_l, cond_l))
        for s in body:
            self.gen_stmt(s)
        self.loopstack.pop()
        self.ldslot("R0", sb + 2)
        self.emit("add", "R0", "R0", 1)
        self.stslot("R0", sb + 2)
        self.emit("jmp", cond_l)
        self.L(end_l)
        self.endscope()

    def gen_fn(self, n):
        name, params, body = n[1], n[2], n[4]
        ptypes = self.fns[name][0]

        saved_ops = self.ops
        self.ops = []
        self.slot = 0
        self.maxslot = 0
        self.spill = 0
        self.maxspill = 0
        self.newscope()
        ii = 0
        fi = 0
        pro = []
        for p, pt in zip(params, ptypes):
            idx = self.declare(p[0], pt)
            if pt == "f64":
                pro.append(("f", idx, fi))
                fi += 1
            elif pt == "str":
                pro.append(("s", idx, ii))
                ii += 2
            else:
                pro.append(("i", idx, ii))
                ii += 1
        self.epilogue = self.uniq() + "ep"
        for s in body:
            self.gen_stmt(s)
        self.emit("set", "R0", 0)
        self.L(self.epilogue)
        self.endscope()
        body_ops = self.ops
        fbytes = self.frame_bytes()

        self.ops = saved_ops
        self.L("fn_" + name)
        self.emit_prologue(fbytes)
        for kind, idx, k in pro:
            if kind == "f":
                self.emit("fstore", self.argf[k], "FREG", 8 * idx)
            elif kind == "s":
                self.stslot(self.argi[k], idx)
                self.stslot(self.argi[k + 1], idx + 1)
            else:
                self.stslot(self.argi[k], idx)
        for op in body_ops:
            self.ops.append(op)
        self.emit_epilogue(fbytes)

    def frame_bytes(self):
        frame_slots = self.maxslot + self.maxspill + 2
        return (frame_slots * 8 + 15) // 16 * 16

    def emit_prologue(self, fbytes):
        if self.arch == "x86-64":
            self.emit("push_rbp")
            self.emit("subsp", fbytes)
            self.emit("mov", "FREG", "SP")
        else:
            self.emit("subsp", fbytes + 16)
            self.emit("mov", "FREG", "SP")
            self.emit("store_lr", fbytes)

    def emit_epilogue(self, fbytes):
        if self.arch == "x86-64":
            self.emit("mov", "SP", "FREG")
            self.emit("addsp", fbytes)
            self.emit("pop_rbp")
            self.emit("ret")
        else:
            self.emit("load_lr", fbytes)
            self.emit("mov", "SP", "FREG")
            self.emit("addsp", fbytes + 16)
            self.emit("ret")

    def program(self, ast):
        items = ast[1]

        for it in items:
            if it[0] == "def":
                pts = [type_of_ann(p[1], 0) for p in it[2]]
                self.fns[it[1]] = (pts, type_of_ann(it[3], 0))

        saved_scopes = self.scopes
        self.scopes = [{}]
        try:
            for it in items:
                self.gate(it)
        finally:
            self.scopes = saved_scopes

        goff = 0
        for it in items:
            if it[0] == "let":
                ty = self.expr_type(it[3])
                n = 2 if ty == "str" else 1
                self.globals[it[1]] = (goff, ty, True)
                goff += n * 8
        self.gsize = goff

        self.L("_start")
        self.emit("call", "rt_init")
        self.emit("call", "mandela_main")
        self.emit("set", "A0", 0)
        self.emit("call", "rt_exit")

        for it in items:
            if it[0] == "def":
                self.gen_fn(it)

        saved_ops = self.ops
        self.ops = []
        self.slot = 0
        self.maxslot = 0
        self.spill = 0
        self.maxspill = 0
        self.newscope()
        for it in items:
            if it[0] in ("def", "struct"):
                continue
            if it[0] == "let":
                ty = self.expr_type(it[3])
                goff = self.globals[it[1]][0]
                self.gen_expr(it[3])
                self.emit("addr", "R8", "mnd_g")
                if ty == "f64":
                    self.emit("fstore", "F0", "R8", goff)
                elif ty == "str":
                    self.emit("store", "R0", "R8", goff)
                    self.emit("store", self.strret2, "R8", goff + 8)
                else:
                    self.emit("store", "R0", "R8", goff)
                continue
            self.gen_stmt(it)
        self.emit("set", "R0", 0)
        self.endscope()
        main_ops = self.ops
        fbytes = self.frame_bytes()
        self.ops = saved_ops
        self.L("mandela_main")
        self.emit_prologue(fbytes)
        for op in main_ops:
            self.ops.append(op)
        self.emit_epilogue(fbytes)

def main():
    argv = sys.argv[1:]
    src = None
    out = None
    arch = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        elif a == "--arch" and i + 1 < len(argv):
            arch = argv[i + 1]
            i += 2
        elif a.startswith("--arch="):
            arch = a[7:]
            i += 1
        elif a in ("-h", "--help"):
            usage()
        elif src is None:
            src = a
            i += 1
        else:
            sys.stderr.write("error: unexpected argument '%s'\n" % a)
            usage()
    if src is None:
        usage()
    if arch is None:
        m = os.uname().machine
        arch = {"x86_64": "x86-64", "aarch64": "arm64"}.get(m)
        if arch is None:
            sys.stderr.write("error: cannot detect architecture; pass "
                             "--arch x86-64|arm64\n")
            sys.exit(2)
    if arch not in ("x86-64", "arm64"):
        sys.stderr.write("error: unknown architecture '%s' (expected "
                         "x86-64 or arm64)\n" % arch)
        sys.exit(2)
    if out is None:
        out = src[:-4] if src.endswith(".mnd") else src
    try:
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot open file '%s'\n" % src)
        sys.exit(1)
    try:
        toks = mandela.tokenize(text)
        ast = mandela.Parser(toks).parse()
    except mandela.CompileError as e:
        sys.stderr.write("error: %s\n  --> %s:%d:%d\n"
                         % (e.msg, src, e.line, e.col))
        sys.exit(1)
    lw = Lowering(arch)
    try:
        lw.program(ast)
    except NativeError as e:
        sys.stderr.write("error: %s\n" % e.msg)
        if e.line:
            sys.stderr.write("  --> %s:%d:0\n" % (src, e.line))
        sys.exit(1)

    rt_ops, rt_ro = mc_rt.gen_runtime()
    ops = rt_ops + lw.ops
    ro = dict(rt_ro)
    ro.update(lw.ro)

    if arch == "x86-64":
        a = mc_x64.Asm()
        for op in ops:
            mc_isa.emit_x64(a, op)
        machine = mc_elf.EM_X86_64
    else:
        a = mc_a64.Asm()
        for op in ops:
            mc_isa.emit_a64(a, op)
        machine = mc_elf.EM_AARCH64

    while len(a.buf) % 8:
        a.byte(0)
    for lbl in sorted(ro):
        a.labels[lbl] = len(a.buf)
        a.buf.extend(ro[lbl])

    if "_start" not in a.labels:
        raise SystemExit("internal: no _start")

    text_base = mc_elf.BASE + 64 + 2 * 56

    rw_vaddr = mc_elf.BASE + ((len(a.buf) + 64 + 2 * 56 + 4095) // 4096) * 4096

    bss_abs = {"rt_data": rw_vaddr, "mnd_g": rw_vaddr + 80}
    bss_size = 80 + lw.gsize
    a.bss_abs = bss_abs
    a.text_base = text_base
    code = resolve_with_bss(a, arch)
    elf = mc_elf.build_elf(code, bss_size, a.labels["_start"], machine)
    with open(out, "wb") as fh:
        fh.write(elf)
    os.chmod(out, 0o755)

def resolve_with_bss(a, arch):
    buf = a.buf
    for pos, label, kind in a.fixups:
        if label in a.bss_abs:
            target = a.bss_abs[label]
        else:
            if label not in a.labels:
                raise SystemExit("internal: unbound label %s" % label)
            target = a.text_base + a.labels[label]
        if kind == "rel32":
            rel = target - (a.text_base + pos + 4)
            for i in range(4):
                buf[pos + i] = (rel >> (8 * i)) & 0xFF
        elif kind == "abs64":
            for i in range(8):
                buf[pos + i] = (target >> (8 * i)) & 0xFF
        elif kind in ("b", "bcond", "adr"):
            rel = target - (a.text_base + pos)
            insn = int.from_bytes(bytes(buf[pos:pos + 4]), "little")
            if kind == "b":
                assert rel % 4 == 0 and -(1 << 27) <= rel < (1 << 27)
                insn |= ((rel >> 2) & 0x3FFFFFF)
            elif kind == "bcond":
                assert rel % 4 == 0 and -1048576 <= rel < 1048576
                insn |= ((rel >> 2) & 0x7FFFF) << 5
            else:
                assert rel % 4 == 0 and -(1 << 20) <= rel < (1 << 20)
                insn |= ((rel >> 2) & 0x7FFFF) << 5
            buf[pos:pos + 4] = insn.to_bytes(4, "little")
        else:
            raise SystemExit("internal: bad fixup %s" % kind)
    return bytes(buf)

def usage():
    sys.stderr.write(
        "usage: mandela build --backend native <file.mnd> [-o out] "
        "[--arch x86-64|arm64]\n")
    sys.exit(2)

if __name__ == "__main__":
    main()
