# For Want of a Nail
A game of armies, letters, sieges, and hunger

# Concept

When the fastest means of communication is a man on a horse, victory is a matter of being in the right place at the right time. The mightiest army does no good if it is a hundred miles from the battle. Nor can ten thousand soldiers win a war if they arrive to the fight half-starved.

The Copper Coast is a land of clashing faiths and marching boots, mountains and forests alive with the ring of steel on steel and the thunder of hoofbeats. Four great powers collide: Royal Delisgar, ancient and decadent; the Principality of Allakia, its rebellious offshoot; the Boonan Free State, a loose alliance of convenience; and the Sultanate of Dinn, a rising star whose borders advance year by year.

In For Want of a Nail, you take command of one of these factions, leading your army from the front. But this game is not won through clever tactics on the battlefield. You have more pressing concerns. First and foremost, how will you keep your army fed? Every soldier must eat, and your wagons grow lighter with every day that passes. Second, where are your foes? All you know is what information you recieve from scouts and letters, reports that may be weeks out of date by the time they arrive. You must do what you can with this fuzzy picture, splitting your army to cover more ground or driving forward in one decisive thrust as you see fit. 

# Repository Structure

```
forwantofanail/
├── environment.yml
├── README.md
└── forwantofanail/
    ├── api/
    │   ├── app.py                # FastAPI app + dashboard/static routes
    │   ├── design_doc.md
    │   ├── routes.py             # REST endpoints
    │   └── schemas.py            # Request schemas
    ├── core/
    │   ├── database.py           # SQLAlchemy engine/session helpers
    │   ├── scenario.py           # External scenario resolver and binding
    │   ├── models.py             # World + runtime DB models
    │   ├── initialize_db.py      # Reset/load scenario data from CSV
    │   ├── migrate_runtime_tables.py
    │   └── game_state.py
    ├── mechanics/
    │   ├── movement.py           # Adjacency + movement rules
    │   ├── supply.py             # Supply capacity + consumption
    │   └── time.py               # Watch progression
    └── web/
        └── static/
            ├── dev_dashboard.html
            ├── player_dashboard.html
            ├── terrain/          # Engine-owned terrain textures
            └── icons/
                ├── strongholds/
                ├── armies/
                └── map_views/
```

# Getting Started

## 1) Create environment

```bash
conda env create -f environment.yml
conda activate forwantofanail
```

## 2) Apply schema and initialize/reset database

Set `SCENARIO_DIR` to the absolute path of an external scenario package. There is no repository-data fallback. Validate it before use:

```bash
export SCENARIO_DIR=/absolute/path/to/copper-coast
python -m forwantofanail.core.scenario validate
```

Application startup never applies DDL. Apply the Alembic schema first:

```bash
alembic upgrade head
```

Run this when starting a new game state from CSVs or after schema changes:

```bash
python -m forwantofanail.core.initialize_db --reset
```

Reset validates and reads the configured package directly. It never copies assets into `web/static` or writes into the scenario. The scenario map is served from the allowlisted `display_map`; portraits are served from `portraits_dir`.

The convenient local working package may live at the ignored `forwantofanail/data/` path. Create a validated, deterministic versioned backup outside the repository with:

```bash
export SCENARIO_ARCHIVE_DIR=/absolute/path/to/scenario-archives
python scripts/export_scenario.py --label release-1
```

Omit `--label` for a name containing the manifest version and UTC timestamp. The command refuses overwrites, excludes hidden metadata, normalizes archive ordering and timestamps, and writes a matching `.sha256` file.

This reset also auto-generates one garrison army for every stronghold based on its type.
It also creates the provisional authoritative history snapshot for tick 0. The scenario manifest
references `history_export.json`, which owns the georeferenced basemap and faction colors used by
historical exports. Its georeferenced basemap remains external scenario data.
The player map uses the manifest's `stronghold_points` shapefile and that GeoTIFF to position
globally known historical stronghold hotspots. The point layer must include `.shp`, `.shx`, `.dbf`,
and `.prj` components and exactly one `GRID_ID` point for every scenario stronghold. Immutable
faction/type metadata still comes from `strongholds.csv`; optional card prose belongs in its
`historical_gloss` column and is omitted from the card when blank.

Army-management suggestions live at the manifest's `army_management_templates` path.

That file is keyed by faction name and supports:

* `commander_titles`: random title suggestions for `NEW ARMY`
* `commander_names`: random unused commander-name suggestions
* `army_names`: random unused army-name suggestions

If all configured names in a field have already been used, the modal leaves that field blank.

