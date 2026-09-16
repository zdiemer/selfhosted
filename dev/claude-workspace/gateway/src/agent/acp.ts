import { type ChildProcess, spawn } from "node:child_process";

// A minimal ACP (Agent Client Protocol) client: JSON-RPC 2.0, one message per
// line, over a child process's stdio. Verified against the real thing rather
// than written from the spec — `printf '<initialize>' | muse-acp` and the same
// through `gemini --acp` both answer:
//
//   {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,
//     "agentCapabilities":{"loadSession":true,...},"agentInfo":{...}}}
//
// Why ACP and not each CLI's own headless mode: it is the only one of the
// available protocols with a permission request in it. `codex exec --json` and
// `muse exec --json` can only be run pre-approved, which on a pod holding
// cluster-admin is not a mode worth offering from a phone. ACP's
// session/request_permission is what lets every agent reach the same "reply
// 1/2/3" prompt claude has always used.

export const PROTOCOL_VERSION = 1;

interface Rpc {
  jsonrpc: "2.0";
  id?: number | string;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

/** What `initialize` tells us about the far end. */
export interface AgentCapabilities {
  /** session/load is available, so a thread can be resumed. When false, every
   * run starts fresh and the gateway says so rather than pretending. */
  loadSession?: boolean;
}

/**
 * A session configuration option, as `session/new` and `session/load` publish
 * them. This is ACP's standard way to pick a model — it replaced the unstable
 * `session/set_model` — and it is how `!model` and `!effort` reach an agent
 * that has no CLI flag we can pass.
 *
 * ⚠️ The identifier field is `id` on the wire. The published spec calls it
 * `configId`, and muse-acp 0.4.3 emits `id`; both are read, because being wrong
 * about this means silently never matching anything.
 *
 * Categories seen in the wild (muse-acp): `model`, `thought_level` (reasoning
 * effort) and `mode` (its own approval enforcement — left alone, since this
 * gateway does permissions itself and muse's default routes them to us).
 */
export interface ConfigOption {
  id?: string;
  configId?: string;
  name?: string;
  description?: string;
  category?: string;
  type?: string;
  currentValue?: string;
  options?: { value: string; name?: string; description?: string }[];
}

export const MODEL_CATEGORY = "model";
export const EFFORT_CATEGORY = "thought_level";

export function optionId(o: ConfigOption): string {
  return o.id ?? o.configId ?? "";
}

/** The advertised option for a category, if the agent offers one. */
export function optionByCategory(
  options: ConfigOption[] | undefined,
  category: string,
): ConfigOption | undefined {
  return (options ?? []).find((o) => o.category === category);
}

/** The values an option will accept. Empty is meaningful rather than broken:
 * muse-acp advertises `model` with an empty list until `muse login` has
 * happened, because it refreshes the catalog from the host. */
export function optionValues(o: ConfigOption | undefined): string[] {
  return (o?.options ?? []).map((v) => v.value);
}

export interface AcpHandlers {
  /** A session/update notification's `update` object, already unwrapped. */
  onUpdate(update: Record<string, unknown>): void;
  /** session/request_permission. Resolve with the chosen optionId, or null to
   * report the request cancelled. */
  onPermission(params: PermissionRequest): Promise<string | null>;
  /** The process died. Any in-flight request is already rejected by then. */
  onExit(code: number | null, stderr: string): void;
}

export interface PermissionOption {
  optionId: string;
  name?: string;
  kind?: "allow_once" | "allow_always" | "reject_once" | "reject_always";
}

export interface PermissionRequest {
  sessionId?: string;
  toolCall?: {
    toolCallId?: string;
    title?: string;
    kind?: string;
    rawInput?: unknown;
  };
  options?: PermissionOption[];
}

export class AcpClient {
  private child: ChildProcess;
  private nextId = 1;
  private waiting = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();
  private buf = "";
  private stderr = "";
  private dead = false;

  capabilities: AgentCapabilities = {};

