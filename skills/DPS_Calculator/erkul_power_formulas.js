// =============================================================================
// ERKUL.GAMES POWER ALLOCATOR FORMULAS
// Reverse-engineered from main.df4e6a453b05ea42.js
// =============================================================================

// =============================================================================
// 1. POWER PLANT OUTPUT CALCULATION (totalAvailablePowerSegments)
// =============================================================================
// Context: `e` = powerPlants array, `i` = weapons array from extractItems()
// `A` = powered-on power plant items

let E = 0, k = 0;
const A = e
  .map(V => this.loadout?.find(Q => Q.item === V))
  .filter(V => V?.poweredOn)
  .map(V => V?.item)
  .filter(V => V);

A.forEach(V => {
  // Sum each PP's powerSegment generation, divided by number of active PPs
  E += Math.round((V.data?.resource?.online?.generation?.powerSegment || 0) / A.length);
  // Sum each PP's size
  k += V.data?.size || 0;
});

// FORMULA: totalSegments = roundedSegmentSum + (numActivePPs - 1) * totalSize
this.results.powerConfiguration.totalAvailablePowerSegments = E + (A.length - 1) * k;


// =============================================================================
// 2. WEAPON POOL SETUP (poolSize / pip count for weapons)
// =============================================================================
// Weapons use a FIXED pool from ship data, not from component powerSegment
// `i` = weapons array, `H` = total weapon power consumption

let H = 0;
i.forEach(V => {
  H += V.data?.resource?.online?.consumption?.power || 0;
});
H = Math.ceil(H);

// Create weapon segment array: each pip = 1 segment, size = poolSize
this.segmentConfiguration.weapon = new Array(
  this.ship?.data?.rnPowerPools?.weaponGun?.poolSize || 0
).fill(void 0).map(() => ({ number: 1, selected: false, disabled: false }));

// Disable pips beyond total weapon power consumption
this.segmentConfiguration.weapon.forEach((V, Q) => {
  if (Q >= H) V.disabled = true;
});

this.results.powerConfiguration.weapon = { power: true, segment: 0, usage: 0 };


// =============================================================================
// 3. ENGINE SEGMENT SETUP
// =============================================================================
// `z` = IFCS powerSegment consumption (or WheeledController fallback)
// `B` = powerConsumptionMinimumFraction (or WheeledController fallback), default 1
// `$` = minimum engine segments (critical pip)

const z = this.ship?.data?.ifcs?.resource?.online?.consumption?.powerSegment
  || this.ship?.data?.items?.controllers?.find(V => "WheeledController" === V.data?.type)
       ?.data?.resource?.online?.consumption?.powerSegment || 0;

const B = this.ship?.data?.ifcs?.resource?.online?.powerConsumptionMinimumFraction
  || this.ship?.data?.items?.controllers?.find(V => "WheeledController" === V.data?.type)
       ?.data?.resource?.online?.powerConsumptionMinimumFraction || 1;

const $ = Math.round(z * B);  // critical (minimum) engine segments

this.segmentConfiguration.engine.push(
  { number: $, selected: false, disabled: false },   // first pip = critical (big)
  ...new Array(Math.round(z - $)).fill(void 0)
    .map(() => ({ number: 1, selected: false, disabled: false }))  // remaining = 1 each
);
this.results.powerConfiguration.engine = { power: true, segment: 0 };


// =============================================================================
// 4. GENERIC COMPONENT SEGMENT SETUP PATTERN
// =============================================================================
// Used for: qdrive, tractorBeam, miningLaser, salvage, lifeSupport, radar, qed, emp
// Each component uses: powerSegment for total pips, powerConsumptionMinimumFraction for critical pip
//
// PATTERN (example for qdrive):
//   totalPips = component.data.resource.online.consumption.powerSegment
//   criticalPips = Math.round(totalPips * powerConsumptionMinimumFraction)
//   first pip = { number: criticalPips }  (the "big" critical pip)
//   remaining = totalPips - criticalPips pips of { number: 1 } each
//
// EXCEPTION: lifeSupport and coolers use `conversionMinimumFraction` instead of
//   `powerConsumptionMinimumFraction`, with fallback `1 / (totalPips || 1)`

