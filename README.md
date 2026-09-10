# Afker Atem Platform (افكر اطعام)

Phase 1: two public forms — "I have food to give" and "Register a center" —
writing directly into a Google Sheet. No matching automation yet (that's Phase 2).

## Local setup

1. `pip install -r requirements.txt`
2. Set environment variables (see below)
3. `python app.py` — runs on http://localhost:5000

## Environment variables

| Variable | Description |
|---|---|
| `SHEET_ID` | The Google Sheet ID (from its URL, between `/d/` and `/edit`) |
| `GOOGLE_CREDENTIALS_JSON` | The **full contents** of your service account JSON key file, pasted as a single-line string |
| `SECRET_KEY` | Any random string, used for flash messages |
| `PORT` | Set automatically by Railway, defaults to 5000 locally |

The sheet must be shared with the service account's email (found inside the
JSON key file as `client_email`) with **Editor** access.

## Sheet structure

The app auto-creates these two tabs on first run if they don't exist:

**Givers**
Timestamp, Name, Phone, Area, Food Type, Meal Category, Quantity, Ready Time,
Recurring?, Pickup Available?, Status

**Centers**
Timestamp, Center Name, Center Type, Area, Contact Name, Contact Phone,
Ritual Schedule, Capacity, Status

New center registrations get `Status = Applied` — flip to `Verified` manually
in the sheet once you've confirmed the center is real.

## Deploying to Railway

1. Push this folder to a new GitHub repo
2. In Railway: New Project → Deploy from GitHub repo → select it
3. In the Railway project's Variables tab, add `SHEET_ID`, `GOOGLE_CREDENTIALS_JSON`,
   and `SECRET_KEY` (same values as local setup)
4. Railway will detect the `Procfile` and deploy automatically
5. Once deployed, Railway gives you a public URL — that's the live site

## What's NOT built yet (Phase 2+)

- Matching logic (area + timing → suggested center)
- 24h reminder notifications
- Admin review dashboard for unmatched offers / center verification
- WhatsApp confirmation loop
