
## Instructions

- Do NOT describe the details of the AgL standard library (modules, module content, exact function signatures, type defintions, etc.) in the AgL language reference. The `packages/stdlib/src/*.agl` sources are the standard library's reference.
- The language reference MAY name a library type or function where the language's own semantics depend on it — the builtin exceptions, `ExecResult`, `Agent`, `AgentRequest`, `Session`, and the `std/config` engine settings.

## Fence metadata

Reference `agl` fences must statically compile by default. Put metadata comments
on consecutive lines directly before the fence, in the same Markdown container;
blank lines or unrelated comments break adjacency. A list marker may start the
metadata block; later comments and the fence use its continuation indentation.
Plain `agl` and attribute-style `{.agl}` fence headers share these rules.

- `<!-- agl-check: fragment -->` marks an incomplete example; `error` marks an
  intentional static rejection. Both must fail compilation; never use `skip`.
- `<!-- agl-context: <AgL source line> -->` supplies hidden declarations/imports.
  Multiple lines join in source order and are prepended to the fence body for
  compilation and highlighting. Check and context markers may appear in any order.
  One optional space after `:` is formatting; further spaces are AgL indentation
  (e.g. `<!-- agl-context:   x: int -->` supplies `  x: int`).
- Remove `fragment` when context makes the example compile. Context comments are
  not layout-style-checked or rewritten; displayed fence bodies remain checked.
