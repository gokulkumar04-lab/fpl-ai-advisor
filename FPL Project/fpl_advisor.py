"""
FPL AI Advisor - Squad Fetcher + Claude AI Analysis
Fetches a user's FPL squad, enriches it with live stats, and runs it
through Claude for transfer suggestions, captaincy picks, and more.
"""

import os
import sys
import io
import anthropic
import requests
from dotenv import load_dotenv

# Load ANTHROPIC_API_KEY from .env file if present (falls back to system env var)
load_dotenv()

# Fix Windows console encoding so £ and accented player names display correctly
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── API endpoints ──────────────────────────────────────────────────────────────
BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
PICKS_URL     = "https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/"

# FPL uses numeric codes for player positions and availability status
POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
STATUS_MAP   = {
    "a": "Available",
    "d": "Doubtful",
    "i": "Injured",
    "s": "Suspended",
    "u": "Unavailable",
    "n": "Not in squad",
}

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def fetch_json(url: str) -> dict:
    """GET a URL and return parsed JSON, or exit on failure."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        print(f"[HTTP error] {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("[Error] Could not reach the FPL API. Check your internet connection.")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("[Error] Request timed out.")
        sys.exit(1)


# ── Bootstrap data helpers ─────────────────────────────────────────────────────

def get_current_gameweek(events: list) -> int:
    """
    Find the current (live) gameweek from the bootstrap events list.
    Falls back to the most recent finished gameweek if none is marked current.
    """
    for event in events:
        if event.get("is_current"):
            return event["id"]

    # Edge case: between gameweeks, pick the last finished one
    finished = [e for e in events if e.get("finished")]
    if finished:
        return finished[-1]["id"]

    raise ValueError("Could not determine the current gameweek.")


def build_player_lookup(elements: list, teams: list) -> dict:
    """
    Build a dict keyed by player ID containing the stats we care about.
    'now_cost' is stored as tenths of a million by FPL (e.g. 65 -> £6.5m).
    """
    team_lookup = {t["id"]: t["short_name"] for t in teams}

    return {
        player["id"]: {
            "name":         player["web_name"],
            "full_name":    f"{player['first_name']} {player['second_name']}",
            "position":     POSITION_MAP.get(player["element_type"], "???"),
            "team":         team_lookup.get(player["team"], "???"),
            "price":        player["now_cost"] / 10,          # convert to £m
            "total_points": player["total_points"],
            "form":         player["form"],                   # rolling avg (string)
            "minutes":      player["minutes"],
            "status":       STATUS_MAP.get(player["status"], player["status"]),
            "news":         player.get("news", ""),           # injury / suspension note
        }
        for player in elements
    }


# ── Display helpers ────────────────────────────────────────────────────────────

def format_player_row(pick: dict, player: dict) -> str:
    """Build a single formatted line for one player."""
    # Captain / vice-captain badges
    badge = ""
    if pick["is_captain"]:
        badge = " (C)"
    elif pick["is_vice_captain"]:
        badge = " (V)"

    name_col   = f"{player['name']}{badge}"
    pos_col    = f"[{player['position']}]"
    team_col   = player["team"]
    price_col  = f"£{player['price']:.1f}m"
    pts_col    = f"{player['total_points']}pts"
    form_col   = f"Form:{player['form']}"
    mins_col   = f"{player['minutes']}min"
    status_col = player["status"]

    # Show injury/suspension news if there is any
    news_str = f"  ** {player['news']}" if player["news"] else ""

    return (
        f"  {pos_col:<5} {name_col:<26} {team_col:<5} "
        f"{price_col:<8} {pts_col:<8} {form_col:<12} {mins_col:<9} {status_col}"
        f"{news_str}"
    )


def print_squad(picks: list, player_lookup: dict, gameweek: int, team_id: int) -> None:
    """Print a formatted squad summary split into Starting XI and Bench."""

    # FPL positions 1-11 = starters, 12-15 = bench (ordered by bench priority)
    starters = [p for p in picks if p["position"] <= 11]
    bench    = [p for p in picks if p["position"] > 11]

    header  = f"  {'POS':<5} {'Name':<26} {'Club':<5} {'Price':<8} {'Pts':<8} {'Form':<12} {'Mins':<9} Status"
    divider = "  " + "-" * 90

    print()
    print("=" * 94)
    print(f"  FPL Squad  |  Team ID: {team_id}  |  Gameweek {gameweek}")
    print("=" * 94)

    # ── Starting XI ───────────────────────────────────────────────────────────
    print("\n  -- STARTING XI --")
    print(header)
    print(divider)
    for pick in starters:
        player = player_lookup.get(pick["element"])
        if player:
            print(format_player_row(pick, player))

    # ── Bench ─────────────────────────────────────────────────────────────────
    print("\n  -- BENCH --")
    print(header)
    print(divider)
    for pick in sorted(bench, key=lambda p: p["position"]):
        player = player_lookup.get(pick["element"])
        if player:
            print(format_player_row(pick, player))

    # ── Quick stats ───────────────────────────────────────────────────────────
    all_players = [player_lookup[p["element"]] for p in picks if p["element"] in player_lookup]
    squad_value = sum(p["price"] for p in all_players)
    total_pts   = sum(p["total_points"] for p in all_players)
    unavailable = [p for p in all_players if p["status"] != "Available"]

    print()
    print("-" * 94)
    print(f"  Squad value : £{squad_value:.1f}m")
    print(f"  Total pts   : {total_pts} (sum of all 15 players this season)")
    if unavailable:
        print(f"  Concerns    : {len(unavailable)} player(s) not fully available")
        for p in unavailable:
            news = f" -- {p['news']}" if p["news"] else ""
            print(f"                {p['name']} ({p['status']}){news}")
    else:
        print("  Concerns    : None -- all players available")
    print("-" * 94)
    print()


# ── Bootstrap cache ────────────────────────────────────────────────────────────
# Populated once by main() so tool functions can access it without re-fetching.

_cache: dict = {}

FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"


# ── Tool implementations ───────────────────────────────────────────────────────

def tool_get_user_squad(fpl_id: int) -> str:
    """Fetch the user's current squad with enriched player stats."""
    gameweek      = _cache["gameweek"]
    player_lookup = _cache["player_lookup"]

    picks_data = fetch_json(PICKS_URL.format(team_id=fpl_id, gw=gameweek))
    picks      = picks_data["picks"]

    lines = [f"Squad for FPL ID {fpl_id} — Gameweek {gameweek}:\n"]
    for title, section_picks in [
        ("STARTING XI", [p for p in picks if p["position"] <= 11]),
        ("BENCH",       sorted([p for p in picks if p["position"] > 11],
                               key=lambda p: p["position"])),
    ]:
        lines.append(f"--- {title} ---")
        for pick in section_picks:
            p = player_lookup.get(pick["element"])
            if not p:
                continue
            role = " [C]" if pick["is_captain"] else " [V]" if pick["is_vice_captain"] else ""
            status_note = f" | {p['status']}" if p["status"] != "Available" else ""
            news_note   = f" | {p['news']}"   if p["news"]                  else ""
            lines.append(
                f"  {p['position']} {p['name']}{role} ({p['team']}) "
                f"£{p['price']:.1f}m | {p['total_points']}pts | "
                f"Form:{p['form']} | {p['minutes']}min"
                f"{status_note}{news_note}"
            )
        lines.append("")

    return "\n".join(lines)


