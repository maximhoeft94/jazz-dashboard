#!/usr/bin/env python3
"""Pull Utah Jazz and league data from stats.nba.com and embed it in index.html.

    python fetch_jazz.py                         # current + previous season, rewrites ./index.html
    python fetch_jazz.py --seasons 2026-27       # one season only
    python fetch_jazz.py --html docs/index.html
    python fetch_jazz.py --sample                # simulated API responses (no network), for layout testing

Regular season only. Needs Python 3.9+ and no third-party packages. Makes about 12 requests per season.
Finished seasons are cached in jazz_cache.json, so later runs only re-pull the current season.

stats.nba.com is undocumented, changes without notice, and often refuses requests from cloud servers
(including some GitHub Actions runners). If a pull hangs or times out, run it on your own computer.

Endpoints used (no API key needed), all under https://stats.nba.com/stats/:
    leaguedashteamstats      all 30 teams: Base, Advanced, Four Factors (league averages and ranks)
    leaguegamelog            every Jazz game: date, opponent, score
    leaguedashplayerstats    every player in the league: Base + Advanced totals (for percentiles)
    commonteamroster         Jazz positions and jersey numbers
    leaguedashlineups        Jazz 5-man and 2-man lineups (Advanced + Base)
    teamplayeronoffsummary   Jazz on/off court ratings per player
    shotchartdetail          every Jazz shot (x/y, zone, made) and league zone averages
"""
import argparse, datetime as dt, json, math, random, re, sys, time, zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://stats.nba.com/stats"
JAZZ_ID = 1610612762
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

# team id -> (abbreviation, display color). Colors are for charts only.
TEAMS = {
    1610612737: ("ATL", "#e03a3e"), 1610612738: ("BOS", "#007a33"), 1610612751: ("BKN", "#555555"),
    1610612766: ("CHA", "#00788c"), 1610612741: ("CHI", "#ce1141"), 1610612739: ("CLE", "#860038"),
    1610612742: ("DAL", "#00538c"), 1610612743: ("DEN", "#0e2240"), 1610612765: ("DET", "#c8102e"),
    1610612744: ("GSW", "#1d428a"), 1610612745: ("HOU", "#ce1141"), 1610612754: ("IND", "#002d62"),
    1610612746: ("LAC", "#1d428a"), 1610612747: ("LAL", "#552583"), 1610612763: ("MEM", "#5d76a9"),
    1610612748: ("MIA", "#98002e"), 1610612749: ("MIL", "#00471b"), 1610612750: ("MIN", "#236192"),
    1610612740: ("NOP", "#0c2340"), 1610612752: ("NYK", "#f58426"), 1610612760: ("OKC", "#007ac1"),
    1610612753: ("ORL", "#0077c0"), 1610612755: ("PHI", "#006bb6"), 1610612756: ("PHX", "#e56020"),
    1610612757: ("POR", "#e03a3e"), 1610612758: ("SAC", "#5a2d81"), 1610612759: ("SAS", "#8a8d8f"),
    1610612761: ("TOR", "#ce1141"), 1610612762: ("UTA", "#4b2a85"), 1610612764: ("WAS", "#002b5c"),
}
SEASON_TYPE = "Regular Season"


# ---------------------------------------------------------------- http
def get_json(endpoint, params, tries=4):
    url = "%s/%s?%s" % (BASE, endpoint, urlencode(params))
    last = None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=45) as r:
                data = json.loads(r.read().decode("utf-8"))
            time.sleep(0.8)  # stats.nba.com rate-limits hard; stay polite
            return data
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError("Request failed after %d tries: %s (%s)" % (tries, url, last))


def sets(resp):
    """stats.nba.com returns {"resultSets":[{name, headers, rowSet}]} (or "resultSet"). Return {name: [row dicts]}."""
    rs = resp.get("resultSets", resp.get("resultSet"))
    if isinstance(rs, dict):
        rs = [rs]
    out = {}
    for s in rs or []:
        hdr = s.get("headers", [])
        if hdr and isinstance(hdr[0], dict):  # some endpoints nest headers; flatten the last row of names
            hdr = hdr[-1].get("columnNames", [])
        out[s.get("name", "")] = [dict(zip(hdr, row)) for row in s.get("rowSet", [])]
    return out


def first(resp):
    s = sets(resp)
    return next(iter(s.values()), [])


def current_season(today=None):
    today = today or dt.date.today()
    y = today.year if today.month >= 10 else today.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def prev_season(s):
    y = int(s[:4]) - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def r(v, d=1):
    return None if v is None else round(float(v), d)


def pct(v, d=1):
    """Fractions (0.553) become percentages (55.3)."""
    if v is None:
        return None
    v = float(v)
    return round(v * 100 if abs(v) <= 1.5 else v, d)


