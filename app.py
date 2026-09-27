import os
import json
import random
import string
from datetime import datetime, date, timedelta

import requests
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
WAITLIST_TAB = "waitlist"
NEED_SLOTS_TAB = "need_slots"
BLOCKS_TAB = "blocks"
STORIES_TAB = "stories"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Mama Tuhibbun <onboarding@resend.dev>")

# All enum-style values (governorate, area, type, status, yes/no, etc.) are
# stored as their actual Egyptian Arabic text directly in the sheet - not an
# English key translated for display. This makes the raw sheet readable to
# a human without hurting the system's ability to filter/sort/extract data.
MAEDET_RAHMAN_TYPE = "مائدة رحمن"
OTHER = "تاني"

STATUS_SECURED = "مؤمّنة"
STATUS_DELIVERED = "اتوصلت"
STATUS_FAILED = "فشل"
STATUS_CANCELLED = "اتلغت"

SLOT_OPEN = "متاحة"
SLOT_FULL = "مكتملة"

# Weekday name (Egyptian week starts Saturday) keyed by Python's
# date.weekday() (Monday=0 ... Sunday=6).
WEEKDAY_TO_ARABIC = {
    5: "السبت", 6: "الأحد", 0: "الإتنين", 1: "التلات",
    2: "الأربع", 3: "الخميس", 4: "الجمعة",
}

SLOT_GENERATION_DAYS_AHEAD = 21
CANCELLATION_MIN_NOTICE_HOURS = 24
BLOCK_DAYS = 30

MEAL_COMPOSITION_GUIDELINE = (
    "ربع فرخة أو حاجة بروتين تعادلها (زي الكفتة) — تقريبًا 250 جرام، "
    "بالإضافة لنشويات (رز، عيش، أو مكرونة)، طبخة خضار أو سلطة أو بطاطس لو الوجبة سندوتش، "
    "مشروب، وحاجة حلوة بسيطة (فاكهة، بسكويت، أو كيك)."
)
PACKAGING_GUIDELINE = (
    "لفّ الأكل كويس وقفل العبوات كويس عشان متتسربش أثناء النقل. "
    "وحاجة أخيرة: مش لازم يكون الأكل مطبوخ في البيت، ممكن يكون مشترى برضه — "
    "المهم إنه يوصل زي ما كنت هتحبه لنفسك أو لأهلك."
)
DISCLAIMER_TEXT = (
    "مما تحبون منصة تواصل بس بين اللي حابب يدّي واللي محتاج ياخد — "
    "مش مسؤولة عن أي حاجة تحصل بعد كده بين الطرفين. اللي بيدّي هو المسؤول عن "
    "التزامه وتوصيله، واللي بياخد مسؤول عن استلامه والتأكد من الأكل، من غير أي "
    "مسؤولية قانونية أو غيرها على المنصة. أي اتفاق أو مشكلة تحصل، هي بين الطرفين بس."
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

app.jinja_env.globals["meal_guideline_text"] = MEAL_COMPOSITION_GUIDELINE
app.jinja_env.globals["packaging_guideline_text"] = PACKAGING_GUIDELINE
app.jinja_env.globals["disclaimer_text"] = DISCLAIMER_TEXT


def get_sheet_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON environment variable is not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(tab_name, header_row):
    client = get_sheet_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        try:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(header_row))
            ws.append_row(header_row)
        except gspread.exceptions.APIError as e:
            if "already exists" in str(e):
                ws = spreadsheet.worksheet(tab_name)
            else:
                raise

    if not ws.get_all_values():
        ws.append_row(header_row)

    return ws


GIVERS_HEADER = [
    "Timestamp", "Giver Type", "Name", "Phone", "Email", "Area",
    "Food Type", "Time Preference", "Meal Slot",
    "Quantity", "Quantity Unit", "Ready Date", "Recurring?", "Recurring Days",
    "Delivery Contact Who", "Delivery Contact Name", "Delivery Contact Phone", "Backup Phone",
    "Status", "Matched Center", "Matched Center Row",
    "Slot Row", "Delivery Confirmed", "Receipt Confirmed", "No Show Reported",
    "Confirm Token", "Disclaimer Accepted",
]

USERS_HEADER = ["Phone", "Real Name", "Nickname", "Email", "Created At"]

WAITLIST_HEADER = ["Timestamp", "Email", "Area", "Place Type"]

CENTERS_HEADER = [
    "Timestamp", "Center Name", "Bio", "Center Type", "Governorate", "Ownership Type",
    "Area", "Address", "Maps Link", "Social Link",
    "Target Group", "Beneficiaries", "Staff Members", "Total To Feed", "Preferred Meal Type",
    "Submitter Role", "Submitter Name", "Submitter Phone",
    "Contact Name", "Contact Phone", "Contact Email", "Ritual Schedule",
    "Receives Meals", "Meal Slots", "Days Open",
    "Receives Groceries", "Grocery Hours",
    "Has Capacity Limit", "Capacity Per Slot",
    "Photo URLs", "Status", "Last Matched", "Visits",
    "Disclaimer Accepted",
]

NEED_SLOTS_HEADER = [
    "Center Row", "Center Name", "Center Type", "Governorate", "Area",
    "Date", "Meal Slot", "Quantity Needed", "Quantity Secured", "Status",
]

BLOCKS_HEADER = ["Phone", "Blocked Until", "Reason", "Created At"]

STORIES_HEADER = ["Timestamp", "Author Type", "Author Name", "Center Name", "Story Text"]


def ensure_columns(ws, required_header):
    header = ws.row_values(1)
    for col_name in required_header:
        if col_name not in header:
            header.append(col_name)
            ws.update_cell(1, len(header), col_name)
    return header


def col_index(header, col_name):
    try:
        return header.index(col_name) + 1
    except ValueError:
        return None


def get_drive_access_token():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds.token


def upload_image_to_drive(file_storage):
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
    return f"{random.choice(ANONYMOUS_ADJECTIVES)} {random.choice(ANONYMOUS_NOUNS)} {random.randint(10, 99)}"


def generate_token(length=16):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def read_row(ws, header, row_number):
    values = ws.row_values(row_number)
    return {col: (values[i] if i < len(values) else "") for i, col in enumerate(header)}


def get_user_by_phone(phone):
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
    phone = session.get("user_phone")
    if not phone:
        return None
    return get_user_by_phone(phone)


# ---------------------------------------------------------------------------
# Blocking (a phone can be blocked whether or not it ever created an account -
# giving doesn't require login, so blocks are tracked independently by phone)
# ---------------------------------------------------------------------------

