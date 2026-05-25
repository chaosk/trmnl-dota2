"""
OpenDota transform for TRMNL Dota stats plugin.

Polls player profile; fetches matches + hero stats; builds a GitHub-style
activity heatmap, recent match list, and top heroes for Liquid templates.
"""
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

BASE_URL = "https://api.opendota.com/api"
USER_AGENT = "trmnl-dota2-plugin/1.0"
HTTP_TIMEOUT = 5.0
MATCH_LIMIT = 120
MATCH_FETCH_PER_BUCKET = 60
HEATMAP_WEEKS = 26
HEAT_MAX_LEVEL = 8
RECENT_MATCHES = 10
TOP_HEROES = 6
MEDALS = ("", "Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine", "Immortal")
HERO_ICON_CDN = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/{slug}.png"
)
AVATAR_KEYS = ("avatarmedium", "avatarfull", "avatar")

GAME_MODES: dict[int, str] = {
    0: "Unknown",
    1: "All Pick",
    2: "Captains Mode",
    3: "Random Draft",
    4: "Single Draft",
    5: "All Random",
    11: "Mid Only",
    12: "Least Played",
    13: "Limited Heroes",
    16: "Captains Draft",
    18: "Ability Draft",
    20: "ARDM",
    21: "1v1 Mid",
    22: "All Draft",
    23: "Turbo",
}

LOBBY_TYPES: dict[int, str] = {
    0: "Normal",
    1: "Practice",
    2: "Tournament",
    3: "Tutorial",
    4: "Co-op",
    5: "Ranked",
    6: "Solo Queue",
    7: "Ranked",
    8: "1v1 Mid",
    9: "Battle Cup",
}

MATCH_SKILL: dict[int, str] = {1: "Normal", 2: "High", 3: "Very High"}

JsonDict = dict[str, Any]


def fetch_json(path: str) -> Any:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def merge_matches(
    significant_matches: list[JsonDict] | None,
    other_matches: list[JsonDict] | None,
    limit: int = MATCH_LIMIT,
) -> list[JsonDict]:
    """OpenDota splits significant (ranked/AP etc.) vs non-significant (often Turbo)."""
    seen: set[int] = set()
    merged: list[JsonDict] = []
    combined = sorted(
        (significant_matches or []) + (other_matches or []),
        key=lambda m: m.get("start_time", 0),
        reverse=True,
    )
    for match in combined:
        match_id = match.get("match_id")
        if match_id is not None:
            mid = int(match_id)
            if mid in seen:
                continue
            seen.add(mid)
        merged.append(match)
        if len(merged) >= limit:
            break
    return merged


def fetch_player_matches(account_id: str) -> list[JsonDict]:
    bucket = MATCH_FETCH_PER_BUCKET
    significant = fetch_json(f"/players/{account_id}/matches?limit={bucket}")
    other = fetch_json(f"/players/{account_id}/matches?limit={bucket}&significant=0")
    return merge_matches(significant, other)


def fetch_player_heroes(account_id: str) -> list[JsonDict]:
    return fetch_json(f"/players/{account_id}/heroes?significant=0")


def polled_player(input_data: JsonDict) -> JsonDict:
    nested = input_data.get("data")
    if isinstance(nested, dict) and nested.get("profile"):
        return nested
    if input_data.get("profile"):
        return input_data
    return {}


def account_id_from_input(input_data: JsonDict) -> str | None:
    for key in ("account_id", "steam_id"):
        val = input_data.get(key)
        if val not in (None, ""):
            return str(val).strip()

    plugin_settings = (input_data.get("trmnl") or {}).get("plugin_settings") or {}
    fields = (
        input_data.get("custom_fields")
        or input_data.get("custom_fields_values")
        or plugin_settings.get("custom_fields_values")
        or {}
    )
    for key in ("account_id", "steam_id"):
        val = fields.get(key)
        if val not in (None, ""):
            return str(val).strip()

    aid = (polled_player(input_data).get("profile") or {}).get("account_id")
    return str(aid) if aid is not None else None


def player_won(match: JsonDict) -> bool:
    return match.get("radiant_win") is (match.get("player_slot", 0) < 128)


def player_team(match: JsonDict) -> str:
    return "Radiant" if match.get("player_slot", 0) < 128 else "Dire"


