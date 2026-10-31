#!/usr/bin/env python3
"""Mandela bootstrap engine: lexer, parser and tree-walking interpreter.

Exists only to run the self-hosted compiler (src/*.mnd) and user programs
until the toolchain emits native code. Not part of the language itself.
"""

import math
import os
import sys
import threading


class CompileError(Exception):
    def __init__(self, msg, line, col):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col
        self.file = CUR_FILE


class Panic(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


class Fatal(Exception):
    def __init__(self, msg, code):
        super().__init__(msg if msg is not None else "")
        self.msg = msg
        self.code = code


class ReturnEx(Exception):
    def __init__(self, value):
        self.value = value


class BreakEx(Exception):
    pass


class ContinueEx(Exception):
    pass


class StructDef:
    __slots__ = ("name", "fields", "methods")

    def __init__(self, name, fields, methods):
        self.name = name
        self.fields = fields
        self.methods = methods


class MStruct:
    __slots__ = ("sd", "vals")

    def __init__(self, sd, vals):
        self.sd = sd
        self.vals = vals


class MndDict:
    __slots__ = ("d",)

    def __init__(self):
        self.d = {}


class MndArena:
    __slots__ = ("freed", "total")

    def __init__(self):
        self.freed = False
        self.total = 0


class MndPtr:
    __slots__ = ("slots", "ln", "off", "owner")

    def __init__(self, slots, ln, off, owner):
        self.slots = slots
        self.ln = ln
        self.off = off
        self.owner = owner


class MModule:
    __slots__ = ("name", "path", "env", "members")

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.env = None
        self.members = {}


KEYWORDS = {
    "def", "return", "if", "elif", "else", "while", "for", "in", "let",
    "mut", "true", "false", "nil", "and", "or", "not", "break", "continue",
    "struct", "unsafe", "import", "from", "as",
}

OPS2 = ("->", "==", "!=", "<=", ">=")
OPS1 = set("+-*/%=<>()[],:.{}")


def strip_indent(raw):
    i = 0
    wchars = 0
    width = 0
    while i < len(raw):
        c = raw[i]
        if c == " ":
            wchars += 1
            width += 1
            i += 1
        elif c == "\t":
            wchars += 1
            width += 4
            i += 1
        else:
            break
    return raw[i:], wchars, width


def tokenize(src):
    toks = []
    lines = src.split("\n")
    indents = [0]
    depth = 0
    for idx, raw in enumerate(lines):
        ln = idx + 1
        content, wchars, width = strip_indent(raw)
        if content == "" or content.startswith("#"):
            continue
        if depth == 0:
            if width > indents[-1]:
                indents.append(width)
                toks.append(("indent", None, ln, 1))
            else:
                while indents[-1] > width:
                    indents.pop()
                    toks.append(("dedent", None, ln, 1))
                if indents[-1] != width:
                    raise CompileError(
                        "unindent does not match any outer indentation level", ln, 1)
        j = 0
        n = len(content)
        while j < n:
            c = content[j]
            if c in " \t":
                j += 1
                continue
            if c == "#":
                break
            col = wchars + j + 1
            if c.isdigit():
                k = j
                while k < n and content[k].isdigit():
                    k += 1
                if k + 1 < n and content[k] == "." and content[k + 1].isdigit():
                    k += 1
                    while k < n and content[k].isdigit():
                        k += 1
                    if k < n and content[k] in "eE":
                        m = k + 1
                        if m < n and content[m] in "+-":
                            m += 1
                        if m < n and content[m].isdigit():
                            k = m
                            while k < n and content[k].isdigit():
                                k += 1
                    toks.append(("fnum", float(content[j:k]), ln, col))
                    j = k
                    continue
                v = 0
                while j < k:
                    v = v * 10 + ord(content[j]) - 48
                    j += 1
                toks.append(("int", v, ln, col))
                continue
            if c.isalpha() or c == "_":
                k = j
                while k < n and (content[k].isalnum() or content[k] == "_"):
                    k += 1
                word = content[j:k]
                toks.append(("kw" if word in KEYWORDS else "id", word, ln, col))
                j = k
                continue
            if c == '"':
                k = j + 1
                buf = []
                while True:
                    if k >= n:
                        raise CompileError("unterminated string literal", ln, col)
                    ch = content[k]
                    if ch == '"':
                        k += 1
                        break
                    if ch == "\\":
                        if k + 1 >= n:
                            raise CompileError("unterminated string literal", ln, col)
                        esc = content[k + 1]
                        if esc == "n":
                            buf.append("\n")
                        elif esc == "t":
                            buf.append("\t")
                        elif esc == "r":
                            buf.append("\r")
                        elif esc == "0":
                            buf.append("\0")
                        elif esc == '"':
                            buf.append('"')
                        elif esc == "\\":
                            buf.append("\\")
                        else:
                            raise CompileError(
                                "invalid escape sequence '\\%s' in string literal" % esc,
                                ln, wchars + k + 1)
                        k += 2
                        continue
                    buf.append(ch)
                    k += 1
                toks.append(("str", "".join(buf), ln, col))
                j = k
                continue
            two = content[j:j + 2]
            if two in OPS2:
                toks.append(("op", two, ln, col))
                j += 2
                continue
            if c in OPS1:
                toks.append(("op", c, ln, col))
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth > 0:
                        depth -= 1
                j += 1
                continue
            raise CompileError("unexpected character '%s'" % c, ln, col)
        if depth == 0 and toks and toks[-1][0] not in ("nl", "indent", "dedent"):
            toks.append(("nl", None, ln, col))
    if depth > 0:
        raise CompileError("unclosed '(' or '[' at end of file", len(lines), 1)
    if toks and toks[-1][0] != "nl":
        toks.append(("nl", None, len(lines), 1))
    while indents[-1] > 0:
        indents.pop()
        toks.append(("dedent", None, len(lines), 1))
    toks.append(("eof", None, len(lines) + 1, 1))
    return toks


def describe(t):
    tt, v = t[0], t[1]
    if tt in ("op", "kw"):
        return "`%s`" % v
    if tt == "id":
        return "identifier `%s`" % v
    if tt == "int":
        return "integer `%d`" % v
    if tt == "fnum":
        return "floating-point `%s`" % v
    if tt == "str":
        return "string literal"
    if tt == "nl":
        return "end of line"
    if tt == "indent":
        return "an indented block"
    if tt == "dedent":
        return "end of block"
    return "end of file"


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, ttype, val=None):
        t = self.toks[self.i]
        return t[0] == ttype and (val is None or t[1] == val)

    def eat(self, ttype, val=None):
        if self.at(ttype, val):
            return self.next()
        return None

    def expect(self, ttype, val, what):
        if self.at(ttype, val):
            return self.next()
        t = self.peek()
        raise CompileError("expected %s, found %s" % (what, describe(t)), t[2], t[3])

    def parse(self):
        items = []
        while True:
            while self.at("nl"):
                self.next()
            if self.at("eof"):
                break
            if self.at("kw", "def"):
                items.append(self.parse_def())
            elif self.at("kw", "struct"):
                items.append(self.parse_struct())
            elif self.at("kw", "import"):
                items.append(self.parse_import())
            elif self.at("kw", "from"):
                items.append(self.parse_from())
            else:
                items.append(self.parse_stmt())
        return ["program", items, 1, 1]

    def parse_dotted(self):
        t = self.expect("id", None, "a module path")
        parts = [t[1]]
        while self.eat("op", "."):
            t = self.expect("id", None, "a module path component")
            parts.append(t[1])
        return ".".join(parts)

    def parse_import(self):
        t = self.next()
        dotted = self.parse_dotted()
        alias = ""
        if self.eat("kw", "as"):
            alias = self.expect("id", None, "an alias")[1]
        return ["import", dotted, alias, t[2], t[3]]

    def parse_from(self):
        t = self.next()
        dotted = self.parse_dotted()
        self.expect("kw", "import", "`import`")
        names = []
        while True:
            n = self.expect("id", None, "an imported name")
            alias = ""
            if self.eat("kw", "as"):
                alias = self.expect("id", None, "an alias")[1]
            names.append([n[1], alias])
            if not self.eat("op", ","):
                break
        return ["from", dotted, names, t[2], t[3]]

    def parse_struct(self):
        t = self.next()
        name = self.expect("id", None, "struct name")
        self.expect("op", ":", "`:`")
        self.expect("nl", None, "end of line")
        if not self.at("indent"):
            tok = self.peek()
            raise CompileError("expected an indented struct body, found %s"
                               % describe(tok), tok[2], tok[3])
        self.next()
        fields = []
        methods = []
        while not self.at("dedent") and not self.at("eof"):
            while self.at("nl"):
                self.next()
            if self.at("dedent") or self.at("eof"):
                break
            if self.at("kw", "def"):
                methods.append(self.parse_def())
            else:
                fn = self.expect("id", None, "a field name")
                self.expect("op", ":", "`:`")
                ft = self.parse_type()
                fields.append([fn[1], ft, fn[2], fn[3]])
                if not (self.at("nl") or self.at("dedent") or self.at("eof")):
                    tok = self.peek()
                    raise CompileError("expected end of line, found %s"
                                       % describe(tok), tok[2], tok[3])
        if not self.at("dedent"):
            tok = self.peek()
            raise CompileError("unexpected end of file: unclosed struct body",
                               tok[2], tok[3])
        self.next()
        return ["struct", name[1], fields, methods, t[2], t[3]]

    def parse_def(self):
        self.next()
        name = self.expect("id", None, "function name")
        self.expect("op", "(", "`(`")
        params = []
        if not self.at("op", ")"):
            while True:
                if self.at("kw", "mut"):
                    self.next()
                pn = self.expect("id", None, "parameter name")
                pt = ""
                if pn[1] != "self" or self.at("op", ":"):
                    self.expect("op", ":", "`:`")
                    pt = self.parse_type()
                params.append([pn[1], pt, pn[2], pn[3]])
                if not self.eat("op", ","):
                    break
        self.expect("op", ")", "`)`")
        ret = ""
        if self.eat("op", "->"):
            ret = self.parse_type()
        body = self.parse_block()
        return ["def", name[1], params, ret, body, name[2], name[3]]

    def parse_type(self):
        t = self.expect("id", None, "a type name")
        if t[1] == "list":
            self.expect("op", "[", "`[` after `list`")
            inner = self.parse_type()
            self.expect("op", "]", "`]`")
            return "list[%s]" % inner
        if t[1] == "dict":
            self.expect("op", "[", "`[` after `dict`")
            k = self.parse_type()
            self.expect("op", ",", "`,`")
            v = self.parse_type()
            self.expect("op", "]", "`]`")
            return "dict[%s,%s]" % (k, v)
        if t[1] == "ptr":
            self.expect("op", "[", "`[` after `ptr`")
            inner = self.parse_type()
            self.expect("op", "]", "`]`")
            return "ptr[%s]" % inner
        return t[1]

    def parse_block(self):
        self.expect("op", ":", "`:`")
        if self.eat("nl"):
            if not self.at("indent"):
                t = self.peek()
                raise CompileError("expected an indented block, found %s" % describe(t),
                                   t[2], t[3])
            self.next()
            body = []
            while not self.at("dedent") and not self.at("eof"):
                while self.at("nl"):
                    self.next()
                if self.at("dedent") or self.at("eof"):
                    break
                body.append(self.parse_stmt())
            if not self.at("dedent"):
                t = self.peek()
                raise CompileError("unexpected end of file: unclosed block", t[2], t[3])
            self.next()
            return body
        return [self.parse_simple()]

    def parse_stmt(self):
        if self.at("kw", "if"):
            return self.parse_if()
        if self.at("kw", "while"):
            return self.parse_while()
        if self.at("kw", "for"):
            return self.parse_for()
        if self.at("kw", "unsafe"):
            return self.parse_unsafe()
        if self.at("kw", "import") or self.at("kw", "from"):
            t = self.peek()
            raise CompileError("`import` is only allowed at the top level of a module",
                               t[2], t[3])
        return self.parse_simple()

    def parse_unsafe(self):
        t = self.next()
        body = self.parse_block()
        return ["unsafe", body, t[2], t[3]]

    def parse_simple(self):
        if self.at("kw", "let"):
            node = self.parse_let()
        elif self.at("kw", "return"):
            node = self.parse_return()
        elif self.at("kw", "break"):
            t = self.next()
            node = ["break", t[2], t[3]]
        elif self.at("kw", "continue"):
            t = self.next()
            node = ["continue", t[2], t[3]]
        else:
            node = self.parse_assign_or_expr()
        if self.at("nl"):
            self.next()
        elif not (self.at("eof") or self.at("dedent")):
            t = self.peek()
            raise CompileError("expected end of line, found %s" % describe(t), t[2], t[3])
        return node

    def parse_let(self):
        self.next()
        mut = False
        if self.at("kw", "mut"):
            self.next()
            mut = True
        name = self.expect("id", None, "a variable name")
        ty = ""
        if self.eat("op", ":"):
            ty = self.parse_type()
        self.expect("op", "=", "`=`")
        val = self.parse_expr()
        return ["let", name[1], ty, val, mut, name[2], name[3]]

    def parse_return(self):
        t = self.next()
        val = None
        if not (self.at("nl") or self.at("eof") or self.at("dedent")):
            val = self.parse_expr()
        return ["return", val, t[2], t[3]]

    def parse_assign_or_expr(self):
        e = self.parse_expr()
        if self.at("op", "="):
            op = self.next()
            if e[0] not in ("var", "index", "field"):
                raise CompileError("invalid assignment target", e[-2], e[-1])
            rhs = self.parse_expr()
            return ["assign", e, rhs, op[2], op[3]]
        return ["expr", e, e[-2], e[-1]]

    def parse_if(self):
        t = self.next()
        branches = []
        cond = self.parse_expr()
        body = self.parse_block()
        branches.append([cond, body])
        els = None
        while True:
            while self.at("nl"):
                self.next()
            if self.at("kw", "elif"):
                self.next()
                c2 = self.parse_expr()
                b2 = self.parse_block()
                branches.append([c2, b2])
            elif self.at("kw", "else"):
                self.next()
                els = self.parse_block()
                break
            else:
                break
        return ["if", branches, els, t[2], t[3]]

    def parse_while(self):
        t = self.next()
        cond = self.parse_expr()
        body = self.parse_block()
        return ["while", cond, body, t[2], t[3]]

    def parse_for(self):
        t = self.next()
        name = self.expect("id", None, "a loop variable name")
        self.expect("kw", "in", "`in`")
        it = self.parse_expr()
        body = self.parse_block()
        return ["for", name[1], it, body, t[2], t[3]]

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        e = self.parse_and()
        while self.at("kw", "or"):
            op = self.next()
            e = ["bin", "or", e, self.parse_and(), op[2], op[3]]
        return e

    def parse_and(self):
        e = self.parse_not()
        while self.at("kw", "and"):
            op = self.next()
            e = ["bin", "and", e, self.parse_not(), op[2], op[3]]
        return e

    def parse_not(self):
        if self.at("kw", "not"):
            op = self.next()
            return ["un", "not", self.parse_not(), op[2], op[3]]
        return self.parse_cmp()

    def parse_cmp(self):
        e = self.parse_add()
        t = self.peek()
        if t[0] == "op" and t[1] in ("==", "!=", "<", "<=", ">", ">="):
            self.next()
            return ["bin", t[1], e, self.parse_add(), t[2], t[3]]
        return e

    def parse_add(self):
        e = self.parse_mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.next()
            e = ["bin", op[1], e, self.parse_mul(), op[2], op[3]]
        return e

    def parse_mul(self):
        e = self.parse_unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.next()
            e = ["bin", op[1], e, self.parse_unary(), op[2], op[3]]
        return e

    def parse_unary(self):
        if self.at("op", "-"):
            op = self.next()
            return ["un", "-", self.parse_unary(), op[2], op[3]]
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_atom()
        while True:
            if self.at("op", "("):
                op = self.next()
                args = []
                if not self.at("op", ")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.eat("op", ","):
                            break
                        if self.at("op", ")"):
                            break
                self.expect("op", ")", "`)`")
                e = ["call", e, args, op[2], op[3]]
            elif self.at("op", "["):
                op = self.next()
                idx = self.parse_expr()
                self.expect("op", "]", "`]`")
                e = ["index", e, idx, op[2], op[3]]
            elif self.at("op", "."):
                self.next()
                nm = self.expect("id", None, "a field or method name")
                e = ["field", e, nm[1], nm[2], nm[3]]
            else:
                break
        return e

    def parse_atom(self):
        t = self.peek()
        if t[0] == "int":
            self.next()
            return ["num", t[1], t[2], t[3]]
        if t[0] == "fnum":
            self.next()
            return ["fnum", t[1], t[2], t[3]]
        if t[0] == "str":
            self.next()
            return ["str", t[1], t[2], t[3]]
        if t[0] == "id":
            nxt = self.toks[self.i + 1]
            if nxt[0] == "op" and nxt[1] == "{":
                return self.parse_slit()
            self.next()
            return ["var", t[1], t[2], t[3]]
        if t[0] == "kw":
            if t[1] == "true":
                self.next()
                return ["bool", True, t[2], t[3]]
            if t[1] == "false":
                self.next()
                return ["bool", False, t[2], t[3]]
            if t[1] == "nil":
                self.next()
                return ["nil", t[2], t[3]]
        if t[0] == "op" and t[1] == "(":
            self.next()
            e = self.parse_expr()
            self.expect("op", ")", "`)`")
            return e
        if t[0] == "op" and t[1] == "{":
            return self.parse_dictlit()
        if t[0] == "op" and t[1] == "[":
            self.next()
            items = []
            if not self.at("op", "]"):
                while True:
                    items.append(self.parse_expr())
                    if not self.eat("op", ","):
                        break
                    if self.at("op", "]"):
                        break
            self.expect("op", "]", "`]`")
            return ["list", items, t[2], t[3]]
        raise CompileError("expected an expression, found %s" % describe(t), t[2], t[3])

    def parse_dictlit(self):
        t = self.next()
        pairs = []
        if not self.at("op", "}"):
            while True:
                k = self.parse_expr()
                self.expect("op", ":", "`:`")
                v = self.parse_expr()
                pairs.append([k, v, k[-2], k[-1]])
                if not self.eat("op", ","):
                    break
                if self.at("op", "}"):
                    break
        self.expect("op", "}", "`}`")
        return ["dict", pairs, t[2], t[3]]

    def parse_slit(self):
        name = self.next()
        self.next()
        fields = []
        if not self.at("op", "}"):
            while True:
                fn = self.expect("id", None, "a field name")
                self.expect("op", ":", "`:`")
                v = self.parse_expr()
                fields.append([fn[1], v, fn[2], fn[3]])
                if not self.eat("op", ","):
                    break
                if self.at("op", "}"):
                    break
        self.expect("op", "}", "`}`")
        return ["slit", name[1], fields, name[2], name[3]]


class Env:
    __slots__ = ("vars", "parent")

    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent


def type_name(v):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "f64"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, MndDict):
        return "dict"
    if isinstance(v, MStruct):
        return v.sd.name
    if isinstance(v, MndPtr):
        return "ptr"
    if isinstance(v, MndArena):
        return "arena"
    return type(v).__name__


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_f64(v):
    return isinstance(v, float)


