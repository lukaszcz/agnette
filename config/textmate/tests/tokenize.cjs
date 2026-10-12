// Native TextMate tokenization shared by the optional suite and pytest probes.
const fs = require('fs');
const tm = require('vscode-textmate');
const onig = require('vscode-oniguruma');

function codepointOffsets(text) {
  const offsets = [0];
  let position = 0;
  for (const character of text) {
    for (let unit = 1; unit < character.length; unit++) offsets.push(position);
    offsets.push(++position);
  }
  return offsets;
}

async function tokenizeCases(cases) {
  await onig.loadWASM(fs.readFileSync(require.resolve('vscode-oniguruma/release/onig.wasm')));
  const results = [];
  for (const { grammar, source } of cases) {
    const registry = new tm.Registry({
      onigLib: Promise.resolve({
        createOnigScanner: patterns => new onig.OnigScanner(patterns),
        createOnigString: text => new onig.OnigString(text),
      }),
      loadGrammar: async () => grammar,
    });
    try {
      const loaded = await registry.loadGrammar(grammar.scopeName);
      let stack = tm.INITIAL;
      let offset = 0;
      const tokens = [];
      // Only CR/LF are line boundaries; Unicode separators remain ordinary content.
      const parts = source.split(/(\r\n|\r|\n)/);
      for (let i = 0; i < parts.length; i += 2) {
        const line = parts[i];
        const positions = codepointOffsets(line);
        const result = loaded.tokenizeLine(line, stack);
        stack = result.ruleStack;
        for (const token of result.tokens) {
          // TextMate can include its synthetic end-of-line character in a token.
          const end = Math.min(token.endIndex, line.length);
          const text = line.slice(token.startIndex, end);
          if (text) tokens.push({
            start: offset + positions[token.startIndex],
            end: offset + positions[end],
            text,
            scopes: token.scopes,
          });
        }
        offset += positions[line.length];
        const newline = parts[i + 1];
        if (newline) {
          tokens.push({ start: offset, end: offset + newline.length, text: newline, scopes: [] });
          offset += newline.length;
        }
      }
      results.push(tokens);
    } finally {
      registry.dispose();
    }
  }
  return results;
}

exports.tokenizeCases = tokenizeCases;

if (require.main === module) {
  const grammar = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
  tokenizeCases(sources.map(source => ({ grammar, source })))
    .then(results => process.stdout.write(JSON.stringify(results)))
    .catch(error => { console.error(error); process.exitCode = 1; });
}