// --- qdrive example ---
o.forEach(V => {
  const Q = V.data?.resource?.online?.consumption?.powerSegment || 0;
  const te = Math.round(Q * (V.data?.resource?.online?.powerConsumptionMinimumFraction || 1));
  this.segmentConfiguration.qdrive.push(
    { number: te, selected: false, disabled: false },
    ...new Array(Math.round(Q - te)).fill(void 0)
      .map(() => ({ number: 1, selected: false, disabled: false }))
  );
});
this.segmentConfiguration.qdrive.sort((V, Q) => Q.number - V.number);
this.results.powerConfiguration.qdrive = { power: "nav" === this.results.shipMode, segment: 0 };

// --- lifeSupport (uses conversionMinimumFraction) ---
r.forEach(V => {
  const Q = V.data?.resource?.online?.consumption?.powerSegment || 0;
  const te = Math.round(Q * (V.data?.resource?.online?.conversionMinimumFraction || 1 / (Q || 1)));
  this.segmentConfiguration.lifeSupport.push(
    { number: te, selected: false, disabled: false },
    ...new Array(Math.round(Q - te)).fill(void 0)
      .map(() => ({ number: 1, selected: false, disabled: false }))
  );
});

// --- coolers (uses conversionMinimumFraction, stored as array of arrays) ---
a.forEach(V => {
  const Q = V.data?.resource?.online?.consumption?.powerSegment || 0;
  const te = Math.round(Q * (V.data?.resource?.online?.conversionMinimumFraction || 1 / (Q || 1)));
  this.segmentConfiguration.coolers.push([
    { number: te, selected: false, disabled: false },
    ...new Array(Math.round(Q - te)).fill(void 0)
      .map(() => ({ number: 1, selected: false, disabled: false }))
  ]);
});
this.segmentConfiguration.coolers.forEach(V => V.sort((Q, Z) => Z.number - Q.number));
this.results.powerConfiguration.coolers = this.segmentConfiguration.coolers.map(() => ({
  power: true, segment: 0, coolingGeneration: 0
}));


// =============================================================================
// 5. SHIELD SEGMENT SETUP (initShieldSegments)
// =============================================================================
initShieldSegments(shieldItems) {
  this.segmentConfiguration.shield = [];

  const nonEmpty = shieldItems
    .map(r => this.findItem(r))
    .filter(r => "Empty" !== r?.item?.data?.name);

  // If all shields powered on and more than 2, turn off shields after index 1
  if (nonEmpty.every(r => r.poweredOn) && nonEmpty.length > 2) {
    nonEmpty.forEach((r, s) => { if (s > 1) r.poweredOn = false; });
  }

  // Powered-on shields, sorted by critical pip size descending
  const poweredOn = nonEmpty
    .filter(r => r?.poweredOn)
    .map(r => r.item)
    .sort((r, s) => {
      const critSize = h =>
        Math.round(
          (h.data?.resource?.online?.conversionMinimumFraction || 0) *
          (h.data?.resource?.online?.consumption?.powerSegment || 0)
        ) || Infinity;
      return critSize(s) - critSize(r);  // descending
    });

  const poweredOff = nonEmpty.filter(r => !r?.poweredOn).map(r => r.item);

  // Powered-on shields: critical pip + individual pips
  poweredOn.forEach(r => {
    const totalSeg = r.data?.resource?.online?.consumption?.powerSegment || 0;
    const critSeg = Math.round(
      totalSeg * (r.data?.resource?.online?.conversionMinimumFraction || 1 / (totalSeg || 1))
    );
    this.segmentConfiguration.shield.push(
      { number: critSeg, selected: false, disabled: false,
        index: shieldItems.findIndex(_ => r === _), critical: true },
      ...new Array(Math.round(totalSeg - critSeg)).fill(void 0)
        .map(() => ({ number: 1, selected: false, disabled: false,
          index: shieldItems.findIndex(v => r === v), critical: false }))
    );
  });

  // Powered-off shields: all pips disabled
  poweredOff.forEach(r => {
    this.segmentConfiguration.shield.push(
      ...new Array(Math.round(r.data?.resource?.online?.consumption?.powerSegment || 0))
        .fill(void 0)
        .map(() => ({ number: 1, selected: false, disabled: true,
          index: shieldItems.findIndex(h => r === h), critical: false }))
    );
  });

  // Sort: critical first, then by pip size desc, then enabled before disabled, then by index
  this.segmentConfiguration.shield.sort((r, s) =>
    Number(s.critical) - Number(r.critical) ||
    s.number - r.number ||
    Number(r.disabled) - Number(s.disabled) ||
    (r.index || 0) - (s.index || 0)
  );

  // Shields only powered in SCM mode
  this.results.powerConfiguration.shield = {
    power: "scm" === this.results.shipMode,
    segment: 0
  };
}


