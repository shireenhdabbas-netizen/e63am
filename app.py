import os
import json
import random
import requests
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session

import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(days=90)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB cap on uploads

SHEET_ID = os.environ.get("SHEET_ID")
GIVERS_TAB = "givers"
CENTERS_TAB = "centers"
USERS_TAB = "users"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Afker Atem <onboarding@resend.dev>")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheet_client():
    """
    Loads Google service account credentials from the GOOGLE_CREDENTIALS_JSON
    environment variable (the raw contents of the downloaded JSON key file)
    and returns an authorized gspread client.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON environment variable is not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(tab_name, header_row):
    """
    Returns the worksheet for the given tab, creating it with a header row
    if it doesn't exist yet.
    """
    client = get_sheet_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        try:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(header_row))
            ws.append_row(header_row)
        except gspread.exceptions.APIError as e:
            # Another near-simultaneous request already created this tab
            # (e.g. a double form submission) - just use the existing one.
            if "already exists" in str(e):
                ws = spreadsheet.worksheet(tab_name)
            else:
                raise

    # If the tab exists but is empty (e.g. manually created), add headers now
    if not ws.get_all_values():
        ws.append_row(header_row)

    return ws


GIVERS_HEADER = [
    "Timestamp", "Giver Type", "Name", "Phone", "Email", "Governorate", "Area",
    "Area Flexibility", "Additional Areas",
    "Food Type", "Time Preference", "Meal Slot",
    "Quantity Max", "Quantity Confirmed", "Quantity Unit",
    "Ready Date", "Recurring?", "Recurring Days", "Pickup Available?",
    "Delivery Contact Who", "Delivery Contact Name", "Delivery Contact Phone", "Backup Phone",
    "Status", "Matched Center", "Matched Center Row"
]

USERS_HEADER = ["Phone", "Real Name", "Nickname", "Email", "Created At"]

CENTERS_HEADER = [
    "Timestamp", "Center Name", "Bio", "Center Type", "Governorate", "Ownership Type",
    "Area", "Address", "Maps Link", "Social Link",
    "Target Group", "Beneficiaries", "Staff Members", "Total To Feed", "Preferred Meal Type",
    "Submitter Role", "Submitter Name", "Submitter Phone",
    "Contact Name", "Contact Phone", "Ritual Schedule",
    "Receives Meals", "Meal Slots", "Days Open",
    "Receives Groceries", "Grocery Hours",
    "Has Capacity Limit", "Capacity Per Slot",
    "Photo URLs", "Status", "Last Matched", "Visits"
]


def ensure_columns(ws, required_header):
    """
    Makes sure every column in required_header exists in row 1 of the sheet,
    appending any that are missing (for sheets created before a column was
    added to the schema). Returns the current header row after any additions.
    """
    header = ws.row_values(1)
    changed = False
    for col_name in required_header:
        if col_name not in header:
            header.append(col_name)
            ws.update_cell(1, len(header), col_name)
            changed = True
    return header


def col_index(header, col_name):
    """1-based column index for a header name, or None if not present."""
    try:
        return header.index(col_name) + 1
    except ValueError:
        return None


def get_drive_access_token():
    """Reuses the same Google service account already set up for Sheets to get a Drive API access token."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds.token


def upload_image_to_drive(file_storage):
    """
    Uploads one uploaded image file to Google Drive under the app's service
    account, makes it viewable by anyone with the link, and returns a
    direct-view URL usable in an <img> tag. Returns None on failure (a
    photo upload failing shouldn't block the rest of the registration).
    """
    try:
        access_token = get_drive_access_token()
        metadata = {"name": file_storage.filename}
        files = {
            "data": (None, json.dumps(metadata), "application/json"),
            "file": (file_storage.filename, file_storage.stream, file_storage.mimetype),
        }
        resp = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            headers={"Authorization": f"Bearer {access_token}"},
            files=files,
            timeout=30,
        )
        resp.raise_for_status()
        file_id = resp.json()["id"]

        # Make it viewable by anyone with the link (service-account uploads
        # are private by default)
        requests.post(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"role": "reader", "type": "anyone"},
            timeout=15,
        )

        return f"https://drive.google.com/uc?export=view&id={file_id}"
    except Exception as e:
        print(f"Drive image upload failed for {file_storage.filename}: {e}")
        return None


