"""
FPL AI Advisor — Recommendation API (Phase 4)

FastAPI service deployed on Railway.
n8n calls POST /run-weekly-batch every Thursday at 9 PM.
For each registered user, this service:
  1. Fetches their FPL squad + fixtures + injury news (from FPL public API)
  2. Builds a single structured prompt
  3. Makes ONE Claude call per user (no agentic loop — ~80% fewer tokens)
  4. Sends an HTML email via Gmail SMTP

Endpoints:
  GET  /health              — keep-alive ping (UptimeRobot uses this)
  POST /run-weekly-batch    — n8n triggers this every Thursday
  GET  /unsubscribe         — email opt-out link
"""

import json
import os
import re
import time
import traceback

import resend

import anthropic
import gspread
import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from google.oauth2.service_account import Credentials

# ── FPL API endpoints ──────────────────────────────────────────────────────────
BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
PICKS_URL     = "https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/"
FIXTURES_URL  = "https://fantasy.premierleague.com/api/fixtures/"

# FPL numeric codes for position and availability
POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
STATUS_MAP = {
    "a": "Available",
    "d": "Doubtful",
    "i": "Injured",
    "s": "Suspended",
    "u": "Unavailable",
    "n": "Not in squad",
}

# Google Sheets OAuth scopes
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


# ── HTTP helper ────────────────────────────────────────────────────────────────

def fetch_json(url: str) -> dict:
    """GET a URL and return parsed JSON. Raises on HTTP error."""
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


# ── Google Sheets helper ───────────────────────────────────────────────────────

def get_sheet():
    """Connect to the Google Sheet using the service account from env vars."""
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ["GOOGLE_SHEET_ID"]).sheet1


# ── FPL data helpers ───────────────────────────────────────────────────────────

def get_current_gameweek(events: list) -> int:
    """Return the active gameweek number from the bootstrap events list."""
    for event in events:
        if event.get("is_current"):
            return event["id"]
    finished = [e for e in events if e.get("finished")]
    if finished:
        return finished[-1]["id"]
    raise ValueError("Could not determine the current gameweek.")