To adopt an ongoing database after applying the scenario-binding migration, validate its immutable identities and bind it without resetting:

```bash
python -m forwantofanail.core.scenario bind-existing
```

If you want to keep existing scenario/world rows and only ensure runtime tables exist:

```bash
python -m forwantofanail.core.migrate_runtime_tables
```

## 3) Start dev API server

```bash
uvicorn forwantofanail.api.app:app --reload
```

## 4) Open local tools

* Interactive API docs: `http://127.0.0.1:8000/docs`
* Dev dashboard: `http://127.0.0.1:8000/dev/dashboard`
* Player dashboard: `http://127.0.0.1:8000/player/dashboard`

## 5) Configure authentication

Administrative endpoints require `X-Admin-Token` matching `ADMIN_TOKEN`. Player claims require `GAME_PASSWORD`; session tokens are stored hashed and browser sessions use an HttpOnly cookie.

```bash
export ADMIN_TOKEN="replace-with-a-long-random-secret"
export GAME_PASSWORD="shared-player-password"
export SESSION_SECRET="replace-with-an-independent-random-secret"
```

Production additionally requires PostgreSQL, `APP_ENV=production`, and the canonical `PUBLIC_ORIGIN` (for example `https://game.example.com`). The server refuses to start production with SQLite, missing secrets, or interactive API documentation enabled.

## API notes (current implementation)

* `GET /v1/auth/commanders` returns claimable commander summaries; `POST /v1/auth/claim` requires the shared game password.
* `GET /v1/me/brief` returns a labeled, plain-text snapshot of the authenticated army's current condition, orders, attention items, and viewer-filtered local situation.
* `GET /v1/me/navigation/route?origin=current&destination=sh_<id>&allow_off_road=false` returns a diegetic strategic route summary, semantic legs, travel totals, and an initial compass instruction without exposing H3 cells. A stronghold reference may replace `current`; off-road planning uses the current army's mobility profile but deliberately ignores live remote intelligence.
* `GET /v1/me/roads/border` derives adjacent off-environs road cells from the authenticated army's visibility.
* Staging validation accepts a contiguous `staged_path` rooted at the authenticated army; arbitrary remote origins are rejected.
* `POST /v1/me/actions/plan` replaces active queue with either forage, a staged march path, or halt (empty march path).
* `GET /v1/me/orders/standing` and `POST /v1/me/orders/standing/follow-road` manage standing-order state.
* `GET /v1/me/army-management` returns the active army plus same-cell same-faction armies/garrison for the army-management modal.
* `POST /v1/me/army-management/apply` atomically applies same-hex army reorganization, including detachment transfers, supply transfers, renames, commander swaps, and new-army creation.
* `GET /v1/me/alerts` provides cursor pagination over durable per-commander alert receipts. Delivery is acknowledged after rendering and alerts are marked read when opened.
* Mutating order, message, management, claim, and time-control calls require `Idempotency-Key`.
* `follow-road` standing order cannot be enabled while the army is holding (no active action).
* Actions support queueing: multiple `queued` actions per commander, one `in_progress`.
* Movement does not start during watch `0` (Night), but in-progress movement can complete at Night.
* Supply is consumed once daily at the transition into watch `0` (Night).
* Siege orders start at watch execution; replacing an order does not lift an effective siege until an incompatible order actually takes effect.

## Authoritative history and video export

Every completed watch is archived in `world_snapshots`, while battles, conquests, siege transitions,
and field-army creation/destruction are recorded in `world_history_events`. Snapshot and event writes
share the gameplay transaction. History begins with the first snapshot captured after this feature is
deployed; earlier watches are intentionally not reconstructed.

For an in-progress deployment, apply the migration and capture the current watch as the baseline:

```bash
alembic upgrade head
python -m forwantofanail.history.capture
```

When a game ends before another watch advances, finalize its last frame explicitly:

```bash
python -m forwantofanail.history.capture --finalize
```

Export finalized watches to PNG and a 2 fps H.264 MP4 (FFmpeg must be installed):

```bash
python -m forwantofanail.history.export
```

The default output is `exports/game-history`, including retained numbered PNGs, `game-history.mp4`,
and `manifest.json`. Use `--no-video` for frames only, `--include-provisional` to include the current
unfinished watch, and `--help` for tick-range, dimensions, frame rate, marker duration, output, and
scenario-config overrides. The exporter only reads the database and rejects missing/non-final ticks
instead of fabricating frames.

## Commander tool facade

Authenticated API sessions can discover the provider-neutral commander tools at
`GET /v1/tools` and invoke one at `POST /v1/tools/{tool_name}`. Mutating tools
require an `Idempotency-Key` header. The facade deliberately returns opaque,
session-bound tactical handles rather than map cell identifiers.

