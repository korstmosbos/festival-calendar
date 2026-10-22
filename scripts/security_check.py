#!/usr/bin/env python3
"""Repository security gate. Runs on every pull request and before every deploy.

Fails (exit 1) if anything in the repository could hide or smuggle in malicious content:

  * files outside the allowlisted paths/types (no new scripts, binaries, symlinks, executables)
  * invalid UTF-8, byte-order marks, NUL bytes, CR line endings, oversized files
  * invisible or deceptive Unicode anywhere: bidi overrides ("Trojan Source"), zero-width
    characters, tag characters, variation selectors, private-use, unassigned code points,
    exotic whitespace, control characters
  * any non-ASCII character in code or config (.py .js .css .html .yml), so nothing in code
    can look like one thing and be another
  * non-Latin letters in data (blocks Cyrillic/Greek look-alikes where a Cyrillic letter imitates a Latin one)
  * workflows that are not locked down: unpinned actions, risky triggers, untrusted
    event text in shell steps, missing least-privilege permissions
  * build scripts that import network/process/serialization modules or call eval/exec
  * web app code that uses eval-like APIs or talks to hosts outside an allowlist

Standard library only. Run it with:  python3 -I scripts/security_check.py
"""
import ast, re, stat, sys, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- file allowlist
ALLOWED = [
    r"README\.md", r"SECURITY\.md", r"\.gitignore",
    r"\.github/CODEOWNERS", r"\.github/dependabot\.yml",
    r"\.github/workflows/(deploy|check)\.yml",
    r"\.github/ISSUE_TEMPLATE/(festival\.yml|config\.yml)",
    r"\.github/pull_request_template\.md",
    r"data/editions\.csv",
    r"data/festivals/(north-america|europe|asia-pacific|latin-america|africa)\.csv",
    r"scripts/(build|security_check)\.py",
    r"src/app\.html",
]
ALLOWED_RE = re.compile("^(?:" + "|".join(ALLOWED) + ")$")
IGNORED_DIRS = {".git", "site", "__pycache__"}
MAX_BYTES = 400_000
CODE_SUFFIXES = {".py", ".js", ".css", ".html", ".yml", ".yaml"}

# ---------------------------------------------------------------- unicode policy
# Characters that render invisibly or reorder text. Listed explicitly (as escapes, so this
# file stays pure ASCII) in addition to the category checks below, for clarity in reviews.
EXPLICIT_BAD = {
    0x00AD,                     # soft hyphen
    0x034F,                     # combining grapheme joiner
    0x061C,                     # arabic letter mark
    0x115F, 0x1160, 0x3164, 0xFFA0,  # hangul fillers (render blank)
    0x180E,                     # mongolian vowel separator
    *range(0x200B, 0x2010),     # zero-width space/joiners, LRM, RLM
    *range(0x202A, 0x202F),     # bidi embeddings and overrides (LRE RLE PDF LRO RLO)
    *range(0x2060, 0x2070),     # word joiner, invisible operators, bidi isolates (LRI RLI FSI PDI)
    0xFEFF,                     # zero-width no-break space / BOM
    *range(0xFFF9, 0xFFFC),     # interlinear annotation
}
# Punctuation and symbols allowed outside code (prose and data). Everything else non-ASCII
# must be a Latin-script letter in precomposed (NFC) form.
PROSE_SYMBOLS = {
    0x00A9, 0x00B0, 0x00B7, 0x00D7,          # (c) degree middle-dot multiplication
    0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026,  # dashes, quotes, ellipsis
    0x2190, 0x2192,                          # arrows
}


def char_problem(ch, code_file):
    cp = ord(ch)
    if ch in "\n\t":
        return None
    if cp < 0x20 or cp == 0x7F:
        return "control character"
    if cp < 0x80:
        return None
    if code_file:
        return "non-ASCII character in code (write it as an escape such as \\u2014 or &mdash;)"
    cat = unicodedata.category(ch)
    if cp in EXPLICIT_BAD or cat == "Cf":
        return "invisible/formatting character"
    if 0xE0000 <= cp <= 0xE007F:
        return "tag character"
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return "variation selector"
    if cat in ("Co", "Cn", "Cs", "Cc"):
        return "private-use, unassigned or surrogate code point"
    if cat in ("Zs", "Zl", "Zp"):
        return "non-standard whitespace"
    if cat.startswith("M"):
        return "combining mark (use the precomposed letter)"
    if cp in PROSE_SYMBOLS:
        return None
    if cat.startswith("L"):
        name = unicodedata.name(ch, "")
        if name.startswith("LATIN ") and unicodedata.normalize("NFC", ch) == ch:
            return None
        return "non-Latin letter (possible look-alike)"
    return "unexpected symbol"


