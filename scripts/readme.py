#!/usr/bin/env python3
"""Render the profile README as images and refresh its recent contributions (PRs and issues).

    python scripts/readme.py                    # fetch my PRs, redraw, update README.md
    python scripts/readme.py --from prs.json    # same, from a saved search API response

The README is a column of images drawn at the same scale: the intro and stack, the
contributions heading (linked to all my PRs) and one image per PR (linked to the PR).
Each comes in a wide layout for desktop and a narrow one for phones, in light and dark,
so GitHub picks whichever fits the screen and theme. All text is drawn as vector
outlines from scripts/glyphs.json, so it looks the same in every browser, and each
image's alt text carries its content for screen readers.
Standard library only.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

USER = "Axyss"
ALL_PRS = (f"https://github.com/search?q=author%3A{USER}+is%3Apr+-user%3A{USER}"
           "&type=pullrequests&s=created&o=desc")
ROWS = 5
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"

# What sits above "00 / About me": "wordmark", "greeting", "prompt", "mark", "outline" or None.
HEADER = "outline"
NAME, SITE = "Alejandro", "axyss.dev"

INTRO = ("I love building open source tools and trying out new tech. These days I'm mostly into "
         "self-hosting and trying the shiny new frontier model of the month.")

# Edit the stack here; icon keys are defined in ICONS at the bottom of the file.
STACK = [
    ("Proficient", ["python", "java", "typescript", "vue"]),
    ("Familiar", ["csharp", "nextjs", "kotlin"]),
    ("Currently checking out", ["gemini", "t3code", "fable"]),
]

# GitHub's own text colours. Lines and tiles use the text colour at low opacity,
# so they also blend into the dimmed and high-contrast themes.
THEMES = {
    "light": dict(fg="#1f2328", muted="#59636e"),
    "dark": dict(fg="#f0f6fc", muted="#9198a1"),
}

# Two layouts: GitHub's profile column is ~840px on desktop, so 1 unit ≈ 1px there;
# phones get a narrower, taller layout so the text stays readable.
LAYOUTS = {"wide": 840, "narrow": 380}
NARROW_MAX = 600  # px; phones get the narrow layout
SLANT = 0.615  # dx/dy of the diagonal strokes in the logo (≈58°)

# The logo's five strokes, in its own units; LOGO_BOX is x, y, width, height of the whole mark.
LOGO = ["190,76 119,192 143,191 198,102 247,308 272,309 216,78", "336,76 308,76 254,155 261,185",
        "210,219 148,309 175,309 217,249", "118,231 94,232 47,309 71,308", "336,257 294,309 325,309"]
LOGO_BOX = (47, 76, 289, 233)


# ---------------------------------------------------------------------------- text

class Face:
    """One font weight from glyphs.json, drawn as <use> references to glyph outlines."""

    def __init__(self, key, tag):
        data = GLYPHS[key]
        self.tag, self.upm = tag, data["upm"]
        self.cap = data["cap"] / self.upm
        self.cmap, self.glyphs, self.kern = data["cmap"], data["glyphs"], data["kern"]
        self.ids = {name: f"{tag}{i:x}" for i, name in enumerate(self.glyphs)}

    def clean(self, s):
        """Keep what the face can draw: accents fall back to the bare letter, the rest is dropped."""
        out = []
        for ch in s:
            if ch in self.cmap:
                out.append(ch)
            elif ch.isspace():
                out.append(" ")
            else:
                out.extend(c for c in unicodedata.normalize("NFKD", ch) if c in self.cmap)
        return re.sub(r" {2,}", " ", "".join(out)).strip()

    def _kern(self, a, b):
        row = self.kern.get(_base(a))
        return row.get(_base(b), 0) if row else 0

    def advances(self, s, size, ls=0):
        """Yield (char, x offset in font units) and finally the total width in px."""
        k, pen, prev = size / self.upm, 0.0, None
        for ch in s:
            if prev is not None:
                pen += self._kern(prev, ch) + ls / k
            yield ch, pen
            pen += self.glyphs[self.cmap[ch]][0]
            prev = ch
        yield None, pen * k

    def width(self, s, size, ls=0):
        *_, (_, w) = self.advances(s, size, ls)
        return w

    def fit(self, s, size, max_w, ls=0):
        """Shorten s with an ellipsis until it fits max_w, preferring a word boundary."""
        s = self.clean(s)
        if self.width(s, size, ls) <= max_w:
            return s
        cut = s
        while cut and self.width(cut.rstrip() + "…", size, ls) > max_w:
            cut = cut[:-1]
        space = cut.rfind(" ")
        if space > len(cut) * 0.75:
            cut = cut[:space]
        return cut.rstrip(" -–—:;,.(/") + "…"

    def wrap(self, s, size, max_w):
        """Greedy line breaking on spaces."""
        lines, line = [], ""
        for word in self.clean(s).split(" "):
            test = f"{line} {word}" if line else word
            if line and self.width(test, size) > max_w:
                lines.append(line)
                line = word
            else:
                line = test
        return lines + [line] if line else lines


def _base(ch):
    return unicodedata.normalize("NFD", ch)[0]


class Doc:
    """An SVG under construction: collects the glyph outlines used and the body markup."""

    def __init__(self, width, delay=0.0):
        self.w, self.defs, self.body, self.delay = width, {}, [], delay

    def _glyph(self, face, name, x, y=0):
        adv, shape = face.glyphs[name]
        if isinstance(shape, list):  # composite: base letter + accent
            return "".join(self._glyph(face, n, x + dx, y + dy) for n, dx, dy in shape)
        if not shape:
            return ""
        gid = face.ids[name]
        self.defs.setdefault(gid, f'<path id="{gid}" d="{shape}"/>')
        return f'<use href="#{gid}"{_attr("x", x)}{_attr("y", y)}/>'

    def text(self, face, s, size, x, y, cls, anchor="start", ls=0):
        """Draw s with its baseline at y. Returns the drawn width in px."""
        s = face.clean(s)
        *placed, (_, w) = face.advances(s, size, ls)
        x -= {"start": 0, "middle": w / 2, "end": w}[anchor]
        uses = "".join(self._glyph(face, face.cmap[ch], round(off)) for ch, off in placed if ch != " ")
        self.body.append(f'<g class="{cls}" transform="translate({_n(x)} {_n(y)}) scale({_n(size / face.upm, 5)})">{uses}</g>')
        return w

    def add(self, markup):
        self.body.append(markup)

    def enter(self, step=0.04):
        """Open a group that fades in after the previous one."""
        self.body.append(f'<g class="in" style="animation-delay:{self.delay:.2f}s">')
        self.delay += step

    def leave(self):
        self.body.append("</g>")

    def render(self, theme, h, label):
        t = THEMES[theme]
        css = (f".f{{fill:{t['fg']}}}.m{{fill:{t['muted']}}}"
               f".s{{stroke:{t['fg']};stroke-opacity:.2}}.o{{fill:none;stroke:{t['fg']}}}"
               ".in{animation:in .7s cubic-bezier(.2,.8,.2,1) backwards}"
               "@keyframes in{from{opacity:0;transform:translateY(5px)}}"
               ".rule{transform-box:fill-box;transform-origin:left;animation:rule .9s cubic-bezier(.6,0,.2,1) backwards}"
               "@keyframes rule{from{transform:scaleX(0)}}"
               ".k{animation:k 1ms steps(1) backwards}@keyframes k{from{opacity:0}}"
               "@keyframes mv{from{transform:translateX(var(--from))}}"
               "@keyframes bl{50%{opacity:0}}"
               "@media (prefers-reduced-motion:reduce){*{animation:none!important}}")
        label = html.escape(label, quote=True)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{_n(h)}" viewBox="0 0 {self.w} {_n(h)}" '
                f'role="img" aria-label="{label}"><title>{label}</title><style>{css}</style>'
                f'<defs>{"".join(self.defs.values())}</defs>{"".join(self.body)}</svg>\n')


def _n(v, digits=2):
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _attr(name, v):
    return f' {name}="{_n(v)}"' if round(v, 2) else ""


# ----------------------------------------------------------------------- sections
# Each section draws at vertical offset y and returns the y where the next one starts.

def intro(d, y, size, leading, wrap=None):
    lines = SANS_REGULAR.wrap(INTRO, size, wrap or d.w)
    d.enter()
    for i, line in enumerate(lines):
        d.text(SANS_REGULAR, line, size, 0, y + round(leading * 0.72) + i * leading, "f")
    d.leave()
    return y + len(lines) * leading


SEP_H, SEP_BASE, SEP_SIZE = 38, 24, 13


def separator(d, y, num, title, right=0):
    """"01 / STACK ────" section divider."""
    base = y + SEP_BASE
    d.enter(0.06)
    d.text(MONO, num, 11, 0, base, "m", ls=1)
    dx = 13 * SLANT
    pts = [(22 + dx, base - 12), (26.2 + dx, base - 12), (26.2, base + 1), (22, base + 1)]
    d.add(f'<polygon class="f" points="{" ".join(f"{_n(a)},{_n(b)}" for a, b in pts)}"/>')
    x = 22 + dx + 4.2 + 9
    x += d.text(SANS_BOLD, title.upper(), SEP_SIZE, x, base, "f", ls=3.2)
    end = d.w - right
    d.leave()
    mid = round(base - SEP_SIZE * SANS_BOLD.cap / 2) + 0.5
    d.add(f'<path class="s rule" style="animation-delay:{d.delay:.2f}s" d="M{_n(x + 14)} {_n(mid)}H{_n(end)}"/>')
    return y + SEP_H




# GitHub's own Octicons (MIT, @primer/octicons 19.42.0), 16x16, for a row's kind and state.
STATE_ICONS = {
    ("pr", "merged"): "M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.95-.218ZM4.25 13.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm8.5-4.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5ZM5 3.25a.75.75 0 1 0 0 .005V3.25Z",
    ("pr", "open"): "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z",
    ("issue", "closed"): "M11.28 6.78a.75.75 0 0 0-1.06-1.06L7.25 8.69 5.78 7.22a.75.75 0 0 0-1.06 1.06l2 2a.75.75 0 0 0 1.06 0l3.5-3.5Z M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0Zm-1.5 0a6.5 6.5 0 1 0-13 0 6.5 6.5 0 0 0 13 0Z",
    ("issue", "open"): "M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z",
}


def marker(x, base, kind, status, size=12):
    """The row's icon, centred on the text's x-height: merged or open PR, closed or open issue."""
    top = base - 4.3 - size / 2
    return (f'<path class="f" fill-rule="evenodd" transform="translate({_n(x)} {_n(top)}) scale({_n(size / 16, 4)})" '
            f'd="{STATE_ICONS[(kind, status)]}"/>')


def logo(d, x, y, h, outline=False, step=0.07):
    """Draw the logo h px tall with its top-left corner at x, y; the strokes fade in one by one."""
    s = h / LOGO_BOX[3]
    d.add(f'<g transform="translate({_n(x - LOGO_BOX[0] * s)} {_n(y - LOGO_BOX[1] * s)}) scale({_n(s, 5)})">')
    for pts in LOGO:
        d.enter(step)
        if outline:
            d.add(f'<polygon class="o" stroke-opacity=".3" vector-effect="non-scaling-stroke" points="{pts}"/>')
        else:
            d.add(f'<polygon class="f" points="{pts}"/>')
        d.leave()
    d.add("</g>")
    return LOGO_BOX[2] * s


def header(d, style):
    """The small header above "00 / About me". Returns where the page continues."""
    wide = d.w >= 600
    if style == "wordmark":  # logo and AXYSS on the left, the site on the right
        h = 24
        x = logo(d, 0, 2, h) + 12
        base = round(2 + h / 2 + 15 * SANS_BOLD.cap / 2)
        d.enter()
        d.text(SANS_BOLD, "AXYSS", 15, x, base, "f", ls=4)
        d.text(MONO, SITE, 11, d.w, base, "m", anchor="end", ls=0.6)
        d.leave()
        return 2 + h + 14
    if style == "greeting":  # "Hi, I'm Alejandro." with the logo at the far end
        size = 30 if wide else 24
        base = round(size * 1.02)
        d.enter()
        d.text(SANS, f"Hi, I'm {NAME}.", size, 0, base, "f", ls=-0.4)
        d.leave()
        h = round(size * SANS.cap * 1.25)
        logo(d, d.w - 1 - LOGO_BOX[2] * h / LOGO_BOX[3], base - h, h)
        return base + 16
    if style == "prompt":  # $ whoami, typed out, with a blinking cursor
        size, base = 14, 17
        adv = MONO.width("m", size)
        d.enter()
        d.text(MONO, "~ $", size, 0, base, "m")
        d.leave()
        x0, word, t0, dt = MONO.width("~ $ ", size), "whoami", d.delay + 0.35, 0.08
        for i, ch in enumerate(word):
            d.add(f'<g class="k" style="animation-delay:{t0 + i * dt:.2f}s">')
            d.text(MONO, ch, size, x0 + i * adv, base, "f")
            d.add("</g>")
        n = len(word)
        d.add(f'<rect class="f" x="{_n(x0 + n * adv + 2)}" y="{base - 13}" width="{_n(adv)}" height="16" '
              f'style="--from:{_n(-n * adv)}px;animation:mv {n * dt:.2f}s steps({n},jump-start) {t0:.2f}s backwards,'
              f'bl 1.1s steps(1) {t0 + n * dt + 0.3:.2f}s infinite"/>')
        d.delay = t0 + n * dt
        return base + 14
    if style == "mark":  # just the logo
        h = 40 if wide else 34
        logo(d, 0, 2, h)
        return 2 + h + 12
    return 0


CHIP_H, CHIP_GAP, ICON = 24, 6, 14
HEAD_SIZE, HEAD_LS = 10.5, 1.5


def chip_parts(key):
    """(icon path or letters, text): a lettered mark stands in for the start of its name,
    so "T3 Code" reads as a bold T3 followed by Code."""
    label, box, _, path = ICONS[key]
    if not path and label.startswith(f"{box} "):
        return box, label[len(box) + 1:]
    return box, label


def chip_width(key):
    mark, text = chip_parts(key)
    lead = ICON if ICONS[key][3] else SANS_BOLD.width(mark, 9.5 / SANS_BOLD.cap, -0.3)
    return round(7 + lead + 6 + MONO.width(text, 12) + 10)


def chip(d, theme, key, x, y):
    """A tag: icon and name in a box with two corners cut along the logo's diagonal."""
    t = THEMES[theme]
    _, box, scale, path = ICONS[key]
    mark, text = chip_parts(key)
    w, c = chip_width(key), 6
    cx = c * SLANT
    pts = [(x + cx, y), (x + w, y), (x + w, y + CHIP_H - c), (x + w - cx, y + CHIP_H), (x, y + CHIP_H), (x, y + c)]
    d.add(f'<polygon points="{" ".join(f"{_n(a)},{_n(b)}" for a, b in pts)}" fill="{t["fg"]}" fill-opacity=".045" '
          f'stroke="{t["fg"]}" stroke-opacity=".26"/>')
    mid = y + CHIP_H / 2
    if path:
        size = ICON * scale
        d.add(f'<path class="f" transform="translate({_n(x + 7 + (ICON - size) / 2)} {_n(mid - size / 2)}) '
              f'scale({_n(size / box, 5)})" d="{path}"/>')
        tx = x + 7 + ICON + 6
    else:  # no official mark: letter it, e.g. "T3"
        tx = x + 7 + d.text(SANS_BOLD, mark, 9.5 / SANS_BOLD.cap, x + 7, mid + 9.5 / 2, "f", ls=-0.3) + 6
    d.text(MONO, text, 12, tx, round(mid + 12 * MONO.cap / 2, 1), "f")


