import { config } from "../config.ts";

// One concurrency ceiling for the whole pod, not one per agent. GW_MAX_CONCURRENT
// bounds how many agent processes this container will have up at once, and that
// is a statement about the pod's memory and the PVC's patience — not about which
// CLI is running. Two codex runs and a claude run cost the same as three claude
// runs, so they draw on the same budget.
//
// Lived in claude.ts as a private `activeCount` until there was a second backend
// to share it with.

let active = 0;

export function acquireSlot(): void {
  active++;
}

export function releaseSlot(): void {
  active--;
}

export function atCapacity(): boolean {
  return active >= config.maxConcurrentClaude;
}
