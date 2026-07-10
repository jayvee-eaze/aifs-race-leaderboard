#!/usr/bin/env python3
# build_leaderboard.py  --  STANDALONE sales leaderboard data pull, DAILY / WEEKLY / MONTHLY.
# Pulls per-rep sales performance straight from GoHighLevel (opportunities + appointments) and
# Stripe (cash collected), buckets it into three periods, and writes leaderboard.json.
#
# No dependency on any other project. Standard library only (urllib), zero pip installs.
#
# Secrets from env (GitHub Actions secrets or Railway variables, NEVER hardcoded):
#   GHL_API_TOKEN         GHL Private Integration token (opportunities.readonly + calendars/events.readonly)
#   STRIPE_API_KEY        Stripe secret/restricted key (charges read)
# Optional env (AIFS defaults baked in):
#   GHL_LOCATION_ID (default 61bBcrk5Fi4BuTWwvW0P)
#   GHL_PIPELINE_ID (default PJbkfqE3g4KRP8i9ZeLb)
#   GHL_CALENDAR_GROUP_ID (default 3ThPJMJXcptrv4goAE9Y)
#   WINDOW_START (default 2026-06-15, the AIFS launch date; floors the month/week windows)
#
# Periods (Australia/Brisbane clock), CALENDAR based so quotas reset the way they are enforced:
#   daily   = today
#   weekly  = this calendar week, Monday to today
#   monthly = this calendar month, the 1st to today (never earlier than WINDOW_START)
#
# Fail-safe: ANY pull error raises and exits non-zero BEFORE the file is touched, so a failed run
# keeps the last good leaderboard.json. The write is atomic (tmp + os.replace).

import os, sys, json, time, base64, urllib.request, urllib.parse, datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__)) + "/"
OUT = HERE + "leaderboard.json"
BNE = ZoneInfo("Australia/Brisbane")
now = datetime.datetime.now(BNE)
today = now.date()

GHL_LOC   = os.environ.get("GHL_LOCATION_ID", "61bBcrk5Fi4BuTWwvW0P")
GHL_PIPE  = os.environ.get("GHL_PIPELINE_ID", "PJbkfqE3g4KRP8i9ZeLb")
GHL_GROUP = os.environ.get("GHL_CALENDAR_GROUP_ID", "3ThPJMJXcptrv4goAE9Y")
WINDOW_START = datetime.date.fromisoformat(os.environ.get("WINDOW_START", "2026-06-15"))

# GHL user id -> rep name. The AIFS closing team.
# James Wellington and Matthew Burns are intentionally excluded (no longer on the team).
USER = {
    "KyR0lFZOC0l0GQHM6SLv": "Caleb Chase",
    "Z3WFuyTIWmoZMmzNJrRl": "Dan Baldasso",
}
FIRST = {full: full.split()[0] for full in USER.values()}

# Optional: GHL pipeline stage ids that mean "won" for teams that use a Closed-Won STAGE
# without setting the opportunity status to "won". Comma separated. Status=="won" is the primary signal.
WON_STAGES = set(x.strip() for x in os.environ.get("WON_STAGE_IDS", "").split(",") if x.strip())

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Optional rep headshots. photos.json maps full name -> image url or data URI. It survives the daily
# rebuild (this file is committed, not overwritten by the pull), so photos persist across refreshes.
_PF = HERE + "photos.json"
PHOTOS = {}
if os.path.exists(_PF):
    try:
        PHOTOS = json.load(open(_PF))
    except Exception:
        PHOTOS = {}


def env(k):
    v = os.environ.get(k)
    if not v:
        sys.exit(f"build_leaderboard FAILED: missing secret {k}")
    return v