def player_avatar_url(profile: JsonDict) -> str:
    for key in AVATAR_KEYS:
        url = profile.get(key)
        if url:
            return str(url)
    return ""


def hero_slug(hero_record: JsonDict) -> str:
    name = hero_record.get("name") or ""
    prefix = "npc_dota_hero_"
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name.replace(prefix, "")


def hero_lookup(all_heroes: list[JsonDict]) -> tuple[dict[int, str], dict[int, str]]:
    names: dict[int, str] = {}
    icons: dict[int, str] = {}
    for hero in all_heroes:
        hero_id = hero.get("id")
        if hero_id is None:
            continue
        hid = int(hero_id)
        names[hid] = hero["localized_name"]
        slug = hero_slug(hero)
        if slug:
            icons[hid] = HERO_ICON_CDN.format(slug=slug)
    return names, icons


def kda_segments(kills: int, deaths: int, assists: int) -> tuple[int, int, int]:
    total = kills + deaths + assists
    if total == 0:
        return 0, 0, 0
    k_pct = int(round(100 * kills / total))
    d_pct = int(round(100 * deaths / total))
    a_pct = max(0, 100 - k_pct - d_pct)
    return k_pct, d_pct, a_pct


def format_duration_clock(seconds: int | float | None) -> str:
    if not seconds:
        return "—"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def format_relative_time(ts: int | float | None) -> str:
    if not ts:
        return "—"
    now = datetime.now(timezone.utc)
    played = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = now - played
    days = delta.days
    if days <= 0:
        hours = max(1, delta.seconds // 3600)
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    if days == 1:
        return "1 day ago"
    if days < 60:
        return f"{days} days ago"
    return played.strftime("%b %d")


def format_rank(rank_tier: int | None) -> str | None:
    if not rank_tier:
        return None
    medal = rank_tier // 10
    stars = rank_tier % 10
    name = MEDALS[medal] if 0 < medal < len(MEDALS) else "Ranked"
    return f"{name} {stars}" if stars else name


def format_rank_bracket(rank_tier: int) -> str:
    medal = rank_tier // 10
    stars = rank_tier % 10
    name = MEDALS[medal] if 0 < medal < len(MEDALS) else "—"
    return f"{name} [{stars}]" if stars else name


def match_bracket(match: JsonDict) -> str:
    skill = match.get("skill")
    if skill in MATCH_SKILL:
        return MATCH_SKILL[skill]
    lobby = match.get("lobby_type")
    if lobby in LOBBY_TYPES:
        return LOBBY_TYPES[lobby]
    return "Normal"


def match_game_mode(match: JsonDict) -> str:
    return GAME_MODES.get(match.get("game_mode"), "Unknown")


def match_rank_label(match: JsonDict, fallback_rank: str | None) -> str:
    avg = match.get("average_rank")
    if avg:
        return format_rank_bracket(int(avg))
    return fallback_rank or "—"


def build_heatmap(
    matches: list[JsonDict],
    weeks: int = HEATMAP_WEEKS,
) -> tuple[list[list[JsonDict]], int]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=weeks * 7 - 1)
    counts: dict[str, int] = {}

    for match in matches:
        ts = match.get("start_time")
        if not ts:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if start <= day <= end:
            key = day.isoformat()
            counts[key] = counts.get(key, 0) + 1

    grid_start = start - timedelta(days=(start.weekday() + 1) % 7)
    day: date = grid_start
    weeks_out: list[list[JsonDict]] = []

    while len(weeks_out) < weeks:
        week_days: list[JsonDict] = []
        in_range_counts: list[int] = []
        for _ in range(7):
            key = day.isoformat()
            count = counts.get(key, 0)
            in_range = start <= day <= end
            week_days.append(
                {"date": key, "count": count, "in_range": in_range, "level": 0}
            )
            if in_range and count:
                in_range_counts.append(count)
            day += timedelta(days=1)

        peak = max(in_range_counts, default=0)
        for cell in week_days:
            if cell["in_range"] and cell["count"] and peak:
                cell["level"] = min(
                    HEAT_MAX_LEVEL,
                    max(1, round(cell["count"] / peak * HEAT_MAX_LEVEL)),
                )
        weeks_out.append(week_days)

    return weeks_out, sum(counts.values())


