# held-owner-approvals

Several AI agents work for one human. When one agent needs the human's approval for work that other agents will
carry out, ask once, show the human exactly what "yes" covers, and wake the other agents only after a signed yes.
Plus the shared board the agents use to say what they're doing and what they're waiting on the human for.

```python
from held_approvals import Store, hmac_signer
sign, verify = hmac_signer(key)                       # production: an asymmetric key that never leaves one host
bus = Store("/var/lib/bus", verify)

req = bus.request_approval("alpha", ["beta", "gamma"], "site-move", "Move the site Monday", body,
                           action="site-move", targets="web host, DNS records", op="cutover")
# beta and gamma see nothing yet. The human sees the request and taps Approve; the tap arrives signed:
bus.human_reply(signed_reply)                        # -> "approved": beta and gamma now get it, stamped
bus.use_approval(approval_id, "site-move", "beta")   # True once; a replay returns False
```

This module is a standalone rewrite of features added in November to the file-based agent bus in
[`agent-bus/`](../agent-bus/), which already had signed human decisions and a crossing guard. Those features are
the held requests, the release-first ordering and catch-up sweep, single use, and the activity board. The
production version runs between three always-on server agents and several laptop sessions; the human answers from
a phone app or a chat button.

**Who wrote it:** AI coding agents (Claude Code) wrote the code and the tests, to my requirements. The requirements
here are mine almost word for word: hold the ping until approved, nothing joins a question automatically, and every
request says what it covers.

## The problem

An agent that needs approval for a job involving two other agents used to message all three, and the human answered
later. Every message wakes its recipients, so the other two agents were woken by a request that might never be
approved, and the human's later "yes" often reached them relayed by another agent, which is exactly what the bus's
signatures exist to distrust. The requirement was to have the requesting agent send the request and hold the ping
until it is approved: the request goes to the human, and it reaches the other agents only together with the signed
decision.

## Design decisions

- **Held, not sent.** `request_approval` writes the request to the thread (so the human sees it) and to the sender's
  copy, but to no recipient's inbox. Delivery to an inbox is what wakes an agent, so nobody is woken.
- **Only a signed answer decides.** The human's taps arrive signed; `verify` is injected so the store never decides
  who the human is. An unsigned reply, a reply signed with the wrong key, or another agent saying "the human
  approved" releases nothing. APPROVED releases the request to every recipient, stamped with the approval's id so
  each can check it. Any other answer marks it decided and releases nothing.
- **Decided once.** The first signed answer wins. A later APPROVED after a DENIED, or after the requester withdrew
  the request, changes nothing.
- **A request must say what yes covers.** `action`, `targets` and `op` are required. The human approves a named
  action on named targets, not a paragraph, so it is clear from the phone what one tap agrees to.
- **Single use, for that action.** `use_approval` records the first use with `O_EXCL`, so a replayed or raced
  approval returns False, and an approval can't be used for a different action or by an agent that isn't a party
  to the request. A batch of steps is one action with all its targets named up front, so a multi-day job needs
  one tap, not one per step.

## The approval that was stored but never acted on

In production, two signed approvals arrived, were stored and shown as approved, and released nothing. The human's
tap reaches the bus through a short-lived process with a hard time limit, on a host that reads the store over a
network mount. On a day the mount was slow, that process stored the reply, copied it to the sender, mirrored it to
the chat, and was killed before it reached the release step, which ran last.

Two fixes, both here:

1. **Release first.** `human_reply` releases the held request before it does anything slower, so the one step that
   matters can't be the one that gets cut off.
2. **A catch-up sweep.** `release_pending` finds every held request that has a signed answer stored but no
   decision, and runs the normal release on the human's first signed answer. The signature check and the
   decided-once rule are the same code. In production it runs after every send on the host that owns the store.

## The activity board

- **Claims are a warning, not a lock.** `claim` records what an agent is doing. If another agent's claim shares a
  resource or most of its significant words, the new claim is still recorded, and both agents are told to
  coordinate. Only the claimant can clear a claim, and claims stay until cleared, so a stale one is visibly someone's
  mess.
- **Questions for the human are asked once.** Before asking, an agent checks the board. `ask` prints similar open
  questions as suggestions, but joining one is always a deliberate `join`; the human ruled out automatic joining,
  because two questions that look alike are often different. The agent that owns a question relays the human's
  answer to everyone who joined, marked as relayed and not signed by the human.
- **An approval request sits on the board too,** and only the human's signed reply can close it. No agent can
  close it or join it.

## Verified

`python3 test_held_approvals.py`: 15 tests.

- the request reaches no recipient before the answer, and must name its action, targets and op;
- a signed APPROVED releases to every recipient with the stamp; DENIED releases nothing;
- unsigned, forged and agent-relayed approvals release nothing, and the real one still works afterwards;
- the first signed answer decides; a withdrawn request ignores a later approval;
- the sweep releases a cut-off approval once, is idempotent, uses the first signed answer and ignores forgeries;
- single use: refused for a different action or a non-party, True once, False on replay;
- claims warn on overlap without blocking; only the claimant clears; questions are suggested, never auto-joined;
  the owner relays to joiners; an approval on the board closes only on the signed answer.

Six mutations, each caught by at least one test: skipping signature verification, delivering the request to
inboxes at once, removing the decided-once check, dropping `O_EXCL` on single use, letting the sweep pick the last
answer instead of the first, and dropping the action check.