# ---------------------------------------------------------------- params
def dash_params(season, **kw):
    p = dict(Conference="", DateFrom="", DateTo="", Division="", GameScope="", GameSegment="", LastNGames=0,
             LeagueID="00", Location="", MeasureType="Base", Month=0, OpponentTeamID=0, Outcome="", PORound=0,
             PaceAdjust="N", PerMode="PerGame", Period=0, PlayerExperience="", PlayerPosition="", PlusMinus="N",
             Rank="N", Season=season, SeasonSegment="", SeasonType=SEASON_TYPE, ShotClockRange="", StarterBench="",
             TeamID=0, TwoWay=0, VsConference="", VsDivision="")
    p.update(kw)
    return p


def player_params(season, **kw):
    p = dash_params(season, College="", Country="", DraftPick="", DraftYear="", Height="", Weight="", PerMode="Totals")
    p.update(kw)
    return p


def lineup_params(season, n, measure):
    p = dash_params(season, GroupQuantity=n, MeasureType=measure, PerMode="Totals", TeamID=JAZZ_ID)
    for k in ("GameScope", "PlayerExperience", "PlayerPosition", "StarterBench", "TwoWay"):
        p.pop(k, None)
    return p


def onoff_params(season):
    p = dash_params(season, PerMode="Totals", TeamID=JAZZ_ID)
    for k in ("Conference", "Division", "GameScope", "PORound", "PlayerExperience", "PlayerPosition", "ShotClockRange", "StarterBench", "TwoWay"):
        p.pop(k, None)
    return p


def shot_params(season):
    return dict(AheadBehind="", ClutchTime="", ContextFilter="", ContextMeasure="FGA", DateFrom="", DateTo="",
                EndPeriod="", EndRange="", GameID="", GameSegment="", LastNGames=0, LeagueID="00", Location="",
                Month=0, OpponentTeamID=0, Outcome="", Period=0, PlayerID=0, PlayerPosition="", PointDiff="",
                Position="", RangeType="", RookieYear="", Season=season, SeasonSegment="", SeasonType=SEASON_TYPE,
                StartPeriod="", StartRange="", TeamID=JAZZ_ID, VsConference="", VsDivision="")


