// Replay queued MR-Mem writes with re-authorization and 4xx/5xx differentiation.
import fs from 'node:fs';
import path from 'node:path';
import { wb } from './workbench-client.mjs';

export function retryFilePath(env = process.env) {
  return path.join(env.WORKBOT_WORK_DIR || '/workbot/workspace', 'memory-retry.jsonl');
}

export const RETRY_FILE = retryFilePath();

export async function retryMemoryWrites({
  retryFile = retryFilePath(),
  memUrl = (process.env.MRMEM_URL || 'http://127.0.0.1:8880').replace(/\/$/, ''),
  memToken = process.env.MRMEM_TOKEN || '',
  agentIdentity = {
    provider: 'host',
    bot_id: process.env.WORKBOT_BOT_ID || 'workbot',
    external_user_id: process.env.WORKBOT_AGENT_EXTERNAL_ID || 'agent',
  },
  authorize = (identity, action) => wb('/v1/memory/authorize', { identity, action }),
  fetchImpl = fetch,
} = {}) {
  if (!fs.existsSync(retryFile)) return 0;
  const lines = fs.readFileSync(retryFile, 'utf8').split('\n').filter(Boolean);
  const left = [];
  for (const line of lines) {
    try {
      const { path: p, body, identity } = JSON.parse(line);
      // Re-check the grant at replay time: a revoked grant drops the queued write.
      const allowed = await authorize(identity || agentIdentity, 'memory.write')
        .then(() => true)
        .catch(error => (error.status === 403 ? false : null));
      if (allowed === false) continue;
      if (allowed === null) { left.push(line); continue; }
      const response = await fetchImpl(memUrl + p, {
        method: 'POST',
        signal: AbortSignal.timeout(8000),
        headers: { Authorization: 'Bearer ' + memToken, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      // 4xx: MR-Mem rejected the write itself; replaying it unchanged can never succeed.
      if (response.status >= 400 && response.status < 500) {
        console.error('memory write dropped:', response.status);
        continue;
      }
      if (!response.ok) left.push(line);
    } catch {
      left.push(line);
    }
  }
  fs.writeFileSync(retryFile, left.length ? left.join('\n') + '\n' : '');
  return lines.length - left.length;
}