The same registry is available through stateless MCP at `POST /mcp`, using the
same bearer session token. A local MCP host can bridge stdio to a running game:

```bash
FWOAN_API_URL=http://127.0.0.1:8000 \
FWOAN_SESSION_TOKEN='<api-session-token>' \
python -m forwantofanail.agent_tools.stdio_proxy
```

Export the canonical JSON Schema catalog for any other model/tool runtime with:

```bash
python -m forwantofanail.agent_tools.export commander-tools.json
```

## Agent commanders

Agent control is assigned by an administrator from the dev dashboard and is
mutually exclusive with a human commander claim. Each enabled agent receives
one queued heartbeat per watch. Time advancement waits for those heartbeats to
complete or be explicitly skipped.

The scenario-owned strategic atlas is generated during scenario authoring, not
at API startup. After changing locations, roads, or strongholds, regenerate it:

```bash
python -m forwantofanail.agent_runtime.strategic_atlas
```

Review the configured package's `agent_strategic_atlas.json`, set `selected: true`
only on non-city choke-point candidates that should be promoted, rerun the
generator to build their corridors, and commit the artifact. CI/deployment can
verify that it is current without rewriting it:

```bash
python -m forwantofanail.agent_runtime.strategic_atlas --check
```

Every agent heartbeat receives a compact static atlas. The detailed
`fwoan_get_strategic_overview` tool exposes the same scenario-static material
without current remote control, army, garrison, or siege information. New
assignments must establish a structured strategic plan; plans and passive-watch
review state are persisted alongside revisioned scratchpad memory.

Configure either or both provider profiles:

```bash
export OPENAI_API_KEY='...'
export OPENAI_AGENT_MODEL='your-tool-capable-model'

export OLLAMA_BASE_URL='http://127.0.0.1:11434'
export OLLAMA_AGENT_MODEL='your-installed-tool-capable-model'
```

Run the API, then start one or more independent workers:

```bash
python -m forwantofanail.agent_runtime.worker --concurrency 4
```

Optional, non-CI provider evaluations can review a completed transcript with
either a configured OpenAI or Ollama profile. The report includes six rubric
scores and source/evaluator token usage:

```bash
python -m forwantofanail.agent_runtime.evaluate_strategy --run-id 42 --profile openai_default
```

The scenario owns `agent_rules.md`, `agent_commander_dossiers.json`, and
`agent_profiles.json`. Original commanders use authored dossiers; subcommanders
receive deterministic dossiers when they are created. The database stores
versioned scratchpads and append-only visible run transcripts. Hidden provider
reasoning is neither stored nor displayed.

# Data Structure

Table: armies
- army_id INT PRIMARY KEY
- location_id CHAR(15) FOREIGN KEY REFERENCES locations(location_id)
- army_name VARCHAR(100)
- army_faction VARCHAR(100)
- commander_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- garrison_stronghold_id INT NULL FOREIGN KEY REFERENCES strongholds(stronghold_id)
- army_supply INT
- army_morale INT
- army_resting_morale INT
- is_embarked BOOL
- is_garrison BOOL
- noncombattant_percent FLOAT

Table: detachments 
- detachment_id INT PRIMARY KEY
- detachment_name VARCHAR(100)
- army_id INT FOREIGN KEY REFERENCES armies(army_id)
- is_heavy BOOL
- is_cavalry BOOL
- wagon_count INT
- warrior_count INT
- is_mercenary BOOL

Table: detachment_specials 
- detachment_id INT FOREIGN KEY REFERENCES detachments(detachment_id)
- special_name VARCHAR(100)

Table: commanders
- commander_id INT PRIMARY KEY
- commander_name VARCHAR(100)
- commander_age INT
- commander_title VARCHAR(100)

Table: commander_traits
- commander_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- trait_name VARCHAR(100)

Table: locations
- location_id CHAR(15) PRIMARY KEY
- is_road BOOL
- region VARCHAR(100)
- terrain_id INT FOREIGN KEY REFERENCES terrain_types(terrain_id)
- settlement INT
- foraged_this_season INT (0–3 seasonal depletion counter)

Table: terrain_types
- terrain_id INT PRIMARY KEY
- terrain_name VARCHAR(100)
- speed_multiplier DOUBLE
- scout_multiplier DOUBLE
- is_water BOOL

Table: strongholds
- stronghold_id INT PRIMARY KEY
- location_id CHAR(15) FOREIGN KEY REFERENCES locations(location_Id)
- stronghold_name VARCHAR(100)
- stronghold_type VARCHAR(30)
- control VARCHAR(30)
- stronghold_threshold INT

