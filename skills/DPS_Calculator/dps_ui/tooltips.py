# -*- coding: utf-8 -*-
"""tooltips.py -- plain-English explanations for every labelled figure in the
DPS Calculator.  Pure data: no Qt, no imports from dps_ui or services.

WHY THIS FILE EXISTS
    GitHub issue #7 (MesopotamiaAlpha): "It would be interesting to add pop-up
    windows when hovering the mouse over an icon or value to indicate what it
    is" / "These columns lack descriptions to understand what they refer to."
    Every column in this calculator is a 3-6 character abbreviation with no
    unit attached, and several of the totals carry caveats that are invisible
    on screen.  This is the copy for those.

HOW IT IS KEYED -- read this before wiring anything
    Keys are the STABLE FIELD KEY, never the visible label.  Visible labels go
    through ``shared.i18n.s_``, so "Effic" is a translation lookup and would
    stop matching the moment anyone ships a localisation -- silently, because a
    missing tooltip just does not appear.  The field key is the second element
    of each ``*_TABLE_COLS`` 5-tuple in ``dps_ui/constants.py`` and is what the
    renderer already has in hand.

    Field keys are NOT unique across tables, so every key is namespaced:

        tip("weapon.efficiency")     -> the Effic column in the weapon picker
        tip("shield.hp")             -> the shield's HP pool
        tip("cooler.hp")             -> falls back to table.hp, the cooler's
                                        own component health

    ``shield.hp`` and ``table.hp`` mean genuinely different things (a shield
    pool versus a component's durability), and the weapon table calls its
    component health ``wp_hp`` for exactly that reason.  Do not collapse them.

FALLBACK RULE
    ``tip()`` tries the namespaced key first, then ``table.<field>`` for the
    handful of columns whose meaning is identical in every picker (name, class,
    grade, hp, power_draw, em_max, plus the Sz and cart pseudo-columns).  A
    table that needs different words for one of those overrides it in its own
    dict -- see SHIELD_TIPS["hp"] and COOLER_TIPS["ir_max"].

WHAT IS DELIBERATELY ABSENT
    Columns whose meaning could not be established from the code are left OUT
    rather than guessed at.  A confidently wrong tooltip is worse than none,
    because it gets believed.  The omissions are listed in OMITTED below so the
    next person does not have to re-derive that they were considered.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Columns shared by every picker table.  Reached through tip()'s fallback.
# ---------------------------------------------------------------------------

TABLE_TIPS = {
    "size":
        "The item's size class. This picker only lists items that fit, so "
        "everything here is at or below the hardpoint size shown in the "
        "coloured badge on the section header.",
    "name":
        "The item's in-game name. Click the row to fit it to this hardpoint.",
    "cart":
        "Opens the market lookup for this item -- where it is sold and for how "
        "much. It does not fit the item to the ship.",
    "class":
        "The class label the data source carries for this item. It is a "
        "category, not a performance rating.",
    "grade":
        "The grade label the data source carries for this item. It is a "
        "category, not a number you can compare across sizes.",
    "hp":
        "The component's own health, in hit points. Components are "
        "destructible: this is how much punishment the item itself takes "
        "before it is knocked out. It is not the ship's hull or shield HP.",
    "power_draw":
        "Power this item draws, in the same power units as a power plant's "
        "output and the power allocator's draw figure.",
    "em_max":
        "Electromagnetic signature this item adds -- how much more visible it "
        "makes you to EM scanning.",
}

# ---------------------------------------------------------------------------
# WEAPON_TABLE_COLS -- the weapon picker
# ---------------------------------------------------------------------------

WEAPON_TIPS = {
    "group":
        "Weapon class, abbreviated to two letters: LR laser repeater, LC laser "
        "cannon, LG laser gatling, LS laser scattergun, LB laser beam, BR/BC/BG/BS "
        "the ballistic equivalents, DC/DR/DS distortion, PC plasma cannon, TC "
        "tachyon, NC neutron, RP rocket pod. Anything without a short code shows "
        "the first three letters of its group name.",
    "dps_sus":
        "Sustained damage per second -- what the gun actually puts out over a "
        "long engagement, once heat build-up or charge regeneration limits it. "
        "This is the figure to compare loadouts on. With the power allocator "
        "active it is recomputed for your current weapon power allocation. An "
        "em-dash means this gun has no published sustained value.",
    "dps_raw":
        "Burst damage per second: damage per shot multiplied by shots per "
        "second, with no heat or ammo limit applied. A gun only holds this for "
        "the opening seconds of a fight. Always equal to or higher than the "
        "sustained figure beside it.",
    "efficiency":
        "Burst DPS divided by the gun's power draw, then divided by 100 to keep "
        "the column narrow. It is a comparison ratio rather than a game stat -- "
        "higher means more damage per point of power. Guns that draw no power "
        "show an em-dash.",
    "alpha":
        "Damage per SHOT, not per second: every damage type added together, "
        "including any explosion the round carries. For a scattergun this is "
        "the whole pellet spread landing at once; for a charge weapon it is a "
        "fully charged shot.",
    "rps":
        "Shots per second. The Selected Weapon panel shows the same thing as "
        "rounds per minute, which is this figure times 60.",
    "speed":
        "Projectile speed in metres per second. Slower rounds need more lead "
        "on a moving target.",
    "range":
        "Metres the projectile travels before it expires -- speed multiplied by "
        "lifetime. It is a hard cut-off, not a damage-falloff range: past it "
        "the shot simply ceases to exist.",
    "spread":
        "Maximum spread in degrees. 0.00 is a perfectly accurate weapon; "
        "higher values scatter more at range.",
    "power":
        "Power the gun draws when it fires. Under-powering weapons in the "
        "power allocator slows their charge regeneration, which lowers the "
        "sustained DPS above.",
    "ammo":
        "Shots the gun carries. For a ballistic weapon this is physical rounds "
        "that run out until you rearm; for an energy weapon it is the size of a "
        "charge pool that refills on its own. The two are not interchangeable, "
        "even though they share this column.",
    "pen":
        "Penetration in metres -- how far the round punches through material "
        "before stopping.",
    "wp_hp":
        "The gun's own health. A hardpoint can be shot off your ship, and this "
        "is how much damage the weapon absorbs first. Not hull or shield HP.",
}

# ---------------------------------------------------------------------------
# MISSILE_TABLE_COLS
# ---------------------------------------------------------------------------

MISSILE_TIPS = {
    "tracking":
        "The signal the missile homes on, abbreviated from its tracking type: "
        "IR infrared (heat), EM electromagnetic, CR cross-section. It has to "
        "match something the target is actually radiating.",
    "total_dmg":
        "Total damage of ONE missile, every damage type added together. It is "
        "damage per hit, not per second -- missiles have no DPS.",
    "speed":
        "Missile flight speed in metres per second.",
    "lock_range":
        "Maximum lock distance, shown in thousands of metres. Past it the "
        "missile cannot acquire the target at all.",
    "lock_time":
        "Seconds you have to hold a lock before the missile can be fired.",
}

# ---------------------------------------------------------------------------
# SHIELD_TABLE_COLS
# ---------------------------------------------------------------------------

SHIELD_TIPS = {
    # Override: in every other table "hp" is the item's own durability.
    "hp":
        "The shield pool, in hit points. Damage lands here before it reaches "
        "the hull. This is the shield's own capacity, not the generator's "
        "durability.",
    "regen":
        "Shield hit points restored per second at FULL power. The power "
        "allocator scales this down when the shield bank runs on fewer pips; "
        "the Regen/s row in the Shields panel shows the scaled figure.",
    "res_phys_max":
        "Physical damage this shield removes at FULL power allocation -- the "
        "top of the item's minimum-to-maximum range. A positive figure is "
        "damage taken away; 0% means the shield does nothing against physical. "
        "The Shields panel on the right shows the value for your current pip "
        "allocation instead, averaged across every shield fitted.",
    "res_energy_max":
        "Energy damage this shield removes at FULL power allocation. Many "
        "shields resist no energy damage at all and read 0% here, which is a "
        "real value rather than missing data.",
    "res_dist_max":
        "Distortion damage this shield removes at FULL power allocation. Same "
        "min-to-max range as the other two resistance columns, so the Shields "
        "panel will show a lower figure whenever the bank is on partial pips.",
}

# ---------------------------------------------------------------------------
# COOLER_TABLE_COLS
# ---------------------------------------------------------------------------

COOLER_TIPS = {
    "cooling_rate":
        "Cooling this unit provides. Cooling is what lets heat-limited weapons "
        "keep firing before they overheat, so it feeds the sustained DPS total "
        "in the footer.",
    "ir_max":
        "Infrared (heat) signature the cooler emits. Coolers are the ONLY "
        "source of the ship's IR figure in this calculator -- nothing else "
        "contributes to it -- and turning cooler pips down in the power "
        "allocator is what lowers it.",
}

# ---------------------------------------------------------------------------
# RADAR_TABLE_COLS
# ---------------------------------------------------------------------------

RADAR_TIPS = {
    # detection_min / detection_max are deliberately absent -- see OMITTED.
}

# ---------------------------------------------------------------------------
# PP_COLS -- power plants
# ---------------------------------------------------------------------------

POWERPLANT_TIPS = {
    "output":
        "Power this plant generates. The power allocator's output readout is "
        "this figure totalled over every plant fitted, and everything else on "
        "the ship draws against it.",
    "ir_max":
        "Infrared signature the plant emits.",
    "em_max":
        "Electromagnetic signature the plant emits. It is scaled by how much "
        "of the plant's output you are actually drawing, so a lightly loaded "
        "plant is quieter than a saturated one.",
}

# ---------------------------------------------------------------------------
# QD_COLS -- quantum drives
# ---------------------------------------------------------------------------

QDRIVE_TIPS = {
    "speed":
        "Quantum travel speed, shown in kilometres per second.",
    "jump_range":
        "The longest single jump the drive can make, in gigametres. An "
        "infinity symbol means the data source reports no limit for it.",
    "spool":
        "Seconds of spool-up before a jump begins.",
    "cooldown":
        "Seconds after a jump before the drive can spool again.",
    "fuel_rate":
        "Quantum fuel consumed per megametre travelled. Lower goes further on "
        "the same tank.",
    # power_draw is deliberately absent -- see OMITTED.
}

# ---------------------------------------------------------------------------
# MOUNT_TABLE_COLS -- gimbals and utility mounts
# ---------------------------------------------------------------------------

MOUNT_TIPS = {
    "port_max_size":
        "The largest weapon this mount will hold. A gimbal mount occupies the "
        "hardpoint's own size and carries a gun one size smaller -- that size "
        "drop is what you pay for tracking.",
}

# ---------------------------------------------------------------------------
# MISSILE_RACK_TABLE_COLS
# ---------------------------------------------------------------------------

MISSILE_RACK_TIPS = {
    "missile_count":
        "How many missiles this rack holds.",
    "missile_size":
        "Size of each missile the rack takes. The name usually encodes both: an "
        "MSD-322 is a size-3 rack carrying two size-2 missiles.",
}

# ---------------------------------------------------------------------------
# EMP_TABLE_COLS
# ---------------------------------------------------------------------------

EMP_TIPS = {
    "charge_time":
        "Seconds spent charging before the burst can be released.",
    "cooldown_time":
        "Seconds after a burst before it can be charged again.",
    "emp_radius":
        "Radius of the burst, in metres.",
    "distortion_dmg":
        "Distortion damage the burst applies. It is the only damage figure an "
        "EMP has -- the data carries no physical or energy value for it.",
}

# ---------------------------------------------------------------------------
# QED_TABLE_COLS -- quantum enforcement / interdiction
# ---------------------------------------------------------------------------

QED_TIPS = {
    "power_draw":
        "Power the interdiction generator draws while it is running.",
}

# ---------------------------------------------------------------------------
# BOMB_TABLE_COLS
# ---------------------------------------------------------------------------

BOMB_TIPS = {
    "arm_time":
        "Seconds after release before the bomb will detonate.",
    "max_lifetime":
        "Seconds the bomb exists before it expires unexploded.",
    "min_radius":
        "Inner blast radius in metres. Bomb DAMAGE is not present in the data "
        "source, so this panel can only show you blast geometry -- there is no "
        "damage figure here to compare bombs on.",
    "max_radius":
        "Outer blast radius in metres. As with the inner radius, no damage "
        "figure exists in the data for bombs.",
}

# ---------------------------------------------------------------------------
# MINING_LASER_TABLE_COLS
# ---------------------------------------------------------------------------

MINING_LASER_TIPS = {
    "module_slots":
        "Number of sub-slots on this laser for mining modules (the consumable "
        "gadgets that change how a rock breaks).",
    # instability / resistance_mod / filter_mod / throttle_min are absent -- see OMITTED.
}

# ---------------------------------------------------------------------------
# TOOL_ARM_TABLE_COLS
# ---------------------------------------------------------------------------

TOOL_ARM_TIPS = {
    "name":
        "The mining or salvage arm housing itself -- the structure the head "
        "mounts into. On most ships it is fixed and cannot be swapped.",
}

# ---------------------------------------------------------------------------
# SALVAGE_HEAD_TABLE_COLS
# ---------------------------------------------------------------------------

SALVAGE_HEAD_TIPS = {
    "max_force":
        "Tractor force the head applies, in kilonewtons.",
    "max_distance":
        "Maximum working distance in metres. Beyond it the beam does nothing.",
}

# ---------------------------------------------------------------------------
# ORE_POD_TABLE_COLS / FUEL_TANK_TABLE_COLS
# ---------------------------------------------------------------------------

ORE_POD_TIPS = {
    "capacity":
        "Capacity in SCU (standard cargo units), as the column header labels "
        "it. Both figures come from the item's resource-container capacity.",
}

FUEL_TANK_TIPS = {
    "capacity":
        "Capacity of the external tank, from the item's resource-container "
        "capacity. The data source attaches no unit to this field, so treat it "
        "as a figure for comparing tanks against each other.",
}

# ---------------------------------------------------------------------------
# ERKUL_MODULE_TABLE_COLS
# ---------------------------------------------------------------------------

MODULE_TIPS = {
    "sub_type":
        "The module's sub-type from the data source -- which slot family it "
        "belongs to, not a performance figure.",
}

# ---------------------------------------------------------------------------
# The footer strip (app.py _build_footer)
# ---------------------------------------------------------------------------

FOOTER_TIPS = {
    "dps_raw":
        "Burst: burst DPS of every gun fitted, pilot and turret together -- "
        "damage per shot times rate of fire, with no heat or ammo limit. It "
        "describes the opening seconds of a fight, not the fight.",
    "dps_sus":
        "DPS: sustained DPS of every gun fitted, the figure to compare "
        "loadouts on. Guns whose sustained value is unpublished are LEFT OUT "
        "rather than counted as zero, so on a loadout containing one this total "
        "is a FLOOR -- the real output is at least this much. Counting them as "
        "zero once reported a ship with an 18,000 DPS stock beam as zero "
        "sustained.",
    "alpha":
        "Alpha T+P: the damage of one full volley from every gun fitted, "
        "Turret and Pilot together. Damage per shot, not per second.",
    "shld_hp":
        "Shield: total shield hit points from every generator fitted. Shields "
        "show their full pool here even when unpowered -- it is the regen and "
        "resistance rows in the Shields panel that move with your power "
        "allocation.",
    "hull_hp":
        "Hull: the ship's hull hit points, taken from its armour health and "
        "falling back to total hull HP when the ship carries no armour entry. "
        "A property of the ship itself, not of anything you fit.",
    "cooling":
        "Cooling: total cooling from every cooler fitted. Cooling is what lets "
        "heat-limited weapons keep firing, so it feeds the sustained DPS figure "
        "to its left.",
}

# ---------------------------------------------------------------------------
# The power allocator (power_widget.py)
# ---------------------------------------------------------------------------

POWER_TIPS = {
    "em_sig":
        "EM signature -- how loud you are to electromagnetic scanning. Built "
        "from your power plants (scaled by how much of their output you are "
        "drawing) plus powered weapons, shields and coolers. An em-dash means a "
        "fitted power component has no data, so the figure would be "
        "understated rather than slightly off.",
    "ir_sig":
        "IR signature -- your heat. It comes from COOLERS ONLY, scaled by their "
        "pip allocation and by how hard they are working. Turning cooler pips "
        "down is the way to lower it.",
    "cs_sig":
        "CS signature -- cross-section, your physical size to radar. It comes "
        "from the ship's hull and its armour, so nothing you fit in this "
        "calculator changes it.",
    "pp_online":
        "Power plants online, shown over your total power capacity. The two "
        "numbers are different units sharing one readout: the left is a COUNT "
        "of plants, the right is capacity in power segments.",
    "total_capacity":
        "Total power segments (pips) your power plants provide. Everything you "
        "switch on draws against this. If no power plant resolves for the ship "
        "this reads 0 and the percentage beside it also reads 0% -- that is a "
        "missing denominator, not an idle ship.",
    "total_draw":
        "Power segments currently drawn, over the total available. The draw "
        "side counts only the categories that are switched on -- a component "
        "toggled off contributes nothing.",
    "consumption_pct":
        "Draw as a percentage of capacity: green below 80%, amber from 80%, red "
        "above 100%. This tool lets you go ABOVE 100% and shows you the "
        "overdraw, where erkul.games instead prevents you from allocating past "
        "100%. That is a deliberate difference, not a bug -- knowing by how "
        "much a plan overdraws is more useful than being unable to express the "
        "plan.",
    "SCM":
        "Space Combat Manoeuvring mode: the allocation you fly with in a fight. "
        "Shields are powered and the quantum drive is not.",
    "NAV":
        "Navigation mode: the allocation you travel with. The quantum drive is "
        "powered and shields are UNPOWERED, which is why shield regen reads 0.0 "
        "in NAV. That zero is a measurement, not missing data.",
    "current_seg":
        "Pips you have allocated to this component. Click a pip to set the "
        "level; the bar fills from the bottom up.",
    "default_seg":
        "The ship's stock allocation for this component. Green pips are at or "
        "below it; amber pips are above it, meaning you have pushed this "
        "component past its default.",
    "enabled":
        "Right-click a bar to switch that component off entirely, or click the "
        "icon at the foot of the column to switch the whole category. A "
        "component that is off draws no power and adds no signature.",
}

# ---------------------------------------------------------------------------
# Power allocator category columns (keys from power_engine.CATEGORY_ORDER)
# ---------------------------------------------------------------------------

POWER_CATEGORY_TIPS = {
    "weaponGun":
        "WPN -- weapon power. Pips here set your weapon power ratio, which "
        "scales sustained DPS: an under-powered gun regenerates its charge more "
        "slowly and so sustains less.",
    "thruster":
        "THR -- thruster power. The calculator budgets what thrusters draw "
        "against your total capacity but models nothing else about them, so "
        "what this column changes on screen is the power budget alone.",
    "shield":
        "SHD -- shield power. Pips here scale shield regen AND slide resistance "
        "between each shield's minimum and maximum, so both the Regen/s row and "
        "the three resist rows in the Shields panel move with this column.",
    "radar":
        "RDR -- radar power. Switching it off frees its segments for other "
        "categories. The calculator does not model detection range, so what you "
        "would lose by doing that is not shown anywhere here.",
    "lifeSupport":
        "LSP -- life support power. Its draw is counted against your capacity "
        "and nothing else about it is modelled, so it appears here only as a "
        "claim on the power budget.",
    "cooler":
        "CLR -- cooler power. This is the only column that changes your IR "
        "signature, and it also gates how much of your cooling you actually "
        "get.",
    "quantumDrive":
        "QDR -- quantum drive power. Unpowered in SCM and powered in NAV, which "
        "is why switching mode moves the whole board.",
    "utility":
        "UTL -- utility items grouped into one column (tractor beams, mining "
        "and salvage gear, interdiction and EMP hardware).",
}

# ---------------------------------------------------------------------------
# The right-hand ship / loadout panel (app.py _ov_vars keys)
# ---------------------------------------------------------------------------

SHIP_TIPS = {
    "pilot_dps_raw":
        "Burst DPS of the guns YOU fire from the pilot seat. Turret guns are "
        "counted in the block below, so a turret ship's real output is the two "
        "added together.",
    "pilot_dps_sus":
        "Sustained DPS of the guns you fire from the pilot seat. This block "
        "uses each gun's unmodified figure at full weapon power, to match "
        "erkul's display -- the footer DPS total is the one that reflects your "
        "power allocation, so the two can legitimately disagree.",
    "pilot_alpha":
        "Damage of one volley from the pilot's guns. Per shot, not per second.",
    "turret_dps_raw":
        "Burst DPS of guns in turret hardpoints. Listed apart from the pilot "
        "guns because reaching them needs a gunner or a seat switch.",
    "turret_dps_sus":
        "Sustained DPS of guns in turret hardpoints, at full weapon power "
        "(same erkul-matching basis as the pilot figure above).",
    "turret_alpha":
        "Damage of one volley from the turret guns. Per shot, not per second.",
    "missile_dmg":
        "Total damage of every missile currently loaded, fired once. Not per "
        "second and not repeatable -- once they are gone they are gone.",
    "gun_slots":
        "How many weapon hardpoints have something fitted. Grouped turrets "
        "count every barrel, so a turret carrying four guns adds four.",
    "miss_slots":
        "How many missile racks have something fitted.",
    "wpn_name":
        "The weapon currently selected in a picker below. This whole block "
        "describes that one gun, not the ship.",
    "wpn_size":
        "The selected weapon's size class.",
    "wpn_dps_burst":
        "Burst DPS of the selected weapon: damage per shot times rate of fire, "
        "with no heat or ammo limit.",
    "wpn_dps_sus":
        "Sustained DPS of the selected weapon, once heat or charge "
        "regeneration limits it. An em-dash means no sustained value is "
        "published for this gun.",
    "wpn_alpha":
        "Damage per shot for the selected weapon, all damage types added "
        "together -- the whole pellet spread for a scattergun, a full charge "
        "for a charge weapon.",
    "wpn_fire_rate":
        "Rate of fire in rounds per MINUTE. The picker table shows the same "
        "thing as RPS, shots per second, which is this divided by 60.",
    "wpn_ammo":
        "Shots carried. Ballistic guns carry rounds that run out until you "
        "rearm; energy guns show the size of a charge pool that refills.",
    "wpn_speed":
        "Projectile speed in metres per second.",
    "wpn_range":
        "Metres the projectile travels before expiring. A hard cut-off, not a "
        "damage-falloff range.",
    "wpn_spread":
        "Maximum spread in degrees. 0 is perfectly accurate.",
    "wpn_power":
        "Power the selected weapon draws when firing.",
    "wpn_pen":
        "Penetration in metres -- how far the round punches through material.",
    "wpn_hp":
        "The selected gun's own health. Weapons can be shot off a hardpoint.",
    "shld_hp":
        "Total shield hit points from every generator fitted. Full pool "
        "regardless of power state.",
    "shld_regen":
        "Shield hit points per second for your CURRENT power allocation. A "
        "reading of 0.0 means the bank is switched off or has no pips -- that "
        "is a measured zero. An em-dash means no shields are fitted at all.",
    "shld_phys":
        "Average PHYSICAL resistance across the shields fitted, slid between "
        "each shield's minimum and maximum by your pip allocation. With the "
        "power allocator off it shows the maxima. It is an average, not a sum: "
        "two shields do not stack resistance here.",
    "shld_enrg":
        "Average ENERGY resistance across the shields fitted, on the same "
        "pip-interpolated basis. Plenty of shields resist no energy damage and "
        "read 0%.",
    "shld_dist":
        "Average DISTORTION resistance across the shields fitted, on the same "
        "pip-interpolated basis.",
    "hull_hp":
        "The ship's hull hit points, from its armour health (falling back to "
        "total hull HP when it carries no armour entry).",
    "armor_type":
        "The armour sub-type the data source reports for this hull.",
    "armor_phys":
        "How the hull's armour changes incoming PHYSICAL damage, as a signed "
        "percentage of normal. NEGATIVE is good: -19% means the hull takes 19% "
        "less. Note this is the opposite sign convention to the shield resist "
        "rows above, where a POSITIVE figure is damage removed.",
    "armor_enrg":
        "How the armour changes incoming ENERGY damage, signed against normal. "
        "Negative takes less, positive takes more.",
    "armor_dist":
        "How the armour changes incoming DISTORTION damage, signed against "
        "normal. Negative takes less, positive takes more.",
    "cargo":
        "Cargo capacity in SCU (standard cargo units).",
    "crew":
        "Crew the ship is designed for. It matters above: manned turrets need "
        "somebody sitting in them.",
    "scm_speed":
        "SCM speed -- top speed in metres per second in combat mode.",
    "ab_speed":
        "AB speed -- top speed in metres per second with the afterburner lit.",
    "h2_fuel":
        "Hydrogen fuel capacity: the fuel your thrusters burn. Separate from "
        "quantum fuel.",
    "qt_fuel":
        "Quantum fuel capacity, used only for quantum jumps. Running dry here "
        "does not stop you flying.",
    "pwr_output":
        "Total power your fitted power plants generate.",
    "pwr_draw":
        "Total power every fitted component draws. This is a straight sum over "
        "everything equipped, NOT the power allocator's live draw -- the "
        "allocator counts only what is switched on, so the two figures differ "
        "by design.",
    "pwr_margin":
        "Output minus draw. A negative figure means the fit asks for more "
        "power than the plants make. It only appears once a power plant has "
        "been resolved for the ship.",
    "cooling":
        "Total cooling from every cooler fitted.",
    "sig_em":
        "EM signature -- the same figure as the lightning readout in the power "
        "allocator. An em-dash means a fitted power component has no data.",
    "sig_ir":
        "IR signature -- the same figure as the flame readout in the power "
        "allocator. It comes from coolers only.",
    "sig_cs":
        "CS signature -- cross-section, your physical size to radar. Set by the "
        "hull and its armour; nothing you fit changes it.",
    # qt_speed is deliberately absent -- see OMITTED.
}

# ---------------------------------------------------------------------------
# Time-To-Kill panel (ttk_dialog.py)
# ---------------------------------------------------------------------------

TTK_TIPS = {
    "shield":
        "The TARGET ship's total shield HP, from the shields it carries as "
        "stock -- not from anything you have selected.",
    "hull":
        "The target ship's hull hit points.",
    "eff":
        "Effective HP -- shield plus hull added together. It ignores shield "
        "regeneration during the fight, so treat it as the floor of what you "
        "have to chew through rather than the true total.",
    "regen":
        "The target's shield regeneration per second. If your sustained DPS "
        "does not exceed it you cannot break the shield under sustained fire, "
        "and this panel will say the kill is not possible.",
    "ttk":
        "Seconds to kill: effective HP divided by YOUR sustained DPS. It "
        "assumes every shot hits and nothing regenerates. Your own sustained "
        "DPS can itself be a floor -- guns with no published sustained value "
        "are excluded from it -- in which case the real kill is faster than "
        "shown.",
}


# ---------------------------------------------------------------------------
# Registry + accessor
# ---------------------------------------------------------------------------

#: namespace -> the dict holding that panel's tooltips.
GROUPS = {
    "table":          TABLE_TIPS,
    "weapon":         WEAPON_TIPS,
    "missile":        MISSILE_TIPS,
    "shield":         SHIELD_TIPS,
    "cooler":         COOLER_TIPS,
    "radar":          RADAR_TIPS,
    "powerplant":     POWERPLANT_TIPS,
    "qdrive":         QDRIVE_TIPS,
    "mount":          MOUNT_TIPS,
    "missile_rack":   MISSILE_RACK_TIPS,
    "emp":            EMP_TIPS,
    "qed":            QED_TIPS,
    "bomb":           BOMB_TIPS,
    "mining_laser":   MINING_LASER_TIPS,
    "tool_arm":       TOOL_ARM_TIPS,
    "salvage_head":   SALVAGE_HEAD_TIPS,
    "ore_pod":        ORE_POD_TIPS,
    "fuel_tank":      FUEL_TANK_TIPS,
    "module":         MODULE_TIPS,
    "footer":         FOOTER_TIPS,
    "power":          POWER_TIPS,
    "power_category": POWER_CATEGORY_TIPS,
    "ship":           SHIP_TIPS,
    "ttk":            TTK_TIPS,
}

#: Where the test looks for each namespace's field keys.  ``spec`` names the
#: ``*_TABLE_COLS`` list in dps_ui/constants.py whose 5-tuples must contain the
#: key; ``files`` are source files (relative to the skill root) where a literal
#: occurrence of the key is accepted instead.  A namespace may use either or
#: both.  This is the anti-rot contract: a key that stops appearing on screen
#: fails the test rather than silently going quiet.
SOURCE_MAP = {
    "table":          {"spec": None,                        "files": ["dps_ui/constants.py", "dps_ui/widgets.py"]},
    "weapon":         {"spec": "WEAPON_TABLE_COLS",         "files": []},
    "missile":        {"spec": "MISSILE_TABLE_COLS",        "files": []},
    "shield":         {"spec": "SHIELD_TABLE_COLS",         "files": []},
    "cooler":         {"spec": "COOLER_TABLE_COLS",         "files": []},
    "radar":          {"spec": "RADAR_TABLE_COLS",          "files": []},
    "powerplant":     {"spec": "PP_COLS",                   "files": []},
    "qdrive":         {"spec": "QD_COLS",                   "files": []},
    "mount":          {"spec": "MOUNT_TABLE_COLS",          "files": []},
    "missile_rack":   {"spec": "MISSILE_RACK_TABLE_COLS",   "files": []},
    "emp":            {"spec": "EMP_TABLE_COLS",            "files": []},
    "qed":            {"spec": "QED_TABLE_COLS",            "files": []},
    "bomb":           {"spec": "BOMB_TABLE_COLS",           "files": []},
    "mining_laser":   {"spec": "MINING_LASER_TABLE_COLS",   "files": []},
    "tool_arm":       {"spec": "TOOL_ARM_TABLE_COLS",       "files": []},
    "salvage_head":   {"spec": "SALVAGE_HEAD_TABLE_COLS",   "files": []},
    "ore_pod":        {"spec": "ORE_POD_TABLE_COLS",        "files": []},
    "fuel_tank":      {"spec": "FUEL_TANK_TABLE_COLS",      "files": []},
    "module":         {"spec": "ERKUL_MODULE_TABLE_COLS",   "files": []},
    "footer":         {"spec": None,                        "files": ["dps_ui/app.py"]},
    "power":          {"spec": None,                        "files": ["dps_ui/power_widget.py"]},
    "power_category": {"spec": None,                        "files": ["services/power_engine.py"]},
    "ship":           {"spec": None,                        "files": ["dps_ui/app.py"]},
    "ttk":            {"spec": None,                        "files": ["dps_ui/ttk_dialog.py"]},
}

#: Flat, fully-qualified lookup: "namespace.field" -> text.
TIPS = {
    f"{ns}.{field}": text
    for ns, group in GROUPS.items()
    for field, text in group.items()
}

#: Every key this module defines.  Exported so the test can walk it.
KEYS = frozenset(TIPS)

#: Field keys whose meaning is identical across pickers, so a namespaced miss
#: falls back to the ``table.*`` entry.
_FALLBACK_NS = "table"

#: A handful of keys are NOT field keys -- they label a pseudo-column or a
#: button that carries no entry in any ``*_TABLE_COLS`` tuple.  For those the
#: test looks for this literal instead of a quoted field key.  Keeping the
#: anchor here rather than inside the test means the thing being pinned is
#: visible from the copy it pins.
ANCHORS = {
    # The right-hand "buy" column is drawn from a shopping-cart glyph in
    # dps_ui/widgets.py; there is no field key behind it.
    "table.cart": "\U0001f6d2",
}

#: UI strings this module deliberately does NOT explain, and why.  Kept in the
#: source so the decision does not have to be re-made from scratch.
OMITTED = {
    "radar.detection_min":
        "RADAR_TABLE_COLS 'Det' column, from radar.detectionLifetimeMin. "
        "'Detection lifetime' cannot be pinned down from the code -- neither "
        "the unit nor whether higher is better is derivable -- so no copy.",
    "radar.detection_max":
        "Same field family as detection_min ('Max' column). Omitted for the "
        "same reason.",
    "qdrive.power_draw":
        "QD_COLS has a 'Power' column, but compute_qdrive_stats_erkul hardcodes "
        "power_draw to 0.0, so the column can only ever render an em-dash. "
        "Worth fixing in the stat extractor rather than papering over with a "
        "tooltip.",
    "ship.qt_speed":
        "The 'QT speed' row is set to the literal '?' in _update_overview and "
        "is never computed. Nothing to describe until it is.",
    "mining_laser.instability":
        "miningLaser.laserInstability. A mining mechanic whose direction and "
        "unit are not derivable here.",
    "mining_laser.resistance_mod":
        "miningLaser.resistanceModifier. Additive or multiplicative, and in "
        "which direction, is not established by the code.",
    "mining_laser.filter_mod":
        "miningLaser.filterModifier. Same problem.",
    "mining_laser.throttle_min":
        "miningLaser.throttleMinimum. Same problem.",
    "CML_TABLE_COLS":
        "Imported by app.py and never used to build a table, so no such panel "
        "renders. Same for MINING_MODIFIER_TABLE_COLS and "
        "SALVAGE_MODIFIER_TABLE_COLS.",
}


def tip(key: str, default: str = "") -> str:
    """Return the tooltip for a fully-qualified ``"namespace.field"`` key.

    An unknown key returns ``default`` and NEVER raises -- a missing tooltip
    must not be able to break a table build.

    Lookup order:
      1. the exact key, e.g. ``"shield.hp"``
      2. ``"table.<field>"`` for the columns whose meaning is shared across
         every picker, e.g. ``"cooler.hp"`` -> ``"table.hp"``

    >>> tip("weapon.rps").startswith("Shots per second")
    True
    >>> tip("cooler.hp") == tip("table.hp")
    True
    >>> tip("no.such.thing", "n/a")
    'n/a'
    """
    if not isinstance(key, str):
        return default
    text = TIPS.get(key)
    if text is not None:
        return text
    ns, _, field = key.partition(".")
    if field and ns != _FALLBACK_NS:
        text = TIPS.get(f"{_FALLBACK_NS}.{field}")
        if text is not None:
            return text
    return default


def tip_for(namespace: str, field: str, default: str = "") -> str:
    """Convenience wrapper: ``tip_for("weapon", "efficiency")``.

    Useful at render time, where the namespace comes from which table is being
    built and the field from the column tuple.
    """
    if not isinstance(namespace, str) or not isinstance(field, str):
        return default
    return tip(f"{namespace}.{field}", default)


def group(namespace: str) -> dict:
    """Return a namespace's dict (empty if unknown). Read-only by convention."""
    return GROUPS.get(namespace, {})


__all__ = [
    "GROUPS", "SOURCE_MAP", "ANCHORS", "TIPS", "KEYS", "OMITTED",
    "tip", "tip_for", "group",
    "TABLE_TIPS", "WEAPON_TIPS", "MISSILE_TIPS", "SHIELD_TIPS", "COOLER_TIPS",
    "RADAR_TIPS", "POWERPLANT_TIPS", "QDRIVE_TIPS", "MOUNT_TIPS",
    "MISSILE_RACK_TIPS", "EMP_TIPS", "QED_TIPS", "BOMB_TIPS",
    "MINING_LASER_TIPS", "TOOL_ARM_TIPS", "SALVAGE_HEAD_TIPS", "ORE_POD_TIPS",
    "FUEL_TANK_TIPS", "MODULE_TIPS", "FOOTER_TIPS", "POWER_TIPS",
    "POWER_CATEGORY_TIPS", "SHIP_TIPS", "TTK_TIPS",
]