def build_player_lookup(elements: list, teams: list) -> dict:
    """
    Build a dict keyed by player ID with all stats we need.
    Includes team_id (integer) for fixture lookup, and team (short name string).
    """
    team_lookup = {t["id"]: t["short_name"] for t in teams}
    return {
        player["id"]: {
            "name":         player["web_name"],
            "position":     POSITION_MAP.get(player["element_type"], "???"),
            "team":         team_lookup.get(player["team"], "???"),
            "team_id":      player["team"],           # integer — used for fixture lookup
            "price":        player["now_cost"] / 10,  # convert tenths → £m
            "total_points": player["total_points"],
            "form":         float(player.get("form") or 0),
            "minutes":      player["minutes"],
            "status":       STATUS_MAP.get(player["status"], player["status"]),
            "news":         player.get("news", ""),
        }
        for player in elements
    }


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_prompt(
    fpl_id: int,
    manager_name: str,
    team_name: str,
    bootstrap_data: dict,
    fixtures_data: list,
) -> tuple[str, int]:
    """
    Assemble all FPL data into a single structured prompt string.
    Returns (prompt_text, current_gameweek).

    Sections in the prompt:
      1. Squad (15 players with stats, captaincy flags, bench order)
      2. Upcoming fixtures for the user's clubs (3 GWs, blank GW labelled)
      3. Availability concerns in the squad
      4. Top transfer targets per position (form × fixture score)
    """
    events   = bootstrap_data["events"]
    elements = bootstrap_data["elements"]
    teams    = bootstrap_data["teams"]

    current_gw    = get_current_gameweek(events)
    player_lookup = build_player_lookup(elements, teams)
    team_by_id    = {t["id"]: t["short_name"] for t in teams}

    # ── Fetch user squad ───────────────────────────────────────────────────────
    picks_data = fetch_json(PICKS_URL.format(team_id=fpl_id, gw=current_gw))
    picks      = picks_data["picks"]

    squad_info   = []   # enriched player dicts
    squad_names  = set()
    squad_team_ids = set()

    for pick in picks:
        player = player_lookup.get(pick["element"])
        if not player:
            continue
        entry = {
            **player,
            "is_captain":      pick["is_captain"],
            "is_vice_captain": pick["is_vice_captain"],
            "bench_pos":       pick["position"],   # 1-11 = starter, 12-15 = bench
        }
        squad_info.append(entry)
        squad_names.add(player["name"])
        squad_team_ids.add(player["team_id"])

    # ── Section 1: Squad ──────────────────────────────────────────────────────
    squad_lines = [
        f"## YOUR SQUAD — {manager_name} | {team_name} | GW{current_gw}\n"
    ]

    starters = [p for p in squad_info if p["bench_pos"] <= 11]
    bench    = sorted([p for p in squad_info if p["bench_pos"] > 11],
                      key=lambda x: x["bench_pos"])

    squad_lines.append("### STARTING XI")
    for p in starters:
        role   = " [C]" if p["is_captain"] else " [V]" if p["is_vice_captain"] else ""
        status = f" | {p['status']}" if p["status"] != "Available" else ""
        news   = f" | {p['news']}"   if p["news"]                  else ""
        squad_lines.append(
            f"  {p['position']} {p['name']}{role} ({p['team']}) "
            f"£{p['price']:.1f}m | {p['total_points']}pts | Form:{p['form']:.1f}"
            f"{status}{news}"
        )

    squad_lines.append("\n### BENCH (priority order)")
    for p in bench:
        status = f" | {p['status']}" if p["status"] != "Available" else ""
        news   = f" | {p['news']}"   if p["news"]                  else ""
        squad_lines.append(
            f"  {p['position']} {p['name']} ({p['team']}) "
            f"£{p['price']:.1f}m | {p['total_points']}pts | Form:{p['form']:.1f}"
            f"{status}{news}"
        )

    # ── Section 2: Fixtures for squad teams (next 3 GWs) ─────────────────────
    next_gws = [current_gw + 1, current_gw + 2, current_gw + 3]

    # Build per-team, per-GW fixture map
    team_gw_fixtures: dict[int, dict[int, str]] = {}
    for f in fixtures_data:
        if f.get("finished"):
            continue
        event = f.get("event")
        if event not in next_gws:
            continue
        for team_id, opp_id, is_home in [
            (f["team_h"], f["team_a"], True),
            (f["team_a"], f["team_h"], False),
        ]:
            if team_id not in squad_team_ids:
                continue
            opp = team_by_id.get(opp_id, "???")
            fdr = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
            venue = "H" if is_home else "A"
            team_gw_fixtures.setdefault(team_id, {})[event] = (
                f"GW{event}: {opp}({venue}) FDR:{fdr}/5"
            )

    fixture_lines = ["\n## UPCOMING FIXTURES (next 3 GWs) for your squad's clubs\n"]
    for team_id in sorted(squad_team_ids):
        short = team_by_id.get(team_id, "???")
        parts = [f"  {short}:"]
        for gw in next_gws:
            gw_data = team_gw_fixtures.get(team_id, {})
            if gw in gw_data:
                parts.append(gw_data[gw])
            else:
                parts.append(f"[BLANK GW{gw}]")
        fixture_lines.append("  ".join(parts))

    # ── Section 3: Availability concerns in the squad ─────────────────────────
    concerns = [p for p in squad_info if p["status"] != "Available"]
    injury_lines = ["\n## SQUAD AVAILABILITY CONCERNS\n"]
    if concerns:
        for p in concerns:
            injury_lines.append(
                f"  [{p['status']}] {p['name']} ({p['team']}) — {p['news']}"
            )
    else:
        injury_lines.append("  All 15 players are currently available.")

    # ── Section 4: Top transfer targets (form × fixture score) ────────────────
    # fixture_score = 6 - avg FDR of next 3 GWs (higher score = easier run)
    team_avg_fdr: dict[int, float] = {}
    for tid in range(1, 21):
        fdrs = []
        for f in fixtures_data:
            if f.get("finished"):
                continue
            event = f.get("event")
            if event not in next_gws:
                continue
            if f["team_h"] == tid:
                fdrs.append(f["team_h_difficulty"])
            elif f["team_a"] == tid:
                fdrs.append(f["team_a_difficulty"])
        while len(fdrs) < 3:
            fdrs.append(5)   # blank GW treated as hardest difficulty
        team_avg_fdr[tid] = sum(fdrs) / 3

    target_lines = ["\n## TOP TRANSFER TARGETS (not in your squad)\n"]
    for position in ("GKP", "DEF", "MID", "FWD"):
        candidates = []
        for player in player_lookup.values():
            if player["position"] != position:
                continue
            if player["name"] in squad_names:
                continue
            if player["status"] not in ("Available", "Doubtful"):
                continue
            if player["minutes"] < 100:   # filter out rarely-playing players
                continue
            avg_fdr       = team_avg_fdr.get(player["team_id"], 3.0)
            fixture_score = 6.0 - avg_fdr  # 1-5 scale (higher = easier)
            score         = player["form"] * fixture_score
            candidates.append({**player, "score": score, "avg_fdr": avg_fdr})

        candidates.sort(key=lambda x: x["score"], reverse=True)
        target_lines.append(f"### {position} targets")
        for t in candidates[:5]:
            target_lines.append(
                f"  {t['name']} ({t['team']}) £{t['price']:.1f}m | "
                f"Form:{t['form']:.1f} | {t['total_points']}pts | "
                f"Avg FDR next 3: {t['avg_fdr']:.1f}/5"
            )
        target_lines.append("")

    # ── Assemble full prompt ───────────────────────────────────────────────────
    instructions = (
        "You are an expert Fantasy Premier League (FPL) advisor. "
        "All data below is live and pre-fetched for this user. "
        "Analyse the squad and provide a concise, actionable weekly report "
        "in exactly these 4 sections:\n\n"
        "1. CAPTAINCY PICK — recommend captain and vice-captain with reasoning\n"
        "2. TRANSFER SUGGESTIONS — up to 2 transfers (sell → buy), with reasoning\n"
        "3. BENCH ORDER — optimal bench priority (positions 12-15)\n"
        "4. OVERALL VERDICT — 3-5 bullet points on squad strengths and risks\n\n"
        "Be direct, data-driven, and keep each section under 100 words. "
        "Note any blank gameweeks explicitly in your advice.\n\n"
    )

    data_sections = squad_lines + fixture_lines + injury_lines + target_lines
    prompt = instructions + "\n".join(data_sections)
    return prompt, current_gw


