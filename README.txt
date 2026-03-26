================================================================
  SC_TOOLBOX Beta V1 -- Star Citizen Companion Suite
================================================================

OVERVIEW
--------
SC_Toolbox is a unified launcher for six interactive Star Citizen
gameplay tools. It runs as a lightweight desktop overlay with
global hotkeys, so you can pull up any tool instantly while
playing without alt-tabbing out of the game. Each tool opens as
its own always-on-top window that you can position, resize, and
toggle with a single keypress.

The toolbox is designed to work alongside WingmanAI (Anthropic's
voice-controlled assistant for Star Citizen), but it also runs
fully standalone. Just launch it with the included batch file and
all six tools are available through the tile launcher or hotkeys.

Data is pulled live from community APIs and cached locally for
speed. Sources include erkul.games for DPS and loadout data,
uexcorp.space for market prices, trade routes, and ship info,
scmdb.net for mission data and blueprints, and fleetyards.net
for ship hardpoint details.


INSTALLATION
------------
Option A -- Automatic (recommended):
  1. Double-click INSTALL_AND_LAUNCH.bat
  2. If Python is not installed, it will download and install it
  3. The toolbox launches automatically after setup

Option B -- Manual:
  1. Install Python 3.10 or newer (with tkinter included)
  2. Install pynput for global hotkeys:
       pip install pynput
  3. Install requests (used by DPS Calculator and Cargo Loader):
       pip install requests
  4. Double-click LAUNCH.bat

Requirements:
  - Windows 10 or 11
  - Python 3.10+ with tkinter (included in standard installer)
  - Internet connection (for fetching live game data)
  - pynput (for global hotkeys -- installed automatically by bat)


QUICK START
-----------
1. Run INSTALL_AND_LAUNCH.bat (first time) or LAUNCH.bat
2. The SC_Toolbox launcher window appears with six tool tiles
3. Click any tile to launch that tool, or use the hotkeys below
4. Press the launcher hotkey again to hide/show the launcher

