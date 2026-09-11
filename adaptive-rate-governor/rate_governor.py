#!/usr/bin/env python3
"""Preventive, self-tuning rate governor for outbound Apple requests (itunes.apple.com + apps.apple.com).

Why: the version poller fans ~13 apps x up to 57 countries out through concurrent ThreadPools, so Apple
sees a ~400-wide burst and 403/429-blocks the runner IP. A single process-global token bucket keyed by
domain-group ("apple") meters EVERY Apple request through one gate, turning the burst into a smooth,
safe stream — pacing *preventively* instead of the old reactive back-off-after-you're-blocked.

Three modes via env GOVERNOR_MODE:
  off     no-op passthrough — today's behaviour byte-for-byte (default until validated)
  shadow  compute the pacing decision + log what it WOULD do, but NEVER block (secondary/validation run)
  on      enforce: block callers until a token is available

The allowed rate self-tunes with AIMD from Apple's 403/429 feedback (see on_clean_run / on_rate_limited):
  clean run    -> rate = min(ceiling, rate + INCREMENT)   additive increase
  any 403/429  -> rate = max(floor,   rate * DECREASE)    multiplicative decrease

State (a GCRA 'theoretical arrival time' cursor per group + learned rate_per_min) persists in the
runner-local poll heartbeat so it carries across the per-minute fresh-process runs and keeps learning.
Fail-open: any internal error logs and lets the request proceed — a governor bug must never take the
poller down. A per-group semaphore (MAX_CONCURRENCY) also bounds how many callers can be inside
acquire() at once, as a resource-usage cap (GCRA itself is already accurate regardless of concurrency).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

# --- seeds (see scripts/analyze_rate_history.py; conservative, AIMD tunes from here) ---
SEED_RATE_PER_MIN = float(os.environ.get("GOVERNOR_SEED_RATE", "60"))
RATE_FLOOR = 10.0
# Measured 2026-07-27: Apple's origin ceiling for our IP is ~700/min (clean ≤600, 40% 403 at 720). Keep
# the AIMD ceiling comfortably under that. Env-configurable so we can retune without a deploy (was 300).
RATE_CEILING = float(os.environ.get("GOVERNOR_CEILING", "300"))
AIMD_INCREMENT = 5.0     # +req/min per clean run
AIMD_DECREASE = 0.5      # xrate on any 403/429
BURST = 30.0             # max tokens the bucket can hold (small burst allowance)
# Glide-mode reactive pause floor: once AIMD's multiplicative-decrease has dragged the rate down to this
# fraction of the ceiling, stop polling entirely (a real pause) instead of limping on — below it Apple is
# clearly hard-blocking and low-rate polling just sustains it. The "levels" are the rate itself; this is
# the single terminal pause. Env-tunable. Default ~12% (e.g. 75/min at a 600 ceiling).
PAUSE_FRAC = float(os.environ.get("GOVERNOR_PAUSE_FRAC", "0.125"))
# Owner-requested (2026-08-05): a real recover-then-retrip traced to hundreds of threads (up to ~13 apps
# x 32 workers) all racing acquire() near-simultaneously. Each computes its own wait from a SNAPSHOT of
# the bucket, then sleeps independently -- but Python thread-scheduling overhead across hundreds of
# threads is itself real wall-clock time the bucket keeps refilling during, so "later" callers see far
# more tokens than the naive serialized-queue model assumes. Measured: 413 concurrent callers at a
# 250/min rate cleared the gate in ~7.5s wall-clock, not the ~92s the wait math implies (sum of all
# individual waits, if truly serialized, was ~2738s). Capping how many threads can be INSIDE acquire()
# at once (computing+sleeping) bounds that scheduling-overhead window directly. Applies to every acquire()
# caller uniformly (http_client.py AND amp_api.py's direct calls) with no change needed at either site.
MAX_CONCURRENCY = int(os.environ.get("GOVERNOR_MAX_CONCURRENCY", "20"))

HEARTBEAT_FILE = os.environ.get(
    "GOVERNOR_HEARTBEAT_FILE", os.path.expanduser("~/.governor_heartbeat.json"))
_HB_KEY = "rate_governor"

_APPLE_DOMAINS = ("itunes.apple.com", "apps.apple.com")


def is_apple_url(url) -> bool:
    u = str(url or "")
    return any(d in u for d in _APPLE_DOMAINS)


def _mode() -> str:
    m = os.environ.get("GOVERNOR_MODE", "off").strip().lower()
    return m if m in ("off", "shadow", "on") else "off"


class _Bucket:
    __slots__ = ("tat", "rate_per_min", "would_wait_total", "grants")
    # tat = "theoretical arrival time" (GCRA) -- the wall-clock instant the bucket next considers
    # itself free to grant. Replaces the old tokens+last_refill pair; see RateGovernor.acquire().

    def __init__(self, tat, rate_per_min):
        self.tat = tat
        self.rate_per_min = rate_per_min
        self.would_wait_total = 0.0   # shadow-mode: total seconds it WOULD have blocked
        self.grants = 0               # requests metered this run


class RateGovernor:
    """Process-global token bucket. Inject `now`/`sleep` for tests."""

    def __init__(self, mode=None, heartbeat_file=HEARTBEAT_FILE,
                 now=time.time, sleep=time.sleep,
                 seed_rate=SEED_RATE_PER_MIN, floor=RATE_FLOOR, ceiling=RATE_CEILING,
                 increment=AIMD_INCREMENT, decrease=AIMD_DECREASE, burst=BURST,
                 max_concurrency=None):
        self._mode = mode if mode is not None else _mode()
        self._hb = heartbeat_file
        self._now = now
        self._sleep = sleep
        self._seed = seed_rate
        self._floor = floor
        self._ceiling = ceiling
        self._inc = increment
        self._dec = decrease
        self._burst = burst
        self._max_concurrency = MAX_CONCURRENCY if max_concurrency is None else max_concurrency
        self._lock = threading.Lock()
        self._buckets = {}
        self._sems = {}
        self._sems_lock = threading.Lock()
        self._loaded = False

    def _sem(self, group):
        """Per-group semaphore bounding how many callers can be inside acquire() (computing a wait
        and/or sleeping) at once -- see MAX_CONCURRENCY."""
        with self._sems_lock:
            s = self._sems.get(group)
            if s is None:
                s = threading.Semaphore(max(1, self._max_concurrency))
                self._sems[group] = s
            return s

    # ---- state persistence (heartbeat, merge-preserving) ----
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._hb) as f:
                data = json.load(f)
            saved = (data or {}).get(_HB_KEY, {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            saved = {}
        for group, st in saved.items():
            try:
                self._buckets[group] = _Bucket(
                    float(st.get("tat", self._now())),
                    float(st.get("rate_per_min", self._seed)))
            except (TypeError, ValueError):
                continue

    def save(self):
        """Merge governor state into the heartbeat file (best-effort, atomic)."""
        try:
            try:
                with open(self._hb) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            with self._lock:
                data[_HB_KEY] = {
                    g: {"tat": round(b.tat, 3), "rate_per_min": round(b.rate_per_min, 3)}
                    for g, b in self._buckets.items()}
            tmp = self._hb + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._hb)
        except OSError as e:
            print(f"  [governor] warn: could not persist state: {e}", file=sys.stderr)

    def _bucket(self, group):
        b = self._buckets.get(group)
        if b is None:
            now = self._now()
            interval = 60.0 / max(1e-6, self._seed)
            # Pre-credit the burst allowance so the first `burst` callers get instant grants before
            # queuing starts, matching the old tokens=burst initial state. See acquire()'s docstring
            # for why (burst-1), not burst.
            b = _Bucket(now - max(0.0, self._burst - 1.0) * interval, self._seed)
            self._buckets[group] = b
        return b

    # ---- the gate ----
    def acquire(self, group="apple", block=True):
        """Meter one request. Returns the seconds waited (or would-wait in shadow). Fail-open.

        GCRA-based (2026-08-05 redesign, replacing the old token-count-plus-periodic-refill model).
        Each caller's grant is computed in ONE atomic step under the lock, from a single 'theoretical
        arrival time' (tat) cursor and the REAL current time at the moment it actually holds the lock
        -- there is no separate "refill based on elapsed-since-last-call" step for scheduling delay to
        exploit. That distinction is what the old design got wrong under high concurrency: hundreds of
        threads racing the lock each computed their wait from a stale snapshot, but the real wall-clock
        time spent just scheduling that many threads was itself time the OLD bucket kept refilling
        during -- so later callers saw far more capacity than intended (measured: 413 threads cleared a
        250/min gate in ~7.5s wall-clock, not the ~92s the pacing implies; only ~8% of the intended rate
        was actually enforced). GCRA has no such gap: whatever real time elapses before a caller reaches
        the lock is automatically and correctly reflected, because `now` is read fresh at that exact
        moment, not assumed from an earlier snapshot.

        Still held under the per-group semaphore (MAX_CONCURRENCY, added earlier the same day) as a
        resource-usage bound -- fewer threads piled up sleeping at once -- not for pacing correctness;
        GCRA alone is already accurate regardless of concurrency."""
        if self._mode == "off":
            return 0.0
        sem = self._sem(group)
        sem.acquire()
        try:
            self._load()
            with self._lock:
                b = self._bucket(group)
                b.grants += 1
                now = self._now()
                interval = 60.0 / max(1e-6, b.rate_per_min)
                # Clamp: don't let more than `burst` requests' worth of idle time bank as free credit,
                # regardless of how long the bucket sat unused (mirrors the old tokens<=burst cap).
                floor = now - max(0.0, self._burst - 1.0) * interval
                tat = max(b.tat, floor)
                wait = max(0.0, tat - now)
                b.tat = tat + interval
                if self._mode == "shadow":
                    b.would_wait_total += wait
            # sleep OUTSIDE the lock so other callers can reserve their own slots
            if self._mode == "on" and block and wait > 0:
                self._sleep(wait)
            return wait
        except Exception as e:   # fail-open — never break the poller
            print(f"  [governor] acquire error (proceeding): {e}", file=sys.stderr)
            return 0.0
        finally:
            sem.release()

    # ---- AIMD feedback ----
    def on_clean_run(self, group="apple"):
        try:
            self._load()
            with self._lock:
                b = self._bucket(group)
                b.rate_per_min = min(self._ceiling, b.rate_per_min + self._inc)
        except Exception as e:
            print(f"  [governor] on_clean_run error: {e}", file=sys.stderr)

    def on_rate_limited(self, group="apple"):
        try:
            self._load()
            with self._lock:
                b = self._bucket(group)
                b.rate_per_min = max(self._floor, b.rate_per_min * self._dec)
        except Exception as e:
            print(f"  [governor] on_rate_limited error: {e}", file=sys.stderr)

    def dampen(self, group="apple", factor=0.5):
        """PREEMPTIVE rate cut — distinct from on_rate_limited(), which only fires reactively after a
        real 403/429. Called when a predictive signal (recent-volume trajectory, historically-risky
        day/hour bucket) suggests a rate-limit is likely before Apple has actually returned one, so a
        gentler single cut is applied instead of waiting to hit the wall. `factor` defaults softer
        than on_rate_limited's AIMD_DECREASE (0.5 vs 0.5 is the same magnitude here deliberately for
        the first version — tune independently via the caller once there's real shadow-mode data to
        judge against)."""
        try:
            self._load()
            with self._lock:
                b = self._bucket(group)
                b.rate_per_min = max(self._floor, b.rate_per_min * factor)
        except Exception as e:
            print(f"  [governor] dampen error: {e}", file=sys.stderr)

    def rate(self, group="apple"):
        self._load()
        return self._bucket(group).rate_per_min

    def paused(self, group="apple"):
        """Glide-mode reactive pause: True once AIMD has dragged the rate down to the floor (PAUSE_FRAC × ceiling)."""
        return self.rate(group) <= self._ceiling * PAUSE_FRAC

    def reset_rate(self, group="apple", to=None):
        """Lift the rate after a confirmed recovery (default: seed) so the group exits the pause and re-ramps."""
        self._load()
        self._bucket(group).rate_per_min = float(self._seed if to is None else to)
        self.save()

    def grants(self, group="apple"):
        """How many requests were metered through the gate this run (0 if the group is untouched)."""
        b = self._buckets.get(group)
        return b.grants if b else 0

    def log_summary(self, group="apple"):
        """Emit a one-line summary (shadow mode shows what it would have done)."""
        b = self._buckets.get(group)
        if not b:
            return
        pace_min = b.grants / max(1e-6, b.rate_per_min)   # minutes to pace THIS run's volume at the learned rate
        if self._mode == "shadow":
            # NOTE: in shadow the scheduler isn't cutting volume, so `metered` is the FULL request
            # count; once both are 'on' the scheduler shrinks it to a handful and pace drops to seconds.
            print(f"  [governor:SHADOW] group={group} metered={b.grants} req this run; learned rate={b.rate_per_min:.0f}/min; "
                  f"~{pace_min:.1f} min to pace this run's (un-scheduled) volume")
        else:
            print(f"  [governor:on] group={group} metered={b.grants} req; rate={b.rate_per_min:.0f}/min")


# --- process-global singleton + convenience API used by http_client / amp_api / check_versions ---
_GOV = None
_GOV_LOCK = threading.Lock()


def governor():
    global _GOV
    if _GOV is None:
        with _GOV_LOCK:
            if _GOV is None:
                _GOV = RateGovernor()
    return _GOV


def set_governor(gov):
    """Inject a governor (tests)."""
    global _GOV
    _GOV = gov


def acquire(group="apple", block=True):
    return governor().acquire(group, block=block)


def on_clean_run(group="apple"):
    governor().on_clean_run(group)


def on_rate_limited(group="apple"):
    governor().on_rate_limited(group)


def dampen(group="apple", factor=0.5):
    governor().dampen(group, factor)


def rate(group="apple"):
    return governor().rate(group)


def paused(group="apple"):
    return governor().paused(group)


def reset_rate(group="apple", to=None):
    governor().reset_rate(group, to)


def grants(group="apple"):
    return _GOV.grants(group) if _GOV is not None else 0


def save():
    if _GOV is not None:
        _GOV.save()


def log_summary(group="apple"):
    if _GOV is not None:
        _GOV.log_summary(group)