// =============================================================================
// 6. DEFAULT PIP ALLOCATION (initSegmentsDistribution)
// =============================================================================
initSegmentsDistribution() {
  // Helper: add segments to a power category
  const addSegment = (category, segCount, coolerIndex) => {
    const config = this.results.powerConfiguration[category];
    if (!config) return;
    if (Array.isArray(config)) {
      // coolers: array of configs
      config.forEach((item, idx) => {
        if (idx === coolerIndex && this.getEmptySegments() >= segCount) {
          item.segment += segCount;
        }
      });
    } else {
      if (this.getEmptySegments() >= segCount) {
        config.segment += segCount;
      }
    }
  };

  // Helper: select first/critical pip for a category
  const selectFirst = (category, segments, criticalOnly = false, coolerIndex) => {
    if (criticalOnly && segments.length) {
      // Select ALL critical non-disabled pips (used for shields)
      segments.filter(_ => _.critical && !_.disabled).forEach(_ => {
        if (!_.disabled) {
          _.selected = true;
          addSegment(category, _.number, coolerIndex);
        }
      });
    } else if (segments.length) {
      // Select just the FIRST non-disabled pip
      const first = segments[0];
      if (!first.disabled) {
        first.selected = true;
        addSegment(category, first.number, coolerIndex);
      }
    }
  };

  // Helper: fill remaining pips greedily
  const fillRemaining = (category, segments, coolerIndex) => {
    while (
      this.getEmptySegments() &&
      segments.filter(h => !h.selected && !h.disabled).length &&
      segments.filter(h => !h.selected && !h.disabled)[0].number <= this.getEmptySegments()
    ) {
      const next = segments.filter(_ => !_.selected && !_.disabled)[0];
      next.selected = true;
      addSegment(category, next.number, coolerIndex);
    }
  };

  // PHASE 1: Select first/critical pip for each category
  Object.keys(this.segmentConfiguration).forEach(category => {
    if ("coolers" === category) {
      this.segmentConfiguration[category].forEach((segs, idx) =>
        selectFirst(category, segs, false, idx)
      );
    } else if ("shield" === category) {
      // Only in SCM mode, and select ALL critical pips
      if ("scm" === this.results.shipMode)
        selectFirst(category, this.segmentConfiguration[category], true);
    } else if ("qdrive" === category) {
      // Only in NAV mode
      if ("nav" === this.results.shipMode)
        selectFirst(category, this.segmentConfiguration[category]);
    } else if ("tractorBeam" === category) {
      return; // skip tractor beam in first pass
    } else {
      selectFirst(category, this.segmentConfiguration[category]);
    }
  });

  // PHASE 2: Fill remaining pips in priority order
  const scmOrder = [
    "coolers", "lifeSupport", "miningLaser", "weapon", "shield",
    "engine", "radar", "emp", "qed", "salvage"
  ];
  const navOrder = [
    "coolers", "lifeSupport", "qdrive", "miningLaser",
    "engine", "radar", "salvage", "weapon", "emp", "qed"
  ];

  const fillOrder = ("scm" === this.results.shipMode) ? scmOrder : navOrder;

  fillOrder.forEach(category => {
    Object.keys(this.segmentConfiguration).forEach(segKey => {
      if (segKey === category) {
        if ("coolers" === segKey) {
          this.segmentConfiguration[segKey].forEach((segs, idx) =>
            fillRemaining(segKey, segs, idx)
          );
        } else {
          fillRemaining(segKey, this.segmentConfiguration[segKey]);
        }
      }
    });
  });
}


