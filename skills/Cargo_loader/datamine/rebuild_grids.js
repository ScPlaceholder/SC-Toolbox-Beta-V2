#!/usr/bin/env node
/*
 * rebuild_grids.js - rebuild Cargo Loader ship grids from scunpacked-data.
 *
 * Source: github.com/StarCitizenWiki/scunpacked-data (ships.json, one game build).
 * Target: the loader .cargo_cache.json ship format:
 *   { manufacturer, name, capacity, groups: [{ x, z, grids: [{ x, y, z,
 *     width, height, length, minSize?, maxSize? }] }], labels: [...] }
 *
 * The loader axes (width/height/length) do NOT necessarily equal the
 * files X/Y/Z. The axis mapping is derived, not hardcoded: `derive` scores
 * all 6 permutations across every ship present in both sources and reports
 * the evidence. `diff` produces the ship-by-ship review report. Nothing is
 * written into the loader live cache by this tool.
 *
 * MinSize/MaxSize: scunpacked carries them per grid as metre extents
 * ({X,Y,Z}); the loader format carries scalar container classes (1..32 SCU).
 * Object forms are converted to cells with the SAME axis mapping as the grid
 * dims and reduced to classes:
 *   maxSize = largest class whose dims fit the extents in some rotation
 *   minSize = smallest class whose dims reach the extents in some rotation
 * Scalar forms (older fixtures, sc-cargo.space dumps) pass through unchanged.
 *
 * Usage:
 *   node rebuild_grids.js inspect  <ships.json>
 *   node rebuild_grids.js derive   <ships.json> <cargo_cache.json>
 *   node rebuild_grids.js diff     <ships.json> <cargo_cache.json> [report.md]
 *   node rebuild_grids.js convert  <ships.json> <cargo_cache.json> [XYZmap] [out.json]
 *   node rebuild_grids.js test
 *
 * XYZmap is the axis mapping as the loader axis order spelled in file axes,
 * e.g. "YZX" means width=Y, height=Z, length=X. Defaults to the winner of
 * `derive`.
 *
 * Credit line for the tool UI:
 *   "Ship data: StarCitizenWiki/scunpacked-data. Star Citizen content (c) Cloud Imperium Games."
 */
"use strict";

const fs = require("fs");
const path = require("path");

const M_PER_SCU = 1.25;
const BUILD = "4.10.1-LIVE.12660092";
const COMMIT = "e96132078ae6a1a5f62a183fb1523dc006dcfddb"; // scunpacked-data commit for BUILD
const ATTRIBUTION =
  "Ship data: StarCitizenWiki/scunpacked-data. Star Citizen content (c) Cloud Imperium Games.";

// Container classes, from container_schema.json (w,h,l in cells, long axis l).
// Max stack height constraints mirror cargo_engine.placement.max_containers_in_slot.
const CONTAINER_DIMS = {
  1: [1, 1, 1], 2: [1, 1, 2], 4: [2, 1, 2], 8: [2, 2, 2],
  16: [2, 2, 4], 24: [2, 2, 6], 32: [2, 2, 8],
};
const CONTAINER_MAX_STACK = { 2: 1, 4: 1 };
const CONTAINER_CLASSES = Object.keys(CONTAINER_DIMS).map(Number).sort((a, b) => a - b);

// field access (scunpacked uses PascalCase; accept lowercase too)
function num(obj, ...keys) {
  for (const k of keys) {
    if (obj && typeof obj[k] === "number" && isFinite(obj[k])) return obj[k];
    if (obj && typeof obj[k] === "string" && obj[k] !== "" && isFinite(Number(obj[k])))
      return Number(obj[k]);
  }
  return undefined;
}
function pick(obj, ...keys) {
  for (const k of keys) if (obj && obj[k] !== undefined && obj[k] !== null) return obj[k];
  return undefined;
}

// A MinSize/MaxSize field is either a scalar class (old fixtures, sc-cargo.space)
// or a metre-extents object {X,Y,Z}. Returns {scalar:n} or {vec:{X,Y,Z}}.
function sizeField(obj, ...keys) {
  const v = pick(obj, ...keys);
  if (v === undefined) return undefined;
  if (typeof v === "number" && isFinite(v)) return { scalar: v };
  if (typeof v === "string" && v !== "" && isFinite(Number(v))) return { scalar: Number(v) };
  if (v && typeof v === "object") {
    const X = num(v, "X", "x"), Y = num(v, "Y", "y"), Z = num(v, "Z", "z");
    if (X !== undefined || Y !== undefined || Z !== undefined)
      return { vec: { X: X || 0, Y: Y || 0, Z: Z || 0 } };
  }
  return undefined;
}

