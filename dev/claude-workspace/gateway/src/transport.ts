import { chunkText } from "./chunk.ts";
import { config } from "./config.ts";

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
  /** Resolves to the ref the NEXT edit should target, or undefined to keep
   * targeting the current one. Signal makes each revision its own message and
   * only tracks the newest as editable, so it hands back the new timestamp;
   * WhatsApp keeps addressing the original key and returns nothing. */
  edit?(chatKey: string, target: MsgRef, text: string): Promise<MsgRef | void>;
  /** The fastest the live status message may be redrawn on this surface, in
   * ms. A floor, not a cadence: how often it actually redraws is the chat's
   * verbosity (status.ts). Absent means the shared default — a surface only
   * sets this if its own rate limits differ, which on an unofficial client
   * they very much do. */
  minEditIntervalMs?: number;
  /** A stable string identity for a ref, so an inbound reaction can be matched
   * against the outbound message it points at. The two arrive in different
   * shapes on both networks — Signal gives a target timestamp rather than the
   * ref we kept, WhatsApp a bare key id — so equality has to be the
   * transport's business, not a deep compare out here. */
  refId?(ref: MsgRef): string | undefined;
  /** Deliver a file. Optional: a surface without it says so rather than
   * silently dropping what claude tried to send. */
  sendFile?(chatKey: string, file: string, caption: string): Promise<void>;
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
  for (const chunk of chunkText(text, t.chunkLimit).chunks) {
    const ref = await t.send(chatKey, chunk);
    first ??= ref;
  }
  return first;
}

// What a reply had to leave out, per chat. Only long-form output — claude's
// answers and !bash — feeds this; a `!status` reply in between must not throw
// away the page you were about to ask for.
const overflow = new Map<string, string>();

/**
 * Send long-form output and remember what didn't fit, so `!more` can page it.
 * Paging chains naturally: the next page goes out through here too and leaves
 * its own remainder behind.
 */
export async function sendReply(
  chatKey: string,
  text: string,
): Promise<MsgRef | undefined> {
  const t = transportFor(chatKey);
  if (!t) {
    console.error(`no transport for ${chatKey}`);
    return undefined;
  }
  const { chunks, rest } = chunkText(text, t.chunkLimit);
  if (rest) overflow.set(chatKey, rest);
  else overflow.delete(chatKey);

  let first: MsgRef | undefined;
  for (const chunk of chunks) {
    const ref = await t.send(chatKey, chunk);
    first ??= ref;
  }
  return first;
}

/** The unsent remainder, cleared as it is handed over. */
export function takeOverflow(chatKey: string): string {
  const rest = overflow.get(chatKey) ?? "";
  overflow.delete(chatKey);
  return rest;
}

export function overflowSize(chatKey: string): number {
  return overflow.get(chatKey)?.length ?? 0;
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

/** The outcome of one edit: whether it happened at all — the caller may want
 * to fall back to sending a fresh message (WhatsApp refuses edits past ~15
 * minutes) — and what the next edit should target. */
export interface EditResult {
  ok: boolean;
  next?: MsgRef;
}

export async function editMsg(
  chatKey: string,
  target: MsgRef | undefined,
  text: string,
): Promise<EditResult> {
  const t = transportFor(chatKey);
  if (!t?.edit || target === undefined) return { ok: false };
  try {
    const next = await t.edit(chatKey, target, clampToLimit(text, t.chunkLimit));
    return { ok: true, next: next ?? undefined };
  } catch (err) {
    console.warn(`edit failed on ${chatKey}: ${(err as Error).message}`);
    return { ok: false };
  }
}

/** Identity of a ref on its own surface, for matching inbound reactions. */
export function refIdOf(chatKey: string, ref: MsgRef | undefined): string | undefined {
  if (ref === undefined) return undefined;
  return transportFor(chatKey)?.refId?.(ref);
}

/** Deliver a file, reporting rather than swallowing a failure — an attachment
 * that never arrives with no word is worse than an error message. */
export async function sendFileTo(
  chatKey: string,
  file: string,
  caption = "",
): Promise<string | undefined> {
  const t = transportFor(chatKey);
  if (!t) return "no transport";
  if (!t.sendFile) return "this surface can't send files";
  try {
    await t.sendFile(chatKey, file, caption);
    return undefined;
  } catch (err) {
    return (err as Error).message;
  }
}

export function canEdit(chatKey: string): boolean {
  return Boolean(transportFor(chatKey)?.edit);
}

/** The fastest this chat's surface will redraw a status message. */
export function minEditIntervalFor(chatKey: string): number {
  return (
    transportFor(chatKey)?.minEditIntervalMs ?? config.progress.minEditIntervalMs
  );
}
