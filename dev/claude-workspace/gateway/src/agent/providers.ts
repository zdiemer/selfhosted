import { config } from "../config.ts";
import { makeAcpBackend, type ProviderSpec } from "./acp-backend.ts";
import type { AgentBackend } from "./backend.ts";

// The non-claude agents, and the one thing that differs between them: what to
// exec to get an ACP server on stdio.
//
// Each of these was checked against the image rather than taken from docs,
// because the docs were wrong twice:
//   - gemini speaks ACP itself (`--acp`; `--experimental-acp` is the
//     deprecated spelling, still accepted).
//   - codex does NOT have an `acp` subcommand. It has `app-server`, its own
//     JSON-RPC. The bridge is @agentclientprotocol/codex-acp, from the
//     protocol's own org.
//   - muse does NOT speak ACP either. It has `serve`, which is Meta's MSP.
//     muse-acp is the adapter, and it drives the `muse` binary — so `muse
//     login` is still the credential, and there is no API key anywhere.
//
// There are no alias tables here any more. The first version of this file had
// invented ones ({mini: "gpt-5.4-mini"}, {flash, pro}, {spark}) — guesses that
// `!model` resolved against and then dropped on the floor. An ACP agent
// publishes its real catalogue as a `model` config option on session/new, so
// `!model` lists and validates against that instead. See agent/acp.ts.
export const PROVIDERS: ProviderSpec[] = [
  {
    id: "codex",
    label: "Codex",
    command: "codex-acp",
    args: [],
    defaultModel: () => config.agents.codexModel,
  },
  {
    id: "gemini",
    label: "Gemini",
    command: "gemini",
    args: ["--acp"],
    defaultModel: () => config.agents.geminiModel,
  },
  {
    id: "muse",
    label: "Muse",
    command: "muse-acp",
    args: [],
    // ⚠️ The default is `muse-spark-1.3`, NOT `muse-spark-1.3-contributor`.
    // They are the same model; the contributor variant is ~12x cheaper because
    // its data is, in Meta's words, "used to improve our products". What goes
    // through this gateway is private repos, ~/code checkouts and a
    // cluster-admin seat. That is not training data, and the price difference
    // is not a reason to make it some. `!model` can still select the
    // contributor row per chat for throwaway work.
    // https://developer.meta.com/ai/models/muse-spark/
    defaultModel: () => config.agents.museModel,
  },
];

/** The ACP backends this instance offers. `messaging.agents` narrows it — a
 * delegated instance has no business handing out three more subscriptions. */
export function acpBackends(): AgentBackend[] {
  return PROVIDERS.filter((p) => config.agents.enabled.includes(p.id)).map(
    makeAcpBackend,
  );
}