def fdiv(l, r):
    """IEEE 754 division: 1.0/0.0 is inf, 0.0/0.0 is nan (Python raises)."""
    if r != 0.0:
        return l / r
    if l != l or l == 0.0:
        return float("nan")
    return math.copysign(math.inf, l) * math.copysign(1.0, r)


def fmod_(l, r):
    """C fmod() semantics: fmod(x, 0) and fmod(inf, y) are nan."""
    if l != l or r != r or math.isinf(l) or r == 0.0:
        return float("nan")
    if math.isinf(r):
        return l
    return math.fmod(l, r)


def c_round(v):
    """C round(): half away from zero (Python's round() is banker's)."""
    if v != v or math.isinf(v) or v == 0.0:
        return v
    f = math.floor(v)
    d = v - f
    if d > 0.5:
        return float(f + 1)
    if d < 0.5:
        return float(f)
    if v > 0:
        return float(f + 1)
    return float(f)


def parse_f64_str(s):
    """Strict decimal grammar: [+-]?(digits[.digits]?|.digits)([eE][+-]digits)?"""
    if s == "":
        raise Panic("cannot parse float from empty string")
    i, n = 0, len(s)
    if s[i] in "+-":
        i += 1
    dig = 0
    while i < n and "0" <= s[i] <= "9":
        i += 1
        dig += 1
    if i < n and s[i] == ".":
        i += 1
        while i < n and "0" <= s[i] <= "9":
            i += 1
            dig += 1
    if dig == 0:
        raise Panic("invalid float literal")
    if i < n and (s[i] == "e" or s[i] == "E"):
        i += 1
        if i < n and s[i] in "+-":
            i += 1
        edig = 0
        while i < n and "0" <= s[i] <= "9":
            i += 1
            edig += 1
        if edig == 0:
            raise Panic("invalid float literal")
    if i != n:
        raise Panic("invalid float literal")
    return float(s)


