import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SHEET_ID = os.environ.get("SHEET_ID")
GIVERS_TAB = "givers"
CENTERS_TAB = "centers"

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
    "Timestamp", "Giver Type", "Name", "Phone", "Governorate", "Area",
    "Area Flexibility", "Additional Areas",
    "Food Type", "Time Preference", "Meal Slot",
    "Quantity Max", "Quantity Confirmed", "Quantity Unit",
    "Ready Time", "Recurring?", "Recurring Days", "Pickup Available?",
    "Delivery Contact Name", "Delivery Contact Phone",
    "Status", "Matched Center", "Matched Center Row"
]

CENTERS_HEADER = [
    "Timestamp", "Center Name", "Bio", "Center Type", "Governorate", "Ownership Type",
    "Area", "Address", "Maps Link", "Social Link",
    "Target Group", "Beneficiaries", "Staff Members", "Total To Feed", "Preferred Meal Type",
    "Submitter Role", "Submitter Name", "Submitter Phone",
    "Contact Name", "Contact Phone", "Ritual Schedule",
    "Receives Meals", "Meal Slots", "Days Open",
    "Receives Groceries", "Grocery Hours",
    "Has Capacity Limit", "Capacity Per Slot",
    "Photo URL", "Status", "Last Matched", "Visits"
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


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/give", methods=["GET", "POST"])
def give_food():
    if request.method == "POST":
        giver_type = request.form.get("giver_type", "")
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
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
        ready_time_raw = request.form.get("ready_time", "")
        recurring = request.form.get("recurring", "No")
        recurring_days = ", ".join(request.form.getlist("recurring_days"))
        pickup = "No"  # Phase 1: self-delivery only, no pickup/volunteer option yet
        delivery_contact_name = request.form.get("delivery_contact_name", "").strip()
        delivery_contact_phone = request.form.get("delivery_contact_phone", "").strip()

        errors = []

        if not giver_type or not name or not phone or not governorate or not area or not quantity_max:
            errors.append("Please fill in all required fields.")
        if not delivery_contact_name or not delivery_contact_phone:
            errors.append("Please fill in who the center should contact about this delivery.")

        ready_time = None
        if ready_time_raw:
            try:
                ready_time = datetime.fromisoformat(ready_time_raw)
            except ValueError:
                errors.append("Invalid date/time format.")
        else:
            errors.append("Please select a ready time.")

        # 24-hour minimum lead time check (skip this check for recurring/standing offers)
        if ready_time and recurring != "Yes":
            if ready_time < datetime.now() + timedelta(hours=24):
                errors.append(
                    "Ready time must be at least 24 hours from now, so we can "
                    "arrange a center and let them know ahead of time."
                )

        if errors:
            for e in errors:
                flash(e)
            return render_template("give.html", form=request.form)

        # Stash the validated submission and move to browsing centers -
        # nothing is written to the sheet until the giver actually picks one.
        session["pending_giver"] = {
            "giver_type": giver_type, "name": name, "phone": phone,
            "governorate": governorate, "area": area,
            "area_flexibility": area_flexibility, "additional_areas": additional_areas,
            "food_type": food_type, "time_preference": time_preference, "meal_slot": meal_slot,
            "quantity_max": quantity_max, "quantity_unit": quantity_unit,
            "ready_time": ready_time.isoformat(timespec="minutes"),
            "recurring": recurring, "recurring_days": recurring_days, "pickup": pickup,
            "delivery_contact_name": delivery_contact_name,
            "delivery_contact_phone": delivery_contact_phone,
        }
        return redirect(url_for("browse_centers"))

    return render_template("give.html", form={})


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
            pending["giver_type"], pending["name"], pending["phone"],
            pending["governorate"], pending["area"],
            pending["area_flexibility"], ", ".join(pending.get("additional_areas", [])),
            pending["food_type"], pending["time_preference"], pending["meal_slot"],
            pending["quantity_max"], "", pending["quantity_unit"],
            pending["ready_time"], pending["recurring"], pending["recurring_days"], pending["pickup"],
            pending["delivery_contact_name"], pending["delivery_contact_phone"],
            "Pending", "", "",
        ])
        session.pop("pending_giver", None)
        return render_template("give_confirmation.html", area=pending["area"],
                                quantity_number=pending["quantity_max"], quantity_unit=pending["quantity_unit"],
                                food_type=pending["food_type"], meal_slot=pending["meal_slot"],
                                ready_time=datetime.fromisoformat(pending["ready_time"]),
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
        pending["giver_type"], pending["name"], pending["phone"],
        pending["governorate"], pending["area"],
        pending["area_flexibility"], ", ".join(pending.get("additional_areas", [])),
        pending["food_type"], pending["time_preference"], pending["meal_slot"],
        pending["quantity_max"], "", pending["quantity_unit"],
        pending["ready_time"], pending["recurring"], pending["recurring_days"], pending["pickup"],
        pending["delivery_contact_name"], pending["delivery_contact_phone"],
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
                                ready_time=datetime.fromisoformat(giver.get("Ready Time")),
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
        photo_url = request.form.get("photo_url", "").strip()

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

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_center.html", form={})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