# ---------------------------------------------------------------- one season
def build_season(season, fetch=get_json, log=print):
    # --- all 30 teams
    base = {t["TEAM_ID"]: t for t in first(fetch("leaguedashteamstats", dash_params(season)))}
    if not base:
        log("  %s: no team stats yet (season not started?)" % season)
        return None
    adv = {t["TEAM_ID"]: t for t in first(fetch("leaguedashteamstats", dash_params(season, MeasureType="Advanced")))}
    ff = {t["TEAM_ID"]: t for t in first(fetch("leaguedashteamstats", dash_params(season, MeasureType="Four Factors")))}
    teams = []
    for tid, b in base.items():
        if tid not in TEAMS:
            continue
        a, f = adv.get(tid, {}), ff.get(tid, {})
        ab, color = TEAMS[tid]
        teams.append(dict(
            id=tid, ab=ab, name=b.get("TEAM_NAME", ab), color=color,
            gp=b.get("GP", 0), w=b.get("W", 0), l=b.get("L", 0),
            pts=r(b.get("PTS")), fgm=r(b.get("FGM")), fga=r(b.get("FGA")), fgp=pct(b.get("FG_PCT")),
            fg3m=r(b.get("FG3M")), fg3a=r(b.get("FG3A")), fg3p=pct(b.get("FG3_PCT")),
            ftm=r(b.get("FTM")), fta=r(b.get("FTA")), ftp=pct(b.get("FT_PCT")),
            oreb=r(b.get("OREB")), dreb=r(b.get("DREB")), reb=r(b.get("REB")), ast=r(b.get("AST")),
            tov=r(b.get("TOV")), stl=r(b.get("STL")), blk=r(b.get("BLK")), pf=r(b.get("PF")), pm=r(b.get("PLUS_MINUS")),
            ortg=r(a.get("OFF_RATING")), drtg=r(a.get("DEF_RATING")), net=r(a.get("NET_RATING")), pace=r(a.get("PACE"), 2),
            ts=pct(a.get("TS_PCT")), efg=pct(a.get("EFG_PCT")), astp=pct(a.get("AST_PCT")), orebp=pct(a.get("OREB_PCT")),
            drebp=pct(a.get("DREB_PCT")), tovp=pct(a.get("TM_TOV_PCT")),
            ftr=r(f.get("FTA_RATE"), 3), oefg=pct(f.get("OPP_EFG_PCT")), otovp=pct(f.get("OPP_TOV_PCT")),
            oorebp=pct(f.get("OPP_OREB_PCT")), oftr=r(f.get("OPP_FTA_RATE"), 3),
        ))
    log("  %s: %d teams" % (season, len(teams)))

    # --- Jazz games
    gl = first(fetch("leaguegamelog", dict(Counter=0, DateFrom="", DateTo="", Direction="ASC", LeagueID="00",
                                           PlayerOrTeam="T", Season=season, SeasonType=SEASON_TYPE, Sorter="DATE")))
    games = []
    for g in gl:
        if g.get("TEAM_ID") != JAZZ_ID or g.get("PTS") is None:
            continue
        m = g.get("MATCHUP", "")
        opp = m.split()[-1] if m else ""
        pts = int(g["PTS"])
        pm = g.get("PLUS_MINUS")
        games.append(dict(id=g.get("GAME_ID"), d=str(g.get("GAME_DATE", ""))[:10], opp=opp, home="vs." in m,
                          pts=pts, opts=int(round(pts - pm)) if pm is not None else None))
    games.sort(key=lambda x: (x["d"], x["id"] or ""))
    log("  %s: %d Jazz games" % (season, len(games)))

    # --- every player (league-wide, for percentiles) and Jazz-only totals (traded players count only Jazz games)
    def pull_players(**kw):
        pb = first(fetch("leaguedashplayerstats", player_params(season, **kw)))
        pa = {p["PLAYER_ID"]: p for p in first(fetch("leaguedashplayerstats", player_params(season, MeasureType="Advanced", **kw)))}
        out = []
        for p in pb:
            if not p.get("GP"):
                continue
            a = pa.get(p["PLAYER_ID"], {})
            out.append(dict(
                id=p["PLAYER_ID"], name=p.get("PLAYER_NAME", ""), team=p.get("TEAM_ABBREVIATION", ""), age=r(p.get("AGE"), 0),
                gp=p.get("GP", 0), min=r(p.get("MIN")), pts=p.get("PTS", 0), fgm=p.get("FGM", 0), fga=p.get("FGA", 0),
                fg3m=p.get("FG3M", 0), fg3a=p.get("FG3A", 0), ftm=p.get("FTM", 0), fta=p.get("FTA", 0),
                oreb=p.get("OREB", 0), dreb=p.get("DREB", 0), reb=p.get("REB", 0), ast=p.get("AST", 0), tov=p.get("TOV", 0),
                stl=p.get("STL", 0), blk=p.get("BLK", 0), pf=p.get("PF", 0), pm=p.get("PLUS_MINUS", 0),
                ortg=r(a.get("OFF_RATING")), drtg=r(a.get("DEF_RATING")), net=r(a.get("NET_RATING")), usg=pct(a.get("USG_PCT")),
                ts=pct(a.get("TS_PCT")), efg=pct(a.get("EFG_PCT")), astp=pct(a.get("AST_PCT")), rebp=pct(a.get("REB_PCT")),
                pie=pct(a.get("PIE")), poss=a.get("POSS"),
            ))
        return out
    players = pull_players()
    jazz = pull_players(TeamID=JAZZ_ID)
    for p in jazz:
        p["team"] = "UTA"
    log("  %s: %d players league-wide, %d for the Jazz" % (season, len(players), len(jazz)))

    # --- Jazz roster (positions, numbers)
    roster = {}
    try:
        rs = sets(fetch("commonteamroster", dict(LeagueID="00", Season=season, TeamID=JAZZ_ID)))
        for p in rs.get("CommonTeamRoster", []):
            roster[str(p["PLAYER_ID"])] = dict(pos=p.get("POSITION", ""), num=p.get("NUM", ""))
    except RuntimeError as e:
        log("  WARNING: roster unavailable (%s)" % e)

    # --- lineups
    lineups = {}
    for n in (5, 2):
        la = first(fetch("leaguedashlineups", lineup_params(season, n, "Advanced")))
        lb = {x["GROUP_ID"]: x for x in first(fetch("leaguedashlineups", lineup_params(season, n, "Base")))}
        out = []
        for x in la:
            ids = [int(v) for v in str(x.get("GROUP_ID", "")).strip("-").split("-") if v]
            b = lb.get(x.get("GROUP_ID"), {})
            mins = r(x.get("MIN"))
            pace = x.get("PACE")
            poss = x.get("POSS") or (round(mins * pace / 48) if mins and pace else None)
            out.append(dict(ids=ids, gp=x.get("GP", 0), min=mins, poss=poss, ortg=r(x.get("OFF_RATING")),
                            drtg=r(x.get("DEF_RATING")), net=r(x.get("NET_RATING")), pace=r(pace), pm=b.get("PLUS_MINUS")))
        lineups[str(n)] = out
        log("  %s: %d %d-man lineups" % (season, len(out), n))

    # --- on/off
    onoff = []
    try:
        oo = sets(fetch("teamplayeronoffsummary", onoff_params(season)))
        on = {x["VS_PLAYER_ID"]: x for x in oo.get("PlayersOnCourtTeamPlayerOnOffSummary", [])}
        off = {x["VS_PLAYER_ID"]: x for x in oo.get("PlayersOffCourtTeamPlayerOnOffSummary", [])}
        for pid, x in on.items():
            y = off.get(pid, {})
            onoff.append(dict(id=pid, onMin=r(x.get("MIN")), onO=r(x.get("OFF_RATING")), onD=r(x.get("DEF_RATING")), onNet=r(x.get("NET_RATING")),
                              offMin=r(y.get("MIN")), offO=r(y.get("OFF_RATING")), offD=r(y.get("DEF_RATING")), offNet=r(y.get("NET_RATING"))))
    except RuntimeError as e:
        log("  WARNING: on/off unavailable (%s)" % e)

    # --- shots
    sc = sets(fetch("shotchartdetail", shot_params(season)))
    zb, za, zr = [], [], []
    ix = lambda L, v: L.index(v) if v in L else (L.append(v) or len(L) - 1)
    rows = []
    for s in sc.get("Shot_Chart_Detail", []):
        rows.append([s["PLAYER_ID"], int(s["LOC_X"]), int(s["LOC_Y"]), int(s["SHOT_MADE_FLAG"]),
                     ix(zb, s["SHOT_ZONE_BASIC"]), ix(za, s["SHOT_ZONE_AREA"]), ix(zr, s["SHOT_ZONE_RANGE"]),
                     1 if "3PT" in str(s.get("SHOT_TYPE", "")) else 0, str(s.get("GAME_DATE", ""))])
    lavg = [dict(zb=z["SHOT_ZONE_BASIC"], za=z["SHOT_ZONE_AREA"], zr=z["SHOT_ZONE_RANGE"], fga=z["FGA"], fgm=z["FGM"])
            for z in sc.get("LeagueAverages", [])]
    log("  %s: %d Jazz shots, %d league zone averages" % (season, len(rows), len(lavg)))

    names = {str(p["id"]): p["name"] for p in jazz}
    for s_ in sc.get("Shot_Chart_Detail", []):
        names.setdefault(str(s_["PLAYER_ID"]), s_.get("PLAYER_NAME", ""))
    return dict(label=season, teams=teams, games=games, players=players, jazz=jazz, names=names, roster=roster, lineups=lineups, onoff=onoff,
                shots=dict(cols=["p", "x", "y", "m", "zb", "za", "zr", "three", "d"], zb=zb, za=za, zr=zr, rows=rows),
                zoneAvg=lavg)