def stack(d, theme, y, col_x):
    """Wide: a table, the level on the left and its chips from col_x (the PR titles' column).
    Narrow: each level above its chips, which wrap if they run out of room."""
    head_w = max(MONO.width(h.upper(), HEAD_SIZE, HEAD_LS) for h, _ in STACK)
    table = d.w >= 600
    x0 = max(col_x, round(head_w) + 24) if table else 0
    for heading, keys in STACK:
        d.enter(0.05)
        top = y if table else y + 18
        base = round(top + CHIP_H / 2 + HEAD_SIZE * MONO.cap / 2, 1) if table else y + 9
        d.text(MONO, heading.upper(), HEAD_SIZE, 0, base, "m", ls=HEAD_LS)
        d.leave()
        x = x0
        for key in keys:
            w = chip_width(key)
            if x > x0 and x + w > d.w - 1:
                x, top = x0, top + CHIP_H + CHIP_GAP
            d.enter(0.03)
            chip(d, theme, key, x + 0.5, top + 0.5)
            d.leave()
            x += w + CHIP_GAP
        y = top + CHIP_H + (8 if table else 14)
    return y - (8 if table else 14) + 1


ROW_H, ROW_BASE = 34, 21


def repo_label(d, pr, x, base, room):
    """owner/ dimmed, project name bright, #number dimmed; shortens the owner first."""
    repo = pr["repo"]
    if MONO.width(repo, 12) > room:
        repo = "…/" + MONO.fit(repo.split("/", 1)[1], 12, room - MONO.width("…/", 12))
    owner, slash, name = repo.partition("/")
    x += d.text(MONO, owner + slash, 12, x, base, "m")
    x += d.text(MONO, name, 12, x, base, "f") + MONO.width(" ", 12)
    d.text(MONO, f"#{pr['num']}", 12, x, base, "m")


