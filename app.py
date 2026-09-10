import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SHEET_ID = os.environ.get("SHEET_ID")
GIVERS_TAB = "Givers"
CENTERS_TAB = "Centers"

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
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(header_row))
        ws.append_row(header_row)
    return ws


GIVERS_HEADER = [
    "Timestamp", "Name", "Phone", "Area", "Food Type", "Meal Category",
    "Quantity", "Ready Time", "Recurring?", "Pickup Available?", "Status"
]

CENTERS_HEADER = [
    "Timestamp", "Center Name", "Center Type", "Area", "Contact Name",
    "Contact Phone", "Ritual Schedule", "Capacity", "Status"
]


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
            recurring, pickup, "Pending",
        ])

        return render_template("give_confirmation.html", area=area, quantity=quantity,
                                food_type=food_type, meal_category=meal_category,
                                ready_time=ready_time)

    return render_template("give.html", form={})


@app.route("/register-center", methods=["GET", "POST"])
def register_center():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
        center_type = request.form.get("center_type", "")
        area = request.form.get("area", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        contact_phone = request.form.get("contact_phone", "").strip()
        ritual_schedule = request.form.get("ritual_schedule", "").strip()
        capacity = request.form.get("capacity", "").strip()

        errors = []
        if not center_name or not area or not contact_name or not contact_phone:
            errors.append("Please fill in all required fields.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_center.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, center_type, area, contact_name, contact_phone,
            ritual_schedule, capacity, "Applied",
        ])

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_center.html", form={})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