def build_data(seasons, fetch=get_json, log=print, cache_path=None, sample=False):
    cache = {}
    if cache_path and Path(cache_path).exists():
        cache = json.loads(Path(cache_path).read_text())
    cur = current_season()
    out = {}
    for s in seasons:
        if s != cur and s in cache and not sample:
            log("  %s: from cache" % s)
            out[s] = cache[s]
            continue
        log("Pulling %s ..." % s)
        d = build_season(s, fetch, log)
        if d:
            out[s] = d
            if s != cur and cache_path and not sample:
                cache[s] = d
    if cache_path and cache and not sample:
        Path(cache_path).write_text(json.dumps(cache))
    if not out:
        raise RuntimeError("No season returned data. Nothing was written.")
    now = dt.datetime.now(dt.timezone.utc)
    return dict(sample=sample, team=dict(id=JAZZ_ID, ab="UTA", name="Utah Jazz"), current=cur if cur in out else list(out)[0],
                order=[s for s in seasons if s in out], updatedAt=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                generated=now.date().isoformat(), seasonGames=82, seasons=out)


def embed(html_path, data):
    p = Path(html_path)
    html = p.read_text(encoding="utf-8")
    pat = re.compile(r"/\*DATA_START\*/.*?/\*DATA_END\*/", re.S)
    if not pat.search(html):
        sys.exit("Could not find the /*DATA_START*/ ... /*DATA_END*/ markers in %s" % p)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    p.write_text(pat.sub(lambda m: "/*DATA_START*/const DATA = %s;/*DATA_END*/" % blob, html, count=1), encoding="utf-8")
    return p


