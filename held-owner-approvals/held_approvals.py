"""held_approvals.py: ask the human once, wake the other agents only after the human said yes.

A file store for two things several AI agents share with one human:

1. HELD APPROVAL REQUESTS. Agent A needs the human's approval for work that agents B and C will carry out. The request
   is stored and shown to the human, but it reaches nobody's inbox (and so wakes nobody) until the human's SIGNED reply
   arrives. APPROVED releases it to every recipient, stamped with the approval's id; DENIED or any other answer only
   marks it decided. The first signed answer decides, exactly once. An unsigned reply that merely claims to come from
   the human releases nothing. A catch-up sweep releases any request whose signed answer is stored but whose release
   was cut off. Each approval can be used once, for the action it named.

2. THE ACTIVITY BOARD. Claims ("I'm doing X") and open questions for the human. A claim that overlaps someone else's
   is a warning, not a lock. A new question that looks like an open one prints a suggestion; joining it is always a
   deliberate act, never automatic. The agent that owns a question relays the human's answer to everyone who joined.

Signature checking is injected (`verify(reply) -> bool`), so the store never decides who the human is. The demo signer
here is an HMAC; the production bus uses an asymmetric key that lives only on the host the human's taps arrive on.
Every write is atomic (temp file + rename), so a reader never sees half a record.
"""
import json
import os
import re
import secrets
import time

ID_RE = re.compile(r"^[0-9a-f]{6}$")
REQUIRED = ("action", "targets", "op")          # what an approval request must say, so the human knows what "yes" covers


class Refused(Exception):
    pass


# ------------------------------------------------------------------ storage helpers
def _new_id():
    return secrets.token_hex(3)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, sort_keys=True)
    os.replace(tmp, path)


def _read(path):
    with open(path) as f:
        return json.load(f)


def _ls(d):
    try:
        return sorted(os.listdir(d))
    except FileNotFoundError:
        return []


