import { Cron } from "croner";
import { config, type ScheduleSpec } from "./config.ts";

// Recurring runs the GATEWAY fires (values.yaml messaging.schedules), as
// opposed to wakeup.ts, which fires what the model armed for itself. The
// difference is who is responsible for the next occurrence: a ScheduleWakeup
// chain dies the first time a run errors out or forgets to re-arm, which is
// exactly the failure mode that left a trading agent dead in a tmux session
// for three weeks. A cron entry in values.yaml cannot forget itself.
//
// Deliberately no catch-up for firings missed while the pod was down: these
// are recurring check-ins, and a market-open run fired at 11pm because the
// pod was rescheduled is worse than no run — the next occurrence comes soon
// enough. (A pending model-armed wakeup DOES fire overdue on boot; that is a
// one-shot promise, not a cadence.)

type Runner = (spec: ScheduleSpec, chatKey: string) => void;

let runner: Runner = () => {};
const jobs = new Map<string, Cron>();

/** Registered by router.ts at import time — same shape as setWakeupRunner,
 * for the same cycle-avoiding reason. */
export function setScheduleRunner(r: Runner): void {
  runner = r;
}

/** The chat a surface's schedules report into: the owner's 1:1, owner being
 * the first configured allowed sender. Schedules need no identifiers of their
 * own in values.yaml — which is what keeps them out of the Secret. */
function ownerChatKey(surface: "signal" | "whatsapp"): string | undefined {
  const senders =
    surface === "signal"
      ? config.signal.allowedSenders
      : config.whatsapp.allowedSenders;
  // Signal allowlists can carry non-ACI entries that identity.ts ignores;
  // the owner has to be one that can actually match.
  const owner = senders.find((s) =>
    surface === "signal" ? /^[0-9a-f-]{36}$/i.test(s) : true,
  );
  return owner ? `${surface}:${owner}` : undefined;
}

/** Arm one surface's schedules as its transport connects. Called through
 * onTransportReady like rearmWakeups, and re-entrant for the same reason —
 * a transport reconnecting must replace its jobs, not double them. */
export function armSchedules(prefix: string): void {
  if (prefix !== "signal" && prefix !== "whatsapp") return;
  for (const spec of config.schedules) {
    if ((spec.surface ?? "signal") !== prefix) continue;
    const chatKey = ownerChatKey(prefix);
    if (!chatKey) {
      console.error(`schedule ${spec.name}: no owner sender on ${prefix}`);
      continue;
    }
    jobs.get(spec.name)?.stop();
    try {
      const job = new Cron(
        spec.cron,
        { timezone: spec.timezone, protect: true },
        () => runner(spec, chatKey),
      );
      jobs.set(spec.name, job);
      console.log(
        `schedule ${spec.name}: armed (${spec.cron}` +
          `${spec.timezone ? ` ${spec.timezone}` : ""}), next ` +
          `${job.nextRun()?.toISOString() ?? "never"}`,
      );
    } catch (err) {
      console.error(
        `schedule ${spec.name}: bad cron "${spec.cron}": ${(err as Error).message}`,
      );
    }
  }
}

/** For !status: name and next firing per armed schedule. */
export function scheduleStatus(): { name: string; next: Date | null }[] {
  return [...jobs.entries()].map(([name, job]) => ({
    name,
    next: job.nextRun(),
  }));
}