def title_column(prs):
    """Wide layout: where the titles start, so they line up from one row image to the next."""
    repo_w = max(MONO.width(f"{p['repo']} #{p['num']}", 12) for p in prs)
    return round(min(max(20 + repo_w + 28, 220), 300))


def row(d, pr, title_x):
    """One PR. Wide: a single line. Narrow: repo and date on top, title underneath."""
    d.enter(0.06)
    date = pr["date"].strftime("%b %Y").upper()
    base = ROW_BASE
    d.add(marker(0, base, pr["kind"], pr["status"]))
    dw = d.text(MONO, date, 11, d.w, base, "m", anchor="end", ls=1.1)
    if d.w >= 600:
        repo_label(d, pr, 20, base, title_x - 20 - 24 - MONO.width(f" #{pr['num']}", 12))
        d.text(SANS, SANS.fit(pr["title"], 14.5, d.w - dw - 28 - title_x), 14.5, title_x, base, "f")
        h = ROW_H
    else:
        repo_label(d, pr, 20, base, d.w - dw - 16 - 20 - MONO.width(f" #{pr['num']}", 12))
        lines = SANS.wrap(pr["title"], 14.5, d.w - 20)
        if len(lines) > 2:  # at most two lines, the second one ellipsised
            lines = [lines[0], SANS.fit(" ".join(lines[1:]), 14.5, d.w - 20)]
        for k, line in enumerate(lines):
            d.text(SANS, line, 14.5, 20, base + 22 + 20 * k, "f")
        h = ROW_H + 22 + 20 * (len(lines) - 1)
    d.add(f'<path class="s" d="M0 {_n(h - 0.5)}H{d.w}" stroke-dasharray="2 4"/>')
    d.leave()
    return h


