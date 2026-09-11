/**
 * gated_updater.js — a human-approve-once, apply-when-safe update state machine with a version-
 * migration guard and auto-rollback on a failed apply.
 *
 * Standalone, generalized version of the core state machine behind a mod auto-updater I run for a
 * game server fleet: check daily for available updates, require a one-time human approval before
 * anything is touched, then apply automatically the next time the system is in a safe state to do
 * so (an empty server, in the original) — with a hard safety guard that refuses to auto-apply a
 * real version migration (as opposed to a routine same-version update), and an assumed
 * backup-and-verify apply step that can report a rollback. All I/O (checking for updates, checking
 * whether it's currently safe to apply, actually applying, persisting state) is injected, so this
 * runs and is fully testable with zero real subprocesses, files, or a real game server.
 */

const IDLE = "idle";
const PENDING_APPROVAL = "pending_approval";
const ARMED = "armed";

/**
 * @param {object} deps
 * @param {() => Promise<{signature: string, changes: any[]}|null>} deps.checkForUpdates
 *   Returns null/no changes, or a plan with a stable signature (same signature = same pending set,
 *   used to dedupe re-notifying about a plan nothing has changed about) and the change list itself.
 * @param {() => Promise<boolean>} deps.isSafeWindow
 *   True when it's currently safe to apply (e.g. the server has zero players online right now).
 * @param {() => Promise<{migrationRequired: boolean}>} deps.checkMigrationGuard
 *   Called right before applying. If migrationRequired is true, apply is REFUSED and the plan is
 *   disarmed — a version migration is a deliberate, by-hand decision, never something this
 *   auto-applies, no matter how long it's been armed.
 * @param {() => Promise<"success"|"rolled_back"|"deferred">} deps.apply
 *   Actually applies the change set (with its own backup+verify+rollback internally) and reports
 *   the outcome. "deferred" means the safe window closed between the check and the apply attempt —
 *   stay armed, try again next time.
 */
function createGatedUpdater(deps) {
  let state = { status: IDLE, planSignature: null, plan: null };

  async function checkAndNotify() {
    const plan = await deps.checkForUpdates();
    if (!plan) {
      state = { status: IDLE, planSignature: null, plan: null };
      return { notified: false, reason: "no-changes" };
    }
    if (state.planSignature === plan.signature) {
      // Same pending plan as last time we notified — don't re-notify (this is what stops a
      // periodic re-check from spamming a duplicate approval prompt for something unchanged).
      return { notified: false, reason: "already-notified" };
    }
    state = { status: PENDING_APPROVAL, planSignature: plan.signature, plan: plan.changes };
    return { notified: true, plan: plan.changes };
  }

  function approve() {
    if (state.status !== PENDING_APPROVAL) return false;
    state = { ...state, status: ARMED };
    return true;
  }

  function dismiss() {
    if (state.status === IDLE) return false;
    state = { status: IDLE, planSignature: null, plan: null };
    return true;
  }

  /** Called on a schedule (e.g. every few minutes). No-op unless armed. */
  async function tryApply() {
    if (state.status !== ARMED) return { attempted: false };
    if (!(await deps.isSafeWindow())) return { attempted: false, reason: "not-safe-window" };

    const guard = await deps.checkMigrationGuard();
    if (guard.migrationRequired) {
      state = { status: IDLE, planSignature: null, plan: null };
      return { attempted: true, outcome: "refused-migration-guard" };
    }

    const outcome = await deps.apply();
    if (outcome === "deferred") {
      // The safe window closed between our check above and the apply call actually starting
      // (a real race — a player could join in that gap). Stay armed; the next tick tries again.
      return { attempted: true, outcome: "deferred" };
    }
    // success or rolled_back are both TERMINAL for this plan — either it's applied, or it failed
    // and was safely undone. Either way there's nothing left to retry; disarm.
    state = { status: IDLE, planSignature: null, plan: null };
    return { attempted: true, outcome };
  }

  return { checkAndNotify, approve, dismiss, tryApply, getState: () => ({ ...state }) };
}

module.exports = { createGatedUpdater, IDLE, PENDING_APPROVAL, ARMED };
