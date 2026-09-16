import type {
  BackgroundTask,
  RunHooks,
  RunOverrides,
  RunResult,
} from "../claude.ts";

// The agent behind a chat. claude was the only one for this surface's whole
// life, and its shape is still the contract: RunResult, RunHooks and
// RunOverrides are claude.ts's own types, reused rather than re-invented,
// because router.ts already speaks them and nothing else needs to change for a
// second backend to arrive.
//
// Everything non-claude is driven over ACP (Agent Client Protocol) — see
// acp.ts. That is what makes one interface plausible at all: ACP gives session
// create/load/prompt/cancel and, crucially, a permission request the gateway
// can answer from the phone, which is this surface's whole reason to exist.

export type AgentId = "claude" | "codex" | "gemini" | "muse";

/**
 * What a backend can actually do. Every one of these is true for claude and
 * false for the ACP backends, and each is a real hole rather than a rounding
 * error — so the router reads these flags and SAYS SO rather than accepting a
 * command and quietly dropping it. A `!plan on` that reports success and then
 * lets the model edit files is worse than a refusal.
 */
export interface AgentSupports {
  /** Can this agent's model actually be chosen? claude has `--model`; an ACP
   * agent can only do it if it advertises a `model` config option, which is
   * not knowable until a session exists — hence supportsFor() rather than a
   * static field.
   *
   * This one was missing at first, and its absence was the exact bug the rest
   * of this interface exists to prevent: `!model` on an ACP backend wrote the
   * value to state, answered "✓ … applies to the next message", and was read
   * by nobody. */
  model: boolean;
  /** `--effort` on claude; a `thought_level` config option on an ACP agent
   * (muse advertises one — none|minimal|low|medium|high|xhigh|max|ultra). */
  effort: boolean;
  /** `--permission-mode plan`. */
  plan: boolean;
  /** `--append-system-prompt`. ACP backends get the prompt prepended to the
   * first message of a session instead. */
  systemPrompt: boolean;
  /** The model can arm a ScheduleWakeup the gateway then owes it (wakeup.ts).
   * Gateway-owned schedules (schedules.ts) work regardless — those timers live
   * on this side of the process boundary. */
  wakeup: boolean;
  /** Background tasks, and with them the parked-run machinery: a process held
   * open past its first answer so it can wake itself when work reports. */
  backgroundTasks: boolean;
  /** !usage, which counts tokens by reading ~/.claude/projects transcripts. */
  usage: boolean;
  /** MCP servers registered for a cwd at project scope in ~/.claude.json. */
  projectMcp: boolean;
}

export interface AgentBackend {
  id: AgentId;
  /** What to call it in chat. */
  label: string;
  /** The binary this backend needs on PATH, for a startup check that says
   * which agent is missing rather than failing at the first message. */
  command: string;

  run(
    chatKey: string,
    message: string,
    contextPrefix: string,
    hooks: RunHooks,
    run_?: RunOverrides,
  ): Promise<RunResult>;

  stop(chatKey: string): boolean;
  isRunning(chatKey: string): boolean;
  isWaiting(chatKey: string): boolean;
  waitingOn(chatKey: string): BackgroundTask[];
  hasLiveWakeup(chatKey: string): boolean;
  handOff(chatKey: string, message: string, contextPrefix?: string): boolean;
  runningChats(): string[];

  /** Newest session id on disk for `cwd`, for a `!resume` with no argument —
   * the cross-surface handoff from a /term or Happy session. Undefined when a
   * backend keeps no discoverable transcript. */
  latestSessionId(cwd: string): string | undefined;
  /** Transcript bytes a cold resume would have to rebuild, for the health
   * warnings in router.ts. 0 when the backend exposes no transcript. */
  resumableBytes(cwd: string, sessionId: string): number;

  /** Alias → model id, for typing shortcuts on a phone. claude's are the only
   * hardcoded ones left; an ACP agent tells us its real catalogue instead (see
   * modelChoices), which is better than anything that could be guessed here. */
  models: Record<string, string>;
  defaultModel: string;

  /**
   * What this agent says it will accept for `!model` / `!effort` in this chat.
   * Empty `values` means "not known yet" — an ACP agent only publishes its
   * catalogue once a session exists, and muse leaves the model list empty until
   * `muse login` has refreshed it. Callers must treat unknown as "accept it and
   * let the next run be the judge", not as "reject".
   */
  choicesFor(
    chatKey: string,
    kind: "model" | "effort",
  ): { values: { value: string; name?: string }[]; current?: string };

  /**
   * Per chat, because two of these are only knowable from a live session. An
   * agent that has not run yet answers optimistically: refusing a command
   * before we know whether it works would be its own kind of lying.
   */
  supportsFor(chatKey: string): AgentSupports;
}