# ── Claude call ────────────────────────────────────────────────────────────────

def generate_recommendation(
    fpl_id: int,
    manager_name: str,
    team_name: str,
    bootstrap_data: dict,
    fixtures_data: list,
) -> dict:
    """
    Build the prompt and make a single Claude call.
    Returns a dict with raw text, parsed sections, and gameweek number.
    """
    prompt, current_gw = build_prompt(
        fpl_id, manager_name, team_name, bootstrap_data, fixtures_data
    )

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Retry up to 3 times with exponential backoff (handles 529 overloaded + network blips)
    delays = [5, 15, 30]
    last_error = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system="You are an expert FPL advisor. Be concise and actionable.",
                messages=[{"role": "user", "content": prompt}],
            )
            break  # success — exit retry loop
        except (anthropic.APIStatusError, anthropic.APIConnectionError, OSError) as e:
            last_error = e
            print(f"[Retry {attempt}/3] Claude error: {e}. Waiting {delay}s...")
            time.sleep(delay)
    else:
        raise last_error  # all 3 attempts failed

    raw_text = response.content[0].text
    sections = parse_sections(raw_text)

    return {
        "raw":          raw_text,
        "sections":     sections,
        "gameweek":     current_gw,
        "manager_name": manager_name,
        "team_name":    team_name,
    }