def is_blocked(phone):
    """Returns the block-end date (date object) if this phone is currently
    blocked, or None. Reads the newest block entry for the phone."""
    ws = get_worksheet(BLOCKS_TAB, BLOCKS_HEADER)
    header = ensure_columns(ws, BLOCKS_HEADER)
    phone_i = col_index(header, "Phone")
    until_i = col_index(header, "Blocked Until")
    all_values = ws.get_all_values()
    latest_until = None
    for row in all_values[1:]:
        if len(row) >= phone_i and row[phone_i - 1].strip() == phone.strip():
            until_raw = row[until_i - 1] if len(row) >= until_i else ""
            if until_raw:
                try:
                    until_date = date.fromisoformat(until_raw)
                    if latest_until is None or until_date > latest_until:
                        latest_until = until_date
                except ValueError:
                    continue
    if latest_until and latest_until >= date.today():
        return latest_until
    return None


def block_phone(phone, reason):
    ws = get_worksheet(BLOCKS_TAB, BLOCKS_HEADER)
    ensure_columns(ws, BLOCKS_HEADER)
    until = (date.today() + timedelta(days=BLOCK_DAYS)).isoformat()
    ws.append_row([phone, until, reason, datetime.now().isoformat(timespec="seconds")])


def check_missed_confirmations(phone):
    """
    Lazily scans this phone's past commitments: if the ready date has passed,
    the meal was never marked delivered, and the place hasn't already reported
    a no-show (which is handled separately), the giver's own missing
    confirmation is itself the failure signal - mark it failed and apply the
    immediate 30-day block. Runs whenever this phone touches the site
    (login, my-activity, or trying to commit to something new), since there's
    no background scheduler.
    """
    ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(ws, GIVERS_HEADER)
    phone_i = col_index(header, "Phone")
    status_i = col_index(header, "Status")
    ready_date_i = col_index(header, "Ready Date")
    delivered_i = col_index(header, "Delivery Confirmed")
    no_show_i = col_index(header, "No Show Reported")
    all_values = ws.get_all_values()

    today = date.today()
    newly_blocked = False
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) < phone_i or row[phone_i - 1].strip() != phone.strip():
            continue
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status != STATUS_SECURED:
            continue
        ready_raw = row[ready_date_i - 1] if len(row) >= ready_date_i else ""
        delivered = row[delivered_i - 1] if len(row) >= delivered_i else ""
        no_show = row[no_show_i - 1] if len(row) >= no_show_i else ""
        if no_show == "أيوه":
            continue  # already handled via the place's no-show report
        if delivered == "أيوه":
            continue
        try:
            ready_date_val = date.fromisoformat(ready_raw)
        except (ValueError, TypeError):
            continue
        if ready_date_val < today:
            ws.update_cell(row_num, status_i, STATUS_FAILED)
            newly_blocked = True

    if newly_blocked:
        block_phone(phone, "لم يتأكد إنه وصّل الوجبة بعد ما فات معادها")


def find_candidate_places(governorate, areas, only_maedet_rahman=False,
                           date_filter=None, quantity_filter=None, food_type_filter=None):
    """
    Returns (matched, other) - matched are places in one of the selected
    areas (within the governorate); other are places in the same governorate
    but a different area ("other areas to consider"). Both lists are sorted
    Verified-first, then least-recently-matched first. Optional filters are
    pattern-based (checked against the place's registered recurring info,
    not live per-date slot data - the give-to-place page is what shows the
    authoritative, already-secured-aware picture).
    """
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    all_values = centers_ws.get_all_values()
    if len(all_values) <= 1:
        return [], []

    gov_i = col_index(centers_header, "Governorate")
    status_i = col_index(centers_header, "Status")
    type_i = col_index(centers_header, "Center Type")

    target_governorate = governorate.strip()
    target_areas = {a.strip() for a in areas if a.strip()}
    matched, other = [], []

    weekday_name = None
    if date_filter:
        weekday_name = WEEKDAY_TO_ARABIC.get(date_filter.weekday())

    for row_num, row in enumerate(all_values[1:], start=2):
        row_governorate = row[gov_i - 1].strip() if len(row) >= gov_i else ""
        if row_governorate != target_governorate:
            continue
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status == "مرفوض":
            continue
        row_type = row[type_i - 1] if len(row) >= type_i else ""
        is_maedet_rahman = (row_type == MAEDET_RAHMAN_TYPE)
        if only_maedet_rahman != is_maedet_rahman:
            continue

        info = {col: (row[i] if i < len(row) else "") for i, col in enumerate(centers_header)}
        info["_row"] = row_num

        if food_type_filter == "وجبات" and info.get("Receives Meals") != "أيوه":
            continue
        if food_type_filter == "مواد غذائية" and info.get("Receives Groceries") != "أيوه":
            continue

        if weekday_name:
            days_open = [d.strip() for d in (info.get("Days Open") or "").split(",")]
            if info.get("Receives Meals") != "أيوه" or weekday_name not in days_open:
                continue

        if quantity_filter:
            has_cap = info.get("Has Capacity Limit") == "أيوه"
            if has_cap and not is_maedet_rahman:
                # Centers must be fully coverable in one commitment - only
                # show ones the giver's stated amount can fully cover.
                try:
                    cap = int(info.get("Capacity Per Slot") or 0)
                except ValueError:
                    cap = 0
                if cap and cap > quantity_filter:
                    continue
            # Ma'edet Rahman accepts partial contributions toward its total,
            # so any offered quantity is potentially useful - no filtering.

        if info.get("Area", "").strip() in target_areas:
            matched.append(info)
        else:
            other.append(info)

    def sort_key(c):
        return (c.get("Status") != "موثّق", c.get("Last Matched", ""))

    matched.sort(key=sort_key)
    other.sort(key=sort_key)
    return matched, other


