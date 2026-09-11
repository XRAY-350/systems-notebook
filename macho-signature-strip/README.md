# macho-signature-strip

A pure-Python implementation of `codesign --remove-signature`, with no dependency on macOS,
`codesign`, `ldid`, or any Apple toolchain — just the standard library.

```
python3 macho_strip.py /path/to/binary-or-directory
```

## Why this exists

A build/re-signing pipeline I run needs to strip code signatures from Mach-O binaries as a step
before re-packaging and re-signing them. Development happens on macOS, where `codesign` is right
there. Production runs on Linux CI, which has no code-signing tools at all — no `codesign`, no
`ldid`, nothing. The pipeline needed to behave identically on both, so I wrote the signature
removal from scratch against Apple's own Mach-O load-command format instead of shelling out to a
platform-specific tool.

## What "remove a code signature" actually means

Mach-O binaries carry their signature as a `LC_CODE_SIGNATURE` load command pointing at a blob
appended to the end of the file, inside the `__LINKEDIT` segment. Removing it correctly means:

1. Delete the `LC_CODE_SIGNATURE` (and `LC_DYLIB_CODE_SIGN_DRS`) load commands from the command
   table.
2. Decrement `ncmds` / `sizeofcmds` in the Mach-O header to match.
3. Shrink `__LINKEDIT`'s recorded file size by the signature blob's size.
4. Truncate the file at the signature's data offset — the signature is always the last thing in
   the file, so once it's logically gone, the trailing bytes are gone too.
5. For a FAT (multi-architecture) binary, do all of the above per-architecture-slice, in place,
   without disturbing the FAT header's slice offsets.

Steps 1-5 are the "obvious" version, and most from-scratch reimplementations stop there. There's a
sixth step that only shows up when your output actually gets used downstream by something other
than the OS loader.

## The part that isn't in the Apple documentation

The pipeline this came from re-signs binaries with a third-party signing tool, not `codesign`
itself. That tool's whole-bundle signing path asserts that a binary's symbol-table string table
ends within a small margin of the file's real end (`stroff + strsize >= file_size - 0x10`).
Compilers routinely leave a handful of zero alignment bytes after the string table — completely
normal, completely ignored by the OS loader. But once the code signature (previously the file's
tail) is stripped, those alignment bytes become the new tail, and on some binaries the gap is just
large enough to trip that assertion, even though the binary is structurally valid by every measure
that actually matters.

`_normalize_symtab_string_padding` folds that harmless padding into the recorded string-table size
so the string table appears to end exactly at the file's end — but only after checking, byte by
byte, that the gap really is all zeros, that it's small (1-16 bytes, not something that looks like
real missing data), and that no *other* load command's data range extends into it first. Get any
of those checks wrong and you'd silently corrupt a binary that happened to have real data sitting
in what looked like padding. The function's failure mode is deliberately "do nothing" — every guard
that doesn't pass just returns the input unchanged, never a partial patch.

## How I know it's correct

This isn't validated by "it ran without an exception." Before this implementation replaced a
different, slower signature-removal path in the real pipeline, both were run against the same real
binaries and their output was diffed byte-for-byte until it matched exactly — a from-scratch binary
patcher earns trust from bytes matching, not from the absence of a stack trace.

## What's deliberately NOT in this snippet

This is the standalone stripping logic only. The real pipeline wraps it with an app-specific
walker (find every Mach-O in an extracted bundle, delete `_CodeSignature` directories, prefer the
native `codesign` when it's actually available and fall back to this otherwise) — left out here
because that part is genuinely pipeline-specific glue, not general-purpose. `strip_file` and
`strip_directory` at the bottom of `macho_strip.py` are a minimal, complete version of that glue
if you want it.
