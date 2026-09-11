/**
 * drift_guard.js — periodic access-control drift detection against a golden manifest, with
 * DIFFERENT trust tiers for different kinds of changes.
 *
 * This is a standalone, generalized re-implementation of the core algorithm behind a permission
 * auditor I run in production, rewritten against plain objects instead of a live platform's real
 * API objects (so it's runnable and testable on its own, with no external service, no real
 * resource IDs, no dependency on the platform it was originally built for). The shape of the
 * problem and the fix are unchanged; see README.md for the real incident that caused this to
 * exist.
 *
 * Model: a "resource" (e.g. a channel, a folder, an endpoint) has a set of "overwrites" — explicit
 * grants/denials layered on top of a default. Overwrites come in two kinds:
 *   - SYSTEM-tier (e.g. a role-based grant): expected to be stable and owner-controlled. Any drift
 *     here is very likely accidental or malicious, so it's safe to auto-correct back to the golden
 *     manifest.
 *   - USER-tier (e.g. a one-off grant to a specific account): far more likely to be a deliberate,
 *     still-valid special case added AFTER the manifest snapshot was taken (an integration, a
 *     one-off exception). Auto-reverting those risks undoing something that was meant to stay —
 *     so these are only ever REPORTED, never auto-corrected.
 */

const SYSTEM = "system";
const USER = "user";

/**
 * @param {object} manifest  { [resourceId]: { overwrites: [{ id, tier, allow: string[], deny: string[] }] } }
 * @param {object} live      same shape, the current real state
 * @returns {{ corrections: object[], reports: object[] }}
 *   corrections — SYSTEM-tier drift found (id + what it should be, for the caller to actually apply)
 *   reports     — USER-tier drift found (never auto-applied, surfaced for a human to review)
 */
function computeDrift(manifest, live) {
  const corrections = [];
  const reports = [];

  for (const [resourceId, goldenEntry] of Object.entries(manifest)) {
    const liveEntry = live[resourceId];
    if (!liveEntry) {
      // Whole resource missing from live state entirely — always worth surfacing, regardless of tier.
      reports.push({ resourceId, kind: "resource-missing" });
      continue;
    }

    const goldenById = new Map(goldenEntry.overwrites.map((o) => [o.id, o]));
    const liveById = new Map(liveEntry.overwrites.map((o) => [o.id, o]));

    // Changed or removed overwrites (present in golden, different-or-absent in live).
    for (const [id, golden] of goldenById) {
      const current = liveById.get(id);
      const changed = !current || !sameOverwrite(golden, current);
      if (!changed) continue;

      const delta = { resourceId, overwriteId: id, tier: golden.tier, golden, current: current || null };
      if (golden.tier === SYSTEM) {
        corrections.push(delta);
      } else {
        reports.push(delta);
      }
    }

    // New overwrites not in the golden manifest at all.
    for (const [id, current] of liveById) {
      if (goldenById.has(id)) continue;
      const delta = { resourceId, overwriteId: id, tier: current.tier, golden: null, current };
      // A brand-new overwrite has no golden entry to compare tiers against by definition here, so
      // this uses the LIVE entry's own tier — a new system-tier grant appearing (e.g. a role
      // suddenly given permissions nobody granted) is exactly the class of thing worth
      // auto-reverting; a new user-tier grant is exactly the "probably deliberate" case.
      if (current.tier === SYSTEM) {
        corrections.push(delta);
      } else {
        reports.push(delta);
      }
    }
  }

  return { corrections, reports };
}

function sameOverwrite(a, b) {
  return sameSet(a.allow, b.allow) && sameSet(a.deny, b.deny);
}

function sameSet(a, b) {
  if (a.length !== b.length) return false;
  const s = new Set(a);
  return b.every((x) => s.has(x));
}

module.exports = { computeDrift, SYSTEM, USER };
