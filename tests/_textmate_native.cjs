// Keep the pytest probe's per-codepoint scope protocol over the shared engine.
const fs = require('fs');
const { tokenizeCases } = require('../config/textmate/tests/tokenize.cjs');

const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
tokenizeCases(cases)
  .then(results => process.stdout.write(JSON.stringify(
    results.map(tokens => tokens.flatMap(token => Array.from(token.text, () => token.scopes)))
  )))
  .catch(error => { console.error(error); process.exitCode = 1; });
