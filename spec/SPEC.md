# The Mandela Language Specification — v0.7.0

Status: draft, implemented by the self-hosted compiler in `src/` and the code
generator in `bootstrap/codegen.py`.

Mandela is a statically typed, indentation-based language: a compact syntax on
the surface, strict static checking underneath, and native machine code at the
end of the build.

## 1. Lexical structure

### 1.1 Source files

Source files are UTF-8 text with the `.mnd` extension. Statements are separated
by newlines; blocks are defined by indentation (4 spaces per level is
conventional, any consistent width is accepted). Tabs count as 4 columns.
Blank lines and comment-only lines carry no indentation significance.

### 1.2 Comments

A comment starts with `#` and extends to the end of the line. There are no
block comments and no docstrings.

### 1.3 Identifiers and keywords

Identifiers match `[A-Za-z_][A-Za-z0-9_]*`. The reserved words are:

```text
def  return  if  elif  else  while  for  in  let  mut  struct
true  false  nil  and  or  not  break  continue  unsafe
import  from  as
```

### 1.4 Operators and punctuation

```text
+  -  *  /  %  =  ==  !=  <  <=  >  >=  ->  (  )  [  ]  {  }  ,  :  .
```

### 1.5 Literals

- Integer: `[0-9]+`, signed 64-bit.
- Floating point: `[0-9]+.[0-9]+` with an optional exponent
  `([eE][+-]?[0-9]+)?`, IEEE 754 binary64. `2.` and `.5` are not floats —
  the dot requires digits on both sides. There are no suffixes and no
  hexadecimal or underscore forms.
- String: `"..."` with escapes `\n \t \r \0 \" \\`. Strings do not span lines.
- Booleans: `true`, `false`. Unit: `nil`.

## 2. Types

| Type | Description |
| --- | --- |
| `int` | Signed 64-bit integer. Division `/` and remainder `%` truncate toward zero. Overflow wraps in two's complement. |
| `f64` | IEEE 754 double-precision float; see §12. |
| `str` | Immutable string. Indexing yields a one-character string. In native builds strings are byte sequences, so string offsets and lengths are reliable for ASCII input in v0.5. |
| `bool` | `true` or `false`. Conditions must be `bool`; there is no truthiness coercion. |
| `nil` | The unit-ish value `nil`, returned by functions without `->`. |
| `list[T]` | Growable array of `T`. Empty literals require an annotation: `let a: list[int] = []`. |
| `dict[K, V]` | Insertion-ordered map from `K` keys to `V` values; see §7. |
| `ptr[T]` | Raw pointer to `T` slots; see §9. `nil` is the null pointer. |
| `Arena` | A memory region; pointers are allocated from it and released together; see §9. |
| `struct` | Nominal record type defined in source; see §5. |

There is no implicit conversion between any two types. In particular an
integer never becomes a float on its own: `let x: f64 = 2` is `E0308` — write
`2.0`, or convert explicitly with `f64(2)`.

## 3. Declarations

```text
let x = 10                     # immutable, type inferred
let mut x = 10                 # mutable
let x: int = 10                # explicit annotation
let a: list[int] = []          # empty list needs an annotation
let d: dict[str, int] = {}     # empty dict needs an annotation
```

Every variable must be declared with `let` before assignment. Re-declaring a
name with a new `let` shadows it. Assignment to a name declared without `mut`
is an error (`E0384`). Loop variables and parameters are immutable.

## 4. Functions

```text
def add(a: int, b: int) -> int:
    return a + b
```

- Parameter annotations are mandatory. The return annotation is optional.
- A function with no `->` returns `nil` and may only use bare `return`.
- Functions are hoisted: they may be called before their definition and may
  recurse. Redefining a name is `E0428`.
- Top-level `let` declarations are globals; functions may read and (if declared
  `mut`) assign them.

## 5. Structs

A struct is a nominal record type with named fields and methods:

