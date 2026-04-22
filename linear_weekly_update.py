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

ACTIVE_CYCLE_QUERY = """
query ActiveCycle($teamKey: String!) {
  teams(filter: { key: { eq: $teamKey } }) {
    nodes {
      name
      activeCycle {
        name
        number
        startsAt
        endsAt
        progress
        issues(first: 250) {
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
        }
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


def fetch_active_cycle(team_key, api_key):
    """Fetch the active cycle for a team. Returns None if no active cycle."""
    data = _execute_graphql(ACTIVE_CYCLE_QUERY, {"teamKey": team_key}, api_key)
    teams = data["teams"]["nodes"]
    if not teams:
        return None
    team = teams[0]
    cycle = team.get("activeCycle")
    if not cycle:
        return None
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
            for issue in cycle["issues"]["nodes"]
        ],
    }


# ── Discord embed builder ──────────────────────────────────────────────────

STATE_TYPE_ORDER = ["completed", "started", "unstarted", "triage", "backlog", "cancelled"]
STATE_TYPE_LABELS = {
    "completed": "Completed",
    "started": "In Progress",
    "unstarted": "Todo",
    "triage": "Triage",
    "backlog": "Backlog",
    "cancelled": "Cancelled",
}


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


def build_embed(cycle_data):
    """Build a Discord embed dict from cycle data."""
    progress = cycle_data["progress"]
    issues = cycle_data["issues"]

    # Group issues by state type
    by_state = defaultdict(list)
    for issue in issues:
        by_state[issue["state_type"]].append(issue)

    # Build fields for each state group (in order, skip empty)
    fields = []
    for state_type in STATE_TYPE_ORDER:
        group = by_state.get(state_type, [])
        if not group:
            continue
        label = STATE_TYPE_LABELS.get(state_type, state_type.title())
        lines = [f"`{i['identifier']}` {_truncate(i['title'])}" for i in group]
        # Discord field value max is 1024 chars; truncate if needed
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1020] + "\n..."
        fields.append({"name": f"{label} ({len(group)})", "value": value, "inline": False})

    # Team progress: per-member breakdown
    member_stats = defaultdict(lambda: {"done": 0, "active": 0, "other": 0})
    for issue in issues:
        name = issue["assignee"]
        if issue["state_type"] == "completed":
            member_stats[name]["done"] += 1
        elif issue["state_type"] == "started":
            member_stats[name]["active"] += 1
        else:
            member_stats[name]["other"] += 1

    # Sort by most completed, then most active
    sorted_members = sorted(
        member_stats.items(), key=lambda x: (x[1]["done"], x[1]["active"]), reverse=True
    )
    member_lines = []
    for name, stats in sorted_members:
        parts = []
        if stats["done"]:
            parts.append(f"{stats['done']} done")
        if stats["active"]:
            parts.append(f"{stats['active']} active")
        if stats["other"]:
            parts.append(f"{stats['other']} other")
        member_lines.append(f"**{name}**: {', '.join(parts)}")

    if member_lines:
        member_value = "\n".join(member_lines)
        if len(member_value) > 1024:
            member_value = member_value[:1020] + "\n..."
        fields.append({"name": "Team Progress", "value": member_value, "inline": False})

    completed_count = len(by_state.get("completed", []))
    total_count = len(issues)

    embed = {
        "title": f"Sprint Update: {cycle_data['cycle_name']} - {cycle_data['team_name']}",
        "description": (
            f"**{cycle_data['starts_at']}** to **{cycle_data['ends_at']}**\n\n"
            f"{_progress_bar(progress)}"
        ),
        "color": _progress_color(progress),
        "fields": fields,
        "footer": {"text": f"{completed_count}/{total_count} issues completed"},
    }
    return embed


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

    embed = build_embed(cycle_data)

    if args.dry_run:
        print(json.dumps({"embeds": [embed]}, indent=2))
    else:
        try:
            send_discord_embed(webhook_url, [embed])
            print("Discord update posted successfully.")
        except requests.HTTPError as e:
            print(f"Error posting to Discord: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