Table: movements
- army_id INT FOREIGN KEY REFERENCES armies(army_id)
- location_id CHAR(15) FOREIGN KEY REFERENCES locations(location_id)
- date DATE
- watch INT

Table: game_clock
- singleton_id INT PRIMARY KEY (always 1)
- day INT
- watch INT

Table: auth_tokens
- token VARCHAR(128) PRIMARY KEY
- commander_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- created_at DATETIME

Table: actions
- action_id INT PRIMARY KEY
- commander_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- kind VARCHAR(40)
- state VARCHAR(30)
- parameters_json TEXT
- accepted_at DATETIME
- started_day INT NULL
- started_watch INT NULL
- eta_day INT NULL
- eta_watch INT NULL

Table: messages
- message_id INT PRIMARY KEY
- sender_commander_id INT NULL FOREIGN KEY REFERENCES commanders(commander_id)
- sender_stronghold_id INT NULL FOREIGN KEY REFERENCES strongholds(stronghold_id)
- sender_name VARCHAR(100)
- recipient_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- content TEXT
- priority VARCHAR(20)
- sent_day INT
- sent_watch INT
- delivery_day INT
- delivery_watch INT
- status VARCHAR(20)  # in_transit | received | lost
- is_read BOOL
- created_at DATETIME

Table: standing_orders
- commander_id INT PRIMARY KEY FOREIGN KEY REFERENCES commanders(commander_id)
- follow_road_enabled BOOL
- last_report TEXT NULL
- last_report_day INT NULL
- last_report_watch INT NULL
- updated_at DATETIME

Table: alerts
- alert_id INT PRIMARY KEY
- recipient_commander_id INT NULL FOREIGN KEY REFERENCES commanders(commander_id)   # NULL => global/all players
- alert_type VARCHAR(20)   # world event | action | report | violence
- signal_kind VARCHAR(20)  # event | state
- category VARCHAR(40)
- importance VARCHAR(20)
- message TEXT
- payload_json TEXT
- created_day INT
- created_watch INT
- delivered_day INT
- delivered_watch INT
- is_read BOOL
- created_at DATETIME

# Turn Structure and Movement
Each in-game day is divided into five Watches: Matin, Prime, Sixbell, Vesper, and Night. In the current implementation, movement actions do not start during watch 0 (Night), but an in-progress move may complete at Night if its ETA is reached.

The LOCATIONS table divides the game map into a collection of discrete locations. This can be visualized as overlaying a tiling of hexagonal cells onto the region. The LOCATION_ID field contains h3 indices, which can be used to determine adjacency between cells. The h3 values are only used for graph connectivity; the scale is set at 1 league per cell. 

When moving between two locations where IS_ROAD==TRUE ("on-road"), an army can move 1 league (1 cell) per Watch. Off-road, an army can move 1 league every other watch (half-speed). Wagons cannot move off-road at all.

Whenever an army enters a new cell, a record is added to the MOVEMENTS table, recording the army_id, location_id of the cell it entered, date, and watch (as INT where Night=0, Matin=1, Prime=2, Sixbell=3, Vesper=4).

# Supply

Supply consumption is applied once per day at the transition into watch `0` (Night).

Daily consumption:
- infantry: `1` per unit
- noncombatants: `1` per unit
- cavalry: `10` per unit
- wagons: `10` per unit

Carrying capacity:
- infantry: `15` per unit
- noncombatants: `15` per unit
- cavalry: `75` per unit
- wagons: `1000` per unit

# Scouting

During the day, an army's scouts see everything in its cell, adjacent cells, and the next ring of cells as well (equivalent to h3.grid_disk(army_location_id, 2)). If the army has any cavalry detachments, this range is doubled (h3.grid_disk(army_location_id, 4)).

Scout reports contain accurate summaries of terrain, roads, water features, strongholds, and armies (friend or foe) within range.

# Terrain

Armies cannot enter open water unless they are embarked on ships (IS_EMBARKED=TRUE). If a cell has "river" terrain but also IS_ROAD, then there is a bridge and armies can move through at on-road speeds. Otherwise, all-cavalry armies can ford rivers at normal speed, but if an army contains any infantry it must take a full day to ford the river. Wagons cannot enter river cells at all.

Some terrain types reduce scouting distance to a fraction of the normal value (stored in the SCOUT_MULTIPLIER field). Other terrain types reduce the speed of an army traveling off-road (stored in the SPEED_MULTIPLIER field). 
