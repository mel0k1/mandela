# The Mandela Language Specification — v0.2.0

Status: draft, implemented by the self-hosted compiler in `src/`.

Mandela is a statically typed, indentation-based language. The syntax follows
Python; the discipline follows Rust; the integer semantics follow C.

## 1. Lexical structure

### 1.1 Source files

Source files are UTF-8 text with the `.mnd` extension. Statements are separated
by newlines; blocks are defined by indentation (4 spaces per level is
conventional, any consistent width is accepted). Tabs count as 4 columns.
Blank lines and comment-only lines carry no indentation significance.

### 1.2 Comments

A comment starts with `#` and extends to the end of the line. There are no
block comments and no docstrings in v0.1.

### 1.3 Identifiers and keywords

Identifiers match `[A-Za-z_][A-Za-z0-9_]*`. The reserved words are:

```text
def  return  if  elif  else  while  for  in  let  mut  struct
true  false  nil  and  or  not  break  continue
```

### 1.4 Operators and punctuation

```text
+  -  *  /  %  =  ==  !=  <  <=  >  >=  ->  (  )  [  ]  {  }  ,  :  .
```

### 1.5 Literals

- Integer: `[0-9]+`, signed 64-bit.
- String: `"..."` with escapes `\n \t \r \0 \" \\`. Strings do not span lines.
- Booleans: `true`, `false`. Unit: `nil`.

## 2. Types

| Type | Description |
| --- | --- |
| `int` | Signed 64-bit integer. Division `/` and remainder `%` truncate toward zero (C semantics). Overflow behaviour is unspecified in v0.1. |
| `str` | Immutable Unicode string. Indexing yields a one-character string. |
| `bool` | `true` or `false`. Conditions must be `bool`; there is no truthiness coercion. |
| `nil` | The unit-ish value `nil`, returned by functions without `->`. |
| `list[T]` | Growable array of `T`. Empty literals require an annotation: `let a: list[int] = []`. |
| `struct` | Nominal record type defined in source; see §5. |

There is no implicit conversion between any two types.

## 3. Declarations

```text
let x = 10            # immutable, type inferred
let mut x = 10        # mutable
let x: int = 10       # explicit annotation
let a: list[int] = [] # empty list needs an annotation
```

Every variable must be declared with `let` before assignment. Re-declaring a
name with a new `let` shadows it (Rust-style). Assignment to a name declared
without `mut` is an error (`E0384`). Loop variables and parameters are
immutable.

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
- Struct types compose with lists: `list[Point]`.
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
- A method that mutates `self` declares `mut self` (like Rust's `&mut self`).
  Inside it, assigning to `self.field` is allowed.
- Calling a `mut self` method on an immutable binding is `E0596`; a non-`mut`
  method cannot assign to `self.field` (`E0384`).
- Methods may call other methods through `self`; they may also call ordinary
  functions. Ordinary functions cannot be called as methods and vice versa.

## 6. Statements

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
```

- `range(a)`, `range(a, b)`, `range(a, b, step)` is allowed only as the `for`
  iterable (`E0308` elsewhere). `step` must keep the loop finite in v0.1.
- `break` / `continue` are valid only inside loops (`E0268` / `E0267`).
- `return` at top level is `E0572`.
- Single-line suites are allowed: `if x == 1: print("one")`.

## 7. Expressions

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

- `+` adds integers or concatenates strings; mixing is `E0369`.
- `/` and `%` truncate toward zero and panic on a zero divisor.
- `==`/`!=` require the same type on both sides (`E0308`).
- Ordering comparisons require two ints or two strings (`E0369`).
- Indexing out of bounds panics at runtime:
  `panic: index out of bounds: the len is 3 but the index is 5`.
- Strings are immutable: `s[0] = "x"` is `E0594`.
- List literals `[1, 2, 3]` must have one element type (`E0308`).
- Trailing commas are allowed in call arguments, list literals and struct
  literals.

## 8. Built-ins

| Function | Signature | Notes |
| --- | --- | --- |
| `print(x)` | any -> nil | One argument, newline-terminated. |
| `len(x)` | str/list[T] -> int | |
| `str(x)` | any -> str | Debug formatting. |
| `int(x)` | str/int -> int | Strict parse; panics on invalid digits. |
| `chr(i)` | int -> str | Panics outside Unicode scalar range. |
| `ord(s)` | str -> int | Requires a one-character string. |
| `push(l, v)` | list[T], T -> list[T] | Appends in place. |
| `pop(l)` | list[T] -> T | Panics on an empty list. |
| `split(s, sep)` | str, str -> list[str] | |
| `substr(s, start, len)` | str, int, int -> str | Offsets are clamped. |
| `read_file(p)` | str -> str | Fatal error if unreadable. |
| `write_file(p, s)` | str, str -> nil | Fatal error if unwritable. |
| `args()` | -> list[str] | Arguments after `--`. |
| `fatal(msg)` | str -> never | Prints `msg` to stderr, exit code 1. |
| `exit(code)` | int -> never | Terminates with `code`. |
| `range(...)` | int -> iterable | Only in `for`. |

`print` / `str` format a struct value as `Point { x: 1, y: 2 }`.

## 9. Panics

Runtime contract violations abort the process with exit code `101` (like Rust)
and a message on stderr:

```text
panic: index out of bounds: the len is 3 but the index is 5
panic: attempt to divide by zero
panic: invalid digit found in string
```

## 10. The strict compiler

`mandela check` performs full static checking and reports every error in one
pass, rustc-style, then exits with code 1 if any error was found. Warning
(unused variables, functions that never return) do not fail the build.

| Code | Meaning |
| --- | --- |
| E0061 | Wrong number of arguments |
| E0062 | Struct field specified more than once in a literal |
| E0063 | Missing field(s) in a struct initializer |
| E0124 | Struct field declared more than once |
| E0267 | `continue` outside of a loop |
| E0268 | `break` outside of a loop |
| E0277 | Type is not an iterator / not indexable |
| E0282 | Type annotations needed (empty list literal) |
| E0308 | Mismatched types |
| E0369 | Operator not applicable to these types |
| E0384 | Assignment to immutable variable (including `v.f` and `self.f`) |
| E0412 | Unknown type name |
| E0424 | `self` parameter misplaced (missing in a method, or in a plain function) |
| E0425 | Cannot find value or function in this scope |
| E0428 | Name defined multiple times |
| E0560 | Struct has no field named (in a literal) |
| E0572 | `return` outside of a function |
| E0592 | Duplicate method definition |
| E0594 | Assignment to a string index |
| E0596 | `mut self` method called on an immutable binding |
| E0599 | No method named found for type/struct |
| E0609 | No field named on type |
| E0618 | Call of a non-function value |

Syntax errors carry no code, mirroring rustc:

```text
error: expected `(`, found identifier `x`
  --> demo.mnd:1:9
  |
1 | def foo x
  |         ^
```

## 11. Execution model

The reference pipeline is:

1. `bootstrap/mandela.py` — a minimal interpreter (Python) able to run the
   toolchain itself.
2. `src/*.mnd` — the compiler front-end written in Mandela: `lex`, `parse`,
   `check_program`.
3. `bin/mandela run` — runs the self-hosted checker first; a clean program is
   then executed by the bootstrap engine.

Functions are hoisted; top-level statements execute in order. The bootstrap
engine is scaffolding and will be replaced by native code generation (roadmap
v0.2).
