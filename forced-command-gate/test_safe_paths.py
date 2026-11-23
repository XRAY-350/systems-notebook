#!/usr/bin/env python3
"""Tests for safe_paths.py. Each attack test plants the trap a less-trusted account could plant, runs the helper the way
root code would, and checks both that it refused and that the victim file is untouched. Runs as any user (the
"trusted uid" is the test's own uid; the untrusted cases use a uid set that excludes it). Run: python3 test_safe_paths.py"""
import os
import stat
import tempfile
import unittest

import safe_paths as sp

UID, GID = os.getuid(), os.getgid()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())      # realpath: on macOS /var is itself a symlink
        os.chmod(self.root, 0o755)
        self.home = os.path.join(self.root, "home")            # the less-trusted user's writable dir
        os.mkdir(self.home)
        self.victim = os.path.join(self.root, "victim.conf")    # a "system file" root must never touch
        with open(self.victim, "w") as f:
            f.write("ORIGINAL\n")
        self.trusted = (0, UID)

    def victim_ok(self):
        with open(self.victim) as f:
            return f.read() == "ORIGINAL\n"


class OpenUserFile(Base):
    def test_creates_and_writes_a_plain_marker(self):
        p = os.path.join(self.home, ".marker")
        sp.write_user_file(p, b'{"at":1}', UID, GID)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b'{"at":1}')

    def test_planted_symlink_is_refused(self):
        p = os.path.join(self.home, ".marker")
        os.symlink(self.victim, p)
        with self.assertRaises(sp.UnsafePath):
            sp.write_user_file(p, b"PWNED", UID, GID)
        self.assertTrue(self.victim_ok())

    def test_dangling_symlink_is_refused_not_created_through(self):
        target = os.path.join(self.root, "created-by-root")
        p = os.path.join(self.home, ".marker")
        os.symlink(target, p)
        with self.assertRaises(sp.UnsafePath):
            sp.write_user_file(p, b"x", UID, GID)
        self.assertFalse(os.path.exists(target))

    def test_planted_hard_link_is_refused(self):
        p = os.path.join(self.home, ".marker")
        os.link(self.victim, p)
        with self.assertRaises(sp.UnsafePath):
            sp.write_user_file(p, b"PWNED", UID, GID)
        self.assertTrue(self.victim_ok())

    def test_directory_in_place_of_the_file_is_refused(self):
        p = os.path.join(self.home, ".marker")
        os.mkdir(p)
        with self.assertRaises(OSError):
            sp.open_user_file(p, UID, GID)


class SafeParent(Base):
    def test_plain_root_owned_tree_passes_and_is_created(self):
        p = os.path.join(self.root, "srv", "app", "conf.d")
        self.assertEqual(sp.safe_parent(p, trusted_uids=self.trusted, boundary=self.root), p)
        self.assertTrue(os.path.isdir(p))

    def test_symlinked_directory_is_refused(self):
        os.symlink(self.root, os.path.join(self.home, "etc"))          # home/etc -> somewhere root owns
        with self.assertRaises(sp.UnsafePath):
            sp.safe_parent(os.path.join(self.home, "etc", "x"), trusted_uids=self.trusted, boundary=self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")),
                         "refused only after creating a directory through the link (the late realpath check is a backstop)")

    def test_symlink_refused_even_under_a_lax_prefix(self):
        os.symlink(self.root, os.path.join(self.home, "link"))
        with self.assertRaises(sp.UnsafePath):
            sp.safe_parent(os.path.join(self.home, "link", "x"), trusted_uids=self.trusted, lax_prefixes=[self.home], boundary=self.root)

    def test_directory_owned_by_an_untrusted_uid_is_refused(self):
        with self.assertRaises(sp.UnsafePath):
            sp.safe_parent(os.path.join(self.home, "sub"), trusted_uids=(0,) if UID else (12345,), boundary=self.root)

    def test_lax_prefix_skips_only_the_ownership_rule(self):
        p = os.path.join(self.home, "sub")
        sp.safe_parent(p, trusted_uids=(0,) if UID else (12345,), lax_prefixes=[self.root], boundary=self.root)
        self.assertTrue(os.path.isdir(p))

    def test_group_or_world_writable_directory_is_refused(self):
        os.chmod(self.home, 0o777)
        with self.assertRaises(sp.UnsafePath):
            sp.safe_parent(os.path.join(self.home, "sub"), trusted_uids=self.trusted, boundary=self.root)

    def test_sticky_world_writable_directory_is_allowed(self):
        os.chmod(self.home, 0o777 | stat.S_ISVTX)                      # like /tmp
        sp.safe_parent(os.path.join(self.home, "sub"), trusted_uids=self.trusted, boundary=self.root)

    def test_file_in_place_of_a_directory_is_refused(self):
        f = os.path.join(self.home, "notadir")
        open(f, "w").close()
        with self.assertRaises(sp.UnsafePath):
            sp.safe_parent(os.path.join(f, "x"), trusted_uids=self.trusted, boundary=self.root)


if __name__ == "__main__":
    unittest.main(verbosity=1)
