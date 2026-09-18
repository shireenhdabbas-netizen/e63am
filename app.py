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
WAITLIST_TAB = "waitlist"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Mama Tuhibbun <onboarding@resend.dev>")

MAEDET_RAHMAN_TYPE = "Ma'edet Rahman (community meal table)"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Stored/matched values stay in English (so matching logic and the sheet
# stay stable and simple to work with) - this dict translates them to
# Egyptian Arabic wherever they're displayed back to a person. Used via
# the `ar` Jinja filter below, and mirrored in the registration forms' JS
# for the two area dropdowns.
AR_LABELS = {
    # Governorates
    "Cairo": "القاهرة", "Giza": "الجيزة",
    # Cairo areas
    "Shubra": "شبرا", "El-Zawia El-Hamra": "الزاوية الحمراء", "Hadayek El-Kobba": "حدائق القبة",
    "Rod El-Farg": "روض الفرج", "El-Sharabia": "الشرابية", "El-Sahel": "الساحل",
    "El-Zaiton": "الزيتون", "Al-Ameria": "الأميرية", "Masr El-Qadima": "مصر القديمة",
    "El-Khalifa": "الخليفة", "El-Moqattam": "المقطم", "El-Basatin": "البساتين",
    "Dar El-Salam": "دار السلام", "El-Sayeda Zeinab": "السيدة زينب", "El-Tebin": "التبين",
    "Helwan": "حلوان", "El-Maasara": "المعصرة", "El-Maadi": "المعادي", "Tora": "طرة",
    "15 May": "15 مايو", "Manshaet Naser": "منشأة ناصر", "El-Waily": "الوايلي",
    "Wast El-Qahira (Downtown)": "وسط القاهرة", "Boulak": "بولاق", "Gharb El-Qahira": "غرب القاهرة",
    "Abdeen": "عابدين", "Azbakia": "الأزبكية", "Moski": "الموسكي", "Bab El-Shaaria": "باب الشعرية",
    "Misr El-Gadidah (Heliopolis)": "مصر الجديدة (هليوبوليس)", "El-Nozha": "النزهة",
    "Sharq Madinet Nasr": "شرق مدينة نصر", "Gharb Madinet Nasr": "غرب مدينة نصر",
    "El-Salam Awal": "السلام أول", "El-Salam Thani": "السلام ثاني", "El-Mataria": "المطرية",
    "Ain Shams": "عين شمس", "El-Marg": "المرج", "Zamalek": "الزمالك", "Garden City": "جاردن سيتي",
    "New Cairo": "القاهرة الجديدة", "Katameya": "القطامية",
    # Giza areas
    "Dokki": "الدقي", "Agouza / Mohandessin": "العجوزة / المهندسين", "Omraniya": "العمرانية",
    "Haram": "الهرم", "Boulak El Dakrour": "بولاق الدكرور", "El Warraq": "الوراق",
    "North Giza": "شمال الجيزة", "South Giza": "جنوب الجيزة", "6th of October": "السادس من أكتوبر",
    "Sheikh Zayed": "الشيخ زايد", "Imbaba": "إمبابة", "Faisal": "فيصل", "Giza Square": "ميدان الجيزة",
    "Al-Ayyat": "العياط", "Atfih": "أطفيح", "Awsim": "أوسيم", "Al-Badrashin": "البدرشين",
    "Al-Hawamdiya": "الحوامدية", "Al-Saff": "الصف", "Kerdasa": "كرداسة",
    "Manshaat El Qanater": "منشأة القناطر", "Other": "تاني",
    # Giver type
    "Individual": "فرد", "Restaurant / Business": "مطعم / منشأة",
    # Food type
    "Hot meal": "وجبة سخنة", "Cold meal": "وجبة باردة", "Groceries": "مواد غذائية",
    # Time preference
    "Flexible": "مرن حسب احتياج المكان", "Specific": "وقت محدد",
    # Meal slots
    "Breakfast (9-11)": "فطار (9-11)", "Late breakfast (12-2)": "فطار متأخر (12-2)",
    "Lunch (4-6)": "غدا (4-6)", "Dinner (8-10)": "عشا (8-10)",
    # Quantity units
    "meals": "وجبة", "portions": "حصة", "kg": "كجم", "boxes": "كرتونة", "items": "قطعة",
    # Yes/No (recurring, capacity limit, etc.)
    "Yes": "أيوه", "No": "لأ",
    # Days
    "Saturday": "السبت", "Sunday": "الأحد", "Monday": "الإتنين", "Tuesday": "التلات",
    "Wednesday": "الأربع", "Thursday": "الخميس", "Friday": "الجمعة",
    # Delivery contact who
    "Me": "أنا", "Someone else": "حد تاني",
    # Status
    "Matched": "مؤمّنة", "Verified": "موثّق", "Unverified": "لسه مش موثّق", "Rejected": "مرفوض",
    # Center type
    "Mosque": "مسجد", "Church": "كنيسة", "Community center": "مركز خدمة مجتمعي",
    "Orphanage": "دار أيتام", "Elderly home": "دار مسنين", "Shop": "محل",
    "Local NGO / Community organization": "منظمة أهلية محلية",
    MAEDET_RAHMAN_TYPE: "مائدة رحمن", "Homeless outreach point": "نقطة دعم للمشردين",
    # Ownership type
    "Governmental": "حكومي", "Private": "خاص", "Local NGO": "منظمة أهلية",
    "Independent / community-run": "مستقل / أهلي",
    # Target groups served
    "Elderly": "مسنين", "Elderly with chronic illness": "مسنين بأمراض مزمنة", "Children": "أطفال",
    "Children with disabilities": "أطفال ذوي إعاقة", "People with disabilities": "ذوي إعاقة",
    "Widows": "أرامل", "Families in need": "أسر محتاجة", "Orphans": "أيتام", "Homeless": "مشردين",
    # Submitter role
    "Staff": "من فريق المكان", "Info-only": "بشارك معلومات بس",
    # Place type (waitlist)
    "Center": "جهة استقبال", "Ma'edet Rahman": "مائدة رحمن",
}


@app.template_filter("ar")
def ar_label(value):
    """
    Translates a stored English value (or a comma-joined list of them, e.g.
    "Elderly, Children") to Egyptian Arabic for display, using AR_LABELS.
    Falls back to the original value if there's no translation on file.
    """
    if not value:
        return value
    if isinstance(value, str) and ", " in value:
        return "، ".join(AR_LABELS.get(p.strip(), p.strip()) for p in value.split(", "))
    return AR_LABELS.get(value, value)


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
    "Timestamp", "Giver Type", "Name", "Phone", "Email", "Area",
    "Food Type", "Time Preference", "Meal Slot",
    "Quantity", "Quantity Unit", "Ready Date", "Recurring?", "Recurring Days",
    "Delivery Contact Who", "Delivery Contact Name", "Delivery Contact Phone", "Backup Phone",
    "Status", "Matched Center", "Matched Center Row"
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
    "Photo URLs", "Status", "Last Matched", "Visits"
]