// scunpacked ship -> normalized record
function normName(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/\[concept\]|\(concept\)/g, "")
    .replace(/mk\s*(ii|2)/g, "mk2")
    .replace(/mk\s*(i|1)/g, "mk1")
    .replace(/[^a-z0-9]+/g, "")
    .trim();
}

// name matching: scunpacked Names carry the manufacturer prefix and
// sometimes a suffix the loader name lacks ("Crusader A2 Hercules
// Starlifter" vs "A2 Hercules"). Match on (a) exact normalized name with
// the manufacturer prefix stripped, (b) exact normalized full name,
// (c) containment either way, longer key wins, min key length 3.
// Each loader ship is claimed by at most one files ship (linkCache).
function mfrStripName(raw) {
  const name = scShipName(raw, "");
  const mfr = pick(raw, "Manufacturer", "manufacturer") || {};
  const mfrName = (mfr && typeof mfr === "object" ? pick(mfr, "Name", "name") : String(mfr || "")) || "";
  const code = (mfr && typeof mfr === "object" ? pick(mfr, "Code", "code") : "") || "";
  const first = (name.split(/s+/)[0] || "").toLowerCase();
  if (first.length > 1 &&
      (mfrName.toLowerCase().indexOf(first) === 0 ||
       code.toLowerCase() === first ||
       { misc: 1, tmbl: 1, grin: 1, drak: 1, cno: 1, anvl: 1, aegs: 1, orig: 1, argo: 1, crus: 1 }[first]))
    return name.slice(name.indexOf(" ") + 1).trim() || name;
  return name;
}
function buildIndex(ships) {
  return ships.map((s) => ({ key: normName(s.name), ship: s }))
    .sort((a, b) => b.key.length - a.key.length);
}
function findMatch(key, index) {
  if (!key || key.length < 3) return undefined;
  let hit = index.find((e) => e.key === key);
  if (hit) return hit.ship;
  hit = index.find((e) => e.key.length >= 3 &&
    (key.indexOf(e.key) !== -1 || e.key.indexOf(key) !== -1));
  return hit ? hit.ship : undefined;
}
// Claim-aware linking: files ship -> loader ship (or null when new).
// sc ships must expose .name (full) and .base (prefix-stripped).
function linkCache(scShips, cacheShips) {
  const index = buildIndex(cacheShips);
  const claimed = new Set();
  const links = new Map();
  const tryLink = (sc, key) => {
    if (links.has(sc) || !key || key.length < 3) return;
    let hit = index.find((e) => e.key === key && !claimed.has(e.ship));
    if (!hit) hit = index.find((e) => e.key.length >= 3 && !claimed.has(e.ship) &&
      (key.indexOf(e.key) !== -1 || e.key.indexOf(key) !== -1));
    if (hit) { claimed.add(hit.ship); links.set(sc, hit.ship); }
  };
  const byBaseLen = scShips.slice().sort((a, b) =>
    normName(a.base || a.name).length - normName(b.base || b.name).length);
  for (const sc of byBaseLen) tryLink(sc, normName(sc.base || sc.name)); // pass 1: stripped
  for (const sc of byBaseLen) tryLink(sc, normName(sc.name));            // pass 2: full name
  return links;
}

function scShipName(raw, key) {
  return pick(raw, "Name", "name", "ClassName", "className", "vehicleName") || key || "";
}
function scManufacturer(raw) {
  const m = pick(raw, "Manufacturer", "manufacturer", "Maker", "maker");
  if (m && typeof m === "object") return pick(m, "Name", "name") || pick(m, "Code", "code") || "";
  return m || "";
}

