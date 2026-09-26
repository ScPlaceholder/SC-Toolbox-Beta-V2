================================================================
  SC TOOLBOX v1.2 -- Star Citizen Companion Suite
================================================================

OVERVIEW
--------
SC Toolbox is a unified launcher for seven interactive Star Citizen
gameplay tools. It runs as a lightweight desktop overlay with global
hotkeys, so you can pull up any tool instantly while playing without
alt-tabbing out of the game. Each tool opens as its own always-on-top
window that you can position, resize, and toggle with a single
keypress.

The toolbox runs fully standalone or as a WingmanAI voice-activated
skill. Data is pulled live from community APIs and cached locally
for speed.


INSTALLATION
------------
Option A -- Installer (recommended):
  1. Download SC_Toolbox_Setup.exe from GitHub Releases
  2. Run the installer -- Python and all dependencies are bundled
  3. Launch from the desktop shortcut or Start Menu

Option B -- From source (advanced):
  1. Install Python 3.10 or newer
  2. Run INSTALL_AND_LAUNCH.bat (auto-installs dependencies)
  3. Or manually: pip install -r requirements.txt
     then: python skill_launcher.py

Requirements:
  - Windows 10 or 11 (64-bit)
  - Internet connection (for fetching live game data)


QUICK START
-----------
1. Launch SC Toolbox from the desktop shortcut
2. The launcher window appears with seven tool tiles
3. Click any tile to launch that tool, or use the hotkeys below
4. Press the launcher hotkey to hide/show the launcher

Default Hotkeys:
  Ctrl + 0     Toggle SC Toolbox launcher window
  Shift + 1    DPS Calculator
  Shift + 2    Cargo Loader
  Shift + 3    Mission Database
  Shift + 4    Mining Loadout
  Shift + 5    Item Finder
  Shift + 6    Trade Hub
  Shift + 7    Craft Database
  Shift + 8    Battle Buddy
  Shift + 9    Starmap
  Shift + 0    Mouse Blocker
  Ctrl + 1     Mining Signals
  Ctrl + 2     SuitMk2
  Ctrl + 3     Toolbox Assistant
  Ctrl + 4     PlayTime Calculator

Every default is a modifier plus a number (no letter or symbol keys), and
none is bound in Star Citizen's default keyboard profile. If two tools share
a combo the launcher keeps the first and warns in its status line.

All hotkeys can be customized in Settings.


================================================================
  TOOL GUIDES
================================================================

1. DPS CALCULATOR (Shift+1)
----------------------------
   Data: erkul.games API + UEX Corp API (component prices)

   WHAT IT DOES:
   A full ship loadout viewer and DPS calculator inspired by
   erkul.games. Build and compare ship loadouts with weapons,
   missiles, shields, power plants, coolers, quantum drives,
   thrusters, and radar. Computes DPS, alpha damage, sustained
   damage, power consumption, heat output, and signature levels.
   Component prices are pulled live from UEX Corp.

   LAYOUT:
   Three-panel design:
   - Left panel: Ship selector and component category list.
     Click a category to browse available components.
   - Center panel: Two tabs:
       "Defenses / Systems" -- shields, coolers, radar, armor
       "Power & Propulsion" -- power plant, Q-drive, thrusters
   - Right panel: Weapons and missiles table with DPS columns.
   - Footer: Totals bar showing alpha damage, DPS, heat, and
     signature levels across all components.

   HOW TO USE:
   1. Select a ship from the fuzzy-search dropdown at the top.
      Stock components auto-populate for all hardpoints.
   2. The weapons table on the right shows each gun and turret
      hardpoint with name, size, damage per second (by type:
      physical, energy, distortion, thermal), alpha damage,
      rate of fire, and ammo count.
   3. Click any component row to open a swap picker popup.
      Browse all compatible components, filter by text, and
      click one to slot it into your loadout.
   4. Use the "Leave Empty" button in the picker to remove a
      component from a hardpoint.
   5. Switch between SCM and NAV flight modes to see how power
      consumption changes. SCM has weapons and shields active;
      NAV has quantum drive active.
   6. Component rows show live pricing bubbles from UEX Corp
      with buy locations and prices.

   TIPS:
   - Sustained DPS accounts for overheat and ammo regen, giving
     a more realistic damage number than raw DPS.
   - Use the tutorial button in the title bar for a guided
     walkthrough covering weapons, defenses, and power systems.


