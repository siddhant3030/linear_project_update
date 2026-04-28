#!/usr/bin/env python3
"""
Fetches Linear cycle/sprint progress and posts a formatted summary to Discord.
Run via cron every Monday at 9 AM, or manually with --dry-run.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

LINEAR_API_URL = "https://api.linear.app/graphql"

# ── Linear GraphQL ──────────────────────────────────────────────────────────

CYCLE_META_QUERY = """
query ActiveCycle($teamKey: String!) {
  teams(filter: { key: { eq: $teamKey } }) {
    nodes {
      name
      activeCycle {
        id
        name
        number
        startsAt
        endsAt
        progress
      }
    }
  }
}
"""

PAST_CYCLES_QUERY = """
query PastCycles($teamKey: String!) {
  teams(filter: { key: { eq: $teamKey } }) {
    nodes {
      name
      cycles(first: 10, filter: { isActive: { eq: false }, isPast: { eq: true } }) {
        nodes {
          id
          name
          number
          startsAt
          endsAt
          progress
        }
      }
    }
  }
}
"""

CYCLE_ISSUES_QUERY = """
query CycleIssues($cycleId: String!, $after: String) {
  cycle(id: $cycleId) {
    issues(first: 50, after: $after) {
      nodes {
        identifier
        title
        priority
        updatedAt
        state {
          name
          type
        }
        assignee {
          name
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

STALE_THRESHOLD_DAYS = 5

# Linear priority: 0=none, 1=urgent, 2=high, 3=medium, 4=low
PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
PRIORITY_ICONS = {0: "", 1: "\U0001f534", 2: "\U0001f7e0", 3: "\U0001f7e1", 4: "\U0001f535"}


def _execute_graphql(query, variables, api_key):
    """Execute a GraphQL query against the Linear API."""
    response = requests.post(
        LINEAR_API_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "errors" in body:
        raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")
    return body["data"]


def _fetch_all_cycle_issues(cycle_id, api_key):
    """Paginate through all issues in a cycle (50 at a time)."""
    all_issues = []
    cursor = None
    while True:
        variables = {"cycleId": cycle_id}
        if cursor:
            variables["after"] = cursor
        data = _execute_graphql(CYCLE_ISSUES_QUERY, variables, api_key)
        issues_data = data["cycle"]["issues"]
        all_issues.extend(issues_data["nodes"])
        page_info = issues_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return all_issues


def _build_issue_list(issues):
    """Convert raw GraphQL issue nodes into standardized dicts."""
    return [
        {
            "identifier": issue["identifier"],
            "title": issue["title"],
            "priority": issue.get("priority", 0),
            "state_name": issue["state"]["name"],
            "state_type": issue["state"]["type"],
            "assignee": issue["assignee"]["name"] if issue.get("assignee") else "Unassigned",
            "updated_at": issue.get("updatedAt", ""),
        }
        for issue in issues
    ]


def _get_previous_cycle(team_key, api_key, current_cycle_number):
    """Get the cycle just before the given cycle number. Returns (cycle_meta, issues) or (None, [])."""
    data = _execute_graphql(PAST_CYCLES_QUERY, {"teamKey": team_key}, api_key)
    teams = data["teams"]["nodes"]
    if not teams:
        return None, []
    cycles = teams[0]["cycles"]["nodes"]
    # Find the cycle with number = current - 1
    prev = next((c for c in cycles if c["number"] == current_cycle_number - 1), None)
    if not prev:
        return None, []
    issues = _fetch_all_cycle_issues(prev["id"], api_key)
    return prev, _build_issue_list(issues)


def fetch_past_cycle(team_key, api_key, cycle_number):
    """Fetch a past cycle by number. Returns None if not found."""
    data = _execute_graphql(PAST_CYCLES_QUERY, {"teamKey": team_key}, api_key)
    teams = data["teams"]["nodes"]
    if not teams:
        return None
    team = teams[0]
    cycles = team["cycles"]["nodes"]

    cycle = None
    if cycle_number == "last":
        sorted_cycles = sorted(cycles, key=lambda c: c["number"], reverse=True)
        cycle = sorted_cycles[0] if sorted_cycles else None
    else:
        num = int(cycle_number)
        cycle = next((c for c in cycles if c["number"] == num), None)

    if not cycle:
        return None

    issues = _fetch_all_cycle_issues(cycle["id"], api_key)

    return {
        "team_name": team["name"],
        "cycle_name": cycle.get("name") or f"Cycle {cycle['number']}",
        "cycle_number": cycle["number"],
        "starts_at": cycle["startsAt"][:10],
        "ends_at": cycle["endsAt"][:10],
        "progress": cycle["progress"],
        "issues": _build_issue_list(issues),
    }


def fetch_active_cycle(team_key, api_key):
    """Fetch the active cycle for a team. Returns None if no active cycle."""
    data = _execute_graphql(CYCLE_META_QUERY, {"teamKey": team_key}, api_key)
    teams = data["teams"]["nodes"]
    if not teams:
        return None
    team = teams[0]
    cycle = team.get("activeCycle")
    if not cycle:
        return None

    issues = _fetch_all_cycle_issues(cycle["id"], api_key)

    return {
        "team_name": team["name"],
        "cycle_name": cycle.get("name") or f"Cycle {cycle['number']}",
        "cycle_number": cycle["number"],
        "starts_at": cycle["startsAt"][:10],
        "ends_at": cycle["endsAt"][:10],
        "progress": cycle["progress"],
        "issues": _build_issue_list(issues),
    }


# ── Discord embed builder ──────────────────────────────────────────────────

# Members to include (case-insensitive partial match against assignee name)
TRACKED_MEMBERS = ["himanshu", "ishan", "siddhant"]

# Group by state_name for granularity (In Progress vs In Review)
STATE_DISPLAY_ORDER = ["Done", "In Review", "In Progress", "Todo", "Backlog", "Triage", "Cancelled"]

# Map Linear state names to display names; unmapped names pass through as-is
STATE_NAME_MAP = {
    "done": "Done",
    "in review": "In Review",
    "in progress": "In Progress",
    "todo": "Todo",
    "backlog": "Backlog",
    "triage": "Triage",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
}


def _display_state(state_name):
    """Normalize a Linear state name to a display name."""
    return STATE_NAME_MAP.get(state_name.lower(), state_name)


def _is_tracked_member(assignee):
    """Check if assignee matches any tracked member (case-insensitive partial)."""
    assignee_lower = assignee.lower()
    return any(m in assignee_lower for m in TRACKED_MEMBERS)


def _progress_bar(progress, length=20):
    """Build a text progress bar like ████████░░░░░░░░░░░░ 40%"""
    filled = round(progress * length)
    empty = length - filled
    pct = round(progress * 100)
    return f"{'█' * filled}{'░' * empty} {pct}%"


def _progress_color(progress):
    """Return a Discord embed color int based on progress."""
    if progress >= 0.75:
        return 0x2ECC71  # green
    if progress >= 0.40:
        return 0xF1C40F  # yellow
    return 0xE74C3C  # red


def _truncate(text, max_len=40):
    return text if len(text) <= max_len else text[: max_len - 1] + "\u2026"


def generate_summary(cycle_data, issues):
    """Use Gemini to generate a brief narrative summary of the sprint."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    from google import genai

    # Build context
    by_member = defaultdict(lambda: defaultdict(list))
    for issue in issues:
        state = _display_state(issue["state_name"])
        by_member[issue["assignee"]][state].append(issue["title"])

    member_summaries = []
    for name, states in by_member.items():
        first_name = name.split("@")[0].split()[0].title()
        parts = []
        for s in ["Done", "In Review", "In Progress", "Todo"]:
            if states.get(s):
                titles = [f"  - {t}" for t in states[s]]
                parts.append(f"  {s}:\n" + "\n".join(titles))
        member_summaries.append(f"{first_name}:\n" + "\n".join(parts))

    context = (
        f"Sprint: {cycle_data['cycle_name']}\n"
        f"Team: {cycle_data['team_name']}\n"
        f"Period: {cycle_data['starts_at']} to {cycle_data['ends_at']}\n"
        f"Progress: {round(cycle_data['progress'] * 100)}%\n"
        f"Issues: {len(issues)} total\n\n"
        + "\n\n".join(member_summaries)
    )

    prompt = (
        "You are a concise engineering manager. Given this sprint data, write a "
        "2-3 sentence summary highlighting: what themes/areas the team focused on, "
        "key accomplishments, and any concerns (too many items in progress, blockers, etc). "
        "Keep it under 200 words. No bullet points, just a brief narrative paragraph.\n\n"
        f"{context}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Warning: AI summary failed: {e}", file=sys.stderr)
        return None


def _quickchart_url(chart_config):
    """Generate a QuickChart.io URL for a chart config dict."""
    import urllib.parse
    base = "https://quickchart.io/chart"
    config_str = json.dumps(chart_config, separators=(",", ":"))
    return f"{base}?c={urllib.parse.quote(config_str)}&w=500&h=300&bkg=rgb(47,49,54)"


def _build_member_chart(by_member):
    """Build a horizontal stacked bar chart URL showing per-member state breakdown."""
    names = []
    done_data = []
    review_data = []
    progress_data = []
    todo_data = []

    for name in sorted(by_member.keys()):
        first_name = name.split("@")[0].split()[0].title()
        names.append(first_name)
        stats = defaultdict(int)
        for i in by_member[name]:
            stats[_display_state(i["state_name"])] += 1
        done_data.append(stats.get("Done", 0))
        review_data.append(stats.get("In Review", 0))
        progress_data.append(stats.get("In Progress", 0))
        todo_data.append(stats.get("Todo", 0))

    chart = {
        "type": "horizontalBar",
        "data": {
            "labels": names,
            "datasets": [
                {"label": "Done", "data": done_data, "backgroundColor": "rgb(46,204,113)"},
                {"label": "In Review", "data": review_data, "backgroundColor": "rgb(155,89,182)"},
                {"label": "In Progress", "data": progress_data, "backgroundColor": "rgb(52,152,219)"},
                {"label": "Todo", "data": todo_data, "backgroundColor": "rgb(149,165,166)"},
            ],
        },
        "options": {
            "plugins": {"legend": {"labels": {"fontColor": "white"}}},
            "scales": {
                "xAxes": [{"stacked": True, "ticks": {"fontColor": "white"}}],
                "yAxes": [{"stacked": True, "ticks": {"fontColor": "white"}}],
            },
        },
    }
    return _quickchart_url(chart)


def _build_cycle_history_chart(current_cycle, prev_cycle_meta):
    """Build a bar chart comparing current vs previous cycle progress."""
    if not prev_cycle_meta:
        return None

    prev_name = prev_cycle_meta.get("name") or f"Cycle {prev_cycle_meta['number']}"
    cur_name = current_cycle["cycle_name"]

    chart = {
        "type": "bar",
        "data": {
            "labels": [prev_name, cur_name],
            "datasets": [
                {
                    "label": "Progress %",
                    "data": [
                        round(prev_cycle_meta["progress"] * 100),
                        round(current_cycle["progress"] * 100),
                    ],
                    "backgroundColor": ["rgb(149,165,166)", "rgb(52,152,219)"],
                }
            ],
        },
        "options": {
            "plugins": {"legend": {"display": False}},
            "scales": {
                "yAxes": [{"ticks": {"fontColor": "white", "max": 100, "beginAtZero": True}}],
                "xAxes": [{"ticks": {"fontColor": "white"}}],
            },
        },
    }
    return _quickchart_url(chart)


def _days_since_update(updated_at_str):
    """Return number of days since the issue was last updated."""
    if not updated_at_str:
        return 0
    updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - updated).days


STATE_ICONS = {
    "Done": "\u2705",
    "In Review": "\U0001f50d",
    "In Progress": "\U0001f535",
    "Todo": "\u2b1c",
    "Backlog": "\u2b1c",
    "Triage": "\u2b1c",
    "Cancelled": "\u274c",
}

# Order for sorting issues within each person's list
STATE_SORT_ORDER = {s: i for i, s in enumerate(STATE_DISPLAY_ORDER)}


def build_embeds(cycle_data, ai_summary=None, prev_cycle_issues=None, prev_cycle_meta=None):
    """Build Discord embeds: summary + per-member + carry-over + stale warnings."""
    progress = cycle_data["progress"]
    all_issues = cycle_data["issues"]

    # Filter to tracked members only
    issues = [i for i in all_issues if _is_tracked_member(i["assignee"])]

    # Carry-over detection: issues in both current and previous cycle that aren't done
    carryover_ids = set()
    if prev_cycle_issues:
        prev_ids = {i["identifier"] for i in prev_cycle_issues}
        carryover_ids = {
            i["identifier"]
            for i in issues
            if i["identifier"] in prev_ids and _display_state(i["state_name"]) != "Done"
        }

    # Stale detection: in-progress/in-review issues not updated in 5+ days
    stale_ids = set()
    for i in issues:
        display = _display_state(i["state_name"])
        if display in ("In Progress", "In Review") and _days_since_update(i["updated_at"]) >= STALE_THRESHOLD_DAYS:
            stale_ids.add(i["identifier"])

    # Per-member issues
    by_member = defaultdict(list)
    for issue in issues:
        by_member[issue["assignee"]].append(issue)

    # Summary stats
    done_count = sum(1 for i in issues if _display_state(i["state_name"]) == "Done")
    total_count = len(issues)

    # Build summary embed
    summary_lines = []
    for name in sorted(by_member.keys()):
        member_issues = by_member[name]
        stats = defaultdict(int)
        for i in member_issues:
            stats[_display_state(i["state_name"])] += 1
        parts = []
        for s in ["Done", "In Review", "In Progress", "Todo"]:
            if stats.get(s):
                parts.append(f"{stats[s]} {s.lower()}")
        for s, count in stats.items():
            if s not in ["Done", "In Review", "In Progress", "Todo"]:
                parts.append(f"{count} {s.lower()}")
        first_name = name.split("@")[0].split()[0].title()
        summary_lines.append(f"**{first_name}**: {', '.join(parts)}")

    description = (
        f"**{cycle_data['starts_at']}** to **{cycle_data['ends_at']}**\n\n"
        f"{_progress_bar(progress)}\n\n"
    )

    # Sprint comparison
    if prev_cycle_meta:
        prev_name = prev_cycle_meta.get("name") or f"Cycle {prev_cycle_meta['number']}"
        prev_pct = round(prev_cycle_meta["progress"] * 100)
        cur_pct = round(progress * 100)
        delta = cur_pct - prev_pct
        arrow = "\u2b06\ufe0f" if delta > 0 else ("\u2b07\ufe0f" if delta < 0 else "\u27a1\ufe0f")
        description += f"{arrow} **vs {prev_name} (final): {prev_pct}%** | Current: {cur_pct}%\n\n"

    if ai_summary:
        description += f"*{ai_summary}*\n\n"
    description += "\n".join(summary_lines)

    summary_embed = {
        "title": f"Sprint Update: {cycle_data['cycle_name']} - {cycle_data['team_name']}",
        "description": description,
        "color": _progress_color(progress),
        "footer": {"text": f"{done_count}/{total_count} issues completed"},
    }

    # Build one embed per member — sorted by priority within each state group
    member_embeds = []
    for name in sorted(by_member.keys()):
        member_issues = by_member[name]
        # Sort: state order first, then priority (1=urgent first, 0=none last)
        member_issues.sort(
            key=lambda i: (
                STATE_SORT_ORDER.get(_display_state(i["state_name"]), 99),
                (i["priority"] if i["priority"] > 0 else 5),  # push "none" after low
            )
        )

        # Per-member stats
        stats = defaultdict(int)
        for i in member_issues:
            stats[_display_state(i["state_name"])] += 1
        member_done = stats.get("Done", 0)
        member_total = len(member_issues)
        member_progress = member_done / member_total if member_total > 0 else 0
        first_name = name.split("@")[0].split()[0].title()

        # Build issue lines grouped by state with priority icons
        lines = []
        current_state = None
        for i in member_issues:
            display = _display_state(i["state_name"])
            if display != current_state:
                current_state = display
                state_count = stats[display]
                lines.append(f"\n**{display}** ({state_count})")

            pri_icon = PRIORITY_ICONS.get(i["priority"], "")
            pri_tag = f" {pri_icon}" if pri_icon else ""
            tags = ""
            if i["identifier"] in stale_ids:
                days = _days_since_update(i["updated_at"])
                tags += f" \u26a0\ufe0f `{days}d`"
            if i["identifier"] in carryover_ids:
                tags += " \U0001f501"
            lines.append(f"`{i['identifier']}` {_truncate(i['title'])}{pri_tag}{tags}")

        subtitle_parts = []
        for s in ["Done", "In Review", "In Progress", "Todo"]:
            if stats.get(s):
                subtitle_parts.append(f"{stats[s]} {s.lower()}")

        value = f"{_progress_bar(member_progress, 15)}\n" + "\n".join(lines)
        if len(value) > 4096:
            value = value[:4092] + "\n..."

        member_embeds.append({
            "title": f"{first_name} ({', '.join(subtitle_parts)})",
            "description": value,
            "color": _progress_color(member_progress),
        })

    # Carry-over summary embed (if any)
    alerts_embeds = []
    if carryover_ids:
        carryover_issues = [i for i in issues if i["identifier"] in carryover_ids]
        lines = [f"\U0001f501 `{i['identifier']}` {_truncate(i['title'], 50)} — *{i['assignee'].split('@')[0].split()[0].title()}*" for i in carryover_issues]
        value = "\n".join(lines)
        if len(value) > 4096:
            value = value[:4092] + "\n..."
        alerts_embeds.append({
            "title": f"\U0001f501 Carry-over from previous cycle ({len(carryover_ids)} issues)",
            "description": value,
            "color": 0xE67E22,  # orange
        })

    if stale_ids:
        stale_issues = [i for i in issues if i["identifier"] in stale_ids]
        lines = [
            f"\u26a0\ufe0f `{i['identifier']}` {_truncate(i['title'], 50)} — *{i['assignee'].split('@')[0].split()[0].title()}* ({_days_since_update(i['updated_at'])}d)"
            for i in stale_issues
        ]
        value = "\n".join(lines)
        if len(value) > 4096:
            value = value[:4092] + "\n..."
        alerts_embeds.append({
            "title": f"\u26a0\ufe0f Stale issues — no updates in {STALE_THRESHOLD_DAYS}+ days ({len(stale_ids)} issues)",
            "description": value,
            "color": 0xE74C3C,  # red
        })

    # Chart embeds
    chart_embeds = []
    try:
        member_chart_url = _build_member_chart(by_member)
        chart_embeds.append({
            "title": "Team Breakdown",
            "image": {"url": member_chart_url},
            "color": 0x3498DB,
        })
    except Exception:
        pass

    try:
        cycle_chart_url = _build_cycle_history_chart(cycle_data, prev_cycle_meta)
        if cycle_chart_url:
            chart_embeds.append({
                "title": "Cycle Comparison",
                "image": {"url": cycle_chart_url},
                "color": 0x3498DB,
            })
    except Exception:
        pass

    return [summary_embed] + alerts_embeds + member_embeds + chart_embeds


# ── Discord sender ──────────────────────────────────────────────────────────


def send_discord_embed(webhook_url, embeds, content=None):
    """Post rich embeds to a Discord webhook. Splits into batches of 10 (Discord limit)."""
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        data = {"embeds": batch}
        if content and i == 0:
            data["content"] = content
        response = requests.post(webhook_url, json=data, timeout=10)
        response.raise_for_status()


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Post Linear cycle progress to Discord.")
    parser.add_argument("--team-key", type=str, help="Linear team key (overrides LINEAR_TEAM_KEY)")
    parser.add_argument(
        "--webhook-url", type=str, help="Discord webhook URL (overrides LINEAR_DISCORD_WEBHOOK)"
    )
    parser.add_argument(
        "--cycle", type=str, default=None,
        help="Cycle to report: 'last' for most recent past cycle, or a cycle number (e.g. 4). Defaults to active cycle."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print Discord payload to stdout instead of posting"
    )
    args = parser.parse_args()

    api_key = os.environ.get("LINEAR_API_KEY")
    team_key = args.team_key or os.environ.get("LINEAR_TEAM_KEY")
    webhook_url = args.webhook_url or os.environ.get("LINEAR_DISCORD_WEBHOOK")

    if not api_key:
        print("Error: LINEAR_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    if not team_key:
        print("Error: LINEAR_TEAM_KEY is not set and --team-key not provided.", file=sys.stderr)
        sys.exit(1)
    if not webhook_url and not args.dry_run:
        print(
            "Error: LINEAR_DISCORD_WEBHOOK is not set and --webhook-url not provided.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if args.cycle:
            cycle_data = fetch_past_cycle(team_key, api_key, args.cycle)
        else:
            cycle_data = fetch_active_cycle(team_key, api_key)
    except requests.HTTPError as e:
        print(f"Error calling Linear API: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Linear API error: {e}", file=sys.stderr)
        sys.exit(1)

    if cycle_data is None:
        if args.cycle:
            print(f"Cycle '{args.cycle}' not found for team '{team_key}'.", file=sys.stderr)
        else:
            print(f"No active cycle found for team '{team_key}'. Nothing to report.")
        sys.exit(0)

    # Fetch previous cycle for carry-over detection and sprint comparison
    prev_cycle_meta = None
    prev_cycle_issues = []
    try:
        prev_cycle_meta, prev_cycle_issues = _get_previous_cycle(
            team_key, api_key, cycle_data["cycle_number"]
        )
    except Exception as e:
        print(f"Warning: Could not fetch previous cycle: {e}", file=sys.stderr)

    # Filter issues for summary context
    tracked_issues = [
        i for i in cycle_data["issues"] if _is_tracked_member(i["assignee"])
    ]
    ai_summary = generate_summary(cycle_data, tracked_issues)
    embeds = build_embeds(
        cycle_data,
        ai_summary=ai_summary,
        prev_cycle_issues=prev_cycle_issues,
        prev_cycle_meta=prev_cycle_meta,
    )

    if args.dry_run:
        print(json.dumps({"embeds": embeds}, indent=2))
    else:
        try:
            send_discord_embed(webhook_url, embeds)
            print("Discord update posted successfully.")
        except requests.HTTPError as e:
            print(f"Error posting to Discord: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