  constructor(
    command: string,
    args: string[],
    cwd: string,
    env: NodeJS.ProcessEnv,
    private handlers: AcpHandlers,
  ) {
    this.child = spawn(command, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stdout?.on("data", (d: Buffer) => this.feed(d.toString("utf8")));
    // Every adapter uses stderr for diagnostics rather than protocol —
    // muse-acp prints its schema-compat lines there, gemini its trust warnings
    // — so it is kept for the death notice and otherwise ignored. Bounded: a
    // chatty agent must not grow this without limit.
    this.child.stderr?.on("data", (d: Buffer) => {
      this.stderr = (this.stderr + d.toString("utf8")).slice(-4000);
    });
    // An agent that dies during startup makes the first write an EPIPE. It has
    // to be handled here; an unhandled 'error' on the stream takes the whole
    // gateway down with it.
    this.child.stdin?.on("error", () => {});
    this.child.on("error", (e) => this.die(null, String(e)));
    this.child.on("close", (code) => this.die(code, this.stderr));
  }

  private die(code: number | null, stderr: string): void {
    if (this.dead) return;
    this.dead = true;
    for (const { reject } of this.waiting.values())
      reject(new Error(`agent exited (${code ?? "signal"})`));
    this.waiting.clear();
    this.handlers.onExit(code, stderr);
  }

  private feed(chunk: string): void {
    this.buf += chunk;
    let nl: number;
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (!line) continue;
      let msg: Rpc;
      try {
        msg = JSON.parse(line) as Rpc;
      } catch {
        // Not protocol. Some agents print a banner before the first message;
        // dropping it is better than tearing the run down over it.
        continue;
      }
      this.dispatch(msg);
    }
  }

  private dispatch(msg: Rpc): void {
    // A response to something we asked.
    if (msg.id !== undefined && msg.method === undefined) {
      const w = this.waiting.get(msg.id as number);
      if (!w) return;
      this.waiting.delete(msg.id as number);
      if (msg.error) w.reject(new Error(msg.error.message));
      else w.resolve(msg.result);
      return;
    }
    // A request FROM the agent — it wants something from us.
    if (msg.id !== undefined && msg.method) {
      void this.serve(msg);
      return;
    }
    // A notification.
    if (msg.method === "session/update") {
      const update = (msg.params as { update?: Record<string, unknown> })
        ?.update;
      if (update) this.handlers.onUpdate(update);
    }
  }

  private async serve(msg: Rpc): Promise<void> {
    if (msg.method === "session/request_permission") {
      const params = msg.params as PermissionRequest;
      const optionId = await this.handlers.onPermission(params);
      this.send({
        jsonrpc: "2.0",
        id: msg.id,
        result: optionId
          ? { outcome: { outcome: "selected", optionId } }
          : { outcome: { outcome: "cancelled" } },
      });
      return;
    }
    // We advertise no filesystem and no terminal, so nothing else should
    // arrive. Answer with an error rather than silence: an agent left waiting
    // on a reply that never comes hangs the whole run.
    this.send({
      jsonrpc: "2.0",
      id: msg.id,
      error: { code: -32601, message: `${msg.method} is not supported` },
    });
  }

  private send(msg: Rpc): void {
    if (this.dead) return;
    this.child.stdin?.write(JSON.stringify(msg) + "\n");
  }

  request<T = unknown>(method: string, params?: unknown): Promise<T> {
    if (this.dead) return Promise.reject(new Error("agent is not running"));
    const id = this.nextId++;
    const p = new Promise<T>((resolve, reject) => {
      this.waiting.set(id, {
        resolve: resolve as (v: unknown) => void,
        reject,
      });
    });
    this.send({ jsonrpc: "2.0", id, method, params });
    return p;
  }

  notify(method: string, params?: unknown): void {
    this.send({ jsonrpc: "2.0", method, params });
  }

  /** The handshake. Declares what this client can do — which is deliberately
   * very little: no filesystem, no terminal. The gateway is a chat relay, not
   * an editor, and an agent told the client has no file tools uses its own,
   * which is what we want it doing anyway. */
  async initialize(): Promise<void> {
    const res = await this.request<{
      protocolVersion?: number;
      agentCapabilities?: AgentCapabilities;
    }>("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      clientCapabilities: {
        fs: { readTextFile: false, writeTextFile: false },
        terminal: false,
      },
    });
    this.capabilities = res?.agentCapabilities ?? {};
  }

  /** Change one config option. The response carries the complete updated list,
   * so the caller can refresh what it knows from the same round trip. */
  async setConfigOption(
    sessionId: string,
    configId: string,
    value: string,
  ): Promise<ConfigOption[] | undefined> {
    const res = await this.request<{ configOptions?: ConfigOption[] }>(
      "session/set_config_option",
      { sessionId, configId, type: "id", value },
    );
    return res?.configOptions;
  }

  kill(): void {
    this.child.kill("SIGTERM");
  }

  get alive(): boolean {
    return !this.dead;
  }
}