// Extract cargo grids from one scunpacked ship entry.
// Tolerant about field casing/extras; returns [] when the ship has no cargo.
function scGrids(raw) {
  const grids = pick(raw, "CargoGrids", "cargoGrids", "CargoGrid", "cargoGrid");
  if (!grids) return [];
  const list = Array.isArray(grids) ? grids : [grids];
  const out = [];
  for (const g of list) {
    const X = num(g, "X", "x", "SizeX", "sizeX");
    const Y = num(g, "Y", "y", "SizeY", "sizeY");
    const Z = num(g, "Z", "z", "SizeZ", "sizeZ");
    if (X === undefined || Y === undefined || Z === undefined) continue;
    const pos = pick(g, "Position", "position", "Offset", "offset") || {};
    out.push({
      X, Y, Z, // metres
      scu: num(g, "SCU", "scu"),
      minSize: sizeField(g, "MinSize", "minSize"),
      maxSize: sizeField(g, "MaxSize", "maxSize"),
      px: num(pos, "X", "x") || 0,
      py: num(pos, "Y", "y") || 0,
      pz: num(pos, "Z", "z") || 0,
    });
  }
  return out;
}

function loadScunpacked(file) {
  const obj = JSON.parse(fs.readFileSync(file, "utf8"));
  const entries = Array.isArray(obj)
    ? obj.map((e, i) => [String(i), e])
    : Object.entries(obj);
  const ships = [];
  for (const [key, raw] of entries) {
    if (!raw || typeof raw !== "object") continue;
    const grids = scGrids(raw);
    if (!grids.length) continue; // non-cargo entries carry no CargoGrids
    ships.push({
      key,
      name: scShipName(raw, key),
      base: mfrStripName(raw),
      manufacturer: scManufacturer(raw),
      grids,
      raw,
    });
  }
  const seen = new Set();
  return ships.filter((sh) => {
    const sig = normName(sh.name) + "|" +
      sh.grids.map((g) => [g.X, g.Y, g.Z].join("x")).sort().join("|");
    if (seen.has(sig)) return false;
    seen.add(sig);
    return true;
  });
}

function loadCache(file) {
  const obj = JSON.parse(fs.readFileSync(file, "utf8"));
  return obj.ships || [];
}

// When one files Name carries several distinct grid sets (e.g. two
// Aegis Hammerhead entries, 40 vs 64 SCU), keep the set that matches the
// loader baseline; on a tie keep the plainest ClassName. The dropped sets
// are reported on stderr.
function resolveDuplicates(scShips, cacheShips) {
  const index = buildIndex(cacheShips);
  const byName = new Map();
  for (const sc of scShips) {
    const k = normName(sc.name);
    if (!byName.has(k)) byName.set(k, []);
    byName.get(k).push(sc);
  }
  const drop = new Set();
  for (const group of byName.values()) {
    const sigs = new Map();
    for (const sc of group) {
      const sig = sc.grids.map((g) => [g.X, g.Y, g.Z].map((m) => Math.round(m / M_PER_SCU)).sort((a, b) => a - b).join("x")).sort().join("|");
      if (!sigs.has(sig)) sigs.set(sig, []);
      sigs.get(sig).push(sc);
    }
    if (sigs.size <= 1) continue;
    const old = findMatch(normName(group[0].base || group[0].name), index) ||
                findMatch(normName(group[0].name), index);
    let keep = null;
    if (old) {
      const want = cacheGrids(old).map((g) => [g.w, g.h, g.l].sort((a, b) => a - b).join("x")).sort().join("|");
      for (const [sig, list] of sigs) if (sig === want) { keep = list[0]; break; }
    }
    if (!keep) {
      let best = null;
      for (const list of sigs.values())
        for (const sc of list) {
          const cls = pick(sc.raw, "ClassName", "className") || "";
          if (!best || cls.length < best.cls.length) best = { sc, cls };
        }
      keep = best.sc;
    }
    for (const sc of group) if (sc !== keep) {
      drop.add(sc);
      console.error("resolveDuplicates: kept '" + keep.name + "' (" +
        (pick(keep.raw, "ClassName", "className") || keep.key) + "), dropped '" +
        (pick(sc.raw, "ClassName", "className") || sc.key) + "'");
    }
  }
  return scShips.filter((sc) => !drop.has(sc));
}

// loader cache helpers
function cacheGrids(ship) {
  const out = [];
  for (const group of ship.groups || []) {
    const gx = group.x || 0, gz = group.z || 0;
    for (const g of group.grids || []) {
      out.push({
        x: gx + (g.x || 0),
        y: g.y || 0,
        z: gz + (g.z || 0),
        w: Math.max(1, g.width || 1),
        h: Math.max(1, g.height || 1),
        l: Math.max(1, g.length || 1),
        maxSize: g.maxSize !== undefined ? g.maxSize : null,
        minSize: g.minSize !== undefined ? g.minSize : null,
      });
    }
  }
  return out;
}
function cacheVolume(ship) {
  return cacheGrids(ship).reduce((a, g) => a + g.w * g.h * g.l, 0);
}

