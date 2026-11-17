// Imported FIRST by gateway.mjs: ES modules evaluate their imports before the importing module's
// body, so env files must be loaded here or other modules would read their config too early.
import fs from 'node:fs';

for (const file of (process.env.WORKBOT_ENV_FILES || '').split(',').filter(Boolean)) {
  if (!fs.existsSync(file)) continue;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^([A-Z_][A-Z_0-9]*)=(.*)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim().replace(/^(['"])(.*)\1$/, '$2');
  }
}
