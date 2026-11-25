// Pure helpers for inbound attachments and crash-recovery spool compaction (no Pi runtime dependency).
import fs from 'node:fs';

export function extractAttachments(message) {
  if (!message || typeof message !== 'object') return [];
  const items = [];
  const caption = (message.caption || '').trim();
  if (message.document) {
    const d = message.document;
    items.push({
      kind: 'document',
      file_name: d.file_name || '',
      mime_type: d.mime_type || '',
      file_size: typeof d.file_size === 'number' ? d.file_size : null,
      file_ref: String(d.file_id || d.file_unique_id || ''),
      caption,
    });
  }
  if (Array.isArray(message.photo) && message.photo.length) {
    const p = message.photo[message.photo.length - 1];
    items.push({
      kind: 'photo',
      file_name: p.file_name || '',
      mime_type: p.mime_type || 'image/jpeg',
      file_size: typeof p.file_size === 'number' ? p.file_size : null,
      file_ref: String(p.file_id || p.file_unique_id || ''),
      caption,
    });
  }
  for (const kind of ['voice', 'audio', 'video']) {
    if (message[kind]) {
      const m = message[kind];
      items.push({
        kind,
        file_name: m.file_name || '',
        mime_type: m.mime_type || '',
        file_size: typeof m.file_size === 'number' ? m.file_size : null,
        file_ref: String(m.file_id || m.file_unique_id || ''),
        caption,
      });
    }
  }
  if (Array.isArray(message.attachments)) {
    for (const a of message.attachments) {
      if (a && a.file_ref) items.push(a);
    }
  }
  return items.filter(a => a.file_ref);
}

export function formatInboundText(text, attachments = []) {
  const clean = (text || '').trim();
  if (!attachments.length) return clean;
  const lines = attachments.map(a => `[附件 ${a.kind}: ${a.file_name || a.file_ref} (ref=${a.file_ref})]`);
  return [clean, ...lines].filter(Boolean).join('\n');
}

export function openSpool(spoolFile) {
  const open = new Map();
  if (!fs.existsSync(spoolFile)) return open;
  for (const line of fs.readFileSync(spoolFile, 'utf8').split('\n').filter(Boolean)) {
    try {
      const entry = JSON.parse(line);
      if (entry.done) open.delete(entry.update_id);
      else open.set(entry.update_id, entry);
    } catch {}
  }
  return open;
}

export function compactSpool(spoolFile) {
  const open = openSpool(spoolFile);
  const temp = spoolFile + '.tmp';
  fs.writeFileSync(temp, [...open.values()].map(e => JSON.stringify(e) + '\n').join(''));
  fs.renameSync(temp, spoolFile);
  return open;
}
