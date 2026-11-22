"""Ratings of a party and feedback about its players: who may send them, what the solver is told, aggregates.

Players rate the party as a whole (like / dislike) and may send feedback that names players, with a category.
Compatibility for the solver is built from three sources, all per directed pair (voter -> player):
  * the old per-player votes (still read until they expire; no new ones are made),
  * a like or dislike *inferred* from a voter's party ratings: how much better (or worse) they rate parties that
    contain the player than parties that do not, shrunk towards 0 when there is little data,
  * tagged feedback: a dislike whose weight depends on the category (toxicity counts more than lateness).

Privacy: nothing here is shown to other players. Organizers get totals and anonymised notes (/feedback). The
solver export does contain from/to entries, because the solver reads them.
"""
from collections import defaultdict
from datetime import timedelta

from .db import parse_iso

# category -> (label, default compatibility weight); cfg["feedback_weights"] overrides the weights
FEEDBACK_CATEGORIES = {"toxic": ("Toxic or hostile behaviour", 1.0),
                       "communication": ("Poor communication", 0.5),
                       "late_no_show": ("Late or no-show", 0.2),
                       "performance": ("Mechanics or performance", 0.0),
                       "other": ("Something else", 0.0)}
PARTY_RATINGS = ("like", "dislike")
MAX_NOTE = 800
LIKE_CAP, DISLIKE_CAP = 1.0, 3.0     # largest weight one exported entry can carry


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


def feedback_weight(cfg, category):
    """How strongly a feedback category counts as a dislike towards the player named."""
    return float((cfg.get("feedback_weights") or {}).get(category, FEEDBACK_CATEGORIES[category][1]))


def check_rater(db, cfg, now, voter_id, party_id):
    """(party, '') when `voter_id` may rate this party now, else (None, reason)."""
    party = db.party(party_id)
    if party is None or party["status"] not in ("confirmed", "done"):
        return None, "That party is not confirmed."
    if str(voter_id) not in {m["discord_id"] for m in db.party_members(party_id)}:
        return None, "You were not in that party."
    why = time_reason(party, cfg, now)
    return (None, why) if why else (party, "")


def cast_party_rating(ctx, voter_id, party_id, vote):
    """Store a rating of the whole party if allowed. Returns (ok, message)."""
    if vote not in PARTY_RATINGS:
        return False, "That is not a rating."
    party, why = check_rater(ctx.db, ctx.cfg, ctx.now(), voter_id, party_id)
    if party is None:
        return False, why
    ctx.db.add_party_rating(party, voter_id, vote, ctx.now())
    return True, ""


def submit_feedback(ctx, reporter_id, party_id, targets, category, note=""):
    """Replace this reporter's feedback for a party: one row per player named (or one row naming nobody).
    Returns (ok, message)."""
    if category not in FEEDBACK_CATEGORIES:
        return False, "Pick what happened."
    party, why = check_rater(ctx.db, ctx.cfg, ctx.now(), reporter_id, party_id)
    if party is None:
        return False, why
    members = {m["discord_id"] for m in ctx.db.party_members(party_id)}
    targets = sorted({str(t) for t in targets})
    if str(reporter_id) in targets:
        return False, "You cannot send feedback about yourself."
    if any(t not in members for t in targets):
        return False, "That player was not in your party."
    note = (note or "").strip()[:MAX_NOTE]
    ctx.db.replace_party_feedback(party, reporter_id, [(t, category, note) for t in targets or [None]], ctx.now())
    return True, ""


def rateable_parties(ctx, user_id):
    """Approved parties of this player whose slot has ended and whose rating window is open, newest first."""
    now = ctx.now()
    out = []
    for p in ctx.db.member_parties(user_id):
        if rating_open(p, ctx.cfg, now) and len(ctx.db.party_members(p["party_id"])) > 1:
            out.append(p)
    return out


