# systems-notebook

Standalone, working code pulled out of production systems I built and operate, each with a short
write-up of the actual engineering problem it solves — not toy examples, real pieces extracted from
real infrastructure and cleaned up to stand on their own.

The full story behind each of these — the systems they came from, the incidents that shaped them,
and a lot more that isn't code at all — is written up at
**[sourcekit.org/portfolio](https://sourcekit.org/portfolio/)**.

## What's here

| Module | What it is |
|---|---|
| [`macho-signature-strip/`](macho-signature-strip/) | A pure-Python reimplementation of `codesign --remove-signature`, built so a build pipeline that develops on macOS but runs its CI on Linux behaves identically on both, with zero platform-specific tooling. Verified byte-for-byte against real signed binaries, not just "it doesn't crash." |
| [`adaptive-rate-governor/`](adaptive-rate-governor/) | A self-tuning, preventive rate limiter for a high-fanout service hitting a third-party API with undocumented rate limits. AIMD tuning, GCRA pacing (with the measured concurrency bug the naive token-bucket design had), a shadow mode for testing a new limit against real traffic without ever blocking anything. |
| [`passkey-credential-bridge/`](passkey-credential-bridge/) | ~30 lines that let one WebAuthn passkey work across two independently-built apps with different credential storage formats, by translating at the read/write boundary instead of migrating either app's internal format. |

Each directory is self-contained: the code, and a README explaining the actual problem, what was
tried and rejected, and why the final shape looks the way it does. No secrets, no internal
infrastructure details, no proprietary business logic — the code here works as written, with the
context genuinely generalized rather than search-and-replaced.

## Why extracted, not linked

The production repos these came from are private — they contain live infrastructure config,
credentials references, and enough operational detail about systems currently in use that making
them public outright isn't something I'm going to do casually. What's here is the reusable
engineering underneath that, pulled out and verified standalone, so it can actually be read and run
without needing access to anything private.