```text
struct Point:
    x: int
    y: int

    def dot(self, other: Point) -> int:
        return self.x * other.x + self.y * other.y

    def move_by(mut self, dx: int, dy: int):
        self.x = self.x + dx
        self.y = self.y + dy
```

- Structs are declared only at top level. Redefining a name is `E0428`.
- Fields are `name: Type` lines; a duplicate field is `E0124`.
- Values are built with a literal: `Point { x: 1, y: 2 }`. All fields must be
  given exactly once: missing is `E0063`, unknown is `E0560`, repeated is
  `E0062`, wrong value type is `E0308`.
- Struct types compose with lists and dicts: `list[Point]`,
  `dict[str, Point]`.
- `==` / `!=` compare two values of the same struct field-by-field.
- Field access is `value.field` (`E0609` if there is no such field).
- Field assignment `value.field = e` requires the binding to be `let mut`,
  otherwise `E0384`.

### 5.1 Methods

A `def` inside a struct body is a method. Its first parameter must be `self`
(`E0424` otherwise); `self` has the struct's type.

- Methods are called as `value.method(args)`; the checker reports `E0599` if
  no method with that name exists on the struct (or on the type, for
  non-struct receivers).
- A method that mutates `self` declares `mut self`. Inside it, assigning to
  `self.field` is allowed.
- Calling a `mut self` method on an immutable binding is `E0596`; a non-`mut`
  method cannot assign to `self.field` (`E0384`).
- Methods may call other methods through `self`; they may also call ordinary
  functions. Ordinary functions cannot be called as methods and vice versa.

## 6. Modules and packages

Every `.mnd` source file is a module. A module's top-level `def`, `struct` and
`let` declarations are private to it; other modules see only what they import.

```text
import parser                  # binds `parser`
import util.helper             # binds `util`; use util.helper.f(...)
import util.helper as h        # binds `h` = module util.helper
from parser import parse       # binds `parse` into this module
from util.nums import double as d2
```

`import` is allowed only at the top level of a module; inside a function it is
a syntax error. Modules are executed at import time: their top-level `let`
initializers and statements run once, on first import, and the resulting
values are shared by every later importer.

Resolution. A dotted path `a.b.c` maps to file `a/b/c.mnd` or to package
`a/b/c/` (a directory with `__init__.mnd`). Intermediate components are
directories; a directory without `__init__.mnd` is a namespace package and
contributes no declarations. The search order for every import is: the
directory of the importing file, then the directory of the entry program.
The first base directory under which the leaf resolves wins; all components
resolve under that same base.

Names. `import a.b` binds the top package `a`, matching Python; the submodule
is reached as `a.b`. An alias (`as`) binds the imported module itself.
`from a.b import name` binds `name` directly; functions keep their signatures,
`let` values are imported read-only, and structs are imported by name — a
struct literal `Point{...}` requires the struct to be imported this way, since
qualified type names (`a.b.Point`) are not supported in v0.7. Importing under
an alias binds the alias: `from geom import Point as P` allows `P{...}`.

Modules are namespaces, not values: a module name cannot be assigned, passed
or called (`E0609`, `E0618`). Struct names must be unique across all modules
of a program; two modules defining the same struct name is `E0428`.

Diagnostics:

| Code | Meaning |
| --- | --- |
| E0252 | The name is defined multiple times (import collides with an existing binding) |
| E0432 | Cannot find module / circular import |
| E0433 | Could not find `name` in module |

An import that is never used produces a warning (`unused import`), which does
not fail the build. Prefixing the binding with `_` suppresses it.

## 7. Dictionaries

`dict[K, V]` is an insertion-ordered map. `K` is `str` or `int`; `V` is any
type.

```text
let mut stock: dict[str, int] = {"apples": 3, "bananas": 5}
let by_id: dict[int, str] = {1: "one", 2: "two"}
```

- A literal is `{ key: value, ... }`; trailing commas are allowed. All keys
  must have one type and all values must have one type (`E0308` otherwise).
  A key type other than `str`/`int` is rejected (`E0308`).
