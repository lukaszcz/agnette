# Control Flow

[← Index](index.md)

Control flow in AgL is expression-oriented: every construct produces a
value. `if` with `else` and `case` have the common type of their branches;
loops and else-less `if` have type `unit` and evaluate to `()`. Exception
control flow — `try`, `catch`, `raise` — is covered in
[Exceptions](exceptions.md).

## `if`

```ebnf
if_expr        ::= "if" "|"? if_cond_branch ("|" if_cond_branch)* if_else_branch?
if_cond_branch ::= or_expr "=>" branch_body
if_else_branch ::= "|"? "else" "=>" branch_body
```

An optional `|` after `if` lets every branch, the first included, start with
`|`. The `else` branch, if present, is last; its own `|` is optional:

```agl
enum Status
  | Complete
  | Blocked(reason: text)

program def main(status: Status, score: int) -> unit =
  if status is Complete =>
    print "done"
  else =>
    print status

  let label: text = if | score > 90 => "A" | score > 75 => "B" | else => "C"
  print(if score > 50 => "passed" else => "failed")
```

1. Conditions are evaluated left to right; each must be `bool`.
2. The first true condition's body runs in a fresh branch scope.
3. With no true condition, `else` runs if present; otherwise the `if`
   returns `()`.

With `else`, the `if` has the common type `T` of its branches (after
`int → decimal` widening). Without `else`, it has type `unit` and every branch
body must have type `unit` — an else-less `if` is for effects:

```agl
program def main() -> unit =
  let res = exec "make test"
  if res.exit-code != 0 =>
    print "command failed: %{res.stderr}"
```