def ensure_upcoming_slots(center_row, center):
    """
    Auto-generates concrete dated need-slots for the next few weeks from a
    place's registered recurring pattern (days open + meal slots), entered
    once at registration - no manual re-posting required. Only applies to
    places that receive meals AND have stated a real capacity limit; a place
    with no stated cap is genuinely open-ended, so it doesn't need dated
    slot tracking (any date works, any amount is useful).
    """
    if center.get("Receives Meals") != "أيوه":
        return
    if center.get("Has Capacity Limit") != "أيوه":
        return
    capacity = (center.get("Capacity Per Slot") or "").strip()
    if not capacity:
        return
    days_open = [d.strip() for d in (center.get("Days Open") or "").split(",") if d.strip()]
    if not days_open:
        return
    meal_slots = [m.strip() for m in (center.get("Meal Slots") or "").split(",") if m.strip()] or [""]

    ws = get_worksheet(NEED_SLOTS_TAB, NEED_SLOTS_HEADER)
    header = ensure_columns(ws, NEED_SLOTS_HEADER)
    all_values = ws.get_all_values()
    row_i = col_index(header, "Center Row")
    date_i = col_index(header, "Date")
    slot_i = col_index(header, "Meal Slot")

    existing = set()
    for row in all_values[1:]:
        if len(row) >= row_i and row[row_i - 1] == str(center_row):
            existing.add((row[date_i - 1] if len(row) >= date_i else "",
                          row[slot_i - 1] if len(row) >= slot_i else ""))

    new_rows = []
    today = date.today()
    for offset in range(1, SLOT_GENERATION_DAYS_AHEAD + 1):
        d = today + timedelta(days=offset)
        weekday_name = WEEKDAY_TO_ARABIC.get(d.weekday())
        if weekday_name not in days_open:
            continue
        for meal_slot in meal_slots:
            key = (d.isoformat(), meal_slot)
            if key in existing:
                continue
            new_rows.append([
                center_row, center.get("Center Name", ""), center.get("Center Type", ""),
                center.get("Governorate", ""), center.get("Area", ""),
                d.isoformat(), meal_slot, capacity, 0, SLOT_OPEN,
            ])
    if new_rows:
        ws.append_rows(new_rows)


def get_open_slots_for_center(center_row):
    """Returns available dated slots (still under capacity, in the future),
    sorted chronologically, each with a computed 'Remaining' amount."""
    ws = get_worksheet(NEED_SLOTS_TAB, NEED_SLOTS_HEADER)
    header = ensure_columns(ws, NEED_SLOTS_HEADER)
    all_values = ws.get_all_values()
    row_i = col_index(header, "Center Row")

    today = date.today()
    slots = []
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) < row_i or row[row_i - 1] != str(center_row):
            continue
        info = {col: (row[i] if i < len(row) else "") for i, col in enumerate(header)}
        if info.get("Status") == SLOT_FULL:
            continue
        try:
            slot_date = date.fromisoformat(info.get("Date", ""))
        except ValueError:
            continue
        if slot_date < today:
            continue
        try:
            needed = int(info.get("Quantity Needed") or 0)
            secured = int(info.get("Quantity Secured") or 0)
        except ValueError:
            needed, secured = 0, 0
        remaining = needed - secured
        if remaining <= 0:
            continue
        info["_row"] = row_num
        info["_date_obj"] = slot_date
        info["_weekday_ar"] = WEEKDAY_TO_ARABIC.get(slot_date.weekday(), "")
        info["Remaining"] = remaining
        slots.append(info)

    slots.sort(key=lambda s: (s["_date_obj"], s.get("Meal Slot", "")))
    return slots


def secure_slot(slot_row, quantity):
    ws = get_worksheet(NEED_SLOTS_TAB, NEED_SLOTS_HEADER)
    header = ensure_columns(ws, NEED_SLOTS_HEADER)
    secured_i = col_index(header, "Quantity Secured")
    needed_i = col_index(header, "Quantity Needed")
    status_i = col_index(header, "Status")

    current_secured = ws.cell(slot_row, secured_i).value
    try:
        current_secured = int(current_secured)
    except (ValueError, TypeError):
        current_secured = 0
    needed = ws.cell(slot_row, needed_i).value
    try:
        needed = int(needed)
    except (ValueError, TypeError):
        needed = 0

    new_secured = current_secured + quantity
    ws.update_cell(slot_row, secured_i, new_secured)
    if new_secured >= needed:
        ws.update_cell(slot_row, status_i, SLOT_FULL)


def release_slot(slot_row, quantity):
    """Used when a commitment is cancelled - frees the reserved amount back up."""
    ws = get_worksheet(NEED_SLOTS_TAB, NEED_SLOTS_HEADER)
    header = ensure_columns(ws, NEED_SLOTS_HEADER)
    secured_i = col_index(header, "Quantity Secured")
    status_i = col_index(header, "Status")

    current_secured = ws.cell(slot_row, secured_i).value
    try:
        current_secured = int(current_secured)
    except (ValueError, TypeError):
        current_secured = 0
    new_secured = max(0, current_secured - quantity)
    ws.update_cell(slot_row, secured_i, new_secured)
    ws.update_cell(slot_row, status_i, SLOT_OPEN)


def apply_confirmed_match(giver_row_number, center_row_number):
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
        if new_visits == 1:
            centers_ws.update_cell(center_row_number, status_i, "موثّق")

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    givers_ws.update_cell(giver_row_number, col_index(givers_header, "Status"), STATUS_SECURED)


def notify_center_of_match(center, giver, giver_row_number):
    center_email = center.get("Contact Email")
    if not center_email:
        return
    quantity = giver.get("Quantity")
    unit = giver.get("Quantity Unit")
    delivery_who = giver.get("Delivery Contact Name") or giver.get("Name")
    delivery_phone = giver.get("Delivery Contact Phone") or giver.get("Phone")
    backup_phone = giver.get("Backup Phone")
    token = giver.get("Confirm Token")
    confirm_link = url_for("confirm_receipt", giver_row=giver_row_number, token=token, _external=True)
    no_show_link = url_for("report_no_show", giver_row=giver_row_number, token=token, _external=True)
    send_email(
        center_email,
        f"وجبة مؤمّنة ليكم — {quantity} {unit}",
        f'<div dir="rtl" style="text-align:right;font-family:Tahoma,Arial,sans-serif;">'
        f"<p>أهلاً،</p>"
        f"<p>حد أمّن إنه هيبعتلكم أكل من خلال مما تحبون:</p>"
        f"<ul>"
        f"<li>الكمية: <strong>{quantity} {unit}</strong></li>"
        f"<li>النوع: {giver.get('Food Type')}"
        f"{' — ' + giver.get('Meal Slot') if giver.get('Meal Slot') else ''}</li>"
        f"<li>هيكون جاهز يوم: {giver.get('Ready Date')}</li>"
        f"<li>هيتواصل معاكم: <strong>{delivery_who}</strong> — {delivery_phone}"
        f"{' (رقم احتياطي: ' + backup_phone + ')' if backup_phone else ''}</li>"
        f"</ul>"
        f'<p>بعد ما الأكل يوصلكم، ممكن تأكدوا الاستلام من هنا: '
        f'<a href="{confirm_link}">أكّد إنك استلمت</a></p>'
        f'<p style="color:#999;font-size:12px;">لو المعاد فات وحد ماجاش خالص، قوللنا من هنا: '
        f'<a href="{no_show_link}">محدش جه</a></p>'
        f"<p>— مما تحبون</p></div>",
    )


