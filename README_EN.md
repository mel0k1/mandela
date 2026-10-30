<div align="center">

<img src="assets/mandela.jpg" width="180" alt="Mandela">

# Mandela

**Python syntax · C++ power · Rust strictness**

The Mandela compiler is written in Mandela itself.

[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.1%20bootstrap-orange.svg)]()
[![Tests](https://img.shields.io/badge/tests-17%20passing-brightgreen.svg)](tests/run_tests.sh)

</div>

---

Mandela is a general-purpose programming language aimed at both low-level systems code and everyday scripting. The syntax stays as close to Python as possible: indentation-based blocks, readable commands, no ceremony. Under the hood it enforces Rust-grade discipline: static typing, immutability by default, and a compiler that refuses to let mistakes through. Native performance and C++-level control are on the roadmap.

## Philosophy

| From | What we take |
| --- | --- |
| **Python** | Syntax: indentation instead of braces, `def`, `for x in ...`, `print`, `len` |
| **Rust** | Strictness: static types, `let`/`let mut`, rustc-style diagnostics, panics with exit code 101 |
| **C++** | Low-level reach: C integer semantics, a path to manual memory and `unsafe` |

## Quick start

```bash
git clone https://github.com/mel0k1/mandela.git
cd mandela
./bin/mandela run examples/hello.mnd
```

Nothing to build: you only need `python3` (the bootstrap engine).

## Example

```python
# examples/fib.mnd
def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

for i in range(0, 11):
    print("fib(" + str(i) + ") = " + str(fib(i)))
```

```text
$ ./bin/mandela run examples/fib.mnd
fib(0) = 0
fib(1) = 1
...
fib(10) = 55
```

An ASCII mandelbrot fits in plain integers too — see [examples/mandel.mnd](examples/mandel.mnd).

## The compiler catches it

`./bin/mandela check` reports mistakes before the program runs, formatted like rustc:

```text
error[E0384]: cannot assign to `n`, as it is not declared as mutable
  --> demo.mnd:5:1
  |
5 | n = "ten"
  | ^
error[E0308]: mismatched types: expected `int`, found `str`
  --> demo.mnd:5:1
  |
5 | n = "ten"
  | ^
error[E0061]: this function takes 2 arguments but 1 arguments were supplied
  --> demo.mnd:6:11
  |
6 | print(area(2))
  |           ^
```

Error codes mirror rustc wherever the meaning matches: `E0308`, `E0369`, `E0384`, `E0061`, `E0425`, `E0428`, `E0277`, `E0268`, `E0267`, `E0572`, `E0282`, `E0412`, `E0594`, `E0618`. The full list lives in the [specification](spec/SPEC.md).

## Architecture: self-hosted

The defining feature of Mandela is **self-hosting**. The compiler front-end (lexer, parser, type checker) is written neither in Python nor in C, but in Mandela itself:

```text
          your .mnd code
               │
               ▼
   ┌───────────────────────────┐
   │  src/*.mnd                │  ← the Mandela compiler,
   │  lexer · parser · checker │    written in Mandela
   └────────────┬──────────────┘
                │  rustc-style diagnostics
                ▼
        check passed?
                │
                ▼
   ┌───────────────────────────┐
   │  bootstrap/mandela.py     │  ← bootstrap engine (Python),
   │  temporary runner         │    runs both the compiler and code
   └───────────────────────────┘
```

`bootstrap/mandela.py` is temporary scaffolding: a minimal interpreter that exists only to launch the self-hosted compiler. It will be retired once Mandela emits machine code (see the roadmap).

## CLI

```bash
./bin/mandela run file.mnd [args...]   # check and execute
./bin/mandela check file.mnd           # strict check only
./bin/mandela lex file.mnd             # token dump (debug)
./bin/mandela ast file.mnd             # AST dump as s-expressions (debug)
```

## v0.1 features

- Static typing with inference: `let x = 5`, `let name: str = "..."`, `list[int]`
- Immutability by default: `let` vs `let mut`
- Integers (C division semantics), strings, `bool`, `nil`, lists
- `if/elif/else`, `while`, `for x in range(...)`, `for x in list`, `break`, `continue`
- Functions with mandatory parameter annotations, recursion, function hoisting
- Built-ins: `print len str int chr ord push pop split substr read_file write_file args fatal exit range`
- Strict type checker with rustc-style diagnostics, unused-variable warnings

## Roadmap

| Version | Plan |
| --- | --- |
| v0.2 | C codegen and native binaries, bootstrap engine retirement |
| v0.3 | `unsafe` blocks, `ptr<T>`, manual memory management |
| v0.4 | Floating point, structs, methods |
| v0.5 | Modules, package manager |

## Development

```bash
make test        # 17 tests: positive and negative
```

Repository layout:

```text
bin/mandela          CLI
bootstrap/mandela.py bootstrap engine (temporary)
src/*.mnd            the compiler, written in Mandela
spec/SPEC.md         language specification v0.1
examples/            example programs
tests/               positive and negative tests
```

## License

[GPL-3.0](LICENSE) © mel0k1
