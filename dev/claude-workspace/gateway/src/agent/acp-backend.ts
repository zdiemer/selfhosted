import { requestApproval } from "../approvals.ts";
import type {
  RunHooks,
  RunOverrides,
  RunResult,
  StreamEvent,
} from "../claude.ts";
import { config } from "../config.ts";
import {
  getChat,
  resumeIdForSlot,
  sessionPatchForSlot,
  threadOf,
  threadPatch,
  updateChat,
} from "../state.ts";
import {
  AcpClient,
  type ConfigOption,
  EFFORT_CATEGORY,
  MODEL_CATEGORY,
  optionByCategory,
  optionId,
  optionValues,
  type PermissionRequest,
} from "./acp.ts";
import type { AgentBackend, AgentId } from "./backend.ts";
import { acquireSlot, releaseSlot } from "./slots.ts";

// An AgentBackend built on an ACP server. One of these per provider; they
// differ only by the ProviderSpec below, because ACP is the whole point — the
// per-CLI knowledge stops at "what do I exec".

export interface ProviderSpec {
  id: AgentId;
  label: string;
  /** argv[0]; also what the startup check looks for on PATH. */
  command: string;
  args: string[];
  /** Chart-configurable default, read lazily so an env change needs no rebuild.
   * Now genuinely applied at session start rather than merely displayed. */
  defaultModel: () => string;
}

interface Live {
  client: AcpClient;
  sessionId?: string;
}

const live = new Map<string, Live>();

/**
 * The last config options each chat's agent published, keyed the same way as
 * `live`. Kept after the run ends because `!model` is asked BETWEEN runs, when
 * there is no session to ask — and an agent's catalogue is a property of the
 * agent and its login, not of the turn.
 *
 * In memory only. A pod restart loses it, and the first message afterwards
 * fills it in again; the cost of being wrong is a list we cannot show yet, not
 * a wrong answer.
 */
const knownOptions = new Map<string, ConfigOption[]>();

/** ACP has no background tasks and no self-wake: a turn ends when
 * session/prompt resolves, and the process has nothing left to do. So none of
 * claude's parking machinery applies, and the honest answers here are the
 * empty ones. */
const NO_BACKGROUND = {
  isWaiting: () => false,
  waitingOn: () => [],
  hasLiveWakeup: () => false,
  handOff: () => false,
};

export function makeAcpBackend(spec: ProviderSpec): AgentBackend {
  const key = (chatKey: string) => `${spec.id}:${chatKey}`;

  return {
    id: spec.id,
    label: spec.label,
    command: spec.command,
    // No hardcoded aliases: the agent publishes its own catalogue (choicesFor).
    models: {},
    get defaultModel() {
      return spec.defaultModel();
    },

    ...NO_BACKGROUND,

    isRunning: (chatKey) => live.has(key(chatKey)),
    runningChats: () =>
      [...live.keys()]
        .filter((k) => k.startsWith(`${spec.id}:`))
        .map((k) => k.slice(spec.id.length + 1)),
    stop(chatKey) {
      const l = live.get(key(chatKey));
      if (!l) return false;
      // Cancel first so the agent can stop cleanly and flush whatever it has;
      // the kill is the backstop for one that ignores it.
      if (l.sessionId) l.client.notify("session/cancel", { sessionId: l.sessionId });
      l.client.kill();
      return true;
    },

    // Neither transcript hook applies: an ACP agent's history lives wherever
    // that CLI keeps it, in a format this gateway does not read. `!resume` with
    // no id therefore says "no sessions found" rather than silently handing
    // back claude's, and the transcript-health nudges stay quiet.
    latestSessionId: () => undefined,
    resumableBytes: () => 0,

    // model and effort are whatever the agent last said it offers; the rest are
    // Claude Code flags with no ACP equivalent at all. Optimistic before the
    // first run: an agent we have never spoken to gets the benefit of the
    // doubt, because refusing `!model` on a guess would be its own small lie.
    supportsFor(chatKey) {
      const opts = knownOptions.get(key(chatKey));
      const has = (category: string) =>
        opts ? Boolean(optionByCategory(opts, category)) : true;
      return {
        model: has(MODEL_CATEGORY),
        effort: has(EFFORT_CATEGORY),
        plan: false,
        systemPrompt: false,
        wakeup: false,
        backgroundTasks: false,
        usage: false,
        projectMcp: false,
      };
    },

    choicesFor(chatKey, kind) {
      const opt = optionByCategory(
        knownOptions.get(key(chatKey)),
        kind === "model" ? MODEL_CATEGORY : EFFORT_CATEGORY,
      );
      return { values: opt?.options ?? [], current: opt?.currentValue };
    },

    run: (chatKey, message, contextPrefix, hooks, run_) =>
      runAcp(spec, chatKey, message, contextPrefix, hooks, run_),
  };
}

