// Happy 1.2.5 persists startup metadata before the agent has a transcript ID.
// Read authoritative metadata with Happy's own credential/decryption adapters.
// Never print credentials, encrypted payloads, or conversation messages.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

try {
  const root = process.env.HAPPY_PACKAGE_ROOT ?? "/usr/local/lib/node_modules/happy";
  if (JSON.parse(fs.readFileSync(path.join(root, "package.json"))).version !== "1.2.5")
    throw new Error("unsupported Happy version");
  const dist = path.join(root, "dist");
  const load = async (prefix) => {
    const files = fs.readdirSync(dist).filter(name => name.startsWith(prefix) && name.endsWith(".mjs"));
    if (files.length !== 1) throw new Error("ambiguous Happy adapter");
    return import(pathToFileURL(path.join(dist, files[0])).href);
  };
  const { c: configuration, z: decrypt, t: decodeBase64 } = await load("types-");
  const { readCredentials, readPersistedSessions } = await load("persistence-");
  const id = process.argv[2];
  const saved = readPersistedSessions()[id];
  const credentials = await readCredentials();
  if (!saved || !credentials) throw new Error("missing reconnect credentials");
  const response = await fetch(`${configuration.serverUrl}/v1/sessions`, {
    headers: {
      Authorization: `Bearer ${credentials.token}`,
      "X-Happy-Client": `cli-coding-session/${configuration.currentCliVersion}`,
    },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error("metadata lookup failed");
  const session = (await response.json()).sessions?.find(session => session.id === id);
  if (!session) throw new Error("session unavailable");
  const metadata = decrypt(decodeBase64(saved.encryptionKey), saved.encryptionVariant,
    decodeBase64(session.metadata));
  if (!metadata || typeof metadata !== "object") throw new Error("invalid metadata");
  const selected = {};
  for (const key of ["path", "host", "flavor", "claudeSessionId", "codexThreadId",
    "lifecycleState", "dangerouslySkipPermissions", "sandbox"]) {
    if (metadata[key] !== undefined) selected[key] = metadata[key];
  }
  console.log(JSON.stringify({ metadata: selected }));
} catch {
  console.error("Happy recovery metadata lookup unavailable");
  process.exitCode = 1;
}
