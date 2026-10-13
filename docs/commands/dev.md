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
| `sublime` | Sublime Text syntax YAML (`source.agl`), also read by bat |
| `emacs` | Lisp keyword/builtin inventory providing `agl-inventory` |

Without `--output`, the generated definition is written to stdout. With `--output`,
AgM writes a UTF-8 file, creates missing parent directories, and replaces an existing
file. Unknown targets are rejected. Shell completion suggests target names and output
paths.

```sh
agm dev highlight kde --output build/highlighting/agl.xml
agm dev highlight textmate --output build/highlighting/AgL.tmLanguage.json
agm dev highlight sublime --output build/highlighting/agl.sublime-syntax
agm dev highlight emacs --output build/emacs/agl-inventory.el
```

## Installed definitions

`just install [PREFIX] [--force]` installs:

- `<AgM home>/highlighting/agl.xml`, `agl.tmLanguage.json` (TextMate) and
  `agl.sublime-syntax` (Sublime Text).
- `$(bat --config-dir)/syntaxes/agl.sublime-syntax`, followed by `bat cache --build`,
  when `bat` (or Debian's `batcat`) is installed. Prefix installs skip bat.
- `<XDG data>/org.kde.syntax-highlighting/syntax/agl.xml` for Kate and other KDE editors.
- `<XDG data>/pandoc/defaults/agl.yaml`; use `pandoc -d agl input.md -o output.html`.
- Generated `agl-inventory.el` inside the Emacs mode package, when Emacs is available.

Generation uses the **installed** `agm`, not a similarly named executable on `PATH`.
AgM home honors `AGM_HOME`, else a populated installation-prefix `.agm`, else
`~/.agm`; an explicit prefix uses `PREFIX/.agm`, overriding `AGM_HOME`.
XDG data honors `XDG_DATA_HOME`, defaulting to `~/.local/share`; an explicit prefix
uses `PREFIX/.local/share`, overriding `XDG_DATA_HOME`. Generated definitions always
refresh. Existing Pandoc defaults are preserved unless `--force` is supplied;
forced defaults are replaced without a backup. No generated definitions are tracked.

Pandoc does not auto-load syntax definitions from its data directory: select
`-d agl` explicitly. With a prefix, set `XDG_DATA_HOME=PREFIX/.local/share` when
running Pandoc, or pass the defaults file's full path. You can bypass defaults:

```sh
pandoc --syntax-definition /path/to/AgM-home/highlighting/agl.xml input.md -o output.html
```

Use `agl` as the fenced-code language in Markdown. Kate discovers the installed
KDE syntax definition for `.agl` files. TextMate-compatible editors need their own
registration of `agl.tmLanguage.json` with scope `source.agl`; no editor settings
are changed automatically. Sublime Text loads `agl.sublime-syntax` from its `Packages/User`
directory. bat highlights `.agl` files and `--language agl`; rerun `bat cache --build`
after upgrading bat. mcat bundles its syntaxes and cannot load the definition. Emacs's package includes the generated inventory and
hand-written font-lock, installed into `package-user-dir` (normally
`~/.emacs.d/elpa/`, or under the prefix's `HOME`); non-package.el setup is described
in `config/emacs/README.md`. Micro's rules remain hand-written.

Portable grammars approximate lexical/contextual roles; they do not run scope
resolution. The REPL uses the exact lexical tier; documentation adds scope-resolved
names with lexical fallback. See [highlighting architecture](../arch/highlighting.md)
for native recovery and target restrictions. `agm help dev highlight` documents the
same targets and output option; there are no semantic or theme export flags.

## Exact documentation highlighting

The `highlight` extra registers the Pygments `agl` lexer and Python-Markdown
`agl_highlight` extension; the repository's docs environment installs both with
`uv sync --group docs`. `mkdocs.yml` wires the extension to SuperFences.
Pygments accepts hidden `context` source and a `semantic` option (default on).
Adjacent metadata supplies context in Markdown without displaying it. This example
has a hidden `record Point` declaration with an `x: int` field:

<!-- agl-context: record Point -->
<!-- agl-context:   x: int -->
```agl
let p: Point = Point(x = 1)
```

Hidden source participates in compile checks and name resolution, but never appears
in the highlighted body. It does not affect Pandoc; that path uses KDE XML only.
