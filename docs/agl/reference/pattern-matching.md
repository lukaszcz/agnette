# Pattern Matching

[← Index](index.md)

Patterns appear in `case` branches ([Control flow](control-flow.md#case)). The
`|` before the first branch is optional; each further branch starts with `|`.
`let` and `var` bind a single name, never a pattern; use `case` to destructure.

## Pattern forms

```ebnf
pattern        ::= pattern_atom ("as" name)*
pattern_atom   ::= "_"                                    (* wildcard *)
                 | literal                                (* literal pattern *)
                 | name                                   (* field-directed binder or bare constructor *)
                 | name "(" pattern_fields? ")"          (* unqualified constructor *)
                 | qualifier_chain name
                     ("(" pattern_fields? ")")?           (* qualified constructor *)
pattern_fields ::= pattern_field ("," pattern_field)* ","?
pattern_field  ::= pattern                                (* positional sub-pattern *)
                 | field_name "=" pattern                 (* named sub-pattern *)
```

`qualifier_chain` is specified in [Lexical
structure](lexical-structure.md#qualifier-chains) and [Grammar](grammar.md#patterns).

### Wildcard `_`

Matches anything and binds nothing. The same spelling is the unreadable
discard binder of `let _ = value`
([Bindings and scope](bindings-and-scope.md#let-immutable-binding)).

```agl
enum FixResult
  | Complete(output: text)
  | Blocked(reason: text)

def output-or-empty(result: FixResult) -> text =
  case result of
    | Complete(output) => output
    | _ => ""
```

### As-pattern binder

`pattern as name` matches `pattern` and also binds `name` to the complete value
matched at that position. It binds less tightly than every other pattern form,
so it wraps the whole preceding pattern. Several `as` binders may be chained;
each receives the same value. An `as` binder is always a variable binder, even
when its spelling is also a constructor name, and `_` is not a valid binder
name.

```agl
enum Shape
  | Rect(w: int, h: int)
  | Circle(r: int)

def describe(shape: Shape) -> text =
  case shape of
    | Rect(w, h) as rectangle => "%{rectangle} has area %{w * h}"
    | _ as other => "other shape: %{other}"
```

### Variable binders

At a `case` branch root, a bare name is never a variable binder: it must denote
a visible fieldless constructor. A root binder is explicit — `_ as name` is the
catch-all binder, and a constructor or literal may be followed by `as name`.
Inside a constructor pattern, a bare name binds a field only when it is that
field's name ([Payload sub-patterns](#payload-sub-patterns)). Binders are
immutable and branch-local. Ordinary value bindings do not affect
constructor lookup in patterns, and capitalization carries no meaning
([Lexical structure](lexical-structure.md)).

### Literal patterns

An `int`, `decimal`, `bool`, `null`, or string literal matches by value
equality (the `==` semantics, so the pattern `1` matches the value `1.0`):

```agl
def greeting(attempt: int) -> text =
  case attempt of
    | 1 => "first try"
    | _ => "retry"
```

Restrictions:

- A pattern string literal cannot contain interpolation.
- There are no negative-number patterns (patterns have no unary minus).
- The literal's type must be comparable with the scrutinee's static type
  (the same type after `int → decimal` widening): an `int` pattern against a
  `text` scrutinee — or any literal other than `null` against a `json`
  scrutinee — is a static error, not a dead branch. A scrutinee whose static
  type is a type parameter `T` (bare, or a field typed `T`) never admits a
  literal pattern, bounded or not ([Constraint
  blocks](generics.md#constraint-blocks)).

### Constructor patterns

A constructor pattern matches a record value, including a member record of an
enum, and optionally destructures its fields. A pattern is a constructor
pattern when it is one of:

- a **bare name at a `case` branch root that denotes a visible constructor** —
  matches a fieldless record only (see below),
- a **call form** `name(…)`, where the parentheses may be empty, or
- a **qualified** `Enum::member`, `Record::Record(…)`,
  `module::Enum::member`, or `module::Record` form.

```agl
enum Review
  | Pass
  | Fail(reason: text)

def summarize(review: Review) -> text =
  case review of
    | Review::Pass => "passed"              # qualified nullary constructor
    | Fail(reason = "stuck") => "blocked"  # nested scalar literal
    | Fail(reason = reason) => reason        # named field binder
```

The first branch could equivalently use bare `Pass` or `Pass()`.

A bare constructor name matches **fieldless** records only — an enum member, a
standalone record, or an alias leading to one. A bare name for a record with
fields is a static error, even when every field has a default (unlike a bare
constructor reference in value position, which constructs with the defaults;
see [Fieldless and all-defaulted constructor
references](expressions.md#fieldless-and-all-defaulted-constructor-references)):
write `Fail()` to ignore every field, named-only fields included, or
destructure them. The call and qualified forms apply to every member record and
standalone record. A record's owner-qualified form repeats its name,
`Record::Record(…)`, and either position may instead name an alias leading to
the record; a pattern spelled through an alias matches the declaration the
alias denotes, at the alias's type arguments ([Type
aliases](types.md#type-aliases)).

The scrutinee's static nominal type selects the constructor among those
visible where the pattern is written — at every [lookup
step](scopes.md#names-and-visibility) there, not only the nearest one with the
spelling, so a same-named member of an enum in a nearer scope never hides the
scrutinee's own member. Candidates constructing one declaration at the
scrutinee's type arguments, such as a member reached through two aliases, are
one candidate. A spelling that no visible constructor of that type shares is a
static error, even when the type has a member of that name — for instance when
only a function returning the type was imported. When two enums contribute the
same unqualified member spelling, or a record constructor spelling collides
with an injected member name, the scrutinee type selects the intended
constructor, so the pattern needs no qualification.

#### Module-qualified constructor patterns

A constructor pattern may carry the same qualifiers as a value-position
constructor:

<!-- agl-check: fragment -->
```agl
import mylib

def color-name(value: mylib::Color) -> text =
  case value of
    | mylib::Color::Red => "red"
    | mylib::Color::Blue => "blue"
```

The prefix may name the owning enum (`Color::Red`), a module and owning type
(`mylib::Color::Red`, `mylib::Point`), the current module (`::Color::Red`,
`::Point`), a module alone (`mylib::Red`, `::Red`; see [Module-qualified enum
members](modules.md#module-qualified-enum-members)), or a named scope
(`Shapes::Point(x)`, `mylib::Shapes::Point(x)`; see [Scopes](scopes.md)). A
module route uses slash segments (`company/colors::Color::Red`); constructor
qualification uses `::`, never `.`. An enum owner qualifies only its inline
members. A qualified spelling is looked up by its whole path, exactly as in
value position ([Qualifier chains](lexical-structure.md#qualifier-chains)).
Qualification is optional when the scrutinee type selects the constructor;
when present, it must identify the scrutinee's exact nominal type. The
right-hand side of `is`/`is not` takes the same qualifiers.

#### Payload sub-patterns

Payload sub-patterns follow the same positional-greedy binding as calls:

- **Positional sub-patterns** fill positional-capable (pos-only/standard) field
  slots left to right, in declaration order.
- **Named sub-patterns** (`field = pattern`) bind a specific field by name; they
  may follow positional sub-patterns.
- **Bare-name shorthand** (`x` in a position where positional slots are
  exhausted) means `x = x`. It is valid only when `x` names a named-only field.

A field's zone is set by its own `@arg-pos`, `@arg-std`, or `@arg-named`
[attribute](attributes.md#zone-attributes), else by one on its declaration,
else standard ([Record types](types.md#record-types)), so the same zones shape
destructuring and construction: a positional-only field is reached only by
position, a named-only field only by a named sub-pattern or the shorthand.

Once a sub-pattern is assigned to a field, a bare name is interpreted by that
field: the field's own name binds the field, while a nullary constructor of the
field's enum type matches that constructor; any other bare name is a static
error. A spelling that is both the field name and a constructor of its type is
ambiguous; write `name()` (or a qualified name) for the constructor and
`_ as name` for the binder. A bare name never moves to another field; use a
named sub-pattern to select it.

```agl
enum Outcome
  | Ok(value: int)
  | Err(reason: text, fatal: bool)

def describe(r: Outcome) -> text =
  case r of
    | Ok(value) => "ok %{value}"                        # binds the field 'value'
    | Err(reason, fatal = true) => "fatal: %{reason}"
    | Err(_ as why, fatal) => "error: %{why} (%{fatal})"  # renamed binder
```

The fully named spelling of `Ok(value)` is `Ok(value = value)`; a binder is
renamed with an as-pattern, as in `Ok(value = _ as v)`.

Named sub-patterns nest arbitrarily — a wildcard, literal, field-directed bare
name, explicit binder, or another constructor pattern. Here the last arm's
binder is required because `int` is an open domain:

```agl
enum Response
  | Complete
  | Failed(code: int)

def describe(response: Response) -> text =
  case response of
    | Complete => "complete"
    | Failed(code = 503) => "unavailable"  # nested scalar literal
    | Failed(code = code) => "error %{code}"  # matched-field binder covers other ints
```

Static rules:

1. A constructor pattern requires a **record- or enum-typed scrutinee**;
   matching one against any other type, including an exception, is a static
   error.
2. An enum-member record must belong to the scrutinee's enum. A record
   constructor must denote the scrutinee's exact record type. Qualifiers and
   type aliases resolve transparently, but module identity and instantiated
   type arguments must agree.
3. Each field may appear at most once in a pattern.
4. Fields not mentioned in the pattern are ignored (patterns need not be
   complete).
5. A name may be bound only once per pattern.
6. A positional sub-pattern must precede all named sub-patterns. A positional
   sub-pattern that lands on a named-only field (no positional slot left) is a
   static error unless it is a bare name (read as `name = name`).
7. Naming a positional-only field in a named sub-pattern is a static error;
   rename its binder with an as-pattern in the field's positional slot instead
   (`Rect(_ as w)`).

### Patterns on generic nominal types

Constructor patterns work on instances of **generic** enums and records
([Generics](generics.md)) exactly as on monomorphic ones, unqualified or
qualified. When the scrutinee is a concrete instance such as `Option[int]`, a
destructured field is bound at the **instantiated** type — matching
`Some(value)` against an `Option[int]` binds `value: int`:

```agl
enum Option[T]
  | None
  | Some(value: T)

def describe-option(o: Option[int]) -> text =
  case o of
    | Option::None => "missing"
    | Option::Some(value) => "found %{value}"   # value: int
```

The `Option::` qualifier is optional, since the scrutinee's static type
determines constructor ownership; when present, it must agree with that type.
The type arguments come from the scrutinee; an applied qualifier
(`Option[int]::Some(value)`) is allowed and must match them. Records follow the same rule, and a transparent record alias is also a
constructor-pattern spelling for its target:

```agl
record Box[T]
  value: T

type IntBox = Box[int]

def unbox(box: IntBox) -> int =
  case box of
    | IntBox(value) => value    # value: int
```

## Matching semantics

1. The scrutinee is evaluated exactly once.
2. Branch patterns are tried **in order**; the first matching branch runs.
3. Pattern variables are bound as immutable snapshots of the selected field
   values in a fresh branch scope ([Bindings and scope](bindings-and-scope.md)).
   Reassigning a `var` field after matching does not change the bound name,
   though a bound array, dictionary, or record value still aliases that value.
4. Every possible scrutinee value matches some branch, and every branch is
   selectable for at least one value (see below), so a valid `case` always
   selects a branch.

## Exhaustiveness and redundancy

Every `case` must be **exhaustive**: its arms together must cover every value
of the scrutinee type. Coverage includes the complete nested pattern, not only
the outer constructor: matching `Some(true)` and `None` does not cover
`Some(false)`.

Enum member records, record constructors, and the two boolean values form
closed domains, covered by listing every remaining constructor or literal. A
record type has exactly one constructor, so its empty constructor pattern (for
example, `Point()`) covers every `Point` value, whatever its field domains. A
record pattern that constrains fields is exhaustive only if its nested patterns
cover those fields; an empty record-constructor pattern can cover the
remainder. The rule applies recursively: a payload of enum or `bool` type may
itself be covered by a complete set of nested patterns.

The domains of `int`, `decimal`, `text`, `json`, arrays, dictionaries,
exceptions, `unit`, functions, and an unresolved type parameter are open. A
finite set of literals cannot exhaust an open domain; the remaining values need
an irrefutable wildcard or binder pattern, also when the open domain occurs
inside a constructor payload.

`bottom` has an empty domain: a diverging expression (`raise`, `return`,
`break`, or `continue`) produces no value to match, so every arm of a `case`
over `bottom` is redundant.

Every arm must also be **non-redundant**: an arm is redundant when every value
it could match is already covered by earlier arms, as with duplicate arms or
arms after a wildcard. Partial overlap is allowed when the later arm still
matches at least one new value.

`MatchError` is an ordinary constructible exception, not an automatic `case`
fallback. To reject part of a domain deliberately, cover it with an arm that
raises it explicitly ([Exceptions](exceptions.md#matcherror)):

```agl
enum Response
  | Complete(body: text)
  | Rejected(reason: text)
  | Deferred

def body-of(response: Response) -> text =
  case response of
    | Complete(body) => body
    | _ as other =>
      raise MatchError(
        message = "expected a completed response",
        scrutinee-type = "Response",
        scrutinee = other as json,
      )
```

## `is` versus `case`

Use `is` / `is not` ([Expressions](expressions.md#enum-member-and-exception-tests-is-is-not))
to test an enum member without destructuring — typically in `if` and `until`
conditions — and `case` when you need its fields:

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

program def main(artifact: text) -> unit =
  var current = artifact
  do[3]
    let review: Review = ask "Review %{current}"
    case review of
      | Fail(issues) => current := ask "Fix %{issues} in %{current}"
      | Pass => ()
  until review is Pass
```

`is` / `is not` also apply to generic enum instances, with the member qualified
as in a pattern:

```agl
program def main() -> unit =
  let probe: Option[int] = Some(value = 99)
  if probe is Option::Some => print "probe is some"
  if probe is not Option::None => print "probe is not none"
```

There is no constructor pattern for exceptions (rule 1 above); use
`case e as? T of` to test and destructure together ([`is` on
exceptions](expressions.md#enum-member-and-exception-tests-is-is-not)):

```agl
exception Problem
exception Timeout extends Problem
exception Detailed extends Problem
  detail: text

def describe(e: Problem) -> text =
  case e as? Detailed of
    | Some(value) => "detail: %{value.detail}"
    | None =>
      if e is Timeout => "timed out" else => e.message
```