// axis mapping: map = {w, h, l} each one of X|Y|Z (a permutation)
const PERMUTATIONS = (() => {
  const perms = [];
  for (const w of "XYZ") for (const h of "XYZ") for (const l of "XYZ")
    if (w !== h && h !== l && w !== l) perms.push({ w, h, l });
  return perms;
})();

// metres -> cells; flags values that are not near a multiple of 1.25 m
function toCells(metres) {
  const exact = metres / M_PER_SCU;
  const cells = Math.round(exact);
  return { cells, exact, roundError: Math.abs(exact - cells) };
}

function mappedTriple(grid, map) {
  return [grid[map.w], grid[map.h], grid[map.l]];
}

// ordered-within, sorted-across multiset key. Axes are NOT sorted inside a
// triple: doing so would make all 6 axis permutations indistinguishable.
// Grid order within a ship is irrelevant.
function shapeMultiset(grids, map, cellsFn) {
  const keys = grids
    .map((g) => {
      const t = map ? mappedTriple(g, map) : [g.w, g.h, g.l];
      const c = cellsFn ? t.map(cellsFn) : t;
      return c.join("x");
    })
    .sort();
  return keys.join("|");
}

// Rotation-tolerant variant: two grids are considered the same shape when
// they are identical or one is the other with w and l swapped (height is
// never swapped - a 2-high bay is not a 2-long bay). Canonical key per grid
// is the lexicographically smaller of "wxhxl" / "lxhxw".
function rotationMultiset(grids) {
  return grids
    .map((g) => {
      const a = [g.w, g.h, g.l].join("x");
      const b = [g.l, g.h, g.w].join("x");
      return a < b ? a : b;
    })
    .sort()
    .join("|");
}

// unique rotations of a container's dims, honoring its max stack height
function rotationsOf(cls) {
  const dims = CONTAINER_DIMS[cls];
  const maxCh = CONTAINER_MAX_STACK[cls];
  const seen = new Set();
  const out = [];
  for (const a of dims) for (const b of dims) for (const c of dims) {
    const have = {};
    for (const d of [a, b, c]) have[d] = (have[d] || 0) + 1;
    const need = {};
    for (const d of dims) need[d] = (need[d] || 0) + 1;
    let same = true;
    for (const d in need) if (need[d] !== have[d]) same = false;
    if (!same) continue;
    const key = [a, b, c].join(",");
    if (seen.has(key)) continue;
    seen.add(key);
    if (maxCh !== undefined && b > maxCh) continue; // height exceeds stack limit
    out.push([a, b, c]);
  }
  return out;
}

// largest container class whose dims fit inside the MaxSize extents
function maxSizeClass(field, map) {
  if (field === undefined) return null;
  if (field.scalar !== undefined) return field.scalar;
  const ext = mappedTriple(field.vec, map).map((m) => toCells(m).cells);
  for (let i = CONTAINER_CLASSES.length - 1; i >= 0; i--)
    if (rotationsOf(CONTAINER_CLASSES[i]).some(([cw, ch, cl]) =>
        cw <= ext[0] && ch <= ext[1] && cl <= ext[2])) return CONTAINER_CLASSES[i];
  return null;
}

// smallest container class whose dims reach the MinSize extents in every axis
// (under some rotation): a bay whose min extent is 2 cells rejects 1-SCU boxes.
function minSizeClass(field, map) {
  if (field === undefined) return null;
  if (field.scalar !== undefined) return field.scalar;
  const ext = mappedTriple(field.vec, map).map((m) => toCells(m).cells);
  for (const cls of CONTAINER_CLASSES) {
    if (rotationsOf(cls).some(([cw, ch, cl]) =>
        cw >= ext[0] && ch >= ext[1] && cl >= ext[2])) return cls;
  }
  return CONTAINER_CLASSES[CONTAINER_CLASSES.length - 1];
}