def wrap64(v):
    v &= (1 << 64) - 1
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def truthy(v):
    if not isinstance(v, bool):
        raise Panic("expected `bool`, found `%s`" % type_name(v))
    return v


def ml_eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)):
            return False
        return len(a) == len(b) and all(ml_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, MStruct) or isinstance(b, MStruct):
        if not (isinstance(a, MStruct) and isinstance(b, MStruct)):
            return False
        if a.sd.name != b.sd.name:
            return False
        return all(ml_eq(a.vals[f[0]], b.vals[f[0]]) for f in a.sd.fields)
    if isinstance(a, MndDict) or isinstance(b, MndDict):
        if not (isinstance(a, MndDict) and isinstance(b, MndDict)):
            return False
        if len(a.d) != len(b.d):
            return False
        for k in a.d:
            if k not in b.d or not ml_eq(a.d[k], b.d[k]):
                return False
        return True
    if isinstance(a, MndPtr) or isinstance(b, MndPtr):
        if not (isinstance(a, MndPtr) and isinstance(b, MndPtr)):
            return False
        return a.slots is b.slots and a.off == b.off
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, float) and isinstance(b, float) and a == b
    return a == b


def panic_index(n, i):
    raise Panic("index out of bounds: the len is %d but the index is %d" % (n, i))


