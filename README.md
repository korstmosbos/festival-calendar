# Festival Circuit

A calendar of about 530 major music and art festivals across North America, Europe, Asia-Pacific, Latin America and Africa. You can filter it by region, country, genre and size, and subscribe to it from Google Calendar, Zoho Calendar, Apple Calendar or Outlook.

**Site:** `https://<owner>.github.io/<repo>/`
**Subscribe to everything:** `https://<owner>.github.io/<repo>/feeds/all.ics`

## How the dates work

Each festival has a **recurrence pattern** that describes its usual timing, such as "last Friday of July, 3 days". Until a festival announces its dates, the calendar shows a **projected** date, marked *Dates TBC*.

When dates are announced, add them to `data/editions.csv`. They replace the projection and the event changes to **Confirmed**.

Each event in the feeds keeps a stable ID based on the festival, the year and the weekend number. When dates or details change, the existing event updates in subscribers' calendars and no duplicate is created. Google re-reads subscribed feeds about every 12–24 hours, and Zoho on its own schedule.

## Updating the calendar

All changes go through a **pull request**. Before anything can be merged, it must pass the
**Security and data check** and be approved by the repository owner. After merging, the site
and feeds republish in about two minutes. If a check fails, the message names the file, line
and problem. See [SECURITY.md](SECURITY.md) for everything that is checked.

**Easiest:** open an issue with the **Add or correct a festival** form. There is also a link to
it in the site header. The weekly scheduled task works through these requests, checks them
against official sources and opens a pull request for review.

**Direct edit:** open a file on GitHub, click the pencil icon, choose **Create a new branch and
start a pull request**, and submit.

| To… | Edit | Example row |
|---|---|---|
| Confirm dates | `data/editions.csv` | `coachella,2027-04-09,2027-04-11,confirmed,https://…` |
| Mark a year as not happening | `data/editions.csv` | `glastonbury,2026,,skipped,Fallow year` |
| Cancel an edition | `data/editions.csv` | `some-fest,2027,,cancelled,https://…` |
| Add or fix a festival | `data/festivals/<region>.csv` | see below |

For festivals with two weekends, add one row per weekend in `editions.csv`.

### Festival columns (`data/festivals/<region>.csv`)

The region comes from the file name: `north-america`, `europe`, `asia-pacific`, `latin-america` or `africa`.

- `id`: lowercase letters, digits and dashes. Never change it, because subscribers' events are keyed on it.
- `name`, `city`, `country`
- `genres`: separated by `;`, chosen from Pop, Rock, Indie, Hip-Hop, R&B/Soul, Electronic, Metal, Punk, Jazz, Blues, Folk, Country, Latin, Reggae, Afro, World, Classical, Arts
- `attendance`: typical total attendance across all days. This sets the size band:
  - Boutique: under 10k
  - Mid-size: 10k–50k
  - Large: 50k–150k
  - Major: 150k–500k
  - Mega: 500k+
- `pattern`: the usual timing
  - `7/L/Fri/3`: July, last Friday, 3 days
  - `4/2/Fri/3+7`: April, 2nd Friday, 3 days, repeated 7 days later (a second weekend)
  - `F/06-21/1`: a fixed date, June 21, 1 day
- `years`: leave blank for every year. Otherwise `odd`, `even`, or `every5:2022` (every 5 years counting from 2022).
- `website`

## Feeds

All feeds are under `/feeds/`:

- `all.ics`: every festival
- `region/<region>.ics`
- `country/<country>.ics`
- `genre/<genre>.ics`
- `size/<size>.ics`
- `region-genre/<region>--<genre>.ics`: one region and one genre combined

The **Subscribe** button on the site picks the feed that matches your current filters.

## Running locally

```
python3 -I scripts/security_check.py   # the same scan that runs on every pull request
python3 -I scripts/build.py            # validates the data and writes site/
cd site && python3 -m http.server
```

## About the data

The festival list was compiled in October 2026. Attendance figures are approximate typical totals. Dates for 2027 not yet announced are projections and are labelled that way everywhere.