// Score every permutation: how many ships present in both sources have an
// identical grid-shape multiset under that mapping (secondary: SCU total).
function derive(scShips, cacheShips) {
  const links = linkCache(scShips, cacheShips);
  const rows = PERMUTATIONS.map((map) => ({
    map,
    exactShape: 0,
    gridMatch: 0,
    scuMatch: 0,
    shipsCompared: 0,
  }));
  for (const sc of scShips) {
    const cs = links.get(sc);
    if (!cs) continue;
    const loaderShapes = shapeMultiset(cacheGrids(cs), null, null);
    const loaderScu = typeof cs.capacity === "number" ? cs.capacity : cacheVolume(cs);
    const filesScu = sc.grids.reduce((a, g) => a + (g.scu ?? 0), 0);
    for (const row of rows) {
      const mapped = sc.grids.map((g) => {
        const t = mappedTriple(g, row.map);
        const c = t.map((m) => toCells(m).cells);
        return { w: c[0], h: c[1], l: c[2] };
      });
      if (shapeMultiset(mapped, null, null) === loaderShapes) row.exactShape++;
      const want = loaderShapes.split('|').filter(Boolean);
      const have = shapeMultiset(mapped, null, null).split('|').filter(Boolean);
      let matched = 0;
      const pool = have.slice();
      for (const t of want) { const i = pool.indexOf(t); if (i !== -1) { matched++; pool.splice(i, 1); } }
      row.gridMatch += matched;
      if (filesScu === loaderScu) row.scuMatch++;
      row.shipsCompared++;
    }
  }
  rows.sort((a, b) => b.exactShape - a.exactShape || b.gridMatch - a.gridMatch || b.scuMatch - a.scuMatch);
  return rows;
}

// diff
function diff(scShips, cacheShips, map) {
  const links = linkCache(scShips, cacheShips);
  const scByLink = new Map();
  for (const [sc, old] of links) scByLink.set(old, sc);
  const linkedSc = new Set(links.keys());

  const report = {
    map,
    both: [],
    onlyLoader: [],
    onlyFiles: [],
    roundingFlags: [],
  };

  for (const cs of cacheShips) {
    const sc = scByLink.get(cs);
    if (!sc) { report.onlyLoader.push(cs.name); continue; }

    const loaderGrids = cacheGrids(cs);
    const loaderShapes = shapeMultiset(loaderGrids, null, null);
    const loaderRotShapes = rotationMultiset(loaderGrids);
    const loaderScu = cacheVolume(cs);
    const filesScu = sc.grids.reduce((a, g) => a + (g.scu ?? 0), 0);

    const mapped = sc.grids.map((g) => {
      const t = mappedTriple(g, map);
      const cells = t.map(toCells);
      return {
        dims: cells.map((c) => c.cells),
        roundErrors: cells.map((c) => c.roundError),
        scu: g.scu,
        minCls: minSizeClass(g.minSize, map),
        maxCls: maxSizeClass(g.maxSize, map),
      };
    });
    for (const m of mapped)
      if (m.roundErrors.some((e) => e > 0.02))
        report.roundingFlags.push(
          sc.name + ": " + m.dims.join("x") + " (errors " +
            m.roundErrors.map((e) => e.toFixed(2)).join(",") + ")"
        );

    const asShapes = mapped.map((m) => ({ w: m.dims[0], h: m.dims[1], l: m.dims[2] }));
    const filesShapes = shapeMultiset(asShapes, null, null);
    const filesRotShapes = rotationMultiset(asShapes);

    let cls, shape;
    if (filesShapes === loaderShapes && filesScu === loaderScu) { cls = "MATCH_ALL"; shape = "same"; }
    else if (filesRotShapes === loaderRotShapes) { cls = "SCU_ONLY_ROT"; shape = "rotation"; }
    else if (filesScu === loaderScu) { cls = "SCU_ONLY"; shape = "different"; }
    else { cls = "DIFF"; shape = "different"; }

    report.both.push({
      name: cs.name,
      class: cls,
      shape,
      loaderScu, filesScu,
      loaderGrids: loaderGrids.map((g) => [g.w, g.h, g.l].join("x")),
      filesGrids: mapped.map((m) => m.dims.join("x")),
      minSize: mapped.map((m) => (m.minCls == null ? "null" : m.minCls)),
      maxSize: mapped.map((m) => (m.maxCls == null ? "null" : m.maxCls)),
    });
  }
  for (const sc of scShips)
    if (!linkedSc.has(sc)) report.onlyFiles.push(sc.name);

  report.both.sort((a, b) => a.class.localeCompare(b.class) || a.name.localeCompare(b.name));
  report.onlyLoader.sort();
  report.onlyFiles.sort();
  return report;
}