def tool_get_fixtures(team_short_name: str) -> str:
    """Return the next 5 upcoming fixtures for a team with FDR and home/away."""
    # Lazy-load fixtures on first call and cache them
    if "fixtures" not in _cache:
        _cache["fixtures"] = fetch_json(FIXTURES_URL)

    fixtures       = _cache["fixtures"]
    team_by_id     = _cache["team_by_id"]      # id  -> short_name
    team_id_by_short = _cache["team_id_by_short"]  # short_name -> id
    current_gw     = _cache["gameweek"]

    # Case-insensitive team lookup
    team_id = None
    for short, tid in team_id_by_short.items():
        if short.upper() == team_short_name.strip().upper():
            team_id = tid
            break

    if team_id is None:
        available = ", ".join(sorted(team_id_by_short.keys()))
        return f"Team '{team_short_name}' not found. Valid short names: {available}"

    upcoming = []
    for f in fixtures:
        if f.get("finished"):
            continue
        event = f.get("event")
        if event is None or event < current_gw:
            continue

        if f["team_h"] == team_id:
            opponent = team_by_id.get(f["team_a"], "???")
            fdr      = f["team_h_difficulty"]
            upcoming.append((event, f"GW{event}: vs {opponent} (H) — FDR {fdr}/5"))
        elif f["team_a"] == team_id:
            opponent = team_by_id.get(f["team_h"], "???")
            fdr      = f["team_a_difficulty"]
            upcoming.append((event, f"GW{event}: vs {opponent} (A) — FDR {fdr}/5"))

    upcoming.sort(key=lambda x: x[0])
    next5 = upcoming[:5]

    if not next5:
        return f"No upcoming fixtures found for {team_short_name.upper()}."

    lines = [f"Next {len(next5)} fixtures for {team_short_name.upper()}:"]
    for _, desc in next5:
        lines.append(f"  {desc}")
    return "\n".join(lines)


