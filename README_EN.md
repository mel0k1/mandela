<div align="center">

<img src="assets/mandela.jpg" width="180" alt="Mandela">

# Mandela

**Simple syntax · Strict typing · Native code**

The Mandela compiler is written in Mandela itself.

English | [Русский](README.md)

[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.6%20machine%20code%20backend-orange.svg)]()
[![Tests](https://img.shields.io/badge/tests-96%20passing-brightgreen.svg)](tests/run_tests.sh)

</div>

---

Mandela is a general-purpose programming language. A concise, indentation-based
syntax: no braces, no semicolons, programs read like pseudocode. Strict static
typing with type inference, immutability by default. Detailed compiler
diagnostics catch mistakes before the program runs. And `mandela build` turns a
program into a native executable — machine code for your platform.

## Principles

- **Simplified syntax.** Indentation-based blocks, `def`, `for x in ...`,
  `print`, `len` — minimal ceremony, maximum readability.
- **Strict typing.** The type of every variable is known before the program
  runs. No implicit conversions; `let` is immutable by default, mutation
  requires `let mut`.
- **Honest errors.** Every diagnostic carries a number (`error[E0308]`), a
  file, a line and a caret under the offending token. All errors in one pass.
- **Native result.** One `.mnd` file → check → intermediate representation →
  machine code via the system toolchain.

## Quick start

```bash
git clone https://github.com/mel0k1/mandela.git
cd mandela
./bin/mandela run examples/hello.mnd      # instant start: interpreter
./bin/mandela build examples/hello.mnd    # or a native binary right away
./hello
```

No extra dependencies: the bootstrap engine and the machine-code backend
ship with the repository. The C backend additionally needs a system compiler
(`cc`, `gcc`, `clang`).

## Example

```mandela
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

An ASCII mandelbrot fits in plain integers too — see [examples/mandel.mnd](examples/mandel.mnd),
and a floating-point version lives in [examples/mandelbrot.mnd](examples/mandelbrot.mnd).

## Structs and methods

```mandela
# examples/structs.mnd
struct Point:
    x: int
    y: int

    def dot(self, other: Point) -> int:
        return self.x * other.x + self.y * other.y

    def move_by(mut self, dx: int, dy: int):
        self.x = self.x + dx
        self.y = self.y + dy

def main():
    let mut a = Point { x: 1, y: 2 }
    let b = Point { x: 3, y: 4 }
    a.move_by(10, 20)      # ok: a is `let mut`
    print(a.dot(b))
```

A method that mutates `self` declares `mut self`; calling it on an immutable
binding is a compile-time error (`E0596`).

## Dictionaries and string methods

```mandela
# examples/dicts.mnd
let mut freq: dict[str, int] = {}
for w in "to be or not to be".split(" "):
    if freq.has(w):
        freq[w] = freq[w] + 1
    else:
        freq[w] = 1
print(freq)                       # {"to": 2, "be": 2, "or": 1, "not": 1}

print("  Hello  ".trim().upper()) # HELLO
print("mandela".replace("a", "4"))# m4ndel4
```

Dictionaries keep insertion order; keys are `str` or `int`. Dictionary methods:
`keys`, `values`, `has`, `remove`. String methods: `upper`, `lower`, `trim`,
`split`, `contains`, `starts_with`, `ends_with`, `replace`, `find`, `substr`,
`repeat`, `chars`.

## Floating point

```mandela
# examples/floats.mnd
let radius = 2.5
let pi = 3.14159
print(pi * radius * radius)      # 19.6349375

def hypot(a: f64, b: f64) -> f64:
    return sqrt(a * a + b * b)

print(hypot(3.0, 4.0))           # 5.0
print(f64(7) / 2.0)              # 3.5 — explicit int → f64 conversion
print(int(3.9))                  # 3   — truncation toward zero
print(1.0 / 0.0)                 # inf — IEEE 754, no panic
```

The `f64` type is an IEEE 754 double. `int` and `f64` never mix implicitly
(`x + n` is `error[E0369]`); conversions are explicit: `f64(x)` and `int(x)`.
Division by zero does not panic: `1.0/0.0 = inf`, `0.0/0.0 = nan`. Output uses
the shortest decimal string that round-trips: `2.0`, `0.1`,
`0.30000000000000004`, `1e+16`. Math built-ins: `abs`, `sqrt`, `floor`,
`ceil`, `round`, `pow`.

## Pointers and arenas

There is no garbage collector: bulk buffers are allocated explicitly from
arenas and released as whole regions. Every memory operation is checked at
runtime — there is no undefined behavior; a broken program fails with an
honest, deterministic panic:

```mandela
# examples/arenas.mnd
struct Vec:
    data: ptr[int]
    len: int
    cap: int

    def push(mut self, a: Arena, v: int):
        if self.len == self.cap:
            self.grow(a)              # doubling inside the same arena
        unsafe:
            self.data[self.len] = v
        self.len = self.len + 1

def main():
    let a = arena_new()
    let mut v = Vec { data: alloc(4, 0), len: 0, cap: 4 }
    for i in range(1, 21):
        v.push(a, i * i)
    unsafe:
        print(arena_free(a))          # 896: the whole region at once
```

Reading and writing through a pointer, as well as `arena_free`, are only
allowed inside an `unsafe` block (`E0133`). Out-of-bounds access, use of `nil`,
use after `arena_free` and double free are deterministic panics with exit code
101 — identical in the interpreter and in the native binary.

## The compiler catches it

`./bin/mandela check` reports mistakes before the program runs, each with a
code, a location and a caret:

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

Diagnostics are numbered with codes: `E0061`, `E0062`, `E0063`, `E0308`,
`E0369`, `E0384`, `E0412`, `E0425`, `E0428`, `E0596`, `E0599`, `E0609` and
more. The full list lives in the [specification](spec/SPEC.md).

## Building native code

```bash
./bin/mandela build program.mnd -o program   # check → native binary
./program
./bin/mandela build program.mnd --emit c     # dump the intermediate representation
./bin/mandela build program.mnd --emit asm   # assembly listing
```

The pipeline: the self-hosted checker validates types → the backend lowers the
AST to a self-contained intermediate representation — a single portable source
whose runtime is described by a handful of structs: a tagged value, a string, a
list, a struct, a dict; user structs become definition tables, methods become
plain functions. The system compiler then turns it into machine code with
`-O2`. The test suite guarantees the interpreter and the native binary print
the same output.

## Architecture: self-hosted

The defining feature of Mandela is **self-hosting**. The compiler front-end
(lexer, parser, type checker) is written in Mandela itself:

```text
          your .mnd code
               │
               ▼
   ┌───────────────────────────┐
   │  src/*.mnd                │  ← the Mandela compiler,
   │  lexer · parser · checker │    written in Mandela
   └────────────┬──────────────┘
                │  error[E0308] with a caret
                ▼
        check passed?
                │
      ┌─────────┴──────────┐
      ▼                    ▼
┌────────────────┐   ┌────────────────────────┐
│ bootstrap/     │   │ bootstrap/codegen.py   │
│ mandela.py     │   │ AST → IR → cc -O2      │
│ interpreter    │   │ native binary          │
└────────────────┘   └────────────────────────┘
 run: instant start   build: machine code
 and debugging
```

`bootstrap/mandela.py` is temporary scaffolding: a minimal interpreter that
exists only to launch the self-hosted compiler. It will be retired once Mandela
generates machine code on its own (see the roadmap).

## CLI

```bash
./bin/mandela run file.mnd [args...]    # check and execute (interpreter)
./bin/mandela build file.mnd [-o out]   # build a native binary (via C)
./bin/mandela build file.mnd --backend native -o out   # pure machine code
./bin/mandela build file.mnd --emit c   # dump the intermediate representation
./bin/mandela build file.mnd --emit asm # assembly listing
./bin/mandela check file.mnd            # strict check only
./bin/mandela lex file.mnd              # token dump (debug)
./bin/mandela ast file.mnd              # AST dump as s-expressions (debug)
```

## v0.6 features

- Static typing with inference: `let x = 5`, `let x = 2.5`, `list[int]`,
  `dict[str, int]`, `ptr[int]`, `ptr[Node]`
- Floating point `f64` (IEEE 754): literals `2.5`, `1.5e-3`, panic-free
  arithmetic (`inf`/`nan`), shortest round-trip printing, math built-ins
  `abs sqrt floor ceil round pow`, explicit `f64()`/`int()` conversions
- Immutability by default: `let` vs `let mut`, `mut self` for methods
- 64-bit integers (truncating division, two's-complement wrap-around),
  strings, `bool`, `nil`, lists, insertion-ordered dictionaries
- String methods: `upper lower trim split contains starts_with ends_with
  replace find substr repeat chars`
- Dictionary methods: `keys values has remove`
- Structs with fields and methods, `self`, `mut self` discipline
- Memory: arenas (`arena_new`, `arena_alloc`, `arena_free`), `ptr[T]` pointers
  with fill initialization, `unsafe` blocks, deterministic panics instead of UB
- `if/elif/else`, `while`, `for x in range(...)/list/string/dict`,
  `break`, `continue`
- Functions with mandatory parameter annotations, recursion, function hoisting
- Built-ins: `print len str int f64 chr ord push pop split substr read_file
  write_file args fatal exit range alloc arena_new arena_alloc arena_free
  ptr_len is_null abs sqrt floor ceil round pow`
- Code generation: `--backend native` — a direct machine-code backend for
  x86-64/ARM64 (instructions and the ELF image are emitted byte by byte — no C,
  no assembler, no linker, no libc; scalars, functions, recursion, loops,
  globals, print via write(2)); or the classic route through the system
  toolchain with `--emit c|asm|bin`
- Strict type checker with `E0xxx` diagnostics, unused-variable warnings

## Roadmap

| Version | Plan |
| --- | --- |
| v0.7 | Modules instead of linear scripts, package manager |
| v0.8 | Self-compilation: the Mandela compiler emits machine code for itself |
| v0.9 | Composite types in the machine-code backend: lists, dicts, structs |

## Development

```bash
make test        # 96 tests: positive, negative, panic and both-backend parity
```

Every positive test runs three times: on the interpreter, through the C
backend and through the machine-code backend — the output must match byte
for byte. Panic tests verify the interpreter and the compiled binaries abort
with the same message and exit code 101.

Repository layout:

```text
bin/mandela          CLI
bootstrap/mandela.py bootstrap engine (interpreter)
bootstrap/codegen.py C code-generation backend
bootstrap/mc_*.py    machine-code backend (x86-64/ARM64 encoders, ELF, runtime)
src/*.mnd            the compiler, written in Mandela
spec/SPEC.md         language specification v0.6
examples/            example programs
tests/               positive, negative, panic and native tests
```

## License

[GPL-3.0](LICENSE) © mel0k1
