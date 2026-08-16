/**
 * Which identifiers in an inbound envelope are allowed to *authenticate* a
 * sender, as opposed to merely describe one.
 *
 * Both networks hand the gateway several ids for one person, and they are not
 * of equal weight:
 *
 * - Signal's `sourceUuid` (the ACI) is the identity the sealed-sender
 *   certificate is issued for and the session is established with. It cannot
 *   be asserted by anyone but the account holder.
 * - Signal's `sourceNumber` is a *phone number*: recyclable by the carrier,
 *   transferable by SIM swap, and re-registerable on Signal by whoever holds
 *   it next (they get a fresh ACI, but the same number). As a credential for a
 *   pod bound to cluster-admin it is the weakest link in the chain.
 * - WhatsApp's `remoteJid` / `participant` is the JID the Signal-protocol
 *   session is with. Its `*Pn` and `*Alt` siblings are the server's claim
 *   about which other identity belongs to the same person — useful for display,
 *   never proof, and trusting them means trusting WhatsApp's servers to not
 *   mis-map a LID onto an allowlisted number.
 *
 * Matching admits a sender if ANY id matches (router.matchesAllowlist), so the
 * weakest id in the list is the real gate. These helpers narrow the list to the
 * ids that actually carry cryptographic weight before it ever reaches the
 * allowlist.
 */

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Signal ACIs (and PNIs) are UUIDs; a phone number never matches this. */
export function isUuid(value: string | undefined): boolean {
  return Boolean(value && UUID_RE.test(value));
}

/**
 * The subset of a Signal envelope's identifiers that may authenticate. Every
 * field signal-cli offers is still *checked* for shape rather than position,
 * because `source` carries the number on some envelopes and the ACI on others.
 */
export function signalAuthIds(ids: (string | undefined)[]): string[] {
  return ids.filter((id): id is string => isUuid(id));
}

/**
 * Allowlist entries that can never match under `signalAuthIds` — i.e. phone
 * numbers left in `SIGNAL_ALLOWED_SENDERS`. Reported at startup, because the
 * failure mode otherwise is a silent lockout: messages simply stop being
 * answered with nothing in the log tying it to the config.
 */
export function unusableSignalEntries(allowed: string[]): string[] {
  return allowed.filter((entry) => !isUuid(entry));
}