Default Hotkeys:
  Shift + `    Toggle SC_Toolbox launcher window
  Shift + 1    DPS Calculator
  Shift + 2    Cargo Loader
  Shift + 3    Mission Database
  Shift + 4    Mining Loadout
  Shift + 5    Market Finder
  Shift + 6    Trade Hub

All hotkeys can be customized in Settings (see below).


================================================================
  SKILL GUIDES
================================================================

1. DPS CALCULATOR (Shift+1)
----------------------------
   Data Source: erkul.games API + fleetyards.net API

   WHAT IT DOES:
   A full ship loadout viewer and DPS calculator, styled after
   erkul.games. Shows weapons, missiles, shields, power plants,
   coolers, quantum drives, thrusters, and an overall ship
   summary. Computes raw DPS, sustained DPS, alpha damage,
   shield HP, and power consumption for any loadout.

   LAYOUT:
   The window has three panels side by side:
   - Left panel: Weapons (guns and turrets) with per-weapon DPS
   - Center panel: Two sub-tabs:
       "Defenses / Systems" -- shields, coolers, radars
       "Power & Propulsion" -- power plants, QD, thrusters
   - Right panel: Overview summary + Power Allocator simulator

   HOW TO USE:
   1. Select a ship from the dropdown at the top of the window.
      Stock weapons, shields, and components auto-populate.
   2. The left panel shows all gun and turret hardpoints. Each
      row displays the weapon name, size, DPS (raw), sustained
      DPS, and damage type breakdown (physical/energy/distortion).
   3. Click any weapon row to open a swap picker -- browse all
      weapons that fit that hardpoint size and select a new one.
   4. Switch to the center panel's "Defenses / Systems" tab to
      see shields, coolers, and radars. Click any row to swap.
   5. Switch to "Power & Propulsion" to see power plants,
      quantum drives, thrusters, and fuel tanks.
   6. The right panel shows a two-column ship summary with total
      DPS, burst damage, shield HP, and key stats at a glance.

   POWER ALLOCATOR:
   Below the overview panel is the Power Allocator simulator.
   It models the in-game power triangle system.
   - Each powered component category (weapons, shields, etc.)
     has a vertical stack of "pip" bars.
   - Click a pip bar to increase or decrease power to that group.
     More pips = more power = higher output but more draw.
   - The consumption bar at the bottom shows total power draw
     versus your power plant's output capacity.
   - Toggle between SCM and NAV flight modes:
       SCM mode: weapons and shields are ON, quantum drive is OFF
       NAV mode: weapons and shields are OFF, quantum drive is ON
   - Click the category icon at the top of a pip column to
     toggle all components in that category on or off.

   TIPS:
   - Sustained DPS accounts for overheat and ammo regeneration,
     giving a more realistic damage estimate than raw DPS.
   - Data is cached for 2 hours. Close and reopen the tool to
     force a refresh on patch days.
   - The color stripe on each component row indicates its type
     (blue for energy, orange for thermal, etc.).


2. CARGO LOADER (Shift+2)
--------------------------
   Data Source: sc-cargo.space (auto-fetched)

   WHAT IT DOES:
   A 3D isometric cargo grid viewer and container optimizer.
   Shows every cargo bay slot for a ship and calculates the
   best container mix to maximize your cargo capacity. Renders
   containers in an isometric projection so you can visualize
   exactly how boxes stack in each grid bay.

   HOW TO USE:
   1. Select a ship from the dropdown. The cargo grid layout
      loads automatically, showing all cargo bay slots.
   2. The isometric 3D view displays containers color-coded by
      size (1, 2, 4, 8, 16, 24, or 32 SCU).
   3. Use the container count spinners on the side panel to
      manually set how many of each container size you want.
   4. Click "Optimize" to auto-calculate the best container mix
      that fills the most SCU in the available grid slots.
   5. Click "Clear" to remove all containers from the grid.
   6. Click "Reset" to restore the default container loadout
      for the selected ship.

   CUSTOM SHIP LAYOUTS:
   If your ship's cargo layout is not in the built-in database,
   you can create a custom layout using the cargo_grid_editor.html
   file (located in the Cargo_loader folder). Open it in a browser,
   design your grid, export the JSON, and add it to the app's
   reference loadouts.

   TIPS:
   - Ships with known authoritative container mixes (e.g.,
     Caterpillar, C2 Hercules, Constellation Taurus) use their
     verified loadouts automatically.
   - Container sizes follow the in-game standard: 1, 2, 4, 8,
     16, 24, and 32 SCU.
   - 4 SCU containers are flat crates and cannot be rotated to
     stand on end.


3. MISSION DATABASE (Shift+3)
------------------------------
   Data Source: scmdb.net

   WHAT IT DOES:
   A browsable database of all Star Citizen missions, crafting
   blueprints, and mining resource locations. Three separate
   pages cover different aspects of the game's PvE content.

   LAYOUT:
   Three page tabs along the top:
     Missions    -- browse and filter all in-game missions
     Fabricator  -- crafting blueprints and material requirements
     Resources   -- mining resource locations by planet/moon

   LIVE vs PTU TOGGLE:
   A toggle in the top bar lets you switch between LIVE server
   data and PTU (Public Test Universe) data. Some content
   (especially Fabricator blueprints) may only be available on
   PTU. The app auto-switches to PTU if you open the Fabricator
   page and no LIVE crafting data is available.

   MISSIONS PAGE:
   1. Browse mission cards in a scrollable grid. Each card shows
      the mission name, faction badge, type tags (Delivery,
      Combat, Bounty Hunt, etc.), and reward in aUEC.
   2. Filter missions using the sidebar controls:
      - Category: career, story
      - System: Stanton, Pyro, Nyx, Multi
      - Type: Delivery, Combat, Salvage, Investigation, etc.
      - Faction: filter by the mission-giving faction
      - Legality: Legal or Illegal
      - Chain/Once: repeatable vs one-time missions
   3. Click any mission card to open a detail view showing the
      full description, objectives, payout tiers, and a
      calculator for estimating earnings based on completion
      time and multipliers.

   FABRICATOR PAGE:
   1. Browse crafting blueprints in a scrollable grid.
   2. Filter by category, search by name.
   3. Click a blueprint to see required materials, quantities,
      and crafting details.
   4. Note: Fabricator data is primarily available on PTU.

   RESOURCES PAGE:
   1. Browse mining resource locations in a filterable table.
   2. Filter by resource name using the checkbox dropdown.
   3. Each entry shows the resource, planet/moon, and location.

   TIPS:
   - Use the search bar to quickly find missions by name.
   - Mission rewards shown are base values; actual payouts may
     vary with reputation and other in-game modifiers.
   - The faction badge shows 2-letter initials for quick
     identification.


4. MINING LOADOUT (Shift+4)
-----------------------------
   Data Source: uexcorp.space API v2

   WHAT IT DOES:
   A mining equipment optimizer for configuring mining lasers,
   modules, and gadgets on the Prospector, MOLE, and Golem.
   Shows stat breakdowns for power, resistance, instability,
   inert material reduction, charge rate, and charge window.

   HOW TO USE:
   1. Select your ship at the top: Prospector (1 turret, size 1
      laser), MOLE (3 turrets, size 2 lasers), or Golem (1
      turret, size 1 laser).
   2. Each turret shows a laser dropdown and module slot
      dropdowns. Select your mining laser from the list.
   3. Assign up to 2 modules per turret (active or passive).
      Active modules have limited uses and a duration timer;
      passive modules are always on.
   4. Select a mining gadget from the gadget dropdown (gadgets
      apply to the whole ship, not per-turret).
   5. The stats panel on the right updates live, showing:
      - Mining laser power (min/max/extraction)
      - Resistance modifier
      - Instability modifier
      - Inert material modifier
      - Charge rate and charge window modifiers
      - Total loadout price (combined cost of all equipment)
   6. Price details and buy locations are shown below the stats.

   TIPS:
   - Stock lasers are pre-selected when you switch ships.
   - Module effects stack -- two instability-reducing modules
     give a larger total reduction.
   - Active modules (like the Stampede) give big boosts but
     have limited uses per mining session.
   - Gadgets affect the charge window and instability globally.


5. MARKET FINDER (Shift+5)
----------------------------
   Data Source: uexcorp.space API v2

   WHAT IT DOES:
   A searchable catalog of all purchasable items in Star Citizen,
   including armor, weapons, clothing, ship components, food,
   drinks, and ships. Shows where to buy and sell each item,
   with prices and terminal locations.

   LAYOUT:
   Category tabs along the top:
     All            -- every item in the database
     Armor          -- helmets, chest plates, legs, arms
     Weapons        -- personal FPS weapons
     Ship Weapons   -- vehicle-mounted guns
     Missiles       -- missile racks and missiles
     Clothing       -- undersuits, jackets, pants
     Ship Components-- power plants, coolers, shields, QDs
     Utility        -- tractor beams, multitools, attachments
     Sustenance     -- food and drinks
     Misc           -- liveries, commodities, miscellaneous
     Ships          -- flyable ships with purchase locations
     Rentals        -- ship rental locations and prices

   HOW TO USE:
   1. Click a category tab to filter by item type, or stay on
      "All" to see everything.
   2. Type in the search bar to filter items by name. Results
      update as you type.
   3. The item list shows name, category, and base price.
   4. Click any item to open the detail panel on the right,
      which shows:
      - Item description and stats
      - Buy locations: which terminals sell it and at what price
      - Sell locations: where you can sell it and for how much
   5. On the Ships tab, each entry includes cargo capacity,
      crew size, and purchase price. Click a ship to see all
      terminals where it can be bought.
   6. On the Rentals tab, see ship rental prices and which
      terminals offer rentals.

   TIPS:
   - Data refreshes automatically every hour. Prices reflect
     the latest data from uexcorp.space.
   - The cache file is stored locally; delete .uex_cache.json
     in the Market_Finder folder to force a fresh fetch.


6. TRADE HUB (Shift+6)
------------------------
   Data Source: uexcorp.space API v2

   WHAT IT DOES:
   A trade route calculator that finds profitable single-hop
   and multi-leg trade routes. Filters by star system, location,
   terminal, commodity, and ship cargo capacity. Shows estimated
   profit, ROI, investment cost, and travel distance for each
   route.

   LAYOUT:
   Two sections in the main view:
   - Single Routes table: one-hop buy-here-sell-there routes
   - Loop Routes table: multi-leg chains where the sell terminal
     of one leg is the buy terminal of the next

   Sidebar filters on the left let you narrow results.

   HOW TO USE:
   1. Select your ship from the quick-ship dropdown at the top
      to cap routes by your cargo capacity (SCU). Or leave it
      on "No Ship Cap" to see all routes.
   2. Use the sidebar filters to narrow results:
      - Buy System / Sell System: filter origin or destination
        by star system (Stanton, Pyro, etc.)
      - Buy Location / Sell Location: filter by planet or station
      - Buy Terminal / Sell Terminal: filter by specific terminal
      - Commodity: show only routes for a specific commodity
   3. The single routes table columns are:
      Item       -- commodity being traded
      Buy At     -- terminal where you purchase
      CS         -- crime stat risk at origin
      Invest     -- investment cost (price x SCU)
      SCU        -- available supply at origin
      SCU-U      -- user-reported supply
      Sell At    -- terminal where you sell
      CS         -- crime stat risk at destination
      Invest     -- sell value
      SCU-C      -- demand capacity at destination
      SCU-U      -- user-reported demand
      Distance   -- travel distance between terminals
      ETA        -- estimated travel time
      ROI        -- return on investment percentage
      Income     -- estimated profit per run
   4. Click a column header to sort by that column.
   5. The loop routes table shows multi-leg chains with:
      Origin Terminal, System, number of Legs, Commodity Chain,
      minimum available SCU, and total estimated profit.
   6. Click a loop route to expand and see each individual leg.

   TIPS:
   - Set your ship to get routes capped to your actual cargo
     capacity for realistic profit estimates.
   - Sort by Income for highest absolute profit, or by ROI for
     best return on investment.
   - Multi-leg loops often beat single routes for profit per
     hour if you are already at the sell terminal.
   - Use system filters to restrict routes to your current star
     system and avoid quantum travel between systems.


================================================================
  SETTINGS & CUSTOMIZATION
================================================================
Opening Settings:
  Click the "Settings & Keybinds" button at the bottom of the
  SC_Toolbox launcher window to expand the settings panel.

Changing Hotkeys:
  1. Expand the settings panel.
  2. Each tool has a hotkey entry field showing its current
     binding (e.g., <shift>+1).
  3. Type the new hotkey using pynput format:
       <shift>+1    <ctrl>+F2    <alt>+q    F5
  4. Click "Apply Hotkeys" to save and activate.
  5. The hotkey badges on each tile update to show the new keys.

Window Positions:
  Each tool remembers its last window position and size.
  Drag a tool window to reposition it, and the location is saved
  automatically for the next launch.

Settings File:
  All settings are stored in skill_launcher_settings.json in the
  SC_Toolbox_Beta_V1 folder. You can edit this file manually if
  needed, but use the Settings panel for hotkey changes.


================================================================
  TROUBLESHOOTING
================================================================
Python not found:
  Run INSTALL_AND_LAUNCH.bat to auto-install Python. If you
  already have Python installed, make sure it is 3.10 or newer
  and includes tkinter (the standard Windows installer does).

Hotkeys not working:
  Install pynput: pip install pynput
  If hotkeys still do not respond, check that no other program
  is capturing the same key combination (e.g., Discord, OBS).

Tool window does not appear:
  The tool may have launched off-screen. Delete the settings
  file (skill_launcher_settings.json) to reset all window
  positions to defaults, then relaunch.

API timeout or no data:
  Check your internet connection. The APIs (erkul.games,
  uexcorp.space, scmdb.net) must be reachable. If one is down,
  that specific tool will show an error but others will work.

Stale data after a game patch:
  Delete the cache files to force a fresh fetch:
    DPS Calculator:  skills/DPS_Calculator/.erkul_cache.json
    DPS Calculator:  skills/DPS_Calculator/.fy_hardpoints_cache.json
    Cargo Loader:    skills/Cargo_loader/.cargo_cache.json
    Mission Database:skills/Mission_Database/.scmdb_cache.json
    Market Finder:   skills/Market_Finder/.uex_cache.json
  Trade Hub and Mining Loadout store config files but fetch
  data live each session.

Reporting bugs:
  Join the Discord (link below) and describe the issue with
  your Python version and any error messages from the tool's
  log files (trade_hub.log, mining_loadout.log, etc.).


================================================================
  CREDITS & DATA SOURCES
================================================================
erkul.games      -- DPS calculator data, weapon stats, loadouts
                    Support: patreon.com/erkul
uexcorp.space    -- Market prices, trade routes, ship data,
                    mining equipment stats
scmdb.net        -- Mission database, crafting blueprints,
                    mining resource locations
fleetyards.net   -- Ship hardpoint data (power plants, QDs,
                    thrusters, fuel tanks)

Discord: https://discord.gg/A7JDCxmC


================================================================
  VERSION HISTORY
================================================================
Beta V1 -- March 2026 -- Initial Release
  Included tools:
    - DPS Calculator (erkul.games + fleetyards.net)
    - Cargo Loader (sc-cargo.space)
    - Mission Database (scmdb.net)
    - Mining Loadout (uexcorp.space)
    - Market Finder (uexcorp.space)
    - Trade Hub (uexcorp.space)
  Features:
    - Unified launcher with tile grid
    - Global hotkeys via pynput
    - Customizable keybinds and window positions
    - Local data caching for performance
    - Always-on-top overlay windows
