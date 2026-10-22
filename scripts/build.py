#!/usr/bin/env python3
"""Build the festival calendar: validate data, expand dates, write JSON + iCalendar feeds.

Inputs
  data/festivals/<region>.csv   one row per festival (the region comes from the file name)
  data/editions.csv             announced dates that override the projected pattern

Outputs (into site/)
  data/festivals.json           everything the web app needs
  feeds/*.ics                   subscribable calendars (all, per region, country, genre, size, region+genre)

Standard library only, so it runs anywhere (including GitHub Actions) with plain `python3`.
"""
import csv, datetime as dt, json, os, re, sys, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = Path(os.environ.get("OUT_DIR", ROOT / "site"))
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
REPO = os.environ.get("REPO", "")  # owner/name, used for "suggest a correction" links

REGIONS = {
    "north-america": "North America",
    "europe": "Europe",
    "asia-pacific": "Asia-Pacific",
    "latin-america": "Latin America",
    "africa": "Africa",
}
GENRES = ["Pop", "Rock", "Indie", "Hip-Hop", "R&B/Soul", "Electronic", "Metal", "Punk", "Jazz",
          "Blues", "Folk", "Country", "Latin", "Reggae", "Afro", "World", "Classical", "Arts"]
SIZES = [  # (slug, label, min attendance inclusive)
    ("mega", "Mega (500k+)", 500_000),
    ("major", "Major (150k–500k)", 150_000),
    ("large", "Large (50k–150k)", 50_000),
    ("mid", "Mid-size (10k–50k)", 10_000),
    ("boutique", "Boutique (under 10k)", 0),
]
DOW = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
PATTERN_RE = re.compile(r"^(?:(\d{1,2})/(\d|L)/(Mon|Tue|Wed|Thu|Fri|Sat|Sun)|F/(\d\d)-(\d\d))/(\d{1,3})(?:\+(\d{1,3}))?$")


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def size_of(n):
    for sl, label, lo in SIZES:
        if n >= lo:
            return sl, label


def nth_weekday(year, month, n, wd):
    if n == "L":
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        d = nxt - dt.timedelta(days=1)
        return d - dt.timedelta(days=(d.weekday() - wd) % 7)
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(wd - d.weekday()) % 7)
    d += dt.timedelta(weeks=int(n) - 1)
    return d if d.month == month else None


def project(pattern, year):
    """Return [(start, end_inclusive)] for a year from a recurrence pattern."""
    m = PATTERN_RE.match(pattern)
    month, n, wd, fm, fd, length, repeat = m.groups()
    if fm:
        start = dt.date(year, int(fm), int(fd))
    else:
        start = nth_weekday(year, int(month), n, DOW[wd])
        if start is None:
            return []
    length = int(length)
    out = [(start, start + dt.timedelta(days=length - 1))]
    if repeat:
        s2 = start + dt.timedelta(days=int(repeat))
        out.append((s2, s2 + dt.timedelta(days=length - 1)))
    return out


def runs_in(rule, year):
    if not rule:
        return True
    if rule == "odd":
        return year % 2 == 1
    if rule == "even":
        return year % 2 == 0
    m = re.match(r"every(\d+):(\d{4})$", rule)
    return bool(m) and (year - int(m.group(2))) % int(m.group(1)) == 0


