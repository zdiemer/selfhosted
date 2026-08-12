import { chunkText } from "./chunk.ts";

// The outbound side of a chat surface, split out of router.ts so the status
// message (status.ts) can reach it without importing the inbound dispatcher.
//
// Only `send` is required. Reactions and edits are what Signal and WhatsApp
// happen to support and the stdin dev transport fakes; a surface that can do
// neither still works, it just loses the acknowledgement and the live status.

/**
 * Opaque, transport-owned handle to one message. Signal uses
 * `{ts, author}` (a reaction needs both), WhatsApp a Baileys `WAMessageKey`.
 * Nothing outside a transport may look inside one.
 */
export type MsgRef = unknown;

export interface Transport {
  /** Chunk limit per message for this surface. */
  chunkLimit: number;
  /** Resolves to a handle on the sent message, or undefined if the surface
   * can't identify one — which only costs the caller an edit target. */
  send(chatKey: string, text: string): Promise<MsgRef | undefined>;
  /** Replace this author's reaction on `target`. Both networks treat a second
   * reaction from the same account as a replacement, so there is no separate
   * "clear" step; `remove` is for taking the last one back entirely. */
  react?(
    chatKey: string,
    target: MsgRef,
    emoji: string,
    remove?: boolean,
  ): Promise<void>;
  edit?(chatKey: string, target: MsgRef, text: string): Promise<void>;
}

const transports = new Map<string, Transport>();

export function registerTransport(prefix: string, t: Transport): void {
  transports.set(prefix, t);
}

function transportFor(chatKey: string): Transport | undefined {
  return transports.get(chatKey.split(":", 1)[0]);
}

// Registering a transport is not the same as being able to send on it: the
// signal-cli socket and the Baileys websocket both connect asynchronously, and
// anything sent before they do is dropped on the floor. The restart notice in
// particular has exactly one chance to go out, so it waits for this.
const ready = new Set<string>();
const readyHandlers: ((prefix: string) => void)[] = [];

/** Called by a transport once it can actually deliver. Only the first connect
 * counts — a reconnect is not a restart and must not re-announce. */
export function markReady(prefix: string): void {
  if (ready.has(prefix)) return;
  ready.add(prefix);
  for (const h of readyHandlers) h(prefix);
}

export function onTransportReady(h: (prefix: string) => void): void {
  readyHandlers.push(h);
  for (const p of ready) h(p);
}

/** Send a reply, split across as many messages as the surface needs. Returns
 * the first chunk's handle — the later ones are unaddressable by design, which
 * is why the status message never goes through here. */
export async function sendTo(
  chatKey: string,
  text: string,
): Promise<MsgRef | undefined> {
  const t = transportFor(chatKey);
  if (!t) {
    console.error(`no transport for ${chatKey}`);
    return undefined;
  }
  let first: MsgRef | undefined;
  for (const chunk of chunkText(text, t.chunkLimit)) {
    const ref = await t.send(chatKey, chunk);
    first ??= ref;
  }
  return first;
}

/** Send exactly one message, truncating rather than splitting. An edit target
 * has to stay a single message, so the status line can never be chunked. */
export async function sendOne(
  chatKey: string,
  text: string,
): Promise<MsgRef | undefined> {
  const t = transportFor(chatKey);
  if (!t) {
    console.error(`no transport for ${chatKey}`);
    return undefined;
  }
  return t.send(chatKey, clampToLimit(text, t.chunkLimit));
}

export function clampToLimit(text: string, limit: number): string {
  return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
}

/**
 * Best-effort acknowledgement. A failed reaction or edit must never break
 * message handling — same contract as the read receipts these sit alongside
 * (signal.ts `sendReadReceipt`, whatsapp.ts `markRead`).
 */
export async function reactTo(
  chatKey: string,
  target: MsgRef | undefined,
  emoji: string,
  remove = false,
): Promise<void> {
  const t = transportFor(chatKey);
  if (!t?.react || target === undefined) return;
  try {
    await t.react(chatKey, target, emoji, remove);
  } catch (err) {
    console.warn(`react failed on ${chatKey}: ${(err as Error).message}`);
  }
}

/** Returns false if the edit didn't happen — the caller may want to fall back
 * to sending a fresh message (WhatsApp refuses edits past ~15 minutes). */
export async function editMsg(
  chatKey: string,
  target: MsgRef | undefined,
  text: string,
): Promise<boolean> {
  const t = transportFor(chatKey);
  if (!t?.edit || target === undefined) return false;
  try {
    await t.edit(chatKey, target, clampToLimit(text, t.chunkLimit));
    return true;
  } catch (err) {
    console.warn(`edit failed on ${chatKey}: ${(err as Error).message}`);
    return false;
  }
}

export function canEdit(chatKey: string): boolean {
  return Boolean(transportFor(chatKey)?.edit);
}
