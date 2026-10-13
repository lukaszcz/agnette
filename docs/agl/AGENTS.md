
## Instructions

- Do NOT describe the details of the AgL standard library (modules, module content, exact function signatures, type defintions, etc.) in the AgL language reference. The `packages/stdlib/src/*.agl` sources are the standard library's reference.
- The language reference MAY name a library type or function where the language's own semantics depend on it — the builtin exceptions, `ExecResult`, `Agent`, `AgentRequest`, `Session`, and the `std/config` engine settings.

## Fence metadata

Reference `agl` fences must statically compile without static warnings by default.
Put metadata comments on consecutive lines directly before the fence, in the same
Markdown container; blank lines or unrelated comments break adjacency. A list
marker may start the metadata block; later comments and the fence use its continuation indentation.
Backtick and tilde fences with plain `agl` or attribute-style `{.agl}` headers
share these rules; normal SuperFences options still apply.

- `<!-- agl-check: fragment -->` marks an incomplete example; `error` marks an
  intentional static rejection. Both must fail compilation; never use `skip`.
  `warns` marks a compiling example that deliberately demonstrates a static
  warning; it must draw one.
- `<!-- agl-context: <AgL source line> -->` supplies hidden declarations/imports.
  Multiple lines join in source order and are prepended to the fence body for
  compilation and highlighting. Check and context markers may appear in any order.
  One optional space after `:` is formatting; further spaces are AgL indentation
  (e.g. `<!-- agl-context:   x: int -->` supplies `  x: int`).
- Remove `fragment` when context makes the example compile. Context comments are
  not layout-style-checked or rewritten; displayed fence bodies remain checked.

`agnette.agl.highlight.context.fence_metadata` shares this contract between the
snippet guard, style tooling and Markdown adapter. Rendering removes accepted
context markers and injects URL-safe base64 into a fence-local option; check markers
remain comments. Rejected or uninjectable headers retain authored metadata.
Context never leaks to another fence or document. Pandoc's portable grammar does
not consume these markers.
