#!/usr/bin/env python3
"""Tests for held_approvals.py. Run: python3 test_held_approvals.py"""
import os
import tempfile
import unittest

import held_approvals as ha

SIGN, VERIFY = ha.hmac_signer(b"test-key-not-a-secret")
FORGE, _ = ha.hmac_signer(b"some-other-key")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = ha.Store(tempfile.mkdtemp(), VERIFY, clock=self.clock)
        self.req = self.s.request_approval("alpha", ["beta", "gamma"], "move-site", "Move the site Monday",
                                           "steps...", action="site-move", targets="web host, DNS", op="cutover")

    def answer(self, subject, signer=SIGN, mid=None, frm="human"):
        r = {"id": ha._new_id(), "from": frm, "in_reply_to": mid or self.req["id"], "thread": "move-site",
             "subject": subject, "body": ""}
        return self.s.human_reply(signer(r) if signer else r)


class Holding(Base):
    def test_request_reaches_nobody_before_the_answer(self):
        self.assertEqual(self.s.inbox("beta") + self.s.inbox("gamma"), [])
        self.assertTrue(os.listdir(self.s.p("threads", "move-site")))      # the human can see it on the thread

    def test_request_must_say_what_yes_covers(self):
        with self.assertRaises(ha.Refused):
            self.s.request_approval("alpha", ["beta"], "t", "s", "b", action="x", targets="", op="y")

    def test_signed_approval_releases_to_every_recipient_with_a_stamp(self):
        self.assertEqual(self.answer("APPROVED"), "approved")
        for a in ("beta", "gamma"):
            (m,) = self.s.inbox(a)
            self.assertEqual(m["id"], self.req["id"])
            self.assertIn("[APPROVED by the human: approval", m["body"])
            self.assertTrue(m["approved_by"])
        self.assertEqual(self.s.inbox("alpha"), [])                         # the requester already has it

    def test_denied_marks_it_decided_and_releases_nothing(self):
        self.assertEqual(self.answer("DENIED"), "not-approved")
        self.assertEqual([m for m in self.s.inbox("beta") if m["id"] == self.req["id"]], [])

    def test_unsigned_or_forged_or_relayed_yes_releases_nothing(self):
        self.assertIsNone(self.answer("APPROVED", signer=None))
        self.assertIsNone(self.answer("APPROVED", signer=FORGE))
        self.assertIsNone(self.answer("APPROVED", frm="alpha"))           # an agent saying "the human approved"
        self.assertEqual([m for m in self.s.inbox("beta") if m["id"] == self.req["id"]], [])
        self.assertEqual(self.answer("APPROVED"), "approved")              # the real one still works afterwards

    def test_first_signed_answer_decides_once(self):
        self.answer("DENIED")
        self.assertIsNone(self.answer("APPROVED"))
        self.assertEqual([m for m in self.s.inbox("beta") if m["id"] == self.req["id"]], [])

    def test_withdrawn_request_ignores_a_later_approval(self):
        with self.assertRaises(ha.Refused):
            self.s.withdraw(self.req["id"], by="beta")                     # only the requester or the human
        self.s.withdraw(self.req["id"], by="alpha")
        self.assertIsNone(self.answer("APPROVED"))
        self.assertEqual([m for m in self.s.inbox("beta") if m["id"] == self.req["id"]], [])


class CatchUp(Base):
    def test_sweep_releases_an_answer_whose_release_was_cut_off(self):
        # simulate a caller killed after storing the signed answer but before acting on it
        r = SIGN({"id": ha._new_id(), "from": "human", "in_reply_to": self.req["id"], "thread": "move-site",
                  "subject": "APPROVED", "body": "", "ts": self.clock()})
        self.s._deliver(dict(r, to=[]), inbox=False)
        self.assertEqual(self.s.inbox("beta"), [])
        self.assertEqual(self.s.release_pending(), [self.req["id"]])
        self.assertEqual(len([m for m in self.s.inbox("beta") if m["id"] == self.req["id"]]), 1)
        self.assertEqual(self.s.release_pending(), [])                     # idempotent: nothing left, no second copy

    def test_sweep_uses_the_first_answer_and_ignores_forgeries(self):
        for subj, signer in (("APPROVED", FORGE), ("DENIED", SIGN), ("APPROVED", SIGN)):
            r = signer({"id": ha._new_id(), "from": "human", "in_reply_to": self.req["id"], "thread": "move-site",
                        "subject": subj, "body": "", "ts": self.clock()})
            self.s._deliver(dict(r, to=[]), inbox=False)
        self.s.release_pending()
        self.assertEqual(ha._read(self.s.p("held", f"{self.req['id']}.json"))["decided"], "not-approved")