def add_to_waitlist(email, area, place_type):
    if not email:
        return
    ws = get_worksheet(WAITLIST_TAB, WAITLIST_HEADER)
    ensure_columns(ws, WAITLIST_HEADER)
    ws.append_row([datetime.now().isoformat(timespec="seconds"), email, area, place_type])


def notify_waitlist_for_area(area, place_name, place_type):
    ws = get_worksheet(WAITLIST_TAB, WAITLIST_HEADER)
    header = ensure_columns(ws, WAITLIST_HEADER)
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return

    email_i = col_index(header, "Email")
    area_i = col_index(header, "Area")
    type_i = col_index(header, "Place Type")
    target_area = area.strip()

    browse_route = "browse_maedet_rahman" if place_type == MAEDET_RAHMAN_TYPE else "browse_places"
    browse_link = url_for(browse_route, _external=True)

    for row in all_values[1:]:
        row_area = row[area_i - 1].strip() if len(row) >= area_i else ""
        row_type = row[type_i - 1] if len(row) >= type_i else ""
        if row_area != target_area or row_type != place_type:
            continue
        email = row[email_i - 1] if len(row) >= email_i else ""
        if email:
            send_email(
                email,
                f"{place_name} انضم في منطقتك!",
                f'<div dir="rtl" style="text-align:right;font-family:Tahoma,Arial,sans-serif;">'
                f"<p>أهلاً،</p>"
                f"<p><strong>{place_name}</strong> سجّل دلوقتي في {area} — "
                f'رجع <a href="{browse_link}">هنا</a> عشان تأمّن الوجبة.</p>'
                f"<p>— مما تحبون</p></div>",
            )


def get_homepage_stats():
    """Live counters for the homepage: registered places, meals delivered
    (confirmed on both sides), and unique givers ("people engaged")."""
    stats = {"places": 0, "meals_delivered": 0, "unique_givers": 0}
    try:
        centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
        centers_values = centers_ws.get_all_values()
        status_i = col_index(centers_header, "Status")
        stats["places"] = sum(
            1 for row in centers_values[1:]
            if len(row) < status_i or row[status_i - 1] != "مرفوض"
        )

        givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
        givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
        givers_values = givers_ws.get_all_values()
        status_i = col_index(givers_header, "Status")
        quantity_i = col_index(givers_header, "Quantity")
        phone_i = col_index(givers_header, "Phone")

        delivered_total = 0
        unique_phones = set()
        for row in givers_values[1:]:
            status = row[status_i - 1] if len(row) >= status_i else ""
            if status in (STATUS_SECURED, STATUS_DELIVERED):
                phone = row[phone_i - 1] if len(row) >= phone_i else ""
                if phone:
                    unique_phones.add(phone.strip())
            if status == STATUS_DELIVERED:
                try:
                    delivered_total += int(row[quantity_i - 1])
                except (ValueError, IndexError):
                    delivered_total += 1
        stats["meals_delivered"] = delivered_total
        stats["unique_givers"] = len(unique_phones)
    except Exception as e:
        print(f"Homepage stats failed: {e}")
    return stats


def get_recent_stories(limit=5):
    try:
        ws = get_worksheet(STORIES_TAB, STORIES_HEADER)
        header = ensure_columns(ws, STORIES_HEADER)
        all_values = ws.get_all_values()
        stories = [
            {col: (row[i] if i < len(row) else "") for i, col in enumerate(header)}
            for row in all_values[1:]
        ]
        stories.reverse()
        return [s for s in stories if s.get("Story Text")][:limit]
    except Exception as e:
        print(f"Fetching stories failed: {e}")
        return []


@app.route("/")
def home():
    return render_template("home.html", user=current_user(),
                            stats=get_homepage_stats(), stories=get_recent_stories())


@app.route("/disclaimer")
def disclaimer():
    return render_template("disclaimer.html")


@app.route("/browse-places")
def browse_places():
    governorate = request.args.get("governorate", "")
    areas = request.args.getlist("area")
    date_raw = request.args.get("date_needed", "")
    quantity_raw = request.args.get("quantity_needed", "")
    food_type = request.args.get("food_type_filter", "")

    date_filter = None
    if date_raw:
        try:
            date_filter = date.fromisoformat(date_raw)
        except ValueError:
            date_filter = None
    quantity_filter = None
    if quantity_raw:
        try:
            quantity_filter = int(quantity_raw)
        except ValueError:
            quantity_filter = None

    matched, other = (None, None)
    if areas:
        matched, other = find_candidate_places(
            governorate, areas, only_maedet_rahman=False,
            date_filter=date_filter, quantity_filter=quantity_filter, food_type_filter=food_type,
        )
    return render_template(
        "browse_places.html", governorate=governorate, areas=areas,
        matched=matched, other=other,
        date_needed=date_raw, quantity_needed=quantity_raw, food_type_filter=food_type,
    )


@app.route("/browse-maedet-rahman")
def browse_maedet_rahman():
    governorate = request.args.get("governorate", "")
    areas = request.args.getlist("area")
    date_raw = request.args.get("date_needed", "")
    quantity_raw = request.args.get("quantity_needed", "")
    food_type = request.args.get("food_type_filter", "")

    date_filter = None
    if date_raw:
        try:
            date_filter = date.fromisoformat(date_raw)
        except ValueError:
            date_filter = None
    quantity_filter = None
    if quantity_raw:
        try:
            quantity_filter = int(quantity_raw)
        except ValueError:
            quantity_filter = None

    matched, other = (None, None)
    if areas:
        matched, other = find_candidate_places(
            governorate, areas, only_maedet_rahman=True,
            date_filter=date_filter, quantity_filter=quantity_filter, food_type_filter=food_type,
        )
    return render_template(
        "browse_maedet_rahman.html", governorate=governorate, areas=areas,
        matched=matched, other=other,
        date_needed=date_raw, quantity_needed=quantity_raw, food_type_filter=food_type,
    )