A branch body is a suite (indented block) or an inline body. An inline body
after `=>` is exactly one item: an `or_expr`, an inline assignment
(`assign_target := or_expr`), `raise`, or `return` — not a `;` sequence, a
binder, or an open `case`, `if`, `try`, or loop. Write those as a suite or
parenthesize them (`(let x = e; body)`); see [Inline
bodies](grammar.md#inline-bodies). A `:=` to an outer `var` inside a branch
persists after the `if`.

## `case`

```ebnf
case_expr       ::= "case" or_expr "of" case_body
case_body       ::= case_branch_seq
                  | NEWLINE INDENT case_branch_seq NEWLINE? DEDENT
case_branch_seq ::= "|"? case_branch ("|" case_branch)*
case_branch     ::= pattern "=>" branch_body
```

The branch list either follows `of` on the same line or forms an indented
block below it. The first branch's `|` is optional in both forms; every later
branch starts with `|`. Branch bodies take the same forms as `if` branch
bodies.

```agl
enum Action
  | Stop
  | Continue(prompt: text)
  | Escalate(reason: text)

def next-prompt(action: Action) -> text =
  case action of
    | Stop => "Stop."
    | Continue(prompt) => prompt
    | Escalate(reason) => "Investigate:\n%{reason}"
```

The scrutinee is evaluated once; patterns are tried in order, and the first
match's body runs in a fresh branch scope with the pattern's bindings. The arms
must cover every possible scrutinee value, and no arm may be fully covered by
earlier arms; either violation is a static error ([Pattern
matching](pattern-matching.md)). The `case` has the common type of its branch
bodies (after `int → decimal` widening), and an outer expected type
propagates into every branch.

`case` and `if` are the loosest expression forms: where a following `|` would
be ambiguous — a branch body, an `if` or `until` condition — they must be
parenthesized.

## Loops

AgL has one loop construct covering every loop shape — `for`, `while`, `do`,
a `[n]` bound, and `until`/`done` termination — plus `break`/`continue`:

```ebnf
loop         ::= for_clause? while_clause? "do" loop_bound?
                 (suite loop_end? | marked_body loop_end)
for_clause   ::= "for" name "in" or_expr range_tail? NEWLINE?
range_tail   ::= ("to" | "downto") or_expr ("step" or_expr)?
while_clause ::= "while" or_expr NEWLINE?
loop_bound   ::= "[" or_expr "]"
loop_end     ::= "until" or_expr | "done"
```

A loop has **at most one `for`** clause and **at most one `while`** clause, in
that order, so a `while` guard can read the `for` variable. `do` is always
present and may carry an iteration **bound** `[n]`. The body is an indented
suite or an inline `;`-separated sequence
([`marked_body`](grammar.md#inline-bodies)), ended by `until E`, `done`, or —
in the suite form only — nothing. `done` and an omitted terminator mean
`until false`.

A loop body is the one inline position that admits `case`, `if`, `try`, and a
nested loop directly: the terminator closes the body, so no open form can
extend past it. In `do … until p until q` the inner `until` binds first. The
`until` or `done` terminator may start its own line aligned with `do`
([Layout](lexical-structure.md#layout-indentation-newlines-continuation)).

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

def process(item: text) -> unit = print item

program def main(items: array[text], n: int, target: int, artifact: text) -> unit =
  # collection iteration, terminated by `done`
  for x in items do
    process(x)
  done

  # inclusive integer ranges with an optional stride
  for i in 1 to n do print i done
  for i in n downto 1 step 2 do print i done

  # a while guard with a bound as a safety limit
  var count: int = 0
  while count < target do[1000]
    count := count + 1
  done

  # no for/while clause: the body runs before the first `until` test
  var review: Review = ask "Review %{artifact}"
  do[5]
    case review of
      | Fail(issues) => review := ask "Fix %{issues}"
      | Pass => break
  until review is Pass
```

Every loop has type **`unit`** and returns `()`. Its body is a discarded-value
position on every iteration, so a bare body value must have type `unit` (or
bottom). A final `let` or `var` in the body is unit-valued and may bind a value
for the `until` condition:

```agl
program def main() -> unit =
  var attempts = 0
  do
    attempts := attempts + 1
    let finished = attempts >= 3
  until finished
```

### Clause semantics

**`for` — collection iteration.** `for x in COLLECTION` iterates an
`array[T]` (elements, in order), a `dict[K, V]` (keys, in insertion order), or
a `text` (each character as a length-1 `text`). A dict key is bound as
originally inserted (see [Destructive
assignment](bindings-and-scope.md#destructive-assignment)). `COLLECTION` is
evaluated once, at loop entry.

For an `array`, the loop iterates that same mutable value and takes its length
at entry as an upper bound. Each iteration reads the element at the cursor's
current index from the live array, so assigning to an element the cursor has
not yet reached — through any alias, including from the body — changes what
`x` is bound to there. Appending or inserting cannot raise the entry-time
bound; removing elements or clearing the array ends iteration when the cursor
reaches the live end. Insertions and removals before the cursor shift live
indices as usual and can make an element be visited again or skipped. For a
`dict`, `x` ranges over the key sequence fixed at entry; indexed assignment
cannot change the key set, but a `d[k]` read in the body sees any value updated
during the loop.

**`for` — integer range.** `for i in a to b` runs `i = a, a+1, …, b`;
`for i in a downto b` runs `i = a, a-1, …, b` (both inclusive). An optional
`step k` sets a positive `int` stride; the default is `1`. `a`, `b`, and `k`
are each evaluated once at loop entry, in that order. A degenerate range
(`a > b` for `to`, `a < b` for `downto`) runs the body zero times. A
non-positive step raises **`RangeError`** at loop entry.

**`while E`.** `E` (`bool`) is evaluated before each body execution; if false,
the loop exits.

**`[n]` bound.** An `int` expression evaluated once at loop entry, in the
enclosing scope, that limits **body executions**: starting an execution beyond
the `n`-th raises **`MaxIterationsExceeded`**. If `n ≤ 0` the body runs zero
times and the loop completes normally. Without a bound, the loop has no
iteration limit.

**`until E`.** `E` (`bool`) is evaluated after each body execution, in the
iteration scope; if true, the loop exits. A loop with no `for` or `while`
clause therefore runs its body at least once, unless its bound is `≤ 0`.
Because `done` means `until false`, a `do[n] … done` loop with no other exit
runs its body `n` times and then raises `MaxIterationsExceeded`.

A loop exits without an exception on `for` exhaustion, a false `while`, a true
`until`, or `break`. A `return` inside a loop instead returns from the
enclosing function.

### Iteration order and scope

Each iteration opens a fresh scope. The `for` variable is a fresh
**immutable** binding per iteration (assigning it is a static error), in scope
in the `while` guard, the body, and the `until` condition — not in the `for`
collection or range expressions, the `[n]` bound, or after the loop. Those
entry expressions are evaluated once in the enclosing scope, so the bound can
see no body binding, and mutating a `var` it read does not change it.

The `while` guard runs before the body and cannot read body bindings. The
`until` condition is evaluated in the iteration's scope and sees the body's
`let`/`var` bindings, including a final binder; body bindings do not survive
to the next iteration or past the loop. Persistent state lives in an outer
`var` mutated with `:=`.

### `break` and `continue`

`break` exits the innermost enclosing loop, which then returns `()`.
`continue` starts the next iteration: from the body or `until` condition it
skips the rest of the current iteration (including the `until` test when it
occurs in the body); from a `while` guard it starts over without entering the
body. The next iteration performs its normal `for` advance and loop checks.
Both are nullary expressions of the **bottom type**, usable in any value
position:

```agl
enum Item
  | Skip
  | Stop
  | Task(name: text)

program def main(items: array[Item], finished: bool, count: int) -> unit =
  for x in items do
    if x is Skip => continue
    if x is Stop => break
    let v: int = if finished => break else => count   # bottom in a typed branch
    print x
  done
```

`break` and `continue` are valid anywhere in a loop's **interior** — its
`while` guard, body, or `until` condition — when the loop is in the same
function or lambda. Outside any loop, or across a `def` or lambda (`fn`)
boundary into an enclosing loop, they are a static error. `break` inside a
`try` body exits the loop; `catch` does not intercept it.

A `return` inside a loop is not a `break`: it returns from the nearest
enclosing `def` or `fn`, abandoning every enclosing loop without a loop
result.