def parse_sections(text: str) -> dict:
    """Extract the 4 labelled sections from Claude's response text."""
    sections = {"captaincy": "", "transfers": "", "bench": "", "verdict": ""}
    patterns = [
        ("captaincy", r"1\.\s*CAPTAINCY PICK(.*?)(?=2\.\s*TRANSFER|$)"),
        ("transfers", r"2\.\s*TRANSFER SUGGESTIONS?(.*?)(?=3\.\s*BENCH|$)"),
        ("bench",     r"3\.\s*BENCH ORDER(.*?)(?=4\.\s*OVERALL|$)"),
        ("verdict",   r"4\.\s*OVERALL VERDICT(.*?)$"),
    ]
    for key, pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            sections[key] = match.group(1).strip()
    return sections


# ── HTML email builder ─────────────────────────────────────────────────────────

def build_html_email(rec: dict, fpl_id: int) -> str:
    """
    Build an HTML email from the recommendation dict.
    Uses dark-green header, white body, 4 colour-coded sections.
    All styles are inline (Outlook-compatible).
    """
    gw      = rec["gameweek"]
    manager = rec["manager_name"]
    team    = rec["team_name"]
    s       = rec["sections"]

    api_base_url    = os.environ.get("API_BASE_URL", "https://your-app.railway.app")
    unsubscribe_url = f"{api_base_url}/unsubscribe?fpl_id={fpl_id}"

    def nl2br(text: str) -> str:
        return text.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,0.1);">

      <!-- Header -->
      <tr>
        <td style="background:#1a472a;padding:24px 32px;">
          <h1 style="color:#ffffff;margin:0;font-size:22px;">
            &#9917; FPL AI Advisor &#8212; Gameweek {gw}
          </h1>
          <p style="color:#a8d5b5;margin:6px 0 0;font-size:14px;">
            Weekly analysis for {manager} ({team})
          </p>
        </td>
      </tr>

      <!-- Section 1: Captaincy -->
      <tr>
        <td style="padding:24px 32px 0;">
          <h2 style="color:#1a472a;font-size:16px;margin:0 0 10px;
                     border-bottom:2px solid #1a472a;padding-bottom:6px;">
            1. &#127919; Captaincy Pick
          </h2>
          <p style="color:#333;font-size:14px;line-height:1.6;margin:0;">
            {nl2br(s.get("captaincy", ""))}
          </p>
        </td>
      </tr>

      <!-- Section 2: Transfers -->
      <tr>
        <td style="padding:20px 32px 0;">
          <h2 style="color:#c0392b;font-size:16px;margin:0 0 10px;
                     border-bottom:2px solid #c0392b;padding-bottom:6px;">
            2. &#128260; Transfer Suggestions
          </h2>
          <p style="color:#333;font-size:14px;line-height:1.6;margin:0;">
            {nl2br(s.get("transfers", ""))}
          </p>
        </td>
      </tr>

      <!-- Section 3: Bench Order -->
      <tr>
        <td style="padding:20px 32px 0;">
          <h2 style="color:#2980b9;font-size:16px;margin:0 0 10px;
                     border-bottom:2px solid #2980b9;padding-bottom:6px;">
            3. &#129681; Bench Order
          </h2>
          <p style="color:#333;font-size:14px;line-height:1.6;margin:0;">
            {nl2br(s.get("bench", ""))}
          </p>
        </td>
      </tr>

      <!-- Section 4: Overall Verdict -->
      <tr>
        <td style="padding:20px 32px 0;">
          <h2 style="color:#8e44ad;font-size:16px;margin:0 0 10px;
                     border-bottom:2px solid #8e44ad;padding-bottom:6px;">
            4. &#128202; Overall Verdict
          </h2>
          <p style="color:#333;font-size:14px;line-height:1.6;margin:0;">
            {nl2br(s.get("verdict", ""))}
          </p>
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="padding:24px 32px;background:#f8f8f8;border-top:1px solid #eee;
                   margin-top:24px;">
          <p style="color:#aaa;font-size:12px;margin:0;text-align:center;">
            Built with Claude AI + FPL public API &#8226;
            Delivered every Thursday at 9 PM<br>
            <a href="{unsubscribe_url}"
               style="color:#aaa;text-decoration:underline;">Unsubscribe</a>
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str) -> None:
    """Send an HTML email via Resend HTTP API (Railway blocks SMTP ports)."""
    resend.api_key = os.environ["RESEND_API_KEY"]
    resend.Emails.send({
        "from": "FPL AI Advisor <onboarding@resend.dev>",
        "to":   to_email,
        "subject": subject,
        "html": html_body,
    })