function reportMarkdown(report, deriveRows) {
  const counts = { MATCH_ALL: 0, SCU_ONLY_ROT: 0, SCU_ONLY: 0, DIFF: 0 };
  for (const b of report.both) counts[b.class]++;
  const L = [];
  L.push("# Cargo grid diff - scunpacked-data " + BUILD + " vs loader cache");
  L.push("");
  L.push("Source commit: `" + COMMIT + "`. 1 SCU cell = " + M_PER_SCU + " m.");
  L.push("");
  L.push("## Axis mapping evidence (all 6 permutations, ships present in both sources)");
  L.push("");
  L.push("| mapping (w,h,l <- files) | exact shape matches | grids matched | total-SCU matches | ships compared |");
  L.push("|---|---|---|---|");
  for (const r of deriveRows)
    L.push(
      "| " + r.map.w + "," + r.map.h + "," + r.map.l + " | " + r.exactShape + " | " + r.gridMatch +
        " | " + r.scuMatch + " | " + r.shipsCompared + " |"
    );
  L.push("");
  L.push("Chosen mapping: **width=" + report.map.w + ", height=" + report.map.h +
    ", length=" + report.map.l + "** (file axes).");
  L.push("");
  L.push("Grid-shape equality for the SCU_ONLY split allows each grid's footprint to rotate");
  L.push("(w and l swapped, height never swapped) and ignores grid order within the ship.");
  L.push("");
  L.push("## Summary counts");
  L.push("");
  L.push("- MATCH_ALL (grid shapes + total SCU agree): **" + counts.MATCH_ALL + "**");
  L.push("- SCU_ONLY_ROT (total SCU agrees; shapes differ only by grid order/rotation): **" + counts.SCU_ONLY_ROT + "**");
  L.push("- SCU_ONLY (total SCU agrees, real shape differences): **" + counts.SCU_ONLY + "**");
  L.push("- DIFF (SCU differs): **" + counts.DIFF + "**");
  L.push("- only in loader (sc-cargo.space): **" + report.onlyLoader.length + "**");
  L.push("- only in scunpacked: **" + report.onlyFiles.length + "**");
  L.push("");
  if (report.roundingFlags.length) {
    L.push("## Rounding flags (dims not a clean multiple of 1.25 m; nearest cell used)");
    L.push("");
    for (const f of report.roundingFlags) L.push("- " + f);
    L.push("");
  }
  for (const cls of ["SCU_ONLY", "DIFF"]) {
    L.push("## " + cls);
    L.push("");
    for (const b of report.both.filter((x) => x.class === cls)) {
      L.push("### " + b.name);
      L.push("- loader SCU " + b.loaderScu + " (" + b.loaderGrids.join(", ") + ")");
      L.push("- files  SCU " + b.filesScu + " (" + b.filesGrids.join(", ") + ")");
      L.push("- files minSize per grid (class): " + b.minSize.join(", "));
      L.push("- files maxSize per grid (class): " + b.maxSize.join(", "));
      L.push("");
    }
  }
  L.push("## SCU_ONLY_ROT (order/rotation only - not real differences)");
  L.push("");
  for (const b of report.both.filter((x) => x.class === "SCU_ONLY_ROT")) {
    L.push("### " + b.name);
    L.push("- loader SCU " + b.loaderScu + " (" + b.loaderGrids.join(", ") + ")");
    L.push("- files  SCU " + b.filesScu + " (" + b.filesGrids.join(", ") + ")");
    L.push("");
  }
  L.push("## Only in loader (not in scunpacked ships.json)");
  L.push("");
  for (const n of report.onlyLoader) L.push("- " + n);
  L.push("");
  L.push("## Only in scunpacked (not in loader cache)");
  L.push("");
  for (const n of report.onlyFiles) L.push("- " + n);
  L.push("");
  L.push("_Attribution: " + ATTRIBUTION + "_");
  return L.join("\n");
}