2. CARGO LOADER (Shift+2)
--------------------------
   Data: sc-cargo.space + UEX Corp API

   WHAT IT DOES:
   A 3D isometric cargo grid viewer and container packing
   optimizer. Visualizes every cargo bay slot for a ship and
   calculates the best container mix to maximize SCU capacity.
   Supports 30+ ships with pre-made cargo grid layouts.

   HOW TO USE:
   1. Select a ship from the fuzzy-search dropdown. The cargo
      grid layout loads automatically.
   2. The isometric 3D view renders containers color-coded by
      size (1, 2, 4, 8, 16, 24, or 32 SCU).
   3. Select a commodity from the dropdown to see color-coded
      cargo types based on UEX commodity data.
   4. Click in the canvas to place containers. Drag to pan,
      scroll to zoom.
   5. Stats panel shows total SCU, packing efficiency, and
      price summary.

   SHIPS SUPPORTED:
   400i, 890 Jump, A2/C2/M2 Hercules, Carrack, Caterpillar,
   Clipper, Constellation (all variants), Cutlass (all variants),
   Freelancer (all variants), Hammerhead, Hermes, Idris M/P,
   Mercury Star Runner, MOTH, Polaris, Raft, Reclaimer,
   Retaliator, SRV, Starfarer (both), Zeus CL, and more.

   TIPS:
   - Container sizes follow the in-game standard: 1, 2, 4, 8,
     16, 24, and 32 SCU.
   - Reference loadouts for popular haulers (Caterpillar, C2,
     Taurus) are verified and loaded automatically.


3. MISSION DATABASE (Shift+3)
------------------------------
   Data: scmdb.net (LIVE + PTU)

   WHAT IT DOES:
   A browsable database of all Star Citizen missions, crafting
   blueprints, and mining resource locations. Three pages cover
   different aspects of PvE content, with separate data for
   LIVE and PTU servers.

   LAYOUT:
   Three tabs along the top:
     Missions    -- browse and filter all in-game missions
     Fabricator  -- crafting blueprints and material requirements
     Resources   -- mining resource locations by planet/moon

   LIVE vs PTU:
   Toggle buttons in the title bar switch between LIVE and PTU
   data. The app auto-switches to PTU if no LIVE data is
   available for the current page.

   MISSIONS PAGE:
   1. Browse mission cards in a scrollable grid. Each card shows
      the mission name, faction badge, type tags, and reward.
   2. Filter using the sidebar:
      - Search bar: filter by mission name
      - Category buttons: Trading, Combat, Transport, etc.
      - System buttons: Stanton, Pyro, Nyx, etc.
      - Type dropdown: Delivery, Bounty Hunt, Salvage, etc.
      - Faction dropdown: filter by mission giver
      - Rank slider: filter by mission rank (0-5)
      - Reward range: set min/max aUEC payout
      - Clear All button: reset all filters
   3. Click a mission card to open a detail view with full
      description, objectives, payout tiers, and an earnings
      calculator.

   FABRICATOR PAGE:
   1. Browse crafting blueprints in a scrollable grid.
   2. Filter by type (Armor, Shield, Weapon, Component, Ammo),
      manufacturer, material, armor slot, and subtype.
   3. Click a blueprint card to see required ingredients,
      quantities, rarity, crafting time, and output details.

   RESOURCES PAGE:
   Browse mining and harvesting node data with location info.

   TIPS:
   - Use the search bar for quick name lookups across all pages.
   - Fabricator data is often PTU-only; switch to PTU if the
     page appears empty.


4. MINING LOADOUT (Shift+4)
-----------------------------
   Data: UEX Corp API v2

   WHAT IT DOES:
   A mining equipment optimizer for configuring lasers, modules,
   and gadgets on mining ships. Shows live stat calculations for
   DPS, charges, extraction efficiency, power draw, and total
   equipment cost with pricing breakdown.

   LAYOUT:
   - Left sidebar: Ship quick-select buttons
   - Center: Turret panels with laser and module dropdowns
   - Right: Stats panel and detail cards

   HOW TO USE:
   1. Click a ship button on the left sidebar: MOLE (3 turrets),
      Prospector (1 turret), Expanse, or others as they appear.
   2. Each turret panel shows a laser dropdown and two module
      slot dropdowns. Select your mining laser from the list.
   3. Assign up to 2 modules per turret. Active modules have
      limited uses and a duration timer; passive modules are
      always active.
   4. Select a gadget from the gadget dropdown (applies to the
      whole ship, not per-turret).
   5. The stats panel updates live showing:
      - Mining laser power and extraction efficiency
      - Resistance, instability, and charge modifiers
      - Total loadout price with pricing breakdown
   6. Detail cards below the stats show pricing sources and
      buy locations. Cards are expandable and lockable.

   TIPS:
   - Stock lasers are pre-selected when you switch ships.
   - Module effects stack -- two instability reducers give a
     larger total reduction.
   - Active modules like the Stampede give big boosts but have
     limited uses per session.
   - Use the tutorial button for ship-specific loadout tips.


