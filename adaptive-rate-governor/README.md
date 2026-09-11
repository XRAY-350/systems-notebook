# adaptive-rate-governor

A self-tuning, preventive rate limiter for outbound requests against a third-party API with
undocumented, IP-based rate limits. AIMD (additive-increase/multiplicative-decrease) tuning, GCRA
(generic cell rate algorithm) pacing, state that survives process restarts, and a shadow mode for
testing a new limit against production traffic without ever actually blocking anything.

```python
import rate_governor as rg

rg.governor()._mode = "on"   # or set env GOVERNOR_MODE=on
wait = rg.acquire("some-api")      # blocks until a token is available, returns seconds waited
# ... make the request ...
if got_rate_limited:
    rg.on_rate_limited("some-api")  # cut the rate
else:
    rg.on_clean_run("some-api")     # nudge the rate back up
rg.save()                     # persist learned rate across process restarts
```

## Why this exists

A version-monitoring service I built polls a public app-metadata API across many apps and many
regional storefronts on a schedule, fanned out across a thread pool for throughput. At scale that
fan-out produces a wide concurrent burst against a single origin, which its (undocumented) rate
limiter doesn't like — sustained polling got the source IP rate-limited repeatedly. A *reactive*
back-off (wait after you get blocked) kept causing the same blocks over and over, because by the
time you find out you're over the limit, you already fired a burst of requests that will each
individually get blocked too.

This is a *preventive* governor instead: every outbound request goes through one process-global
gate first, which meters it against a learned safe rate — so the burst gets smoothed into a steady
stream before it ever leaves the process, rather than getting punished for it after the fact.

## The pacing algorithm: GCRA, not token-bucket-with-periodic-refill

The first version of this used the textbook token-bucket design: a token count, refilled based on
elapsed time since the last refill. It had a real, measured bug under high concurrency: hundreds of
threads racing to acquire a token each computed their wait from a *stale snapshot* of the bucket,
then slept independently — but scheduling hundreds of threads is itself real wall-clock time, which
the *old* bucket design kept refilling during. Measured on a real production run: 413 concurrent
callers against a 250/min-rate gate cleared it in ~7.5 seconds of wall-clock time, not the ~92
seconds the pacing math implied — only about 8% of the intended rate was actually being enforced.

GCRA doesn't have that gap. Each caller's grant is computed in one atomic step, from a single
"theoretical arrival time" cursor and the *real* current time read at the exact moment the caller
holds the lock — there's no separate refill-since-last-call step for scheduling delay to exploit.
Whatever wall-clock time elapses before a caller reaches the lock is automatically and correctly
reflected, because `now` is never assumed from an earlier snapshot.

## The AIMD loop

The allowed rate isn't fixed — it self-tunes:

- Every clean run nudges the rate up a little (`rate = min(ceiling, rate + increment)`).
- Every time the caller reports getting rate-limited anyway, the rate gets cut hard
  (`rate = max(floor, rate * decrease)`).

The asymmetry (small steps up, big cuts down) is deliberate: overshooting the real limit is
expensive (you get blocked again), undershooting it just costs a little throughput, so the
algorithm should be quick to back off and slow to creep back up.

## Shadow mode

Before trusting a new rate ceiling (or the governor itself) against production traffic, `mode=shadow`
runs the full pacing computation and logs exactly what it *would* have done — including the total
seconds it would have blocked — without ever actually blocking a caller. That's how a governor gets
validated against real, messy production load before it's allowed to actually enforce anything: you
can see its decisions line up with reality first.

## Fail-open by design

Every public method wraps its body in a `try/except` that logs and returns a harmless default (zero
wait, unchanged rate) on any internal error. A bug in the rate limiter should never be able to take
down the thing it's protecting — worst case it's back to unmetered traffic, not a hung process.