// convert
function convert(scShips, cacheShips, map) {
  const links = linkCache(scShips, cacheShips);
  const ships = [];
  for (const sc of scShips) {
    const old = links.get(sc) || null;
    let cursor = 0;
    const grids = sc.grids.map((g) => {
      const t = mappedTriple(g, map).map((m) => toCells(m).cells);
      const out = {
        x: cursor,
        y: 0,
        z: 0,
        width: t[0], height: t[1], length: t[2],
      };
      cursor += t[0] + 1; // 1-cell gap; positions are layout-only (packing is per-slot)
      const mn = minSizeClass(g.minSize, map);
      const mx = maxSizeClass(g.maxSize, map);
      if (mn !== null && mn !== undefined) out.minSize = mn;
      if (mx !== null && mx !== undefined) out.maxSize = mx;
      return out;
    });
    const capacity = sc.grids.reduce((a, g) => a + (g.scu ?? 0), 0);
    ships.push({
      manufacturer: (old && old.manufacturer) || sc.manufacturer || "",
      name: (old && old.name) || sc.base || sc.name, // keep the loader display name when known
      capacity,
      groups: [{ x: 0, z: 0, grids }],
      labels: (old && old.labels) || [],
      provenance: { source: "scunpacked-data", build: BUILD, commit: COMMIT },
    });
  }
  // Loader ships with no scunpacked counterpart (concepts, unreleased,
  // or ships whose game files carry no cargo grid) are carried over
  // unchanged so the rebuild never loses ships.
  const claimed = new Set(links.values());
  for (const old of cacheShips) {
    if (claimed.has(old)) continue;
    ships.push(old);
  }
  ships.sort((a, b) => a.name.localeCompare(b.name));
  return { ts: Date.now() / 1000, ships };
}

// inspect
function inspect(file) {
  const obj = JSON.parse(fs.readFileSync(file, "utf8"));
  const entries = Array.isArray(obj)
    ? obj.map((e, i) => [String(i), e])
    : Object.entries(obj);
  for (const [k, raw] of entries.slice(0, 3)) {
    if (!raw || typeof raw !== "object") continue;
    console.log("- entry " + JSON.stringify(k) + ":", Object.keys(raw).join(", "));
    const cg = pick(raw, "CargoGrids", "cargoGrids");
    if (cg) {
      const first = (Array.isArray(cg) ? cg : [cg])[0];
      console.log("  CargoGrids[0] keys:", Object.keys(first).join(", "));
      console.log("  CargoGrids[0]:", JSON.stringify(first).slice(0, 300));
    }
    console.log("  Name-ish:", scShipName(raw, k), "| Manufacturer-ish:", scManufacturer(raw));
  }
  console.log("total entries:", entries.length);
}

// fixture test: `node rebuild_grids.js test`
// Fails if a grid whose files entry carries MaxSize converts to a null/missing
// maxSize (the original bug: num() on an object extent returned undefined).
function test() {
  const dir = path.join(__dirname, "fixtures");
  const scShips = loadScunpacked(path.join(dir, "ships_fixture.json"));
  const cache = loadCache(path.join(dir, "cache_fixture.json"));
  const map = { w: "X", h: "Z", l: "Y" };
  let failures = 0;
  const check = (label, cond, detail) => {
    if (cond) console.log("ok   " + label);
    else { failures++; console.error("FAIL " + label + (detail ? " - " + detail : "")); }
  };

  // --- unit: extent -> class reduction ---
  // Drake Caterpillar Nose-style extents: MaxSize {X:5, Y:5, Z:2.5} m.
  // map w=X,h=Z,l=Y -> cells (4, 2, 4): class 16 (2x2x4) is the largest that fits.
  const noseMax = maxSizeClass({ vec: { X: 5, Y: 5, Z: 2.5 } }, map);
  check("MaxSize {5,5,2.5} m -> class 16", noseMax === 16, "got " + noseMax);
  check("class 24 (2x2x6) does not fit 4x2x4",
    !rotationsOf(24).some(([cw, ch, cl]) => cw <= 4 && ch <= 2 && cl <= 4));
  const noseMin = minSizeClass({ vec: { X: 1.25, Y: 1.25, Z: 1.25 } }, map);
  check("MinSize {1.25,1.25,1.25} m -> class 1", noseMin === 1, "got " + noseMin);
  // scalar passthrough (old fixture/sc-cargo.space form)
  check("scalar MaxSize 2 passes through",
    maxSizeClass({ scalar: 2 }, map) === 2);
  check("missing MaxSize -> null", maxSizeClass(undefined, map) === null);

  // --- rotation multiset ---
  check("rotationMultiset treats w<->l swap as equal",
    rotationMultiset([{ w: 1, h: 1, l: 4 }]) === rotationMultiset([{ w: 4, h: 1, l: 1 }]));
  check("rotationMultiset keeps height significant",
    rotationMultiset([{ w: 1, h: 2, l: 3 }]) !== rotationMultiset([{ w: 1, h: 3, l: 2 }]));

  // --- fixture convert: no null maxSize where the files carry one ---
  const data = convert(scShips, cache, map);
  const byName = new Map(scShips.map((s) => [s.name, s]));
  let checked = 0;
  const nulls = [];
  for (const ship of data.ships) {
    const scShip = byName.get(ship.name);
    if (!scShip) continue; // carried-over loader ship
    const grids = (ship.groups || []).flatMap((grp) => grp.grids || []);
    for (let i = 0; i < grids.length; i++) {
      if (scShip.grids[i] && scShip.grids[i].maxSize !== undefined) {
        checked++;
        if (grids[i].maxSize === undefined || grids[i].maxSize === null)
          nulls.push(ship.name + " grid " + i + " (" +
            [grids[i].width, grids[i].height, grids[i].length].join("x") + ")");
      }
    }
  }
  check("every files grid with MaxSize produced a maxSize class", nulls.length === 0,
    nulls.join("; ") || (checked + " grids checked"));

  // --- fixture diff runs with the new classification ---
  const rep = diff(scShips, cache, map);
  // 3 fixture cache ships; Caterpillar + Avenger Titan link to files ships,
  // MOLE stays only-in-loader.
  check("diff linked the expected fixture ships", rep.both.length === 2 &&
    rep.onlyLoader.length === 1, "both=" + rep.both.length + " onlyLoader=" + rep.onlyLoader.length);

  if (failures) { console.error(failures + " fixture test(s) FAILED"); process.exit(1); }
  console.log("all fixture tests passed");
}