@app.route("/notify-me", methods=["POST"])
def notify_me():
    email = request.form.get("email", "").strip()
    area = request.form.get("area", "").strip()
    place_type = request.form.get("place_type", "جهة استقبال")
    add_to_waitlist(email, area, place_type)
    flash("تمام، هنبلغك أول ما مكان يتسجّل في المنطقة دي.")
    if place_type == MAEDET_RAHMAN_TYPE:
        return redirect(url_for("browse_maedet_rahman", governorate=request.form.get("governorate", ""), area=area))
    return redirect(url_for("browse_places", governorate=request.form.get("governorate", ""), area=area))


@app.route("/give-to-place/<int:center_row>", methods=["GET", "POST"])
def give_to_place(center_row):
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    center = read_row(centers_ws, centers_header, center_row)
    if not center.get("Center Name"):
        return redirect(url_for("browse_places"))

    is_maedet_rahman = (center.get("Center Type") == MAEDET_RAHMAN_TYPE)
    uses_slots = (center.get("Receives Meals") == "أيوه" and center.get("Has Capacity Limit") == "أيوه"
                  and (center.get("Capacity Per Slot") or "").strip())

    if uses_slots:
        ensure_upcoming_slots(center_row, center)

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()

        blocked_until = is_blocked(phone) if phone else None
        if blocked_until:
            flash(f"للأسف رقمك متوقف مؤقتًا لحد {blocked_until.strftime('%d/%m/%Y')} بسبب وجبة سابقة ما اتأكدتش. "
                  f"لو ده غلط، ابعتلنا إيميل من صفحة تواصل معنا.")
            return render_template("give_to_place.html", center=center, form=request.form,
                                    is_maedet_rahman=is_maedet_rahman, uses_slots=uses_slots,
                                    slots=get_open_slots_for_center(center_row) if uses_slots else [])
        if phone:
            check_missed_confirmations(phone)
            blocked_until = is_blocked(phone)
            if blocked_until:
                flash(f"للأسف رقمك اتوقف لحد {blocked_until.strftime('%d/%m/%Y')} — في وجبة سابقة فات معادها من غير ما تتأكد.")
                return render_template("give_to_place.html", center=center, form=request.form,
                                        is_maedet_rahman=is_maedet_rahman, uses_slots=uses_slots,
                                        slots=get_open_slots_for_center(center_row) if uses_slots else [])

        giver_type = request.form.get("giver_type", "")
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        food_type = request.form.get("food_type", "")
        disclaimer_ack = request.form.get("disclaimer_ack") == "on"

        delivery_contact_who = request.form.get("delivery_contact_who", "أنا")
        if delivery_contact_who == "حد تاني":
            delivery_contact_name = request.form.get("delivery_contact_name", "").strip()
            delivery_contact_phone = request.form.get("delivery_contact_phone", "").strip()
        else:
            delivery_contact_name = name
            delivery_contact_phone = phone
        backup_phone = request.form.get("backup_phone", "").strip()

        errors = []
        if not giver_type or not name or not phone or not email:
            errors.append("من فضلك املا كل الحقول المطلوبة (الإيميل بقى مطلوب برضه).")
        if not disclaimer_ack:
            errors.append("لازم توافق على إخلاء المسؤولية الأول.")
        if delivery_contact_who == "حد تاني" and (not delivery_contact_name or not delivery_contact_phone):
            errors.append("من فضلك اكتب اسم ورقم الشخص اللي هيتواصل معاه المكان.")

        selected_slot = None
        ready_date = None
        recurring = "لأ"
        recurring_days = ""
        meal_slot = ""
        time_preference = ""
        quantity = None
        quantity_unit = request.form.get("quantity_unit", "وجبة")

        if uses_slots:
            slot_row_raw = request.form.get("slot_row", "")
            available_slots = get_open_slots_for_center(center_row)
            for s in available_slots:
                if str(s["_row"]) == slot_row_raw:
                    selected_slot = s
                    break
            if not selected_slot:
                errors.append("من فضلك اختار ميعاد متاح.")
            else:
                ready_date = selected_slot["_date_obj"]
                meal_slot = selected_slot.get("Meal Slot", "")
                remaining = selected_slot["Remaining"]
                if is_maedet_rahman:
                    try:
                        quantity = int(request.form.get("quantity_number", "0"))
                    except ValueError:
                        quantity = 0
                    if quantity < 1 or quantity > remaining:
                        errors.append(f"الكمية لازم تكون بين 1 و {remaining} (المتبقي للميعاد ده).")
                else:
                    # Centers: no partial - the giver covers the full remaining need.
                    quantity = remaining
        else:
            time_preference = request.form.get("time_preference", "مرن حسب احتياج المكان") if food_type != "مواد غذائية" else ""
            meal_slot = request.form.get("meal_slot", "") if (food_type != "مواد غذائية" and time_preference == "وقت محدد") else ""
            quantity_raw = request.form.get("quantity_number", "").strip()
            try:
                quantity = int(quantity_raw)
            except ValueError:
                quantity = None
            if not quantity or quantity < 1:
                errors.append("من فضلك اكتب كمية صحيحة.")
            ready_date_raw = request.form.get("ready_date", "")
            recurring = request.form.get("recurring", "لأ")
            recurring_days = ", ".join(request.form.getlist("recurring_days"))
            if ready_date_raw:
                try:
                    ready_date = date.fromisoformat(ready_date_raw)
                except ValueError:
                    errors.append("صيغة التاريخ غير صحيحة.")
            else:
                errors.append("من فضلك اختار تاريخ.")
            if ready_date and recurring != "أيوه":
                if ready_date < date.today() + timedelta(days=1):
                    errors.append("التاريخ لازم يكون بكرة على الأقل.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("give_to_place.html", center=center, form=request.form,
                                    is_maedet_rahman=is_maedet_rahman, uses_slots=uses_slots,
                                    slots=get_open_slots_for_center(center_row) if uses_slots else [])

        confirm_token = generate_token()
        givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
        ensure_columns(givers_ws, GIVERS_HEADER)
        givers_ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            giver_type, name, phone, email, center.get("Area", ""),
            food_type, time_preference, meal_slot,
            quantity, quantity_unit,
            ready_date.isoformat(), recurring, recurring_days,
            delivery_contact_who, delivery_contact_name, delivery_contact_phone, backup_phone,
            STATUS_SECURED, center.get("Center Name", ""), center_row,
            selected_slot["_row"] if selected_slot else "",
            "لأ", "لأ", "لأ", confirm_token, "أيوه",
        ])
        giver_row_number = len(givers_ws.get_all_values())

        if selected_slot:
            secure_slot(selected_slot["_row"], quantity)

        apply_confirmed_match(giver_row_number, center_row)

        giver_row = read_row(givers_ws, ensure_columns(givers_ws, GIVERS_HEADER), giver_row_number)
        notify_center_of_match(center, giver_row, giver_row_number)

        return render_template("give_confirmation.html", area=center.get("Area", ""),
                                quantity_number=quantity, quantity_unit=quantity_unit,
                                food_type=food_type, meal_slot=meal_slot,
                                ready_date=ready_date, matched_center=center.get("Center Name"))

    return render_template("give_to_place.html", center=center, form={},
                            is_maedet_rahman=is_maedet_rahman, uses_slots=uses_slots,
                            slots=get_open_slots_for_center(center_row) if uses_slots else [])