def _req(url, headers=None, data=None, method="GET", tries=3):
    for i in range(tries):
        try:
            body = json.dumps(data).encode() if data is not None else None
            h = dict(headers or {})
            if body is not None:
                h["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(f"request failed {method} {url.split('?')[0]}: {e}")
            time.sleep(2 * (i + 1))


def fmt_day(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def fmt_range(a, b):
    if a == b:
        return fmt_day(b)
    ay = "" if a.year == b.year else f" {a.year}"
    return f"{a.day} {MONTHS[a.month - 1]}{ay} to {fmt_day(b)}"


def istest(name):
    n = (name or "").lower()
    return "test" in n or "gian" in n or "jayvee" in n


def to_date(v):
    """Parse a GHL date field (ISO string or epoch ms) into a Brisbane date, or None."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            ms = int(v)
            return datetime.datetime.fromtimestamp(ms / 1000, BNE).date()
        s = str(v).replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).astimezone(BNE).date()
    except Exception:
        return None


def pull_ghl_opportunities(token):
    H = {"Authorization": f"Bearer {token}", "Version": "2021-07-28", "Accept": "application/json"}
    url = "https://services.leadconnectorhq.com/opportunities/search"
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"location_id": GHL_LOC, "pipeline_id": GHL_PIPE, "page": page, "limit": 100})
        r = _req(url + "?" + q, H)
        opps = r.get("opportunities", []) or []
        out += opps
        meta = r.get("meta", {}) or {}
        if not opps or not meta.get("nextPage") or page > 50:
            break
        page += 1
    return out


def pull_ghl_appointments(token):
    H = {"Authorization": f"Bearer {token}", "Version": "2021-07-28", "Accept": "application/json"}
    start_ms = int(datetime.datetime.combine(WINDOW_START, datetime.time(0, 0), BNE).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    q = urllib.parse.urlencode({"locationId": GHL_LOC, "groupId": GHL_GROUP, "startTime": start_ms, "endTime": end_ms})
    r = _req("https://services.leadconnectorhq.com/calendars/events?" + q, H)
    return r.get("events", []) or []


def pull_stripe_charges(key):
    auth = base64.b64encode((key + ":").encode()).decode()
    H = {"Authorization": f"Basic {auth}"}
    start = int(datetime.datetime.combine(WINDOW_START, datetime.time(0, 0), BNE).timestamp())
    url = f"https://api.stripe.com/v1/charges?limit=100&created[gte]={start}"
    out = []
    while url:
        r = _req(url, H)
        for c in r.get("data", []):
            if (c.get("status") == "succeeded" and c.get("paid") and not c.get("refunded")
                    and c.get("livemode") and c.get("amount", 0) >= 100):
                out.append(c)
        if r.get("data") and r.get("has_more"):
            url = f"https://api.stripe.com/v1/charges?limit=100&created[gte]={start}&starting_after={r['data'][-1]['id']}"
        else:
            url = None
    return out


def build_period(pstart, pend, opps, owned, events, stripe_total, target, team_tiers):
    """Assemble one period block. owned (pipeline) is a live snapshot, same across periods."""
    won = defaultdict(int)
    cash = defaultdict(float)
    for o in opps:
        uid = o.get("assignedTo") or o.get("assignedUserId")
        if uid not in USER:
            continue
        is_won = (o.get("status") or "").lower() == "won" or (o.get("pipelineStageId") in WON_STAGES)
        if not is_won:
            continue
        # Prefer the moment the deal became won; fall back progressively. updatedAt is last so an
        # unrelated later edit cannot silently redate a win into the wrong period.
        d = to_date(o.get("lastStatusChangeAt") or o.get("dateWon") or o.get("wonAt")
                    or o.get("updatedAt") or o.get("createdAt"))
        if d and pstart <= d <= pend:
            won[uid] += 1
            cash[uid] += float(o.get("monetaryValue") or 0)

    booked = defaultdict(int)
    for e in events:
        uid = e.get("assignedUserId") or e.get("userId")
        if uid not in USER:
            continue
        nm = (e.get("title", "") + " " + ((e.get("contact") or {}).get("name") or ""))
        if istest(nm):
            continue
        d = to_date(e.get("startTime"))
        if d and pstart <= d <= pend:
            booked[uid] += 1

    reps = []
    for uid, full in USER.items():
        reps.append({"name": full, "first": FIRST[full], "ghlId": uid,
                     "cash": int(round(cash[uid])), "dealsWon": won[uid],
                     "pipelineOwned": owned[uid], "apptsBooked": booked[uid]})
    reps.sort(key=lambda r: (-r["cash"], -r["dealsWon"], -r["pipelineOwned"]))
    for i, r in enumerate(reps, 1):
        r["rank"] = i
        o = r["pipelineOwned"]
        r["closeRate"] = f"{(r['dealsWon'] / o * 100):.1f}%" if o else "0%"
    reps = [{"rank": r["rank"], "name": r["name"], "first": r["first"], "cash": r["cash"],
             "target": int(target), "dealsWon": r["dealsWon"], "pipelineOwned": r["pipelineOwned"],
             "apptsBooked": r["apptsBooked"], "closeRate": r["closeRate"], "ghlId": r["ghlId"],
             "photo": PHOTOS.get(r["name"])}
            for r in reps]
    team_owned = sum(r["pipelineOwned"] for r in reps)
    team_won = sum(r["dealsWon"] for r in reps)
    unassigned = sum(1 for o in opps if (o.get("assignedTo") or o.get("assignedUserId")) not in USER)
    team = {
        "cash": sum(r["cash"] for r in reps), "dealsWon": team_won,
        "pipelineOwned": team_owned, "apptsBooked": sum(r["apptsBooked"] for r in reps),
        "closeRate": f"{(team_won / team_owned * 100):.1f}%" if team_owned else "0%",
        "unassignedPipeline": unassigned, "stripeCollected": int(round(stripe_total)),
        "tiers": [int(t) for t in team_tiers],
    }
    # target = PER-REP goal for this period; team_tiers = collective milestone ladder for this period
    return {"window": fmt_range(pstart, pend), "targetPerRep": int(target), "team": team, "reps": reps}


def cents(x):
    return int(round(float(x or 0) * 100))


class AuditError(Exception):
    pass


def audit(data, roster_names):
    """Hard validation gate. Runs on the freshly built data BEFORE it is written or published.
    Every number must tie out to the cent and reconcile to the true money source (Stripe), or the
    build fails and the last good leaderboard.json is kept. This is the trust guarantee."""
    fails = []

    # 1. Freshness: the data must be from a pull that ran TODAY.
    if data.get("asOf") != today.isoformat():
        fails.append(f"freshness: asOf {data.get('asOf')} is not today ({today.isoformat()})")

    for pk, pd in data.get("periods", {}).items():
        reps = pd.get("reps", [])
        team = pd.get("team", {})
        names = [r["name"] for r in reps]

        # 2. Roster completeness: exactly the configured reps, no more, no fewer.
        if sorted(names) != sorted(roster_names):
            fails.append(f"{pk}: roster {names} does not match configured {roster_names}")

        # 3. Internal consistency: team totals must equal the sum of reps, cash to the cent.
        if cents(team.get("cash")) != sum(cents(r.get("cash")) for r in reps):
            fails.append(f"{pk}: team.cash {team.get('cash')} != sum of rep cash (to the cent)")
        if team.get("dealsWon") != sum(r.get("dealsWon", 0) for r in reps):
            fails.append(f"{pk}: team.dealsWon != sum of rep deals")
        if team.get("pipelineOwned") != sum(r.get("pipelineOwned", 0) for r in reps):
            fails.append(f"{pk}: team.pipelineOwned != sum of rep pipeline")
        ab = sum((r.get("apptsBooked") or 0) for r in reps)
        if team.get("apptsBooked") is not None and team.get("apptsBooked") != ab:
            fails.append(f"{pk}: team.apptsBooked != sum of rep meetings")

        # 4. Rank/order: exactly cash desc, then deals, then pipeline; rank numbers 1..n in order.
        want = sorted(reps, key=lambda r: (-r.get("cash", 0), -r.get("dealsWon", 0), -r.get("pipelineOwned", 0)))
        for i, (got, exp) in enumerate(zip(reps, want), 1):
            if got.get("name") != exp.get("name") or got.get("rank") != i:
                fails.append(f"{pk}: rank/order wrong at position {i}")
                break

    # 5. Reconciliation to the money truth (Stripe), to the cent. GHL-attributed monthly cash MUST
    #    equal Stripe cash collected this month. A mismatch means attribution is wrong or incomplete,
    #    which is exactly what we must never silently publish.
    rec = data.get("reconciliation") or {}
    if rec.get("status") == "mismatch" and os.environ.get("ALLOW_UNRECONCILED") != "1":
        fails.append(f"reconciliation: GHL ${rec.get('ghlCash')} != Stripe ${rec.get('stripeCash')} "
                     f"(delta {rec.get('deltaCents')} cents). Resolve the source, or set ALLOW_UNRECONCILED=1 to override.")

    if fails:
        raise AuditError("AUDIT FAILED, nothing published:\n  - " + "\n  - ".join(fails))


def build():
    ghl = env("GHL_API_TOKEN")
    stripe_key = env("STRIPE_API_KEY")

    opps = pull_ghl_opportunities(ghl)
    events = pull_ghl_appointments(ghl)
    charges = pull_stripe_charges(stripe_key)

    # pipeline owned is a live snapshot, identical across every period
    owned = defaultdict(int)
    for o in opps:
        uid = o.get("assignedTo") or o.get("assignedUserId")
        if uid in USER:
            owned[uid] += 1

    def stripe_between(a, b):
        lo = datetime.datetime.combine(a, datetime.time(0, 0), BNE).timestamp()
        hi = datetime.datetime.combine(b + datetime.timedelta(days=1), datetime.time(0, 0), BNE).timestamp()
        return sum(c["amount"] for c in charges if lo <= c.get("created", 0) < hi) / 100.0

    # Calendar-based windows so the $150k/rep and team tier quotas reset the way they are enforced:
    #   daily   = today
    #   weekly  = this calendar week, Monday to today
    #   monthly = this calendar month, the 1st to today (never earlier than launch)
    d0 = today
    w0 = max(WINDOW_START, today - datetime.timedelta(days=today.weekday()))
    m0 = max(WINDOW_START, today.replace(day=1))

    # PER-INDIVIDUAL monthly cash goal. Each rep is accountable to collect this much every month.
    month_target = int(os.environ.get("MONTHLY_CASH_TARGET_PER_REP", os.environ.get("MONTHLY_CASH_TARGET", "150000")))
    week_target = round(month_target * 7 / 30)
    day_target = round(month_target / 30)

    # COLLECTIVE team goal, a gamified milestone ladder for the month (default 200k -> 400k -> 600k).
    month_tiers = [int(x) for x in os.environ.get("TEAM_TIERS", "200000,400000,600000").split(",")]
    week_tiers = [round(t * 7 / 30) for t in month_tiers]
    day_tiers = [round(t / 30) for t in month_tiers]

    data = {
        "asOf": today.isoformat(),
        "source": "GoHighLevel opportunities + Stripe payments (live)",
        "rankBy": "cash, then deals won, then pipeline owned",
        "cashTargetPerRep": month_target,
        "teamTiers": month_tiers,
        "roster": list(USER.values()),
        "periods": {
            "daily": {"label": "Today", **build_period(d0, today, opps, owned, events, stripe_between(d0, today), day_target, day_tiers)},
            "weekly": {"label": "This Week", **build_period(w0, today, opps, owned, events, stripe_between(w0, today), week_target, week_tiers)},
            "monthly": {"label": "This Month", **build_period(m0, today, opps, owned, events, stripe_between(m0, today), month_target, month_tiers)},
        },
    }

    # Reconcile GHL-attributed monthly cash against the true money source (Stripe), to the cent.
    stripe_month = stripe_between(m0, today)
    ghl_month_cash = data["periods"]["monthly"]["team"]["cash"]
    delta_c = cents(ghl_month_cash) - cents(stripe_month)
    data["reconciliation"] = {
        "ghlCash": int(round(ghl_month_cash)),
        "stripeCash": int(round(stripe_month)),
        "deltaCents": delta_c,
        "status": "reconciled" if abs(delta_c) <= 1 else "mismatch",
        "checkedAt": now.isoformat(timespec="seconds"),
    }

    # THE GATE: validate everything before anything is written or published.
    audit(data, list(USER.values()))

    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, OUT)
    m = data["periods"]["monthly"]
    print(f"AUDIT PASSED, reconciled to Stripe ({data['reconciliation']['status']}). leaderboard.json written | "
          f"monthly leader {m['reps'][0]['name']} ${m['reps'][0]['cash']:,} | "
          f"team month ${m['team']['cash']:,} / {m['team']['dealsWon']} won / {m['team']['pipelineOwned']} pipeline")


if __name__ == "__main__":
    try:
        build()
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"build_leaderboard FAILED (leaderboard.json left untouched): {e}")