- An empty literal needs an annotation: `let d: dict[str, int] = {}`
  (`E0282` otherwise).
- Reading is `d[k]` (`E0308` if the key type is wrong); a missing key panics:
  `panic: no key `x` in dict`.
- Writing is `d[k] = v`; a missing key is inserted at the end. Dict contents
  are mutable in place regardless of the binding's `mut`.
- A repeated key in a literal keeps the last value.
- `for k in d` iterates the keys in insertion order.
- `len(d)` returns the number of live entries.
- `==` / `!=` compare two dicts of the same type by contents.
- Dicts nest freely: `dict[str, list[int]]`, `dict[int, dict[str, int]]`.

### 6.1 Dictionary methods

| Method | Signature | Notes |
| --- | --- | --- |
| `d.keys()` | -> list[K] | Snapshot of the keys in insertion order. |
| `d.values()` | -> list[V] | Snapshot of the values. |
| `d.has(k)` | K -> bool | `true` when the key is present. |
| `d.remove(k)` | K -> bool | Removes and returns `true` on success; requires `let mut` (`E0596`). |

## 8. Methods on built-in types

String values have built-in methods:

| Method | Signature | Notes |
| --- | --- | --- |
| `s.upper()` / `s.lower()` | -> str | ASCII case folding. |
| `s.trim()` | -> str | Strips whitespace on both ends. |
| `s.split(sep)` | str -> list[str] | Empty separator panics. |
| `s.contains(sub)` | str -> bool | |
| `s.starts_with(p)` / `s.ends_with(p)` | str -> bool | |
| `s.replace(old, new)` | str, str -> str | Empty `old` panics. |
| `s.find(sub)` | str -> int | First index or `-1`. |
| `s.substr(start, len)` | int, int -> str | Offsets are clamped. |
| `s.repeat(n)` | int -> str | `n <= 0` yields an empty string. |
| `s.chars()` | -> list[str] | One-character strings. |

Wrong argument types are `E0308`, wrong arity is `E0061`. The standalone
`split(s, sep)` and `substr(s, start, len)` built-ins remain and behave
identically.

## 9. Pointers, arenas and `unsafe`

Mandela has no garbage collector. Bulk memory is managed explicitly with
region-based allocation: pointers are carved out of arenas and released
together with the arena they came from. All pointer operations are checked at
runtime — there is no undefined behaviour in v0.5.

### 8.1 Allocation

| Function | Signature | Notes |
| --- | --- | --- |
| `alloc(n, fill)` | int, T -> ptr[T] | `n` slots from the process-wide arena, each initialized with a copy of `fill`. The element type is inferred from `fill`. |
| `arena_new()` | -> Arena | Creates a fresh arena. |
| `arena_alloc(a, n, fill)` | Arena, int, T -> ptr[T] | Like `alloc`, but the slots live in `a`. |
| `arena_free(a)` | Arena -> int | [unsafe] Releases every allocation made in `a` and returns the number of slot bytes it had accounted (`16` per slot). |
| `ptr_len(p)` | ptr[T] -> int | Number of slots of the underlying allocation. |
| `is_null(p)` | ptr[T] -> bool | `true` for the null pointer. |

`nil` is the null pointer: it converts to any `ptr[T]` and compares equal to
itself. A negative length panics; `alloc(4, nil)` needs the target type to be
known (`E0282` otherwise).

### 8.2 Operations

```text
let p: ptr[int] = alloc(4, 0)   # four zero slots
p[2] = 41                       # store (unsafe)
print(p[2] + 1)                 # load (unsafe)
let q = p + 2                   # view two slots forward; `q[0]` is `p[2]`
let r = q - 1                   # views share the allocation
```

- Indexing a pointer reads/writes the slot at `view.offset + index`; the
  bounds check reports the absolute slot: for a 2-slot pointer,
  `p[3]` panics with `index out of bounds: the len is 2 but the index is 3`.
- `p + i` and `p - i` shift the view by `i` slots without checking bounds;
  only dereferencing checks. Views are values: they compare equal when they
  share the allocation and the offset (`p == p + 0`).