def load():
    errors, fests, ids = [], [], set()
    for path in sorted((DATA / "festivals").glob("*.csv")):
        region_slug = path.stem
        if region_slug not in REGIONS:
            errors.append(f"{path.name}: file name must be one of {', '.join(REGIONS)}")
            continue
        for ln, r in enumerate(csv.DictReader(path.open(encoding="utf-8")), start=2):
            where = f"{path.name} line {ln}"
            r = {k: (v or "").strip() for k, v in r.items() if k}
            if not r.get("id"):
                errors.append(f"{where}: missing id"); continue
            if not re.fullmatch(r"[a-z0-9-]+", r["id"]):
                errors.append(f"{where}: id '{r['id']}' must be lowercase letters, digits and dashes")
            if r["id"] in ids:
                errors.append(f"{where}: duplicate id '{r['id']}'")
            ids.add(r["id"])
            for k in ("name", "country", "genres", "attendance", "pattern"):
                if not r.get(k):
                    errors.append(f"{where} ({r['id']}): missing {k}")
            genres = [g.strip() for g in r.get("genres", "").split(";") if g.strip()]
            bad = [g for g in genres if g not in GENRES]
            if bad:
                errors.append(f"{where} ({r['id']}): unknown genre(s) {bad}; allowed: {', '.join(GENRES)}")
            try:
                att = int(r.get("attendance") or 0)
            except ValueError:
                errors.append(f"{where} ({r['id']}): attendance must be a whole number"); att = 0
            if r.get("pattern") and not PATTERN_RE.match(r["pattern"]):
                errors.append(f"{where} ({r['id']}): bad pattern '{r['pattern']}' (e.g. 7/L/Fri/3, 4/2/Fri/3+7, F/06-21/1)")
            if r.get("years") and not re.fullmatch(r"odd|even|every\d+:\d{4}", r["years"]):
                errors.append(f"{where} ({r['id']}): years must be blank, odd, even or everyN:YYYY")
            fests.append(dict(r, region=REGIONS[region_slug], region_slug=region_slug,
                              genres=genres, attendance=att))
    editions = {}
    for ln, r in enumerate(csv.DictReader((DATA / "editions.csv").open(encoding="utf-8")), start=2):
        r = {k: (v or "").strip() for k, v in r.items() if k}
        where = f"editions.csv line {ln}"
        if r["id"] not in ids:
            errors.append(f"{where}: unknown festival id '{r['id']}'"); continue
        status = r.get("status") or "confirmed"
        try:
            if status in ("skipped", "cancelled"):
                year = int(r["start"][:4]); s = e = None
            else:
                s = dt.date.fromisoformat(r["start"]); e = dt.date.fromisoformat(r.get("end") or r["start"])
                year = s.year
                if e < s:
                    errors.append(f"{where}: end before start")
        except ValueError:
            errors.append(f"{where}: dates must be YYYY-MM-DD"); continue
        editions.setdefault((r["id"], year), []).append(
            dict(start=s, end=e, status=status, source=r.get("source", "")))
    return fests, editions, errors


def expand(fests, editions, years, today):
    events = []
    for f in fests:
        for y in years:
            eds = editions.get((f["id"], y))
            if eds:
                runs = [(e["start"], e["end"], e["status"], e["source"]) for e in eds if e["start"]]
                if not runs and any(e["status"] == "cancelled" for e in eds):
                    pass  # cancelled with no dates: nothing on the calendar
            elif runs_in(f.get("years", ""), y) and f.get("pattern"):
                runs = [(s, e, "projected", "") for s, e in project(f["pattern"], y)]
            else:
                runs = []
            for i, (s, e, status, source) in enumerate(sorted(runs)):
                if status == "projected" and e < today:
                    continue  # a guessed date for an edition that has already happened is just noise
                events.append(dict(fid=f["id"], year=y, n=i + 1, start=s, end=e,
                                   status=status, source=source,
                                   part=(f"Weekend {i + 1}" if len(runs) > 1 else "")))
    return events


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        c = ch.encode("utf-8")
        if len(cur) + len(c) > (75 if not out else 74):
            out.append(cur.decode("utf-8")); cur = b""
        cur += c
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def vevent(ev, f, stamp):
    title = f["name"] + (f" — {ev['part']}" if ev["part"] else "")
    if ev["status"] == "projected":
        title += " (dates TBC)"
    elif ev["status"] == "cancelled":
        title += " (CANCELLED)"
    place = ", ".join(x for x in (f.get("city"), f["country"]) if x)
    size_label = size_of(f["attendance"])[1]
    desc = [
        f"{place} · {f['region']}",
        f"Genres: {', '.join(f['genres'])}",
        f"Typical attendance: ~{f['attendance']:,} ({size_label})",
        ("Dates CONFIRMED" + (f" — source: {ev['source']}" if ev["source"].startswith("http") else ""))
        if ev["status"] == "confirmed" else
        "Dates PROJECTED from the festival's usual timing — not yet announced. This event will update automatically when dates are confirmed.",
    ]
    if f.get("website"):
        desc.append(f"Website: {f['website']}")
    if SITE_URL:
        desc.append(f"Festival calendar: {SITE_URL}/#{f['id']}")
    # The UID is stable per festival/year/weekend, so date or detail changes update the existing
    # event in subscribers' calendars instead of creating duplicates.
    uid = f"{f['id']}-{ev['year']}-{ev['n']}@festival-calendar"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{ev['start']:%Y%m%d}",
        f"DTEND;VALUE=DATE:{ev['end'] + dt.timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{ics_escape(title)}",
        f"LOCATION:{ics_escape(place)}",
        f"DESCRIPTION:{ics_escape(chr(10).join(desc))}",
        f"CATEGORIES:{ics_escape(','.join(f['genres']))}",
        "TRANSP:TRANSPARENT",
        f"STATUS:{'CANCELLED' if ev['status'] == 'cancelled' else 'CONFIRMED' if ev['status'] == 'confirmed' else 'TENTATIVE'}",
    ]
    if f.get("website"):
        lines.append(f"URL:{f['website']}")
    lines.append("END:VEVENT")
    return lines


