# For Want of a Nail – Minimal REST API Design (v0.1)

## Purpose

This document defines a minimal RESTful API surface for the Copper Coast proof-of-concept.

Design goals:

* A player sees **only** what their in-world commander could plausibly know.
* The API is friendly to both:

  * Human web clients
  * LLM-based AI commanders
* The interface is minimal, stable, and easy to evolve.

This version assumes:

* Single army per commander
* One in-progress action at a time (with queued follow-on actions allowed)
* No combat resolution yet
* Combined “dashboard” view endpoint

---

# 1. Core Design Principles

## 1.1 Commander-Scoped Access

Every authenticated request is scoped to exactly one `commander_id`.

Clients:

* Never submit `commander_id` in reads.
* Never access global army lists.
* Never see ground-truth positions of enemy forces.

All responses are derived from:

```
auth token → commander → army → permitted state
```

---

## 1.2 Combined View Model

The primary read endpoint is:

```
GET /v1/me/view
```

This returns everything the commander is allowed to know:

* Current time (day + watch)
* Their army state
* Local environs (H3 disk)
* Delivered messages
* Current in-progress action

This endpoint is designed to:

* Power the human dashboard
* Serve as the full context input to AI commanders

---

# 2. Authentication

Minimal token-based authentication.

## Login

```
POST /v1/auth/login
```

**Request**

```json
{
  "commander_name": "Queen Sofonisba"
}
```

**Response**

```json
{
  "token": "jwt-or-session-token",
  "commander": {
    "id": "cmd_001",
    "name": "Queen Sofonisba"
  }
}
```

All subsequent requests require:

```
Authorization: Bearer <token>
```

---

# 3. Combined View Endpoint

## Get Commander View

```
GET /v1/me/view
```

### Response

```json
{
  "time": {
    "day": 14,
    "watch": 2,
    "watch_label": "midday"
  },

  "army": {
    "army_id": "army_9",
    "name": "Royal Host",
    "location": {
      "h3": "8a2a1072b59ffff"
    },

    "composition": {
      "detachments": [
        {
          "id": "det_1",
          "name": "Foot",
          "warriors": 1800,
          "wagons": 60,
          "is_cavalry": false
        },
        {
          "id": "det_2",
          "name": "Knights",
          "warriors": 220,
          "wagons": 20,
          "is_cavalry": true
        }
      ],
      "noncombatants": 900
    },

    "supply": {
      "current": 520,
      "capacity": 800,
      "days_estimate": 6.2
    },

    "status_flags": ["marching"]
  },

  "environs": {
    "center_h3": "8a2a1072b59ffff",
    "radius": 2,
    "cells": [
      {
        "h3": "8a2a1072b597fff",
        "terrain_type": "scrub",
        "has_road": true,
        "stronghold": null,
        "observations": []
      },
      {
        "h3": "8a2a1072b58ffff",
        "terrain_type": "farmland",
        "has_road": false,
        "stronghold": {
          "id": "sh_77",
          "name": "Kumba",
          "type": "city",
          "faction": "Allakia"
        },
        "observations": [
          {
            "descriptor": "distant campfires at dusk",
            "confidence": 0.4,
            "as_of": { "day": 14, "watch": 1 }
          }
        ]
      }
    ]
  },

  "messages": {
    "unread_count": 3,
    "latest": [
      {
        "id": "msg_501",
        "from": { "name": "Baron Soman" },
        "delivered_watch": { "day": 14, "watch": 1 },
        "snippet": "Unexpected resistance at Jumba...",
        "is_read": false
      }
    ]
  },

  "current_action": {
    "action_id": "act_880",
    "kind": "move",
    "state": "in_progress",
    "started": { "day": 14, "watch": 1 },
    "eta": { "day": 15, "watch": 0 },
    "summary": "Marching toward Ormo via road"
  }
}
```

---

## Key Constraints

* No enemy army IDs are exposed.
* Observations are diegetic, not omniscient.
* Only delivered messages appear.
* Only the commander's own army appears.

---

# 4. Actions

An action represents the commander's chosen intent.

Only one active action is allowed at a time in v0.1.

---

## Create Action

```
POST /v1/me/actions
```

### Request

```json
{
  "kind": "move",
  "destination_h3": "8a2a1072b2fffff"
}
```

### Response

```json
{
  "action_id": "act_881",
  "kind": "move",
  "state": "queued",
  "accepted_at": "2026-02-26T21:52:11Z"
}
```

---

## Get Current Action

```
GET /v1/me/actions/current
```

Returns either:

```json
null
```

or:

```json
{
  "action_id": "act_881",
  "kind": "move",
  "state": "in_progress",
  "eta": { "day": 15, "watch": 0 }
}
```