def ensure_columns(ws, required_header):
    """
    Makes sure every column in required_header exists in row 1 of the sheet,
    appending any that are missing (for sheets created before a column was
    added to the schema). Returns the current header row after any additions.
    """
    header = ws.row_values(1)
    for col_name in required_header:
        if col_name not in header:
            header.append(col_name)
            ws.update_cell(1, len(header), col_name)
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
    notification, not something that should ever break the actual giving flow.
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


def find_candidate_places(areas, only_maedet_rahman=False):
    """
    Returns every eligible place (center or ma'edet rahman) across the given
    areas, sorted for browsing: Verified first, then within each group the
    one matched least recently (so it spreads out rather than always
    hitting the same place). Unverified places are still included - a
    giver choosing one becomes its first real "visit" (crowd-sourced
    trust, no admin gate).
    only_maedet_rahman=True restricts to open tables; False restricts to
    everything else (limited-beneficiary institutions).
    Returns a list of dicts, each the full place row plus its sheet row
    number under the key "_row".
    """
    centers_ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
    centers_header = ensure_columns(centers_ws, CENTERS_HEADER)
    all_values = centers_ws.get_all_values()
    if len(all_values) <= 1:
        return []

    area_i = col_index(centers_header, "Area")
    status_i = col_index(centers_header, "Status")
    type_i = col_index(centers_header, "Center Type")

    target_areas = set(a.strip().lower() for a in areas if a.strip())
    candidates = []
    for row_num, row in enumerate(all_values[1:], start=2):
        row_area = row[area_i - 1].strip().lower() if len(row) >= area_i else ""
        if row_area not in target_areas:
            continue
        status = row[status_i - 1] if len(row) >= status_i else ""
        if status == "Rejected":
            continue
        row_type = row[type_i - 1] if len(row) >= type_i else ""
        is_maedet_rahman = (row_type == MAEDET_RAHMAN_TYPE)
        if only_maedet_rahman != is_maedet_rahman:
            continue
        info = {col: (row[i] if i < len(row) else "") for i, col in enumerate(centers_header)}
        info["_row"] = row_num
        candidates.append(info)

    # Verified first; within each group, least-recently-matched first
    # (empty "Last Matched" sorts first, i.e. never-matched places surface early)
    candidates.sort(key=lambda c: (c.get("Status") != "Verified", c.get("Last Matched", "")))
    return candidates