@app.route("/confirm-delivery/<int:giver_row>", methods=["POST"])
def confirm_delivery(giver_row):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(givers_ws, GIVERS_HEADER)
    row = read_row(givers_ws, header, giver_row)
    if row.get("Phone", "").strip() != user["Phone"].strip():
        flash("مش قادرين نأكد الوجبة دي.")
        return redirect(url_for("my_activity"))

    givers_ws.update_cell(giver_row, col_index(header, "Delivery Confirmed"), "أيوه")
    if row.get("Receipt Confirmed") == "أيوه":
        givers_ws.update_cell(giver_row, col_index(header, "Status"), STATUS_DELIVERED)
        flash("جميل، الوجبة اتأكدت من الطرفين. تحب تحكيلنا تجربتك؟")
        return redirect(url_for("share_story", giver_row=giver_row))
    flash("تمام، سجّلنا إنك وصّلت. لسه مستنيين تأكيد المكان.")
    return redirect(url_for("my_activity"))


@app.route("/confirm-receipt/<int:giver_row>/<token>")
def confirm_receipt(giver_row, token):
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(givers_ws, GIVERS_HEADER)
    row = read_row(givers_ws, header, giver_row)
    if not row.get("Confirm Token") or row.get("Confirm Token") != token:
        return render_template("confirm_action_result.html", success=False,
                                message="اللينك ده مش صحيح أو منتهي.")
    givers_ws.update_cell(giver_row, col_index(header, "Receipt Confirmed"), "أيوه")
    if row.get("Delivery Confirmed") == "أيوه":
        givers_ws.update_cell(giver_row, col_index(header, "Status"), STATUS_DELIVERED)
    return render_template("confirm_action_result.html", success=True,
                            message="شكرًا! سجّلنا إنكم استلمتوا الوجبة.")


@app.route("/report-no-show/<int:giver_row>/<token>")
def report_no_show(giver_row, token):
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(givers_ws, GIVERS_HEADER)
    row = read_row(givers_ws, header, giver_row)
    if not row.get("Confirm Token") or row.get("Confirm Token") != token:
        return render_template("confirm_action_result.html", success=False,
                                message="اللينك ده مش صحيح أو منتهي.")
    if row.get("Status") == STATUS_SECURED:
        givers_ws.update_cell(giver_row, col_index(header, "No Show Reported"), "أيوه")
        givers_ws.update_cell(giver_row, col_index(header, "Status"), STATUS_FAILED)
        phone = row.get("Phone", "").strip()
        if phone:
            block_phone(phone, "المكان أبلغ إن الوجبة ما وصلتش")
    return render_template("confirm_action_result.html", success=True,
                            message="آسفين إنها ما وصلتش. سجّلنا البلاغ.")


@app.route("/cancel-commitment/<int:giver_row>", methods=["POST"])
def cancel_commitment(giver_row):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(givers_ws, GIVERS_HEADER)
    row = read_row(givers_ws, header, giver_row)
    if row.get("Phone", "").strip() != user["Phone"].strip():
        flash("مش قادرين نلغي الوجبة دي.")
        return redirect(url_for("my_activity"))
    if row.get("Status") != STATUS_SECURED:
        flash("الوجبة دي مش متاحة للإلغاء دلوقتي.")
        return redirect(url_for("my_activity"))

    try:
        ready_date_val = date.fromisoformat(row.get("Ready Date", ""))
    except ValueError:
        ready_date_val = None
    if ready_date_val:
        deadline = datetime.combine(ready_date_val, datetime.min.time()) - timedelta(hours=CANCELLATION_MIN_NOTICE_HOURS)
        if datetime.now() > deadline:
            flash("للأسف الإلغاء بقى مش متاح — احنا داخلين على أقل من 24 ساعة من المعاد. "
                  "لو مش هتقدر توصّل، من فضلك حاول تدبّر حد يوصّل بدالك عشان محدش يستنى فاضي.")
            return redirect(url_for("my_activity"))

    givers_ws.update_cell(giver_row, col_index(header, "Status"), STATUS_CANCELLED)
    slot_row_raw = row.get("Slot Row", "")
    if slot_row_raw:
        try:
            quantity = int(row.get("Quantity") or 0)
            release_slot(int(slot_row_raw), quantity)
        except ValueError:
            pass
    flash("تم الإلغاء. شكرًا إنك قلتلنا بدري.")
    return redirect(url_for("my_activity"))


