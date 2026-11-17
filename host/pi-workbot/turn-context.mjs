// Per-run context shared between the gateway, the tools and the guard extension.
// The identity is the verified Telegram sender (or the agent itself for timer runs);
// the model never chooses it.
const turns = new Map(); // sessionId -> { identity, wakeEventId, taskId, sessionFile, sourceRef, updateId }
globalThis.__workbotTurns = turns;
export const setTurn = (sessionId, turn) => turns.set(sessionId, turn);
export const getTurn = sessionId => turns.get(sessionId);
export const clearTurn = sessionId => turns.delete(sessionId);