def tool_get_injury_news() -> str:
    """Return all Premier League players who are not fully available."""
    player_lookup = _cache["player_lookup"]

    unavailable = [p for p in player_lookup.values() if p["status"] != "Available"]

    if not unavailable:
        return "No injury or availability concerns — all players are available."

    # Sort by severity then team
    order = {"Injured": 0, "Suspended": 1, "Doubtful": 2, "Unavailable": 3}
    unavailable.sort(key=lambda p: (order.get(p["status"], 9), p["team"]))

    lines = [f"Players with availability concerns ({len(unavailable)} total):\n"]
    for p in unavailable:
        news = f" — {p['news']}" if p["news"] else ""
        lines.append(
            f"  [{p['status']}] {p['name']} ({p['team']}, {p['position']}, "
            f"£{p['price']:.1f}m){news}"
        )
    return "\n".join(lines)


def tool_get_player_stats(player_name: str) -> str:
    """Search for a player by name and return their current FPL stats."""
    player_lookup = _cache["player_lookup"]
    query = player_name.lower().strip()

    matches = [
        p for p in player_lookup.values()
        if query in p["name"].lower() or query in p["full_name"].lower()
    ]

    if not matches:
        return f"No player found matching '{player_name}'."

    matches.sort(key=lambda p: p["total_points"], reverse=True)
    total_found = len(matches)
    matches     = matches[:5]   # cap at 5 results

    note  = f" (top 5 of {total_found})" if total_found > 5 else ""
    lines = [f"Player stats for '{player_name}'{note}:\n"]
    for p in matches:
        status_note = f" | {p['status']}" if p["status"] != "Available" else ""
        news_note   = f" | {p['news']}"   if p["news"]                  else ""
        lines.append(
            f"  {p['name']} ({p['team']}, {p['position']}) — "
            f"£{p['price']:.1f}m | {p['total_points']}pts | "
            f"Form:{p['form']} | {p['minutes']}min"
            f"{status_note}{news_note}"
        )
    return "\n".join(lines)


# ── Tool definitions (Anthropic tool use format) ───────────────────────────────

TOOLS = [
    {
        "name": "get_user_squad",
        "description": (
            "Fetches the user's current FPL squad for the active gameweek. "
            "Returns all 15 players with position, club, price, total points, form, "
            "minutes played, and any injury/availability news. "
            "Always call this first to understand the team you're analysing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fpl_id": {
                    "type": "integer",
                    "description": "The user's FPL team ID."
                }
            },
            "required": ["fpl_id"]
        }
    },
    {
        "name": "get_fixtures",
        "description": (
            "Returns the next 5 upcoming Premier League fixtures for a given team, "
            "including the opponent, home (H) or away (A) status, and Fixture Difficulty "
            "Rating (FDR, 1=easiest to 5=hardest). Use this to assess captaincy options "
            "and evaluate transfer targets based on upcoming schedule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_short_name": {
                    "type": "string",
                    "description": (
                        "The team's 3-letter FPL short name, e.g. 'LIV', 'ARS', "
                        "'MCI', 'CHE', 'TOT', 'MUN', 'NEW', 'AVL'."
                    )
                }
            },
            "required": ["team_short_name"]
        }
    },
    {
        "name": "get_injury_news",
        "description": (
            "Returns all Premier League players who are currently injured, doubtful, "
            "suspended, or otherwise unavailable, along with the official FPL news on "
            "their condition. Use this to spot urgent transfer needs in the user's squad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_player_stats",
        "description": (
            "Searches for a Premier League player by name and returns their current FPL "
            "stats: price, total points, recent form, minutes played, and availability. "
            "Use this to evaluate specific transfer targets before recommending them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {
                    "type": "string",
                    "description": (
                        "Player name or partial name to search, e.g. 'Salah', "
                        "'Palmer', 'Mbeumo', 'Isak'."
                    )
                }
            },
            "required": ["player_name"]
        }
    },
]


# ── Tool dispatcher ─────────────────────────────────────────────────────────────

def execute_tool(name: str, tool_input: dict) -> str:
    """Route a tool call from Claude to the correct Python function."""
    if name == "get_user_squad":
        return tool_get_user_squad(tool_input["fpl_id"])
    if name == "get_fixtures":
        return tool_get_fixtures(tool_input["team_short_name"])
    if name == "get_injury_news":
        return tool_get_injury_news()
    if name == "get_player_stats":
        return tool_get_player_stats(tool_input["player_name"])
    return f"[Error] Unknown tool: {name}"


# ── AI advisor (agentic tool-use loop) ────────────────────────────────────────