// =============================================================================
// 7. GET EMPTY SEGMENTS (remaining available power)
// =============================================================================
getEmptySegments() {
  let used = 0;
  const e = this.results.powerConfiguration;

  if (e.weapon.power)      used += e.weapon.segment;
  if (e.engine.power)      used += e.engine.segment;
  if (e.shield.power)      used += e.shield.segment;
  if (e.qdrive.power)      used += e.qdrive.segment;
  if (e.lifeSupport.power) used += e.lifeSupport.segment;
  if (e.radar.power)       used += e.radar.segment;
  if (e.qed.power)         used += e.qed.segment;
  if (e.emp.power)         used += e.emp.segment;
  if (e.miningLaser.power) used += e.miningLaser.segment;
  if (e.salvage.power)     used += e.salvage.segment;
  if (e.tractorBeam.power) used += e.tractorBeam.segment;

  used += e.coolers
    .filter(a => a.power)
    .reduce((a, o) => a + o.segment, 0);

  return this.results.powerConfiguration.totalAvailablePowerSegments - used;
}


// =============================================================================
// 8. POWER PLANT USAGE RATIO
// =============================================================================
getPowerPlantUsageRatio() {
  const { weapons } = this.extractItems();
  let totalUsed = 0;

  // Sum all non-weapon powered segments
  const pc = this.results.powerConfiguration;
  pc.coolers.forEach(s => { if (s.power) totalUsed += s.segment; });
  if (pc.emp.power)         totalUsed += pc.emp.segment;
  if (pc.engine.power)      totalUsed += pc.engine.segment;
  if (pc.lifeSupport.power) totalUsed += pc.lifeSupport.segment;
  if (pc.miningLaser.power) totalUsed += pc.miningLaser.segment;
  if (pc.qdrive.power)      totalUsed += pc.qdrive.segment;
  if (pc.qed.power)         totalUsed += pc.qed.segment;
  if (pc.radar.power)       totalUsed += pc.radar.segment;
  if (pc.salvage.power)     totalUsed += pc.salvage.segment;
  if (pc.shield.power)      totalUsed += pc.shield.segment;
  if (pc.tractorBeam.power) totalUsed += pc.tractorBeam.segment;

  // For weapons: use min(selectedPips, actualPowerConsumption)
  const weaponSelectedPips = this.segmentConfiguration.weapon
    .filter(s => s.selected).reduce((s, u) => s + u.number, 0);
  const weaponActualConsumption = weapons
    .map(s => this.findItem(s)).filter(s => s?.poweredOn)
    .map(s => s?.item)
    .reduce((s, u) => s + (u.data?.resource?.online?.consumption?.power || 0), 0);

  totalUsed += (weaponSelectedPips > weaponActualConsumption)
    ? weaponActualConsumption : weaponSelectedPips;

  // FORMULA: usageRatio = totalUsedSegments / totalAvailableSegments
  return totalUsed / this.results.powerConfiguration.totalAvailablePowerSegments;
}


// =============================================================================
// 9. findRangeObject - lookup power range modifier
// =============================================================================
// `ranges` = [low, medium, high] objects with { start, modifier }
// `value` = the pip count to look up
// Returns the range object where value >= range.start && value < nextRange.start
findRangeObject(ranges, value) {
  return ranges.find((range, index, arr) => {
    let nextStart;
    if (index < arr.length - 1) {
      nextStart = arr[index + 1]?.start || 0;
    } else {
      nextStart = Infinity;  // last range extends to infinity
    }
    return value >= (range?.start || 0) && value < nextStart;
  });
}