@app.route("/story/<int:giver_row>", methods=["GET", "POST"])
def share_story(giver_row):
    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    header = ensure_columns(givers_ws, GIVERS_HEADER)
    row = read_row(givers_ws, header, giver_row)

    if request.method == "POST":
        story_text = request.form.get("story_text", "").strip()
        if story_text:
            ws = get_worksheet(STORIES_TAB, STORIES_HEADER)
            ensure_columns(ws, STORIES_HEADER)
            ws.append_row([
                datetime.now().isoformat(timespec="seconds"),
                "غيرت حد" if row.get("Giver Type") else "",
                row.get("Name", ""), row.get("Matched Center", ""), story_text,
            ])
        flash("شكرًا على المشاركة! 🌿")
        return redirect(url_for("home"))

    return render_template("share_story.html", giver_row=giver_row, matched_center=row.get("Matched Center", ""))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        remember_me = request.form.get("remember_me") == "on"

        if not phone:
            flash("من فضلك اكتب رقم التليفون.")
            return render_template("login.html", form=request.form)

        check_missed_confirmations(phone)

        user = get_user_by_phone(phone)
        if user:
            session["user_phone"] = phone
            session.permanent = remember_me
            return redirect(url_for("home"))

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
        flash("من فضلك اكتب اسمك.")
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

    check_missed_confirmations(user["Phone"])

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    all_values = givers_ws.get_all_values()
    phone_i = col_index(givers_header, "Phone")

    my_donations = []
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) >= phone_i and row[phone_i - 1].strip() == user["Phone"].strip():
            donation = read_row(givers_ws, givers_header, row_num)
            donation["_row"] = row_num
            try:
                donation["_ready_date_obj"] = date.fromisoformat(donation.get("Ready Date", ""))
            except ValueError:
                donation["_ready_date_obj"] = None
            my_donations.append(donation)
    my_donations.reverse()

    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    centers_values = centers_ws.get_all_values()
    submitter_phone_i = col_index(centers_header, "Submitter Phone")

    my_places = []
    for row_num, row in enumerate(centers_values[1:], start=2):
        if len(row) >= submitter_phone_i and row[submitter_phone_i - 1].strip() == user["Phone"].strip():
            place = read_row(centers_ws, centers_header, row_num)
            place["_row"] = row_num
            my_places.append(place)
    my_places.reverse()

    blocked_until = is_blocked(user["Phone"])

    return render_template("my_activity.html", user=user, donations=my_donations,
                            places=my_places, blocked_until=blocked_until,
                            cancellation_hours=CANCELLATION_MIN_NOTICE_HOURS, today=date.today())


def _place_form_common(form):
    """Shared field extraction for both register + edit of centers/ma'edet rahman."""
    governorate = form.get("governorate", "").strip()
    ownership_type = form.get("ownership_type", "").strip()
    ownership_type_other = form.get("ownership_type_other", "").strip()
    if ownership_type == OTHER and ownership_type_other:
        ownership_type = ownership_type_other

    area = form.get("area", "").strip()
    area_other = form.get("area_other", "").strip()
    if area == OTHER and area_other:
        area = area_other

    receives = form.getlist("receives")
    receives_meals = "أيوه" if "وجبات" in receives else "لأ"
    receives_groceries = "أيوه" if "مواد غذائية" in receives else "لأ"
    meal_slots = ", ".join(form.getlist("meal_slots"))
    days_open = ", ".join(form.getlist("days_open"))
    grocery_hours = form.get("grocery_hours", "").strip()

    has_capacity_limit = form.get("has_capacity_limit", "لأ")
    capacity_per_slot = form.get("capacity_per_slot", "").strip()

    return {
        "governorate": governorate, "ownership_type": ownership_type,
        "area": area, "address": form.get("address", "").strip(),
        "maps_link": form.get("maps_link", "").strip(), "social_link": form.get("social_link", "").strip(),
        "submitter_role": form.get("submitter_role", ""), "submitter_name": form.get("submitter_name", "").strip(),
        "submitter_phone": form.get("submitter_phone", "").strip(),
        "contact_name": form.get("contact_name", "").strip(), "contact_phone": form.get("contact_phone", "").strip(),
        "contact_email": form.get("contact_email", "").strip(), "ritual_schedule": form.get("ritual_schedule", "").strip(),
        "receives_meals": receives_meals, "receives_groceries": receives_groceries, "receives": receives,
        "meal_slots": meal_slots, "days_open": days_open, "grocery_hours": grocery_hours,
        "has_capacity_limit": has_capacity_limit, "capacity_per_slot": capacity_per_slot,
    }


@app.route("/register-center", methods=["GET", "POST"])
def register_center():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
        bio = request.form.get("bio", "").strip()
        center_type = request.form.get("center_type", "")
        center_type_other = request.form.get("center_type_other", "").strip()
        if center_type == OTHER and center_type_other:
            center_type = center_type_other

        common = _place_form_common(request.form)

        target_groups = request.form.getlist("target_group")
        target_group_other = request.form.get("target_group_other", "").strip()
        if target_group_other:
            target_groups.append(target_group_other)
        target_group = ", ".join(target_groups)
        beneficiaries = request.form.get("beneficiaries", "").strip()
        staff_members = request.form.get("staff_members", "").strip()
        total_to_feed = request.form.get("total_to_feed", "").strip()
        preferred_meal_type = request.form.get("preferred_meal_type", "").strip()
        disclaimer_ack = request.form.get("disclaimer_ack") == "on"

        has_capacity_limit = common["has_capacity_limit"]
        capacity_per_slot = common["capacity_per_slot"]
        if total_to_feed and not capacity_per_slot:
            has_capacity_limit = "أيوه"
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
        if not center_name or not common["governorate"] or not common["ownership_type"] or not common["area"] or not common["address"]:
            errors.append("من فضلك املا كل حقول الموقع المطلوبة.")
        if not common["submitter_role"] or not common["submitter_name"] or not common["submitter_phone"]:
            errors.append("من فضلك اكتب اسمك، رقمك، ودورك.")
        if not common["contact_name"] or not common["contact_phone"]:
            errors.append("من فضلك اكتب اسم ورقم الشخص المسؤول في المكان.")
        if not common["contact_email"]:
            errors.append("إيميل المكان بقى مطلوب — عشان نبلغكم أول ما حد يأمّن أكل ليكم.")
        if not common["receives"]:
            errors.append("من فضلك اختار إيه اللي المكان بيستقبله: وجبات، مواد غذائية، أو الاتنين.")
        if has_capacity_limit == "أيوه" and not capacity_per_slot:
            errors.append("من فضلك اكتب السقف لكل فترة، أو اختار \"لأ\" لو مفيش سقف ثابت.")
        if not disclaimer_ack:
            errors.append("لازم توافق على إخلاء المسؤولية الأول.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_center.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ensure_columns(ws, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, bio, center_type, common["governorate"], common["ownership_type"],
            common["area"], common["address"], common["maps_link"], common["social_link"],
            target_group, beneficiaries, staff_members, total_to_feed, preferred_meal_type,
            common["submitter_role"], common["submitter_name"], common["submitter_phone"],
            common["contact_name"], common["contact_phone"], common["contact_email"], common["ritual_schedule"],
            common["receives_meals"], common["meal_slots"], common["days_open"],
            common["receives_groceries"], common["grocery_hours"],
            has_capacity_limit, capacity_per_slot,
            photo_url, "لسه مش موثّق", "", 0, "أيوه",
        ])

        notify_waitlist_for_area(common["area"], center_name, "جهة استقبال")

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_center.html", form={})


