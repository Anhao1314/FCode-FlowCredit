// Run every unit test file with the node:test runner from a jailed sandbox.
// The host command policy only allows `node <workspace script>` — no flags —
// so verification commands declare this file as their stable entry point
// instead of `node --test tests/*.test.js`.
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here)
  .filter((name) => name.endsWith('.test.js'))
  .sort();
const result = spawnSync(process.execPath, ['--test', ...files], {
  cwd: here,
  stdio: 'inherit',
});
process.exit(result.status ?? 1);