5. MARKET FINDER (Shift+5)
----------------------------
   Data: UEX Corp API v2

   WHAT IT DOES:
   A searchable commodity browser for all tradeable items in
   Star Citizen. Shows where to buy and sell each commodity,
   with live prices, stock levels, and profit margins. Includes
   ship presets for quick cargo capacity calculations.

   LAYOUT:
   - Tab bar: category tabs (All, Ore, Metals, Minerals,
     Resources, Ships, Consumables, Armor, etc.)
   - Search bar with live filtering
   - Commodity table: scrollable list with columns for buy/sell
     terminals, systems, available SCU, demand, margin, and
     estimated profit
   - Detail panel (right side): expanded pricing, stock levels,
     and profit calculations

   HOW TO USE:
   1. Click a category tab to filter by commodity type, or stay
      on "All" to see everything.
   2. Type in the search bar to filter by name. Results update
      as you type.
   3. Click any row to expand the detail panel showing:
      - All buy terminals with prices and stock
      - All sell terminals with prices and demand
      - Profit margin per SCU
   4. Select a ship preset to auto-calculate profit based on
      your cargo capacity.

   SETTINGS (gear icon):
   - Auto-refresh toggle with configurable interval (5-60 min)
   - Data cache TTL selector (1h, 4h, 12h, 24h)

   TIPS:
   - Sort columns by clicking headers to find the best margins.
   - Use the ship preset dropdown for quick profit estimates.
   - Data refreshes automatically; use the gear icon to adjust
     the refresh interval.


6. TRADE HUB (Shift+6)
------------------------
   Data: UEX Corp API v2

   WHAT IT DOES:
   A trade route calculator that finds profitable single-hop
   and loop trade routes. Filters by location, commodity, and
   ship cargo capacity. Shows profit per run, margin per SCU,
   available stock, and demand at each terminal.

   LAYOUT:
   - Header: location filters, commodity filter, min profit
     input, refresh button, and status display
   - Center: Route table with sortable columns
   - Right: Ship selector with cargo capacity display
   - Routes are color-coded: green (high profit), yellow
     (medium), red (low)

   HOW TO USE:
   1. Select your ship from the dropdown to cap routes by your
      cargo capacity. Leave on "No Ship Cap" to see all routes.
   2. Use the header filters to narrow results:
      - Buy Location: filter by origin terminal or system
      - Sell Location: filter by destination
      - Commodity: show only routes for a specific commodity
      - Min Profit/SCU: set a minimum profit threshold
   3. Route table columns:
      Commodity, Buy Terminal, System, Sell Terminal, System,
      Available SCU, Demand SCU, Margin/SCU, Est. Profit
   4. Click column headers to sort (by profit, margin, etc.).
   5. Auto-refresh keeps routes current (configurable interval).

   SETTINGS (gear icon):
   - Max routes displayed (100-500)
   - Refresh interval (5-300 seconds)
   - Text search filter

   TIPS:
   - Sort by Est. Profit for highest absolute earnings, or by
     Margin/SCU for best return on investment.
   - Use location filters to restrict routes to your current
     star system and avoid long quantum travel.
   - The auto-refresh timer shows when the next update occurs.


7. CRAFT DATABASE (Shift+7)
-----------------------------
   Data: scmdb.net

   WHAT IT DOES:
   A crafting blueprint browser for the Star Citizen Fabricator
   system. Search and filter blueprints by type, manufacturer,
   material, and slot. View detailed ingredient lists with
   quantities and rarity.

   LAYOUT:
   - Stats bar: blueprint count, ingredient count, data version
   - Left sidebar: filter panel with search and toggle buttons
   - Center: blueprint card grid with pagination
   - Detail popup: overlay showing full recipe details

   HOW TO USE:
   1. Browse blueprint cards in the paginated grid.
   2. Use the filter panel on the left to narrow results:
      - Search bar: filter by blueprint name
      - Type buttons: Armor, Shield, Weapon, Component, Ammo
      - Access type: toggle buttons with color coding
      - Manufacturer, Material, Armor Slot, Subtype: multi-
        select dropdowns (dynamically populated)
   3. Click a blueprint card to open the detail popup showing:
      - Blueprint name and icon
      - Crafting time
      - Ingredient table with item names, quantities, and rarity
      - Output details and requirements
   4. Use pagination controls (prev/next, page numbers) to
      navigate through large result sets.

   TIPS:
   - The stats bar shows how many blueprints match your current
     filters out of the total database.
   - Crafting data comes from scmdb.net and may be PTU-only for
     newly added recipes.