@app.route("/register-maedet-rahman", methods=["GET", "POST"])
def register_maedet_rahman():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
        bio = request.form.get("bio", "").strip()
        center_type = MAEDET_RAHMAN_TYPE
        disclaimer_ack = request.form.get("disclaimer_ack") == "on"

        common = _place_form_common(request.form)
        target_group = beneficiaries = staff_members = total_to_feed = preferred_meal_type = ""

        photo_files = request.files.getlist("photos")[:10]
        photo_urls = []
        for f in photo_files:
            if f and f.filename:
                url = upload_image_to_drive(f)
                if url:
                    photo_urls.append(url)
        photo_url = ", ".join(photo_urls)

        errors = []
        if not center_name or not common["governorate"] or not common["ownership_type"] or not common["area"] or not common["address"]:
            errors.append("من فضلك املا كل حقول الموقع المطلوبة.")
        if not common["submitter_role"] or not common["submitter_name"] or not common["submitter_phone"]:
            errors.append("من فضلك اكتب اسمك، رقمك، ودورك.")
        if not common["contact_name"] or not common["contact_phone"]:
            errors.append("من فضلك اكتب اسم ورقم الشخص المسؤول في المكان.")
        if not common["contact_email"]:
            errors.append("إيميل المائدة بقى مطلوب — عشان نبلغكم أول ما حد يأمّن أكل ليها.")
        if not common["receives"]:
            errors.append("من فضلك اختار إيه اللي المائدة بتستقبله: وجبات، مواد غذائية، أو الاتنين.")
        if common["has_capacity_limit"] == "أيوه" and not common["capacity_per_slot"]:
            errors.append("من فضلك اكتب السقف التقريبي، أو اختار \"لأ\" لو المائدة مفتوحة من غير سقف.")
        if not disclaimer_ack:
            errors.append("لازم توافق على إخلاء المسؤولية الأول.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_maedet_rahman.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ensure_columns(ws, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, bio, center_type, common["governorate"], common["ownership_type"],
            common["area"], common["address"], common["maps_link"], common["social_link"],
            target_group, beneficiaries, staff_members, total_to_feed, preferred_meal_type,
            common["submitter_role"], common["submitter_name"], common["submitter_phone"],
            common["contact_name"], common["contact_phone"], common["contact_email"], common["ritual_schedule"],
            common["receives_meals"], common["meal_slots"], common["days_open"],
            common["receives_groceries"], common["grocery_hours"],
            common["has_capacity_limit"], common["capacity_per_slot"],
            photo_url, "لسه مش موثّق", "", 0, "أيوه",
        ])

        notify_waitlist_for_area(common["area"], center_name, MAEDET_RAHMAN_TYPE)

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_maedet_rahman.html", form={})


@app.route("/edit-place/<int:row>", methods=["GET", "POST"])
def edit_place(row):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    header = ensure_columns(ws, CENTERS_HEADER)
    place = read_row(ws, header, row)
    if not place.get("Center Name"):
        flash("المكان ده مش موجود.")
        return redirect(url_for("my_activity"))
    if place.get("Submitter Phone", "").strip() != user["Phone"].strip():
        flash("مش قادرين نعدّل المكان ده.")
        return redirect(url_for("my_activity"))

    is_maedet_rahman = (place.get("Center Type") == MAEDET_RAHMAN_TYPE)
    template_name = "register_maedet_rahman.html" if is_maedet_rahman else "register_center.html"

    if request.method == "POST":
        common = _place_form_common(request.form)
        updates = {
            "Center Name": request.form.get("center_name", "").strip(),
            "Bio": request.form.get("bio", "").strip(),
            "Governorate": common["governorate"], "Ownership Type": common["ownership_type"],
            "Area": common["area"], "Address": common["address"],
            "Maps Link": common["maps_link"], "Social Link": common["social_link"],
            "Contact Name": common["contact_name"], "Contact Phone": common["contact_phone"],
            "Contact Email": common["contact_email"], "Ritual Schedule": common["ritual_schedule"],
            "Receives Meals": common["receives_meals"], "Meal Slots": common["meal_slots"],
            "Days Open": common["days_open"], "Receives Groceries": common["receives_groceries"],
            "Grocery Hours": common["grocery_hours"], "Has Capacity Limit": common["has_capacity_limit"],
            "Capacity Per Slot": common["capacity_per_slot"],
        }
        if not is_maedet_rahman:
            center_type = request.form.get("center_type", "")
            center_type_other = request.form.get("center_type_other", "").strip()
            if center_type == OTHER and center_type_other:
                center_type = center_type_other
            target_groups = request.form.getlist("target_group")
            target_group_other = request.form.get("target_group_other", "").strip()
            if target_group_other:
                target_groups.append(target_group_other)
            updates.update({
                "Center Type": center_type,
                "Target Group": ", ".join(target_groups),
                "Beneficiaries": request.form.get("beneficiaries", "").strip(),
                "Staff Members": request.form.get("staff_members", "").strip(),
                "Total To Feed": request.form.get("total_to_feed", "").strip(),
                "Preferred Meal Type": request.form.get("preferred_meal_type", "").strip(),
            })

        for col_name, value in updates.items():
            idx = col_index(header, col_name)
            if idx:
                ws.update_cell(row, idx, value)

        flash("اتحدّث المكان بنجاح.")
        return redirect(url_for("my_activity"))

    # Pre-fill the same form used at registration, with this place's current data.
    prefill = {
        "center_name": place.get("Center Name", ""), "bio": place.get("Bio", ""),
        "center_type": place.get("Center Type", ""), "governorate": place.get("Governorate", ""),
        "ownership_type": place.get("Ownership Type", ""), "area": place.get("Area", ""),
        "address": place.get("Address", ""), "maps_link": place.get("Maps Link", ""),
        "social_link": place.get("Social Link", ""), "submitter_role": place.get("Submitter Role", ""),
        "submitter_name": place.get("Submitter Name", ""), "submitter_phone": place.get("Submitter Phone", ""),
        "contact_name": place.get("Contact Name", ""), "contact_phone": place.get("Contact Phone", ""),
        "contact_email": place.get("Contact Email", ""), "ritual_schedule": place.get("Ritual Schedule", ""),
        "grocery_hours": place.get("Grocery Hours", ""), "capacity_per_slot": place.get("Capacity Per Slot", ""),
        "beneficiaries": place.get("Beneficiaries", ""), "staff_members": place.get("Staff Members", ""),
        "total_to_feed": place.get("Total To Feed", ""), "preferred_meal_type": place.get("Preferred Meal Type", ""),
    }
    return render_template(template_name, form=prefill, editing_row=row)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
