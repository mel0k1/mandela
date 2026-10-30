#!/usr/bin/env python3
"""Mandela bootstrap engine: lexer, parser and tree-walking interpreter.

Exists only to run the self-hosted compiler (src/*.mnd) and user programs
until the toolchain emits native code. Not part of the language itself.
"""

import sys
import threading


class CompileError(Exception):
    def __init__(self, msg, line, col):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col


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


KEYWORDS = {
    "def", "return", "if", "elif", "else", "while", "for", "in", "let",
    "mut", "true", "false", "nil", "and", "or", "not", "break", "continue",
}

OPS2 = ("->", "==", "!=", "<=", ">=")
OPS1 = set("+-*/%=<>()[],:")


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
                v = 0
                while k < n and content[k].isdigit():
                    v = v * 10 + ord(content[k]) - 48
                    k += 1
                toks.append(("int", v, ln, col))
                j = k
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
                if c in "([":
                    depth += 1
                elif c in ")]":
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
            else:
                items.append(self.parse_stmt())
        return ["program", items, 1, 1]

    def parse_def(self):
        self.next()
        name = self.expect("id", None, "function name")
        self.expect("op", "(", "`(`")
        params = []
        if not self.at("op", ")"):
            while True:
                pn = self.expect("id", None, "parameter name")
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
        return self.parse_simple()

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
            if e[0] not in ("var", "index"):
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
                self.expect("op", ")", "`)`")
                e = ["call", e, args, op[2], op[3]]
            elif self.at("op", "["):
                op = self.next()
                idx = self.parse_expr()
                self.expect("op", "]", "`]`")
                e = ["index", e, idx, op[2], op[3]]
            else:
                break
        return e

    def parse_atom(self):
        t = self.peek()
        if t[0] == "int":
            self.next()
            return ["num", t[1], t[2], t[3]]
        if t[0] == "str":
            self.next()
            return ["str", t[1], t[2], t[3]]
        if t[0] == "id":
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
        if t[0] == "op" and t[1] == "[":
            self.next()
            items = []
            if not self.at("op", "]"):
                while True:
                    items.append(self.parse_expr())
                    if not self.eat("op", ","):
                        break
            self.expect("op", "]", "`]`")
            return ["list", items, t[2], t[3]]
        raise CompileError("expected an expression, found %s" % describe(t), t[2], t[3])


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
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return type(v).__name__


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


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
    return a == b


def panic_index(n, i):
    raise Panic("index out of bounds: the len is %d but the index is %d" % (n, i))


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
    if isinstance(v, str):
        if nested:
            s = (v.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\t", "\\t"))
            return '"%s"' % s
        return v
    if isinstance(v, list):
        return "[" + ", ".join(format_value(x, True) for x in v) + "]"
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
    raise Panic("expected `str` or `list` in len(), found `%s`" % type_name(v))


def bi_str(args, ip):
    return format_value(one_arg(args, "str()"))


def bi_int(args, ip):
    v = one_arg(args, "int()")
    if is_int(v):
        return v
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


BUILTINS = {
    "print": bi_print, "eprint": bi_eprint, "len": bi_len, "str": bi_str,
    "int": bi_int, "chr": bi_chr, "ord": bi_ord, "push": bi_push, "pop": bi_pop,
    "split": bi_split, "substr": bi_substr, "read_file": bi_read_file,
    "write_file": bi_write_file, "args": lambda a, ip: list(ip.argv),
    "fatal": bi_fatal, "exit": bi_exit, "range": bi_range,
}


class Interp:
    def __init__(self, argv):
        self.genv = Env(None)
        self.argv = argv

    def run(self, program):
        for item in program[1]:
            if item[0] == "def":
                self.genv.vars[item[1]] = (item[2], item[4])
        for item in program[1]:
            if item[0] != "def":
                self.exec_stmt(item, self.genv)

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
        else:
            require_list(it)
            seq = it
        benv = Env(env)
        for x in seq:
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
        base = self.eval(target[1], env)
        idx = require_index(self.eval(target[2], env))
        val = self.eval(node[2], env)
        if isinstance(base, str):
            raise Panic("strings are immutable: cannot assign to a string index")
        require_list(base)
        if idx < 0 or idx >= len(base):
            panic_index(len(base), idx)
        base[idx] = val

    def eval(self, node, env):
        tag = node[0]
        if tag == "num" or tag == "str" or tag == "bool":
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
        if tag == "call":
            return self.eval_call(node, env)
        if tag == "index":
            base = self.eval(node[1], env)
            idx = require_index(self.eval(node[2], env))
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
                if not is_int(v):
                    raise Panic("cannot apply unary `-` to type `%s`" % type_name(v))
                return -v
            return not truthy(v)
        raise Panic("interpreter error: unknown expression '%s'" % tag)

    def eval_call(self, node, env):
        fn = node[1]
        if fn[0] != "var":
            raise Panic("cannot call a non-function expression")
        name = fn[1]
        args = [self.eval(a, env) for a in node[2]]
        b = BUILTINS.get(name)
        if b is not None:
            return b(args, self)
        f = self.genv.vars.get(name)
        if f is None or not isinstance(f, tuple):
            raise Panic("undefined function '%s'" % name)
        params, body = f
        if len(args) != len(params):
            raise Panic("function '%s' takes %d argument(s) but %d were given"
                        % (name, len(params), len(args)))
        fenv = Env(self.genv)
        for p, a in zip(params, args):
            fenv.vars[p[0]] = a
        try:
            self.exec_block(body, fenv)
        except ReturnEx as r:
            return r.value
        return None

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
            if is_int(l) and is_int(r):
                return l + r
            raise Panic("cannot apply operator `+` to types `%s` and `%s`"
                        % (type_name(l), type_name(r)))
        if op in ("-", "*", "/", "%"):
            if not (is_int(l) and is_int(r)):
                raise Panic("cannot apply operator `%s` to types `%s` and `%s`"
                            % (op, type_name(l), type_name(r)))
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if r == 0:
                if op == "/":
                    raise Panic("attempt to divide by zero")
                raise Panic("attempt to calculate the remainder with a divisor of zero")
            if op == "/":
                q = abs(l) // abs(r)
                return q if (l >= 0) == (r >= 0) else -q
            m = abs(l) % abs(r)
            return m if l >= 0 else -m
        if op == "==":
            return ml_eq(l, r)
        if op == "!=":
            return not ml_eq(l, r)
        if op in ("<", "<=", ">", ">="):
            ok = (is_int(l) and is_int(r)) or (isinstance(l, str) and isinstance(r, str))
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
    ip = Interp(argv)
    items = []
    global CUR_FILE
    for f in files:
        CUR_FILE = f
        try:
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            raise Fatal("error: cannot open file '%s'" % f, 1)
        items.extend(Parser(tokenize(src)).parse()[1])
    CUR_FILE = files[0]
    ip.run(["program", items, 1, 1])


def main_work(argv):
    args = argv[1:]
    if "--" in args:
        k = args.index("--")
        files, rest = args[:k], args[k + 1:]
    else:
        files, rest = args, []
    if not files:
        sys.stderr.write("usage: mandela.py <script.mnd> [more.mnd ...] [-- args]\n")
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
        sys.stderr.write("error: %s\n  --> %s:%d:%d\n" % (e.msg, CUR_FILE, e.line, e.col))
        return 1
    except RecursionError:
        sys.stderr.write("panic: stack overflow\n")
        return 101
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