================================================================
  LAUNCHER SETTINGS
================================================================
Open Settings by clicking the gear icon on the launcher window.
Settings are organized into three tabs:

TOOLS TAB:
  - Enable/disable individual tools (disabled tools are hidden
    from the launcher and their hotkeys are unbound)
  - Customize the hotkey for each tool
  - Set the launcher toggle hotkey

GRID LAYOUT TAB:
  - Rows and columns for the tile grid
  - Layout type: grid or list view
  - UI scale slider (0.75x to 3.0x)

LANGUAGE TAB:
  Select from 12 languages: English, Deutsch, Francais,
  Espanol, Portugues, Italiano, Nederlands, Polski, Russkij,
  Zhongwen, Nihongo, Hangugeo.

ADDITIONAL SETTINGS:
  - Opacity slider: adjust window transparency
  - Auto-hide: launcher hides when a tool is active

WINDOW POSITIONS:
  Each tool remembers its last window position and size.
  Drag any tool window to reposition it. The position is saved
  automatically for the next launch.

SETTINGS FILE:
  All settings are stored in skill_launcher_settings.json.
  You can edit this file manually, but prefer the Settings panel.


================================================================
  TROUBLESHOOTING
================================================================
App does not launch:
  If you installed via the installer, try running it as
  administrator. If running from source, make sure Python 3.10+
  is installed and run INSTALL_AND_LAUNCH.bat.

Hotkeys not working:
  Check that no other program is capturing the same key
  combination (Discord overlay, OBS, etc.). Try rebinding to
  a different key in Settings.

Tool window does not appear:
  The tool may have launched off-screen. Delete the settings
  file (skill_launcher_settings.json) to reset all window
  positions, then relaunch.

API timeout or no data:
  Check your internet connection. The APIs (erkul.games,
  uexcorp.space, scmdb.net) must be reachable. If one is down,
  that specific tool will show an error but others will work.

Stale data after a game patch:
  Data caches refresh automatically based on their TTL. To
  force an immediate refresh, delete the cache files in the
  skills/ subdirectories (files starting with a dot, e.g.,
  .erkul_cache.json, .uex_cache.json, .scmdb_cache.json).

Crash dialog appears:
  If a tool crashes, SC Toolbox shows a dialog with the crash
  log. Copy the log contents and report the issue on Discord.

Reporting bugs:
  Join the Discord and describe the issue. Include your Windows
  version and any error messages from the crash dialog or log
  files in the logs/ directory.


================================================================
  CREDITS & DATA SOURCES
================================================================
erkul.games      -- DPS calculator data, weapon stats, loadouts
                    Support: patreon.com/erkul
uexcorp.space    -- Market prices, trade routes, ship data,
                    mining equipment, component prices
scmdb.net        -- Mission database, crafting blueprints,
                    mining resource locations
fleetyards.net   -- Ship hardpoint data
sc-cargo.space   -- Cargo grid layouts and container data

Discord: https://discord.gg/D3hqGU5hNt
GitHub:  https://github.com/ScPlaceholder/SC-Toolbox-Beta-V1.2


================================================================
  VERSION HISTORY
================================================================
v1.2.0 -- March 2026
  New:
    - Standalone Windows installer (no Python required)
    - Craft Database tool (Shift+7) for Fabricator blueprints
    - Crash detection with error dialog for all tools
    - Update checker with GitHub release link
    - App icon (desktop shortcut, start menu, installer)
    - 12-language UI translation support
  Improved:
    - UI scale slider (0.75x to 3.0x)
    - Opacity slider for window transparency
    - Auto-hide launcher when tools are active
    - Grid layout customization (rows, columns, list mode)
    - DPS Calculator: UEX market price bubbles on components
    - Settings reorganized into tabbed panel

v1.0.0 -- March 2026 -- Initial Release
  Included tools:
    - DPS Calculator (erkul.games + fleetyards.net)
    - Cargo Loader (sc-cargo.space)
    - Mission Database (scmdb.net)
    - Mining Loadout (uexcorp.space)
    - Item Finder (uexcorp.space)
    - Trade Hub (uexcorp.space)
  Features:
    - Unified launcher with tile grid
    - Global hotkeys via pynput
    - Customizable keybinds and window positions
    - Local data caching for performance
    - Always-on-top overlay windows
