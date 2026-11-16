# Utah Jazz Dashboard

A single-page analytics dashboard for the Utah Jazz: team stats against the rest of the league, player stats, lineup combinations, on/off splits, shot charts, and four statistical models. Everything is in one static HTML file with the season data embedded. There is no backend, no build step, and no API key.

**Live:** https://maximhoeft94.github.io/jazz-dashboard/

![Dashboard overview](screenshots/overview.png)

| Shot chart | Lineups |
|---|---|
| ![Shot chart](screenshots/shot-chart.png) | ![Lineups](screenshots/lineups.png) |



## What it does

The current Jazz season, split into dashboards (tabs), under a header with the record, net rating, offense, defense, and pace:

- **Team Performance (Overview):** the record with home/road, last 10, and streak; the next 10 games with tip time (Mountain), opponent and their record, arena, and TV; and a point-differential bar for every game played
- **Player Performance:** pick a player and a game (or the whole season) to see a zone shot chart in 11 zones colored by FG%, plus that game's scoring line, shooting splits, and a zone-by-zone breakdown
- **Jazz vs. League:** 22 team stats with the league average, the Jazz's rank, and a strip plot of all 30 teams; Dean Oliver's four factors; and splits (home/road, vs. .500 teams, close games, by month)
- **League Table:** all 30 teams (traditional, advanced, four factors), sortable, with a league-average row
- **Players:** per game, per 36, totals, advanced, and shooting views, counting Jazz games only
- **Lineups:** 5-man units and 2-man pairs with a minutes filter, plus an offense-vs-defense scatter
- **On/Off:** Jazz net rating with each player on the floor minus with him off
- **Shot Chart:** every Jazz field goal attempt, as hot/cold hexagons against the league's FG% from the same zone, or as makes and misses
- **Models**, computed in the browser: expected wins (Pythagorean), shot quality vs. shot making, a ridge-regression lineup impact model, and a four-factor regression
- **Player vs. Player:** any Jazz player against any NBA player, in league percentiles

Each dashboard has its own link (for example `#players`), so you can share a specific tab.

## How it works

```
ESPN box scores + play-by-play --(fetch_jazz.py)--> JSON embedded in index.html --> vanilla JS + inline SVG
```

- `index.html` is the whole dashboard: HTML, CSS, JavaScript, and the season data. Every chart is hand-built inline SVG.
- `fetch_jazz.py` downloads every regular-season game's box score and play-by-play from ESPN's public API and computes everything else:
  - **Team ratings, pace, and four factors** from both teams' box scores, using Basketball-Reference's possession estimate
  - **League player stats and percentiles** from every box score (usage, true shooting, assist and rebound rates, PIE)
  - **Jazz lineups, on/off, and on-court ratings** by following which five Jazz players are on the floor through every substitution. Tracked minutes match the official box score within about a minute, and points match the final score exactly
  - **Shot charts and league zone averages** from every shot's x/y location, sorted into the NBA's shot-chart zones
- Each finished game is reduced to a small record in `games_cache.json` (kept between runs by the workflow), so a refresh only downloads games played since the last one. A finished season's output is saved in `jazz_cache.json`.

## Stack

- HTML, CSS, and vanilla JavaScript, with no frameworks or chart libraries
- Python 3.9+ standard library only, for the data pull
- GitHub Actions for the refresh (every 2 hours, committing only when a game has finished), and GitHub Pages for hosting

## Run it

View it: open `index.html` in a browser, or visit the live link.

Refresh the data: the repo refreshes itself. To refresh right away, open the **Actions** tab, pick **Refresh Jazz data**, and click **Run workflow**. To run it yourself:

```
python fetch_jazz.py                                # the current season
```

The first run of a season downloads every game played so far (a few minutes); later runs only download new games and take under a minute.

## Data

Box scores and play-by-play come from ESPN's public, undocumented API, which can change without notice. NBA.com's own stats API is not used because it refuses requests from GitHub's servers. Advanced numbers are computed here with standard formulas, so they can differ slightly from NBA.com's official figures. This project is not affiliated with or endorsed by the NBA, ESPN, or the Utah Jazz.

Built by Maxim Hoeft.