def send_email(to_email, subject, html_body):
    """
    Sends a notification email via Resend. Fails silently (logs to stdout)
    if RESEND_API_KEY isn't set or the request fails - email is a nice-to-have
    notification, not something that should ever break the actual donation flow.
    """
    if not RESEND_API_KEY or not to_email:
        return
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": FROM_EMAIL, "to": [to_email], "subject": subject, "html": html_body},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Email send failed to {to_email}: {e}")


ANONYMOUS_ADJECTIVES = [
    "Kind", "Generous", "Bright", "Gentle", "Warm", "Quiet", "Steady",
    "Hopeful", "Caring", "Humble", "Faithful", "Cheerful", "Patient", "Wise",
]
ANONYMOUS_NOUNS = [
    "Falcon", "Sparrow", "Olive", "Lotus", "River", "Cedar", "Dove",
    "Lantern", "Compass", "Harbor", "Meadow", "Star", "Palm", "Jasmine",
]


def generate_anonymous_nickname():
    """Generates a random display name like 'Kind Falcon 42' for givers who'd rather stay anonymous."""
    return f"{random.choice(ANONYMOUS_ADJECTIVES)} {random.choice(ANONYMOUS_NOUNS)} {random.randint(10, 99)}"


def get_user_by_phone(phone):
    """Returns the user's row as a dict, or None if this phone hasn't signed in before."""
    ws = get_worksheet(USERS_TAB, USERS_HEADER)
    header = ensure_columns(ws, USERS_HEADER)
    phone_i = col_index(header, "Phone")
    all_values = ws.get_all_values()
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) >= phone_i and row[phone_i - 1].strip() == phone.strip():
            return read_row(ws, header, row_num)
    return None


def create_user(phone, real_name, nickname, email):
    ws = get_worksheet(USERS_TAB, USERS_HEADER)
    ws.append_row([phone, real_name, nickname, email, datetime.now().isoformat(timespec="seconds")])


def current_user():
    """Returns the logged-in user's account dict, or None if signed out."""
    phone = session.get("user_phone")
    if not phone:
        return None
    return get_user_by_phone(phone)


def read_row(ws, header, row_number):
    """Returns a dict of {column_name: value} for one sheet row, by header name."""
    values = ws.row_values(row_number)
    return {col: (values[i] if i < len(values) else "") for i, col in enumerate(header)}


def find_candidate_centers(areas):
    """
    Returns every eligible center across the given list of areas, sorted for
    browsing (not auto-picked): Verified centers first, then within each
    group the one matched least recently first (so donations spread out
    rather than always hitting the same center). Unverified centers are
    still included - a giver choosing one becomes its first real "visit"
    (crowd-sourced trust, no admin gate).
    Returns a list of dicts, each the full center row plus its sheet row
    number under the key "_row".
    """
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    all_values = centers_ws.get_all_values()
    if len(all_values) <= 1:
        return []

    area_i = col_index(centers_header, "Area")
    status_i = col_index(centers_header, "Status")

    target_areas = set(a.strip().lower() for a in areas if a.strip())
    candidates = []
    for row_num, row in enumerate(all_values[1:], start=2):
        row_area = row[area_i - 1].strip().lower() if len(row) >= area_i else ""
        if row_area not in target_areas:
            continue
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status == "Rejected":
            continue
        info = {col: (row[i] if i < len(row) else "") for i, col in enumerate(centers_header)}
        info["_row"] = row_num
        candidates.append(info)

    # Verified first; within each group, least-recently-matched first
    # (empty "Last Matched" sorts first, i.e. never-matched centers surface early)
    candidates.sort(key=lambda c: (c.get("Status") != "Verified", c.get("Last Matched", "")))
    return candidates