- `print` formats a pointer as `<ptr>` and an arena as `<arena>`; addresses
  are never shown, so output is reproducible.
- Pointers nest (`ptr[ptr[int]]`) and compose with lists, dicts and structs
  (`list[ptr[Node]]`, a `next: ptr[Node]` field for linked structures).

### 8.3 `unsafe`

Operations that read or write memory manually must sit inside an `unsafe`
block:

```text
unsafe:
    p[0] = 1
    print(p[0])
    print(arena_free(a))
```

An unsafe operation outside the block is `E0133`: indexing a pointer (read or
write) and calling `arena_free` require `unsafe`. Creating pointers, pointer
arithmetic, `ptr_len` and `is_null` are safe. `unsafe` is a normal block: it
introduces a scope and may nest.

`unsafe` means manual memory management, not unchecked memory: even inside an
`unsafe` block every access is bounds-checked, null-checked and checked
against its arena. Mandela has no undefined behaviour — a broken program
panics deterministically instead:

| Panic | Cause |
| --- | --- |
| `panic: null pointer access` | Indexing or measuring `nil`. |
| `panic: index out of bounds: the len is L but the index is I` | Slot access outside the allocation. |
| `panic: use after arena_free` | Access through a pointer whose arena was freed. |
| `panic: double free of an arena` | `arena_free` on the same arena twice. |
| `panic: use of a freed arena` | `arena_alloc` after `arena_free`. |
| `panic: ptr length must not be negative` | Negative `alloc`/`arena_alloc` length. |

## 10. Statements

```text
if cond:
    ...
elif cond:
    ...
else:
    ...

while cond:
    ...

for x in range(0, 10):   # int values
for c in "abc":          # one-character strings
for item in list_value:  # elements
for k in dict_value:     # keys in insertion order
```

- `range(a)`, `range(a, b)`, `range(a, b, step)` is allowed only as the `for`
  iterable (`E0308` elsewhere). A zero `step` panics at runtime.
- `break` / `continue` are valid only inside loops (`E0268` / `E0267`).
- `return` at top level is `E0572`.
- Single-line suites are allowed: `if x == 1: print("one")`.

## 11. Expressions

Precedence, from lowest to highest:

| Level | Operators | Associativity |
| --- | --- | --- |
| 1 | `or` | left |
| 2 | `and` | left |
| 3 | `not` | prefix |
| 4 | `== != < <= > >=` | left |
| 5 | `+ -` | left |
| 6 | `* / %` | left |
| 7 | unary `-` | prefix |
| 8 | call `f(x)`, method `v.m(x)` | postfix |
| 9 | index `a[i]`, field `v.f` | postfix |

Semantics:

- `+` adds integers or floats, or concatenates strings; mixing is `E0369`.
- `ptr[T] + int` and `ptr[T] - int` shift the view by that many slots (§9);
  any other pointer arithmetic combination is `E0369`.
- On floats `/` and `%` never panic: see §12. On integers they truncate
  toward zero and panic on a zero divisor.
- `==`/`!=` require the same type on both sides (`E0308`); lists, dicts and
  structs are compared by contents.
- Ordering comparisons require two ints, two floats or two strings (`E0369`).
- Indexing out of bounds panics at runtime:
  `panic: index out of bounds: the len is 3 but the index is 5`.
- Strings are immutable: `s[0] = "x"` is `E0594`.
- List literals `[1, 2, 3]` must have one element type (`E0308`).
- Trailing commas are allowed in call arguments, list literals, dict literals
  and struct literals.

## 12. Floating point

`f64` is an IEEE 754 binary64 double. Literals always contain a fractional
part (`2.0`, `0.5`) and may carry an exponent (`1.5e-3`, `6.02e23`); unary
`-` applies to floats as to integers.

### 11.1 Arithmetic

`+ - * / %` take two `f64` and return `f64`. Floating arithmetic follows IEEE
754 and never panics:

