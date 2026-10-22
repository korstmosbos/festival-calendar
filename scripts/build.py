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
    ("major", "Major (150k\u2013500k)", 150_000),
    ("large", "Large (50k\u2013150k)", 50_000),
    ("mid", "Mid-size (10k\u201350k)", 10_000),
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


# ---------------------------------------------------------------- strict field validation
FEST_COLUMNS = ["id", "name", "city", "country", "genres", "attendance", "pattern", "years", "website"]
EDITION_COLUMNS = ["id", "start", "end", "status", "source"]
MAX_ROWS = 3000
ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Free text: Latin letters (checked per character below), digits, spaces and a few punctuation marks.
# No < > " ` { } [ ] \\ | ^ ~ = ; $ % or control characters, so nothing can break out of HTML,
# iCalendar or CSV contexts, and nothing can start a spreadsheet formula.
TEXT_PUNCT = set(" .,'&()-/!+:?")
HOST_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?(?:\?[A-Za-z0-9._~%&=+-]*)?")
STATUSES = {"confirmed", "skipped", "cancelled"}


def text_ok(value, maxlen=100):
    """Return an error string, or None if the value is safe display text."""
    if len(value) > maxlen:
        return f"longer than {maxlen} characters"
    if unicodedata.normalize("NFC", value) != value:
        return "uses decomposed accents; retype the accented letters"
    if value[:1] in "=+-@":
        return "may not start with = + - or @"
    if "  " in value:
        return "contains double spaces"
    for ch in value:
        if ch.isascii():
            if not (ch.isalnum() or ch in TEXT_PUNCT):
                return f"contains the character {ch!r}, which is not allowed"
        elif not (unicodedata.category(ch).startswith("L")
                  and unicodedata.name(ch, "").startswith("LATIN ")):
            return f"contains U+{ord(ch):04X} {unicodedata.name(ch, 'UNNAMED')}, which is not allowed"
    return None


def url_ok(value):
    """Only plain https links to ordinary domain names."""
    m = re.fullmatch(r"https://([^/?#]+)(.*)", value)
    if not m or len(value) > 200:
        return "must be an https:// address (at most 200 characters)"
    host, rest = m.group(1), m.group(2)
    if host != host.lower() or not HOST_RE.fullmatch(host):
        return "has an invalid or unusual domain name (lowercase letters, digits, dashes and dots only)"
    if any(label.startswith("xn--") for label in host.split(".")):
        return "uses an internationalized (punycode) domain, which can imitate other sites"
    if "#" in rest or not PATH_RE.fullmatch(rest):
        return "contains characters that are not allowed in links"
    return None


def read_csv(path, columns, errors):
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            errors.append(f"{path.name}: file is empty"); return []
        if header != columns:
            errors.append(f"{path.name}: header must be exactly {','.join(columns)}"); return []
        rows = []
        for ln, cells in enumerate(reader, start=2):
            if len(cells) != len(columns):
                errors.append(f"{path.name} line {ln}: expected {len(columns)} columns, found {len(cells)}")
                continue
            if any(c != c.strip() for c in cells):
                errors.append(f"{path.name} line {ln}: remove spaces at the start or end of a cell")
            rows.append((ln, dict(zip(columns, (c.strip() for c in cells)))))
            if len(rows) > MAX_ROWS:
                errors.append(f"{path.name}: more than {MAX_ROWS} rows"); break
        return rows


