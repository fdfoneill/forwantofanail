# For Want of a Nail
A game of armies, letters, sieges, and hunger

# Concept

When the fastest means of communication is a man on a horse, victory is a matter of being in the right place at the right time. The mightiest army does no good if it is a hundred miles from the battle. Nor can ten thousand soldiers win a war if they arrive to the fight half-starved.

The Copper Coast is a land of clashing faiths and marching boots, mountains and forests alive with the ring of steel on steel and the thunder of hoofbeats. Four great powers collide: Royal Delisgar, ancient and decadent; the Principality of Allakia, its rebellious offshoot; the Boonan Free State, a loose alliance of convenience; and the Sultanate of Dinn, a rising star whose borders advance year by year.

In For Want of a Nail, you take command of one of these factions, leading your army from the front. But this game is not won through clever tactics on the battlefield. You have more pressing concerns. First and foremost, how will you keep your army fed? Every soldier must eat, and your wagons grow lighter with every day that passes. Second, where are your foes? All you know is what information you recieve from scouts and letters, reports that may be weeks out of date by the time they arrive. You must do what you can with this fuzzy picture, splitting your army to cover more ground or driving forward in one decisive thrust as you see fit. 

# Repository Structure

```
forwantofanail/
├── core/
│   ├── __init__.py
│   ├── models.py          # SQLAlchemy models (armies, strongholds, etc.)
│   ├── database.py        # DB connection and session management
│   └── game_state.py      # Core game state queries and updates
├── mechanics/
│   ├── __init__.py
│   ├── movement.py        # Movement calculations, pathfinding
│   ├── supply.py          # Supply consumption, foraging
│   ├── combat.py          # Battle resolution (can be stubbed initially)
│   └── time.py            # Watch progression, time management
├── communication/
│   ├── __init__.py
│   ├── messages.py        # Message creation, delivery, routing
│   ├── orders.py          # Parsing and validating orders
│   └── reports.py         # Generating scout reports, status updates
├── commanders/
│   ├── __init__.py
│   ├── ai_commander.py    # LLM-based commander logic
│   ├── prompts.py         # System prompts for different commander types
│   └── human_interface.py # Utilities for human player interaction
├── api/
│   ├── __init__.py
│   ├── routes.py          # Flask/FastAPI endpoints
│   └── schemas.py         # Request/response schemas
├── web/
│   ├── static/           # CSS, JS, map images
│   └── templates/        # HTML templates
├── data/
│   ├── map_data.json     # Hex grid, terrain, roads
│   └── scenario.json     # Initial faction/army setup
├── tests/
│   └── ...
└── main.py               # Application entry point
```

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
- region VARCHAR(100) FOREIGN KEY REFERENCES strongholds(stronghold_name)
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

# Turn Structure and Movement
Each in-game day is divided into five Watches: Matin, Prime, Noon, Vesper, and Night. Armies normally move and act during the four non-Night watches, though a risk-taking commander can choose to march through the night. 

The LOCATIONS table divides the game map into a collection of discrete locations. This can be visualized as overlaying a tiling of hexagonal cells onto the region. The LOCATION_ID field contains h3 indices, which can be used to determine adjacency between cells. The h3 values are only used for graph connectivity; the scale is set at 1 league per cell. 

When moving between two locations where IS_ROAD==TRUE ("on-road"), an army can move 1 league (1 cell) per Watch. Off-road, an army can move 1 league every other watch (half-speed). Wagons cannot move off-rad at all.

Whenever an army enters a new cell, a record is added to the MOVEMENTS table, recording the army_id, location_id of the cell it entered, date, and watch (as INT where Night=0, Matin=1, Prime=2, Noon=3, Vesper=4).

# Scouting

During the day, an army's scouts see everything in its cell, adjacent cells, and the next ring of cells as well (equivalent to h3.grid_disk(army_location_id, 2)). If the army has any cavalry detachments, this range is doubled (h3.grid_disk(army_location_id, 4)).

Scout reports contain accurate summaries of terrain, roads, water features, strongholds, and armies (friend or foe) within range.

# Terrain

Armies cannot enter open water unless they are embarked on ships (IS_EMBARKED=TRUE). If a cell has "river" terrain but also IS_ROAD, then there is a bridge and armies can move through at on-road speeds. Otherwise, all-cavalry armies can ford rivers at normal speed, but if an army contains any infantry it must take a full day to ford the river. Wagons cannot enter river cells at all.

Some terrain types reduce scouting distance to a fraction of the normal value (stored in the SCOUT_MULTIPLIER field). Other terrain types reduce the speed of an army traveling off-road (stored in the SPEED_MULTIPLIER field). 