// main
function main() {
  const [, , cmd, ...args] = process.argv;
  const die = (msg) => { console.error(msg); process.exit(2); };
  if (!cmd || cmd === "help" || cmd === "--help") return die(
    "usage: rebuild_grids.js inspect|derive|diff|convert|test ... (see header comment)"
  );

  if (cmd === "test") return test();

  if (cmd === "inspect") {
    if (!args[0]) die("inspect needs <ships.json>");
    return inspect(args[0]);
  }

  if (cmd === "derive") {
    const [sf, cf] = args;
    if (!sf || !cf) die("derive needs <ships.json> <cargo_cache.json>");
    const rows = derive(resolveDuplicates(loadScunpacked(sf), loadCache(cf)), loadCache(cf));
    const total = rows[0].shipsCompared;
    for (const r of rows)
      console.log(
        "w,h,l = " + r.map.w + "," + r.map.h + "," + r.map.l +
        "  exact-shape " + r.exactShape + "/" + total +
        "  grids " + r.gridMatch +
        "  scu " + r.scuMatch + "/" + total
      );
    return;
  }

  if (cmd === "diff") {
    const [sf, cf, out] = args;
    if (!sf || !cf) die("diff needs <ships.json> <cargo_cache.json> [report.md]");
    const cache = loadCache(cf);
    const sc = resolveDuplicates(loadScunpacked(sf), cache);
    const rows = derive(sc, cache);
    const map = rows[0].map;
    const rep = diff(sc, cache, map);
    const md = reportMarkdown(rep, rows);
    if (out) { fs.writeFileSync(out, md); console.log("wrote " + out); }
    else console.log(md);
    return;
  }

  if (cmd === "convert") {
    const [sf, cf, mapStr, out] = args;
    if (!sf || !cf) die("convert needs <ships.json> <cargo_cache.json> [XYZmap] [out.json]");
    const cache = loadCache(cf);
    const sc = resolveDuplicates(loadScunpacked(sf), cache);
    let map;
    if (mapStr) {
      const m = /^([XYZ])([XYZ])([XYZ])$/.exec(mapStr);
      if (!m || new Set(m.slice(1)).size !== 3) die("XYZmap must be a permutation of XYZ, e.g. YZX");
      map = { w: m[1], h: m[2], l: m[3] };
    } else {
      map = derive(sc, cache)[0].map;
      console.error("axis map derived: w=" + map.w + " h=" + map.h + " l=" + map.l);
    }
    const data = convert(sc, cache, map);
    const target = out || path.join(process.cwd(), "cargo_grids_scunpacked.json");
    fs.writeFileSync(target, JSON.stringify(data, null, 1));
    console.log("wrote " + target + " (" + data.ships.length + " ships)");
    return;
  }

  die("unknown command: " + cmd);
}

main();