def load():
    errors, fests, ids = [], [], set()
    files = sorted((DATA / "festivals").glob("*"))
    for path in files:
        region_slug = path.stem
        if path.suffix != ".csv" or region_slug not in REGIONS:
            errors.append(f"{path.name}: only {', '.join(s + '.csv' for s in REGIONS)} may be in data/festivals")
            continue
        for ln, r in read_csv(path, FEST_COLUMNS, errors):
            where = f"{path.name} line {ln}"
            if not ID_RE.fullmatch(r["id"]) or len(r["id"]) > 60:
                errors.append(f"{where}: id '{r['id']}' must be lowercase letters and digits joined by single dashes"); continue
            where += f" ({r['id']})"
            if r["id"] in ids:
                errors.append(f"{where}: duplicate id")
            ids.add(r["id"])
            for k in ("name", "country", "genres", "attendance", "pattern"):
                if not r[k]:
                    errors.append(f"{where}: missing {k}")
            for k in ("name", "city", "country"):
                problem = r[k] and text_ok(r[k])
                if problem:
                    errors.append(f"{where}: {k} {problem}")
            genres = [g.strip() for g in r["genres"].split(";") if g.strip()]
            bad = [g for g in genres if g not in GENRES]
            if bad:
                errors.append(f"{where}: unknown genre(s) {bad}; allowed: {', '.join(GENRES)}")
            if len(set(genres)) != len(genres):
                errors.append(f"{where}: genre listed twice")
            if not re.fullmatch(r"\d{1,8}", r["attendance"] or "0"):
                errors.append(f"{where}: attendance must be a whole number with no commas"); att = 0
            else:
                att = int(r["attendance"] or 0)
            if r["pattern"]:
                m = PATTERN_RE.fullmatch(r["pattern"])
                if not m:
                    errors.append(f"{where}: bad pattern '{r['pattern']}' (e.g. 7/L/Fri/3, 4/2/Fri/3+7, F/06-21/1)")
                else:
                    month, n, _, fm, fd, length, repeat = m.groups()
                    try:
                        if fm:
                            dt.date(2028, int(fm), int(fd))
                        elif not (1 <= int(month) <= 12 and (n == "L" or 1 <= int(n) <= 5)):
                            raise ValueError
                        if not (1 <= int(length) <= 366) or (repeat and not 1 <= int(repeat) <= 60):
                            raise ValueError
                    except ValueError:
                        errors.append(f"{where}: pattern '{r['pattern']}' has an impossible month, day or length")
            if r["years"] and not re.fullmatch(r"odd|even|every([2-9]|10):20\d\d", r["years"]):
                errors.append(f"{where}: years must be blank, odd, even or everyN:YYYY")
            if r["website"]:
                problem = url_ok(r["website"])
                if problem:
                    errors.append(f"{where}: website {problem}")
            fests.append(dict(r, region=REGIONS[region_slug], region_slug=region_slug,
                              genres=genres, attendance=att))
    editions = {}
    this_year = dt.date.today().year
    for ln, r in read_csv(DATA / "editions.csv", EDITION_COLUMNS, errors):
        where = f"editions.csv line {ln}"
        if r["id"] not in ids:
            errors.append(f"{where}: unknown festival id '{r['id']}'"); continue
        status = r["status"] or "confirmed"
        if status not in STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(sorted(STATUSES))}"); continue
        src = r["source"]
        if src:
            problem = url_ok(src) if src.startswith("http") else text_ok(src, 80)
            if problem:
                errors.append(f"{where}: source {problem}")
        try:
            if status in ("skipped", "cancelled"):
                if not re.fullmatch(r"20\d\d", r["start"]) or r["end"]:
                    raise ValueError
                year = int(r["start"]); s = e = None
            else:
                if not re.fullmatch(r"\d{4}-\d\d-\d\d", r["start"]) or (r["end"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", r["end"])):
                    raise ValueError
                s = dt.date.fromisoformat(r["start"]); e = dt.date.fromisoformat(r["end"] or r["start"])
                year = s.year
                if e < s or (e - s).days > 366:
                    errors.append(f"{where}: end must be on or after start and within a year of it")
        except ValueError:
            errors.append(f"{where}: dates must be YYYY-MM-DD (or just YYYY for skipped/cancelled)"); continue
        if not this_year - 2 <= year <= this_year + 5:
            errors.append(f"{where}: year {year} is out of range")
        editions.setdefault((r["id"], year), []).append(
            dict(start=s, end=e, status=status, source=src))
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
    title = f["name"] + (f" \u2014 {ev['part']}" if ev["part"] else "")
    if ev["status"] == "projected":
        title += " (dates TBC)"
    elif ev["status"] == "cancelled":
        title += " (CANCELLED)"
    place = ", ".join(x for x in (f.get("city"), f["country"]) if x)
    size_label = size_of(f["attendance"])[1]
    desc = [
        f"{place} \u00b7 {f['region']}",
        f"Genres: {', '.join(f['genres'])}",
        f"Typical attendance: ~{f['attendance']:,} ({size_label})",
        ("Dates CONFIRMED" + (f" \u2014 source: {ev['source']}" if ev["source"].startswith("http") else ""))
        if ev["status"] == "confirmed" else
        "Dates PROJECTED from the festival's usual timing \u2014 not yet announced. This event will update automatically when dates are confirmed.",
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


# The published site may only run its own script file, styles and data, and uses the visitor's
# system fonts. Inline scripts, inline styles, other hosts, fonts, frames, plugins and form posts are blocked.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "font-src 'none'; connect-src 'self'; img-src 'self'; base-uri 'none'; "
       "form-action 'none'; object-src 'none'; manifest-src 'none'; upgrade-insecure-requests")


def write_page():
    """Split src/app.html into index.html + app.css + app.js so the strict CSP can apply."""
    src = (ROOT / "src" / "app.html").read_text(encoding="utf-8")
    styles = re.findall(r"<style>(.*?)</style>", src, re.S)
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    if len(styles) != 1 or len(scripts) != 1 or "<script " in src:
        sys.exit("src/app.html must contain exactly one <style> and one inline <script>")
    title = re.search(r"<title>[^<]*</title>", src).group(0)
    body = re.sub(r"<title>.*?</title>|<meta [^>]*>|<style>.*?</style>|<script>.*?</script>", "", src, flags=re.S)
    base_css = (":root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
                "body{margin:0}img{max-width:100%}[hidden]{display:none!important}\n")
    (OUT / "app.css").write_text(base_css + styles[0], encoding="utf-8")
    (OUT / "app.js").write_text('"use strict";\n' + scripts[0], encoding="utf-8")
    links = "\n".join(l for l in re.findall(r"<link [^>]*>", body))
    body = re.sub(r"<link [^>]*>\n?", "", body)
    page = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\">\n"
            "<meta name=\"referrer\" content=\"strict-origin-when-cross-origin\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            f"{title}\n{links}\n<link rel=\"stylesheet\" href=\"app.css\">\n"
            "<script src=\"app.js\" defer></script>\n</head>\n<body>\n" + body.strip() + "\n</body>\n</html>\n")
    (OUT / "index.html").write_text(page, encoding="utf-8")


def main():
    fests, editions, errors = load()
    if errors:
        print("Data problems found \u2014 fix these and try again:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)
    today = dt.date.today()
    years = [today.year, today.year + 1]
    events = expand(fests, editions, years, today)
    byid = {f["id"]: f for f in fests}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    feeds = []  # (path, name, filter fn, kind, key)
    def add(kind, key, label, pred):
        feeds.append((f"feeds/{kind + '/' if kind != 'all' else ''}{key}.ics", label, pred, kind, key))
    add("all", "all", "Festivals \u2014 All", lambda f: True)
    for rs, rn in REGIONS.items():
        add("region", rs, f"Festivals \u2014 {rn}", lambda f, rn=rn: f["region"] == rn)
        for g in GENRES:
            if any(f["region"] == rn and g in f["genres"] for f in fests):
                add("region-genre", f"{rs}--{slug(g)}", f"Festivals \u2014 {rn} \u00b7 {g}",
                    lambda f, rn=rn, g=g: f["region"] == rn and g in f["genres"])
    for c in sorted({f["country"] for f in fests}):
        add("country", slug(c), f"Festivals \u2014 {c}", lambda f, c=c: f["country"] == c)
    for g in GENRES:
        add("genre", slug(g), f"Festivals \u2014 {g}", lambda f, g=g: g in f["genres"])
    for sl, label, _ in SIZES:
        add("size", sl, f"Festivals \u2014 {label}", lambda f, sl=sl: size_of(f["attendance"])[0] == sl)

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
    write_page()
    (OUT / ".nojekyll").write_text("")
    conf = sum(e["status"] == "confirmed" for e in events)
    print(f"{len(fests)} festivals \u00b7 {len(events)} events ({conf} confirmed) \u00b7 {len(feeds)} feeds -> {OUT}")


if __name__ == "__main__":
    main()
