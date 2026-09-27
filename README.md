# Mama Tuhibbun (مما تحبون)

Connects people who want to give food with nearby places that can receive
it, backed by a Google Sheet — no database. Two kinds of places:

- **Centers** — orphanages, elderly homes, and other places with a known,
  limited number of beneficiaries.
- **Ma'edet Rahman** — open community meal tables anyone can walk up to.

A giver browses by governorate + area (optionally filtered by date needed,
quantity, and food type), picks a place, and the match is finalized
immediately (no separate admin approval step). If no place is registered
yet in someone's area, they can join a waitlist and get emailed once one
signs up there.

Places with a stated capacity per slot get concrete dated need-slots
auto-generated from their recurring days/meal-slot pattern (next 3 weeks,
regenerated as needed) — givers commit against a specific date instead of
an open-ended offer. Centers must cover a slot's full remaining need in
one commitment; Ma'edet Rahman tables accept partial contributions.

After a match, both sides can confirm the meal actually happened (the
giver from their activity page once logged in, the place via a one-time
emailed link) — once both confirm, the giver's asked to share a short
story, shown on the homepage. If a giver never confirms after the date
passes, or a place reports a no-show, that phone number is auto-blocked
from committing to new meals for 30 days.

All enum-style values (governorate, area, center type, status, yes/no,
etc.) are stored as their actual Egyptian Arabic text directly in the
sheet, not as an English key translated for display — so the raw sheet is
readable to a human on its own. **This means values are matched by exact
string, including in the sheet** — if you already have rows from an older
version of this app that used English values (`Cairo`, `Verified`, ...),
they won't match new filters/lookups until you update them to their
Arabic equivalents (see `AR_LABELS`'s old mapping in git history, or the
option values in the templates) or re-register.

## Local setup

1. `pip install -r requirements.txt`
2. Set environment variables (see below)
3. `python app.py` — runs on http://localhost:5000

## Environment variables

| Variable | Description |
|---|---|
| `SHEET_ID` | The Google Sheet ID (from its URL, between `/d/` and `/edit`) |
| `GOOGLE_CREDENTIALS_JSON` | The **full contents** of your service account JSON key file, pasted as a single-line string |
| `SECRET_KEY` | Any random string, used for sessions and flash messages |
| `RESEND_API_KEY` | Optional. Resend API key, used to email places when they get a match and to email waitlisted people when a place registers in their area. Notifications are skipped (not an error) if unset |
| `FROM_EMAIL` | Optional. The "from" address for those emails, e.g. `Mama Tuhibbun <onboarding@resend.dev>` |
| `PORT` | Set automatically by Railway, defaults to 5000 locally |

The sheet must be shared with the service account's email (found inside the
JSON key file as `client_email`) with **Editor** access. Same account is
also used to upload place photos to Google Drive.

## Sheet structure

The app auto-creates these tabs on first run if they don't exist:

**givers** — one row per secured donation: Timestamp, Giver Type, Name,
Phone, Email, Area, Food Type, Time Preference, Meal Slot, Quantity,
Quantity Unit, Ready Date, Recurring?, Recurring Days, Delivery Contact
Who/Name/Phone, Backup Phone, Status, Matched Center, Matched Center Row,
Slot Row, Delivery Confirmed, Receipt Confirmed, No Show Reported,
Confirm Token, Disclaimer Accepted. Status moves from `مؤمّنة` (secured) to
`اتوصلت` (delivered) once both sides confirm, or to `فشل` (failed) /
`اتلغت` (cancelled) on a no-show or a cancellation.

**centers** — both Centers and Ma'edet Rahman tables live here, `Center
Type` tells them apart: Timestamp, Center Name, Bio, Center Type,
Governorate, Ownership Type, Area, Address, Maps Link, Social Link, Target
Group, Beneficiaries, Staff Members, Total To Feed, Preferred Meal Type,
Submitter Role/Name/Phone, Contact Name/Phone/Email, Ritual Schedule,
Receives Meals, Meal Slots, Days Open, Receives Groceries, Grocery Hours,
Has Capacity Limit, Capacity Per Slot, Photo URLs, Status, Last Matched,
Visits, Disclaimer Accepted.

**need_slots** — auto-generated dated slots per place, derived from its
Days Open/Meal Slots/Capacity Per Slot: Center Row, Center Name, Center
Type, Governorate, Area, Date, Meal Slot, Quantity Needed, Quantity
Secured, Status (`متاحة`/`مكتملة`).

**blocks** — phone numbers currently blocked from committing (missed
confirmation or a reported no-show), auto-expiring after 30 days: Phone,
Blocked Until, Reason, Created At.

**stories** — short reflections shared after a confirmed delivery, shown
on the homepage: Timestamp, Author Type, Author Name, Center Name, Story
Text.

**users** — optional accounts (phone-based login, no password): Phone,
Real Name, Nickname, Email, Created At.

**waitlist** — people notified when a place registers in their area:
Timestamp, Email, Area, Place Type.

A place starts `Status = لسه مش موثّق` (unverified) and flips to `موثّق`
(verified) automatically the first time a giver is matched to it — there's
no manual review step. Registering or giving requires accepting the
in-app disclaimer (`/disclaimer`) first.

## Deploying to Railway

1. Push this folder to a new GitHub repo
2. In Railway: New Project → Deploy from GitHub repo → select it
3. In the Railway project's Variables tab, add `SHEET_ID`,
   `GOOGLE_CREDENTIALS_JSON`, `SECRET_KEY`, and optionally
   `RESEND_API_KEY` / `FROM_EMAIL` (same values as local setup)
4. Railway will detect the `Procfile` and deploy automatically
5. Once deployed, Railway gives you a public URL — that's the live site

## What's NOT built yet

- Reminder notifications ahead of a scheduled drop-off
- Admin dashboard (verification is currently automatic on first match)
- WhatsApp confirmation loop