async function runAcp(
  spec: ProviderSpec,
  chatKey: string,
  message: string,
  contextPrefix: string,
  hooks: RunHooks,
  run_?: RunOverrides,
): Promise<RunResult> {
  const chat = getChat(chatKey);
  const cwd = run_?.cwd ?? chat.cwd;
  const thread = threadOf(chat, spec.id);
  const k = `${spec.id}:${chatKey}`;

  // There is no --append-system-prompt over ACP, so the harness prompt rides in
  // front of the first message of a session instead — the same text, one turn
  // later than claude gets it. Only on a fresh session: repeating it every turn
  // would spend context saying the same thing.
  const resumeId = run_?.session
    ? run_.fresh
      ? undefined
      : resumeIdForSlot(thread, run_.session)
    : thread.sessionId;

  const parts = [contextPrefix, message].filter(Boolean);
  if (!resumeId && config.systemPrompt) parts.unshift(config.systemPrompt);
  const prompt = parts.join("\n\n");

  acquireSlot();
  updateChat(chatKey, { inFlight: true });

  let text = "";
  let lastAssistant = "";
  let died: string | null = null;

  const emit = (ev: StreamEvent) => {
    try {
      hooks.onEvent?.(ev);
    } catch {
      // A status-message failure must not take the run with it.
    }
  };

  const client = new AcpClient(
    spec.command,
    spec.args,
    cwd,
    { ...process.env, HOME: config.home },
    {
      onUpdate: (u) => {
        const ev = toStreamEvent(u);
        if (!ev) return;
        const t = assistantTextOf(ev);
        if (t) {
          text += t;
          lastAssistant = text;
        }
        emit(ev);
      },
      onPermission: (params) => answerPermission(chatKey, spec.label, params),
      onExit: (_code, stderr) => {
        died = stderr.trim().split("\n").slice(-3).join("\n");
      },
    },
  );

  live.set(k, { client });

  try {
    await client.initialize();

    // session/load only when the agent says it can; otherwise a resume would
    // be an error where a fresh thread is a working conversation.
    let sessionId: string | undefined;
    // Both session/new and session/load publish the agent's config options, so
    // a resumed thread can still change model.
    let options: ConfigOption[] | undefined;
    if (resumeId && client.capabilities.loadSession) {
      try {
        const res = await client.request<{ configOptions?: ConfigOption[] }>(
          "session/load",
          { sessionId: resumeId, cwd, mcpServers: [] },
        );
        sessionId = resumeId;
        options = res?.configOptions;
      } catch {
        sessionId = undefined; // the thread is gone on their side; start over
      }
    }
    if (!sessionId) {
      const res = await client.request<{
        sessionId?: string;
        configOptions?: ConfigOption[];
      }>("session/new", { cwd, mcpServers: [] });
      sessionId = res?.sessionId;
      options = res?.configOptions;
    }
    if (!sessionId) throw new Error("agent returned no session id");
    // Recorded even when the agent advertised NOTHING (`?? []`), because "we
    // asked and it offers no model option" is exactly what supportsFor needs to
    // know in order to refuse !model honestly. Leaving it unset would keep the
    // chat optimistic forever.
    knownOptions.set(k, options ?? []);

    // Persisted the moment it exists, before the prompt can be interrupted —
    // same reasoning as claude.ts: a run killed by a redeploy must stay
    // resumable.
    const l = live.get(k);
    if (l) l.sessionId = sessionId;
    persistSession(chatKey, spec.id, sessionId, run_);
    emit({ type: "system", subtype: "init", session_id: sessionId });

    // Apply the chat's model and effort. Precedence matches claude.ts:411-412 —
    // a schedule's pin beats the chat's setting beats the chart default — and
    // this is the step whose absence made `!model` a no-op that still answered
    // "✓ applies to the next message".
    options = await applyOption(
      client,
      sessionId,
      options,
      MODEL_CATEGORY,
      run_?.model ?? thread.model ?? spec.defaultModel(),
      spec.label,
    );
    options = await applyOption(
      client,
      sessionId,
      options,
      EFFORT_CATEGORY,
      run_?.effort ?? chat.effort,
      spec.label,
    );
    knownOptions.set(k, options ?? []);

    const res = await client.request<{ stopReason?: string }>("session/prompt", {
      sessionId,
      prompt: [{ type: "text", text: prompt }],
    });

    const cancelled = res?.stopReason === "cancelled";
    const result: RunResult = {
      text: text.trim() || lastAssistant.trim(),
      sessionId,
      isError: false,
      aborted: cancelled || undefined,
    };
    emit({ type: "result", session_id: sessionId, result: result.text });
    if (!cancelled) await hooks.onTurn?.(result, 1, []);
    return result;
  } catch (e) {
    // A dead process is the common failure and its stderr is the only clue
    // there is, so it goes to the chat rather than only the log.
    const why = died || (e instanceof Error ? e.message : String(e));
    console.error(`${spec.id}: ${why}`);
    const result: RunResult = {
      text: `⚠ ${spec.label} failed: ${why}`,
      isError: true,
      aborted: true,
    };
    return result;
  } finally {
    live.delete(k);
    client.kill();
    releaseSlot();
    updateChat(chatKey, { inFlight: false });
  }
}