---

## Cancel Action (Optional in v0.1)

```
POST /v1/me/actions/{action_id}/cancel
```

Only allowed if:

* Action state permits cancellation.

---

# 5. Messages

Messages are diegetic letters moving across the landscape.

They are not instant.

---

## Send Message

```
POST /v1/me/messages
```

### Request

```json
{
  "recipient_id": "cmd_200",
  "content": "Hold Ormo and send scouts east toward the river.",
  "priority": "normal"
}
```

### Response

```json
{
  "message_id": "msg_900",
  "sent_watch": { "day": 14, "watch": 2 },
  "estimated_delivery_watch": { "day": 16, "watch": 0 },
  "status": "in_transit"
}
```

---

## List Delivered Messages

```
GET /v1/me/messages?unread_only=true
```

Returns only messages:

* Where `recipient_id == me`
* Where `delivery_watch <= current_watch`

---

## Get Full Message

```
GET /v1/me/messages/{message_id}
```

Marks message as read.

---

# 6. Time

Players may read the current time but not advance it directly (in multiplayer mode).

## Get Time

```
GET /v1/time
```

Response:

```json
{
  "day": 14,
  "watch": 2,
  "watch_label": "midday"
}
```

Time advancement is handled internally by:

* Game loop
* Admin/referee interface
* Or single-player mode controller

---

# 7. Minimal Data Model Alignment

The API maps directly to:

* `armies`
* `detachments`
* `commanders`
* `messages`
* `movement_history`
* `landscape`
* `strongholds`

As defined in the project structure .

No additional tables are required for v0.1 beyond possibly:

* `actions`

---

# 8. Minimal Endpoint Summary

| Method | Endpoint                   | Purpose                   |
| ------ | -------------------------- | ------------------------- |
| POST   | /v1/auth/login             | Authenticate as commander |
| GET    | /v1/me/view                | Combined dashboard        |
| POST   | /v1/me/actions             | Create new action         |
| GET    | /v1/me/actions/current     | View active action        |
| POST   | /v1/me/actions/{id}/cancel | Cancel action             |
| POST   | /v1/me/messages            | Send letter               |
| GET    | /v1/me/messages            | List delivered letters    |
| GET    | /v1/me/messages/{id}       | Read letter               |
| GET    | /v1/correspondents         | Valid message recipients  |
| GET    | /v1/time                   | Get current day/watch     |

---

# 9. v0.1.2 Implementation Notes (Current Codebase)

The API now persists runtime state in SQLAlchemy tables while keeping the existing normalized world schema intact.

## 9.1 Persisted Runtime Tables Added

The codebase now includes:

* `game_clock` (single-row world time state)
* `auth_tokens` (bearer token -> commander mapping)
* `actions` (queued/in-progress/cancelled action intents)
* `messages` (sender/recipient/content/read status + delivery watch)

## 9.2 Endpoint Persistence Status

The following endpoints are DB-backed (no in-memory fallback):

* `POST /v1/auth/login`
* `GET /v1/commanders`
* `GET /v1/time`
* `POST /v1/admin/time/advance` (dev/admin control)
* `GET /v1/me/view`
* `GET /v1/me/roads/border`
* `POST /v1/me/actions`
* `GET /v1/me/actions/current`
* `POST /v1/me/actions/{id}/cancel`
* `POST /v1/me/messages`
* `GET /v1/me/messages`
* `GET /v1/me/messages/{id}`
* `GET /v1/correspondents`

## 9.3 Contract Deltas from v0.1

Current API behavior differs from the original examples in these ways:

* IDs are `cmd_<int>`, `army_<int>`, `det_<int>`, `sh_<int>`, `act_<int>`, `msg_<int>`.
* `army.supply` includes `current`, `capacity`, `daily_consumption`, and `days_estimate`.
* `current_action.eta` is populated when an action is `in_progress`; queued actions may have `eta = null`.
* Messages are persisted and delivery-gated by `(delivery_day, delivery_watch)`, but currently created as immediately delivered.
* Environs radius is computed server-side: `2` normally, `4` when any detachment is cavalry.
* Action creation accepts `{\"kind\":\"move\", \"destination_h3\": \"...\"}`.
* Actions are queued FIFO per commander (multiple queued, one in-progress).
* Movement actions do not start or resolve in watch `0` (Night), and Night does not count toward movement ETA progress.

## 9.4 Validation Added for Move Actions

`POST /v1/me/actions` with `kind=\"move\"` validates only that `destination_h3` exists. Adjacency + movement constraints (terrain, wagons, river/open-water rules) are validated at action start/resolution time via movement mechanics.