// =============================================================================
// 10. EM SIGNATURE FORMULA
// =============================================================================
getEmSignature() {
  const { coolers, lifeSupports, powerPlants, qdrives, radars, shields, weapons } =
    this.extractItems();

  // --- Power Plants EM contribution ---
  // For each active PP: nominalSignature * rangeModifier, scaled by usageRatio
  const ppUsageRatio = this.getPowerPlantUsageRatio();
  const ppEmContribution = powerPlants
    .map(H => this.findItem(H))
    .filter(H => H?.poweredOn)
    .reduce((total, pp) => {
      let modifier = 1;
      // Segments per PP = round(totalAvailable * usageRatio) / numPPs
      const segsPerPP = Math.round(
        this.results.powerConfiguration.totalAvailablePowerSegments * ppUsageRatio
      ) / powerPlants.length;

      const ranges = pp?.item?.data?.resource?.online?.powerRanges;
      if (ranges) {
        const { low, medium, high } = ranges;
        modifier = this.findRangeObject([low, medium, high], segsPerPP)?.modifier || 1;
      }
      return total +
        (pp?.item.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0) * modifier;
    }, 0) * ppUsageRatio;  // multiply entire PP sum by usage ratio

  // --- Weapons EM contribution ---
  // Simple sum of nominalSignature for all powered-on weapons (no range modifier!)
  let weaponEm = 0;
  if (weapons.length && this.results.powerConfiguration.weapon.power) {
    weaponEm = weapons
      .map(B => this.findItem(B))
      .filter(B => B?.poweredOn)
      .reduce((sum, w) =>
        sum + (w?.item?.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0), 0);
  }

  // --- Life Support EM contribution ---
  // nominalSignature * rangeModifier * (allocatedSegment / totalSegments)
  let lifeSupportEm = 0;
  if (lifeSupports.length && this.results.powerConfiguration.lifeSupport.power) {
    const allocatedSeg = this.results.powerConfiguration.lifeSupport.segment;
    lifeSupportEm = lifeSupports.reduce((sum, ls) => {
      let modifier = 1;
      if (ls.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = ls.data.resource.online.powerRanges;
        modifier = this.findRangeObject([low, medium, high],
          allocatedSeg / lifeSupports.length)?.modifier || 1;
      }
      return sum +
        (ls.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0) * modifier;
    }, 0) * (allocatedSeg / this.segmentConfiguration.lifeSupport
      .reduce((s, v) => s + v.number, 0) || 1);
  }

  // --- QDrive EM contribution ---
  // Same pattern as lifeSupport
  let qdriveEm = 0;
  if (qdrives.length && this.results.powerConfiguration.qdrive.power) {
    const allocatedSeg = this.results.powerConfiguration.qdrive.segment;
    qdriveEm = qdrives.reduce((sum, qd) => {
      let modifier = 1;
      if (qd.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = qd.data.resource.online.powerRanges;
        // NOTE: bug in erkul? uses lifeSupports.length for qdrive range lookup
        modifier = this.findRangeObject([low, medium, high],
          allocatedSeg / lifeSupports.length)?.modifier || 1;
      }
      return sum +
        (qd.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0) * modifier;
    }, 0) * (allocatedSeg / this.segmentConfiguration.qdrive
      .reduce((s, v) => s + v.number, 0) || 1);
  }

  // --- Radar EM contribution ---
  let radarEm = 0;
  if (radars.length && this.results.powerConfiguration.radar.power) {
    const allocatedSeg = this.results.powerConfiguration.radar.segment;
    radarEm = radars.reduce((sum, rd) => {
      let modifier = 1;
      if (rd.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = rd.data.resource.online.powerRanges;
        modifier = this.findRangeObject([low, medium, high],
          allocatedSeg / radars.length)?.modifier || 1;
      }
      return sum +
        (rd.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0) * modifier;
    }, 0) * (allocatedSeg / this.segmentConfiguration.radar
      .reduce((s, v) => s + v.number, 0) || 1);
  }

  // --- Shield EM contribution ---
  let shieldEm = 0;
  if (shields.length && this.results.powerConfiguration.shield.power) {
    // Total selected shield segments (across all shields)
    const totalSelectedShieldSegs = this.segmentConfiguration.shield
      .filter(B => B.selected && !B.disabled)
      .reduce((sum, pip) => sum + pip.number, 0);

    shieldEm = shields.reduce((sum, shield, shieldIdx) => {
      // Segments selected for THIS specific shield
      const thisShieldSelected = this.segmentConfiguration.shield
        .filter(me => me.index === shieldIdx && me.selected && !me.disabled)
        .reduce((s, pip) => s + pip.number, 0);

      // Ratio of selected/total for this shield
      const thisShieldRatio = thisShieldSelected /
        this.segmentConfiguration.shield
          .filter(me => me.index === shieldIdx && !me.disabled)
          .reduce((s, pip) => s + pip.number, 0);

      let modifier = 1;
      if (shield.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = shield.data.resource.online.powerRanges;
        // Range lookup uses TOTAL selected shield segments (not per-shield)
        modifier = this.findRangeObject([low, medium, high], totalSelectedShieldSegs)?.modifier || 1;
      }

      // Only contribute if at least 1 segment selected
      return sum + (thisShieldSelected >= 1
        ? (shield.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0)
          * thisShieldRatio * modifier
        : 0);
    }, 0);
  }

  // --- Cooler EM contribution ---
  let coolerEm = 0;
  coolers.forEach((cooler, coolerIdx) => {
    if (this.results.powerConfiguration.coolers[coolerIdx].power) {
      const nomSig = cooler.data?.resource?.online?.signatureParams?.em?.nominalSignature || 0;
      const allocatedSeg = this.results.powerConfiguration.coolers[coolerIdx].segment;
      const segRatio = allocatedSeg /
        this.segmentConfiguration.coolers[coolerIdx]?.reduce((s, pip) => s + pip.number, 0);

      let modifier = 1;
      if (cooler.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = cooler.data.resource.online.powerRanges;
        modifier = this.findRangeObject([low, medium, high], allocatedSeg)?.modifier || 1;
      }
      coolerEm += nomSig * (segRatio || 1) * modifier;
    }
  });

  // FINAL EM FORMULA:
  this.results.emSignature =
    (ppEmContribution + weaponEm + lifeSupportEm + qdriveEm + radarEm + shieldEm + coolerEm)
    * (this.ship?.data?.armor?.data?.armor?.signalElectromagnetic || 1);
}


