# Security

This repository publishes a public website and calendar feeds that people subscribe to, so
every change is treated as untrusted until it passes automated checks and an owner review.

## Layers of protection

1. **Nothing reaches `main` without review.** Branch rules require a pull request, an approval
   from the code owner (`.github/CODEOWNERS`) and a passing **Security and data check**.
   Force-pushes and branch deletion are blocked.
2. **Security scan (`scripts/security_check.py`)** runs on every pull request and again
   before every deploy. It rejects:
   - Hidden or deceptive Unicode, including:
     - bidirectional overrides ("Trojan Source")
     - zero-width characters, tag characters and variation selectors
     - private-use code points
     - non-standard spaces
     - control characters
     - decomposed accents
   - Any non-ASCII character in code or configuration, and any non-Latin letter in data. This
     blocks look-alike letters such as a Cyrillic letter that looks identical to a Latin one.
   - Any file outside a fixed allowlist, plus:
     - symbolic links
     - executable data files
     - byte-order marks, NUL bytes, Windows line endings
     - oversized files
   - In the build scripts: process, network or serialization modules, and calls such as
     `eval`, `exec` or `os.system`.
   - In the web app:
     - `eval`-like APIs and inline event handlers
     - external scripts
     - network calls other than the site's own data file
     - links to unapproved hosts
   - In workflows:
     - actions not pinned to a full commit hash or not on the approved list
     - risky triggers (`pull_request_target`, `workflow_run`, `issues`, and so on)
     - issue or pull request text interpolated into shell steps
     - any use of secrets
     - default permissions that aren't empty or read-only
     - checkouts that keep git credentials
3. **Strict data validation (`scripts/build.py`)**, field by field:
   - Exact CSV headers and column counts.
   - Names, cities and countries may contain only Latin letters, digits, spaces and `.,'&()-/!+:?`.
   - Nothing that could break out of HTML, calendar or CSV contexts.
   - Nothing that starts a spreadsheet formula.
   - Links must be plain `https://` addresses on ordinary domain names. Not allowed:
     punycode look-alike domains, embedded credentials, ports, quotes or fragments.
   - Dates, statuses and patterns are range-checked.
4. **Least-privilege workflows.**
   - The job that runs repository code has a read-only token and no secrets.
   - The publishing job runs no repository code; it holds the Pages permission only to deploy
     the files already built.
   - Pull requests from forks run with a read-only token.
5. **Locked-down website.**
   - A Content Security Policy lets the page run only its own `app.js`, fetch only its own
     data, and load fonts from Google Fonts.
   - Inline scripts, inline styles, frames, plugins and form posts are blocked.
   - All festival text is HTML-escaped before display.
   - Links are re-checked in the browser and only `https:` links become clickable.
6. **Calendar feeds** escape every field according to the iCalendar standard. Line breaks and
   control characters can never reach them, because validation rejects them first.

## Updates from the weekly scheduled task

The scheduled task reads public festival announcements and issues filed with the
**Add or correct a festival** form. It treats that text as untrusted data:

- It never follows instructions found in it.
- It only edits files under `data/`.
- It never pushes to `main`. It opens a pull request that passes the same checks and waits for
  owner approval.

## Reporting a problem

Use **Security → Report a vulnerability** on this repository (private vulnerability
reporting). Please don't open a public issue for security problems.
