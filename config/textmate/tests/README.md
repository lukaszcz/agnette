# Native TextMate suite

Run `just test-textmate` from the repository root. It installs locked npm dependencies,
generates `build/highlighting/AgL.tmLanguage.json` through `agm dev highlight`, and compares
native scopes with `grammar_eval` over the shared engine corpus. Node and npm are optional;
this suite is outside `just check`.

`tokenize.cjs GRAMMAR.json` reads a JSON array of source strings on stdin and writes a JSON
array of token arrays on stdout. Tokens contain `start`, `end`, `text`, and `scopes`.
Offsets count Unicode codepoints, not JavaScript UTF-16 units. Concatenating token text
reconstructs the unchanged input, including CR, LF, CRLF and empty lines. Newline tokens
have no scopes; the Python adapter maps them to `Category.TEXT`.

The exported `tokenizeCases` also accepts `{grammar, source}` pairs for the native pytest
probes in `tests/test_agl_highlight_textmate_native.py`. They run in `just check` whenever
node is installed, and fail if `node_modules/` is missing. `just setup` installs it when npm
is available.