# ── Background batch task ──────────────────────────────────────────────────────

def process_all_users() -> None:
    """
    Background task: read every row from Google Sheet, generate a
    recommendation for each user, build the HTML email, and send it.

    Called by n8n via POST /run-weekly-batch every Thursday at 9 PM.
    Runs in the background so Railway doesn't timeout on the HTTP call.
    Errors for individual users are logged but don't stop the batch.
    """
    print("[Batch] Starting weekly batch...")

    # ── Fetch FPL data once — shared across all users ──────────────────────
    bootstrap     = fetch_json(BOOTSTRAP_URL)
    fixtures_data = fetch_json(FIXTURES_URL)

    # ── Read all registered users from Google Sheet ────────────────────────
    sheet   = get_sheet()
    records = sheet.get_all_records()
    print(f"[Batch] Processing {len(records)} users...")

    success_count = 0
    error_count   = 0

    for row in records:
        fpl_id       = row.get("FPL_ID")
        email        = row.get("Email")
        manager_name = row.get("Manager_Name") or "Manager"
        team_name    = row.get("Team_Name")    or "Your Team"

        if not fpl_id or not email:
            continue

        try:
            rec = generate_recommendation(
                int(fpl_id), manager_name, team_name,
                bootstrap, fixtures_data,
            )
            html    = build_html_email(rec, int(fpl_id))
            subject = (
                f"FPL AI Advisor — GW{rec['gameweek']} Tips "
                f"for {team_name} \u26bd"
            )
            send_email(email, subject, html)
            print(f"[OK]    Sent to {email} (FPL ID: {fpl_id})")
            success_count += 1

        except Exception as exc:
            print(f"[ERROR] FPL ID {fpl_id} ({email}): {exc}")
            print(traceback.format_exc())   # stdout so Railway logs show it
            error_count += 1

    print(
        f"[Batch] Done. {success_count} sent, {error_count} failed "
        f"out of {len(records)} users."
    )


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="FPL AI Advisor API", version="1.0.0")


@app.get("/health")
def health():
    """Keep-alive endpoint. UptimeRobot pings this every 5 minutes."""
    return {"status": "ok"}


@app.post("/run-weekly-batch")
async def run_weekly_batch(background_tasks: BackgroundTasks):
    """
    n8n calls this every Thursday at 9 PM.
    Immediately returns 202 Accepted; the batch runs in the background.
    """
    background_tasks.add_task(process_all_users)
    return {
        "status":  "started",
        "message": "Weekly batch is running. Check server logs for progress.",
    }


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(fpl_id: int):
    """
    Opt-out endpoint linked from every email footer.
    Finds the user's row by FPL_ID and deletes it from the sheet.
    """
    try:
        sheet   = get_sheet()
        records = sheet.get_all_records()

        for i, row in enumerate(records, start=2):   # row 1 = headers
            if str(row.get("FPL_ID")) == str(fpl_id):
                sheet.delete_rows(i)
                return """
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial;text-align:center;padding:60px;">
  <h2 style="color:#1a472a;">&#10003; You've been unsubscribed</h2>
  <p>FPL ID <strong>{fpl_id}</strong> has been removed.<br>
     No more emails from FPL AI Advisor.</p>
</body>
</html>""".format(fpl_id=fpl_id)

        return """
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial;text-align:center;padding:60px;">
  <h2>FPL ID not found</h2>
  <p>This team ID wasn't in our list &#8212;
     you may already be unsubscribed.</p>
</body>
</html>"""

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