// =============================================================================
// 11. IR SIGNATURE FORMULA
// =============================================================================
getIrSignature() {
  this.results.irSignature = 0;
  const { coolers } = this.extractItems();

  // Cooling ratio: capped at 1.0
  let coolingRatio = this.results.powerConfiguration.coolingConsumption
    / this.results.powerConfiguration.coolingGeneration;
  if (coolingRatio > 1) coolingRatio = 1;

  coolers.forEach((cooler, coolerIdx) => {
    if (this.results.powerConfiguration.coolers[coolerIdx].power) {
      const allocatedSeg = this.results.powerConfiguration.coolers[coolerIdx].segment;

      // Segment ratio = selected segments / total segments for this cooler
      const segRatio = this.segmentConfiguration.coolers[coolerIdx]
        ?.filter(_ => _.selected).reduce((s, pip) => s + pip.number, 0)
        / this.segmentConfiguration.coolers[coolerIdx]
          ?.reduce((s, pip) => s + pip.number, 0) || 0;

      let modifier = 1;
      if (cooler.data?.resource?.online?.powerRanges) {
        const { low, medium, high } = cooler.data.resource.online.powerRanges;
        modifier = this.findRangeObject([low, medium, high], allocatedSeg)?.modifier || 1;
      }

      // FORMULA per cooler:
      //   nominalSignature_IR * segmentRatio * coolingRatio * rangeModifier * armorMultiplier
      this.results.irSignature +=
        (cooler.data?.resource?.online?.signatureParams?.ir?.nominalSignature || 0)
        * segRatio
        * coolingRatio
        * modifier
        * (this.ship?.data?.armor?.data?.armor?.signalInfrared || 1);
    }
  });
}


// =============================================================================
// 12. CS (CROSS-SECTION) SIGNATURE FORMULA
// =============================================================================
getCsSignature() {
  if (this.ship?.data?.crossSection) {
    // FORMULA: max of all crossSection values * armor CS multiplier
    this.results.csSignature =
      Math.max(...Object.values(this.ship.data.crossSection))
      * (this.ship.data.armor?.data?.armor?.signalCrossSection || 1);
  }
}