def apply_confirmed_match(giver_row_number, center_row_number):
    """
    Finalizes a match: flips the giver's row to "Matched" (quantity was
    already written when the row was created, since browsing now always
    happens before committing) and updates the place's visit count /
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
        # First-ever match auto-verifies the place (crowd-sourced trust,
        # no admin gate) instead of requiring manual review.
        if new_visits == 1:
            centers_ws.update_cell(center_row_number, status_i, "Verified")

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    givers_ws.update_cell(giver_row_number, col_index(givers_header, "Status"), "Matched")


def notify_center_of_match(center, giver):
    """Emails the place with everything they need once a giver secures a match."""
    center_email = center.get("Contact Email")
    if not center_email:
        return
    quantity = giver.get("Quantity")
    unit = AR_LABELS.get(giver.get("Quantity Unit"), giver.get("Quantity Unit"))
    delivery_who = giver.get("Delivery Contact Name") or giver.get("Name")
    delivery_phone = giver.get("Delivery Contact Phone") or giver.get("Phone")
    backup_phone = giver.get("Backup Phone")
    send_email(
        center_email,
        f"وجبة مؤمّنة ليكم — {quantity} {unit}",
        f'<div dir="rtl" style="text-align:right;font-family:Tahoma,Arial,sans-serif;">'
        f"<p>أهلاً،</p>"
        f"<p>حد أمّن إنه هيبعتلكم أكل من خلال مما تحبون:</p>"
        f"<ul>"
        f"<li>الكمية: <strong>{quantity} {unit}</strong></li>"
        f"<li>النوع: {AR_LABELS.get(giver.get('Food Type'), giver.get('Food Type'))}"
        f"{' — ' + AR_LABELS.get(giver.get('Meal Slot'), giver.get('Meal Slot')) if giver.get('Meal Slot') else ''}</li>"
        f"<li>هيكون جاهز يوم: {giver.get('Ready Date')}</li>"
        f"<li>هيتواصل معاكم: <strong>{delivery_who}</strong> — {delivery_phone}"
        f"{' (رقم احتياطي: ' + backup_phone + ')' if backup_phone else ''}</li>"
        f"</ul>"
        f"<p>— مما تحبون</p></div>",
    )


def add_to_waitlist(email, area, place_type):
    if not email:
        return
    ws = get_worksheet(WAITLIST_TAB, WAITLIST_HEADER)
    ensure_columns(ws, WAITLIST_HEADER)
    ws.append_row([datetime.now().isoformat(timespec="seconds"), email, area, place_type])


def notify_waitlist_for_area(area, place_name, place_type):
    """
    When a new place registers, emails anyone who signed up to be notified
    about this area + place type, inviting them back to browse and secure
    a meal there.
    """
    ws = get_worksheet(WAITLIST_TAB, WAITLIST_HEADER)
    header = ensure_columns(ws, WAITLIST_HEADER)
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return

    email_i = col_index(header, "Email")
    area_i = col_index(header, "Area")
    type_i = col_index(header, "Place Type")
    target_area = area.strip().lower()

    browse_route = "browse_maedet_rahman" if place_type == "Ma'edet Rahman" else "browse_places"
    browse_link = url_for(browse_route, _external=True)

    for row in all_values[1:]:
        row_area = row[area_i - 1].strip().lower() if len(row) >= area_i else ""
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
                f"<p><strong>{place_name}</strong> سجّل دلوقتي في {AR_LABELS.get(area, area)} — "
                f'رجع <a href="{browse_link}">هنا</a> عشان تأمّن الوجبة.</p>'
                f"<p>— مما تحبون</p></div>",
            )


@app.route("/")
def home():
    return render_template("home.html", user=current_user())


@app.route("/browse-places")
def browse_places():
    governorate = request.args.get("governorate", "")
    area = request.args.get("area", "")
    results = None
    if area:
        results = find_candidate_places([area], only_maedet_rahman=False)
    return render_template("browse_places.html", governorate=governorate, area=area, results=results)


@app.route("/browse-maedet-rahman")
def browse_maedet_rahman():
    governorate = request.args.get("governorate", "")
    area = request.args.get("area", "")
    results = None
    if area:
        results = find_candidate_places([area], only_maedet_rahman=True)
    return render_template("browse_maedet_rahman.html", governorate=governorate, area=area, results=results)


@app.route("/notify-me", methods=["POST"])
def notify_me():
    email = request.form.get("email", "").strip()
    area = request.form.get("area", "").strip()
    place_type = request.form.get("place_type", "Center")
    add_to_waitlist(email, area, place_type)
    flash("تمام، هنبلغك أول ما مكان يتسجّل في المنطقة دي.")
    if place_type == "Ma'edet Rahman":
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

    if request.method == "POST":
        giver_type = request.form.get("giver_type", "")
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        food_type = request.form.get("food_type", "")
        time_preference = request.form.get("time_preference", "Flexible") if food_type != "Groceries" else ""
        meal_slot = request.form.get("meal_slot", "") if (food_type != "Groceries" and time_preference == "Specific") else ""
        quantity = request.form.get("quantity_number", "").strip()
        quantity_unit = request.form.get("quantity_unit", "")
        ready_date_raw = request.form.get("ready_date", "")
        recurring = request.form.get("recurring", "No")
        recurring_days = ", ".join(request.form.getlist("recurring_days"))

        delivery_contact_who = request.form.get("delivery_contact_who", "Me")
        if delivery_contact_who == "Someone else":
            delivery_contact_name = request.form.get("delivery_contact_name", "").strip()
            delivery_contact_phone = request.form.get("delivery_contact_phone", "").strip()
        else:
            delivery_contact_name = name
            delivery_contact_phone = phone
        backup_phone = request.form.get("backup_phone", "").strip()

        errors = []
        if not giver_type or not name or not phone or not quantity:
            errors.append("من فضلك املا كل الحقول المطلوبة.")
        if delivery_contact_who == "Someone else" and (not delivery_contact_name or not delivery_contact_phone):
            errors.append("من فضلك اكتب اسم ورقم الشخص اللي هيتواصل معاه المكان.")

        ready_date = None
        if ready_date_raw:
            try:
                ready_date = date.fromisoformat(ready_date_raw)
            except ValueError:
                errors.append("صيغة التاريخ غير صحيحة.")
        else:
            errors.append("من فضلك اختار تاريخ.")
        if ready_date and recurring != "Yes":
            if ready_date < date.today() + timedelta(days=1):
                errors.append("التاريخ لازم يكون بكرة على الأقل.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("give_to_place.html", center=center, form=request.form,
                                    is_maedet_rahman=is_maedet_rahman)

        givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
        ensure_columns(givers_ws, GIVERS_HEADER)
        givers_ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            giver_type, name, phone, email, center.get("Area", ""),
            food_type, time_preference, meal_slot,
            quantity, quantity_unit,
            ready_date.isoformat(), recurring, recurring_days,
            delivery_contact_who, delivery_contact_name, delivery_contact_phone, backup_phone,
            "Matched", center.get("Center Name", ""), center_row,
        ])
        giver_row_number = len(givers_ws.get_all_values())

        apply_confirmed_match(giver_row_number, center_row)

        giver_row = read_row(givers_ws, ensure_columns(givers_ws, GIVERS_HEADER), giver_row_number)
        notify_center_of_match(center, giver_row)

        return render_template("give_confirmation.html", area=center.get("Area", ""),
                                quantity_number=quantity, quantity_unit=quantity_unit,
                                food_type=food_type, meal_slot=meal_slot,
                                ready_date=ready_date, matched_center=center.get("Center Name"))

    return render_template("give_to_place.html", center=center, form={}, is_maedet_rahman=is_maedet_rahman)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        remember_me = request.form.get("remember_me") == "on"

        if not phone:
            flash("من فضلك اكتب رقم التليفون.")
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

    givers_ws = get_worksheet(GIVERS_TAB, GIVERS_HEADER)
    givers_header = ensure_columns(givers_ws, GIVERS_HEADER)
    all_values = givers_ws.get_all_values()
    phone_i = col_index(givers_header, "Phone")

    my_donations = []
    for row_num, row in enumerate(all_values[1:], start=2):
        if len(row) >= phone_i and row[phone_i - 1].strip() == user["Phone"].strip():
            donation = read_row(givers_ws, givers_header, row_num)
            donation["_row"] = row_num
            my_donations.append(donation)
    my_donations.reverse()  # most recent first

    return render_template("my_activity.html", user=user, donations=my_donations)


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
        contact_email = request.form.get("contact_email", "").strip()
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
            errors.append("من فضلك املا كل حقول الموقع المطلوبة.")
        if not submitter_role or not submitter_name or not submitter_phone:
            errors.append("من فضلك اكتب اسمك، رقمك، ودورك.")
        if not contact_name or not contact_phone:
            errors.append("من فضلك اكتب اسم ورقم الشخص المسؤول في المكان.")
        if not receives:
            errors.append("من فضلك اختار إيه اللي المكان بيستقبله: وجبات، مواد غذائية، أو الاتنين.")
        if has_capacity_limit == "Yes" and not capacity_per_slot:
            errors.append("من فضلك اكتب السقف لكل فترة، أو اختار \"لأ\" لو مفيش سقف ثابت.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_center.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ensure_columns(ws, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, bio, center_type, governorate, ownership_type,
            area, address, maps_link, social_link,
            target_group, beneficiaries, staff_members, total_to_feed, preferred_meal_type,
            submitter_role, submitter_name, submitter_phone,
            contact_name, contact_phone, contact_email, ritual_schedule,
            receives_meals, meal_slots, days_open,
            receives_groceries, grocery_hours,
            has_capacity_limit, capacity_per_slot,
            photo_url, "Unverified", "", 0,
        ])
        new_center_row = len(ws.get_all_values())

        notify_waitlist_for_area(area, center_name, "Center")

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_center.html", form={})


@app.route("/register-maedet-rahman", methods=["GET", "POST"])
def register_maedet_rahman():
    if request.method == "POST":
        center_name = request.form.get("center_name", "").strip()
        bio = request.form.get("bio", "").strip()
        center_type = MAEDET_RAHMAN_TYPE

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
        # An open table has no defined beneficiary list, so these stay blank -
        # they're specific to limited-beneficiary institutions (register_center).
        target_group = ""
        beneficiaries = ""
        staff_members = ""
        total_to_feed = ""
        preferred_meal_type = ""

        submitter_role = request.form.get("submitter_role", "")
        submitter_name = request.form.get("submitter_name", "").strip()
        submitter_phone = request.form.get("submitter_phone", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        contact_phone = request.form.get("contact_phone", "").strip()
        contact_email = request.form.get("contact_email", "").strip()
        ritual_schedule = request.form.get("ritual_schedule", "").strip()

        receives = request.form.getlist("receives")
        receives_meals = "Yes" if "Meals" in receives else "No"
        receives_groceries = "Yes" if "Groceries" in receives else "No"
        meal_slots = ", ".join(request.form.getlist("meal_slots"))
        days_open = ", ".join(request.form.getlist("days_open"))
        grocery_hours = request.form.get("grocery_hours", "").strip()

        has_capacity_limit = request.form.get("has_capacity_limit", "No")
        capacity_per_slot = request.form.get("capacity_per_slot", "").strip()

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
            errors.append("من فضلك املا كل حقول الموقع المطلوبة.")
        if not submitter_role or not submitter_name or not submitter_phone:
            errors.append("من فضلك اكتب اسمك، رقمك، ودورك.")
        if not contact_name or not contact_phone:
            errors.append("من فضلك اكتب اسم ورقم الشخص المسؤول في المكان.")
        if not receives:
            errors.append("من فضلك اختار إيه اللي المائدة بتستقبله: وجبات، مواد غذائية، أو الاتنين.")
        if has_capacity_limit == "Yes" and not capacity_per_slot:
            errors.append("من فضلك اكتب السقف التقريبي، أو اختار \"لأ\" لو المائدة مفتوحة من غير سقف.")

        if errors:
            for e in errors:
                flash(e)
            return render_template("register_maedet_rahman.html", form=request.form)

        ws = get_worksheet(CENTERS_TAB, CENTERS_HEADER)
        ensure_columns(ws, CENTERS_HEADER)
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            center_name, bio, center_type, governorate, ownership_type,
            area, address, maps_link, social_link,
            target_group, beneficiaries, staff_members, total_to_feed, preferred_meal_type,
            submitter_role, submitter_name, submitter_phone,
            contact_name, contact_phone, contact_email, ritual_schedule,
            receives_meals, meal_slots, days_open,
            receives_groceries, grocery_hours,
            has_capacity_limit, capacity_per_slot,
            photo_url, "Unverified", "", 0,
        ])
        new_center_row = len(ws.get_all_values())

        notify_waitlist_for_area(area, center_name, "Ma'edet Rahman")

        return render_template("center_confirmation.html", center_name=center_name)

    return render_template("register_maedet_rahman.html", form={})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