def images(theme, layout, prs):
    """The README's images, top to bottom, as (name, alt, link, svg). The fade-in delays
    carry on from one image to the next, so the page still builds up in one sweep."""
    d = Doc(LAYOUTS[layout])
    wide = layout == "wide"
    title_x = title_column(prs)
    y = header(d, HEADER) if HEADER else 0
    if HEADER == "outline":  # the logo's outline beside the About section; on phones, at the heading's end
        top = y
        lh = 96 if wide else 30
        lw = LOGO_BOX[2] * lh / LOGO_BOX[3]
        y = separator(d, y, "00", "About me", right=lw + (28 if wide else 14)) + 2
        y = intro(d, y, 16 if wide else 15, 24 if wide else 22, wrap=d.w - lw - 36 if wide else None) + 14
        d.delay, later = 0.0, d.delay
        logo(d, d.w - 1 - lw, top + (10 if wide else 4), lh, outline=True)
        d.delay = later
    else:
        y = separator(d, y, "00", "About me") + 2
        y = intro(d, y, 16 if wide else 15, 24 if wide else 22) + 14
    y = separator(d, y, "01", "Stack") - 2
    y = stack(d, theme, y, title_x) + 18
    alt = intro_alt()
    out = [("intro", alt, None, d.render(theme, y, alt))]

    d = Doc(d.w, d.delay)
    y = separator(d, 0, "02", "Recent contributions")
    alt = "Recent contributions: all my pull requests"
    out.append(("contributions", alt, ALL_PRS, d.render(theme, y, alt)))

    for i, pr in enumerate(prs, 1):
        d = Doc(d.w, d.delay)
        alt = pr_alt(pr)
        out.append((f"pr-{i}", alt, pr["url"], d.render(theme, row(d, pr, title_x), alt)))
    return out


