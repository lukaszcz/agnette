# Static Warnings

[← Index](index.md)

A **static warning** is an advisory diagnostic from the static checks: it names
a likely mistake in a program that is otherwise valid. Every warning has a
**name**, and the names below are the whole catalog. Unlike a static error, a
warning does not stop a program by default, but a host can raise any warning to
an error, silence it, or leave it as a warning.

## Catalog

| Name | Reported when | Exemptable |
| ---- | ------------- | ---------- |
| `tab-indentation` | A line is indented with a TAB character. | no |
| `noop-parse-retries` | An `ask` or `exec` call passes `parse-error-retries` with a `text` target, which never fails parsing ([`parse-error-retries`](agent-calls.md#parse-error-retries)). | no |
| `unquoted-exec-hole` | A plain hole inserts runtime text into an `exec` command verbatim ([Hole warnings](shell-execution.md#hole-warnings)). | yes |
| `shell-quoted-hole` | A shell-quoted hole sits inside quotes the `exec` command text itself opens ([Hole warnings](shell-execution.md#hole-warnings)). | no |

A warning is reported as `path:line:col: warning: message [name]`, ending in its
name in brackets.

## Levels

A warning is reported at one of three levels:

| Level | Effect |
| ----- | ------ |
| `allow` | Dropped: not reported. |
| `warn` | Reported; the program is unaffected. The default for every warning. |
| `deny` | Reported as an error (`error: message [name]`), so the program has a static error and nothing executes. |

The level is a host setting by warning name, never part of a program, and a
source [`@allow`](#suppression-with-allow) outranks it. A host may let its users
set it per warning; `agm` reads a [`[warnings]` configuration
table](../../commands/config.md#warnings) and a `--warning NAME=LEVEL` flag on
[`exec`, `check`, and `repl`](../../commands/agl.md#diagnostics-and-warnings),
with the flag winning over the table.

## Suppression with `@allow`

[`@allow("name", …)`](attributes.md#allow-and-exempts) hides the named warnings
reported anywhere inside the declaration it prefixes, from its first attribute to
its end; declarations nested in it inherit it. A warning outside the declaration,
in another module, or not named stays reported. A hidden warning is never reported
at any level, so a `deny` cannot turn it into an error:

```agl
program def main(name: text) -> unit =
  @allow("unquoted-exec-hole")
  let listing: text = exec "ls %{name}"
  print(listing)
```

## Exemption with `@exempts`

Some warnings are about a value placed in a hole; those are marked *exemptable*
in the catalog, and `@exempts` may name only those. Naming any other warning is a
static error. [`@exempts("name", …)`](attributes.md#allow-and-exempts) on a function
declares that a direct call of it, or of the method, placed in a hole of an
[`exec` command](shell-execution.md#hole-warnings) is exempt from the named
warnings. It is how `shell-raw` makes its holes exempt; a
[`@shell-quoted`](attributes.md#shell-quoted) function, such as `shell-quote` or
`shell-join`, is exempt from `unquoted-exec-hole` without naming it. The exemption belongs to the function, so it holds in every module that
imports it; the result of any other expression, such as `passthrough(value) ++ "x"`
or a function value bound to another name, is not exempt:

```agl
@exempts("unquoted-exec-hole")
def passthrough(value: text) -> text = value

program def main(name: text) -> unit =
  exec "echo %{passthrough(name)}"
```

## Hosts and installed libraries

A host does not report warnings from installed libraries, such as the standard
library or an installed package, at any level: the user cannot act on them, so
they can neither print nor be denied. Warnings from code the user is developing
or running locally are reported as usual.
