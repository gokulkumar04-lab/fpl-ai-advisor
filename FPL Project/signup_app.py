"""
FPL AI Advisor — Sign-Up Page
Streamlit app deployed on Streamlit Community Cloud.

Users register their FPL Team ID + email to receive weekly AI-powered
transfer recommendations every Thursday night.
"""

import json
from datetime import datetime, timezone

import gspread
import requests
import streamlit as st
from google.oauth2.service_account import Credentials

# ── Google Sheets helpers ──────────────────────────────────────────────────────

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


@st.cache_resource(show_spinner=False)
def get_sheet():
    """
    Connect to Google Sheets using service account credentials stored in
    Streamlit secrets. Cached so we reuse the connection across reruns.
    """
    creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["GOOGLE_SHEET_ID"]).sheet1


def is_duplicate(sheet, fpl_id: int) -> bool:
    """Return True if the FPL ID is already registered in the sheet."""
    try:
        records = sheet.get_all_records()
        return any(str(r.get("FPL_ID")) == str(fpl_id) for r in records)
    except gspread.exceptions.APIError:
        return False


def register_user(
    sheet,
    fpl_id: int,
    email: str,
    manager_name: str,
    team_name: str,
) -> None:
    """Append a new row to the Google Sheet."""
    timestamp = datetime.now(timezone.utc).isoformat()
    sheet.append_row([fpl_id, email, manager_name, team_name, timestamp])


# ── FPL API helpers ────────────────────────────────────────────────────────────

_FPL_ENTRY_URL = "https://fantasy.premierleague.com/api/entry/{}/"


def validate_fpl_id(fpl_id: int) -> dict | None:
    """
    Call the FPL API to validate the team ID.
    Returns {"manager_name": str, "team_name": str} on success, None on failure.
    """
    try:
        response = requests.get(_FPL_ENTRY_URL.format(fpl_id), timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        manager_name = f"{data['player_first_name']} {data['player_last_name']}"
        team_name = data["name"]
        return {"manager_name": manager_name, "team_name": team_name}
    except (requests.RequestException, KeyError, ValueError):
        return None


# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="FPL AI Advisor",
    page_icon="⚽",
    layout="centered",
)

st.title("⚽ FPL AI Advisor")
st.subheader("Get personalised weekly transfer advice — delivered every Thursday night.")
st.write(
    "Enter your FPL Team ID and email below. Every Thursday, our AI will analyse your squad "
    "and send you tailored captain picks, transfer suggestions, and bench order advice."
)

st.divider()

with st.form("signup_form", clear_on_submit=False):
    fpl_id_raw = st.text_input(
        "FPL Team ID",
        placeholder="e.g. 6723617",
        help="Find your team ID at fantasy.premierleague.com → Points → click your team name → copy the number from the URL.",
    )
    email = st.text_input(
        "Email Address",
        placeholder="you@example.com",
    )
    submitted = st.form_submit_button("Sign Me Up →", use_container_width=True)

if submitted:
    # ── Validate FPL ID is a positive integer ─────────────────────────────────
    try:
        fpl_id = int(fpl_id_raw.strip())
        if fpl_id <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        st.error("Please enter a valid FPL Team ID (numbers only, e.g. 6723617).")
        st.stop()

    # ── Basic email validation ─────────────────────────────────────────────────
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        st.error("Please enter a valid email address.")
        st.stop()

    # ── Validate FPL ID against the official API ───────────────────────────────
    with st.spinner("Checking your FPL ID..."):
        info = validate_fpl_id(fpl_id)

    if info is None:
        st.error(
            f"FPL Team ID **{fpl_id}** wasn't found. "
            "Double-check your ID at fantasy.premierleague.com."
        )
        st.stop()

    manager_name = info["manager_name"]
    team_name = info["team_name"]

    # ── Check for duplicate registration ──────────────────────────────────────
    with st.spinner("Checking registration..."):
        sheet = get_sheet()
        if is_duplicate(sheet, fpl_id):
            st.warning(
                f"**{team_name}** (managed by {manager_name}) is already registered! "
                "Check your inbox every Thursday night. ✉️"
            )
            st.stop()

    # ── Register the user ─────────────────────────────────────────────────────
    with st.spinner("Registering you..."):
        register_user(sheet, fpl_id, email, manager_name, team_name)

    st.success(
        f"You're in, **{manager_name}**! (**{team_name}**) \n\n"
        "Expect your first AI-powered analysis this Thursday at 9 PM. ⚽"
    )
    st.balloons()

st.divider()
st.caption("Built with Claude AI + FPL public API • Emails sent every Thursday night")