def ptr_alive(p):
    if p.owner is not None and p.owner.freed:
        raise Panic("use after arena_free")


def require_list(v):
    if not isinstance(v, list):
        raise Panic("expected a `list`, found `%s`" % type_name(v))


def require_index(v):
    if not is_int(v):
        raise Panic("index must be `int`, found `%s`" % type_name(v))
    return v


def format_value(v, nested=False):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        if nested:
            s = (v.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\t", "\\t"))
            return '"%s"' % s
        return v
    if isinstance(v, list):
        return "[" + ", ".join(format_value(x, True) for x in v) + "]"
    if isinstance(v, MStruct):
        parts = ["%s: %s" % (f[0], format_value(v.vals[f[0]], True))
                 for f in v.sd.fields]
        return "%s { %s }" % (v.sd.name, ", ".join(parts))
    if isinstance(v, MndDict):
        parts = ["%s: %s" % (format_value(k, True), format_value(x, True))
                 for k, x in v.d.items()]
        return "{" + ", ".join(parts) + "}"
    if isinstance(v, MndPtr):
        return "<ptr>"
    if isinstance(v, MndArena):
        return "<arena>"
    return str(v)


def one_arg(args, name):
    if len(args) != 1:
        raise Panic("%s takes exactly 1 argument but %d were given" % (name, len(args)))
    return args[0]


def bi_print(args, ip):
    sys.stdout.write(format_value(one_arg(args, "print()")) + "\n")
    return None


def bi_eprint(args, ip):
    sys.stderr.write(format_value(one_arg(args, "eprint()")) + "\n")
    return None


def bi_len(args, ip):
    v = one_arg(args, "len()")
    if isinstance(v, str) or isinstance(v, list):
        return len(v)
    if isinstance(v, MndDict):
        return len(v.d)
    raise Panic("expected `str`, a list or a dict in len(), found `%s`" % type_name(v))


def bi_str(args, ip):
    return format_value(one_arg(args, "str()"))


def bi_int(args, ip):
    v = one_arg(args, "int()")
    if is_int(v):
        return v
    if isinstance(v, float):
        if v != v:
            raise Panic("cannot convert `nan` to `int`")
        if v >= 9223372036854775808.0 or v < -9223372036854775808.0:
            raise Panic("number too large to fit in target type")
        return math.trunc(v)
    if isinstance(v, str):
        s = v
        neg = False
        if s[:1] in ("+", "-"):
            neg = s[0] == "-"
            s = s[1:]
        if s == "":
            raise Panic("cannot parse integer from empty string")
        for c in s:
            if not ("0" <= c <= "9"):
                raise Panic("invalid digit found in string")
        n = int(s)
        if neg:
            n = -n
        if n < -(2 ** 63) or n > 2 ** 63 - 1:
            raise Panic("number too large to fit in target type")
        return n
    raise Panic("expected `str` or `int` in int(), found `%s`" % type_name(v))


def bi_chr(args, ip):
    v = one_arg(args, "chr()")
    if not is_int(v):
        raise Panic("expected `int` in chr(), found `%s`" % type_name(v))
    if v < 0 or v > 0x10FFFF or 0xD800 <= v <= 0xDFFF:
        raise Panic("invalid code point in chr(): %d" % v)
    return chr(v)


def bi_ord(args, ip):
    v = one_arg(args, "ord()")
    if not isinstance(v, str):
        raise Panic("expected `str` in ord(), found `%s`" % type_name(v))
    if len(v) != 1:
        raise Panic("expected a single-character string in ord()")
    return ord(v)


def bi_f64(args, ip):
    v = one_arg(args, "f64()")
    if is_f64(v):
        return v
    if is_int(v):
        return float(v)
    if isinstance(v, str):
        return parse_f64_str(v)
    raise Panic("expected `str`, `int` or `f64` in f64(), found `%s`" % type_name(v))


def bi_abs(args, ip):
    v = one_arg(args, "abs()")
    if is_int(v):
        return wrap64(abs(v))
    if is_f64(v):
        return abs(v)
    raise Panic("expected `int` or `f64` in abs(), found `%s`" % type_name(v))


def bi_sqrt(args, ip):
    v = one_arg(args, "sqrt()")
    if not is_f64(v):
        raise Panic("expected `f64` in sqrt(), found `%s`" % type_name(v))
    if v < 0.0:
        return float("nan")
    return math.sqrt(v)


def bi_floor(args, ip):
    v = one_arg(args, "floor()")
    if not is_f64(v):
        raise Panic("expected `f64` in floor(), found `%s`" % type_name(v))
    if v != v or math.isinf(v) or v == 0.0:
        return v
    return float(math.floor(v))


def bi_ceil(args, ip):
    v = one_arg(args, "ceil()")
    if not is_f64(v):
        raise Panic("expected `f64` in ceil(), found `%s`" % type_name(v))
    if v != v or math.isinf(v) or v == 0.0:
        return v
    return float(math.ceil(v))


def bi_round(args, ip):
    v = one_arg(args, "round()")
    if not is_f64(v):
        raise Panic("expected `f64` in round(), found `%s`" % type_name(v))
    return c_round(v)


def fpow(a, b):
    """math.pow() wrapped to C pow() semantics (inf/nan instead of raising)."""
    try:
        return math.pow(a, b)
    except OverflowError:
        neg = (math.copysign(1.0, a) < 0 and b == math.floor(b)
               and int(b) % 2 == 1)
        return math.copysign(math.inf, -1.0 if neg else 1.0)
    except ValueError:
        if a == 0.0 and b < 0.0:
            if (math.copysign(1.0, a) < 0 and b == math.floor(b)
                    and int(b) % 2 == 1):
                return float("-inf")
            return float("inf")
        return float("nan")
    except ZeroDivisionError:
        return float("inf")


def bi_pow(args, ip):
    if len(args) != 2:
        raise Panic("pow() takes exactly 2 arguments but %d were given" % len(args))
    a, b = args
    if not (is_f64(a) and is_f64(b)):
        raise Panic("pow() expects two `f64` arguments, found `%s` and `%s`"
                    % (type_name(a), type_name(b)))
    return fpow(a, b)


def bi_push(args, ip):
    if len(args) != 2:
        raise Panic("push() takes exactly 2 arguments but %d were given" % len(args))
    require_list(args[0])
    args[0].append(args[1])
    return args[0]


def bi_pop(args, ip):
    l = one_arg(args, "pop()")
    require_list(l)
    if len(l) == 0:
        raise Panic("pop from an empty list")
    return l.pop()


def bi_split(args, ip):
    if len(args) != 2:
        raise Panic("split() takes exactly 2 arguments but %d were given" % len(args))
    s, sep = args
    if not isinstance(s, str) or not isinstance(sep, str):
        raise Panic("split() expects two `str` arguments")
    if sep == "":
        raise Panic("split() separator must not be empty")
    return s.split(sep)


def bi_substr(args, ip):
    if len(args) != 3:
        raise Panic("substr() takes exactly 3 arguments but %d were given" % len(args))
    s, start, ln = args
    if not isinstance(s, str):
        raise Panic("substr() expects a `str`, found `%s`" % type_name(s))
    if not is_int(start) or not is_int(ln):
        raise Panic("substr() expects `int` offsets")
    if start < 0:
        start = 0
    if ln < 0:
        ln = 0
    if start > len(s):
        return ""
    return s[start:start + ln]


def bi_read_file(args, ip):
    p = one_arg(args, "read_file()")
    if not isinstance(p, str):
        raise Panic("read_file() expects a `str` path")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        raise Fatal("error: cannot read file '%s'" % p, 1)


def bi_file_exists(args, ip):
    p = one_arg(args, "file_exists()")
    if not isinstance(p, str):
        raise Panic("file_exists() expects a `str` path")
    return os.path.isfile(p)


def bi_write_file(args, ip):
    if len(args) != 2:
        raise Panic("write_file() takes exactly 2 arguments but %d were given" % len(args))
    p, data = args
    if not isinstance(p, str) or not isinstance(data, str):
        raise Panic("write_file() expects two `str` arguments")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError:
        raise Fatal("error: cannot write file '%s'" % p, 1)
    return None


def bi_range(args, ip):
    if len(args) < 1 or len(args) > 3:
        raise Panic("range() takes 1 to 3 arguments but %d were given" % len(args))
    for a in args:
        if not is_int(a):
            raise Panic("range() expects `int` arguments, found `%s`" % type_name(a))
    if len(args) == 3 and args[2] == 0:
        raise Panic("range() arg 3 must not be zero")
    if len(args) == 1:
        return range(args[0])
    if len(args) == 2:
        return range(args[0], args[1])
    return range(args[0], args[1], args[2])


def bi_fatal(args, ip):
    msg = one_arg(args, "fatal()")
    if not isinstance(msg, str):
        raise Panic("fatal() expects a `str` message")
    raise Fatal(msg, 1)


def bi_exit(args, ip):
    code = one_arg(args, "exit()")
    if not is_int(code):
        raise Panic("exit() expects an `int` code")
    raise Fatal(None, code)


def bi_alloc(args, ip):
    if len(args) != 2:
        raise Panic("alloc() takes exactly 2 arguments but %d were given" % len(args))
    n, fill = args
    if not is_int(n):
        raise Panic("alloc() expects an `int` length, found `%s`" % type_name(n))
    if n < 0:
        raise Panic("ptr length must not be negative")
    return MndPtr([fill] * n, n, 0, None)


def bi_arena_new(args, ip):
    if args:
        raise Panic("arena_new() takes exactly 0 arguments but %d were given" % len(args))
    return MndArena()


def bi_arena_alloc(args, ip):
    if len(args) != 3:
        raise Panic("arena_alloc() takes exactly 3 arguments but %d were given" % len(args))
    a, n, fill = args
    if not isinstance(a, MndArena):
        raise Panic("arena_alloc() expects an `Arena`, found `%s`" % type_name(a))
    if not is_int(n):
        raise Panic("arena_alloc() expects an `int` length, found `%s`" % type_name(n))
    if a.freed:
        raise Panic("use of a freed arena")
    if n < 0:
        raise Panic("ptr length must not be negative")
    a.total += n * 16
    return MndPtr([fill] * n, n, 0, a)


def bi_arena_free(args, ip):
    a = one_arg(args, "arena_free()")
    if not isinstance(a, MndArena):
        raise Panic("arena_free() expects an `Arena`, found `%s`" % type_name(a))
    if a.freed:
        raise Panic("double free of an arena")
    a.freed = True
    return a.total


def bi_ptr_len(args, ip):
    v = one_arg(args, "ptr_len()")
    if isinstance(v, MndPtr):
        ptr_alive(v)
        return v.ln
    if v is None:
        raise Panic("null pointer access")
    raise Panic("expected a `ptr` in `ptr_len`, found `%s`" % type_name(v))


def bi_is_null(args, ip):
    v = one_arg(args, "is_null()")
    if v is None:
        return True
    if isinstance(v, MndPtr):
        return False
    raise Panic("expected a `ptr` in `is_null`, found `%s`" % type_name(v))


def m_arity(args, n, name):
    if len(args) != n:
        raise Panic("%s() takes exactly %d argument%s but %d were given"
                    % (name, n, "" if n == 1 else "s", len(args)))


def require_key(v, name):
    if not is_int(v) and not isinstance(v, str):
        raise Panic("dict key must be `str` or `int`, found `%s`" % type_name(v))
    return v


def sm_upper(s, args):
    m_arity(args, 0, "upper")
    return s.upper()


def sm_lower(s, args):
    m_arity(args, 0, "lower")
    return s.lower()


def sm_trim(s, args):
    m_arity(args, 0, "trim")
    return s.strip()


def sm_split(s, args):
    return bi_split([s] + list(args), None)


def sm_contains(s, args):
    m_arity(args, 1, "contains")
    if not isinstance(args[0], str):
        raise Panic("contains() expects a `str` argument")
    return args[0] in s


def sm_starts_with(s, args):
    m_arity(args, 1, "starts_with")
    if not isinstance(args[0], str):
        raise Panic("starts_with() expects a `str` argument")
    return s.startswith(args[0])


def sm_ends_with(s, args):
    m_arity(args, 1, "ends_with")
    if not isinstance(args[0], str):
        raise Panic("ends_with() expects a `str` argument")
    return s.endswith(args[0])


def sm_replace(s, args):
    m_arity(args, 2, "replace")
    old, new = args
    if not isinstance(old, str) or not isinstance(new, str):
        raise Panic("replace() expects two `str` arguments")
    if old == "":
        raise Panic("replace() old must not be empty")
    return s.replace(old, new)


def sm_find(s, args):
    m_arity(args, 1, "find")
    if not isinstance(args[0], str):
        raise Panic("find() expects a `str` argument")
    return s.find(args[0])


def sm_substr(s, args):
    return bi_substr([s] + list(args), None)


def sm_repeat(s, args):
    m_arity(args, 1, "repeat")
    if not is_int(args[0]):
        raise Panic("repeat() expects an `int` argument, found `%s`"
                    % type_name(args[0]))
    return s * args[0] if args[0] > 0 else ""


def sm_chars(s, args):
    m_arity(args, 0, "chars")
    return list(s)


STR_METHODS = {
    "upper": sm_upper, "lower": sm_lower, "trim": sm_trim, "split": sm_split,
    "contains": sm_contains, "starts_with": sm_starts_with,
    "ends_with": sm_ends_with, "replace": sm_replace, "find": sm_find,
    "substr": sm_substr, "repeat": sm_repeat, "chars": sm_chars,
}


def dm_keys(d, args):
    m_arity(args, 0, "keys")
    return list(d.d.keys())


def dm_values(d, args):
    m_arity(args, 0, "values")
    return list(d.d.values())


def dm_has(d, args):
    m_arity(args, 1, "has")
    return require_key(args[0], "has") in d.d


def dm_remove(d, args):
    m_arity(args, 1, "remove")
    return d.d.pop(require_key(args[0], "remove"), _MISSING) is not _MISSING


_MISSING = object()


DICT_METHODS = {
    "keys": dm_keys, "values": dm_values, "has": dm_has, "remove": dm_remove,
}


def call_str_method(s, name, args):
    fn = STR_METHODS.get(name)
    if fn is None:
        raise Panic("no method `%s` on type `str`" % name)
    return fn(s, args)


def call_dict_method(d, name, args):
    fn = DICT_METHODS.get(name)
    if fn is None:
        raise Panic("no method `%s` on type `dict`" % name)
    return fn(d, args)


BUILTINS = {
    "print": bi_print, "eprint": bi_eprint, "len": bi_len, "str": bi_str,
    "int": bi_int, "f64": bi_f64, "chr": bi_chr, "ord": bi_ord,
    "push": bi_push, "pop": bi_pop,
    "split": bi_split, "substr": bi_substr, "read_file": bi_read_file,
    "file_exists": bi_file_exists,
    "write_file": bi_write_file, "args": lambda a, ip: list(ip.argv),
    "fatal": bi_fatal, "exit": bi_exit, "range": bi_range,
    "alloc": bi_alloc, "arena_new": bi_arena_new, "arena_alloc": bi_arena_alloc,
    "arena_free": bi_arena_free, "ptr_len": bi_ptr_len, "is_null": bi_is_null,
    "abs": bi_abs, "sqrt": bi_sqrt, "floor": bi_floor, "ceil": bi_ceil,
    "round": bi_round, "pow": bi_pow,
}


class Interp:
    def __init__(self, argv):
        self.genv = Env(None)
        self.argv = argv
        self.structs = {}
        self.modules = {}
        self.loading = []
        self.entry_dir = os.getcwd()
        self.cur_dir = []

    def search_bases(self, importer_dir):
        bases = []
        for b in (importer_dir, self.entry_dir):
            if b and b not in bases:
                bases.append(b)
        return bases

    def load_path(self, dotted, importer_dir, line, col):
        rel = dotted.replace(".", "/")
        parts = dotted.split(".")
        for base in self.search_bases(importer_dir):
            leaf_file = os.path.join(base, rel + ".mnd")
            leaf_pkg = os.path.join(base, rel, "__init__.mnd")
            if os.path.isfile(leaf_file):
                leaf = self.load_unit(dotted, leaf_file, line, col)
            elif os.path.isfile(leaf_pkg):
                leaf = self.load_unit(dotted, leaf_pkg, line, col)
            else:
                continue
            chain = [leaf]
            for i in range(len(parts) - 1, 0, -1):
                prefix = ".".join(parts[:i])
                pdir = os.path.join(base, "/".join(parts[:i]))
                pinit = os.path.join(pdir, "__init__.mnd")
                if os.path.isfile(pinit):
                    pkg = self.load_unit(prefix, pinit, line, col)
                elif os.path.isdir(pdir):
                    key = os.path.realpath(pdir) + "/"
                    pkg = self.modules.get(key)
                    if pkg is None:
                        pkg = MModule(prefix, pdir)
                        self.modules[key] = pkg
                else:
                    raise CompileError("cannot find package `%s`" % prefix, line, col)
                chain.append(pkg)
            for k in range(len(chain) - 1):
                chain[k + 1].members[parts[len(parts) - k - 1]] = chain[k]
            return chain[-1], leaf
        tried = ", ".join("`%s`" % os.path.join(b, rel + ".mnd")
                          for b in self.search_bases(importer_dir))
        raise CompileError("cannot find module `%s` (tried %s)" % (dotted, tried),
                           line, col)

    def load_unit(self, dotted, path, line, col, menv=None):
        global CUR_FILE
        key = os.path.realpath(path)
        if key in self.loading:
            names = [os.path.basename(p) for p in self.loading[self.loading.index(key):]]
            names.append(os.path.basename(path))
            raise CompileError("circular import: %s" % " -> ".join(names), line, col)
        if key in self.modules:
            return self.modules[key]
        prev_file = CUR_FILE
        CUR_FILE = path
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            raise CompileError("cannot read module file `%s`" % path, line, col)
        mod = MModule(dotted, os.path.abspath(path))
        self.modules[key] = mod
        if menv is None:
            menv = Env(None)
        mod.env = menv
        self.loading.append(key)
        self.cur_dir.append(os.path.dirname(os.path.abspath(path)))
        try:
            ast = Parser(tokenize(src)).parse()
            for item in ast[1]:
                if item[0] == "struct":
                    self.decl_struct(item, menv)
            for item in ast[1]:
                if item[0] == "def":
                    fnval = (item[2], item[4], menv)
                    menv.vars[item[1]] = fnval
                    mod.members[item[1]] = fnval
            for item in ast[1]:
                if item[0] not in ("def", "struct"):
                    self.exec_stmt(item, menv)
            for k, v in menv.vars.items():
                if k not in mod.members:
                    mod.members[k] = v
        finally:
            self.loading.pop()
            self.cur_dir.pop()
            CUR_FILE = prev_file
        return mod

    def decl_struct(self, item, menv):
        methods = {}
        for m in item[3]:
            methods[m[1]] = (m[2], m[4], menv)
        self.structs[item[1]] = StructDef(item[1], item[2], methods)

    def exec_block(self, stmts, env):
        for s in stmts:
            self.exec_stmt(s, env)

    def exec_stmt(self, node, env):
        tag = node[0]
        if tag == "expr":
            self.eval(node[1], env)
        elif tag == "let":
            env.vars[node[1]] = self.eval(node[3], env)
        elif tag == "assign":
            self.exec_assign(node, env)
        elif tag == "return":
            v = self.eval(node[1], env) if node[1] is not None else None
            raise ReturnEx(v)
        elif tag == "if":
            for cond, body in node[1]:
                if truthy(self.eval(cond, env)):
                    self.exec_block(body, Env(env))
                    return
            if node[2] is not None:
                self.exec_block(node[2], Env(env))
        elif tag == "while":
            while truthy(self.eval(node[1], env)):
                try:
                    self.exec_block(node[2], Env(env))
                except BreakEx:
                    break
                except ContinueEx:
                    continue
        elif tag == "for":
            self.exec_for(node, env)
        elif tag == "unsafe":
            self.exec_block(node[1], Env(env))
        elif tag == "import":
            top, leaf = self.load_path(node[1], self.cur_dir[-1], node[3], node[4])
            if node[2]:
                env.vars[node[2]] = leaf
            else:
                env.vars[node[1].split(".")[0]] = top
        elif tag == "from":
            mod = self.load_path(node[1], self.cur_dir[-1], node[3], node[4])[1]
            mdir = os.path.dirname(mod.path)
            for pair in node[2]:
                name, alias = pair[0], pair[1]
                if name in mod.members:
                    env.vars[alias or name] = mod.members[name]
                    continue
                sub_file = os.path.join(mdir, name + ".mnd")
                sub_pkg = os.path.join(mdir, name, "__init__.mnd")
                if os.path.isfile(sub_file) or os.path.isfile(sub_pkg):
                    sub = self.load_path(node[1] + "." + name,
                                         self.cur_dir[-1], node[3], node[4])[1]
                    mod.members[name] = sub
                    env.vars[alias or name] = sub
                    continue
                if name not in self.structs:
                    raise CompileError("cannot import name `%s` from module `%s`"
                                       % (name, node[1]), node[3], node[4])
                if alias and alias != name:
                    self.structs[alias] = self.structs[name]
        elif tag == "break":
            raise BreakEx()
        elif tag == "continue":
            raise ContinueEx()
        else:
            raise Panic("interpreter error: unknown statement '%s'" % tag)

    def exec_for(self, node, env):
        it = self.eval(node[2], env)
        if isinstance(it, range):
            seq = it
        elif isinstance(it, str):
            seq = list(it)
        elif isinstance(it, MndDict):
            seq = list(it.d.keys())
        else:
            require_list(it)
            seq = it
        for x in seq:
            benv = Env(env)
            benv.vars[node[1]] = x
            try:
                self.exec_block(node[3], benv)
            except BreakEx:
                break
            except ContinueEx:
                continue

    def exec_assign(self, node, env):
        target = node[1]
        if target[0] == "var":
            name = target[1]
            scope = env
            while scope is not None:
                if name in scope.vars:
                    scope.vars[name] = self.eval(node[2], env)
                    return
                scope = scope.parent
            raise Panic("undefined variable '%s'" % name)
        if target[0] == "field":
            base = self.eval(target[1], env)
            val = self.eval(node[2], env)
            if not isinstance(base, MStruct):
                raise Panic("no field `%s` on type `%s`"
                            % (target[2], type_name(base)))
            if target[2] not in base.vals:
                raise Panic("no field `%s` on type `%s`"
                            % (target[2], base.sd.name))
            base.vals[target[2]] = val
            return
        base = self.eval(target[1], env)
        raw = self.eval(target[2], env)
        val = self.eval(node[2], env)
        if isinstance(base, MndDict):
            if not is_int(raw) and not isinstance(raw, str):
                raise Panic("dict key must be `str` or `int`, found `%s`" % type_name(raw))
            base.d[raw] = val
            return
        idx = require_index(raw)
        if isinstance(base, MndPtr):
            ptr_alive(base)
            pos = base.off + idx
            if pos < 0 or pos >= base.ln:
                panic_index(base.ln, pos)
            base.slots[pos] = val
            return
        if base is None:
            raise Panic("null pointer access")
        if isinstance(base, str):
            raise Panic("strings are immutable: cannot assign to a string index")
        require_list(base)
        if idx < 0 or idx >= len(base):
            panic_index(len(base), idx)
        base[idx] = val

    def eval(self, node, env):
        tag = node[0]
        if tag == "num" or tag == "fnum" or tag == "str" or tag == "bool":
            return node[1]
        if tag == "nil":
            return None
        if tag == "var":
            name = node[1]
            scope = env
            while scope is not None:
                if name in scope.vars:
                    return scope.vars[name]
                scope = scope.parent
            raise Panic("undefined variable '%s'" % name)
        if tag == "list":
            return [self.eval(x, env) for x in node[1]]
        if tag == "dict":
            d = MndDict()
            for p in node[1]:
                d.d[self.eval(p[0], env)] = self.eval(p[1], env)
            return d
        if tag == "slit":
            return self.eval_slit(node, env)
        if tag == "field":
            return self.eval_field(node, env)
        if tag == "call":
            return self.eval_call(node, env)
        if tag == "index":
            base = self.eval(node[1], env)
            raw = self.eval(node[2], env)
            if isinstance(base, MndDict):
                if not is_int(raw) and not isinstance(raw, str):
                    raise Panic("dict key must be `str` or `int`, found `%s`"
                                % type_name(raw))
                if raw not in base.d:
                    raise Panic("no key `%s` in dict" % raw)
                return base.d[raw]
            idx = require_index(raw)
            if isinstance(base, MndPtr):
                ptr_alive(base)
                pos = base.off + idx
                if pos < 0 or pos >= base.ln:
                    panic_index(base.ln, pos)
                return base.slots[pos]
            if base is None:
                raise Panic("null pointer access")
            if isinstance(base, str):
                if idx < 0 or idx >= len(base):
                    panic_index(len(base), idx)
                return base[idx]
            require_list(base)
            if idx < 0 or idx >= len(base):
                panic_index(len(base), idx)
            return base[idx]
        if tag == "bin":
            return self.eval_bin(node, env)
        if tag == "un":
            v = self.eval(node[2], env)
            if node[1] == "-":
                if is_f64(v):
                    return -v
                if not is_int(v):
                    raise Panic("cannot apply unary `-` to type `%s`" % type_name(v))
                return wrap64(-v)
            return not truthy(v)
        raise Panic("interpreter error: unknown expression '%s'" % tag)

    def eval_slit(self, node, env):
        name = node[1]
        sd = self.structs.get(name)
        if sd is None:
            raise Panic("undefined struct '%s'" % name)
        vals = {}
        for f in node[2]:
            if f[0] in vals:
                raise Panic("field `%s` specified more than once" % f[0])
            vals[f[0]] = self.eval(f[1], env)
        for f in sd.fields:
            if f[0] not in vals:
                raise Panic("missing field `%s` in initializer for `%s`"
                            % (f[0], name))
        known = set(f[0] for f in sd.fields)
        for k in vals:
            if k not in known:
                raise Panic("struct `%s` has no field named `%s`" % (name, k))
        return MStruct(sd, vals)

    def eval_field(self, node, env):
        base = self.eval(node[1], env)
        name = node[2]
        if isinstance(base, MStruct):
            if name in base.vals:
                return base.vals[name]
            raise Panic("no field `%s` on type `%s`" % (name, base.sd.name))
        if isinstance(base, MModule):
            if name in base.members:
                return base.members[name]
            raise Panic("no member `%s` in module `%s`" % (name, base.name))
        raise Panic("no field `%s` on type `%s`" % (name, type_name(base)))

    def eval_call(self, node, env):
        fn = node[1]
        if fn[0] == "field":
            return self.eval_method(node, env)
        if fn[0] != "var":
            raise Panic("cannot call a non-function expression")
        name = fn[1]
        args = [self.eval(a, env) for a in node[2]]
        b = BUILTINS.get(name)
        if b is not None:
            return b(args, self)
        f = None
        scope = env
        while scope is not None:
            if name in scope.vars:
                f = scope.vars[name]
                break
            scope = scope.parent
        if f is None or not isinstance(f, tuple):
            raise Panic("undefined function '%s'" % name)
        params, body, menv = f
        if len(args) != len(params):
            raise Panic("function '%s' takes %d argument(s) but %d were given"
                        % (name, len(params), len(args)))
        fenv = Env(menv)
        for p, a in zip(params, args):
            fenv.vars[p[0]] = a
        try:
            self.exec_block(body, fenv)
        except ReturnEx as r:
            return r.value
        return None

    def call_fn(self, f, base, args, what):
        params, body, menv = f
        rest = params[1:] if base is not None else params
        if len(args) != len(rest):
            raise Panic("function `%s` takes %d argument(s) but %d were given"
                        % (what, len(rest), len(args)))
        fenv = Env(menv)
        if base is not None:
            fenv.vars[params[0][0]] = base
        for p, a in zip(rest, args):
            fenv.vars[p[0]] = a
        try:
            self.exec_block(body, fenv)
        except ReturnEx as r:
            return r.value
        return None

    def eval_method(self, node, env):
        fnode = node[1]
        base = self.eval(fnode[1], env)
        mname = fnode[2]
        args = [self.eval(a, env) for a in node[2]]
        if isinstance(base, str):
            return call_str_method(base, mname, args)
        if isinstance(base, MndDict):
            return call_dict_method(base, mname, args)
        if isinstance(base, MModule):
            m = base.members.get(mname)
            if m is None:
                raise Panic("no member `%s` in module `%s`" % (mname, base.name))
            if isinstance(m, MModule):
                raise Panic("module `%s` is not callable" % m.name)
            if not isinstance(m, tuple):
                raise Panic("member `%s` of module `%s` is not a function"
                            % (mname, base.name))
            return self.call_fn(m, None, args, mname)
        if not isinstance(base, MStruct):
            raise Panic("no method `%s` on type `%s`" % (mname, type_name(base)))
        m = base.sd.methods.get(mname)
        if m is None:
            raise Panic("no method `%s` on struct `%s`" % (mname, base.sd.name))
        if not m[0]:
            raise Panic("method `%s` has no `self` parameter" % mname)
        return self.call_fn(m, base, args, mname)

    def eval_bin(self, node, env):
        op = node[1]
        if op == "and":
            if not truthy(self.eval(node[2], env)):
                return False
            return truthy(self.eval(node[3], env))
        if op == "or":
            if truthy(self.eval(node[2], env)):
                return True
            return truthy(self.eval(node[3], env))
        l = self.eval(node[2], env)
        r = self.eval(node[3], env)
        if op == "+":
            if isinstance(l, str) and isinstance(r, str):
                return l + r
            if isinstance(l, MndPtr) and is_int(r):
                return MndPtr(l.slots, l.ln, l.off + r, l.owner)
            if is_f64(l) and is_f64(r):
                return l + r
            if is_int(l) and is_int(r):
                return wrap64(l + r)
            raise Panic("cannot apply operator `+` to types `%s` and `%s`"
                        % (type_name(l), type_name(r)))
        if op == "-" and isinstance(l, MndPtr) and is_int(r):
            return MndPtr(l.slots, l.ln, l.off - r, l.owner)
        if op in ("-", "*", "/", "%"):
            if is_f64(l) and is_f64(r):
                if op == "-":
                    return l - r
                if op == "*":
                    return l * r
                if op == "/":
                    return fdiv(l, r)
                return fmod_(l, r)
            if not (is_int(l) and is_int(r)):
                raise Panic("cannot apply operator `%s` to types `%s` and `%s`"
                            % (op, type_name(l), type_name(r)))
            if op == "-":
                return wrap64(l - r)
            if op == "*":
                return wrap64(l * r)
            if r == 0:
                if op == "/":
                    raise Panic("attempt to divide by zero")
                raise Panic("attempt to calculate the remainder with a divisor of zero")
            if op == "/":
                q = abs(l) // abs(r)
                return wrap64(q if (l >= 0) == (r >= 0) else -q)
            m = abs(l) % abs(r)
            return m if l >= 0 else -m
        if op == "==":
            return ml_eq(l, r)
        if op == "!=":
            return not ml_eq(l, r)
        if op in ("<", "<=", ">", ">="):
            ok = ((is_int(l) and is_int(r)) or (is_f64(l) and is_f64(r))
                  or (isinstance(l, str) and isinstance(r, str)))
            if not ok:
                raise Panic("cannot compare `%s` with `%s`" % (type_name(l), type_name(r)))
            if op == "<":
                return l < r
            if op == "<=":
                return l <= r
            if op == ">":
                return l > r
            return l >= r
        raise Panic("interpreter error: unknown operator '%s'" % op)


CUR_FILE = "<unknown>"


def run_files(files, argv):
    global CUR_FILE
    if len(files) != 1:
        raise Fatal("error: expected a single entry file; other modules are "
                    "pulled in with `import`", 1)
    ip = Interp(argv)
    ip.entry_dir = os.path.dirname(os.path.abspath(files[0]))
    ip.cur_dir = [ip.entry_dir]
    ip.load_unit("", files[0], 1, 1, menv=ip.genv)
    CUR_FILE = files[0]


def main_work(argv):
    args = argv[1:]
    if "--" in args:
        k = args.index("--")
        files, rest = args[:k], args[k + 1:]
    else:
        files, rest = args, []
    if not files:
        sys.stderr.write("usage: mandela.py <script.mnd> [-- args]\n")
        return 2
    try:
        run_files(files, rest)
        return 0
    except Fatal as e:
        if e.msg:
            sys.stderr.write(str(e.msg) + "\n")
        return e.code
    except Panic as e:
        sys.stderr.write("panic: %s\n" % e.msg)
        return 101
    except CompileError as e:
        sys.stderr.write("error: %s\n  --> %s:%d:%d\n" % (e.msg, e.file, e.line, e.col))
        return 1
    except RecursionError:
        sys.stderr.write("panic: stack overflow\n")
        return 101
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except BreakEx:
        sys.stderr.write("error: `break` outside of a loop\n")
        return 1
    except ContinueEx:
        sys.stderr.write("error: `continue` outside of a loop\n")
        return 1


def main():
    for mb in (512, 256, 64):
        try:
            threading.stack_size(mb * 1024 * 1024)
            break
        except (ValueError, RuntimeError, OverflowError):
            continue
    sys.setrecursionlimit(200000)
    holder = {"code": 1}

    def worker():
        try:
            holder["code"] = main_work(sys.argv)
        except BaseException as e:
            sys.stderr.write("fatal: internal bootstrap error: %s: %s\n"
                             % (type(e).__name__, e))
            holder["code"] = 1

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    sys.exit(holder["code"])


if __name__ == "__main__":
    main()
