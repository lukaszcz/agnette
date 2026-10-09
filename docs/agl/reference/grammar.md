# Grammar

[← Index](index.md)

The collected surface grammar. Names, numbers, templates, and layout rules
are specified in [Lexical structure](lexical-structure.md); the semantics of
each form, in the chapter linked from its section.

Notation: `::=` defines a production; `|` separates alternatives; `?`, `*`,
`+` mark optional and repeated elements, and `[ … ]` an optional group; quoted
strings are literal source text. `NEWLINE`, `INDENT`, and `DEDENT` are
**source-layout notation**: they describe a line break and indentation
change, not text written in source.

## Programs and blocks

```ebnf
name       ::= NAME | OP_NAME
field_name ::= NAME

program      ::= module_block EOF
module_block ::= [ module_item ((NEWLINE | ";") module_item)* (NEWLINE | ";")? ]
module_item  ::= scope_region | item
block        ::= item ((NEWLINE | ";") item)* (NEWLINE | ";")?

item ::= header_decl                 (* module root or scope region, header position *)
       | static_decl                 (* module root or scope region *)
       | infix_decl                  (* module root only *)
       | let_decl | var_decl
       | assign_stmt | expr          (* not at a static module root or in a scope region *)

header_decl ::= import_decl | use_decl | export_decl
static_decl ::= record_def | enum_def | exception_def | type_alias
              | func_def | program_func_def | builtin_func_def
              | extern_func_def      (* file-backed modules only *)
              | builtin_var_def      (* standard-library modules only *)

builtin_modifier ::= "builtin" NEWLINE?
```

