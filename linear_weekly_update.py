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

CYCLE_ISSUES_QUERY = """
query CycleIssues($cycleId: String!, $after: String) {
  cycle(id: $cycleId) {
    issues(first: 50, after: $after) {
      nodes {
        identifier
        title
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
        "issues": [
            {
                "identifier": issue["identifier"],
                "title": issue["title"],
                "state_name": issue["state"]["name"],
                "state_type": issue["state"]["type"],
                "assignee": issue["assignee"]["name"] if issue.get("assignee") else "Unassigned",
            }
            for issue in issues
        ],
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


def build_embeds(cycle_data):
    """Build Discord embeds: one summary + one per tracked member."""
    progress = cycle_data["progress"]
    all_issues = cycle_data["issues"]

    # Filter to tracked members only
    issues = [i for i in all_issues if _is_tracked_member(i["assignee"])]

    # Per-member issues grouped by state
    by_member = defaultdict(list)
    for issue in issues:
        by_member[issue["assignee"]].append(issue)

    # Summary stats
    done_count = sum(
        1 for i in issues if _display_state(i["state_name"]) == "Done"
    )
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

    summary_embed = {
        "title": f"Sprint Update: {cycle_data['cycle_name']} - {cycle_data['team_name']}",
        "description": (
            f"**{cycle_data['starts_at']}** to **{cycle_data['ends_at']}**\n\n"
            f"{_progress_bar(progress)}\n\n"
            + "\n".join(summary_lines)
        ),
        "color": _progress_color(progress),
        "footer": {"text": f"{done_count}/{total_count} issues completed"},
    }

    # Build one embed per member
    member_embeds = []
    for name in sorted(by_member.keys()):
        member_issues = by_member[name]
        # Sort issues: done first, then in review, in progress, todo, etc.
        member_issues.sort(
            key=lambda i: STATE_SORT_ORDER.get(_display_state(i["state_name"]), 99)
        )

        lines = []
        for i in member_issues:
            display = _display_state(i["state_name"])
            icon = STATE_ICONS.get(display, "\u2b1c")
            lines.append(f"{icon} `{i['identifier']}` {_truncate(i['title'])}")

        value = "\n".join(lines)
        if len(value) > 4096:
            value = value[:4092] + "\n..."

        first_name = name.split("@")[0].split()[0].title()
        stats = defaultdict(int)
        for i in member_issues:
            stats[_display_state(i["state_name"])] += 1
        subtitle_parts = []
        for s in ["Done", "In Review", "In Progress", "Todo"]:
            if stats.get(s):
                subtitle_parts.append(f"{stats[s]} {s.lower()}")

        member_embeds.append({
            "title": f"{first_name} ({', '.join(subtitle_parts)})",
            "description": value,
            "color": _progress_color(progress),
        })

    return [summary_embed] + member_embeds


# ── Discord sender ──────────────────────────────────────────────────────────


def send_discord_embed(webhook_url, embeds, content=None):
    """Post rich embeds to a Discord webhook."""
    data = {"embeds": embeds}
    if content:
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
        cycle_data = fetch_active_cycle(team_key, api_key)
    except requests.HTTPError as e:
        print(f"Error calling Linear API: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Linear API error: {e}", file=sys.stderr)
        sys.exit(1)

    if cycle_data is None:
        print(f"No active cycle found for team '{team_key}'. Nothing to report.")
        sys.exit(0)

    embeds = build_embeds(cycle_data)

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