class Store:
    def __init__(self, root, verify, clock=time.time):
        self.root, self.verify, self.clock = root, verify, clock

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    # ---------------------------------------------------------- messages
    def _file(self, m):
        return f"{m['ts']:.6f}-{m['from']}-{m['id']}.json"

    def _deliver(self, m, inbox=True):
        fn = self._file(m)
        _write(self.p("threads", m["thread"], fn), m)
        _write(self.p("sent", m["from"], fn), m)
        if inbox:
            for a in m["to"]:
                if a != m["from"]:
                    _write(self.p("inbox", a, fn), m)

    def inbox(self, agent):
        return [_read(self.p("inbox", agent, f)) for f in _ls(self.p("inbox", agent))]

    def send(self, frm, to, thread, subject, body, **extra):
        m = dict(extra, id=_new_id(), ts=self.clock(), to=list(to), thread=thread, subject=subject, body=body)
        m["from"] = frm
        self._deliver(m)
        return m

    # ---------------------------------------------------------- held approval requests
    def request_approval(self, frm, to, thread, subject, body, action, targets, op):
        """Store a request for the human. Recipients other than the sender get nothing until it is approved."""
        fields = {"action": action, "targets": targets, "op": op}
        missing = [k for k in REQUIRED if not (fields[k] or "").strip()]
        if missing:
            raise Refused(f"an approval request must name its {', '.join(missing)}")
        m = {"id": _new_id(), "ts": self.clock(), "from": frm, "to": list(to), "thread": thread, "subject": subject,
             "body": body, **fields, "kind": "approval-request"}
        _write(self.p("held", f"{m['id']}.json"), {"msg": m, "decided": None})
        _write(self.p("board", "questions", f"{m['id']}.json"),          # on the board until the human decides
               {"id": m["id"], "kind": "approval", "topic": subject, "text": body, "owner_agent": frm,
                "contributors": [], "since": m["ts"], "closed": None})
        self._deliver(m, inbox=False)               # thread + sender copy only: the human sees it, the agents don't
        return m

    def human_reply(self, reply):
        """Store the human's reply. Releasing the held request is done FIRST, before anything slower, so a caller with a
        time limit can't be cut off between storing the approval and acting on it."""
        reply = dict(reply, ts=reply.get("ts", self.clock()))
        decided = self._release(reply)
        self._deliver(dict(reply, to=reply.get("to") or []), inbox=True)
        return decided

    def _release(self, reply):
        if reply.get("from") != "human" or not reply.get("in_reply_to"):
            return None
        hp = self.p("held", f"{reply['in_reply_to']}.json")
        if not os.path.exists(hp):
            return None
        held = _read(hp)
        if held.get("decided"):
            return None                              # decided once; a second answer changes nothing
        if not self.verify(reply):
            return None                              # "the human said yes", relayed by someone else, is not a yes
        approved = (reply.get("subject") or "").strip().upper().startswith("APPROVED")
        held.update(decided="approved" if approved else "not-approved", decided_by=reply["id"])
        _write(hp, held)
        if approved:
            m = dict(held["msg"], approved_by=reply["id"])
            m["body"] = f"[APPROVED by the human: approval {reply['id']} on request {m['id']}]\n\n{m['body']}"
            self._deliver(m)                         # now it reaches (and wakes) every recipient
        self.close_question(reply["in_reply_to"], by="human", text=held["decided"], _approval=True)
        return held["decided"]

    def release_pending(self):
        """Catch-up sweep: decide every held request whose signed answer is already stored but was never acted on."""
        done = []
        for f in _ls(self.p("held")):
            if not f.endswith(".json") or not ID_RE.match(f[:-5]):
                continue
            held = _read(self.p("held", f))
            if held.get("decided"):
                continue
            mid, thread = f[:-5], held["msg"]["thread"]
            answers = []
            for g in _ls(self.p("threads", thread)):
                r = _read(self.p("threads", thread, g))
                if r.get("from") == "human" and r.get("in_reply_to") == mid and self.verify(r):
                    answers.append(r)
            if answers:
                self._release(min(answers, key=lambda r: (r["ts"], r["id"])))   # the human's FIRST signed answer
                if _read(self.p("held", f)).get("decided"):
                    done.append(mid)
        return done

    def withdraw(self, mid, by):
        """The requester (or the human) withdraws a request that no longer applies; a later APPROVED releases nothing."""
        hp = self.p("held", f"{mid}.json")
        held = _read(hp)
        if by not in (held["msg"]["from"], "human"):
            raise Refused(f"only {held['msg']['from']} or the human may withdraw {mid}")
        if held.get("decided"):
            raise Refused(f"{mid} is already {held['decided']}")
        held.update(decided="withdrawn", decided_by=by)
        _write(hp, held)

    def use_approval(self, approval_id, action, agent):
        """Record that `agent` is acting on `approval_id`. True the first time, for the action the request named;
        False for a replay. The record is created with O_EXCL, so two agents racing can't both get True."""
        held = next((h for h in (_read(self.p("held", f)) for f in _ls(self.p("held")) if f.endswith(".json"))
                     if h.get("decided") == "approved" and h.get("decided_by") == approval_id), None)
        if held is None:
            raise Refused(f"{approval_id} is not an approval of any request")
        if held["msg"]["action"] != action:
            raise Refused(f"{approval_id} approved {held['msg']['action']!r}, not {action!r}")
        if agent not in held["msg"]["to"] and agent != held["msg"]["from"]:
            raise Refused(f"{agent} is not a party to request {held['msg']['id']}")
        path = self.p("used", f"{approval_id}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            json.dump({"approval": approval_id, "action": action, "agent": agent, "ts": self.clock()}, f)
        return True

    # ---------------------------------------------------------- activity board: claims
    @staticmethod
    def _overlap(a, b):
        words = lambda s: {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3}
        wa, wb = words(a), words(b)
        return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.5

    def claims(self):
        return [_read(self.p("board", "claims", f)) for f in _ls(self.p("board", "claims"))]

    def claim(self, agent, title, on=()):
        """Record a claim. Returns (claim, warnings): an overlapping claim by someone else is reported, never blocking."""
        warnings = [c for c in self.claims() if c["agent"] != agent
                    and ((set(on) & set(c["on"])) or self._overlap(title, c["title"]))]
        c = {"id": _new_id(), "agent": agent, "title": title, "on": list(on), "since": self.clock()}
        _write(self.p("board", "claims", f"{c['id']}.json"), c)
        return c, warnings

    def claim_done(self, cid, agent):
        path = self.p("board", "claims", f"{cid}.json")
        c = _read(path)
        if c["agent"] != agent:
            raise Refused(f"claim {cid} is {c['agent']}'s")
        os.remove(path)

    # ---------------------------------------------------------- activity board: questions for the human
    def questions(self, open_only=True):
        qs = [_read(self.p("board", "questions", f)) for f in _ls(self.p("board", "questions"))]
        return [q for q in qs if not (open_only and q.get("closed"))]

    def ask(self, agent, topic, text):
        """Open a question. Returns (question, suggestions). Similar open questions are only suggested."""
        sugg = [q for q in self.questions() if q["owner_agent"] != agent and self._overlap(topic, q["topic"])]
        q = {"id": _new_id(), "kind": "question", "topic": topic, "text": text, "owner_agent": agent, "contributors": [],
             "since": self.clock(), "closed": None}
        _write(self.p("board", "questions", f"{q['id']}.json"), q)
        return q, sugg

    def join(self, qid, agent, context):
        path = self.p("board", "questions", f"{qid}.json")
        q = _read(path)
        if q.get("closed") or q.get("kind") != "question" or agent == q["owner_agent"]:
            raise Refused(f"{qid} is not an open question you can join")
        q["contributors"].append({"agent": agent, "context": context})
        _write(path, q)

    def close_question(self, qid, by, text, _approval=False):
        path = self.p("board", "questions", f"{qid}.json")
        if not os.path.exists(path):
            return []
        q = _read(path)
        if q.get("closed"):
            return []
        if q.get("kind") == "approval" and not _approval:
            raise Refused(f"{qid} is an approval request: only the human's signed reply decides it")
        if not _approval and by != q["owner_agent"]:
            raise Refused(f"question {qid} is owned by {q['owner_agent']}, who relays the answer")
        q["closed"] = {"by": by, "answer": text, "ts": self.clock()}
        _write(path, q)
        joined = sorted({c["agent"] for c in q["contributors"]} - {q["owner_agent"]})
        for a in joined:                                 # relayed, and marked as relayed (not the human's own words)
            self.send(q["owner_agent"], [a], f"question-{qid}", f"Answer to: {q['topic']}",
                      f"(relayed by {q['owner_agent']}, not signed by the human)\n\n{text}")
        return joined


# ------------------------------------------------------------------ demo signer (HMAC); production: asymmetric, off-host
def hmac_signer(key):
    import hashlib
    import hmac

    def canonical(reply):
        return json.dumps({k: reply.get(k) for k in ("id", "from", "in_reply_to", "subject", "body")},
                          sort_keys=True).encode()

    def sign(reply):
        return dict(reply, sig=hmac.new(key, canonical(reply), hashlib.sha256).hexdigest())

    def verify(reply):
        want = hmac.new(key, canonical(reply), hashlib.sha256).hexdigest()
        return hmac.compare_digest(str(reply.get("sig", "")), want)

    return sign, verify