class SingleUse(Base):
    def test_approval_is_used_once_for_its_own_action_by_a_party(self):
        self.answer("APPROVED")
        aid = ha._read(self.s.p("held", f"{self.req['id']}.json"))["decided_by"]
        with self.assertRaises(ha.Refused):
            self.s.use_approval(aid, "delete-everything", "beta")          # a different action
        with self.assertRaises(ha.Refused):
            self.s.use_approval(aid, "site-move", "delta")                 # not a party to the request
        self.assertTrue(self.s.use_approval(aid, "site-move", "beta"))
        self.assertFalse(self.s.use_approval(aid, "site-move", "gamma"))   # replay
        with self.assertRaises(ha.Refused):
            self.s.use_approval("abcdef", "site-move", "beta")             # not an approval at all


class Board(Base):
    def test_overlapping_claim_warns_but_is_recorded(self):
        self.s.claim("beta", "rotate the backup encryption keys", on=["backups"])
        c, warn = self.s.claim("gamma", "rotate backup encryption keys on the new box")
        self.assertEqual([w["agent"] for w in warn], ["beta"])
        self.assertIn(c["id"], [x["id"] for x in self.s.claims()])        # a warning, not a lock
        _, warn = self.s.claim("gamma", "unrelated thing", on=["backups"])
        self.assertEqual(len(warn), 1)                                      # same resource also warns

    def test_only_the_claimant_clears_a_claim(self):
        c, _ = self.s.claim("beta", "something")
        with self.assertRaises(ha.Refused):
            self.s.claim_done(c["id"], "gamma")
        self.s.claim_done(c["id"], "beta")
        self.assertEqual(self.s.claims(), [])

    def test_similar_question_is_suggested_never_joined_automatically(self):
        q, _ = self.s.ask("beta", "Which weekend for the database upgrade?", "...")
        q2, sugg = self.s.ask("gamma", "database upgrade weekend choice", "...")
        self.assertEqual([x["id"] for x in sugg], [q["id"]])
        self.assertEqual(ha._read(self.s.p("board", "questions", f"{q['id']}.json"))["contributors"], [])
        self.assertNotEqual(q2["id"], q["id"])

    def test_owner_relays_the_answer_to_everyone_who_joined(self):
        q, _ = self.s.ask("beta", "Which weekend?", "...")
        self.s.join(q["id"], "gamma", "I need it too")
        with self.assertRaises(ha.Refused):
            self.s.close_question(q["id"], by="gamma", text="Saturday")    # only the owning agent relays
        self.assertEqual(self.s.close_question(q["id"], by="beta", text="Saturday"), ["gamma"])
        (m,) = self.s.inbox("gamma")
        self.assertIn("not signed by the human", m["body"])
        self.assertIn("Saturday", m["body"])

    def test_approval_request_sits_on_the_board_until_the_signed_answer(self):
        open_ids = [q["id"] for q in self.s.questions()]
        self.assertIn(self.req["id"], open_ids)
        with self.assertRaises(ha.Refused):
            self.s.close_question(self.req["id"], by="alpha", text="approved")   # an agent can't close it
        with self.assertRaises(ha.Refused):
            self.s.join(self.req["id"], "beta", "me too")
        self.answer("APPROVED")
        self.assertNotIn(self.req["id"], [q["id"] for q in self.s.questions()])


if __name__ == "__main__":
    unittest.main(verbosity=1)