# ---------------------------------------------------------------- per-type rules
PY_BANNED_MODULES = {
    "subprocess", "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "telnetlib", "requests",
    "pickle", "marshal", "shelve", "ctypes", "importlib", "multiprocessing", "asyncio", "base64",
    "codecs", "zipimport", "pty", "webbrowser", "xmlrpc", "builtins", "runpy", "code", "shutil",
}
PY_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "globals",
                   "locals", "vars", "open", "breakpoint", "input"}
PY_BANNED_ATTRS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "fork", "remove",
                   "unlink", "rmdir", "removedirs", "rename", "chmod", "chown", "kill", "putenv"}

JS_BANNED = [
    (r"\beval\s*\(", "eval()"),
    (r"\bnew\s+Function\b|\bFunction\s*\(", "Function constructor"),
    (r"document\.write", "document.write"),
    (r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]", "string passed to setTimeout/setInterval"),
    (r"\bimport\s*\(", "dynamic import"),
    (r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|postMessage", "network/messaging API"),
    (r"\bsrcdoc\b|<iframe|<object|<embed", "embedded frame/object"),
    (r"\bon[a-z]+\s*=\s*[\"']", "inline event handler attribute"),
    (r"javascript\s*:", "javascript: URL"),
    (r"\bstyle\s*=\s*[\"']", "inline style attribute (blocked by the site's security policy)"),
    (r"insertAdjacentHTML|outerHTML|createContextualFragment", "raw HTML insertion API"),
]
JS_ALLOWED_HOSTS = {"fonts.googleapis.com", "fonts.gstatic.com", "github.com",
                    "calendar.google.com", "www.google.com"}
FETCH_RE = re.compile(r"\bfetch\s*\(\s*(['\"`])([^'\"`]*)\1")

WF_ACTION_PIN = re.compile(r"^\s*-?\s*uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})\s*(#.*)?$")
WF_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
WF_BAD_TRIGGERS = re.compile(r"^\s*(pull_request_target|workflow_run|issue_comment|issues|discussion\w*|repository_dispatch)\s*:", re.M)
WF_UNTRUSTED = re.compile(
    r"\$\{\{\s*github\.(event\.(issue|comment|review|pull_request|discussion|head_commit|commits|pages)|head_ref)"
)
WF_ALLOWED_ACTIONS = {"actions/checkout", "actions/configure-pages",
                      "actions/upload-pages-artifact", "actions/deploy-pages"}


def check_python(path, text, errs):
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        errs.append(f"{path}: does not parse ({e})"); return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] in PY_BANNED_MODULES:
                    errs.append(f"{path}:{node.lineno}: imports '{n}', which is not allowed in build scripts")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in PY_BANNED_CALLS:
                errs.append(f"{path}:{node.lineno}: calls {f.id}(), which is not allowed")
            if isinstance(f, ast.Attribute) and f.attr in PY_BANNED_ATTRS:
                errs.append(f"{path}:{node.lineno}: calls .{f.attr}(), which is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in ("__name__", "__file__"):
            errs.append(f"{path}:{node.lineno}: uses dunder attribute {node.attr}")


def check_web(path, text, errs):
    for pat, label in JS_BANNED:
        for m in re.finditer(pat, text, re.I):
            ln = text.count("\n", 0, m.start()) + 1
            errs.append(f"{path}:{ln}: uses {label}")
    for m in re.finditer(r"https?://([A-Za-z0-9.-]+)", text):
        host = m.group(1).lower()
        if host not in JS_ALLOWED_HOSTS:
            ln = text.count("\n", 0, m.start()) + 1
            errs.append(f"{path}:{ln}: references unapproved host {host}")
        if m.group(0).startswith("http://"):
            ln = text.count("\n", 0, m.start()) + 1
            errs.append(f"{path}:{ln}: insecure http:// link")
    for m in FETCH_RE.finditer(text):
        if not re.fullmatch(r"data/[a-z0-9_-]+\.json", m.group(2)):
            ln = text.count("\n", 0, m.start()) + 1
            errs.append(f"{path}:{ln}: fetch() may only load the site's own data/*.json")
    if re.search(r"\bfetch\s*\(\s*[^'\"`\s]", text):
        errs.append(f"{path}: fetch() must use a fixed string address")
    scripts = re.findall(r"<script\b[^>]*>", text, re.I)
    for s in scripts:
        m = re.search(r"\bsrc\s*=\s*\"([^\"]+)\"", s)
        if m and not m.group(1).startswith("app.") :
            errs.append(f"{path}: loads external script {m.group(1)}")


def check_workflow(path, text, errs):
    if WF_BAD_TRIGGERS.search(text):
        errs.append(f"{path}: uses a trigger that runs with untrusted input and elevated rights "
                    "(pull_request_target / workflow_run / issues / issue_comment / ...)")
    if WF_UNTRUSTED.search(text):
        errs.append(f"{path}: interpolates untrusted event text (issue/PR titles, branch names...) into the workflow")
    if not re.search(r"^permissions:\s*\{\s*\}\s*$|^permissions:\s*\n(\s+\w[\w-]*:\s*(read|none)\s*\n)+", text, re.M):
        errs.append(f"{path}: top-level permissions must be read-only or empty")
    if re.search(r"\bsecrets\.\w|\bsecrets\s*\[", text):
        errs.append(f"{path}: references repository secrets; this project needs none")
    if "self-hosted" in text:
        errs.append(f"{path}: self-hosted runners are not allowed")
    for i, line in enumerate(text.splitlines(), 1):
        m = WF_USES.match(line)
        if not m:
            continue
        pm = WF_ACTION_PIN.match(line)
        if not pm:
            errs.append(f"{path}:{i}: action '{m.group(1)}' must be pinned to a full 40-character commit hash")
        elif pm.group(1) not in WF_ALLOWED_ACTIONS:
            errs.append(f"{path}:{i}: action '{pm.group(1)}' is not on the approved list")
    for m in re.finditer(r"actions/checkout@[0-9a-f]{40}[^\n]*\n((?:\s+.*\n?)*)", text):
        if "persist-credentials: false" not in m.group(1):
            errs.append(f"{path}: actions/checkout must set persist-credentials: false")


def scan(root=ROOT):
    errs = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        rels = rel.as_posix()
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            errs.append(f"{rels}: symbolic links are not allowed"); continue
        if p.is_dir():
            continue
        if not ALLOWED_RE.match(rels):
            errs.append(f"{rels}: file is not on the allowlist (scripts/security_check.py ALLOWED)"); continue
        if st.st_mode & 0o111 and p.suffix != ".py":
            errs.append(f"{rels}: executable bit set")
        if st.st_size > MAX_BYTES:
            errs.append(f"{rels}: larger than {MAX_BYTES} bytes"); continue
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            errs.append(f"{rels}: starts with a byte-order mark")
        if b"\x00" in raw:
            errs.append(f"{rels}: contains NUL bytes"); continue
        if b"\r" in raw:
            errs.append(f"{rels}: contains carriage returns (use LF line endings)")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            errs.append(f"{rels}: not valid UTF-8 ({e})"); continue
        if unicodedata.normalize("NFC", text) != text:
            errs.append(f"{rels}: text is not NFC-normalized (decomposed accents can hide look-alikes)")
        code = p.suffix in CODE_SUFFIXES or rels == ".github/CODEOWNERS"
        reported = 0
        for ln, line in enumerate(text.split("\n"), 1):
            for col, ch in enumerate(line, 1):
                why = char_problem(ch, code)
                if why:
                    errs.append(f"{rels}:{ln}:{col}: U+{ord(ch):04X} {unicodedata.name(ch, 'UNNAMED')} - {why}")
                    reported += 1
            if reported > 25:
                errs.append(f"{rels}: more problems not shown"); break
        if p.suffix == ".py":
            check_python(rels, text, errs)
        if rels.startswith("src/"):
            check_web(rels, text, errs)
        if rels.startswith(".github/workflows/"):
            check_workflow(rels, text, errs)
    return errs


def main():
    errs = scan()
    if errs:
        print("SECURITY CHECK FAILED\n  " + "\n  ".join(errs), file=sys.stderr)
        sys.exit(1)
    print("Security check passed.")


if __name__ == "__main__":
    main()