// =============================================================================
// 13. WEAPON POWER RATIO
// =============================================================================
getWeaponPowerRatio() {
  const poolSize = this.ship?.data?.rnPowerPools?.weaponGun?.poolSize || 0;
  let totalWeaponConsumption = 0;

  const { weapons } = this.extractItems();
  weapons.map(s => this.findItem(s)).forEach(s => {
    if (s?.poweredOn) {
      totalWeaponConsumption += s.item.data?.resource?.online?.consumption?.power || 0;
    }
  });

  // FORMULA: ratio = poolSize * powerRatioMultiplier / totalConsumption, capped at 1.0
  const ratio = poolSize
    * (this.ship?.data?.buff?.regenModifier?.powerRatioMultiplier || 1)
    / totalWeaponConsumption;

  this.results.weaponPowerRatio = ratio < 1 ? ratio : 1;
}


// =============================================================================
// 14. WEAPON POWER USAGE (percentage)
// =============================================================================
getWeaponPowerUsage() {
  if (!this.results.powerConfiguration.weapon.power) {
    this.results.powerConfiguration.weapon.usage = 0;
    return;
  }

  let totalConsumption = 0;
  const { weapons } = this.extractItems();
  weapons.map(s => this.findItem(s)).forEach(s => {
    if (s?.poweredOn) {
      totalConsumption +=
        (s.item.data?.resource?.online?.consumption?.power || 0)
        / (this.ship?.data?.buff?.regenModifier?.powerRatioMultiplier || 1);
    }
  });

  // FORMULA: if consumption <= segment, usage = 100%; else usage = 100 / overageRatio
  const overageRatio = totalConsumption / this.results.powerConfiguration.weapon.segment;
  this.results.powerConfiguration.weapon.usage =
    overageRatio <= 1 ? 100 : (100 / overageRatio) || 0;
}


// =============================================================================
// 15. COOLING GENERATION
// =============================================================================
getCoolingGeneration() {
  const { coolers } = this.extractItems();
  let maxCoolingGen = 0;  // total possible cooling (all coolers at full power)
  let actualCoolingGen = 0;  // actual cooling based on allocated segments

  coolers.forEach((cooler, idx) => {
    if ("Empty" !== cooler.data?.name) {
      const coolerMaxGen = cooler.data?.resource?.online?.generation?.cooling || 0;
      maxCoolingGen += coolerMaxGen;

      if (this.results.powerConfiguration.coolers[idx].power) {
        const allocatedSeg = this.results.powerConfiguration.coolers[idx].segment;

        let modifier = 1;
        if (cooler.data?.resource?.online?.powerRanges) {
          const { low, medium, high } = cooler.data.resource.online.powerRanges;
          modifier = this.findRangeObject([low, medium, high], allocatedSeg)?.modifier || 1;
        }

        // FORMULA: coolingGen = (allocatedSegs / totalSegs) * maxCooling * rangeModifier
        const segRatioGen = allocatedSeg
          / (cooler.data?.resource?.online?.consumption?.powerSegment || 0)
          * coolerMaxGen;

        this.results.powerConfiguration.coolers[idx].coolingGeneration = segRatioGen * modifier;
        actualCoolingGen += segRatioGen * modifier;
      } else {
        this.results.powerConfiguration.coolers[idx].coolingGeneration = 0;
      }
    } else {
      this.results.powerConfiguration.coolers[idx].coolingGeneration = 0;
    }
  });

  this.results.powerConfiguration.coolingGeneration = actualCoolingGen;
  this.results.powerConfiguration.maxCoolingGeneration = maxCoolingGen;
}


