import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { compactSpool, extractAttachments, formatInboundText, openSpool } from '../host/pi-workbot/intake-helpers.mjs';
import { retryMemoryWrites } from '../host/pi-workbot/memory-retry.mjs';

test('extractAttachments handles photo, document, voice and formats prompt text', () => {
  const msg = {
    caption: '请收补充材料',
    photo: [{ file_id: 'small-1', file_size: 100 }, { file_id: 'large-2', file_size: 900 }],
    document: { file_id: 'doc-1', file_name: 'invoice.pdf', mime_type: 'application/pdf', file_size: 4096 },
  };
  const atts = extractAttachments(msg);
  assert.equal(atts.length, 2);
  assert.equal(atts[0].kind, 'document');
  assert.equal(atts[0].file_ref, 'doc-1');
  assert.equal(atts[1].kind, 'photo');
  assert.equal(atts[1].file_ref, 'large-2');

  const formatted = formatInboundText(msg.caption, atts);
  assert.match(formatted, /请收补充材料/);
  assert.match(formatted, /\[附件 document: invoice\.pdf \(ref=doc-1\)\]/);
  assert.match(formatted, /\[附件 photo: large-2 \(ref=large-2\)\]/);
});

test('compactSpool preserves unfinished updates and drops done entries', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wb-spool-'));
  const spoolFile = path.join(dir, 'inbound.jsonl');
  fs.writeFileSync(spoolFile, [
    JSON.stringify({ update_id: 'u1', text: 'first', attachments: [] }),
    JSON.stringify({ update_id: 'u2', text: 'second', attachments: [{ kind: 'photo', file_ref: 'p2' }] }),
    JSON.stringify({ update_id: 'u1', done: true }),
  ].join('\n') + '\n');

  const open = compactSpool(spoolFile);
  assert.equal(open.size, 1);
  assert.equal(open.get('u2').text, 'second');
  assert.equal(openSpool(spoolFile).size, 1);
  assert.doesNotMatch(fs.readFileSync(spoolFile, 'utf8'), /first/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('retryMemoryWrites handles 200 ok, 403 revoked grant, 4xx drop, and 5xx retain', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wb-mem-'));
  const retryFile = path.join(dir, 'memory-retry.jsonl');
  const rows = [
    { path: '/upsert', identity: { external_user_id: 'ok-agent' }, body: { id: 'm1', text: 'sent ok' } },
    { path: '/upsert', identity: { external_user_id: 'revoked-agent' }, body: { id: 'm2', text: 'revoked' } },
    { path: '/upsert', identity: { external_user_id: 'ok-agent' }, body: { id: 'm3', text: 'bad payload' } },
    { path: '/upsert', identity: { external_user_id: 'ok-agent' }, body: { id: 'm4', text: 'transient fail' } },
  ];
  fs.writeFileSync(retryFile, rows.map(r => JSON.stringify(r)).join('\n') + '\n');

  const received = [];
  const memServer = http.createServer((req, res) => {
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      const payload = JSON.parse(raw);
      received.push(payload.id);
      if (payload.id === 'm1') { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"ok":true}'); }
      else if (payload.id === 'm3') { res.writeHead(422); res.end('invalid'); }
      else { res.writeHead(503); res.end('busy'); }
    });
  });
  await new Promise(resolve => memServer.listen(0, '127.0.0.1', resolve));
  const memUrl = `http://127.0.0.1:${memServer.address().port}`;

  try {
    const processed = await retryMemoryWrites({
      retryFile,
      memUrl,
      memToken: 'tok',
      authorize: async identity => {
        if (identity.external_user_id === 'revoked-agent') {
          const err = new Error('denied');
          err.status = 403;
          throw err;
        }
        return { allowed: true };
      },
    });
    assert.equal(processed, 3); // m1 ok, m2 revoked drop, m3 4xx drop; only m4 retained
    assert.deepEqual(received, ['m1', 'm3', 'm4']);
    const remaining = fs.readFileSync(retryFile, 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].body.id, 'm4');
  } finally {
    memServer.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