def apply_confirmed_match(giver_row_number, center_row_number, final_quantity):
    """
    Finalizes a match once the giver has confirmed the actual quantity
    against the center's real need: updates the giver's row to "Matched"
    with the confirmed quantity, and updates the center's visit count /
    last-matched timestamp / auto-verification.
    """
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    status_i = col_index(centers_header, "Status")
    last_matched_i = col_index(centers_header, "Last Matched")
    visits_i = col_index(centers_header, "Visits")

    now_str = datetime.now().isoformat(timespec="seconds")
    centers_ws.update_cell(center_row_number, last_matched_i, now_str)

    if visits_i:
        current_visits = centers_ws.cell(center_row_number, visits_i).value
        try:
            new_visits = int(current_visits) + 1
        except (ValueError, TypeError):
            new_visits = 1
        centers_ws.update_cell(center_row_number, visits_i, new_visits)
        # First-ever match auto-verifies the center (crowd-sourced trust,
        # no admin gate) instead of requiring manual review.
        if new_visits == 1:
            centers_ws.update_cell(center_row_number, status_i, "Verified")

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    givers_ws.update_cell(giver_row_number, col_index(givers_header, "Quantity Confirmed"), final_quantity)
    givers_ws.update_cell(giver_row_number, col_index(givers_header, "Status"), "Matched")


def notify_pending_givers_in_area(area, center_name, center_row):
    """
    When a new center registers, re-matches any giver who's been sitting
    with Status="Pending" (no center was available when they submitted)
    whose area or additional-areas list includes this one - updates their
    row to "Pending Confirmation" pointing at this center, and emails them
    a direct link to confirm the quantity (skipping needing to resubmit
    the whole form).
    """
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    all_values = givers_ws.get_all_values()
    if len(all_values) <= 1:
        return

    status_i = col_index(givers_header, "Status")
    area_i = col_index(givers_header, "Area")
    additional_areas_i = col_index(givers_header, "Additional Areas")
    email_i = col_index(givers_header, "Email")
    name_i = col_index(givers_header, "Name")
    matched_center_i = col_index(givers_header, "Matched Center")
    matched_center_row_i = col_index(givers_header, "Matched Center Row")
    target_area = area.strip().lower()

    for row_num, row in enumerate(all_values[1:], start=2):
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status != "Pending":
            continue
        row_area = row[area_i - 1].strip().lower() if len(row) >= area_i else ""
        row_additional = row[additional_areas_i - 1].lower() if len(row) >= additional_areas_i else ""
        if target_area != row_area and target_area not in [a.strip() for a in row_additional.split(",")]:
            continue

        # Re-match: point this giver at the new center and flip their status,
        # same state as if they'd just chosen it from the browse list.
        givers_ws.update_cell(row_num, status_i, "Pending Confirmation")
        givers_ws.update_cell(row_num, matched_center_i, center_name)
        givers_ws.update_cell(row_num, matched_center_row_i, center_row)

        email = row[email_i - 1] if len(row) >= email_i else ""
        giver_name = row[name_i - 1] if len(row) >= name_i else "there"
        if email:
            confirm_link = url_for("confirm_match", row_number=row_num, _external=True)
            send_email(
                email,
                f"{center_name} just joined in your area!",
                f"<p>Hi {giver_name},</p>"
                f"<p><strong>{center_name}</strong> just registered in {area} — "
                f"the area you offered to give in. You've been matched with them — "
                f'<a href="{confirm_link}">click here to confirm the details</a>.</p>'
                f"<p>— افكر اطعام</p>",
            )


@app.route("/")
def home():
    return render_template("home.html", user=current_user())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        remember_me = request.form.get("remember_me") == "on"

        if not phone:
            flash("Please enter your phone number.")
            return render_template("login.html", form=request.form)

        user = get_user_by_phone(phone)
        if user:
            session["user_phone"] = phone
            session.permanent = remember_me
            return redirect(url_for("home"))

        # New phone number - collect a bit more before creating the account
        return render_template("complete_profile.html", phone=phone, remember_me=remember_me)

    return render_template("login.html", form={})


@app.route("/complete-profile", methods=["POST"])
def complete_profile():
    phone = request.form.get("phone", "").strip()
    real_name = request.form.get("real_name", "").strip()
    email = request.form.get("email", "").strip()
    nickname_choice = request.form.get("nickname_choice", "real_name")
    remember_me = request.form.get("remember_me") == "on"

    if not phone or not real_name:
        flash("Please fill in your name.")
        return render_template("complete_profile.html", phone=phone, remember_me=remember_me,
                                form=request.form)

    nickname = generate_anonymous_nickname() if nickname_choice == "anonymous" else real_name

    create_user(phone, real_name, nickname, email)
    session["user_phone"] = phone
    session.permanent = remember_me
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.pop("user_phone", None)
    return redirect(url_for("home"))


