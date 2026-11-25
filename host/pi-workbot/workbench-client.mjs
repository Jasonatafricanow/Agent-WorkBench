// Minimal client for the WorkBench Host API. Token and URL come from env; never logged.
export const WORKSPACE = process.env.WORKBENCH_WORKSPACE || '';

export class WorkbenchError extends Error {
  constructor(status, error) { super(error?.message || `HTTP ${status}`); this.status = status; this.code = error?.code; this.details = error; }
}

export async function wb(path, body = {}, { timeoutMs = 15000 } = {}) {
  const base = (process.env.WORKBENCH_URL || 'http://127.0.0.1:8790').replace(/\/$/, '');
  const token = process.env.WORKBENCH_HOST_TOKEN || '';
  const workspace = process.env.WORKBENCH_WORKSPACE || WORKSPACE;
  const response = await fetch(base + path, {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ workspace_id: workspace, ...body }),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) throw new WorkbenchError(response.status, data.error || data);
  return data.result;
}
