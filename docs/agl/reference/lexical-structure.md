# Lexical Structure

[← Index](index.md)

## Source text

AgL source is Unicode text: a surrogate code point (U+D800-U+DFFF) anywhere
in the source is a lexical error. Line endings are normalized before
scanning: every `\r\n` and every lone `\r` is treated as a single `\n`.
Source locations (lines and columns) are 1-based.

## Comments

A `#` that does not continue an identifier begins a comment that runs to the
end of the line (`a#b` is one [identifier](#identifiers)). There are no block
comments.

```agl
# This is a comment
let x = 1   # so is this
```

## Layout: indentation, newlines, continuation

AgL uses significant indentation (Python-style). Newlines separate items in
a block; an increase in indentation opens a nested block and a decrease
closes it. A semicolon `;` also separates items; see
[Program structure](program-structure.md).

The layout rules:

1. **Indentation width.** Leading spaces count 1 column each; a tab advances
   to the next multiple of 4 columns. A dedent must return to an active prior
   indentation level — a misaligned dedent is a lexical error.
2. **Blank lines and comment-only lines** are ignored for layout purposes.
3. **Implicit continuation inside brackets.** While any `(`, `[`, or `{` is
   open, newlines do not terminate the item; the logical line continues until
   the bracket closes, so argument lists and container literals may span
   lines. An interpolation hole is not a bracket: a newline anywhere inside
   `%{…}`, even within brackets there, is a lexical error.
4. **Continuation markers.** None of `|`, `else`, `catch`, `until`, `done`,
   `->`, `=>`, and `=` can begin a construct, so a line whose first token is
   one of them always continues the line before it rather than starting a new
   item, and may sit at any column — including the enclosing construct's —
   without opening a new block.

   This is what lets `if`/`case` branches, `else` branches, `catch` clauses,
   enum members, and the `until`/`done` terminator of a loop align with the
   construct that owns them:

   ```agl
   enum Status
     | Complete
     | Blocked
     | Running

   enum Review
     | Pass
     | Fail(issues: array[text])

   def report(status: Status, critic: Agent) -> unit =
     if
       | status is Complete => ()
       | status is Blocked => (let reason: text = critic.ask("Explain %{status}"); print reason)
       | else => ()

   def review(artifact: text, reviewer: Agent) -> Review =
     var r: Review = Pass
     do[5]
       r := reviewer.ask("Review %{artifact}")
     until r is Pass
     r
   ```

   The same rule lets a long signature or binding wrap before its return type,
   its `=`, or a branch arrow:

   ```agl
   def describe(status: text)
       -> text
       = case status of
         | "ok"
           => "all clear"
         | _
           => "needs attention"

   program def main()
       -> unit
       = print (describe "ok")
   ```
5. **Operator continuation.** A line break is ignored wherever it would
   separate an operator from its operand. This holds for every operator —
   built-in, word (`and`, `or`, `not`, `is`, `in`, `to`, `downto`, `step`,
   `with`, `as`), and user-defined — as well as for `.`, `:=`, and a
   signature's `->`.

   A line ending with an operator that still awaits its operand is unfinished,
   so the next line continues it whatever its indentation:

   ```agl
   program def main() -> unit =
     let scaled = [1, 2, 3].map(fn v => v * 2) |>
     .fold(0, fn (total, v) => total + v)
     let wide = 1 +
         2
     print (scaled + wide)
   ```

   A line that is **more indented** than its block and opens with an operator
   continues the line before it instead of opening one:

   ```agl
   program def main() -> unit =
     let report = [1, 2, 3]
       .select(fn v => v > 1)
       .map(fn v => v * 2)
       |> .fold(0, fn (total, v) => total + v)
     print report
   ```

   The indentation is what makes the second form a continuation. At its block's
   own level a line opening with `.`, `-`, or `not` is an item in its own right,
   so a block ending in a leading-dot method value or a negation keeps its
   meaning:

   ```agl
   record Box
     value: int

   def Box::scale(self, factor: int) -> Box = Box(value = self.value * factor)

   def doubler() -> (Box) -> Box =
     let factor = 2
     .scale(factor)

   program def main() -> unit = print (doubler()(Box(value = 3)).value)
   ```

   Neither form applies to a module header, which spells a path rather than an
   expression.

   `:=` is the one continuation operator that also opens an indented suite
   ([Bindings and scope](bindings-and-scope.md#suite-initializers)), so a line
   more indented than a trailing `:=` opens the assignment's block rather than
   continuing its line.

## Keywords

The following words are **always reserved** and can never be used as names:

```text
record enum type program def fn let var for while do until done
if else case of try catch raise return break continue exception extends builtin extern as as?
true false null
infixl infixr prio
```

`as?` is a single token: no whitespace may separate `as` and `?`. Written
with whitespace, `as` is the cast keyword and `?` a separate placeholder
token.

Every other word is a name, though some have a special role in
particular positions:

- **Built-in call names.** `print`, `render`, `ask`, `ask-request`, `exec`,
  `copy`, `shallow-copy`, `resource`, and `resource-dir` name the built-in
  functions. None may be declared as a `let`/`var` binding, a function or
  lambda parameter, a `for` variable, a pattern binder, or a `catch` binder,
  nor be the bare name of a value an `import` or `use` renames or selects
  ([`use` declarations](modules.md#use-declarations)).
  In a module without a path identity
  ([Modules](modules.md#slash-path-identity)), none may name a root
  function, record, exception, or constructor-bearing alias either, since
  such a declaration is reachable only bare. Elsewhere — in a named scope or
  a module with a path identity — a function or constructor, including an
  enum member, may take the name but is reached only qualified (`Mode::exec`);
  the bare spelling always means the built-in. Each stays legal as a field or
  method name.
- **`self`** is special only as the first parameter of a `def` whose
  enclosing path resolves to a record, enum, exception, or built-in receiver
  type, including a foreign receiver name made bare-visible by an import or
  `use` ([Methods](functions.md#methods)). Elsewhere it is an ordinary name,
  including as a field name or an annotated function parameter.
- **Built-in type names** — `text`, `json`, `bool`, `int`, `decimal`,
  `array`, `dict`, and `unit` — denote the built-in types in type positions.
- **`Eq` and `Hashable`** are the constraint names inside a constraint block
  (`{Eq T}`, `{Hashable K}`); see
  [Constraint blocks](generics.md#constraint-blocks).
- **`at`** introduces the priority of an infix declaration.
- **`_`** is the wildcard in pattern and `catch` positions
  ([Pattern matching](pattern-matching.md)).

### Soft keywords

Soft keywords are promoted to keywords only inside a promotion window, and
are valid names everywhere else.

**Module and scope soft keywords:**

| Keyword | Window |
|---------|--------|
| `import` | At item-start |
| `use` | At item-start when followed by a use declaration form |
| `export` | At item-start |
| `hiding` | Within an import, use, or export declaration |
| `scope` | At item-start, before a complete `NAME (:: NAME)*` scope path |
| `end` | At a region's layout level, while that region is open, before a complete closer path ending the item |

A scope closer must repeat exactly the path of the region it closes. At other
layout levels `end` is a name; for example it can be a record field or the
first expression in a declaration suite.

**Operator soft keywords** — `and`, `or`, `not`, `is`, `in`, `to`, `downto`,
`step`, and `with` spell the built-in operators. An operator's spelling only
has to be unambiguous where an operand could stand instead, so each is promoted
in operator position alone:

| Keywords | Window |
|----------|--------|
| `and` `or` `is` `in` `to` `downto` `step` `with` | Directly after a token that closes an operand |
| `not` | Directly before a token that can start an operand |

A following `=` or `:` marks a name either way — no operand starts with
either — and after `.`, `::` or a declaration keyword only a name can stand.
So `Option::or`, `flag.not()`, `R(step = 2)` and a `with: int` field all name
members the way any other spelling does, while `a or b`, `not ready` and
`1 to 9 step 2` read as operators. The one position an operator word cannot
reach is an enum variant name, where a case branch may legitimately begin with
`not`.

```agl
let import = 1          # 'import' not at item-start: a name
let export = "hello"    # 'export' not at item-start: a name
let use = "hello"       # 'use' without a declaration form: a name
record R
  end: int              # 'end' is a field name, not a closer
```

## Qualifier chains

`::` separates qualifier-chain segments from the member they select. A chain
can begin with a module route, continue through named scopes or types, and end
at a value, type, or enum member:

<!-- agl-check: fragment -->
```agl
foo/bar::thing              # suffix module route, member thing
/foo/bar::thing             # anchored module route
Point::distance             # local scope member
foo/bar::Geometry::Point    # module route, then scope members
::Outer::Inner::name        # path anchored at the current module root
```

A slash-separated path before the first `::` is a module route. A leading `/`
anchors that route to the complete module path; otherwise it may be a suffix
route or an alias. A leading `::` anchors the chain at the current module's
own root. Subsequent `::` segments name scopes or types. How a chain is looked
up: [Suffix and anchored references](modules.md#suffix-and-anchored-references)
and [Names and visibility](scopes.md#names-and-visibility).

Every route and chain segment is byte-adjacent through `::`: `foo/bar::thing`
is a qualifier, while `foo / bar::thing` is division followed by a separate
qualifier. `/` is the division operator only when written with whitespace on
**both** sides. This matches `+`, `-` and `*`, which are identifier characters
and so already need surrounding space to read as operators (`a+b` is one
name). Left unspaced, `/` separates route segments, so a `/` that touches an
operand on exactly one side is rejected:

<!-- agl-check: error -->
```agl
let q = a / b        # division
let r = a/b::thing   # qualifier
let s = a/ b         # error: reads as a path, but the segments are split
let t = a /b         # error: same
```

A chain segment may carry type arguments, as in `Option[int]::Some`, only when
its full path selects a generic type of matching arity and the next segment
selects one of that type's inline members (a record's own name counts:
`Box[int]::Box`). The `[` is byte-adjacent to the segment name. Type arguments
on any other segment — a plain scope, a non-generic type, or a type followed by
another declaration beneath it — are a static error. The type-argument form
`callee::[T]` and typed-call form `callee::[T](args)` (e.g. `ask::[Review](…)`)
instead apply to the complete callee and are not qualifier segments.

## Identifiers

An identifier starts with a letter (any Unicode letter, not just ASCII) or
`_`, and then continues for as long as the next character is **not** whitespace
and **not** one of the delimiter characters:

```
(  )  [  ]  {  }  :  ,  .  |  ;  /  @  =
```

Every other character continues an identifier. In particular the quotes `"`
and `'`, `#`, and the operator characters `+`, `*`, `-`, `?`, `!`, `$`, `<`,
`>` may appear *inside* an identifier, so `ask-prompt`, `valid?`, `exec$`,
`do-it-now!`, `a+b`, `a#b`, and `foo"bar` each scan as a single name. A leading `"`
or `'` (or one preceded by whitespace) still starts a string template, since an
identifier must begin with a letter or `_`. `=` and `@` **are** delimiters, so
`a=b` scans as three tokens and `@arg-pos` as two — the attribute name after
`@` is one identifier, hyphens included.

**Operator names** (`OP_NAME`) are the second lexical class of identifier.
They start with an operator-name character and continue while the next
character is also one. Operator-name characters are Unicode punctuation or
symbol characters, except the structural delimiters: parentheses, brackets,
braces, `:`, `,`, `.`, `;`, quotes, `@`, `#`, and `_`. A run that spells a
reserved operator or punctuation token (`=`, `==`, `!=`, `<`, `<=`, `>`, `>=`,
`->`, `=>`, `:=`, `::`, `+`, `-`, `*`, `/`, `|`, `?`) is that token instead.
Other runs, such as `==>`, `>>`, `|>`, `<|`, `>=>`, `%$`, `~`, and `⊕`, are
operator names.

A `$` at the start of a token opens a [verbatim literal](#verbatim-literals),
so an operator name never *starts* with `$`; inside or at the end of a run
`$` is an ordinary operator-name character (`<$>`, `<|$`).

The placeholder spellings `?` and `?<digits>` (`?1`, `?12`) are reserved
tokens used only as whole call arguments for partial application
([Functions](functions.md#partial-application)). The digits must be adjacent:
`? 1` is a bare `?` followed by an integer literal. A standalone `?` is
therefore not available as an operator name, while longer runs that contain
`?` (`??`, `?=`, `%?`) are ordinary operator names.

Both classes are ordinary names in declaration and reference positions:

| Token | Start | Used for |
| ----- | ----- | -------- |
| `NAME` | a letter or `_` | Every kind of name: types, constructors, variables, fields, functions, parameters, type parameters |
| `OP_NAME` | an operator-name character | Variables, functions, constructors, and other positions that accept a name |

**Capitalization carries no syntactic or semantic meaning.** `option` and
`Option`, `some` and `Some` are equally valid as type names, value names,
constructors, or functions; what a name denotes depends only on its
declaration and position. Names are still matched exactly: `Some` and `some`
are two distinct names. Type names and value names live in separate
namespaces ([Bindings and scope](bindings-and-scope.md)).

### Operator disambiguation

Scanning is maximal munch: a run continues as far as its class allows, then
reserved spellings are recognized. Use spaces around operators when you want
them read as operators.

| Source | Scans as | |
| ----- | ----- | - |
| `ask-prompt` | `ask-prompt` | one identifier |
| `a - b` | `a`, `-`, `b` | spaces break the identifier |
| `a.b` | `a`, `.`, `b` | `.` is a delimiter |
| `a -> b` | `a`, `->`, `b` | arrow |
| `a->b` | `a->b` | one identifier |
| `x == 3` | `x`, `==`, `3` | equality |
| `a+b`, `n*x` | `a+b`, `n*x` | one identifier each |
| `foo"bar` | `foo"bar` | one identifier |
| `valid?` | `valid?` | one identifier |
| `>>`, `\|>`, `%$` | `>>`, `\|>`, `%$` | operator names |
| `?` | `?` | placeholder, not an operator name |
| `?1` | `?1` | numbered placeholder |
| `? 1` | `?`, `1` | placeholder, then an integer |
| `??` | `??` | operator name |
| `ask$`, `a$b` | `ask$`, `a$b` | one identifier each |
| `<$>`, `<\|$` | `<$>`, `<\|$` | operator names |
| `x+$ y` | `x+$`, `y` | one identifier, then another |
| `ask $ x` | `ask`, then a `$` literal | space before `$` opens a `$` literal |
| `$-rf` | a `$` literal, payload `-rf` | token-start `$` |

## Numbers

There are two numeric token forms and **no floating-point tokens**:

| Token | Pattern | Type |
| ----- | ------- | ---- |
| `INT` | `[0-9]+` | `int` (arbitrary precision) |
| `DECIMAL` | `[0-9]+\.[0-9]+` | `decimal` (exact) |

A decimal literal requires digits on both sides of the dot. There is no
exponent notation. Negative numbers are written with the unary minus
operator: `-3` is `-` applied to the literal `3`.

## Strings and templates

Every string literal is a **template**: text fragments plus interpolation
holes. A quoted template supports two kinds of hole, `%{expr}` and `${NAME}`;
a [`$` verbatim literal](#verbatim-literals) is a third template spelling with
only the `%{expr}` hole. Both `"` and `'` are quote delimiters, giving four
quoted forms:

- `"…"` / `'…'` — single-line.
- `"""…"""` / `'''…'''` — triple-quoted, multi-line.

Escapes and the meaning of holes are specified in
[Strings and interpolation](strings-and-interpolation.md).

A triple-quoted template is **dedented**. After its escapes are decoded:

1. one newline directly after the opening quotes is dropped;
2. the margin is the least indentation (spaces and tabs) over its non-blank
   lines, where an interpolation hole counts as non-blank content;
3. every line loses its first *margin* whitespace characters, and holes are
   never altered;
4. one newline directly before the closing quotes is dropped.

```agl
program def main() -> unit =
  let body = """
    Fix these issues:
      - missing tests
  """
  print body    # "Fix these issues:\n  - missing tests"
```

## Verbatim literals

A `$` at the start of a token opens a **verbatim text literal** (`$`
literal) — a third template spelling alongside quoted and triple-quoted
templates — wherever a template may stand (e.g. `receiver.ask $ prompt`).

### Payload

After `$`, leading horizontal whitespace (spaces and tabs) is discarded.
The rest of the line decides the form:

- **Inline.** If the rest contains anything other than a newline, the payload is
  the rest of that line, with trailing horizontal whitespace discarded.
- **Block.** If nothing else follows `$` on its line, the payload is a
  following indented block. The block ends at the first non-blank line
  indented at or below the line holding `$` (or at end of input).

  The dedent margin is the indentation of the block's *first non-blank
  line*. A later non-blank block line indented more than the line holding
  `$` but less than the margin is a lexical error rather than a normal end
  of block — in `$\n    one\n  two`, `two` (indent 2) is more indented than
  the line holding `$` (indent 0) but under the margin `one` set (indent 4),
  so it is rejected. Every block line is dedented by the margin (indentation
  beyond the margin is kept) and the lines are joined with `\n`. Leading and
  interior blank lines are kept as empty lines; blank lines trailing the
  block's last content line are dropped.

A payload that is empty — nothing but whitespace before the line ends
(inline), or a block with no non-blank line — is a lexical error.

### Placement

A `$` literal is not valid directly inside brackets — `(`, `[`, `{`, or a
`%{…}` interpolation hole — so it cannot be a call or constructor argument,
an array or dict element, or the expression inside a hole; use a quoted
template in those positions instead.

### Content

The payload reaches the literal's value verbatim: `#`, `;`, quotes,
parentheses, and every dollar form it contains (`$VAR`, `${NAME}`, `$(cmd)`)
pass through unchanged, never read as AgL syntax. Only two spellings are
recognized inside the payload:

- `%{expr}` — an interpolation hole, on the same terms as any other template
  ([Strings and interpolation](strings-and-interpolation.md)).
- `\%{` — the one escape sequence, for a literal `%{`. Every other backslash
  is literal payload text.

## Operators and punctuation

```text
=>   ->   =   ==   !=   <   <=   >   >=   :=
::   +   -   *   /   @   ?   ?N
(   )   [   ]   {   }
:   ,   .   |   ;
```

Multi-character operators are matched greedily.

`->` is the **function-type and return-type arrow**: `int -> text`,
`def f(x: int) -> text = …`, `fn(x: int) -> text => …`. `=>` is the
**branch and lambda-body arrow**: it separates a condition, pattern, or lambda
head from its body.

`::` is both the [qualifier-chain](#qualifier-chains) separator and the
type-argument introducer `callee::[Type]` / `callee::[Type](args)`. It is one
token, distinct from two `:`. A `::` after a tightly written route or chain
segment separates qualifier segments; a `::` after a complete callee and
directly followed by `[` introduces type arguments.

`==` is equality and `!=` inequality. A single `=` is never a comparison: it
separates a binder, named argument, or field from its value (`let x = …`,
`f(name = …)`, `R(field = …)`). `:=` is destructive assignment.

A `[` that follows `do` — with or without intervening whitespace (`do[n]` and
`do [n]` are equivalent) — opens the loop bound `[expr]`, never an array
literal. A `do` body therefore cannot itself *begin* with a bare array literal;
parenthesize it (`do ([item1, item2]) until …`).

Elsewhere, a `[` adjacent to an expression-ending token starts indexing.
Whitespace keeps the bracket an array literal, so `xs[0]` indexes while
`f [0]` is the single-argument call sugar `f([0])`.

## Attributes

`@` introduces an **attribute** on a declaration — `@name`, or `@name(args)`
with an ordinary call argument list. `@` is a delimiter, so the attribute name
is a separate identifier token and may be hyphenated (`@arg-pos`).

Several attributes may be written in a row. A declaration, a field, and a
parameter take their attributes on the line above or in front of them on the
same line. An enum member takes its attributes after its `|`; its name and
further attributes may continue on indented lines below, with its fields
indented further:

```agl
@doc("Formats one entry.")
def label(@arg-pos count: int, @arg-named key: text) -> text = "%{key}: %{count}"

record Entry
  @arg-named
  label: text
  @arg-named count: int

enum Shape
  | @doc("A rectangle.")
    @arg-pos
    Rect
      width: int
      height: int
  | Empty
```

Placement, catalog, and arguments: [Attributes](attributes.md).

## Operator precedence

From loosest to tightest binding. Infix operators carry an integer
**priority** (larger binds tighter); the levels without one bind tighter than
every infix operator:

| Priority | Operators | Associativity |
| -------- | --------- | ------------- |
| 10 | `or` | left |
| 20 | `and` | left |
| 25 | `not` (prefix) | — |
| 30 | `==` `!=` `<` `<=` `>` `>=` `in` | **non-associative** |
| 40 | `+` `-` | left |
| 50 | `*` `/` | left |
| | `is`, `is not` | non-chaining |
| | `as`, `as?` (casts) | left |
| | `-` (unary prefix) | — |
| | function application `f x` | **non-chaining** |
| | `.name`, `[index]`, `(args)`, `::[T]` (postfix) | left |
| | atoms: literals, names, `( expr )`, `(op)` operator values, `()`, templates, `break`, `continue` | — |

[User-defined operators](#user-defined-operators) take any integer priority
and interleave with the numbered rows. The prelude's operators: `++` is infixr
at `+`'s priority, so `"a" ++ b == c` is `("a" ++ b) == c` and `++` does not
mix with `+`/`-` unparenthesized; `|>` (infixl 5) and `<|` (infixr 4) bind
looser than `or`; `>>` (infixl) and `<<` (infixr) sit at 60. `to`, `downto`, and `step` appear only
in a `for` clause ([Control flow](control-flow.md)).

- **`not`** takes as its operand everything that binds at priority 25 or
  tighter: `not a == b` is `not (a == b)`, while `not a and b` is
  `(not a) and b`.
- **Comparisons** do not chain: `x == y == z`, `1 < 2 < 3`, and `a <= b != c`
  are static errors, while a parenthesized comparison is an ordinary operand
  (`(a == b) == c`).
- Operators at one priority cannot mix left and right associativity in a
  chain; parenthesize one side.
- **`is`** binds tighter than every infix operator and looser than a cast:
  `a == e is X` is `a == (e is X)`, and `x as T is P` is `(x as T) is P`. Its
  right side is a constructor name, so it does not chain.
- **Casts** are left-associative (`x as json as text` is
  `(x as json) as text`) and bind looser than unary minus (`-x as json` is
  `(-x) as json`); see [Casts](expressions.md#casts-as-and-as).
- **Application** is the single-argument call sugar (`print x`, `ask "…"`,
  `f val`). It binds tighter than every binary operator: `print x + 1` is
  `(print x) + 1`. It does not chain — `f g x` is a parse error — but a postfix
  call can be the argument, so `f g(x)` is `f(g(x))`. A `-` or `(` after the
  callee is not an argument: `f -1` is subtraction and `f (x)` a parenthesized
  call.
- An operator name after an expression is read as an infix operator, so an
  operator-name value used as an argument is parenthesized: `print(>>)`, not
  `print >>`. A built-in symbolic operator is a value only in parentheses of
  its own, such as `(+)`; see
  [Operators as function values](expressions.md#operators-as-function-values).

The record-update operator `with` binds looser than every level in the table,
on both sides; see [Record update](expressions.md#record-update). `case` and
`if` expressions are looser still. In positions where a following `|` would be
ambiguous (branch bodies, `if`/`until` conditions)
they must be parenthesized. A loop body is not such a position — the loop
terminator closes it — so they may appear there directly
([Inline bodies](grammar.md#inline-bodies)).

### User-defined operators

The prelude's text concatenation `++` (`+` is numeric only) and the standard
library's other operators are ordinary user-declarable infix operators. An
`infixl` or `infixr` declaration gives an operator name its associativity and
priority, beside the function that implements it:

```agl
infixl <+> at 45
infixr <<< at prio <+> + 1

def <+>(a: int, b: int) -> int = a * 10 + b

def <<<(a: int, b: int) -> int = a - b

program def main() -> unit =
  print (1 <+> 2 <+> 3)    # (1 <+> 2) <+> 3 = 123
  print (9 <<< 5 <<< 2)    # 9 <<< (5 <<< 2) = 6
  print (1 <+> 9 <<< 5)    # 1 <+> (9 <<< 5) = 14
```

- A priority is a literal integer (`at 45`) or relative to another operator
  (`at prio Y + n`, `at prio Y - n`); an omitted priority is 40. `Y` is looked
  up at the module root like any operator name, the module's own infix
  declarations first, in any order; it may be a built-in operator. Relative
  priorities may not form a cycle.
- Infix declarations stand at the module root only. A built-in operator's
  fixity cannot be redeclared.
- An operator in a chain is a name: it is looked up at its site exactly like a
  function of that name — lexical bindings first, then the scope steps, own
  declarations before imports, two distinct imported declarations ambiguous —
  and reached through the same imports, `use` declarations, re-exports,
  renames, and aliases. A use is a call of the selected declaration with the
  two operands.
- Its fixity is the one the selected declaration's module declares for that
  name. An `infixl`/`infixr` declaration applies to the module's own
  declarations of the name, at any scope path, and to its `let`/`var` binders
  of the name; it is never exported on its own. A fixity declaration for a
  name the module does not declare is an error, as is an operator use that
  selects a declaration whose module declares no fixity for it.
- In the REPL the session is one module: an operator's definition comes in
  the same entry as its fixity declaration or an earlier one.
