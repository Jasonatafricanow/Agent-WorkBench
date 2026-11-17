// Local stand-in for grammY's Bot (WORKBOT_TRANSPORT=cli): no Telegram, same gateway code path.
// Reads a script (WORKBOT_CLI_SCRIPT, one message per line) or stdin and feeds each line as a text
// message; replies and deliveries are printed. Script directives:
//   /as <external_user_id>   send the following lines as someone else (default WORKBOT_CLI_USER)
//   /sleep <seconds>         let the wake/delivery loop run
//   /approve <id> | /deny <id>  go through the approval command handler ($APPROVAL = last id printed)
//   # ...                    comment
// After each message the transport waits until the gateway finished that message's Run
// (or WORKBOT_CLI_TIMEOUT seconds). Deliveries are settled as "simulated", never as carrier receipts.
import fs from 'node:fs';
import readline from 'node:readline';

export function createCliBot(env) {
  const handlers = { text: null, edited: null, commands: [] };
  let user = env.WORKBOT_CLI_USER || '1';
  // Ids must not repeat across runs: WorkBench dedupes inbound updates by (bot, update_id).
  let updateId = Date.now(), messageId = Date.now() % 1e9;
  let replied = null;
  let lastApproval = '';
  const out = (who, text) => {
    lastApproval = (String(text).match(/apr-[0-9a-f]{32}/g) || [lastApproval]).at(-1);
    console.log(`\n<<< [${who}]\n${text}\n`);
  };
  const api = {
    sendMessage: async (chatId, text) => { out(`to chat ${chatId}`, text); return { message_id: ++messageId }; },
  };
  const ctxFor = (text) => {
    const chat = { id: Number(user) || user };
    const message = { message_id: ++messageId, text };
    return {
      chat, from: { id: chat.id }, message, update: { update_id: ++updateId },
      reply: async t => { out('reply', t); return { message_id: ++messageId }; },
      replyWithChatAction: async () => {},
    };
  };
  const finish = () => { if (replied) { const done = replied; replied = null; setTimeout(done, 1500); } };
  const waitReply = () => new Promise(resolve => {
    const timer = setTimeout(() => { replied = null; console.log('(no reply within timeout)'); resolve(); },
      Number(env.WORKBOT_CLI_TIMEOUT || 300) * 1000);
    replied = () => { clearTimeout(timer); resolve(); };
  });

  async function feed(line) {
    line = line.trim();
    if (!line || line.startsWith('#')) return;
    if (line.startsWith('/as ')) { user = line.slice(4).trim(); return; }
    line = line.replace('$APPROVAL', lastApproval); // the last approval id this run printed
    if (line.startsWith('/sleep ')) { await new Promise(r => setTimeout(r, Number(line.slice(7)) * 1000)); return; }
    console.log(`>>> [${user}] ${line}`);
    const ctx = ctxFor(line);
    const command = handlers.commands.find(c => c.names.some(n => line.startsWith('/' + n)));
    const wait = waitReply();
    if (command) { ctx.match = line.replace(/^\/\S+\s*/, ''); await command.fn(ctx); finish(); }
    else await handlers.text(ctx);
    await wait;
  }

  return {
    api,
    // The gateway calls this when it finished a message's Run (deliveries may print before that).
    turnDone: finish,
    command(names, fn) { handlers.commands.push({ names: [].concat(names), fn }); },
    on(filter, fn) { if (filter === 'message:text') handlers.text = fn; if (filter === 'edited_message:text') handlers.edited = fn; },
    catch() {},
    async start({ onStart } = {}) {
      onStart?.();
      const source = env.WORKBOT_CLI_SCRIPT ? fs.createReadStream(env.WORKBOT_CLI_SCRIPT, 'utf8') : process.stdin;
      for await (const line of readline.createInterface({ input: source })) await feed(line);
      console.log('(script done)');
      if (env.WORKBOT_CLI_EXIT !== '0') process.exit(0);
    },
  };
}
