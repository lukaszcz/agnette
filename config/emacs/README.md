# AgL Emacs mode

An Emacs major mode for AgL (Agnette Language), the agent workflow DSL of Agnette
(Agentic Notation & Execution Toolkit). It provides syntax highlighting, layout-aware
indentation, declaration navigation, on-save diagnostics through `agm check`, and an
inferior `agm repl`.

## Files

| File | Contents |
| ---- | -------- |
| `agl-inventory.el` | Keyword/builtin inventory, generated at install time; not tracked |
| `agl-mode.el` | Entry point: syntax table, `syntax-propertize`, font-lock, imenu, defun motion, keymap |
| `agl-indent.el` | Indentation engine |
| `agl-flymake.el` | Flymake backend over `agm check` |
| `agl-run.el` | `agl-run` / `agl-check` through `compile` |
| `agl-repl.el` | Inferior AgL REPL over `agm repl` |
| `tests/` | ERT suites; not part of the installed package |

## Install

```bash
just setup-emacs                  # install with package.el
just setup-emacs /tmp/somewhere   # install under a different HOME
```

The installer generates `agl-inventory.el` with the repository's `agm dev highlight
emacs`, then packages it with the hand-written mode. The package is byte-compiled
with generated autoloads, and its version tracks AgM's. `just install` runs this step
when `emacs` is available. It uses Emacs's `package-user-dir` (normally
`~/.emacs.d/elpa/`; a prefix replaces `HOME`) and honors relocation in `early-init.el`.
Reinstallation always replaces the package; `--force` is accepted for installer
parity. Your init file is never modified.

Without package.el (Doom, straight.el, …), generate the inventory from the repository:

```bash
uv run agm dev highlight emacs --output build/emacs/agl-inventory.el
```

Then load both directories:

```elisp
(add-to-list 'load-path "/path/to/agm/build/emacs")
(add-to-list 'load-path "/path/to/agm/config/emacs")
(require 'agl-mode)
```

## Keys

| Key | Command |
| --- | ------- |
| `C-c C-c` | `agl-run` — run the file with `agm exec` |
| `C-c C-k` | `agl-check` — check it with `agm check` in a compilation buffer |
| `C-c C-z` | `agl-repl` — start or switch to the inferior REPL |
| `C-c C-r` | `agl-repl-reload-buffer` — reset the REPL and reload the buffer |
| `M-RET` | `agl-send-region` |
| `C-c C-b` | `agl-send-buffer` |
| `C-c C-l` | `flymake-show-buffer-diagnostics` — list this file's diagnostics |

The **AgL** menu offers the same actions. Set `help-at-pt-display-when-idle` to echo the
diagnostic under point in the minibuffer.

A region is sent to the REPL unsplit; if its last line is indented, the closing blank line
is sent too. Sent source is not echoed back — only the result or definition summary and the
final prompt appear. REPL input is highlighted like source buffers.

## Customization

- `agl-indent-offset` — block indentation, default 2.
- `agl-exec-command`, `agl-check-command`, `agl-repl-command` — command vectors the
  integrations run; put any flags here.
- `agl-flymake-enable` — enable `flymake-mode` with the mode.
- `agl-repl-reload-on-save` — reset and reload the buffer after each save; off by default
  because it clears values entered at the REPL prompt.
- `agl-repl-buffer-name` — the inferior REPL buffer's name.
- `agl-interpolation-face` — the face for `%{`/`}` delimiters.

## Known limits

- Diagnostics describe the **saved** file: imports resolve from the entry file's real path,
  so unsaved edits are not checked.
- Completed `%{…}` holes receive code font-lock; incomplete holes may retain the
  host literal's face.
- Constructor use-sites in expressions are unfaced: capitalization carries no meaning in
  AgL, so faces come from declaration and annotation positions only.
- Indentation does not guess: `TAB` cycles through the levels a line could legally take —
  the columns of enclosing lines, not multiples of `agl-indent-offset` — and
  `indent-region` keeps a line already at a valid level. `RET` indents the new line but
  never re-indents the one it ends.
- Where the grammar fixes a line's level, that level wins: module items return to their
  enclosing root or scope region; `until`, `done`, `catch`, and `else` align with their
  header; a closing bracket aligns with its opening line; enum member fields hang one level
  under the member. A line re-indents as soon as typing settles its construct (an `@` or
  closing bracket, the space after a declaration keyword, a complete branch marker, or the
  newline after a lone `builtin`, `extern`, or `program`).

## Tests

```bash
just test-emacs
```

The recipe generates the inventory first; the ERT suites never run `agm`.
They are outside `just check`, which does not require Emacs.