def inferred_scores(db, cfg, as_of, since):
    """{(voter, target): score in (-1, 1)} inferred from party ratings.

    For each voter and each player seen in their rated parties: how much better they rated the parties that
    had the player than the ones that did not ((mean with - mean without) / 2, likes +1 and dislikes -1),
    times n / (n + compat_prior) where n combines both sample sizes, so thin data stays near 0. Needs
    `compat_min_parties` rated parties together and at least one without. Someone who rates every party
    the same way, or who only ever played with the same people, gets 0: nothing can be told apart.
    """
    ratings = db.latest_party_ratings(as_of, since)
    members = db.party_member_ids({r["party_id"] for r in ratings})
    by_voter = defaultdict(list)                                  # voter -> [(score, members of that party)]
    for r in ratings:
        by_voter[r["voter_id"]].append((1 if r["vote"] == "like" else -1, set(members.get(r["party_id"], ()))))
    out = {}
    for voter, rated in by_voter.items():
        rated = [(s, m) for s, m in rated if voter in m]
        for target in sorted(set().union(*(m for _, m in rated)) - {voter}):
            with_ = [s for s, m in rated if target in m]
            without = [s for s, m in rated if target not in m]
            if len(with_) < cfg["compat_min_parties"] or not without:
                continue
            diff = (sum(with_) / len(with_) - sum(without) / len(without)) / 2
            n = len(with_) * len(without) / (len(with_) + len(without))
            score = diff * n / (n + cfg["compat_prior"])
            if abs(score) >= 0.05:
                out[(voter, target)] = score
    return out


def tag_penalties(db, cfg, as_of, since):
    """{(reporter, target): total weight} of tagged feedback, fading linearly to 0 over `vote_expiry_days`."""
    span = timedelta(days=cfg["vote_expiry_days"])
    out = defaultdict(float)
    for r in db.feedback_rows(as_of, since):
        if r["target_id"] is None:
            continue
        fade = max(0.0, 1 - (as_of - parse_iso(r["created_utc"])) / span)
        out[(r["reporter_id"], r["target_id"])] += feedback_weight(cfg, r["category"]) * fade
    return out


def votes_for_export(db, cfg, player_ids, as_of):
    """The solver's "votes" list: {"from", "to", "vote": like|dislike, "weight"?} per directed pair.

    Each pair's net score adds the old per-player vote (+1 / -1), the inferred score from party ratings and
    minus the tagged-feedback penalties. The sign picks like or dislike; "weight" is the size (a like is capped
    at 1, a dislike at 3) and is left out when it is exactly 1, so data without ratings or feedback exports
    exactly as before. Votes older than `vote_expiry_days` before `as_of` are dropped, and so is any pair where
    one side is not in this export. Everything is read as of `as_of`. Sorted by player id, so the same data
    always gives the same list.
    """
    ids = {str(i) for i in player_ids}
    since = as_of - timedelta(days=cfg["vote_expiry_days"])
    net = defaultdict(float)
    for r in db.latest_votes(as_of, since):
        if r["vote"] != "neutral":
            net[(r["voter_id"], r["target_id"])] += 1 if r["vote"] == "like" else -1
    for pair, score in inferred_scores(db, cfg, as_of, since).items():
        net[pair] += score
    for pair, penalty in tag_penalties(db, cfg, as_of, since).items():
        net[pair] -= penalty
    key = lambda x: (len(x), x)                                   # noqa: E731  (same order as the export's players)
    out = []
    for (voter, target), score in sorted(net.items(), key=lambda kv: (key(kv[0][0]), key(kv[0][1]))):
        if voter not in ids or target not in ids or abs(score) < 0.05:
            continue
        weight = round(min(abs(score), LIKE_CAP if score > 0 else DISLIKE_CAP), 2)
        entry = {"from": voter, "to": target, "vote": "like" if score > 0 else "dislike"}
        if weight != 1:
            entry["weight"] = weight
        out.append(entry)
    return out


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




def party_rating_counts(db, cfg, now):
    """{player_id: (likes, dislikes)}: how many current party ratings were like / dislike for parties the player
    was in, inside the expiry window. The player's own rating does not count."""
    since = now - timedelta(days=cfg["vote_expiry_days"])
    ratings = db.latest_party_ratings(now, since)
    members = db.party_member_ids({r["party_id"] for r in ratings})
    counts = {}
    for r in ratings:
        for pid in members.get(r["party_id"], ()):
            if pid == r["voter_id"]:
                continue
            likes, dislikes = counts.get(pid, (0, 0))
            counts[pid] = (likes + (r["vote"] == "like"), dislikes + (r["vote"] == "dislike"))
    return counts


def feedback_counts(db, cfg, now):
    """{player_id: {category: number of different reporters}}, and the number of general reports that name nobody."""
    since = now - timedelta(days=cfg["vote_expiry_days"])
    who = defaultdict(lambda: defaultdict(set))
    general = 0
    for r in db.feedback_rows(now, since):
        if r["target_id"] is None:
            general += 1
        else:
            who[r["target_id"]][r["category"]].add(r["reporter_id"])
    return {t: {c: len(rs) for c, rs in cats.items()} for t, cats in who.items()}, general
