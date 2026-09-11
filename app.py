import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash

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
    "Timestamp", "Name", "Phone", "Area", "Food Type", "Meal Category",
    "Quantity", "Ready Time", "Recurring?", "Pickup Available?", "Status",
    "Matched Center"
]

CENTERS_HEADER = [
    "Timestamp", "Center Name", "Center Type", "Governorate", "Ownership Type",
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


def find_and_apply_match(area, giver_row_number):
    """
    Looks for the best available center in the same area and links it to
    this giver's row. Preference order:
      1. Verified centers over unverified ("Applied") ones
      2. Among equally-verified centers, the one matched least recently
         (fair rotation, so the same center isn't always picked)
    Unverified centers are still eligible so the very first match to a new
    center can act as its first real-world "visit" (crowd-sourced trust,
    rather than requiring admin verification up front).
    Updates both the center's "Last Matched" timestamp and the giver's
    "Status"/"Matched Center" columns. Returns the matched center name,
    or None if no center exists yet in that area.
    """
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    all_values = centers_ws.get_all_values()
    if len(all_values) <= 1:
        return None  # no centers registered yet

    area_i = col_index(centers_header, "Area")
    status_i = col_index(centers_header, "Status")
    name_i = col_index(centers_header, "Center Name")
    last_matched_i = col_index(centers_header, "Last Matched")

    target_area = area.strip().lower()
    candidates = []  # (sheet_row_number, is_verified, last_matched_str, center_name)
    for row_num, row in enumerate(all_values[1:], start=2):
        row_area = row[area_i - 1].strip().lower() if len(row) >= area_i else ""
        if row_area != target_area:
            continue
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status == "Rejected":
            continue
        center_name = row[name_i - 1] if len(row) >= name_i else ""
        last_matched = row[last_matched_i - 1] if len(row) >= last_matched_i else ""
        candidates.append((row_num, status == "Verified", last_matched, center_name))

    if not candidates:
        return None

    # Prefer Verified centers; within each group, prefer least-recently-matched
    # (empty "Last Matched" sorts first, i.e. never-matched centers go first)
    candidates.sort(key=lambda c: (not c[1], c[2]))
    chosen_row, _, _, chosen_name = candidates[0]

    now_str = datetime.now().isoformat(timespec="seconds")
    centers_ws.update_cell(chosen_row, last_matched_i, now_str)

    visits_i = col_index(centers_header, "Visits")
    if visits_i:
        current_visits = all_values[chosen_row - 1][visits_i - 1] if len(all_values[chosen_row - 1]) >= visits_i else ""
        try:
            new_visits = int(current_visits) + 1
        except ValueError:
            new_visits = 1
        centers_ws.update_cell(chosen_row, visits_i, new_visits)

        # First-ever match auto-verifies the center (crowd-sourced trust,
        # no admin gate) instead of requiring manual review.
        if new_visits == 1:
            centers_ws.update_cell(chosen_row, status_i, "Verified")

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    givers_status_i = col_index(givers_header, "Status")
    givers_matched_i = col_index(givers_header, "Matched Center")
    givers_ws.update_cell(giver_row_number, givers_status_i, "Matched")
    givers_ws.update_cell(giver_row_number, givers_matched_i, chosen_name)

    return chosen_name


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/give", methods=["GET", "POST"])
def give_food():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        area = request.form.get("area", "").strip()
        food_type = request.form.get("food_type", "")
        meal_category = request.form.get("meal_category", "")
        quantity = request.form.get("quantity", "").strip()
        ready_time_raw = request.form.get("ready_time", "")
        recurring = request.form.get("recurring", "No")
        pickup = request.form.get("pickup", "No")

        errors = []

        if not name or not phone or not area or not quantity:
            errors.append("Please fill in all required fields.")

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

        ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            name, phone, area, food_type, meal_category,
            quantity, ready_time.isoformat(timespec="minutes"),
            recurring, pickup, "Pending", "",
        ])
        giver_row_number = len(ws.get_all_values())

        matched_center = find_and_apply_match(area, giver_row_number)

        return render_template("give_confirmation.html", area=area, quantity=quantity,
                                food_type=food_type, meal_category=meal_category,
                                ready_time=ready_time, matched_center=matched_center)

    return render_template("give.html", form={})


@app.route("/register-center", methods=["GET", "POST"])
def register_center():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
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
            center_name, center_type, governorate, ownership_type,
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
