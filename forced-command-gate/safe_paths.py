"""safe_paths.py: how root code touches a path that a less-trusted user can write.

The bug class: a root program opens, truncates or chowns a file inside a directory that some other account can write
(a service user's home, a shared spool). That account plants a symlink (or a hard link) with the expected name, and the
next root run writes through it onto a file of the attacker's choosing, or hands its ownership away. It compiles, the
tests pass, and every normal run works, which is why it ships.

Two helpers cover the two halves:

  open_user_file(path, owner_uid, owner_gid, create)
      For a FILE the less-trusted user may own (a marker, a lock). O_NOFOLLOW refuses a symlink as the last path
      component; fstat on the open fd must show a regular file with exactly one link (a hard link to /etc/shadow is
      a regular file, so the link count is what catches it); ownership is fixed through the fd (fchown), never by path.

  safe_parent(path, trusted_uids=(0,), lax_prefixes=(), boundary="/")
      For a DIRECTORY root is about to create or write a file under. Every existing ancestor must be a real directory
      (lstat, not a symlink), owned by a trusted uid, and not group/other-writable unless it is sticky (like /tmp).
      Prefixes in lax_prefixes skip the ownership rule (an account that is root-equivalent anyway); they never skip the
      symlink rule. After creating missing parts, the resolved path must equal the lexical one.

Neither helper makes a directory safe that root does not control: they refuse, and the caller must not fall back to a
plain open().
"""
import os
import stat


class UnsafePath(OSError):
    pass


def open_user_file(path, owner_uid, owner_gid, create=False, mode=0o644):
    """Return an O_RDWR fd for path, or raise UnsafePath / OSError. Caller closes the fd."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as e:
        if e.errno == getattr(os, "ELOOP", 62) or os.path.islink(path):
            raise UnsafePath(f"{path}: is a symlink") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(f"{path}: not a regular file")
        if st.st_nlink != 1:
            raise UnsafePath(f"{path}: has {st.st_nlink} hard links")
        if (st.st_uid, st.st_gid) != (owner_uid, owner_gid):
            os.fchown(fd, owner_uid, owner_gid)          # by fd: cannot be redirected
        return fd
    except Exception:
        os.close(fd)
        raise


def write_user_file(path, data, owner_uid, owner_gid):
    """Replace the contents of a user-owned marker file safely (create if missing)."""
    fd = open_user_file(path, owner_uid, owner_gid, create=True)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, data)
    finally:
        os.close(fd)


def _ancestors(path):
    out, cur = [], os.path.abspath(path)
    while True:
        out.append(cur)
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return list(reversed(out))
        cur = nxt


def safe_parent(path, trusted_uids=(0,), lax_prefixes=(), create=True, boundary="/"):
    """Check (and optionally create) the directory `path` before root writes a file into it. Ancestors above `boundary`
    are not checked (always "/" in real use; tests set it to their temp tree)."""
    lax = [p.rstrip("/") for p in lax_prefixes]
    top = os.path.abspath(boundary).rstrip("/") or "/"
    for d in _ancestors(path):
        if not os.path.lexists(d) or not (top == "/" or d == top or d.startswith(top + "/")):
            continue
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{d}: is a symlink or not a directory")
        if any(d == x or d.startswith(x + "/") for x in lax):
            continue
        if st.st_uid not in trusted_uids:
            raise UnsafePath(f"{d}: owned by uid {st.st_uid}, not a trusted uid")
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise UnsafePath(f"{d}: writable by group or others and not sticky")
    if create:
        os.makedirs(path, mode=0o755, exist_ok=True)
    if os.path.realpath(path) != os.path.normpath(os.path.abspath(path)):
        raise UnsafePath(f"{path}: resolves through a symlink")     # something was swapped in between
    return path
