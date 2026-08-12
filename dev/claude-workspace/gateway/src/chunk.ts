import { DEFAULT_SESSION } from "./state.ts";

// Chunking for low-bandwidth surfaces: split on line boundaries, cap the
// number of chunks so a runaway response can't flood a metered connection.
const MAX_CHUNKS = 4;

export interface Chunked {
  chunks: string[];
  /** What did not fit. Kept rather than dropped so `!more` can page it. */
  rest: string;
}

export function chunkText(text: string, limit: number): Chunked {
  const chunks: string[] = [];
  let rest = text;
  while (rest.length > 0 && chunks.length < MAX_CHUNKS) {
    if (rest.length <= limit) {
      chunks.push(rest);
      return { chunks, rest: "" };
    }
    // Prefer the last newline inside the window; fall back to a hard cut.
    let cut = rest.lastIndexOf("\n", limit);
    if (cut < limit / 2) cut = limit;
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, "");
  }
  if (rest.length > 0) {
    // Say how to get the rest. The cap is about not flooding a metered
    // connection unasked — asking is what makes it fine.
    chunks[chunks.length - 1] += `\n…+${rest.length} more chars — !more`;
  }
  return { chunks, rest };
}

/** `mode` is the chat's permission stance ("auto", "plan", or "" for the
 * default prompt-on-mutation one) — it changes what a reply means, so it rides
 * along in the banner. */
export function replyPrefix(
  cwd: string,
  sessionId: string | undefined,
  mode: string,
  session = "",
): string {
  const repo = cwd.split("/").filter(Boolean).pop() ?? cwd;
  const sid = sessionId ? sessionId.slice(0, 6) : "new";
  // Name the thread only when there is more than one to confuse it with —
  // "main" on every reply is noise.
  const named = session && session !== DEFAULT_SESSION ? `${session}/` : "";
  return `[${repo} · ${named}${sid}${mode ? ` · ${mode}` : ""}]`;
}