@app.route("/my-activity")
def my_activity():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    all_values = givers_ws.get_all_values()
    phone_i = col_index(givers_header, "Phone")

    my_donations = []
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) >= phone_i and row[phone_i - 1].strip() == user["Phone"].strip():
            my_donations.append(read_row(givers_ws, givers_header, row_num))
    my_donations.reverse()  # most recent first

    return render_template("my_activity.html", user=user, donations=my_donations)


@app.route("/give", methods=["GET", "POST"])
def give_food():
    if request.method == "POST":
        giver_type = request.form.get("giver_type", "")
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        governorate = request.form.get("governorate", "").strip()
        area = request.form.get("area", "").strip()
        area_other = request.form.get("area_other", "").strip()
        if area == "Other" and area_other:
            area = area_other

        area_flexibility = request.form.get("area_flexibility", "Limited")
        additional_areas = request.form.getlist("additional_areas") if area_flexibility == "Open" else []

        food_type = request.form.get("food_type", "")
        time_preference = request.form.get("time_preference", "Flexible") if food_type != "Groceries" else ""
        meal_slot = request.form.get("meal_slot", "") if (food_type != "Groceries" and time_preference == "Specific") else ""
        quantity_max = request.form.get("quantity_number", "").strip()
        quantity_unit = request.form.get("quantity_unit", "")
        ready_date_raw = request.form.get("ready_date", "")
        recurring = request.form.get("recurring", "No")
        recurring_days = ", ".join(request.form.getlist("recurring_days"))
        pickup = "No"  # Phase 1: self-delivery only, no pickup/volunteer option yet

        delivery_contact_who = request.form.get("delivery_contact_who", "Me")
        if delivery_contact_who == "Someone else":
            delivery_contact_name = request.form.get("delivery_contact_name", "").strip()
            delivery_contact_phone = request.form.get("delivery_contact_phone", "").strip()
        else:
            delivery_contact_name = name
            delivery_contact_phone = phone
        backup_phone = request.form.get("backup_phone", "").strip()

        errors = []

        if not giver_type or not name or not phone or not governorate or not area or not quantity_max:
            errors.append("Please fill in all required fields.")
        if delivery_contact_who == "Someone else" and (not delivery_contact_name or not delivery_contact_phone):
            errors.append("Please fill in the name and phone of the person the center should contact.")

        ready_date = None
        if ready_date_raw:
            try:
                ready_date = date.fromisoformat(ready_date_raw)
            except ValueError:
                errors.append("Invalid date format.")
        else:
            errors.append("Please select a ready date.")

        # Minimum one full day's notice (skip this check for recurring/standing offers)
        if ready_date and recurring != "Yes":
            if ready_date < date.today() + timedelta(days=1):
                errors.append(
                    "Ready date must be at least tomorrow, so we can "
                    "arrange a center and let them know ahead of time."
                )

        if errors:
            for e in errors:
                flash(e)
            return render_template("give.html", form=request.form, user=current_user())

        # Stash the validated submission and move to browsing centers -
        # nothing is written to the sheet until the giver actually picks one.
        session["pending_giver"] = {
            "giver_type": giver_type, "name": name, "phone": phone, "email": email,
            "governorate": governorate, "area": area,
            "area_flexibility": area_flexibility, "additional_areas": additional_areas,
            "food_type": food_type, "time_preference": time_preference, "meal_slot": meal_slot,
            "quantity_max": quantity_max, "quantity_unit": quantity_unit,
            "ready_date": ready_date.isoformat(),
            "recurring": recurring, "recurring_days": recurring_days, "pickup": pickup,
            "delivery_contact_who": delivery_contact_who,
            "delivery_contact_name": delivery_contact_name,
            "delivery_contact_phone": delivery_contact_phone,
            "backup_phone": backup_phone,
        }
        return redirect(url_for("browse_centers"))

    return render_template("give.html", form={}, user=current_user())


