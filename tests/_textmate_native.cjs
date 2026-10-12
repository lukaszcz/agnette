// Independent vscode-textmate/Oniguruma execution; one process per batch.
const fs = require('fs');
const tm = require('vscode-textmate');
const onig = require('vscode-oniguruma');

async function main() {
  await onig.loadWASM(fs.readFileSync(require.resolve('vscode-oniguruma/release/onig.wasm')));
  const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
  const results = [];
  for (const { grammar, source } of cases) {
    const registry = new tm.Registry({
      onigLib: Promise.resolve({
        createOnigScanner: patterns => new onig.OnigScanner(patterns),
        createOnigString: text => new onig.OnigString(text),
      }),
      loadGrammar: async () => grammar,
    });
    const loaded = await registry.loadGrammar(grammar.scopeName);
    let stack = tm.INITIAL;
    const colors = [];
    const parts = source.split(/(\r\n|\r|\n)/);
    for (let i = 0; i < parts.length; i += 2) {
      const line = parts[i];
      const result = loaded.tokenizeLine(line, stack);
      stack = result.ruleStack;
      for (const token of result.tokens) {
        for (const _ of line.slice(token.startIndex, token.endIndex)) {
          colors.push(token.scopes);
        }
      }
      if (parts[i + 1]) for (const _ of parts[i + 1]) colors.push([]);
    }
    results.push(colors);
    registry.dispose();
  }
  process.stdout.write(JSON.stringify(results));
}
main().catch(error => { console.error(error); process.exit(1); });