```text
1.0 / 0.0      # inf
-1.0 / 0.0     # -inf
0.0 / 0.0      # nan
1.0 % 0.0      # nan; otherwise % is C fmod: 7.5 % 2.0 == 1.5
```

`nan` compares false with everything, including itself. Overflow produces
`±inf` instead of an error.

### 11.2 Mixing with int

`int` and `f64` are distinct types; every combination must be explicit:

```text
let x = 2.5
let n = 4
print(x + n)     # error[E0369]: cannot apply operator `+` to types `f64` and `int`
print(x + f64(n))# ok: 6.5
print(x * 2.0)   # ok: 5.0
```

- `f64(x)` converts `int` or `str` to `f64`. The string form accepts
  `[+-]?digits[.digits][e±exp]` and panics otherwise
  (`invalid float literal`).
- `int(x)` converts `f64` by truncating toward zero; `nan` and out-of-range
  values panic.
- Comparisons and `==` require both sides to be `f64` (`E0308`/`E0369`),
  except that `nan == nan` is simply `false`.

### 11.3 Formatting

`print` and `str` use the shortest decimal string that round-trips back to
the same double, always showing a fractional part or an exponent:

```text
2.0      0.1      0.30000000000000004      1e+16
1000000000000000.0     2.5e-07     5e-324     inf     nan
```

Fixed notation holds for magnitudes below `1e16`, scientific notation from
`1e16` up and below `1e-4`. Negative zero prints as `-0.0`.

## 13. Built-ins

| Function | Signature | Notes |
| --- | --- | --- |
| `print(x)` | any -> nil | One argument, newline-terminated. |
| `len(x)` | str/list[T]/dict[K, V] -> int | |
| `str(x)` | any -> str | Debug formatting. |
| `int(x)` | str/int/f64 -> int | Strict parse; panics on invalid digits. `f64` truncates toward zero. |
| `f64(x)` | str/int/f64 -> f64 | Conversion or strict parse; see §12. |
| `chr(i)` | int -> str | Panics outside Unicode scalar range. |
| `ord(s)` | str -> int | Requires a one-character string. |
| `push(l, v)` | list[T], T -> list[T] | Appends in place. |
| `pop(l)` | list[T] -> T | Panics on an empty list. |
| `split(s, sep)` | str, str -> list[str] | |
| `substr(s, start, len)` | str, int, int -> str | Offsets are clamped. |
| `read_file(p)` | str -> str | Fatal error if unreadable. |
| `write_file(p, s)` | str, str -> nil | Fatal error if unwritable. |
| `args()` | -> list[str] | Arguments after `--`; program arguments in a native binary. |
| `fatal(msg)` | str -> never | Prints `msg` to stderr, exit code 1. |
| `exit(code)` | int -> never | Terminates with `code`. |
| `abs(x)` | int -> int, f64 -> f64 | Absolute value. |
| `sqrt(x)` | f64 -> f64 | `sqrt` of a negative is `nan`. |
| `floor(x)` / `ceil(x)` | f64 -> f64 | |
| `round(x)` | f64 -> f64 | Half away from zero. |
| `pow(a, b)` | f64, f64 -> f64 | C `pow`. |
| `range(...)` | int -> iterable | Only in `for`. |
| `alloc(n, fill)` | int, T -> ptr[T] | See §9. |
| `arena_new()` | -> Arena | See §9. |
| `arena_alloc(a, n, fill)` | Arena, int, T -> ptr[T] | See §9. |
| `arena_free(a)` | Arena -> int | [unsafe] See §8. |
| `ptr_len(p)` | ptr[T] -> int | See §9. |
| `is_null(p)` | ptr[T] -> bool | See §9. |

`print` / `str` format values as:

```text
42
2.5
1e+16
"quoted\n"
[1, 2, 3]
[1.5, 2.0]
{"a": 1, "b": 2}
Point { x: 1, y: 2 }
<ptr>
<arena>
```

## 14. Panics

Runtime contract violations abort the process with exit code `101` and a
message on stderr:

```text
panic: index out of bounds: the len is 3 but the index is 5
panic: attempt to divide by zero
panic: invalid digit found in string
panic: invalid float literal
panic: cannot parse float from empty string
panic: cannot convert `nan` to `int`
panic: no key `x` in dict
panic: method `bump` takes 1 argument(s) but 2 were given
```

## 15. The strict compiler

`mandela check` performs full static checking and reports every error in one
pass — error code, file, line, column and a caret under the offending token —
then exits with code 1 if any error was found. Warnings (unused variables,
functions that never return) do not fail the build.

| Code | Meaning |
| --- | --- |
| E0061 | Wrong number of arguments |
| E0062 | Struct field specified more than once in a literal |
| E0063 | Missing field(s) in a struct initializer |
| E0124 | Struct field declared more than once |
| E0133 | Unsafe operation (pointer indexing, `arena_free`) outside an `unsafe` block |
| E0252 | The name `x` is defined multiple times (import binding collision) |
| E0267 | `continue` outside of a loop |
| E0268 | `break` outside of a loop |
| E0277 | Type is not an iterator / not indexable |
| E0282 | Type annotations needed (empty list or dict literal, ambiguous `alloc` fill) |
| E0308 | Mismatched types |
| E0369 | Operator not applicable to these types |
| E0384 | Assignment to immutable variable (including `v.f` and `self.f`) |
| E0412 | Unknown type name |
| E0424 | `self` parameter misplaced (missing in a method, or in a plain function) |
| E0425 | Cannot find value or function in this scope |
| E0432 | Cannot find module / circular import |
| E0433 | Could not find `name` in module |
| E0428 | Name defined multiple times |
| E0560 | Struct has no field named (in a literal) |
| E0572 | `return` outside of a function |
| E0592 | Duplicate method definition |
| E0594 | Assignment to a string index |
| E0596 | Mutating method called on an immutable binding |
| E0599 | No method named found for type/struct |
| E0609 | No field named on type |
| E0618 | Call of a non-function value |

Syntax errors carry no code:

```text
error: expected `(`, found identifier `x`
  --> demo.mnd:1:9
  |
1 | def foo x
  |         ^
```

## 16. The toolchain and code generation

`mandela build program.mnd` produces a native executable:

1. **Check** — the self-hosted compiler front-end (`src/`: `lexer.mnd`,
   `parser.mnd`, `checker.mnd`, wired together by `main.mnd` over the module
   system) validates the program. Import graphs are resolved by the checker;
   every imported module is checked once.
2. **Lower** — `bootstrap/codegen.py` lowers the AST to a single self-contained
   C source file. The runtime is a small set of C structs: a tagged
   `MndValue`, `MndStr`, `MndList`, `MndStruct` and an open-addressed
   insertion-ordered `MndDict`. User structs become definition tables (field
   names + method table); methods become plain functions dispatched by name.
3. **Assemble** — the system C toolchain (`cc`, honoured via `$CC`) compiles
   the intermediate representation to native machine code with `-O2`.

`--emit c` writes the intermediate representation as `.c` source, `--emit asm`
writes the assembly listing, `--emit bin` (default) writes the executable.
`mandela run` executes programs on the bootstrap interpreter after the same
check; the two backends are kept output-identical by the test suite.

Modules in v0.7 are a front-end and interpreter feature: `mandela run` and
`mandela check` resolve and verify the full import graph, while the C and
native backends still compile a single file and reject programs with imports
with a clear error. Multi-unit code generation is planned for v0.8.

Memory model in v0.5: runtime objects (strings, lists, dicts, structs) are
allocated from a process-wide arena and live for the duration of the process.
Bulk buffers are explicit: `alloc`/`arena_alloc` hand out pointer views over
arena slots, and `arena_free` releases a whole region at once. Individual
objects cannot be freed one by one; there is no garbage collector.
`bootstrap/mandela.py` (the bootstrap engine) remains only as a temporary
host until the toolchain compiles itself.