def intro_alt():
    stack_text = " ".join(f"{h}: {', '.join(ICONS[k][0] for k in keys)}." for h, keys in STACK)
    return f"About me. {INTRO} Stack. {stack_text}"


def pr_alt(p):
    status = p["status"].capitalize()
    what = "issue in" if p["kind"] == "issue" else "pull request to"
    return f'{status} {what} {p["repo"]} (#{p["num"]}): {p["title"]}, {p["date"]:%B %Y}.'


# ------------------------------------------------------------------ contributions

def fetch(retries=3):
    """My PRs and issues on other people's repos, as one search-API-shaped response."""
    items = []
    for kind in ("pr", "issue"):
        items += search(f"author:{USER} is:{kind} -user:{USER}", retries)["items"]
    return {"items": items}


def search(query, retries):
    q = urllib.parse.urlencode({"q": query, "sort": "updated",
                                "order": "desc", "per_page": 100})
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"{USER}-profile-readme",
               "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"https://api.github.com/search/issues?{q}", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            print(f"GitHub API error ({e}), retrying…", file=sys.stderr)
            time.sleep(5 * 2 ** attempt)


def pick(data):
    rows_ = []
    for it in data.get("items", []):
        if "pull_request" in it:
            done = it["pull_request"].get("merged_at")
            if it.get("draft") or (it["state"] == "closed" and not done):
                continue  # drafts and PRs closed without merging
            kind, status = "pr", "merged" if done else "open"
        else:
            done = it.get("closed_at") if it["state"] == "closed" else None
            if it["state"] == "closed" and it.get("state_reason") != "completed":
                continue  # issues closed as not planned or duplicate
            kind, status = "issue", "closed" if done else "open"
        when = done or it["created_at"]
        title = re.sub(r"^(?:[^\w\[]+|\[[^\]]*\]\s*)+", "", it["title"].strip()) or it["title"].strip()
        rows_.append(dict(repo=it["repository_url"].split("/repos/", 1)[1], num=it["number"],
                          title=title, url=it["html_url"], kind=kind, status=status,
                          date=datetime.strptime(when[:10], "%Y-%m-%d"), sort=when))
    rows_.sort(key=lambda r: r["sort"], reverse=True)
    return rows_[:ROWS]


