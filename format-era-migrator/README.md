# format-era-migrator

Parse a command written in one of several historical format "eras," detected from its own syntax
shape, into an era-agnostic intermediate representation — then recompile it in ANY target era,
with explicit tracking of anything that can't be represented there.

```js
const { convert } = require('./era_migrator');
convert('give @p sword{display:{Name:"Excalibur"}} 1', 'modern');
// -> { command: 'give @p sword[custom_name="Excalibur"] 1', fromEra: 'legacy', toEra: 'modern', dropped: [] }
```

This is a standalone, generalized version of the core algorithm behind a command converter I
built into a native macOS app for Minecraft: paste a command written for an old game version and
convert it to run on a current one, or the reverse, across three genuinely different command
syntaxes the game has used historically for the exact same concepts (a raw NBT-tag block → a
components-wrapped intermediate form → today's bracketed key=value form). The real implementation
covers the game's full item and entity data model, spanning every official release from 1.13.2 to
26.1.2; this version demonstrates the pattern with a small synthetic schema instead, so it's
runnable and testable entirely on its own.

## The actual problem

Three sequential format eras means a naive "convert A to B" function only solves one of the three
real conversion directions people actually want (old→new, new→old, and — since players share
commands across communities running different versions — middle-era→either end). Writing that as
N×N pairwise converters duplicates the same field-mapping logic three-to-six times and gets worse
every time the game adds a fourth era.

## The shape that avoids that

Parse INTO a single era-agnostic intermediate spec once, regardless of source era, then compile
FROM that spec into any target era. Adding support for converting between any two eras is then
"can this spec's fields be parsed from era X" and "can this spec's fields be compiled to era Y" —
independent concerns, each written once — rather than a combinatorial explosion of pairwise
converters.

## Being honest about lossy conversions

Not every field in a richer era has an equivalent in an older one. The real implementation
distinguishes structural fields that convert cleanly across all eras from entity-specific data that
passes through unchanged because it's era-invariant, and flags what doesn't survive a specific
conversion rather than silently dropping it or producing a command that would fail to parse in the
target version. This standalone version keeps that same discipline: converting a modern-only
enchantment format down to the legacy era's numeric-only enchantment IDs returns an explicit
`dropped` list naming exactly what didn't make the trip, instead of quietly emitting an incomplete
command.

## Verified

Round-tripped a command legacy → modern → legacy and confirmed the output matches the original
byte-for-byte (the lossless case, since the input only used features the legacy era can represent).
Separately verified the lossy direction: converting a modern command using named enchantments (no
legacy equivalent in this schema) down to legacy correctly drops exactly those two enchantments and
reports them by name in `dropped`, rather than emitting a legacy command referencing
enchantment IDs that don't exist. Confirmed era auto-detection correctly identifies all three
formats from their syntax alone, and that unrecognized input returns `null` rather than throwing or
silently producing garbage.