Block values, binders in final position, and discarded values:
[Program structure](program-structure.md#blocks).

### Scope regions

```ebnf
scope_region ::= "scope" scope_path (NEWLINE | ";")
                 [scope_item ((NEWLINE | ";") scope_item)* (NEWLINE | ";")?]
                 "end" scope_path
               | "scope" scope_path INDENT
                 scope_item ((NEWLINE | ";") scope_item)* (NEWLINE | ";")?
                 DEDENT NEWLINE "end" scope_path
scope_path   ::= NAME ("::" NAME)*
scope_item   ::= scope_region | header_decl | static_decl | let_decl | var_decl
```

The closer repeats the header's path: `scope A::B` closes with `end A::B`. The
items either share the header's layout level or sit in an indented block
beneath it; the closer stands at the header's own level either way. Regions
appear only as module-root items or as items of another region, and a
multi-segment header is equivalent to nested single-segment regions. `scope`
and `end` are soft keywords ([Keywords](lexical-structure.md#soft-keywords)).
Semantics: [Named scopes](scopes.md).

`builtin` is a **declaration modifier**: it sits on the same line as the
declaration it adorns (`builtin enum …`) or on the line directly above it.
It prefixes a `record`, `enum`, `exception`, or `type` alias; `builtin def`
and `builtin var` are body-less declaration forms of their own. The `extern`
of an `extern def` and the `program` of a `program def` place the same way.

## Import and export declarations

```ebnf
import_decl ::= "import" module_path ["/*"]
                ("as" NAME | "::" tail)? [hiding_clause]
use_decl    ::= "use" use_target ("::" use_tail | "as" NAME) [hiding_clause]
export_decl ::= "export" module_path ["/*"] ["::" braces] [hiding_clause]

tail          ::= "*" | braces | path_atom ["as" ref_name]
use_tail      ::= "*" | braces | (NAME "::")* NAME ["as" NAME]
braces        ::= "{" brace_item ("," brace_item)* ","? "}"
brace_item    ::= path_atom ["as" ref_name]
use_target    ::= "/" qualifier_path | "::" scope_path | qualifier_path
module_path   ::= NAME ("/" NAME)*    (* byte-adjacent, as is a trailing "/*" *)
qualifier_path ::= NAME ("/" NAME)* ("::" NAME)*
ref_name      ::= name
hiding_clause ::= "hiding" path_atom ("," path_atom)*
path_atom     ::= (NAME "::")* name
```

An import alias and a tail are exclusive. Braces cannot be empty or nested,
and cannot be combined with `hiding`. `hiding` follows a plain import, an
import glob, a module wildcard import, or a use glob. Outside braces, a `use`
alias, selection, and rename are `NAME`s; an operator name, selected or as a
rename, needs braces (`use S::{<+>}`, `use S::{plus as <+>}`). Semantics:
[Modules](modules.md).

<!-- agl-check: fragment -->
```agl
import foo/bar
import foo/bar as A
import foo/bar::{x, y}
import foo/bar::x as X
import foo/bar::* hiding internal
import foo/*::*
import foo/bar/* as A
export foo/bar::{x as X, y}
export foo/bar/* hiding internal
use Point::{distance as d}
use ::Scope::*
```

### Suites (indented blocks)

```ebnf
suite ::= NEWLINE INDENT block DEDENT
```

A suite starts on the line after its introducer. Its block is indented more
than the introducing construct and ends when the indentation returns to that
construct's level.

### Inline bodies

Every body may be written as a suite. Written inline, a `;` sequence is
admissible only in the three body positions whose end is marked by a token:
parenthesized blocks, loops, and `try` expressions.

```ebnf
marked_body   ::= (marked_item ";")* marked_item
marked_item   ::= expr | inline_assign | let_decl | var_decl
inline_assign ::= assign_target ":=" or_expr
```

| body | delimiter | inline form |
| ---- | --------- | ----------- |
| `( … )` | `)` | `marked_body` |
| `do … until`/`done` | `until` / `done` | `marked_body` |
| `try … catch` | `catch` | `marked_body`, last item not a `try`, lambda, or record update |
| `def f() = …` | enclosing newline or `;` | exactly one `expr` |
| `… => …` | *none* | exactly one `closed_item` |

A marked body allows any marked item in final position, including a `let` or
`var` binder. A body's last item may not be a form that could consume the
body's own terminator. Nothing consumes `)`. A loop's `until`/`done` occurs
exactly once per loop, so even a nested loop in final position is unambiguous
— `do … until p until q` binds the inner `until` innermost-first. `catch` is a
repeatable clause, so a `try` body's final item excludes a nested `try`, which
would take every following `catch`, and a lambda, whose unmarked body extends
rightwards; a record update is excluded there too. Each stays legal earlier in
the sequence, and in final position once parenthesized —
`try (fn(x: int) -> int => x) catch _ => …`.

An inline `def` body is exactly one expression; its enclosing block separator
— a newline or `;` — ends the body and starts the next block item. A `=>` body
ends at nothing at all — a following `|`, `else`, or `catch` could belong
either to the body or to the enclosing branch list — so it holds a single
`closed_item`: no `;`, no binder, and none of the right-extending forms
(`if`, `case`, `try`, a loop, a lambda) whose own branch lists or body would
swallow the enclosing form's continuation. To write a multi-item body after
`=>`, parenthesize it or use the suite form:

```agl
def show(k: int) -> unit =
  case k of
    | 1 => (let doubled = k * 2; print "doubled:%{doubled}")
    | 2 =>
        let doubled = k * 2
        print "doubled:%{doubled}"
    | _ => ()
```

Parentheses directly after a callee are that call's argument list, so a
parenthesized block passed as an argument carries its own parentheses:
`print((let x = 4; x * 2))`.

## Attributes

```ebnf
attributes    ::= attribute+
attribute     ::= "@" NAME ["(" attr_arg_list? ")"] NEWLINE?
attr_arg_list ::= attr_arg ("," attr_arg)* ","?
attr_arg      ::= element_expr                    (* positional: every attribute but @config *)
                | attr_key "=" element_expr       (* keyed: @config only *)
attr_key      ::= qualifier_chain? name
```

Layout: [Lexical structure](lexical-structure.md#attributes); placement and
catalog: [Attributes](attributes.md).

## Type declarations

```ebnf
decl_head        ::= [scope_path "::"] name
record_def       ::= attributes? builtin_modifier? "record" decl_head type_params?
                    ("="? record_body)?
record_body      ::= NEWLINE INDENT field_def (NEWLINE field_def)* NEWLINE? DEDENT
                   | "(" field_list? ")"
                   | field_list
field_def        ::= attributes? "var"? field_name ":" type_expr ("=" or_expr)?

enum_def         ::= attributes? builtin_modifier? "enum" decl_head type_params?
                    "="? enum_body
enum_body        ::= enum_member_seq
                   | NEWLINE INDENT enum_member_seq NEWLINE? DEDENT
enum_member_seq  ::= "|"? enum_member ("|" enum_member)*
enum_member      ::= attributes? name member_payload?
                   | qualifier_chain name member_type_args?
member_type_args ::= "[" type_expr ("," type_expr)* "]"
member_payload   ::= "(" field_list? ")"
                   | NEWLINE INDENT field_def (NEWLINE field_def)* NEWLINE? DEDENT
field_list       ::= field_inline ("," field_inline)* ","?
field_inline     ::= attributes? "var"? field_name ":" type_expr ("=" or_expr)?

exception_def    ::= attributes? builtin_modifier? "exception" decl_head
                    exception_base? exception_body?
exception_base   ::= "extends" name
exception_body   ::= NEWLINE INDENT field_def (NEWLINE field_def)* NEWLINE? DEDENT
                   | "(" field_list? ")"
                   | field_list

type_alias       ::= attributes? builtin_modifier? "type" decl_head type_params? "=" type_expr

type_params      ::= "[" name ("," name)* "]"
```

A `record` or `exception` without a body declares no fields of its own,
exactly like an empty `()` field list; `=` always requires a body. A field's
`=` default is a constant expression. `var` marks a mutable field of a record
or enum-member record, never of an exception. Attribute layout on enum
members: [Lexical structure](lexical-structure.md#attributes).

An enum member written as a bare `name` declares a record in the enum's scope;
a qualified member references an existing record and has no field list:
`Entry(x: int)` declares `Enum::Entry`, while `::Entry` references the current
module's `Entry`. Semantics: [Record types](types.md#record-types),
[Enum types](types.md#enum-types), [Generics](generics.md).

## Type expressions

```ebnf
type_ann  ::= ":" type_expr

type_expr ::= "(" type_list? ")" "->" type_expr       (* function type *)
            | type_atom "->" type_expr                 (* one-parameter function type *)
            | type_atom
type_atom ::= qualifier_chain? name ["[" type_expr ("," type_expr)* "]"]
type_list ::= type_expr ("," type_expr)* ","?

qualifier_chain   ::= "::" qualifier_segment*
                    | qualifier_segment+
qualifier_segment ::= ["/"] NAME ("/" NAME)* "::"
                    | NAME "[" type_expr ("," type_expr)* "]" "::"
```

The built-in types `unit`, `text`, `json`, `bool`, `int`, `decimal`,
`array[T]`, and `dict[K, V]` are names in type position. `->` is
right-associative, and there are no parenthesized type groups: `(A) -> B`
is a one-parameter function type. A qualifier chain is byte-adjacent through
each `::`, and an applied segment's `[` is byte-adjacent to its name
(`Option[int]::Some`); an ordinary applied type may be spaced
(`Option [int]`). See [Qualifier chains](lexical-structure.md#qualifier-chains)
and [Types](types.md).

## Function declarations

```ebnf
func_def         ::= attributes? "def" func_decl_head type_params? constraint_block?
                     "(" param_list? ")" ("->" type_expr)? ("=" func_body | suite)
program_func_def ::= attributes? "program" NEWLINE? "def" func_decl_head type_params?
                     "(" param_list? ")" ("->" type_expr)? ("=" func_body | suite)
builtin_func_def ::= attributes? "builtin" NEWLINE? "def" func_decl_head type_params?
                     constraint_block? "(" param_list? ")" "->" type_expr
extern_func_def  ::= attributes? "extern" NEWLINE? "def" func_decl_head type_params?
                     constraint_block? "(" param_list? ")" "->" type_expr
func_decl_head   ::= decl_head | builtin_receiver "::" name
builtin_receiver ::= "array" "[" receiver_param "]"
                   | "dict" "[" (receiver_param | "text") "," receiver_param "]"
                   | "text" | "json" | "int" | "decimal" | "bool"
receiver_param   ::= name | "_"
func_body        ::= expr | suite
constraint_block ::= "{" constraint ("," constraint)* ","? "}"
constraint       ::= ("Eq" | "Hashable") name
param_list       ::= param ("," param)* ","?
param            ::= attributes? field_name [":" type_expr] ("=" or_expr)?
```

Only `self` as a method's first parameter may omit its annotation; every other
`def` parameter is annotated. The return type is optional for `def` and
required for the body-less `builtin def` and `extern def`. Before a suite
body, the `=` is optional. `_` is a type parameter only in a built-in method's
receiver (`array[_]::name`), where the method leaves it unnamed. A constraint block follows the type parameters, or
the method name when the declaration has none of its own
(`array[E]::name{Eq E}`). Semantics: [Functions](functions.md),
[Methods](functions.md#methods),
[Constraint blocks](generics.md#constraint-blocks), [Python FFI](ffi.md).

## Infix declarations

```ebnf
infix_decl     ::= ("infixl" | "infixr") infix_op infix_priority?
infix_priority ::= "at" INT
                 | "at" "prio" infix_op ("+" | "-") INT
infix_op       ::= "or" | "and" | "in"
                 | "==" | "!=" | "<" | "<=" | ">" | ">="
                 | "+" | "-" | "*" | "/"
                 | OP_NAME
```

`at` is contextual. A built-in operator appears in an infix declaration only
as the base of a relative priority. Semantics:
[User-defined operators](lexical-structure.md#user-defined-operators).

## Bindings and mutation

```ebnf
let_decl        ::= attributes? "let" decl_head type_ann? "=" init_value
var_decl        ::= attributes? "var" decl_head type_ann? "=" init_value
builtin_var_def ::= attributes? "builtin" NEWLINE? "var" name type_ann ["=" expr]
assign_stmt     ::= assign_target ":=" init_value
init_value      ::= expr | suite
assign_target   ::= qualifier_chain? name
                  | postfix "[" expr "]"
                  | postfix "." field_name
```

A binder names exactly one `decl_head`; there are no destructuring binders.
An initializer is one expression or a
[suite](bindings-and-scope.md#suite-initializers). A `builtin var` has a mandatory type and an optional constant initializer. An
indexing `[` in an assignment target is adjacent to the expression before it:
`xs[0] := …` assigns an element, while `xs [0]` is no target. Semantics:
[Bindings and scope](bindings-and-scope.md).

## Loops

```ebnf
loop         ::= for_clause? while_clause? "do" loop_bound?
                 (suite loop_end? | marked_body loop_end)
for_clause   ::= "for" name "in" or_expr range_tail? NEWLINE?
range_tail   ::= ("to" | "downto") or_expr ("step" or_expr)?
while_clause ::= "while" or_expr NEWLINE?
loop_bound   ::= "[" or_expr "]"
loop_end     ::= "until" or_expr | "done"
```

At most one `for` and one `while` clause, in that order. Only the suite form
may omit its terminator. The `[` after `do` always opens the bound, whatever
its spacing, so a bound never conflicts with an array literal; the bound is
any `int` expression (`do[n + 1]`). Semantics: [Control flow](control-flow.md).

## `if`

```ebnf
if_expr        ::= "if" "|"? if_cond_branch ("|" if_cond_branch)* if_else_branch?
if_cond_branch ::= or_expr "=>" branch_body
if_else_branch ::= "|"? "else" "=>" branch_body

branch_body    ::= suite | closed_item
closed_item    ::= or_expr | inline_assign | raise_expr | return_expr
```

`branch_body` is shared by `if` branches, `case` branches, and `catch`
clauses; see [Inline bodies](#inline-bodies).

## `case`

```ebnf
case_expr       ::= "case" or_expr "of" case_body
case_body       ::= case_branch_seq
                  | NEWLINE INDENT case_branch_seq NEWLINE? DEDENT
case_branch_seq ::= "|"? case_branch ("|" case_branch)*
case_branch     ::= pattern "=>" branch_body
```

The branch list either follows `of` on the same line or forms an indented
block on the lines below it.

## `try` / `catch`

```ebnf
try_expr        ::= "try" try_body catch_clause+
try_body        ::= suite | (marked_item ";")* try_tail
try_tail        ::= or_expr | inline_assign | try_letvar_decl | raise_expr
                  | return_expr | if_expr | case_expr | loop
try_letvar_decl ::= attributes? ("let" | "var") decl_head type_ann? "=" try_value
try_value       ::= or_expr | raise_expr | return_expr | if_expr | case_expr | loop
catch_clause    ::= "catch" catch_pattern "=>" branch_body
catch_pattern   ::= name ("as" name)?
```

An inline try body is a full `;` sequence. Its final item, and a final
binder's initializer, may be an `if`, `case`, or loop, but not a nested `try`,
a lambda, or a record update unless parenthesized (see
[Inline bodies](#inline-bodies)). A `catch_pattern` name is an exception type
or `_`. Semantics: [Exceptions](exceptions.md).

## Patterns

```ebnf
pattern        ::= pattern_atom ("as" name)*
pattern_atom   ::= "_"
                 | INT | DECIMAL | "true" | "false" | "null" | STRING
                 | qualifier_chain? name ["(" pattern_fields? ")"]
pattern_fields ::= pattern_field ("," pattern_field)* ","?
pattern_field  ::= pattern                     (* positional *)
                 | field_name "=" pattern      (* named *)
```

`as` has the lowest pattern precedence and may be chained. A `STRING` pattern
contains no interpolation. A pattern's type arguments come from the
scrutinee; an applied qualifier segment (`Option[int]::Some(…)`) is allowed and
must agree with them.
Semantics: [Pattern matching](pattern-matching.md).

## Expressions

```ebnf
expr      ::= case_expr | if_expr | loop | try_expr | raise_expr
            | return_expr | record_update | lambda_expr | or_expr

record_update ::= update_target "with" field_update ("," field_update)*
update_target ::= record_update | or_expr     (* chaining is left-associative *)
field_update  ::= field_name "=" or_expr

or_expr       ::= infix_operand (infix_op infix_operand)*
infix_operand ::= "not"* is_expr

is_expr        ::= cast "is" "not"? qualifier_chain? name
                 | cast

cast           ::= cast "as" type_expr
                 | cast "as?" type_expr
                 | unary

unary          ::= "-" unary | juxt

juxt           ::= postfix juxt_arg     (* single-argument sugar; non-chaining *)
                 | postfix
juxt_arg       ::= juxt_atom juxt_suffix*
juxt_atom      ::= INT | DECIMAL | "true" | "false" | "null"
                 | array_literal | dict_literal | template
                 | NAME                               (* not OP_NAME *)
                 | qualifier_chain name
juxt_suffix    ::= "." field_name
                 | "[" expr "]"                       (* adjacent bracket only *)
                 | "(" arg_list? ")"
                 | "::" "[" type_expr ("," type_expr)* "]" "(" arg_list? ")"

postfix        ::= atom postfix_suffix* | leading_dot_expr
postfix_suffix ::= "." field_name                     (* member selection *)
                 | "(" arg_list? ")"                  (* call *)
                 | "[" expr "]"                       (* indexing; adjacent bracket only *)
                 | "::" "[" type_expr ("," type_expr)* "]"   (* explicit type arguments *)
leading_dot_expr ::= "." field_name postfix_suffix*

atom           ::= INT | DECIMAL | "true" | "false" | "null"
                 | "(" ")"                            (* unit literal *)
                 | array_literal | dict_literal
                 | qualifier_chain? name              (* reference or constructor *)
                 | template
                 | "(" expr ")"
                 | "(" paren_block ")"
                 | "(" builtin_op ")"                 (* operator as a function value *)
                 | "break" | "continue"

builtin_op     ::= "+" | "-" | "*" | "/" | "==" | "!=" | "<" | "<=" | ">" | ">="

paren_block    ::= (marked_item ";")+ marked_item
                 | inline_assign
                 | let_decl | var_decl

raise_expr     ::= "raise" or_expr
return_expr    ::= "return" or_expr?
```

An operator chain is flat in the grammar and grouped by
[operator precedence](lexical-structure.md#operator-precedence): each binary
operator at its priority and associativity, and each `not` as a prefix whose
operand extends over the following operators of priority 25 or higher (`not a
== b` is `not (a == b)`; `not a and b` is `(not a) and b`). Comparisons do not
chain. `is` takes a constructor name, so it does not chain either.

A leading-dot expression is a unary function whose omitted receiver is the
start of its postfix chain: `.trim().size()` behaves like
`fn(receiver: text) => receiver.trim().size()`
([Methods](functions.md#methods)). The `::[…]` suffix gives a generic callee
explicit type arguments (`id::[int](5)`); a member selected through its owning
generic type writes them on the type segment instead
(`Option[int]::Some(value = 1)`) — see [Generics](generics.md). A
`"." field_name` suffix is always member selection; constructor qualification
uses `::`. A `return` followed by a newline is a bare `return`.

## Lambda expressions

```ebnf
lambda_expr       ::= "fn" lambda_params ("->" type_expr)? "=>" expr
lambda_params     ::= field_name | "(" lambda_param_list? ")"
lambda_param_list ::= lambda_param ("," lambda_param)* ","?
lambda_param      ::= attributes? field_name [":" type_expr]
```

A bare parameter form declares exactly one parameter; zero or several use
parentheses. Parameter and return annotations may be omitted where an
expected function type supplies them ([Functions](functions.md)).

## Calls

```ebnf
arg_list        ::= arg ("," arg)* ","?
arg             ::= element_expr                   (* positional *)
                  | placeholder_arg                (* positional hole *)
                  | field_name "=" element_expr    (* named *)
                  | field_name "=" placeholder_arg (* named hole *)
placeholder_arg ::= "?" | "?<digits>"
element_expr    ::= expr                           (* without a bare record_update *)
```

In comma-separated element positions — call and attribute arguments, array
and dict elements — a record update, including one that forms an inline
lambda's body, is parenthesized so its update list cannot be confused with the
enclosing list ([Record update](expressions.md#record-update)). A placeholder
is legal only as a whole parenthesized call argument, so `f(? + 1)`, a
standalone `?`, and a placeholder in single-argument sugar do not parse
([Partial application](functions.md#partial-application)).

## Literals

```ebnf
array_literal ::= "[" (element_expr ("," element_expr)* ","?)? "]"
dict_literal  ::= "{" (dict_entry ("," dict_entry)* ","?)? "}"
dict_entry    ::= element_expr ":" element_expr
```

## Templates

```ebnf
template      ::= '"' (text_fragment | interpolation | env_hole)* '"'
                | "'" (text_fragment | interpolation | env_hole)* "'"
                | '"""' (text_fragment | interpolation | env_hole)* '"""'
                | "'''" (text_fragment | interpolation | env_hole)* "'''"
                | "$" (verbatim_text | interpolation)+  (* to end of line, or an indented block *)

interpolation ::= "%{" expr "}"                    (* single line *)
env_hole      ::= "${" NAME "}"
```

Escapes and triple-quoted dedent are lexical
([Strings and templates](lexical-structure.md#strings-and-templates)), as is a
`$` template's payload ([Verbatim literals](lexical-structure.md#verbatim-literals)).
Hole semantics: [Strings and interpolation](strings-and-interpolation.md).

```agl
program def main() -> unit =
  let path = "."
  let output: text = exec $ printf '%s' %{path}
  print output
```

## Deterministic-parse notes

- A single `=` belongs to bindings, named arguments, fields, and declarations;
  it is not an expression operator. Equality is `==`.
- A `|`, `else`, `catch`, or `until` at the start of a line attaches to the
  innermost construct that can accept it; the
  [layout rules](lexical-structure.md#layout-indentation-newlines-continuation)
  guarantee each such token belongs to exactly one construct.
- The single-argument call `f x` means `f(x)`. It does not chain, so `f x y`
  is invalid.
- `()` is both the unit literal and the empty argument list of a zero-argument
  call.
- `if`/`until` conditions are `or_expr`, so a right-extending form there is
  parenthesized.
