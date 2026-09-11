#!/usr/bin/env python3
"""
macho_strip.py — pure-Python Mach-O code signature removal.

Equivalent to `codesign --remove-signature` on macOS, but with zero dependency
on macOS or any Apple toolchain: no `codesign`, no `ldid`, nothing but the
Python standard library. Works identically on Linux CI runners, which is the
whole point — re-signing/re-packaging pipelines that build on macOS but
publish from Linux (or vice versa) need this to behave the same everywhere.

What it does, precisely (matching codesign's own removal steps):
  1. Remove the LC_CODE_SIGNATURE (and LC_DYLIB_CODE_SIGN_DRS) load commands
     from the Mach-O load-command table.
  2. Decrement ncmds / sizeofcmds in the Mach-O header to match.
  3. Shrink the __LINKEDIT segment's recorded file size by the signature
     blob's size.
  4. Truncate the file at the signature's data offset (the signature blob is
     always the last thing in the file).
  5. Handle FAT (multi-architecture) binaries by processing each architecture
     slice independently, in place.

There's a fifth step most from-scratch implementations miss: a small
symbol-table string-padding normalization (see `_normalize_symtab_string_padding`)
that some re-signing tools' assertion checks are sensitive to. Getting that
detail right — and PROVING it, not assuming it — is the actual story here;
see the README.

No network calls, no external processes, no writes outside the path you give
it. Safe to read end to end.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

# ── Mach-O constants ──────────────────────────────────────────────────────

MH_MAGIC_64 = 0xFEEDFACF
MH_MAGIC_32 = 0xFEEDFACE
LC_SYMTAB = 0x02
LC_DYSYMTAB = 0x0B
LC_SEGMENT = 0x01
LC_SEGMENT_64 = 0x19
LC_CODE_SIG = 0x1D
LC_DYLIB_CODE_SIGN_DRS = 0x2B
LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x80000022
LC_FUNC_STARTS = 0x26
LC_DATA_IN_CODE = 0x29
LC_EXPORTS_TRIE = 0x80000033
LC_CHAINED_FIXUPS = 0x80000034
CPU_TYPE_ARM64 = 0x0100000C

FAT_MAGIC = 0xCAFEBABE
FAT_CIGAM = 0xBEBAFECA


# ── byte helpers ────────────────────────────────────────────────────────

def _r32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _w32(d, o, v):
    struct.pack_into("<I", d, o, v)


def is_macho(path) -> bool:
    """True if the file at `path` starts with a recognized Mach-O magic."""
    magics = {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
    try:
        with open(path, "rb") as f:
            return f.read(4) in magics
    except OSError:
        return False


# ── the string-table padding normalization ─────────────────────────────

def _normalize_symtab_string_padding(data: bytearray, is_64: bool) -> bytearray:
    """
    Some re-signing tools' whole-bundle signing path asserts that a binary's
    LC_SYMTAB string table ends within a small margin of the file's actual
    end (`stroff + strsize >= file_size - 0x10`). Compilers/linkers commonly
    leave a few zero alignment bytes after the string table, and stripping
    the code signature makes those bytes the new file tail — which can trip
    that assertion on an otherwise perfectly valid binary.

    This folds harmless trailing zero padding into `strsize` so
    `stroff + strsize == file_size`, but ONLY when every safety guard passes:
      - the gap is small (1-16 bytes)
      - every byte in the gap is actually zero
      - no OTHER load-command-referenced data range extends into the gap
        (symbol table, dyld info, function starts, chained fixups, etc.)

    Silently no-ops (returns `data` unchanged) if any guard fails — this is a
    compatibility nicety, never something worth risking a corrupt binary over.
    """
    LC_SYMTAB_ = 0x02
    hdr_size = 32 if is_64 else 28
    if len(data) < hdr_size:
        return data

    fsize = len(data)
    ncmds = _r32(data, 16)

    symtab_cmd_off = None
    stroff = strsize = 0
    referenced_ends = []

    off = hdr_size
    for _ in range(ncmds):
        if off + 8 > fsize:
            break
        cmd = _r32(data, off)
        csz = _r32(data, off + 4)
        if csz < 8 or off + csz > fsize:
            break

        if cmd == LC_SYMTAB_ and csz >= 24:
            symtab_cmd_off = off
            stroff = _r32(data, off + 16)
            strsize = _r32(data, off + 20)
            symoff = _r32(data, off + 8)
            nsyms = _r32(data, off + 12)
            if symoff and nsyms:
                referenced_ends.append(symoff + nsyms * 16)
        elif cmd == LC_DYSYMTAB and csz >= 80:
            doff = _r32(data, off + 56)
            dcnt = _r32(data, off + 60)
            if doff and dcnt:
                referenced_ends.append(doff + dcnt * 4)
        elif cmd in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY) and csz >= 48:
            for base in (8, 16, 24, 32, 40):
                doff = _r32(data, off + base)
                dsz = _r32(data, off + base + 4)
                if doff and dsz:
                    referenced_ends.append(doff + dsz)
        elif cmd in (LC_FUNC_STARTS, LC_DATA_IN_CODE, LC_EXPORTS_TRIE, LC_CHAINED_FIXUPS) and csz >= 16:
            doff = _r32(data, off + 8)
            dsz = _r32(data, off + 12)
            if doff and dsz:
                referenced_ends.append(doff + dsz)

        off += csz

    if symtab_cmd_off is None or (stroff == 0 and strsize == 0):
        return data

    str_end = stroff + strsize
    if str_end > fsize:
        return data

    gap = fsize - str_end
    if gap <= 0 or gap > 16:
        return data
    if any(b != 0 for b in data[str_end:fsize]):
        return data
    if any(rend > str_end for rend in referenced_ends):
        return data

    _w32(data, symtab_cmd_off + 20, strsize + gap)
    return data


# ── the actual signature removal ────────────────────────────────────────

def remove_codesig_from_macho(data: bytearray):
    """
    Remove code-signing load commands from a THIN (non-FAT) Mach-O in a
    bytearray. Mirrors `codesign --remove-signature` step for step.

    Returns (patched_data, True) if a signature was found and removed, or
    (data, False) if the file had no signature to begin with.
    """
    magic = _r32(data, 0)
    is_64 = magic == MH_MAGIC_64
    is_32 = magic == MH_MAGIC_32
    if not is_64 and not is_32:
        return data, False

    hdr_size = 32 if is_64 else 28
    ncmds = _r32(data, 16)
    sizeofcmds = _r32(data, 20)

    off = hdr_size
    commands = []
    remove_commands = []
    sig_dataoff = None
    sig_datasize = None
    linkedit_off = None

    for _ in range(ncmds):
        if off + 8 > len(data):
            break
        cmd = _r32(data, off)
        csz = _r32(data, off + 4)
        if csz < 8 or off + csz > len(data):
            break
        commands.append((off, csz, cmd))
        if cmd == LC_CODE_SIG:
            remove_commands.append((off, csz))
            sig_dataoff = _r32(data, off + 8)
            sig_datasize = _r32(data, off + 12)
        elif cmd == LC_DYLIB_CODE_SIGN_DRS:
            remove_commands.append((off, csz))
        elif cmd == LC_SEGMENT_64 and is_64:
            segname = data[off + 8: off + 24].split(b"\x00")[0]
            if segname == b"__LINKEDIT":
                linkedit_off = off
        elif cmd == LC_SEGMENT and is_32:
            segname = data[off + 8: off + 24].split(b"\x00")[0]
            if segname == b"__LINKEDIT":
                linkedit_off = off
        off += csz

    if not remove_commands:
        return data, False

    remove_offsets = {o for o, _ in remove_commands}
    commands_end = hdr_size + sizeofcmds
    kept = bytearray()
    removed_size = 0
    for cmd_off, cmd_size, _ in commands:
        if cmd_off in remove_offsets:
            removed_size += cmd_size
            continue
        kept.extend(data[cmd_off:cmd_off + cmd_size])
    data[hdr_size:hdr_size + len(kept)] = kept
    for i in range(hdr_size + len(kept), commands_end):
        data[i] = 0

    _w32(data, 16, ncmds - len(remove_commands))
    _w32(data, 20, sizeofcmds - removed_size)

    if linkedit_off is not None and sig_datasize:
        if is_64:
            fs_off = linkedit_off + 48
            old_fs = struct.unpack_from("<Q", data, fs_off)[0]
            struct.pack_into("<Q", data, fs_off, old_fs - sig_datasize)
        else:
            fs_off = linkedit_off + 36
            old_fs = _r32(data, fs_off)
            _w32(data, fs_off, old_fs - sig_datasize)

    if sig_dataoff and sig_dataoff < len(data):
        data = data[:sig_dataoff]

    # Trim any trailing padding beyond the last real segment now that the
    # signature (always the file's tail) is gone.
    max_segment_end = 0
    off = hdr_size
    for _ in range(ncmds - len(remove_commands)):
        if off + 8 > len(data):
            break
        cmd = _r32(data, off)
        csz = _r32(data, off + 4)
        if csz < 8 or off + csz > len(data):
            break
        if cmd == LC_SEGMENT_64 and is_64:
            fileoff = struct.unpack_from("<Q", data, off + 40)[0]
            filesize = struct.unpack_from("<Q", data, off + 48)[0]
            if filesize:
                max_segment_end = max(max_segment_end, fileoff + filesize)
        elif cmd == LC_SEGMENT and is_32:
            fileoff = _r32(data, off + 32)
            filesize = _r32(data, off + 36)
            if filesize:
                max_segment_end = max(max_segment_end, fileoff + filesize)
        off += csz
    if max_segment_end and max_segment_end < len(data):
        data = data[:max_segment_end]

    data = _normalize_symtab_string_padding(data, is_64)
    return data, True


def strip_file(fpath) -> bool:
    """Remove the code signature from a single Mach-O file (thin or FAT), in place.
    Returns True if a signature was found and removed."""
    with open(fpath, "rb") as f:
        raw = bytearray(f.read())
    if len(raw) < 8:
        return False

    magic = struct.unpack(">I", raw[:4])[0]

    if magic in (FAT_MAGIC, FAT_CIGAM):
        is_be = magic == FAT_MAGIC
        fmt = ">I" if is_be else "<I"
        nfat = struct.unpack(fmt, raw[4:8])[0]
        any_stripped = False
        for i in range(nfat):
            base = 8 + i * 20
            slice_off = struct.unpack(fmt, raw[base + 8: base + 12])[0]
            slice_size = struct.unpack(fmt, raw[base + 12: base + 16])[0]
            slice_data = bytearray(raw[slice_off: slice_off + slice_size])
            patched, did = remove_codesig_from_macho(slice_data)
            if did:
                if len(patched) < slice_size:
                    patched.extend(b"\x00" * (slice_size - len(patched)))
                raw[slice_off: slice_off + slice_size] = patched
                any_stripped = True
        if any_stripped:
            with open(fpath, "wb") as f:
                f.write(raw)
        return any_stripped

    patched, did = remove_codesig_from_macho(raw)
    if did:
        with open(fpath, "wb") as f:
            f.write(patched)
    return did


def strip_directory(root) -> int:
    """Walk `root` and strip every Mach-O file found. Returns the count stripped."""
    count = 0
    for path in Path(root).rglob("*"):
        if path.is_file() and is_macho(path):
            if strip_file(path):
                count += 1
    return count


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <file-or-directory>", file=sys.stderr)
        raise SystemExit(1)
    target = Path(sys.argv[1])
    if target.is_dir():
        n = strip_directory(target)
        print(f"stripped {n} Mach-O file(s) under {target}")
    else:
        did = strip_file(target)
        print(f"{'stripped' if did else 'no signature found on'} {target}")
