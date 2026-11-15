#!/usr/bin/env python3
"""Pull Utah Jazz and league data from ESPN's public NBA API and embed it in index.html.

    python fetch_jazz.py                          # current + previous season, rewrites ./index.html
    python fetch_jazz.py --seasons 2026-27        # one season only
    python fetch_jazz.py --html docs/index.html

Regular season only. Needs Python 3.9+ and no third-party packages.

Why ESPN: stats.nba.com and cdn.nba.com refuse requests from GitHub's servers, so the daily refresh cannot
run against them. ESPN serves every game's box score and play-by-play (with shot locations and substitutions),
and every number on the dashboard is computed from those:
    team ratings, pace and four factors     both teams' box scores, with the standard possession estimate
    league player stats and percentiles     every box score
    Jazz lineups, on/off, on-court ratings  the play-by-play, by tracking which five Jazz players are on the floor
    shot charts and league zone averages    every shot's x/y location

Caching: each finished game is reduced to a small record in games_cache.json, so a daily refresh only downloads
games played since the last run. A finished season's final output is kept in jazz_cache.json.

Endpoints (no key needed), under https://site.api.espn.com/apis/site/v2/sports/basketball/nba/:
    scoreboard?dates=YYYYMMDD    games on a date
    summary?event=ID             box score + play-by-play for one game
    teams/26/roster              Jazz ages, positions, numbers (current season)
"""
import argparse, datetime as dt, itertools, json, math, re, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
JAZZ = "UTA"
JAZZ_ESPN_ID = "26"
ABBR = {"UTAH": "UTA", "GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "WSH": "WAS"}  # ESPN -> NBA
COLORS = {
    "ATL": "#e03a3e", "BOS": "#007a33", "BKN": "#555555", "CHA": "#00788c", "CHI": "#ce1141", "CLE": "#860038",
    "DAL": "#00538c", "DEN": "#0e2240", "DET": "#c8102e", "GSW": "#1d428a", "HOU": "#ce1141", "IND": "#002d62",
    "LAC": "#1d428a", "LAL": "#552583", "MEM": "#5d76a9", "MIA": "#98002e", "MIL": "#00471b", "MIN": "#236192",
    "NOP": "#0c2340", "NYK": "#f58426", "OKC": "#007ac1", "ORL": "#0077c0", "PHI": "#006bb6", "PHX": "#e56020",
    "POR": "#e03a3e", "SAC": "#5a2d81", "SAS": "#8a8d8f", "TOR": "#ce1141", "UTA": "#4b2a85", "WAS": "#002b5c",
}
CACHE_VERSION = 2
TOV_TYPES = ("urnover", "Traveling", "Double Dribble", "Discontinued Dribble", "3-Second")
ESPN_KEYS = {"minutes": "min", "points": "pts", "fieldGoalsMade-fieldGoalsAttempted": "fg",
             "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "fg3", "freeThrowsMade-freeThrowsAttempted": "ft",
             "rebounds": "reb", "assists": "ast", "turnovers": "tov", "steals": "stl", "blocks": "blk",
             "offensiveRebounds": "oreb", "defensiveRebounds": "dreb", "fouls": "pf", "plusMinus": "pm"}


# ---------------------------------------------------------------- helpers
def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (jazz-dashboard)"}), timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            time.sleep(0.1)
            return data
        except HTTPError as e:
            last = e
            if e.code in (400, 404):
                break
            time.sleep(2 * (i + 1))
        except (URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("Request failed: %s (%s)" % (url, last))


def ab(x):
    return ABBR.get(x, x)


def current_season(today=None):
    today = today or dt.date.today()
    y = today.year if today.month >= 10 else today.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def prev_season(s):
    y = int(s[:4]) - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def i0(v):
    try:
        return int(float(str(v).replace("+", "")))
    except (TypeError, ValueError):
        return 0


def pair(v):
    a, _, b = str(v or "0-0").partition("-")
    return i0(a), i0(b)


def clock_sec(s):
    s = str(s or "0")
    if ":" in s:
        m, sec = s.split(":")
        return int(m) * 60 + float(sec)
    return float(s)


def pc(a, b):
    return round(100 * a / b, 1) if b else None


def rd(a, b):
    return round(a / b, 3) if b else None


def r1(v):
    return None if v is None else round(v, 1)


def local_date(iso):
    """ESPN times are UTC; shift 6 hours so evening tip-offs keep their US date."""
    return (dt.datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M") - dt.timedelta(hours=6)).date().isoformat()


# ---------------------------------------------------------------- shot zones (NBA shot-chart conventions)
def zone(dx, dy, three, dist):
    """dx, dy in feet from the hoop (dy toward half court). Returns (basic zone, area, range)."""
    side = "Left Side(L)" if dx < 0 else "Right Side(R)"
    if three:
        if dy > 41 or dist > 45:
            return "Backcourt", "Back Court(BC)", "Back Court Shot"
        if dy <= 8.75 and abs(dx) >= 21:
            return ("Left Corner 3" if dx < 0 else "Right Corner 3"), side, "24+ ft."
        basic = "Above the Break 3"
    elif dist <= 4:
        return "Restricted Area", "Center(C)", "Less Than 8 ft."
    elif abs(dx) <= 8 and dy <= 14:
        basic = "In The Paint (Non-RA)"
    else:
        basic = "Mid-Range"
    ang = math.degrees(math.atan2(dx, max(dy, 0.01)))
    area = ("Left Side(L)" if ang < -54 else "Left Side Center(LC)" if ang < -18 else "Center(C)" if ang <= 18
            else "Right Side Center(RC)" if ang <= 54 else "Right Side(R)")
    rng = "Less Than 8 ft." if dist < 8 else "8-16 ft." if dist < 16 else "16-24 ft." if dist < 24 else "24+ ft."
    return basic, area, rng


# ---------------------------------------------------------------- one game -> compact record
def extract_game(s):
    """Reduce an ESPN summary to what the dashboard needs. Returns None if the game is not final."""
    h = s["header"]
    c = h["competitions"][0]
    if not c["status"]["type"].get("completed"):
        return None
    comp = {x["homeAway"]: x for x in c["competitors"]}
    home, away = ab(comp["home"]["team"]["abbreviation"]), ab(comp["away"]["team"]["abbreviation"])
    tid = {comp["home"]["team"]["id"]: home, comp["away"]["team"]["id"]: away}
    plays = sorted(s.get("plays", []), key=lambda p: int(p.get("sequenceNumber") or 0))
    periods = max([len(comp["home"].get("linescores", []))] + [p["period"]["number"] for p in plays] + [4])
    g = dict(id=h["id"], d=local_date(c["date"]), home=home, away=away, hs=i0(comp["home"].get("score")),
             as_=i0(comp["away"].get("score")), ot=max(0, periods - 4), teams={}, players={}, shots=[])

    for t in s["boxscore"]["teams"]:
        a = ab(t["team"]["abbreviation"])
        st = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        fgm, fga = pair(st.get("fieldGoalsMade-fieldGoalsAttempted"))
        m3, a3 = pair(st.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        ftm, fta = pair(st.get("freeThrowsMade-freeThrowsAttempted"))
        g["teams"][a] = dict(fgm=fgm, fga=fga, fg3m=m3, fg3a=a3, ftm=ftm, fta=fta, oreb=i0(st.get("offensiveRebounds")),
                             dreb=i0(st.get("defensiveRebounds")), reb=i0(st.get("totalRebounds")), ast=i0(st.get("assists")),
                             stl=i0(st.get("steals")), blk=i0(st.get("blocks")),
                             tov=i0(st.get("totalTurnovers") or st.get("turnovers")), pf=i0(st.get("fouls")),
                             name=t["team"].get("displayName", a))

    for tp in s["boxscore"]["players"]:
        a = ab(tp["team"]["abbreviation"])
        grp = tp["statistics"][0]
        keys = [ESPN_KEYS.get(k, k) for k in grp.get("keys", [])]
        rows = []
        for ath in grp.get("athletes", []):
            if ath.get("didNotPlay") or not ath.get("stats"):
                continue
            v = dict(zip(keys, ath["stats"]))
            fgm, fga = pair(v.get("fg")); m3, a3 = pair(v.get("fg3")); ftm, fta = pair(v.get("ft"))
            at = ath["athlete"]
            # [id, name, pos, num, starter, min, pts, fgm, fga, fg3m, fg3a, ftm, fta, reb, ast, tov, stl, blk, oreb, dreb, pf, pm]
            rows.append([at["id"], at.get("displayName", ""), (at.get("position") or {}).get("abbreviation", ""), at.get("jersey", ""),
                         1 if ath.get("starter") else 0, i0(v.get("min")), i0(v.get("pts")), fgm, fga, m3, a3, ftm, fta,
                         i0(v.get("reb")), i0(v.get("ast")), i0(v.get("tov")), i0(v.get("stl")), i0(v.get("blk")),
                         i0(v.get("oreb")), i0(v.get("dreb")), i0(v.get("pf")), i0(v.get("pm"))])
        g["players"][a] = rows

    for p in plays:
        if not p.get("shootingPlay") or p.get("pointsAttempted") not in (2, 3):
            continue
        co = p.get("coordinate") or {}
        x, y = co.get("x"), co.get("y")
        team = tid.get((p.get("team") or {}).get("id"))
        parts = p.get("participants") or []
        if x is None or y is None or abs(x) > 1000 or abs(y) > 1000 or not team or not parts:
            continue
        dx, dy = x - 25.0, float(y)
        m = re.search(r"(\d+)-foot", p.get("text", ""))
        dist = float(m.group(1)) if m else math.hypot(dx, dy)
        three = 1 if p.get("pointsAttempted") == 3 else 0
        zb, za, zr = zone(dx, dy, three, dist)
        g["shots"].append([team, parts[0]["athlete"]["id"], round(dx * 10), round(dy * 10), 1 if p.get("scoringPlay") else 0, three, zb, za, zr])

    if JAZZ in (home, away):
        g["stints"] = jazz_stints(plays, g, tid)
    return g


def jazz_stints(plays, g, tid):
    """Follow the five Jazz players on the floor through the play-by-play.
    Returns [[five ids], seconds, jazz pts, opp pts, [jazz FGA, FTA, OREB, TOV], [opp FGA, FTA, OREB, TOV]]."""
    jz_ids = {r[0] for r in g["players"].get(JAZZ, [])}
    starters = [r[0] for r in g["players"].get(JAZZ, []) if r[4]]
    jazz_home = g["home"] == JAZZ
    by_period = {}
    for p in plays:
        by_period.setdefault(p["period"]["number"], []).append(p)
    out, on_end, last = [], set(starters), [0, 0]
    for per in sorted(by_period):
        evs = by_period[per]
        if per == 1 and len(starters) == 5:
            on = set(starters)
        else:
            # period starters: Jazz players whose first appearance in the period is anything other than checking in
            first = {}
            for p in evs:
                ids = [x["athlete"]["id"] for x in (p.get("participants") or []) if x.get("athlete")]
                is_sub = p["type"].get("text") == "Substitution"
                for k, pid in enumerate(ids):
                    if pid in jz_ids and pid not in first:
                        first[pid] = "in" if (is_sub and k == 0) else "on"
            on = [pid for pid, f in first.items() if f == "on"]
            for pid in on_end:  # someone who played the whole period without appearing in a play
                if len(on) >= 5:
                    break
                if pid not in on and first.get(pid) != "in":
                    on.append(pid)
            on = set(on[:5])
        t_prev = 720 if per <= 4 else 300
        cur = dict(sec=0.0, jp=0, op=0, jposs=[0, 0, 0, 0], oposs=[0, 0, 0, 0])

        def flush():
            if cur["sec"] > 0 or cur["jp"] or cur["op"]:
                out.append([sorted(on), round(cur["sec"], 1), cur["jp"], cur["op"], cur["jposs"][:], cur["oposs"][:]])
            cur.update(sec=0.0, jp=0, op=0, jposs=[0, 0, 0, 0], oposs=[0, 0, 0, 0])

        for p in evs:
            t = clock_sec(p["clock"].get("displayValue"))
            cur["sec"] += max(0.0, t_prev - t)
            t_prev = min(t_prev, t)
            hs, as_ = p.get("homeScore"), p.get("awayScore")
            if hs is not None and as_ is not None:
                dh, da = hs - last[0], as_ - last[1]  # corrections after reviews come through as negative changes
                cur["jp"] += dh if jazz_home else da
                cur["op"] += da if jazz_home else dh
                last = [hs, as_]
            team = tid.get((p.get("team") or {}).get("id"))
            typ = p["type"].get("text", "")
            side = cur["jposs"] if team == JAZZ else cur["oposs"] if team else None
            if side is not None:
                if p.get("shootingPlay") and p.get("pointsAttempted") in (2, 3):
                    side[0] += 1
                elif p.get("shootingPlay") and p.get("pointsAttempted") == 1:
                    side[1] += 1
                elif typ == "Offensive Rebound":
                    side[2] += 1
                elif any(k in typ for k in TOV_TYPES):
                    side[3] += 1
            if typ == "Substitution" and team == JAZZ:
                ids = [x["athlete"]["id"] for x in (p.get("participants") or [])]
                if len(ids) >= 2:
                    flush()
                    on.discard(ids[1])
                    on.add(ids[0])
        cur["sec"] += max(0.0, t_prev)
        flush()
        on_end = set(on)
    return out


# ---------------------------------------------------------------- collecting a season
def season_dates(season, today):
    y = int(season[:4])
    d, end = dt.date(y, 10, 1), min(dt.date(y + 1, 4, 30), today)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def collect_games(season, cache, fetch, log, today):
    """Extracted records of every finished regular-season game in a season, downloading only what is new."""
    dates, games = cache.setdefault("dates", {}), cache.setdefault("games", {})
    ids, new_days = [], 0
    for d in season_dates(season, today):
        key = d.strftime("%Y%m%d")
        if key in dates:
            ids += dates[key]
            continue
        evs = fetch("%s/scoreboard?dates=%s" % (API, key)).get("events", [])
        day, all_final = [], True
        for e in evs:
            if (e.get("season") or {}).get("type") != 2:
                continue
            if not e["status"]["type"].get("completed"):
                all_final = False
                continue
            day.append(e["id"])
        ids += day
        new_days += 1
        if all_final and d < today - dt.timedelta(days=1):
            dates[key] = day
    todo = [i for i in ids if i not in games]
    log("  %s: %d finished games, %d to download (%d dates checked)" % (season, len(ids), len(todo), new_days))
    for n, gid in enumerate(todo, 1):
        try:
            rec = extract_game(fetch("%s/summary?event=%s" % (API, gid)))
        except (RuntimeError, KeyError, IndexError, TypeError, ValueError) as e:
            log("    skipped game %s (%s)" % (gid, e))
            continue
        if rec:
            games[gid] = rec
        if n % 100 == 0:
            log("    downloaded %d / %d" % (n, len(todo)))
    return [games[i] for i in ids if i in games]


def poss_est(t, o):
    """Basketball-Reference possession estimate for one team in one game."""
    orb = t["oreb"] / (t["oreb"] + o["dreb"]) if (t["oreb"] + o["dreb"]) else 0
    return t["fga"] + 0.4 * t["fta"] - 1.07 * orb * (t["fga"] - t["fgm"]) + t["tov"]


def pie_num(v):
    return v["pts"] + v["fgm"] + v["ftm"] - v["fga"] - v["fta"] + v["dreb"] + 0.5 * v["oreb"] + v["ast"] + v["stl"] + 0.5 * v["blk"] - v["pf"] - v["tov"]


P_FIELDS = ["min", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "reb", "ast", "tov", "stl", "blk", "oreb", "dreb", "pf", "pm"]


def build_season(season, recs, roster_info, log):
    if not recs:
        return None
    T, players, jazz, games, shots_all, stints = {}, {}, {}, [], [], []
    for g in sorted(recs, key=lambda r: (r["d"], r["id"])):
        if len(g["teams"]) != 2:
            continue
        gmin = 48 + 5 * g["ot"]
        th, ta = g["teams"][g["home"]], g["teams"][g["away"]]
        poss = 0.5 * (poss_est(th, ta) + poss_est(ta, th))
        game_pie = sum(pie_num(dict(g["teams"][x], pts=(g["hs"] if x == g["home"] else g["as_"]))) for x in (g["home"], g["away"]))
        for a, o, pf, pa in ((g["home"], g["away"], g["hs"], g["as_"]), (g["away"], g["home"], g["as_"], g["hs"])):
            t, ot = g["teams"][a], g["teams"][o]
            A = T.setdefault(a, dict(name=t["name"], gp=0, w=0, l=0, pts=0, opp=0, poss=0.0, min=0, tm={}, op={}))
            A["name"] = t["name"]
            A["gp"] += 1; A["w"] += pf > pa; A["l"] += pf < pa; A["pts"] += pf; A["opp"] += pa; A["poss"] += poss; A["min"] += gmin
            for k in ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf"):
                A["tm"][k] = A["tm"].get(k, 0) + t[k]
                A["op"][k] = A["op"].get(k, 0) + ot[k]
            for r in g["players"].get(a, []):
                pid, name, pos, num, starter, mins = r[:6]
                vals = dict(zip(P_FIELDS, [mins] + r[6:22]))
                for store in ([players, jazz] if a == JAZZ else [players]):
                    P = store.get(pid)
                    if P is None:
                        P = store[pid] = dict(id=pid, name=name, team=a, pos=pos, num=num, gp=0,
                                              ctx=dict(tmmin=0, fga=0, fta=0, tov=0, fgm=0, reb=0, oreb=0, poss=0.0, pie=0.0, pienum=0.0),
                                              **{k: 0 for k in P_FIELDS})
                    P["name"], P["team"] = name, a
                    if pos: P["pos"] = pos
                    if num: P["num"] = num
                    if mins > 0: P["gp"] += 1
                    for k in P_FIELDS:
                        P[k] += vals[k]
                    c = P["ctx"]
                    c["tmmin"] += 5 * gmin; c["fga"] += t["fga"]; c["fta"] += t["fta"]; c["tov"] += t["tov"]; c["fgm"] += t["fgm"]
                    c["reb"] += t["reb"]; c["oreb"] += ot["reb"]; c["poss"] += poss * mins / gmin
                    c["pie"] += game_pie; c["pienum"] += pie_num(vals)
        if JAZZ in (g["home"], g["away"]):
            home = g["home"] == JAZZ
            games.append(dict(id=g["id"], d=g["d"], opp=g["away"] if home else g["home"], home=home,
                              pts=g["hs"] if home else g["as_"], opts=g["as_"] if home else g["hs"]))
            stints += [s + [g["id"]] for s in g.get("stints", [])]
        shots_all += [s + [g["d"]] for s in g["shots"]]

    teams = []
    for a, A in T.items():
        n, tm, op = A["gp"], A["tm"], A["op"]
        per = lambda v: round(v / n, 1)
        ortg, drtg = 100 * A["pts"] / A["poss"], 100 * A["opp"] / A["poss"]
        teams.append(dict(
            id=a, ab=a, name=A["name"], color=COLORS.get(a, "#6b7a90"), gp=n, w=A["w"], l=A["l"],
            pts=per(A["pts"]), fgm=per(tm["fgm"]), fga=per(tm["fga"]), fgp=pc(tm["fgm"], tm["fga"]),
            fg3m=per(tm["fg3m"]), fg3a=per(tm["fg3a"]), fg3p=pc(tm["fg3m"], tm["fg3a"]), ftm=per(tm["ftm"]), fta=per(tm["fta"]),
            ftp=pc(tm["ftm"], tm["fta"]), oreb=per(tm["oreb"]), dreb=per(tm["dreb"]), reb=per(tm["reb"]), ast=per(tm["ast"]),
            tov=per(tm["tov"]), stl=per(tm["stl"]), blk=per(tm["blk"]), pf=per(tm["pf"]), pm=round((A["pts"] - A["opp"]) / n, 1),
            ortg=round(ortg, 1), drtg=round(drtg, 1), net=round(ortg - drtg, 1), pace=round(48 * A["poss"] / A["min"], 2),
            ts=pc(A["pts"], 2 * (tm["fga"] + 0.44 * tm["fta"])), efg=pc(tm["fgm"] + 0.5 * tm["fg3m"], tm["fga"]),
            astp=pc(tm["ast"], tm["fgm"]), orebp=pc(tm["oreb"], tm["oreb"] + op["dreb"]), drebp=pc(tm["dreb"], tm["dreb"] + op["oreb"]),
            tovp=pc(tm["tov"], tm["fga"] + 0.44 * tm["fta"] + tm["tov"]), ftr=rd(tm["fta"], tm["fga"]),
            oefg=pc(op["fgm"] + 0.5 * op["fg3m"], op["fga"]), otovp=pc(op["tov"], op["fga"] + 0.44 * op["fta"] + op["tov"]),
            oorebp=pc(op["oreb"], op["oreb"] + tm["dreb"]), oftr=rd(op["fta"], op["fga"]),
        ))
    log("  %s: %d teams, %d Jazz games" % (season, len(teams), len(games)))

    def stint_poss(jpos, opos):
        return 0.5 * (jpos[0] + 0.44 * jpos[1] - jpos[2] + jpos[3] + opos[0] + 0.44 * opos[1] - opos[2] + opos[3])

    oncourt = {}
    for five, sec, jp, opp_, jpos, opos, gid in stints:
        pss = stint_poss(jpos, opos)
        for pid in five:
            o = oncourt.setdefault(pid, [0.0, 0, 0, 0.0])
            o[0] += sec; o[1] += jp; o[2] += opp_; o[3] += pss

    def finish(P, is_jazz):
        P = dict(P)
        c, m = P.pop("ctx"), P["min"]
        P["ts"] = pc(P["pts"], 2 * (P["fga"] + 0.44 * P["fta"]))
        P["efg"] = pc(P["fgm"] + 0.5 * P["fg3m"], P["fga"])
        P["usg"] = pc((P["fga"] + 0.44 * P["fta"] + P["tov"]) * (c["tmmin"] / 5), m * (c["fga"] + 0.44 * c["fta"] + c["tov"])) if m else None
        den = (m / (c["tmmin"] / 5)) * c["fgm"] - P["fgm"] if c["tmmin"] else 0
        P["astp"] = pc(P["ast"], den) if den > 0 else None
        P["rebp"] = pc(P["reb"] * (c["tmmin"] / 5), m * (c["reb"] + c["oreb"])) if m else None
        P["pie"] = pc(c["pienum"], c["pie"]) if c["pie"] else None
        P["poss"] = round(c["poss"])
        P["net"] = round(100 * P["pm"] / c["poss"], 1) if c["poss"] > 0 else None
        P["ortg"] = P["drtg"] = None
        if is_jazz and P["id"] in oncourt and oncourt[P["id"]][3] > 0:
            sec, jp, opp_, pss = oncourt[P["id"]]
            P["ortg"], P["drtg"] = round(100 * jp / pss, 1), round(100 * opp_ / pss, 1)
            P["net"], P["poss"] = round(P["ortg"] - P["drtg"], 1), round(pss)
        ri = roster_info.get(P["id"], {})
        P["age"] = ri.get("age")
        if ri.get("pos"): P["pos"] = ri["pos"]
        if ri.get("num"): P["num"] = ri["num"]
        return P

    league = [finish(P, False) for P in players.values()]
    jz = [dict(finish(P, True), team=JAZZ) for P in jazz.values()]
    log("  %s: %d players league-wide, %d for the Jazz, %d Jazz lineup stints" % (season, len(league), len(jz), len(stints)))

    L5, L2, tot = {}, {}, [0.0, 0, 0, 0.0]
    for five, sec, jp, opp_, jpos, opos, gid in stints:
        if len(five) != 5:
            continue
        pt = jpos[0] + 0.44 * jpos[1] - jpos[2] + jpos[3]
        po = opos[0] + 0.44 * opos[1] - opos[2] + opos[3]
        for key, store in [(tuple(five), L5)] + [(pr, L2) for pr in itertools.combinations(five, 2)]:
            x = store.setdefault(key, dict(sec=0.0, jp=0, op=0, pt=0.0, po=0.0, g=set()))
            x["sec"] += sec; x["jp"] += jp; x["op"] += opp_; x["pt"] += pt; x["po"] += po; x["g"].add(gid)
        tot[0] += sec; tot[1] += jp; tot[2] += opp_; tot[3] += 0.5 * (pt + po)

    def lineup_rows(store):
        rows = []
        for key, x in store.items():
            pss = 0.5 * (x["pt"] + x["po"])
            if x["sec"] < 60 or pss <= 0:
                continue
            o, d_ = 100 * x["jp"] / pss, 100 * x["op"] / pss
            rows.append(dict(ids=list(key), gp=len(x["g"]), min=round(x["sec"] / 60, 1), poss=round(pss), ortg=round(o, 1),
                             drtg=round(d_, 1), net=round(o - d_, 1), pace=round(48 * 60 * pss / x["sec"], 1), pm=x["jp"] - x["op"]))
        return sorted(rows, key=lambda r: -r["min"])

    onoff = []
    for pid, (sec, jp, opp_, pss) in oncourt.items():
        if pss <= 0:
            continue
        fsec, fjp, fop, fpss = tot[0] - sec, tot[1] - jp, tot[2] - opp_, tot[3] - pss
        on_o, on_d = 100 * jp / pss, 100 * opp_ / pss
        off_o, off_d = (100 * fjp / fpss, 100 * fop / fpss) if fpss > 0 else (None, None)
        onoff.append(dict(id=pid, onMin=round(sec / 60, 1), onO=round(on_o, 1), onD=round(on_d, 1), onNet=round(on_o - on_d, 1),
                          offMin=round(fsec / 60, 1), offO=r1(off_o), offD=r1(off_d), offNet=r1(off_o - off_d) if off_o is not None else None))

    zb, za, zr, rows, zavg = [], [], [], [], {}
    ix = lambda L, v: L.index(v) if v in L else (L.append(v) or len(L) - 1)
    for team, pid, x, y, made, three, b, a, r, d in shots_all:
        z = zavg.setdefault((b, a, r), [0, 0]); z[0] += 1; z[1] += made
        if team == JAZZ:
            rows.append([pid, x, y, made, ix(zb, b), ix(za, a), ix(zr, r), three, d.replace("-", "")])
    lavg = [dict(zb=k[0], za=k[1], zr=k[2], fga=v[0], fgm=v[1]) for k, v in zavg.items()]
    log("  %s: %d Jazz shots, %d league shots" % (season, len(rows), len(shots_all)))

    return dict(label=season, teams=teams, games=games, players=league, jazz=jz,
                names={p["id"]: p["name"] for p in jz}, roster={p["id"]: dict(pos=p.get("pos", ""), num=p.get("num", "")) for p in jz},
                lineups={"5": lineup_rows(L5), "2": lineup_rows(L2)}, onoff=onoff,
                shots=dict(cols=["p", "x", "y", "m", "zb", "za", "zr", "three", "d"], zb=zb, za=za, zr=zr, rows=rows), zoneAvg=lavg)


def roster_info(fetch, log):
    try:
        r = fetch("%s/teams/%s/roster" % (API, JAZZ_ESPN_ID))
        return {a["id"]: dict(age=a.get("age"), pos=(a.get("position") or {}).get("abbreviation", ""), num=a.get("jersey", ""))
                for a in r.get("athletes", [])}
    except RuntimeError as e:
        log("  WARNING: roster unavailable (%s)" % e)
        return {}


# ---------------------------------------------------------------- main
def build_data(seasons, fetch=get_json, log=print, cache_path="jazz_cache.json", games_path="games_cache.json", today=None):
    today = today or dt.date.today()
    cur = current_season(today)
    load = lambda p: json.loads(Path(p).read_text()) if p and Path(p).exists() else {}
    done, gc = load(cache_path), load(games_path)
    if done.get("_v") != CACHE_VERSION:
        done = {"_v": CACHE_VERSION}
    if gc.get("_v") != CACHE_VERSION:
        gc = {"_v": CACHE_VERSION}
    ros = roster_info(fetch, log)
    out = {}
    for s in seasons:
        if s != cur and s in done:
            log("  %s: finished season, from cache" % s)
            out[s] = done[s]
            continue
        log("Pulling %s ..." % s)
        recs = collect_games(s, gc.setdefault(s, {}), fetch, log, today)
        if games_path:
            Path(games_path).write_text(json.dumps(gc, separators=(",", ":")))
        d = build_season(s, recs, ros if s == cur else {}, log)
        if d:
            out[s] = d
            if s != cur and today > dt.date(int(s[:4]) + 1, 4, 20):
                done[s] = d
    if cache_path:
        Path(cache_path).write_text(json.dumps(done, separators=(",", ":")))
    if not out:
        raise RuntimeError("No season returned data. Nothing was written.")
    now = dt.datetime.now(dt.timezone.utc)
    return dict(sample=False, source="ESPN", team=dict(id=JAZZ, ab=JAZZ, name="Utah Jazz"), current=cur if cur in out else list(out)[0],
                order=[s for s in seasons if s in out], updatedAt=now.strftime("%Y-%m-%dT%H:%M:%SZ"), generated=now.date().isoformat(),
                seasonGames=82, seasons=out)


def embed(html_path, data):
    p = Path(html_path)
    html = p.read_text(encoding="utf-8")
    pat = re.compile(r"/\*DATA_START\*/.*?/\*DATA_END\*/", re.S)
    if not pat.search(html):
        sys.exit("Could not find the /*DATA_START*/ ... /*DATA_END*/ markers in %s" % p)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    p.write_text(pat.sub(lambda m: "/*DATA_START*/const DATA = %s;/*DATA_END*/" % blob, html, count=1), encoding="utf-8")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default="index.html", help="dashboard file to update")
    ap.add_argument("--seasons", nargs="*", help="seasons like 2026-27 2025-26 (default: current and previous)")
    ap.add_argument("--cache", default="jazz_cache.json", help="finished-season cache (committed)")
    ap.add_argument("--games-cache", default="games_cache.json", help="per-game cache (kept between runs by the workflow)")
    a = ap.parse_args()
    cur = current_season()
    data = build_data(a.seasons or [cur, prev_season(cur)], cache_path=a.cache, games_path=a.games_cache)
    m = re.search(r"/\*DATA_START\*/const DATA = (.*?);/\*DATA_END\*/", Path(a.html).read_text(encoding="utf-8"), re.S)
    try:
        old = json.loads(m.group(1).replace("<\\/", "</")) if m else None
    except json.JSONDecodeError:
        old = None
    if old and old.get("seasons") == json.loads(json.dumps(data["seasons"])) and old.get("order") == data["order"]:
        print("No new games since the last refresh; %s left unchanged." % a.html)
        return
    p = embed(a.html, data)
    print("Updated %s (%d KB). Seasons: %s." % (p, p.stat().st_size // 1024, ", ".join(data["order"])))


if __name__ == "__main__":
    main()