# ---------------------------------------------------------------- sample mode
# A fake stats.nba.com: returns responses shaped like the real endpoints, so --sample exercises the same parsing code.
# Every number and every Jazz player name below is invented.
SAMPLE_JAZZ = [("Darnell Pryce", "G", 24), ("Tobias Wren", "F", 22), ("Ezra Malone", "C", 27), ("Kofi Adeyemi", "F", 21),
               ("Luca Brandt", "G", 20), ("Isaiah Holt", "G-F", 25), ("Marcus Bellamy", "C", 29), ("Jalen Ortiz", "G", 23),
               ("Rafael Duarte", "F", 26), ("Caleb Whitfield", "F-C", 22), ("Noah Kingsley", "G", 30), ("Andre Fontaine", "F", 19),
               ("Silas Gray", "C", 24), ("Victor Okafor", "G", 21), ("Elijah Stroud", "F", 28)]


def sample_fetch_factory(seed=7, today=dt.date(2026, 11, 15)):
    rnd = random.Random(seed)
    rating = {tid: rnd.gauss(0, 5.5) for tid in TEAMS}
    rating[JAZZ_ID] = -4.8
    def res(name, rows):
        hdr = list(rows[0].keys()) if rows else []
        return {"resultSets": [{"name": name, "headers": hdr, "rowSet": [[x[h] for h in hdr] for x in rows]}]}

    cache = {}

    def season_world(season):
        if season in cache:
            return cache[season]
        R = random.Random(zlib.crc32(season.encode()) % 10000 + seed)
        start = dt.date(int(season[:4]), 10, 22)
        end = min(dt.date(int(season[:4]) + 1, 4, 12), today)
        ids = list(TEAMS)
        games, gid, d, played = [], 1, start, {}
        while d <= end:
            R.shuffle(ids)
            avail = [t for t in ids if played.get(t, 0) < 82]
            for i in range(0, min(len(avail) - 1, R.choice([10, 12, 14, 16])), 2):
                h, a = avail[i], avail[i + 1]
                played[h] = played.get(h, 0) + 1; played[a] = played.get(a, 0) + 1
                pace = R.gauss(99.5, 3)
                hp = int(round(pace * (1.14 + (rating[h] + 2.5 + R.gauss(0, 11)) / 200)))
                ap = int(round(pace * (1.14 + (rating[a] + R.gauss(0, 11)) / 200)))
                if hp == ap:
                    hp += 1 + R.randint(0, 6)
                games.append(dict(id="002%s%05d" % (season[2:4], gid), d=d.isoformat(), h=h, a=a, hp=hp, ap=ap, pace=pace))
                gid += 1
            d += dt.timedelta(days=1)
        # jazz roster + league players
        jz = []
        for i, (n, pos, age) in enumerate(SAMPLE_JAZZ):
            jz.append(dict(id=1650000 + i, name=n, pos=pos, age=age + (0 if season.startswith("2026") else -1),
                           skill=R.gauss(0, 1) + (1.4 if i < 3 else 0.6 if i < 7 else -0.2), share=max(0.1, 1 - i * 0.065)))
        league = []
        pid = 1640000
        for tid, (ab, _) in TEAMS.items():
            if tid == JAZZ_ID:
                continue
            for k in range(14):
                league.append(dict(id=pid, name="Player %s-%d" % (ab, k + 1), team=ab, tid=tid, age=R.randint(20, 35),
                                   skill=R.gauss(0, 1) + (1.0 if k < 3 else 0), share=max(0.1, 1 - k * 0.07)))
                pid += 1
        cache[season] = dict(games=games, jz=jz, league=league, R=R)
        return cache[season]

    def team_totals(world, tid):
        gs = [g for g in world["games"] if tid in (g["h"], g["a"])]
        n = len(gs) or 1
        pf = sum(g["hp"] if g["h"] == tid else g["ap"] for g in gs)
        pa = sum(g["ap"] if g["h"] == tid else g["hp"] for g in gs)
        w = sum(1 for g in gs if (g["hp"] > g["ap"]) == (g["h"] == tid))
        pace = sum(g["pace"] for g in gs) / n
        return gs, n, pf, pa, w, pace

    def fake(endpoint, p):
        season = p.get("Season")
        W = season_world(season)
        R = random.Random(zlib.crc32((endpoint + json.dumps(p, sort_keys=True)).encode()) % 100000)
        if endpoint == "leaguedashteamstats":
            rows = []
            for tid, (ab, _) in TEAMS.items():
                gs, n, pf, pa, w, pace = team_totals(W, tid)
                ppg, oppg = pf / n, pa / n
                q = rating[tid]
                fga = 88 + R.gauss(0, 2.5); fg3a = 37 + R.gauss(0, 4); fta = 22 + R.gauss(0, 2.5)
                ftm = fta * (0.78 + R.gauss(0, .03)); fg3m = fg3a * (0.36 + q / 300 + R.gauss(0, .01))
                fgm = (ppg - ftm - fg3m) / 2
                oreb = 10.5 + R.gauss(0, 1.3); dreb = 33 + R.gauss(0, 1.5); tov = 14 + R.gauss(0, 1.2) - q / 12
                poss = pace
                if p["MeasureType"] == "Base":
                    rows.append(dict(TEAM_ID=tid, TEAM_NAME=ab, GP=n, W=w, L=n - w, PTS=ppg, FGM=fgm, FGA=fga, FG_PCT=fgm / fga,
                                     FG3M=fg3m, FG3A=fg3a, FG3_PCT=fg3m / fg3a, FTM=ftm, FTA=fta, FT_PCT=ftm / fta, OREB=oreb, DREB=dreb,
                                     REB=oreb + dreb, AST=25 + q / 3 + R.gauss(0, 1.5), TOV=tov, STL=7.8 + R.gauss(0, .8),
                                     BLK=5 + R.gauss(0, .8), PF=19 + R.gauss(0, 1.2), PLUS_MINUS=ppg - oppg))
                elif p["MeasureType"] == "Advanced":
                    o, d_ = 100 * ppg / poss, 100 * oppg / poss
                    rows.append(dict(TEAM_ID=tid, OFF_RATING=o, DEF_RATING=d_, NET_RATING=o - d_, PACE=pace,
                                     TS_PCT=ppg / (2 * (fga + .44 * fta)), EFG_PCT=(fgm + .5 * fg3m) / fga, AST_PCT=.6 + R.gauss(0, .04),
                                     OREB_PCT=oreb / (oreb + 33), DREB_PCT=dreb / (dreb + 10.5), TM_TOV_PCT=tov / poss))
                else:
                    rows.append(dict(TEAM_ID=tid, EFG_PCT=(fgm + .5 * fg3m) / fga, FTA_RATE=fta / fga, TM_TOV_PCT=tov / poss,
                                     OREB_PCT=oreb / (oreb + 33), OPP_EFG_PCT=.54 - q / 250 + R.gauss(0, .01),
                                     OPP_FTA_RATE=.25 + R.gauss(0, .02), OPP_TOV_PCT=.14 + R.gauss(0, .01), OPP_OREB_PCT=.24 + R.gauss(0, .015)))
            return res("LeagueDashTeamStats", rows)
        if endpoint == "leaguegamelog":
            rows = []
            for g in W["games"]:
                for tid, opp, pts, opts, home in ((g["h"], g["a"], g["hp"], g["ap"], True), (g["a"], g["h"], g["ap"], g["hp"], False)):
                    rows.append(dict(TEAM_ID=tid, TEAM_ABBREVIATION=TEAMS[tid][0], GAME_ID=g["id"], GAME_DATE=g["d"],
                                     MATCHUP="%s %s %s" % (TEAMS[tid][0], "vs." if home else "@", TEAMS[opp][0]),
                                     WL="W" if pts > opts else "L", PTS=pts, PLUS_MINUS=pts - opts))
            return res("LeagueGameLog", rows)
        if endpoint == "leaguedashplayerstats":
            _, njg, *_ = team_totals(W, JAZZ_ID)
            rows = []
            only_jazz = p.get("TeamID") == JAZZ_ID
            R = random.Random(zlib.crc32(("players" + season).encode()))
            for pl in W["jz"] + W["league"]:
                team = pl.get("team", "UTA")
                ng = njg if team == "UTA" else 12 + (len(W["games"]) * 2 // 30 - 12)
                gp = max(1, int(ng * min(1, .55 + pl["share"] * .5) - R.randint(0, max(1, ng // 8))))
                mpg = 8 + 26 * pl["share"] + R.gauss(0, 2)
                mins = gp * mpg
                usg = .14 + .1 * pl["share"] + .02 * pl["skill"]
                fga = mins / 36 * (6 + 12 * usg / .25) ; fg3a = fga * R.uniform(.2, .55); fta = fga * R.uniform(.15, .35)
                fg3m = fg3a * (.35 + .02 * pl["skill"] + R.gauss(0, .02)); fg2a = fga - fg3a; fg2m = fg2a * (.53 + .025 * pl["skill"] + R.gauss(0, .02))
                ftm = fta * (.76 + R.gauss(0, .06)); fgm = fg2m + fg3m; pts = 2 * fg2m + 3 * fg3m + ftm
                reb = mins / 36 * R.uniform(3.5, 11); oreb = reb * R.uniform(.15, .35); ast = mins / 36 * R.uniform(1.5, 8)
                stl = mins / 36 * R.uniform(.6, 1.8); blk = mins / 36 * R.uniform(.2, 2); tov = mins / 36 * R.uniform(1, 3.5)
                o = 112 + 3 * pl["skill"] + R.gauss(0, 2); d_ = 114 - rating.get(pl.get("tid", JAZZ_ID), 0) / 2 + R.gauss(0, 2)
                astp, rebp, pmv = R.uniform(.05, .35), R.uniform(.05, .2), R.gauss(0, 1.5)
                if only_jazz and team != "UTA":
                    continue
                if p["MeasureType"] == "Base":
                    rows.append(dict(PLAYER_ID=pl["id"], PLAYER_NAME=pl["name"], TEAM_ABBREVIATION=team, AGE=pl["age"], GP=gp, MIN=mins,
                                     PTS=round(pts), FGM=round(fgm), FGA=round(fga), FG3M=round(fg3m), FG3A=round(fg3a), FTM=round(ftm), FTA=round(fta),
                                     OREB=round(oreb), DREB=round(reb - oreb), REB=round(reb), AST=round(ast), TOV=round(tov), STL=round(stl),
                                     BLK=round(blk), PF=round(mins / 36 * 2.8), PLUS_MINUS=round(gp * (pl["skill"] * 1.5 + rating.get(pl.get("tid", JAZZ_ID), 0) / 3 + pmv))))
                else:
                    rows.append(dict(PLAYER_ID=pl["id"], OFF_RATING=o, DEF_RATING=d_, NET_RATING=o - d_, USG_PCT=usg,
                                     TS_PCT=pts / (2 * (fga + .44 * fta)), EFG_PCT=(fgm + .5 * fg3m) / fga, AST_PCT=astp,
                                     REB_PCT=rebp, PIE=.08 + .03 * pl["skill"] + .04 * pl["share"], POSS=round(mins * 2.08)))
            return res("LeagueDashPlayerStats", rows)
        if endpoint == "commonteamroster":
            return res("CommonTeamRoster", [dict(PLAYER_ID=pl["id"], PLAYER=pl["name"], POSITION=pl["pos"], NUM=str(i * 3 % 50))
                                            for i, pl in enumerate(W["jz"])])
        if endpoint == "leaguedashlineups":
            n = p["GroupQuantity"]
            R = random.Random(zlib.crc32(("lineups%s%d" % (season, n)).encode()))
            top = W["jz"][:10]
            combos, rows, seen = [], [], set()
            for _ in range(60 if n == 5 else 45):
                picks = sorted(R.sample(top[:7], min(n, 3)) + R.sample(top[3:] if n == 5 else top, max(0, n - 3)), key=lambda x: x["id"])
                key = tuple(sorted(set(x["id"] for x in picks)))
                if len(key) != n or key in seen:
                    continue
                seen.add(key)
                q = sum(x["skill"] for x in picks) / n
                mins = max(2, R.expovariate(1 / (70 if n == 5 else 220)))
                pace = 99 + R.gauss(0, 2.5)
                o = 110 + 3.5 * q + R.gauss(0, 30 / (mins ** .5)); d_ = 116 - 1.5 * q + R.gauss(0, 30 / (mins ** .5))
                gid = "-" + "-".join(str(i) for i in key) + "-"
                if p["MeasureType"] == "Advanced":
                    rows.append(dict(GROUP_ID=gid, GP=max(1, int(mins // 9)), MIN=mins, OFF_RATING=o, DEF_RATING=d_, NET_RATING=o - d_,
                                     PACE=pace, POSS=round(mins * pace / 48)))
                else:
                    rows.append(dict(GROUP_ID=gid, PLUS_MINUS=round((o - d_) * mins * pace / 48 / 100)))
            return res("Lineups", rows)
        if endpoint == "teamplayeronoffsummary":
            on, off = [], []
            for pl in W["jz"]:
                m = 300 + 1500 * pl["share"]
                o = 112 + 2.5 * pl["skill"] + R.gauss(0, 2); d_ = 117 - pl["skill"] + R.gauss(0, 2)
                on.append(dict(VS_PLAYER_ID=pl["id"], MIN=m, OFF_RATING=o, DEF_RATING=d_, NET_RATING=o - d_))
                o2 = 113.5 - 1.2 * pl["skill"] + R.gauss(0, 1.5); d2 = 114.5 + 0.6 * pl["skill"] + R.gauss(0, 1.5)
                off.append(dict(VS_PLAYER_ID=pl["id"], MIN=2200 - m * .6, OFF_RATING=o2, DEF_RATING=d2, NET_RATING=o2 - d2))
            hdr_on = list(on[0]); hdr_off = list(off[0])
            return {"resultSets": [
                {"name": "PlayersOnCourtTeamPlayerOnOffSummary", "headers": hdr_on, "rowSet": [[x[h] for h in hdr_on] for x in on]},
                {"name": "PlayersOffCourtTeamPlayerOnOffSummary", "headers": hdr_off, "rowSet": [[x[h] for h in hdr_off] for x in off]}]}
        if endpoint == "shotchartdetail":
            zones = [  # (basic, area, range, cx, cy, spread, league fg%, is3, weight)
                ("Restricted Area", "Center(C)", "Less Than 8 ft.", 0, 10, 25, .655, 0, 30),
                ("In The Paint (Non-RA)", "Center(C)", "8-16 ft.", 0, 90, 35, .44, 0, 9),
                ("In The Paint (Non-RA)", "Left Side(L)", "8-16 ft.", -60, 40, 20, .42, 0, 4),
                ("In The Paint (Non-RA)", "Right Side(R)", "8-16 ft.", 60, 40, 20, .42, 0, 4),
                ("Mid-Range", "Left Side(L)", "8-16 ft.", -130, 40, 20, .41, 0, 2),
                ("Mid-Range", "Right Side(R)", "8-16 ft.", 130, 40, 20, .41, 0, 2),
                ("Mid-Range", "Left Side Center(LC)", "16-24 ft.", -140, 150, 25, .41, 0, 3),
                ("Mid-Range", "Right Side Center(RC)", "16-24 ft.", 140, 150, 25, .41, 0, 3),
                ("Mid-Range", "Center(C)", "16-24 ft.", 0, 190, 25, .43, 0, 3),
                ("Left Corner 3", "Left Side(L)", "24+ ft.", -228, 30, 8, .385, 1, 5),
                ("Right Corner 3", "Right Side(R)", "24+ ft.", 228, 30, 8, .39, 1, 5),
                ("Above the Break 3", "Left Side Center(LC)", "24+ ft.", -170, 200, 22, .355, 1, 9),
                ("Above the Break 3", "Right Side Center(RC)", "24+ ft.", 170, 200, 22, .355, 1, 9),
                ("Above the Break 3", "Center(C)", "24+ ft.", 0, 265, 22, .355, 1, 8),
            ]
            gs = [g for g in W["games"] if JAZZ_ID in (g["h"], g["a"])]
            players = W["jz"][:12]
            wts = [pl["share"] ** 1.6 for pl in players]
            rows = []
            for g in gs:
                for _ in range(int(R.gauss(88, 4))):
                    pl = R.choices(players, wts)[0]
                    z = R.choices(zones, [zz[8] for zz in zones])[0]
                    x = int(max(-247, min(247, R.gauss(z[3], z[5])))); y = int(max(-45, R.gauss(z[4], z[5] * .8)))
                    if z[7] and z[0] == "Above the Break 3":
                        ang = R.uniform(.25, 2.9); rad = R.uniform(240, 275)
                        x, y = int(rad * math.cos(ang)), int(rad * math.sin(ang))
                    made = 1 if R.random() < z[6] + .025 * pl["skill"] - .01 else 0
                    rows.append(dict(PLAYER_ID=pl["id"], PLAYER_NAME=pl["name"], LOC_X=x, LOC_Y=y, SHOT_MADE_FLAG=made, SHOT_ZONE_BASIC=z[0],
                                     SHOT_ZONE_AREA=z[1], SHOT_ZONE_RANGE=z[2], SHOT_TYPE="3PT Field Goal" if z[7] else "2PT Field Goal",
                                     GAME_DATE=g["d"].replace("-", "")))
            lavg = [dict(SHOT_ZONE_BASIC=z[0], SHOT_ZONE_AREA=z[1], SHOT_ZONE_RANGE=z[2], FGA=z[8] * 900, FGM=round(z[8] * 900 * z[6])) for z in zones]
            h1, h2 = list(rows[0]) if rows else [], list(lavg[0])
            return {"resultSets": [
                {"name": "Shot_Chart_Detail", "headers": h1, "rowSet": [[x[h] for h in h1] for x in rows]},
                {"name": "LeagueAverages", "headers": h2, "rowSet": [[x[h] for h in h2] for x in lavg]}]}
        raise RuntimeError("sample: unknown endpoint " + endpoint)

    return fake


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default="index.html", help="dashboard file to update")
    ap.add_argument("--seasons", nargs="*", help="seasons like 2026-27 2025-26 (default: current and previous)")
    ap.add_argument("--cache", default="jazz_cache.json", help="where finished seasons are cached")
    ap.add_argument("--sample", action="store_true", help="use simulated responses instead of stats.nba.com")
    a = ap.parse_args()
    if a.sample:
        seasons = a.seasons or ["2026-27", "2025-26"]
        data = build_data(seasons, fetch=sample_fetch_factory(), sample=True)
        data["current"] = seasons[0]
    else:
        cur = current_season()
        seasons = a.seasons or [cur, prev_season(cur)]
        data = build_data(seasons, cache_path=a.cache)
    p = embed(a.html, data)
    print("Updated %s (%d KB). Seasons: %s." % (p, p.stat().st_size // 1024, ", ".join(data["order"])))


if __name__ == "__main__":
    main()
