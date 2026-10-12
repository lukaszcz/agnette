# Development and integration tooling

`agm dev` provides development and integration tools.

## Export AgL highlighting definitions

```text
agm dev highlight TARGET [--output PATH]
```

Targets come from the highlighting generator registry:

| Target | Output |
| ------ | ------ |
| `kde` | KSyntaxHighlighting XML, also usable by Pandoc |
| `textmate` | TextMate grammar JSON (`source.agl`) |
| `emacs` | Lisp keyword/builtin inventory providing `agl-inventory` |

Without `--output`, the generated definition is written to stdout. With `--output`,
AgM writes a UTF-8 file, creates missing parent directories, and replaces an existing
file. Unknown targets are rejected. Shell completion suggests target names and output
paths.

```sh
agm dev highlight kde --output build/highlighting/agl.xml
agm dev highlight textmate --output build/highlighting/AgL.tmLanguage.json
agm dev highlight emacs > build/agl-inventory.el
```

## Installed definitions

`just install [PREFIX] [--force]` installs:

- `<AgM home>/highlighting/agl.xml` and `agl.tmLanguage.json` (TextMate).
- `<XDG data>/org.kde.syntax-highlighting/syntax/agl.xml` for Kate and other KDE editors.
- `<XDG data>/pandoc/defaults/agl.yaml`; use `pandoc -d agl input.md -o output.html`.
- Generated `agl-inventory.el` inside the Emacs mode package, when Emacs is available.

AgM home honors `AGM_HOME`, defaulting to `~/.agm`; with a prefix it is `PREFIX/.agm`.
XDG data honors `XDG_DATA_HOME`, defaulting to `~/.local/share`; with a prefix it is
`PREFIX/.local/share`. Generated definitions always refresh. Existing Pandoc defaults
are preserved unless `--force` is supplied. No generated definitions are tracked.

These portable definitions approximate lexical highlighting; they do not provide the
exact highlighting used by the documentation and REPL. See
[highlighting architecture](../arch/highlighting.md) for the target contracts.