def format_recent(
    matches: list[JsonDict],
    hero_names: dict[int, str],
    hero_icons: dict[int, str],
    player_rank: str | None = None,
) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for match in matches[:RECENT_MATCHES]:
        hero_id = match.get("hero_id")
        kills = match.get("kills", 0) or 0
        deaths = match.get("deaths", 0) or 0
        assists = match.get("assists", 0) or 0
        won = player_won(match)
        k_pct, d_pct, a_pct = kda_segments(kills, deaths, assists)
        start_time = match.get("start_time", 0)
        hid = int(hero_id) if hero_id is not None else 0
        rows.append(
            {
                "hero": hero_names.get(hid, f"Hero {hero_id}"),
                "hero_icon": hero_icons.get(hid, ""),
                "team": player_team(match),
                "won": won,
                "bracket": match_bracket(match),
                "game_mode": match_game_mode(match),
                "rank_at_match": match_rank_label(match, player_rank),
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "k_pct": k_pct,
                "d_pct": d_pct,
                "a_pct": a_pct,
                "duration": format_duration_clock(match.get("duration")),
                "when_relative": format_relative_time(start_time),
            }
        )
    return rows


def format_top_heroes(
    hero_stats: list[JsonDict],
    hero_names: dict[int, str],
    hero_icons: dict[int, str],
) -> list[JsonDict]:
    ranked = sorted(hero_stats, key=lambda h: h.get("games", 0), reverse=True)
    rows: list[JsonDict] = []
    for hero in ranked[:TOP_HEROES]:
        hero_id = hero.get("hero_id")
        games = hero.get("games", 0) or 1
        wins = hero.get("win", 0)
        last_played = hero.get("last_played")
        hid = int(hero_id) if hero_id is not None else 0
        rows.append(
            {
                "name": hero_names.get(hid, "Unknown"),
                "hero_icon": hero_icons.get(hid, ""),
                "matches": games,
                "wins": wins,
                "winrate": round(100 * wins / games),
                "last_played": format_relative_time(last_played) if last_played else "—",
            }
        )
    return rows


def _plugin_error(message: str) -> JsonDict:
    return {"error": message, "player_name": "(ノ ゜Д゜)ノ ︵ ┻━┻"}


def run(input_data: JsonDict) -> JsonDict:
    account_id = account_id_from_input(input_data)
    if not account_id:
        return _plugin_error("Set your OpenDota account ID in plugin settings.")

    player = polled_player(input_data)
    try:
        if not player.get("profile"):
            player = fetch_json(f"/players/{account_id}")
        matches = fetch_player_matches(account_id)
        hero_stats = fetch_player_heroes(account_id)
        all_heroes = fetch_json("/heroes")
    except urllib.error.HTTPError as exc:
        return _plugin_error(f"OpenDota request failed ({exc.code}).")
    except urllib.error.URLError:
        return _plugin_error("Could not reach OpenDota API.")

    hero_names, hero_icons = hero_lookup(all_heroes)
    profile = player.get("profile") or {}
    player_rank = format_rank(player.get("rank_tier"))
    heatmap_weeks, heatmap_total = build_heatmap(matches)

    wl: JsonDict = player.get("win_loss") or {}
    if not wl:
        try:
            wl = fetch_json(f"/players/{account_id}/wl")
        except (urllib.error.HTTPError, urllib.error.URLError):
            wl = {}

    mmr_estimate = player.get("mmr_estimate")
    mmr = mmr_estimate.get("estimate") if isinstance(mmr_estimate, dict) else None

    return {
        "player_name": profile.get("personaname") or f"Player {account_id}",
        "player_avatar": player_avatar_url(profile),
        "account_id": account_id,
        "rank_label": player_rank,
        "mmr": mmr,
        "wins": wl.get("win"),
        "losses": wl.get("lose"),
        "heatmap_weeks": heatmap_weeks,
        "heatmap_total": heatmap_total,
        "heatmap_weeks_count": len(heatmap_weeks),
        "recent_matches": format_recent(matches, hero_names, hero_icons, player_rank),
        "top_heroes": format_top_heroes(hero_stats, hero_names, hero_icons),
        "fetched_matches": len(matches),
    }
