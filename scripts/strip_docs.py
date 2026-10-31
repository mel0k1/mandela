#!/usr/bin/env python3
import ast
import io
import sys
import tokenize


def strip_file(path):
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines(True)
    drop = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                n = body[0]
                for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                    drop.add(ln)
    out = []
    for i, line in enumerate(lines, 1):
        if i in drop:
            continue
        out.append(line)
    src = "".join(out)
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    kept = []
    for t in toks:
        if t.type == tokenize.COMMENT:
            if t.start[0] == 1 and t.string.startswith("#!"):
                kept.append(t)
            continue
        kept.append(t)
    stripped = tokenize.untokenize(kept)
    res = []
    for line in stripped.splitlines():
        line = line.rstrip()
        if line.strip() == "":
            line = ""
        res.append(line)
    text = "\n".join(res)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    open(path, "w", encoding="utf-8").write(text + "\n")


for p in sys.argv[1:]:
    strip_file(p)