// =============================================================================
// 16. COOLING CONSUMPTION
// =============================================================================
getCoolingConsumption() {
  this.results.powerConfiguration.coolingConsumption = 0;
  this.results.coolingConsumptions = {
    powerPlants: 0, shields: [], lifeSupports: [], radars: [], qdrives: []
  };

  const { lifeSupports, qdrives, radars, shields, weapons } = this.extractItems();

  // --- Base cooling consumption = all allocated power segments ---
  const weaponSelectedPips = this.segmentConfiguration.weapon
    .filter(v => v.selected).reduce((sum, pip) => sum + pip.number, 0);
  const weaponActualConsumption = weapons
    .map(v => this.findItem(v)).filter(v => v?.poweredOn)
    .map(v => v?.item)
    .reduce((sum, w) => sum + (w.data?.resource?.online?.consumption?.power || 0), 0);

  // Weapon contribution = min(selectedPips, actualConsumption)
  let weaponContrib = weaponSelectedPips > weaponActualConsumption
    ? weaponActualConsumption : weaponSelectedPips;

  // Base = sum of ALL allocated segments (coolers + engine + all categories + weapons)
  const baseCooling =
    this.results.powerConfiguration.coolers.reduce((sum, c) => sum + c.segment, 0)
    + this.results.powerConfiguration.engine.segment
    + this.results.powerConfiguration.lifeSupport.segment
    + this.results.powerConfiguration.miningLaser.segment
    + this.results.powerConfiguration.qdrive.segment
    + this.results.powerConfiguration.qed.segment
    + this.results.powerConfiguration.emp.segment
    + this.results.powerConfiguration.radar.segment
    + this.results.powerConfiguration.salvage.segment
    + this.results.powerConfiguration.shield.segment
    + this.results.powerConfiguration.tractorBeam.segment
    + weaponContrib;

  this.results.powerConfiguration.coolingConsumption += baseCooling;
  this.results.coolingConsumptions.powerPlants = baseCooling;

  // --- Shield cooling consumption ---
  // For each shield: selectedSegments * rangeModifier
  shields.forEach((shield, shieldIdx) => {
    const selectedSegs = this.segmentConfiguration.shield
      .filter(A => A.index === shieldIdx && A.selected)
      .reduce((sum, pip) => sum + pip.number, 0);

    let modifier = 1;
    if (shield.data?.resource?.online?.powerRanges) {
      const { low, medium, high } = shield.data.resource.online.powerRanges;
      modifier = this.findRangeObject([low, medium, high], selectedSegs)?.modifier || 1;
    }

    const shieldCooling = selectedSegs * modifier;
    this.results.powerConfiguration.coolingConsumption += shieldCooling;
    this.results.coolingConsumptions.shields.push(shieldCooling);
  });

  // --- Life Support cooling consumption ---
  lifeSupports.forEach(ls => {
    const selectedSegs = this.segmentConfiguration.lifeSupport
      .filter(k => k.selected).reduce((sum, pip) => sum + pip.number, 0);

    let modifier = 1;
    if (ls.data?.resource?.online?.powerRanges) {
      const { low, medium, high } = ls.data.resource.online.powerRanges;
      modifier = this.findRangeObject([low, medium, high], selectedSegs)?.modifier || 1;
    }

    const lsCooling = selectedSegs * modifier;
    this.results.powerConfiguration.coolingConsumption += lsCooling;
    this.results.coolingConsumptions.lifeSupports.push(lsCooling);
  });

  // --- Radar cooling consumption ---
  radars.forEach(rd => {
    const selectedSegs = this.segmentConfiguration.radar
      .filter(k => k.selected).reduce((sum, pip) => sum + pip.number, 0);

    let modifier = 1;
    if (rd.data?.resource?.online?.powerRanges) {
      const { low, medium, high } = rd.data.resource.online.powerRanges;
      modifier = this.findRangeObject([low, medium, high], selectedSegs)?.modifier || 1;
    }

    const rdCooling = selectedSegs * modifier;
    this.results.powerConfiguration.coolingConsumption += rdCooling;
    this.results.coolingConsumptions.radars.push(rdCooling);
  });

  // --- QDrive cooling consumption ---
  qdrives.forEach(qd => {
    const selectedSegs = this.segmentConfiguration.qdrive
      .filter(k => k.selected).reduce((sum, pip) => sum + pip.number, 0);

    let modifier = 1;
    if (qd.data?.resource?.online?.powerRanges) {
      const { low, medium, high } = qd.data.resource.online.powerRanges;
      modifier = this.findRangeObject([low, medium, high], selectedSegs)?.modifier || 1;
    }

    const qdCooling = selectedSegs * modifier;
    this.results.powerConfiguration.coolingConsumption += qdCooling;
    this.results.coolingConsumptions.qdrives.push(qdCooling);
  });
}
