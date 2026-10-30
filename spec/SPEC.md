# The Mandela Language Specification — v0.3.0

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
block comments and no docstrings in v0.3.

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
| `int` | Signed 64-bit integer. Division `/` and remainder `%` truncate toward zero. Overflow wraps in two's complement. |
| `str` | Immutable string. Indexing yields a one-character string. In native builds strings are byte sequences, so string offsets and lengths are reliable for ASCII input in v0.3. |
| `bool` | `true` or `false`. Conditions must be `bool`; there is no truthiness coercion. |
| `nil` | The unit-ish value `nil`, returned by functions without `->`. |
| `list[T]` | Growable array of `T`. Empty literals require an annotation: `let a: list[int] = []`. |
| `dict[K, V]` | Insertion-ordered map from `K` keys to `V` values; see §6. |
| `struct` | Nominal record type defined in source; see §5. |

There is no implicit conversion between any two types.

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

## 6. Dictionaries

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

## 7. Methods on built-in types

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

## 8. Statements

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

## 9. Expressions

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
- `==`/`!=` require the same type on both sides (`E0308`); lists, dicts and
  structs are compared by contents.
- Ordering comparisons require two ints or two strings (`E0369`).
- Indexing out of bounds panics at runtime:
  `panic: index out of bounds: the len is 3 but the index is 5`.
- Strings are immutable: `s[0] = "x"` is `E0594`.
- List literals `[1, 2, 3]` must have one element type (`E0308`).
- Trailing commas are allowed in call arguments, list literals, dict literals
  and struct literals.

## 10. Built-ins

| Function | Signature | Notes |
| --- | --- | --- |
| `print(x)` | any -> nil | One argument, newline-terminated. |
| `len(x)` | str/list[T]/dict[K, V] -> int | |
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
| `args()` | -> list[str] | Arguments after `--`; program arguments in a native binary. |
| `fatal(msg)` | str -> never | Prints `msg` to stderr, exit code 1. |
| `exit(code)` | int -> never | Terminates with `code`. |
| `range(...)` | int -> iterable | Only in `for`. |

`print` / `str` format values as:

```text
42
"quoted\n"
[1, 2, 3]
{"a": 1, "b": 2}
Point { x: 1, y: 2 }
```

## 11. Panics

Runtime contract violations abort the process with exit code `101` and a
message on stderr:

```text
panic: index out of bounds: the len is 3 but the index is 5
panic: attempt to divide by zero
panic: invalid digit found in string
panic: no key `x` in dict
panic: method `bump` takes 1 argument(s) but 2 were given
```

## 12. The strict compiler

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
| E0267 | `continue` outside of a loop |
| E0268 | `break` outside of a loop |
| E0277 | Type is not an iterator / not indexable |
| E0282 | Type annotations needed (empty list or dict literal) |
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

## 13. The toolchain and code generation

`mandela build program.mnd` produces a native executable:

1. **Check** — the self-hosted compiler front-end (`src/*.mnd`: lexer, parser,
   checker) validates the program.
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

Memory model in v0.3: allocations live for the duration of the process; there
is no garbage collector yet. A dedicated allocator is planned before the
bootstrap engine is retired.
