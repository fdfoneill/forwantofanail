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
    │   ├── models.py             # World + runtime DB models
    │   ├── initialize_db.py      # Reset/load scenario data from CSV
    │   ├── migrate_runtime_tables.py
    │   └── game_state.py
    ├── mechanics/
    │   ├── movement.py           # Adjacency + movement rules
    │   ├── supply.py             # Supply capacity + consumption
    │   └── time.py               # Watch progression
    ├── data/
    │   └── *.csv                 # Scenario source data
    └── web/
        └── static/
            ├── dev_dashboard.html
            ├── player_dashboard.html
            └── icons/strongholds/
```

# Getting Started

## 1) Create environment

```bash
conda env create -f environment.yml
conda activate forwantofanail
```

## 2) Initialize/reset database (fresh scenario)

Run this when starting a new game state from CSVs or after schema changes:

```bash
python -m forwantofanail.core.initialize_db --reset
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

## 5) Optional admin token for time controls

`POST /v1/admin/time/advance` supports optional header `X-Admin-Token`.

If `DEV_ADMIN_TOKEN` is set in your shell, this endpoint requires that exact header value.
The dev dashboard includes an Admin Token field for this.

## API notes (current implementation)

* `GET /v1/commanders` returns commander names for dashboard login selection.
* `GET /v1/me/roads/border?cells=...` returns adjacent off-environs road cells for player-map border road stubs.
* Actions support queueing: multiple `queued` actions per commander, one `in_progress`.
* Movement does not start or complete during watch `0` (Night).
* Supply is consumed once daily at the transition into watch `0` (Night).

# Data Structure

Table: armies
- army_id INT PRIMARY KEY
- location_id CHAR(15) FOREIGN KEY REFERENCES locations(location_id)
- army_name VARCHAR(100)
- army_faction VARCHAR(100)
- commander_id INT FOREIGN KEY REFERENCES commanders(commander_id)
- army_supply INT
- army_morale INT
- is_embarked BOOL
- is_garrison BOOL
- noncombattant_count INT

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
- foraged_this_season BOOL

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

# Turn Structure and Movement
Each in-game day is divided into five Watches: Matin, Prime, Noon, Vesper, and Night. In the current implementation, movement actions progress only during watches 1-4; watch 0 (Night) is a rest watch where movement does not start or complete.

The LOCATIONS table divides the game map into a collection of discrete locations. This can be visualized as overlaying a tiling of hexagonal cells onto the region. The LOCATION_ID field contains h3 indices, which can be used to determine adjacency between cells. The h3 values are only used for graph connectivity; the scale is set at 1 league per cell. 

When moving between two locations where IS_ROAD==TRUE ("on-road"), an army can move 1 league (1 cell) per Watch. Off-road, an army can move 1 league every other watch (half-speed). Wagons cannot move off-road at all.

Whenever an army enters a new cell, a record is added to the MOVEMENTS table, recording the army_id, location_id of the cell it entered, date, and watch (as INT where Night=0, Matin=1, Prime=2, Noon=3, Vesper=4).

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
