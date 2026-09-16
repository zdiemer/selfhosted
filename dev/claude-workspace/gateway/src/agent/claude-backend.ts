import {
  handOff,
  hasLiveWakeup,
  isRunning,
  isWaiting,
  latestSessionId,
  resumableBytes,
  runClaude,
  runningChats,
  stop,
  waitingOn,
} from "../claude.ts";
import { config } from "../config.ts";
import type { AgentBackend } from "./backend.ts";

// claude, as a backend. Every method here forwards to claude.ts unchanged —
// this file exists to give the thing a name and a capability list, not to
// reimplement it. claude.ts stays the elaborate one: it is the only backend
// with background-task parking, self-wake, plan mode, project MCP servers and
// transcript-based usage accounting, and all of that is load-bearing.

export const claudeBackend: AgentBackend = {
  id: "claude",
  label: "Claude",
  command: "claude",

  run: runClaude,
  stop,
  isRunning,
  isWaiting,
  waitingOn,
  hasLiveWakeup,
  handOff,
  runningChats,
  latestSessionId,
  resumableBytes,

  // Aliases so a phone doesn't have to type a full model id.
  models: {
    opus: "claude-opus-5",
    sonnet: "claude-sonnet-5",
    haiku: "claude-haiku-4-5",
    fable: "claude-fable-5",
  },
  get defaultModel(): string {
    return config.model;
  },

  // Constant: claude's flags do not depend on a live session.
  supportsFor: () => ({
    model: true,
    effort: true,
    plan: true,
    systemPrompt: true,
    wakeup: true,
    backgroundTasks: true,
    usage: true,
    projectMcp: true,
  }),

  // claude has no catalogue to ask for, so `!model` keeps taking any id
  // verbatim — which is what makes a model newer than this build usable on the
  // day it ships. The aliases above are the shortcuts.
  choicesFor: () => ({ values: [] }),
};