def write_ics(path, name, items, byid, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Festival Calendar//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-CALDESC:Major music and art festivals. Updates automatically.",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ]
    for ev in items:
        lines += vevent(ev, byid[ev["fid"]], stamp)
    lines.append("END:VCALENDAR")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(fold(l) for l in lines) + "\r\n", encoding="utf-8")


def main():
    fests, editions, errors = load()
    if errors:
        print("Data problems found — fix these and try again:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)
    today = dt.date.today()
    years = [today.year, today.year + 1]
    events = expand(fests, editions, years, today)
    byid = {f["id"]: f for f in fests}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    feeds = []  # (path, name, filter fn, kind, key)
    def add(kind, key, label, pred):
        feeds.append((f"feeds/{kind + '/' if kind != 'all' else ''}{key}.ics", label, pred, kind, key))
    add("all", "all", "Festivals — All", lambda f: True)
    for rs, rn in REGIONS.items():
        add("region", rs, f"Festivals — {rn}", lambda f, rn=rn: f["region"] == rn)
        for g in GENRES:
            if any(f["region"] == rn and g in f["genres"] for f in fests):
                add("region-genre", f"{rs}--{slug(g)}", f"Festivals — {rn} · {g}",
                    lambda f, rn=rn, g=g: f["region"] == rn and g in f["genres"])
    for c in sorted({f["country"] for f in fests}):
        add("country", slug(c), f"Festivals — {c}", lambda f, c=c: f["country"] == c)
    for g in GENRES:
        add("genre", slug(g), f"Festivals — {g}", lambda f, g=g: g in f["genres"])
    for sl, label, _ in SIZES:
        add("size", sl, f"Festivals — {label}", lambda f, sl=sl: size_of(f["attendance"])[0] == sl)

    feed_index = {}
    for path, name, pred, kind, key in feeds:
        items = [e for e in events if pred(byid[e["fid"]])]
        write_ics(OUT / path, name, items, byid, stamp)
        feed_index.setdefault(kind, {})[key] = {"path": path, "name": name, "count": len(items)}

    out = {
        "generated": stamp,
        "siteUrl": SITE_URL,
        "repo": REPO,
        "regions": list(REGIONS.values()),
        "genres": GENRES,
        "sizes": [{"slug": s, "label": l, "min": m} for s, l, m in SIZES],
        "feeds": feed_index,
        "festivals": [
            {k: f.get(k, "") for k in ("id", "name", "city", "country", "region", "genres", "attendance", "website")}
            | {"size": size_of(f["attendance"])[0], "countrySlug": slug(f["country"]), "regionSlug": f["region_slug"]}
            for f in fests
        ],
        "events": [
            {"fid": e["fid"], "start": str(e["start"]), "end": str(e["end"]), "status": e["status"],
             "part": e["part"], "source": e["source"]}
            for e in sorted(events, key=lambda e: (e["start"], e["fid"]))
        ],
    }
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    (OUT / "data" / "festivals.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    app = (ROOT / "src" / "app.html").read_text(encoding="utf-8")
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n'
        '</head>\n<body>\n' + app + '\n</body>\n</html>\n', encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    conf = sum(e["status"] == "confirmed" for e in events)
    print(f"{len(fests)} festivals · {len(events)} events ({conf} confirmed) · {len(feeds)} feeds → {OUT}")


if __name__ == "__main__":
    main()