@app.route("/browse-centers")
def browse_centers():
    pending = session.get("pending_giver")
    if not pending:
        return redirect(url_for("give_food"))

    search_areas = [pending["area"]] + pending.get("additional_areas", [])
    centers = find_candidate_centers(search_areas)

    if not centers:
        # No centers anywhere in range yet - write an honest "no match yet" row.
        ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            pending["giver_type"], pending["name"], pending["phone"], pending.get("email", ""),
            pending["governorate"], pending["area"],
            pending["area_flexibility"], ", ".join(pending.get("additional_areas", [])),
            pending["food_type"], pending["time_preference"], pending["meal_slot"],
            pending["quantity_max"], "", pending["quantity_unit"],
            pending["ready_date"], pending["recurring"], pending["recurring_days"], pending["pickup"],
            pending["delivery_contact_who"], pending["delivery_contact_name"], pending["delivery_contact_phone"],
            pending["backup_phone"],
            "Pending", "", "",
        ])
        if pending.get("email"):
            send_email(
                pending["email"],
                "We'll let you know when a center joins your area",
                f"<p>Hi {pending['name']},</p>"
                f"<p>Thanks for offering to give {pending['quantity_max']} {pending['quantity_unit']} "
                f"in {pending['area']}. There's no registered center there yet, but we'll email you "
                f"the moment one joins so you can come back and give.</p>"
                f"<p>— افكر اطعام</p>",
            )
        session.pop("pending_giver", None)
        return render_template("give_confirmation.html", area=pending["area"],
                                quantity_number=pending["quantity_max"], quantity_unit=pending["quantity_unit"],
                                food_type=pending["food_type"], meal_slot=pending["meal_slot"],
                                ready_date=date.fromisoformat(pending["ready_date"]),
                                matched_center=None)

    return render_template("browse_centers.html", centers=centers, pending=pending)


@app.route("/choose-center/<int:center_row>")
def choose_center(center_row):
    pending = session.get("pending_giver")
    if not pending:
        return redirect(url_for("give_food"))

    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    center = read_row(centers_ws, centers_header, center_row)

    ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    ws.append_row([
        datetime.now().isoformat(timespec="seconds"),
        pending["giver_type"], pending["name"], pending["phone"], pending.get("email", ""),
        pending["governorate"], pending["area"],
        pending["area_flexibility"], ", ".join(pending.get("additional_areas", [])),
        pending["food_type"], pending["time_preference"], pending["meal_slot"],
        pending["quantity_max"], "", pending["quantity_unit"],
        pending["ready_date"], pending["recurring"], pending["recurring_days"], pending["pickup"],
        pending["delivery_contact_who"], pending["delivery_contact_name"], pending["delivery_contact_phone"],
        pending["backup_phone"],
        "Pending Confirmation", center.get("Center Name", ""), center_row,
    ])
    giver_row_number = len(ws.get_all_values())
    session.pop("pending_giver", None)

    return redirect(url_for("confirm_match", row_number=giver_row_number))


@app.route("/confirm-match/<int:row_number>", methods=["GET", "POST"])
def confirm_match(row_number):
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    giver = read_row(givers_ws, givers_header, row_number)

    if giver.get("Status") != "Pending Confirmation":
        # Already confirmed, or an invalid/stale link - nothing to do here.
        return redirect(url_for("home"))

    center_row = int(giver["Matched Center Row"])
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    center = read_row(centers_ws, centers_header, center_row)

    quantity_max = int(giver.get("Quantity Max") or 1)
    # Suggest the lower of what the giver offered and what the center says it needs
    try:
        center_need = int(center.get("Capacity Per Slot") or center.get("Total To Feed") or 0)
    except ValueError:
        center_need = 0
    suggested_quantity = min(quantity_max, center_need) if center_need else quantity_max

    if request.method == "POST":
        try:
            final_quantity = int(request.form.get("final_quantity", "").strip())
        except ValueError:
            final_quantity = 0

        if final_quantity < 1 or final_quantity > quantity_max:
            flash(f"Please enter a quantity between 1 and {quantity_max}.")
            return render_template("confirm_match.html", giver=giver, center=center,
                                    quantity_max=quantity_max, suggested_quantity=suggested_quantity,
                                    row_number=row_number)

        apply_confirmed_match(row_number, center_row, final_quantity)

        return render_template("give_confirmation.html", area=giver.get("Area"),
                                quantity_number=final_quantity, quantity_unit=giver.get("Quantity Unit"),
                                food_type=giver.get("Food Type"), meal_slot=giver.get("Meal Slot"),
                                ready_date=date.fromisoformat(giver.get("Ready Date")),
                                matched_center=center.get("Center Name"))

    return render_template("confirm_match.html", giver=giver, center=center,
                            quantity_max=quantity_max, suggested_quantity=suggested_quantity,
                            row_number=row_number)


