# Strings and Interpolation

[← Index](index.md)

Every string literal in AgL is a **template**: a sequence of literal text
fragments, `%{…}` expression holes, and `${…}` environment holes. A template
evaluates to `text`. The quoted forms, triple-quoted dedent, and the `$`
literal's payload are lexical
([Strings and templates](lexical-structure.md#strings-and-templates)); this
chapter specifies escapes, holes, and rendering.

## Interpolation

An expression hole `%{expr}` holds any expression, `case` and `if` included
(`"%{if n == 1 => "one" else => "many"}"`). Its value is converted to text by
the [uniform rendering rules](#uniform-rendering-rules), the same in every
position: an `ask` prompt, a `print` argument, an `exec` command, or any other
template. A percent sign
not followed by `{` is literal; `\%` produces a literal percent sign.

An environment hole `${NAME}` is shorthand for
`%{std/prelude::getenv("NAME")}`, so it needs no import and a local binding
named `getenv` does not affect it. It reads the environment snapshot supplied
to the running program, and a missing variable raises the same exception as
that call. `NAME` is an AgL identifier. Environment holes are available
wherever the prelude is. `\${` writes a literal `${`.

A template whose holes are all constant is itself a [constant
expression](bindings-and-scope.md#constant-expressions), so it may fill a
position that runs no code — an attribute argument, a `builtin var` default, a
root binding initializer. An environment hole reads the environment and so is
never constant.

## Escapes

| Escape | Decodes to |
| ------ | ---------- |
| `\"` | `"` |
| `\'` | `'` |
| `\\` | `\` |
| `\/` | `/` |
| `\b` | backspace |
| `\f` | form feed |
| `\n` | newline |
| `\r` | carriage return |
| `\t` | tab |
| `\%` | `%` |
| `\${` | `${` |
| `\uXXXX` | the Unicode scalar value `U+XXXX` |

Any other escape is a lexical error. `\uXXXX` denotes a Unicode scalar value
from its 4 hex digits. An astral character (outside the Basic Multilingual
Plane) is written as an adjacent pair of `\uXXXX` escapes — a UTF-16 high
surrogate (`\uD800`-`\uDBFF`) immediately followed by a low surrogate
(`\uDC00`-`\uDFFF`), as in JSON — which decodes to the one character the pair
encodes. Any other surrogate escape is a lexical error, since a `text` value
never holds a surrogate code point ([Types](types.md#text)).

## The `$` literal

A **verbatim text literal** (`$` literal) is a template like any other,
evaluating to `text`, but treats its payload differently from a quoted
template:

- Only `%{expr}` holes interpolate, with the same rules and rendering as any
  other template.
- `${NAME}` is **not** an environment hole: it is inert payload text, along
  with `$VAR`, `$(cmd)`, and every other dollar form. Read the environment in
  a `$` literal with `%{getenv("NAME")}`:

  ```agl
  program def main() -> unit =
    let region: text = ask $ Deploy region is %{getenv("REGION")}.
  ```

- The only escape is `\%{`, for a literal `%{`; `#`, `;`, quotes,
  parentheses, and ordinary backslashes are literal payload text. So `\\%{x}`
  is a backslash followed by a literal `%{x}`; to place a backslash directly
  before an interpolation, interpolate it (`%{"\\"}%{subdir}`).

The payload's extent, trimming, block dedent, and placement are lexical rules
([Verbatim literals](lexical-structure.md#verbatim-literals)). Because the
literal runs to the end of its line and
[application never chains](lexical-structure.md#operator-precedence), a `$`
literal is the last thing on its line and supplies at most one argument
directly; pipe the result instead of nesting another call after it:

```agl
program def main() -> unit =
  let dir = "."
  print <| exec::[text] $ printf '%s' %{dir}
```

[Shell execution](shell-execution.md) and [Agent calls](agent-calls.md) show
it supplying `exec`'s and `ask`'s single argument, including on a method
receiver (`reviewer.ask $ …`) and with explicit type arguments
(`ask::[T] $ …`).

## Uniform rendering rules

| Value type | Rendered as |
| ---------- | ----------- |
| `text` | verbatim (no quotes); nested inside another value, a quoted AgL string literal |
| `int`, `decimal`, `bool` | plain scalar text |
| `json` | compact, single-line JSON |
| `array[E]` | `[e1, e2, …]` — AgL array syntax |
| `dict[K, V]` | `{k1: value1, k2: value2}` — AgL dict syntax, each key in value syntax: a `text` key quoted (`{"a": 1}`), any other key as it renders (`{1: "one"}`, `{Color::Red: 1}`) |
| record | `TypeName(v1, v2, f3 = value3, …)` — AgL constructor form. Fieldless: bare `TypeName` (no parens), which round-trips as written. |
| enum | inline member: the same constructor form, qualified `TypeName::Member(…)`; fieldless inline member: `TypeName::Member`. A referenced member keeps its record's own form. |
| exception | `TypeName(v1, v2, f3 = value3, …)` — the same constructor form, positional fields spanning the whole `extends` chain (base fields first) |
| function | an opaque handle ([Function types](types.md#function-types)) |

A field renders positionally (bare value) iff it is not named-only and sits at
or before the last positional-only field in declaration order; every other
field renders as `name = value`. Both groups keep declaration order.

Interpolation, `print`, and `as text` render structured values on a **single
line**; `render` is multi-line unless `pretty = false`
([`render`](expressions.md#render) defines its parameters). A nested `text`
value, and a string inside a nested `json` value, is emitted as a quoted AgL
string literal: full JSON escaping plus `\%` for `%` and `\${` for `${`, so it
cannot be misread as interpolation syntax and the output is AgL value syntax. A
`json` value printed or interpolated directly is pure JSON.

- `bool` renders as `true` / `false`.
- `decimal` renders in plain fixed-point notation — never scientific
  notation — with trailing zeros dropped (`1.50` → `1.5`, `1E+2` → `100`).

No boundary tags or other wrappers are added around interpolated values. For
when a rendered value parses back to an equal value, see
[Value syntax](host-environment.md#value-syntax).

### Container literals in interpolation

A non-empty array or dict literal written directly in a hole is checked as
`array[json]` or `dict[text, json]`, respectively, and a container literal
nested inside it as `json`; it keeps AgL container rendering. Cast with
`as json` when JSON rendering is required:

```agl
record R
  x: int

program def main() -> unit =
  let r = R(x = 1)
  print "%{r}"                              # R(x = 1)
  print "%{r as json}"                      # {"x": 1}
  print "%{[r as json, null]}"              # [{"x": 1}, null]
```

## Templates in `exec` commands

`exec` templates use the same rendering. Interpolated values are inserted
**verbatim** into the command string — there is **no automatic shell
quoting**; see [Shell execution](shell-execution.md).

## Errors

Unterminated strings and holes, unknown or unpaired-surrogate escapes, a
newline inside `%{…}`, and an empty or bracketed `$` literal are lexical
errors ([Lexical structure](lexical-structure.md)).
