/**
 * era_migrator.js — parse a command/document written in one of several historical format "eras"
 * into an era-agnostic intermediate representation, then recompile it in any target era, with
 * explicit tracking of anything that doesn't survive the trip.
 *
 * This is a standalone, generalized version of the core algorithm behind a command converter I
 * built into a native macOS app for Minecraft: paste a command written for an old game version,
 * convert it to work on a new one (or vice versa), across THREE historically different command
 * syntaxes the game has used for the same concepts. The real implementation covers the game's full
 * item/entity data model; this version demonstrates the pattern with a small synthetic schema
 * instead, so it's runnable and testable on its own.
 *
 * The pattern generalizes past games: any system that has changed its own serialization format
 * over time (a config file format, an API request shape, a save-file layout) faces the same
 * problem — given input in an unknown-but-detectable era, produce equivalent output in a target
 * era, and be honest about what couldn't be represented there.
 */

const ERAS = ["legacy", "intermediate", "modern"];

/**
 * Detect which era a raw command string was written in, from its own syntax shape — no external
 * version metadata needed, the syntax itself is the signal (legacy uses a raw NBT-tag block,
 * intermediate wraps fields in a "components" object, modern uses bracketed key=value pairs).
 */
function detectEra(raw) {
  if (/\[[a-z_]+=/.test(raw)) return "modern";
  if (/custom_name:'/.test(raw) || /enchantments:\{levels:/.test(raw)) return "intermediate";
  if (/\{display:|\{ench:|\{[a-z_]+:\{/.test(raw)) return "legacy";
  return null;
}

/** Parse a raw command into an era-agnostic spec: { id, count, displayName, enchantments }. */
function parse(raw) {
  const era = detectEra(raw);
  if (!era) return null;

  const idMatch = raw.match(/^give \S+ ([a-z_:]+)/);
  const countMatch = raw.match(/\s(\d+)\s*$/);
  const spec = {
    id: idMatch ? idMatch[1] : null,
    count: countMatch ? Number(countMatch[1]) : 1,
    displayName: null,
    enchantments: [],
  };

  if (era === "legacy") {
    const nameMatch = raw.match(/display:\{Name:"([^"]*)"\}/);
    if (nameMatch) spec.displayName = nameMatch[1];
    const enchMatch = raw.match(/ench:\[([^\]]*)\]/);
    if (enchMatch) {
      spec.enchantments = enchMatch[1]
        .split(",")
        .map((e) => e.match(/id:(\d+)/))
        .filter(Boolean)
        .map((m) => `legacy_ench_${m[1]}`);
    }
  } else if (era === "intermediate") {
    const nameMatch = raw.match(/custom_name:'"([^"]*)"'/);
    if (nameMatch) spec.displayName = nameMatch[1];
    const enchMatch = raw.match(/enchantments:\{levels:\{([^}]*)\}\}/);
    if (enchMatch) {
      spec.enchantments = enchMatch[1]
        .split(",")
        .map((e) => e.split(":")[0])
        .filter(Boolean);
    }
  } else {
    // modern: bracketed key=value
    const nameMatch = raw.match(/custom_name="([^"]*)"/);
    if (nameMatch) spec.displayName = nameMatch[1];
    const enchMatch = raw.match(/enchantments=\{([^}]*)\}/);
    if (enchMatch) {
      spec.enchantments = enchMatch[1]
        .split(",")
        .map((e) => e.split(":")[0].trim())
        .filter(Boolean);
    }
  }

  return { spec, era };
}

/**
 * Recompile a spec in a target era's syntax. Some fields don't survive every trip — e.g. this toy
 * schema's "legacy" era has no concept of a structured display-name style, only a raw string, so
 * compiling FROM a richer era TO legacy silently would lose information. `dropped` makes that
 * explicit instead of silent.
 */
function compile(spec, targetEra) {
  const dropped = [];
  let cmd = `give @p ${spec.id}`;

  const extras = [];
  if (spec.displayName) {
    if (targetEra === "legacy") extras.push(`display:{Name:"${spec.displayName}"}`);
    else if (targetEra === "intermediate") extras.push(`custom_name:'"${spec.displayName}"'`);
    else extras.push(`custom_name="${spec.displayName}"`);
  }
  if (spec.enchantments.length) {
    if (targetEra === "legacy") {
      // This toy legacy format can only represent numeric-id enchantments; a named enchantment
      // (from a richer source era) has no legacy equivalent in this simplified schema, so it's
      // dropped rather than emitted as something that would fail to parse in-game.
      const numeric = spec.enchantments.filter((e) => e.startsWith("legacy_ench_"));
      const lost = spec.enchantments.filter((e) => !e.startsWith("legacy_ench_"));
      dropped.push(...lost.map((e) => `enchantment '${e}' (no legacy numeric-id equivalent in this schema)`));
      if (numeric.length) {
        extras.push(`ench:[${numeric.map((e) => `{id:${e.replace("legacy_ench_", "")}}`).join(",")}]`);
      }
    } else if (targetEra === "intermediate") {
      extras.push(`enchantments:{levels:{${spec.enchantments.map((e) => `${e}:1`).join(",")}}}`);
    } else {
      extras.push(`enchantments={${spec.enchantments.map((e) => `${e}:1`).join(",")}}`);
    }
  }

  if (extras.length) {
    cmd += targetEra === "legacy" ? `{${extras.join(",")}}` : `[${extras.join(",")}]`;
  }
  cmd += ` ${spec.count}`;

  return { command: cmd, dropped };
}

/** Convert a raw command from whatever era it's written in to a target era. */
function convert(raw, targetEra) {
  const parsed = parse(raw);
  if (!parsed) return null;
  const { command, dropped } = compile(parsed.spec, targetEra);
  return { command, fromEra: parsed.era, toEra: targetEra, dropped };
}

module.exports = { ERAS, detectEra, parse, compile, convert };