@app.route("/register-center", methods=["GET", "POST"])
def register_center():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
        bio = request.form.get("bio", "").strip()
        center_type = request.form.get("center_type", "")
        center_type_other = request.form.get("center_type_other", "").strip()
        if center_type == "Other" and center_type_other:
            center_type = center_type_other

        governorate = request.form.get("governorate", "").strip()
        ownership_type = request.form.get("ownership_type", "").strip()
        ownership_type_other = request.form.get("ownership_type_other", "").strip()
        if ownership_type == "Other" and ownership_type_other:
            ownership_type = ownership_type_other

        area = request.form.get("area", "").strip()
        area_other = request.form.get("area_other", "").strip()
        if area == "Other" and area_other:
            area = area_other
        address = request.form.get("address", "").strip()
        maps_link = request.form.get("maps_link", "").strip()
        social_link = request.form.get("social_link", "").strip()
        target_groups = request.form.getlist("target_group")
        target_group_other = request.form.get("target_group_other", "").strip()
        if target_group_other:
            target_groups.append(target_group_other)
        target_group = ", ".join(target_groups)
        beneficiaries = request.form.get("beneficiaries", "").strip()
        staff_members = request.form.get("staff_members", "").strip()
        total_to_feed = request.form.get("total_to_feed", "").strip()
        preferred_meal_type = request.form.get("preferred_meal_type", "").strip()

        submitter_role = request.form.get("submitter_role", "")
        submitter_name = request.form.get("submitter_name", "").strip()
        submitter_phone = request.form.get("submitter_phone", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        contact_phone = request.form.get("contact_phone", "").strip()
        ritual_schedule = request.form.get("ritual_schedule", "").strip()

        receives = request.form.getlist("receives")
        receives_meals = "Yes" if "Meals" in receives else "No"
        receives_groceries = "Yes" if "Groceries" in receives else "No"
        meal_slots = ", ".join(request.form.getlist("meal_slots"))
        days_open = ", ".join(request.form.getlist("days_open"))
        grocery_hours = request.form.get("grocery_hours", "").strip()

        has_capacity_limit = request.form.get("has_capacity_limit", "No")
        capacity_per_slot = request.form.get("capacity_per_slot", "").strip()
        # If they went through the institution fields (beneficiaries/staff/total)
        # instead of the capacity question, use their stated total as the capacity
        # rather than asking the same thing twice.
        if total_to_feed and not capacity_per_slot:
            has_capacity_limit = "Yes"
            capacity_per_slot = total_to_feed
        photo_files = request.files.getlist("photos")[:10]
        photo_urls = []
        for f in photo_files:
            if f and f.filename:
                url = upload_image_to_drive(f)
                if url:
                    photo_urls.append(url)
        photo_url = ", ".join(photo_urls)

        errors = []
        if not center_name or not governorate or not ownership_type or not area or not address:
            errors.append("Please fill in all required location fields.")
        if not submitter_role or not submitter_name or not submitter_phone:
            errors.append("Please fill in your own name, phone, and role.")
        if not contact_name or not contact_phone:
            errors.append("Please fill in the center's contact person and phone.")
        if not receives:
            errors.append("Please select what the center can receive: meals, groceries, or both.")
        if has_capacity_limit == "Yes" and not capacity_per_slot:
            errors.append("Please enter a capacity per slot, or select 'No' if you don't have a fixed limit.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_center.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, bio, center_type, governorate, ownership_type,
            area, address, maps_link, social_link,
            target_group, beneficiaries, staff_members, total_to_feed, preferred_meal_type,
            submitter_role, submitter_name, submitter_phone,
            contact_name, contact_phone, ritual_schedule,
            receives_meals, meal_slots, days_open,
            receives_groceries, grocery_hours,
            has_capacity_limit, capacity_per_slot,
            photo_url, "Unverified", "", 0,
        ])
        new_center_row = len(ws.get_all_values())

        notify_pending_givers_in_area(area, center_name, new_center_row)

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_center.html", form={})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