def run_ai_advisor(team_id: int, gameweek: int) -> None:
    """
    Starts a conversation with Claude and lets it autonomously call tools
    to gather FPL data before producing its final analysis.

    Loop structure:
      1. Send user prompt → Claude responds with tool_use blocks
      2. Execute each tool call, collect results
      3. Feed results back as tool_result blocks
      4. Repeat until stop_reason == 'end_turn' (Claude is done)
      5. Print the final text response
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n[Warning] ANTHROPIC_API_KEY is not set.")
        print("  Run:  set ANTHROPIC_API_KEY=your_key_here  (in your terminal)")
        print("Skipping AI analysis.\n")
        return

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = """You are an expert Fantasy Premier League (FPL) advisor.

You have live tools to fetch squad data, fixtures, injury news, and player stats.
A good analysis workflow:
  1. get_user_squad      — see the team
  2. get_injury_news     — spot any urgent concerns
  3. get_fixtures        — check upcoming difficulty for captaincy / transfer decisions
  4. get_player_stats    — evaluate specific transfer targets

After gathering enough data, write a structured report with exactly these four sections:

1. CAPTAINCY PICK
   Recommend captain and vice-captain. Justify with form + fixture difficulty.

2. TRANSFER SUGGESTIONS
   Up to 2 transfers (sell → buy). Give clear reasoning for each.

3. BENCH ORDER
   Optimal bench priority (positions 12-15) with brief justification.

4. OVERALL VERDICT
   3-5 bullet points on squad strengths and risks going forward.

Be direct, data-driven, and concise."""

    messages = [
        {
            "role": "user",
            "content": (
                f"Please analyse my FPL squad (team ID: {team_id}) for Gameweek {gameweek}. "
                "Use the tools to gather what you need, then give me your full recommendations."
            ),
        }
    ]

    print("\n" + "=" * 94)
    print(f"  Claude AI Advisor — Gameweek {gameweek}  (tool-use mode)")
    print("=" * 94)

    # ── Agentic loop ──────────────────────────────────────────────────────────
    turn = 0
    while True:
        turn += 1
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            thinking={"type": "adaptive"},   # let sonnet 4.6 decide how much to think
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Always append the full assistant response so tool_use blocks are preserved
        messages.append({"role": "assistant", "content": response.content})

        # ── Collect any tool_use blocks from this response ─────────────────────
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        # ── Claude finished or has no more tool calls — print final text ───────
        if response.stop_reason == "end_turn" or not tool_use_blocks:
            print()
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            if response.stop_reason == "max_tokens":
                print("\n[Note] Response was cut off (max_tokens reached).")
            print("\n" + "=" * 94 + "\n")
            break

        # ── Claude called tools — execute them and feed results back ──────────
        tool_results = []
        for block in tool_use_blocks:

            # Show the user which tool Claude is calling
            args_preview = str(block.input)
            print(f"\n  [Tool call #{turn}] {block.name}({args_preview})")

            result = execute_tool(block.name, block.input)

            # Print a short preview of the result
            preview = result.replace("\n", " ")[:120]
            print(f"  [Result]          {preview}{'...' if len(result) > 120 else ''}")

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,   # must match the tool_use block id
                "content":     result,
            })

        # Return all tool results to Claude in a single user turn
        messages.append({"role": "user", "content": tool_results})


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Get team ID from the user
    try:
        team_id = int(input("Enter your FPL team ID: ").strip())
    except ValueError:
        print("[Error] Team ID must be a number.")
        sys.exit(1)

    print("\nFetching FPL data...")

    # 2. Download bootstrap-static — needed for player/team lookups
    bootstrap = fetch_json(BOOTSTRAP_URL)
    events    = bootstrap["events"]
    elements  = bootstrap["elements"]
    teams     = bootstrap["teams"]

    # 3. Determine active gameweek
    gameweek = get_current_gameweek(events)
    print(f"Current gameweek: {gameweek}")

    # 4. Build lookup dicts and populate the module-level cache so tool
    #    functions can access bootstrap data without re-fetching it
    player_lookup    = build_player_lookup(elements, teams)
    team_by_id       = {t["id"]: t["short_name"] for t in teams}
    team_id_by_short = {t["short_name"]: t["id"]  for t in teams}

    _cache.update({
        "gameweek":        gameweek,
        "player_lookup":   player_lookup,
        "team_by_id":      team_by_id,
        "team_id_by_short": team_id_by_short,
    })

    # 5. Fetch picks and print the squad summary for the user
    picks_data = fetch_json(PICKS_URL.format(team_id=team_id, gw=gameweek))
    picks      = picks_data["picks"]
    print_squad(picks, player_lookup, gameweek, team_id)

    # 6. Hand off to Claude — it will call tools autonomously and then advise
    run_ai_advisor(team_id, gameweek)


if __name__ == "__main__":
    main()