/**
 * Select one config option for this session, and hand back the refreshed list.
 *
 * Three ways to do nothing, all of them deliberate:
 *  - no value wanted, or it already matches `currentValue` — no round trip;
 *  - the agent advertises no option of that category — it cannot be set, and
 *    supportsFor() will already be telling the chat so;
 *  - the agent advertises the option with an EMPTY value list. muse-acp does
 *    exactly this until `muse login` has refreshed its catalogue from the host,
 *    so an empty list means "not known yet", not "nothing is valid". We try the
 *    set anyway and let the agent be the judge.
 *
 * A value the agent lists but refuses, or one it does not list, throws — the
 * run fails loudly rather than quietly answering on some other model, which is
 * the whole point of this change.
 */
async function applyOption(
  client: AcpClient,
  sessionId: string,
  options: ConfigOption[] | undefined,
  category: string,
  want: string | undefined,
  label: string,
): Promise<ConfigOption[] | undefined> {
  if (!want) return options;
  const opt = optionByCategory(options, category);
  if (!opt) return options;
  if (opt.currentValue === want) return options;

  const values = optionValues(opt);
  if (values.length && !values.includes(want)) {
    throw new Error(
      `${label} does not offer ${category} "${want}" — it offers ${values.join(", ")}`,
    );
  }
  return (await client.setConfigOption(sessionId, optionId(opt), want)) ?? options;
}

function persistSession(
  chatKey: string,
  backend: AgentId,
  sessionId: string,
  run_?: RunOverrides,
): void {
  const chat = getChat(chatKey);
  updateChat(
    chatKey,
    run_?.session
      ? sessionPatchForSlot(chat, backend, run_.session, sessionId)
      : threadPatch(chat, backend, { sessionId }),
  );
}

/**
 * Answer a session/request_permission through the same relay claude uses, then
 * translate the verdict back into one of the options the agent offered.
 *
 * The agent names its own options, so the mapping is by `kind` with a
 * name-based fallback — an agent that offers something unexpected still gets a
 * usable answer rather than a protocol error.
 */
async function answerPermission(
  chatKey: string,
  label: string,
  params: PermissionRequest,
): Promise<string | null> {
  const options = params.options ?? [];
  if (!options.length) return null;

  const pick = (...kinds: string[]): string | undefined =>
    options.find((o) => o.kind && kinds.includes(o.kind))?.optionId;

  // Tool identity for the prompt and for the allowlist: ACP gives a human
  // title and a coarse kind rather than claude's tool name, and the title is
  // what actually tells you what is about to happen.
  const toolName = params.toolCall?.title || params.toolCall?.kind || "a tool";
  const input = params.toolCall?.rawInput ?? {};

  const { promise } = requestApproval(chatKey, toolName, input, label);
  const verdict = await promise;

  if (verdict.behavior === "allow")
    return (
      pick("allow_once", "allow_always") ??
      options.find((o) => /allow|yes|proceed/i.test(o.name ?? ""))?.optionId ??
      options[0].optionId
    );
  return (
    pick("reject_once", "reject_always") ??
    options.find((o) => /reject|deny|no/i.test(o.name ?? ""))?.optionId ??
    null
  );
}

/**
 * ACP session/update → the StreamEvent shape the rest of the gateway already
 * speaks, so status.ts and router.ts need no idea another protocol exists.
 * status.ts's toolLabel() falls through to a short form for names it doesn't
 * know, which is what makes an ACP tool title render sensibly.
 */
export function toStreamEvent(
  u: Record<string, unknown>,
): StreamEvent | undefined {
  const kind = u.sessionUpdate as string | undefined;
  switch (kind) {
    case "agent_message_chunk": {
      const text = textOf(u.content);
      if (!text) return undefined;
      return { type: "assistant", message: { content: [{ type: "text", text }] } };
    }
    case "tool_call": {
      const id = (u.toolCallId as string) ?? "";
      const name =
        (u.title as string) || (u.kind as string) || "tool";
      return {
        type: "assistant",
        message: {
          content: [
            {
              type: "tool_use",
              id,
              name,
              input: (u.rawInput as Record<string, unknown>) ?? {},
            },
          ],
        },
      };
    }
    case "tool_call_update": {
      const status = u.status as string | undefined;
      if (status !== "completed" && status !== "failed") return undefined;
      return {
        type: "user",
        message: {
          content: [
            {
              type: "tool_result",
              tool_use_id: (u.toolCallId as string) ?? "",
              is_error: status === "failed",
            },
          ],
        },
      };
    }
    // agent_thought_chunk, plan, available_commands_update, current_mode_update:
    // nothing this surface shows. A phone wants the answer and what is being
    // done, not the reasoning trace.
    default:
      return undefined;
  }
}

function textOf(content: unknown): string {
  const c = content as { type?: string; text?: string } | undefined;
  return c?.type === "text" && c.text ? c.text : "";
}

function assistantTextOf(ev: StreamEvent): string {
  if (ev.type !== "assistant") return "";
  return (ev.message?.content ?? [])
    .filter((b) => b.type === "text" && b.text)
    .map((b) => b.text)
    .join("");
}
