// Chunking for low-bandwidth surfaces: split on line boundaries, cap the
// number of chunks so a runaway response can't flood a metered connection.
const MAX_CHUNKS = 4;

export function chunkText(text: string, limit: number): string[] {
  const chunks: string[] = [];
  let rest = text;
  while (rest.length > 0 && chunks.length < MAX_CHUNKS) {
    if (rest.length <= limit) {
      chunks.push(rest);
      return chunks;
    }
    // Prefer the last newline inside the window; fall back to a hard cut.
    let cut = rest.lastIndexOf("\n", limit);
    if (cut < limit / 2) cut = limit;
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, "");
  }
  if (rest.length > 0) {
    chunks[chunks.length - 1] += `\n…truncated (${rest.length} more chars)`;
  }
  return chunks;
}

export function replyPrefix(
  cwd: string,
  sessionId: string | undefined,
  auto: boolean,
): string {
  const repo = cwd.split("/").filter(Boolean).pop() ?? cwd;
  const sid = sessionId ? sessionId.slice(0, 6) : "new";
  return `[${repo} · ${sid}${auto ? " · auto" : ""}]`;
}
