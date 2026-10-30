"""Ratings between players who shared a confirmed party: who may vote, what the solver is told, aggregates.

Privacy: nothing here is ever shown to other players. Organizers only get per-player totals
(`summary_counts`). The solver export does contain raw from/to votes, because the solver reads them.
"""
from datetime import timedelta

from .db import parse_iso

VOTES = ("like", "neutral", "dislike")
SYMBOL = {"like": "Like", "neutral": "Neutral", "dislike": "Dislike"}


def slot_end(row):
    """UTC end of a slot: its start plus the actual length if it ended early, else the scheduled length."""
    minutes = row["actual_minutes"] if row["actual_minutes"] else row["minutes"]
    return parse_iso(row["start_utc"]) + timedelta(minutes=minutes)


def party_end(row):
    """When ratings open: the moment the party was marked ended if it was (never later than the slot's end),
    else the slot's end. Rows without an `ended_utc` column count as not ended."""
    ended = row["ended_utc"] if "ended_utc" in row.keys() else None
    return min(parse_iso(ended), slot_end(row)) if ended else slot_end(row)


def window_closes(row, cfg):
    return party_end(row) + timedelta(hours=cfg["vote_window_hours"])


def rating_open(row, cfg, now):
    """The party has ended and the rating window after it is still open."""
    return party_end(row) <= now <= window_closes(row, cfg)


def time_reason(row, cfg, now):
    """Why ratings are closed for this party right now, or '' when they are open."""
    if now < party_end(row):
        return "That party has not ended yet. Ratings open when it does."
    if now > window_closes(row, cfg):
        return f"The rating window closed {cfg['vote_window_hours']} hours after the slot ended."
    return ""


def check_vote(db, cfg, now, voter_id, target_id, party_id):
    """(True, '') when `voter_id` may rate `target_id` for this party, else (False, reason)."""
    voter_id, target_id = str(voter_id), str(target_id)
    if voter_id == target_id:
        return False, "You cannot rate yourself."
    party = db.party(party_id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return False, "That party is not confirmed."
    members = {m["discord_id"] for m in db.party_members(party_id)}
    if voter_id not in members:
        return False, "You were not in that party."
    if target_id not in members:
        return False, "That player was not in your party."
    why = time_reason(party, cfg, now)
    return (False, why) if why else (True, "")


def cast_vote(ctx, voter_id, target_id, vote, party_id):
    """Store a vote if allowed. Returns (ok, message). Appends a row; the latest one per pair is the vote."""
    if vote not in VOTES:
        return False, "That is not a rating."
    now = ctx.now()
    ok, why = check_vote(ctx.db, ctx.cfg, now, voter_id, target_id, party_id)
    if not ok:
        return False, why
    party = ctx.db.party(party_id)
    ctx.db.add_vote(party["run_id"], party["slot_id"], voter_id, target_id, vote, now)
    return True, ""


def rateable_parties(ctx, user_id):
    """Approved parties of this player whose slot has ended and whose rating window is open, newest first."""
    now = ctx.now()
    out = []
    for p in ctx.db.member_parties(user_id):
        if rating_open(p, ctx.cfg, now) and len(ctx.db.party_members(p["party_id"])) > 1:
            out.append(p)
    return out


def current_rating(db, voter_id, target_id, now):
    """The voter's current vote on the target ('neutral' when they never voted)."""
    return db.latest_vote(voter_id, target_id, now) or "neutral"


def votes_for_export(db, cfg, player_ids, as_of):
    """The solver's "votes" list: {"from", "to", "vote"} for like and dislike only.

    For each (voter, target) the latest vote created at or before `as_of` counts; neutral (a changed mind)
    is dropped, so are votes older than `vote_expiry_days` before `as_of`, and so is any pair where one
    side is not in this export. Sorted by player id, so the same data always gives the same list.
    """
    ids = {str(i) for i in player_ids}
    since = as_of - timedelta(days=cfg["vote_expiry_days"])
    rows = [r for r in db.latest_votes(as_of, since)
            if r["vote"] != "neutral" and r["voter_id"] in ids and r["target_id"] in ids]
    key = lambda x: (len(x), x)                                   # noqa: E731  (same order as the export's players)
    rows.sort(key=lambda r: (key(r["voter_id"]), key(r["target_id"])))
    return [{"from": r["voter_id"], "to": r["target_id"], "vote": r["vote"]} for r in rows]


def summary_counts(db, cfg, now):
    """{player_id: (likes, dislikes)} received, from current non-neutral votes inside the expiry window."""
    since = now - timedelta(days=cfg["vote_expiry_days"])
    counts = {}
    for r in db.latest_votes(now, since):
        if r["vote"] == "neutral":
            continue
        likes, dislikes = counts.get(r["target_id"], (0, 0))
        counts[r["target_id"]] = (likes + (r["vote"] == "like"), dislikes + (r["vote"] == "dislike"))
    return counts