def picture(name, alt):
    """Two pictures: the wide layout and the phone one. Each chooses light or dark on its own, and the
    one that doesn't fit the screen shows a blank 0x0 image. Width and theme never share a media
    query, because GitHub rewrites any query that mentions prefers-color-scheme to follow the theme
    chosen in its settings, which would make "(max-width: 600px) and (prefers-color-scheme: dark)"
    match on desktop too."""
    out = []
    for layout, hide in (("wide", f"(max-width: {NARROW_MAX}px)"), ("narrow", f"(min-width: {NARROW_MAX + 1}px)")):
        src = lambda theme: f"assets/{name}-{layout}-{theme}.svg"  # noqa: E731
        # align="top" drops the gap browsers leave under inline images, so the rows sit flush
        out.append("<picture>"
                   f'<source media="{hide}" srcset="assets/blank.svg" width="0" height="0">'
                   f'<source media="(prefers-color-scheme: dark)" srcset="{src("dark")}">'
                   f'<img src="{src("light")}" alt="{html.escape(alt, quote=True)}" width="100%" align="top">'
                   "</picture>")
    return "".join(out)


def update_readme(parts):
    """Replace what sits between the readme:start and readme:end markers in README.md."""
    start, end = "<!-- readme:start -->", "<!-- readme:end -->"
    lines = [picture(name, alt) if href is None else
             f'<a href="{html.escape(href, quote=True)}">{picture(name, alt)}</a>'
             for name, alt, href, _ in parts]
    block = "<p>\n" + "<br>\n".join(lines) + "\n</p>"
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        sys.exit(f"README.md is missing the {start} … {end} markers")
    README.write_text(pattern.sub(lambda _: f"{start}\n{block}\n{end}", text), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", help="read a saved search API response instead of calling GitHub")
    args = ap.parse_args()
    data = json.loads(Path(args.src).read_text(encoding="utf-8")) if args.src else fetch()
    prs = pick(data)
    if not prs:
        print("No pull requests found; leaving the README untouched.", file=sys.stderr)
        return
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "blank.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="0"/>\n', encoding="utf-8")
    written = set()
    for layout in LAYOUTS:
        for theme in THEMES:
            parts = images(theme, layout, prs)
            for name, _, _, svg in parts:
                path = ASSETS / f"{name}-{layout}-{theme}.svg"
                path.write_text(svg, encoding="utf-8")
                written.add(path.name)
    for old in ASSETS.glob("pr-*.svg"):  # rows left over from a longer list
        if old.name not in written:
            old.unlink()
    update_readme(parts)
    for pr in prs:
        print(f'{pr["date"]:%Y-%m-%d} {pr["kind"]:5} {pr["status"]:6} {pr["repo"]}#{pr["num"]} {pr["title"]}')


GLYPHS = json.loads(Path(__file__).with_name("glyphs.json").read_text(encoding="utf-8"))
SANS_REGULAR, SANS, SANS_BOLD = Face("sans-regular", "r"), Face("sans", "s"), Face("sans-bold", "b")
MONO, MONO_BOLD = Face("mono", "m"), Face("mono-bold", "n")

# key: (label, viewBox size, optical scale, path). Simple Icons (CC0) and Devicon (MIT).
# An icon without a path is lettered with the text given in place of the viewBox size.
ICONS = {
    "python": ("Python", 24, 1.0, "M11.858 0c-.978.005-1.913.088-2.736.234C6.7.662 6.26 1.558 6.26 3.21v2.182h5.725v.727H4.111c-1.664 0-3.121 1-3.577 2.903-.526 2.181-.549 3.542 0 5.82.407 1.694 1.379 2.902 3.043 2.902h1.969v-2.616c0-1.89 1.635-3.557 3.576-3.557h5.72c1.591 0 2.862-1.31 2.862-2.91v-5.45c0-1.553-1.309-2.718-2.863-2.977A18 18 0 0 0 11.858 0M8.762 1.755c.591 0 1.074.491 1.074 1.095 0 .601-.483 1.087-1.074 1.087A1.08 1.08 0 0 1 7.688 2.85c0-.604.48-1.095 1.074-1.095m9.656 4.364v2.543c0 1.971-1.671 3.63-3.577 3.63H9.122c-1.566 0-2.862 1.34-2.862 2.91v5.452c0 1.551 1.349 2.464 2.862 2.91 1.813.532 3.55.628 5.72 0 1.44-.418 2.862-1.258 2.862-2.91V18.47h-5.719v-.727h8.582c1.664 0 2.284-1.16 2.862-2.903.598-1.793.573-3.518 0-5.819-.41-1.656-1.196-2.903-2.862-2.903zm-3.216 13.807a1.08 1.08 0 0 1 1.074 1.088c0 .604-.48 1.094-1.074 1.094-.592 0-1.075-.49-1.075-1.094 0-.601.483-1.088 1.075-1.088"),
    "java": ("Java", 128, 1.25, "M47.617 98.12c-19.192 5.362 11.677 16.439 36.115 5.969-4.003-1.556-6.874-3.351-6.874-3.351-10.897 2.06-15.952 2.222-25.844 1.092-8.164-.935-3.397-3.71-3.397-3.71zm33.189-10.46c-14.444 2.779-22.787 2.69-33.354 1.6-8.171-.845-2.822-4.805-2.822-4.805-21.137 7.016 11.767 14.977 41.309 6.336-3.14-1.106-5.133-3.131-5.133-3.131zm11.319-60.575c.001 0-42.731 10.669-22.323 34.187 6.024 6.935-1.58 13.17-1.58 13.17s15.289-7.891 8.269-17.777c-6.559-9.215-11.587-13.793 15.634-29.58zm9.998 81.144s3.529 2.91-3.888 5.159c-14.102 4.272-58.706 5.56-71.095.171-4.45-1.938 3.899-4.625 6.526-5.192 2.739-.593 4.303-.485 4.303-.485-4.952-3.487-32.013 6.85-13.742 9.815 49.821 8.076 90.817-3.637 77.896-9.468zM85 77.896c2.395-1.634 5.703-3.053 5.703-3.053s-9.424 1.685-18.813 2.474c-11.494.964-23.823 1.154-30.012.326-14.652-1.959 8.033-7.348 8.033-7.348s-8.812-.596-19.644 4.644C17.455 81.134 61.958 83.958 85 77.896zm5.609 15.145c-.108.29-.468.616-.468.616 31.273-8.221 19.775-28.979 4.822-23.725-1.312.464-2 1.543-2 1.543s.829-.334 2.678-.72c7.559-1.575 18.389 10.119-5.032 22.286zM64.181 70.069c-4.614-10.429-20.26-19.553.007-35.559C89.459 14.563 76.492 1.587 76.492 1.587c5.23 20.608-18.451 26.833-26.999 39.667-5.821 8.745 2.857 18.142 14.688 28.815zm27.274 51.748c-19.187 3.612-42.854 3.191-56.887.874 0 0 2.874 2.38 17.646 3.331 22.476 1.437 57-.8 57.816-11.436.001 0-1.57 4.032-18.575 7.231z"),
    "typescript": ("TypeScript", 24, 0.88, "M1.125 0C.502 0 0 .502 0 1.125v21.75C0 23.498.502 24 1.125 24h21.75c.623 0 1.125-.502 1.125-1.125V1.125C24 .502 23.498 0 22.875 0zm17.363 9.75c.612 0 1.154.037 1.627.111a6.38 6.38 0 0 1 1.306.34v2.458a3.95 3.95 0 0 0-.643-.361 5.093 5.093 0 0 0-.717-.26 5.453 5.453 0 0 0-1.426-.2c-.3 0-.573.028-.819.086a2.1 2.1 0 0 0-.623.242c-.17.104-.3.229-.393.374a.888.888 0 0 0-.14.49c0 .196.053.373.156.529.104.156.252.304.443.444s.423.276.696.41c.273.135.582.274.926.416.47.197.892.407 1.266.628.374.222.695.473.963.753.268.279.472.598.614.957.142.359.214.776.214 1.253 0 .657-.125 1.21-.373 1.656a3.033 3.033 0 0 1-1.012 1.085 4.38 4.38 0 0 1-1.487.596c-.566.12-1.163.18-1.79.18a9.916 9.916 0 0 1-1.84-.164 5.544 5.544 0 0 1-1.512-.493v-2.63a5.033 5.033 0 0 0 3.237 1.2c.333 0 .624-.03.872-.09.249-.06.456-.144.623-.25.166-.108.29-.234.373-.38a1.023 1.023 0 0 0-.074-1.089 2.12 2.12 0 0 0-.537-.5 5.597 5.597 0 0 0-.807-.444 27.72 27.72 0 0 0-1.007-.436c-.918-.383-1.602-.852-2.053-1.405-.45-.553-.676-1.222-.676-2.005 0-.614.123-1.141.369-1.582.246-.441.58-.804 1.004-1.089a4.494 4.494 0 0 1 1.47-.629 7.536 7.536 0 0 1 1.77-.201zm-15.113.188h9.563v2.166H9.506v9.646H6.789v-9.646H3.375z"),
    "vue": ("Vue", 24, 1.0, "M24,1.61H14.06L12,5.16,9.94,1.61H0L12,22.39ZM12,14.08,5.16,2.23H9.59L12,6.41l2.41-4.18h4.43Z"),
    "csharp": ("C#", 128, 1.1, "M117.5 33.5l.3-.2c-.6-1.1-1.5-2.1-2.4-2.6L67.1 2.9c-.8-.5-1.9-.7-3.1-.7-1.2 0-2.3.3-3.1.7l-48 27.9c-1.7 1-2.9 3.5-2.9 5.4v55.7c0 1.1.2 2.3.9 3.4l-.2.1c.5.8 1.2 1.5 1.9 1.9l48.2 27.9c.8.5 1.9.7 3.1.7 1.2 0 2.3-.3 3.1-.7l48-27.9c1.7-1 2.9-3.5 2.9-5.4V36.1c.1-.8 0-1.7-.4-2.6zm-53.5 70c-21.8 0-39.5-17.7-39.5-39.5S42.2 24.5 64 24.5c14.7 0 27.5 8.1 34.3 20l-13 7.5C81.1 44.5 73.1 39.5 64 39.5c-13.5 0-24.5 11-24.5 24.5s11 24.5 24.5 24.5c9.1 0 17.1-5 21.3-12.4l12.9 7.6c-6.8 11.8-19.6 19.8-34.2 19.8zM115 62h-3.2l-.9 4h4.1v5h-5l-1.2 6h-4.9l1.2-6h-3.8l-1.2 6h-4.8l1.2-6H94v-5h3.5l.9-4H94v-5h5.3l1.2-6h4.9l-1.2 6h3.8l1.2-6h4.8l-1.2 6h2.2v5zm-12.7 4h3.8l.9-4h-3.8z"),
    "nextjs": ("Next.js", 24, 1.0, "M18.665 21.978C16.758 23.255 14.465 24 12 24 5.377 24 0 18.623 0 12S5.377 0 12 0s12 5.377 12 12c0 3.583-1.574 6.801-4.067 9.001L9.219 7.2H7.2v9.596h1.615V9.251l9.85 12.727Zm-3.332-8.533 1.6 2.061V7.2h-1.6v6.245Z"),
    "kotlin": ("Kotlin", 24, 0.84, "M24 24H0V0h24L12 12Z"),
    "claudecode": ("Claude Code", 24, 1.0, "M21 10.5h3v3h-3v3h-1.5v3H18v-3h-1.5v3H15v-3H9v3H7.5v-3H6v3H4.5v-3H3v-3H0v-3h3v-6h18Zm-15 0h1.5v-3H6Zm10.5 0H18v-3h-1.5z"),
    "mcp": ("MCP", 24, 1.0, "M13.85 0a4.16 4.16 0 0 0-2.95 1.217L1.456 10.66a.835.835 0 0 0 0 1.18.835.835 0 0 0 1.18 0l9.442-9.442a2.49 2.49 0 0 1 3.541 0 2.49 2.49 0 0 1 0 3.541L8.59 12.97l-.1.1a.835.835 0 0 0 0 1.18.835.835 0 0 0 1.18 0l.1-.098 7.03-7.034a2.49 2.49 0 0 1 3.542 0l.049.05a2.49 2.49 0 0 1 0 3.54l-8.54 8.54a1.96 1.96 0 0 0 0 2.755l1.753 1.753a.835.835 0 0 0 1.18 0 .835.835 0 0 0 0-1.18l-1.753-1.753a.266.266 0 0 1 0-.394l8.54-8.54a4.185 4.185 0 0 0 0-5.9l-.05-.05a4.16 4.16 0 0 0-2.95-1.218c-.2 0-.401.02-.6.048a4.17 4.17 0 0 0-1.17-3.552A4.16 4.16 0 0 0 13.85 0m0 3.333a.84.84 0 0 0-.59.245L6.275 10.56a4.186 4.186 0 0 0 0 5.902 4.186 4.186 0 0 0 5.902 0L19.16 9.48a.835.835 0 0 0 0-1.18.835.835 0 0 0-1.18 0l-6.985 6.984a2.49 2.49 0 0 1-3.54 0 2.49 2.49 0 0 1 0-3.54l6.983-6.985a.835.835 0 0 0 0-1.18.84.84 0 0 0-.59-.245"),
    "gemini": ("Gemini 4 Neon", 24, 1.05, "M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"),
    "fable": ("Fable 5.2", 24, 1.0, "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"),  # Claude mark
    "t3code": ("T3 Code", "T3", 1.0, ""),  # no official mark: lettered instead
}

if __name__ == "__main__":
    main()
