#!/usr/bin/env python3
"""Draw the profile README as a stack of animated SVG cards.

    python scripts/render_cards.py            # cards/*.svg and the README block
    STATIC=1 python scripts/render_cards.py   # the final frame, into preview/

Reads data/contributions.json and data/repos.json (fetch_contributions.py writes
both; the Cards workflow runs the two every morning) and writes one SVG per card,
then rewrites the block between the cards markers in README.md.

GitHub shows each card through an <img>, which runs no scripts and makes no
outside requests, so a card carries everything it needs: an inline <style> with
CSS keyframes, SMIL <set> elements for the count-up, and Archivo subset with
pyftsubset to the characters that card uses, embedded as a base64 WOFF data URI.

How the motion is built, so STATIC and reduced motion stay honest:
- An element's own attributes are its final frame. Keyframes only say where it
  comes from, and run with `animation-fill-mode: both`.
- So STATIC=1 writes the same SVG without keyframes or SMIL, and under
  prefers-reduced-motion one rule (`animation: none`) shows the final frame.
- CSS can't stop SMIL, so the SMIL count-up frames sit in a `.m` group that
  reduced motion hides. The final value is a separate element beside them.
- Delays are seconds from page load. Each card starts while the one above it is
  still playing, so the page plays from top to bottom.

Colours and type come from jaypurohit.com (website-build/jay-p: tokens.css and
docs/DESIGN-SYSTEM.md): cream on near-black, Archivo, and hot pink only as light.
The name's full stop is the site's travelling dot, and its landing is the site's
field of square dots.
"""

from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from fontTools import subset as pyftsubset  # `pyftsubset` is fontTools.subset.main
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / "preview" / "cards" if STATIC else ROOT / "cards"

# --- What the cards say -------------------------------------------------------

LOGIN = "Jayqtpie"
NAME = ("Jay", "Purohit")  # Two lines. The dot starts as the tittle of the last i.
TAGLINE = "Websites, designed and built in London"
ABOUT = "I design websites and then build them myself, so the thing you approve is the thing that goes live."
EMAIL = "hello@jaypurohit.com"
# Three tiles in a row, each a link. `label` may use {repos}, the public repo count.
TILES = [
    {"slug": "websites", "word": "Websites", "label": "Designed and built in London", "href": "https://jaypurohit.com"},
    {"slug": "code", "word": "Code", "label": "{repos} public repositories",
     "href": f"https://github.com/{LOGIN}?tab=repositories"},
    {"slug": "contact", "word": "Contact", "label": EMAIL, "href": f"mailto:{EMAIL}"},
]
# One card each, in this order. Names as on GitHub.
FEATURED = ["alliance-tracker", "cms", "Ramadan-Planner"]
# Optional. assets/portrait.(png|jpg|jpeg|webp) is drawn beside the name as a
# field of square dots, bigger where the photo is lighter. Best on a dark backdrop.
PORTRAIT = sorted(p for p in ROOT.glob("assets/portrait.*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})

# --- jaypurohit.com's tokens --------------------------------------------------

BG = "#0B0B0A"
TILE_BG = "#1A1917"
RULE = "#262522"
LINE = "#3A3833"
NUM_OFF = "#34322E"
INK = "#EDE9E1"
TEXT_2 = "#D8D3C8"
TEXT_3 = "#C9C4B8"
MUTED = "#A39E93"
FAINT = "#8A8579"
ACCENT = "#FF1F8E"

EASE_OUT = "cubic-bezier(.2,.8,.2,1)"
SPRING = "cubic-bezier(.3,1.6,.5,1)"  # the site's squash and ring
QUAD_OUT = "cubic-bezier(.5,1,.89,1)"
QUAD_IN = "cubic-bezier(.11,0,.5,0)"

W = 880  # Full-width cards' panel.
TILE_W = 286  # Three tile panels and two gaps make up W.
TILE_PCT = 32.5
# Spaces between the tiles' tags would be px wide and push the third tile onto a
# new line on a phone, so the tags touch and each SVG carries transparent margin
# instead: half a gap each side of a tile, and as much each side of a full card as
# lines its edges up with the row. All percentages, so it holds at any width.
TILE_GUTTER = (W - 3 * TILE_W) / 4
CARD_GUTTER = ((TILE_W + 2 * TILE_GUTTER) / (TILE_PCT / 100) - W) / 2
TRACK = -0.045  # The site's display tracking, in em.

# Shared keyframes. Each says only where an element comes from (see the top).
KEYFRAMES = {
    "fade": "from{opacity:0}",
    "rise": "from{opacity:0;transform:translateY(26px)}",
    "lift": "from{opacity:0;transform:translateY(12px)}",
    "swing": "from{opacity:0;transform:translateY(64px) rotate(6deg)}",
    "grow": "from{transform:scaleY(0)}",
    "pop": "from{transform:scale(0)}",
    "wave": "from{transform:scale(.001)}",
    "crest": "0%,70%{opacity:1}100%{opacity:0}",
    "squash": "0%{transform:scale(1.4,.6)}55%{transform:scale(.9,1.12)}100%{transform:none}",
    "landed": f"from{{fill:{INK}}}",
    "light": f"from{{fill:{NUM_OFF}}}",
    "lightbar": f"from{{fill:{LINE}}}",
    "ring": "0%{opacity:1;transform:none;animation-timing-function:ease-out}20%,100%{opacity:0;transform:scale(3.4)}",
    "ring1": "from{opacity:1;transform:none}to{opacity:0;transform:scale(3.4)}",
    "nudge": "50%{transform:translate(3px,-3px)}",
    "wait": "from,to{opacity:0}",
}
BASE_CSS = (
    "text{font-family:Archivo,system-ui,sans-serif}"
    ".fb{transform-box:fill-box;transform-origin:center}"
    ".fbb{transform-box:fill-box;transform-origin:50% 100%}"
)
REDUCED = "@media (prefers-reduced-motion:reduce){*{animation:none!important}.m{display:none}}"


def fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def px(v: float) -> str:
    return fmt(v) + "px"


def st(*decls: str) -> str:
    """A style attribute from the declarations given, or nothing. Several
    `animation` declarations become one list, since a second would replace the first."""
    plays = [x.removeprefix("animation:") for x in decls if x.startswith("animation:")]
    rest = [x for x in decls if x and not x.startswith("animation:")]
    d = ";".join(rest + ([f"animation:{','.join(plays)}"] if plays else []))
    return f' style="{d}"' if d else ""


def grouped(n: int) -> str:
    return f"{n:,}"


def day_month(d: date, year: bool = True) -> str:
    return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")


# --- Fonts --------------------------------------------------------------------


class Kerning:
    """Archivo's pair kerning (GPOS type 2), so text measured here lines up with
    how a browser sets it."""

    def __init__(self, font: TTFont):
        self.tables: list[tuple[int, object]] = []
        if "GPOS" not in font:
            return
        gpos = font["GPOS"].table
        wanted = sorted({i for r in gpos.FeatureList.FeatureRecord if r.FeatureTag == "kern"
                         for i in r.Feature.LookupListIndex})
        for i in wanted:
            lookup = gpos.LookupList.Lookup[i]
            for sub in lookup.SubTable:
                kind = lookup.LookupType
                if kind == 9:
                    kind, sub = sub.ExtensionLookupType, sub.ExtSubTable
                if kind != 2:
                    continue
                if sub.Format == 1:
                    pairs = {
                        first: {r.SecondGlyph: self.x(getattr(r, "Value1", None)) for r in pset.PairValueRecord}
                        for first, pset in zip(sub.Coverage.glyphs, sub.PairSet)
                    }
                    self.tables.append((1, pairs))
                else:
                    self.tables.append((2, (set(sub.Coverage.glyphs), sub.ClassDef1.classDefs,
                                            sub.ClassDef2.classDefs, sub.Class1Record)))

    @staticmethod
    def x(value) -> int:
        return (getattr(value, "XAdvance", 0) or 0) if value is not None else 0

    def __call__(self, a: str, b: str) -> int:
        for kind, table in self.tables:
            if kind == 1:
                if a in table and b in table[a]:
                    return table[a][b]
            else:
                cover, class1, class2, records = table
                if a in cover:
                    return self.x(records[class1.get(a, 0)].Class2Record[class2.get(b, 0)].Value1)
        return 0


class Face:
    """One static weight of Archivo, cut from the variable font at the site's
    default width."""

    def __init__(self, var: TTFont, weight: int, folder: Path):
        font = instancer.instantiateVariableFont(var, {"wght": weight, "wdth": 100})
        font.recalcTimestamp = False  # Same input, same bytes: no daily commit noise.
        self.path = folder / f"archivo-{weight}.ttf"
        font.save(self.path)
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"].metrics
        self.glyphs = font.getGlyphSet()
        self.kern = Kerning(font)

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def gid(self, ch: str) -> str:
        if ord(ch) not in self.cmap:
            sys.exit(f"Archivo has no glyph for {ch!r} (U+{ord(ch):04X})")
        return self.cmap[ord(ch)]

    def advance(self, ch: str) -> float:
        return self.hmtx[self.gid(ch)][0]

    def top(self, ch: str) -> float:
        pen = BoundsPen(self.glyphs)
        self.glyphs[self.gid(ch)].draw(pen)
        return pen.bounds[3]

    def layout(self, text: str, size: float, tracking: float = 0.0) -> tuple[list[float], float]:
        """Each character's x, and the pen position after the last, as a browser
        sets it: kerning, then `letter-spacing` after every character."""
        scale = size / self.upm
        xs, x, prev = [], 0.0, None
        for ch in text:
            g = self.gid(ch)
            if prev is not None:
                x += self.kern(prev, g) * scale
            xs.append(x)
            x += self.hmtx[g][0] * scale + tracking * size
            prev = g
        return xs, x

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        return self.layout(text, size, tracking)[1] - (tracking * size if text else 0)


class Fonts:
    def __init__(self):
        self.var = TTFont(ROOT / "fonts" / "Archivo[wdth,wght].ttf")
        self.dir = Path(tempfile.mkdtemp(prefix="cards-fonts-"))
        self.faces: dict[int, Face] = {}

    def __call__(self, weight: int) -> Face:
        if weight not in self.faces:
            self.faces[weight] = Face(self.var, weight, self.dir)
        return self.faces[weight]

    def face_css(self, weight: int, chars: set[str]) -> str:
        """An @font-face holding only `chars`, as a base64 WOFF data URI."""
        out = self.dir / f"subset-{weight}.woff"
        codes = ",".join(f"U+{ord(ch):04X}" for ch in sorted(chars))
        status = pyftsubset.main([
            str(self(weight).path), f"--unicodes={codes}", f"--output-file={out}",
            "--flavor=woff", "--layout-features=kern", "--no-hinting",
        ])
        if status:
            sys.exit(f"pyftsubset failed with status {status}")
        b64 = base64.b64encode(out.read_bytes()).decode("ascii")
        return f"@font-face{{font-family:Archivo;font-weight:{weight};src:url(data:font/woff;base64,{b64}) format('woff')}}"

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


FONTS: Fonts  # Set in main().


def wrap(text: str, size: float, width: float, weight: int = 400) -> list[str]:
    face, lines, line = FONTS(weight), [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and face.width(trial, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def clip(text: str, size: float, width: float, weight: int = 400) -> str:
    face = FONTS(weight)
    if face.width(text, size) <= width:
        return text
    dots = "…" if face.has("…") else "..."
    while text and face.width(text.rstrip() + dots, size) > width:
        text = text[:-1]
    return text.rstrip() + dots


# --- Cards --------------------------------------------------------------------


class Card:
    def __init__(self, slug: str, w: int, h: int, alt: str, gutter: float = CARD_GUTTER):
        self.slug, self.w, self.h, self.alt, self.gutter = slug, w, h, alt, gutter
        self.defs: list[str] = []
        self.body: list[str] = []
        self.rules: dict[str, str] = {}
        self.chars: dict[int, set[str]] = defaultdict(set)

    def add(self, *parts: str) -> None:
        self.body.extend(parts)

    def keyframes(self, name: str, frames: str) -> None:
        self.rules[name] = f"@keyframes {name}{{{frames}}}"

    def rule(self, name: str, css: str) -> None:
        self.rules[name] = css

    def motion(self, name: str, dur: float, delay: float, ease: str = EASE_OUT,
               fill: str = "both", repeat: str = "") -> str:
        """An `animation` declaration for keyframes `name`, or nothing when STATIC."""
        if STATIC:
            return ""
        if name in KEYFRAMES and name not in self.rules:
            self.keyframes(name, KEYFRAMES[name])
        return f"animation:{name} {fmt(dur)}s {ease} {fmt(delay)}s {fill}" + (f" {repeat}" if repeat else "")

    def text(self, s: str, x: float, y: float, size: float, weight: int = 400, fill: str = INK,
             tracking: float = 0.0, anchor: str = "start", extra: str = "", child: str = "") -> str:
        """A <text>. Tracked text gets one x per character, placed with Archivo's
        own advances and kerning, so it sets the same in every browser."""
        self.chars[weight].update(s)
        attrs = f'y="{fmt(y)}" font-size="{fmt(size)}" font-weight="{weight}" fill="{fill}"'
        if tracking:
            xs, _ = FONTS(weight).layout(s, size, tracking)
            w = FONTS(weight).width(s, size, tracking)
            x0 = x - {"start": 0, "middle": w / 2, "end": w}[anchor]
            xattr = " ".join(fmt(x0 + v) for v in xs)
        else:
            xattr = fmt(x)
            if anchor != "start":
                attrs += f' text-anchor="{anchor}"'
        return f'<text x="{xattr}" {attrs}{extra}>{html.escape(s, quote=False)}{child}</text>'

    def count_up(self, value: int, x: float, y: float, size: float, start: float, dur: float,
                 **kw) -> str:
        """`value`, counted up from zero by SMIL <set> frames, then held. The held
        value is its own element, so reduced motion and STATIC show just that."""
        final = self.text(grouped(value), x, y, size, extra=st(self.motion("wait", start + dur, 0, "linear", "none")), **kw)
        if STATIC:
            return final
        steps = 18
        frames, shown = [], None
        for k in range(steps):
            v = round(value * (1 - (1 - k / steps) ** 3))
            t0 = start + dur * k / steps
            if v == shown:  # Same number as the frame before: stretch that one.
                frames[-1][2] = start + dur * (k + 1) / steps
                continue
            frames.append([v, t0, start + dur * (k + 1) / steps])
            shown = v
        sets = "".join(
            self.text(grouped(v), x, y, size, extra=' visibility="hidden"',
                      child=f'<set attributeName="visibility" to="visible" begin="{fmt(t0)}s" dur="{fmt(t1 - t0)}s"/>', **kw)
            for v, t0, t1 in frames
        )
        return f'<g class="m">{sets}</g>{final}'

    def frame(self) -> None:
        self.add(f'<rect width="{self.w}" height="{self.h}" fill="{BG}"/>',
                 f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" fill="none" stroke="{RULE}"/>')

    def svg(self) -> str:
        css = "".join(FONTS.face_css(w, chars) for w, chars in sorted(self.chars.items()) if chars)
        css += BASE_CSS
        if not STATIC:
            css += "".join(self.rules.values()) + REDUCED
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        full = fmt(self.w + 2 * self.gutter)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{full}" height="{self.h}" '
                f'viewBox="0 0 {full} {self.h}" role="img" aria-label="{html.escape(self.alt)}">'
                f"<style>{css}</style>{defs}<g transform=\"translate({fmt(self.gutter)} 0)\">"
                f"{''.join(self.body)}</g></svg>\n")


def stop_square(c: Card, x: float, base: float, size: float, lit_at: float, ring: str) -> str:
    """The site's square full stop after a word ending at pen position `x`:
    0.15em, 0.035em after the word, its foot 0.012em under the baseline. It
    lights `num-off` to `accent`, and a 1px square ring leaves it."""
    s = 0.15 * size
    left, top = x + 0.035 * size, base + 0.012 * size - s
    box = f'x="{fmt(left)}" y="{fmt(top)}" width="{fmt(s)}" height="{fmt(s)}"'
    out = f'<rect {box} fill="{ACCENT}"{st(c.motion("light", .25, lit_at, "ease-in"))}/>'
    if ring:
        out += (f'<rect {box} class="fb" fill="none" stroke="{ACCENT}" vector-effect="non-scaling-stroke" '
                f'opacity="0"{st(ring)}/>')
    return out


def arrow(c: Card, x: float, y: float, delay: float) -> str:
    return (f'<g transform="translate({fmt(x)} {fmt(y)})"><path d="M0 11L11 0M3 0H11V8" fill="none" stroke="{INK}" '
            f'stroke-width="1.6"{st(c.motion("nudge", .7, delay, "ease-in-out", "none"))}/></g>')


def hero(stats: Stats) -> Card:
    H = 420
    c = Card("hero", W, H, f"{' '.join(NAME)}. {ABOUT}")
    c.frame()
    F, x0, y1 = 128, 40, 180
    y2 = y1 + 0.88 * F
    face = FONTS(800)
    em = F / face.upm
    first, second = NAME
    cut = second.rindex("i")
    second = second[:cut] + "ı" + second[cut + 1:]  # dotless: the dot is its tittle
    xs, pen = face.layout(second, F, TRACK)
    S = 0.15 * F

    # Where the dot sits, perches and lands (jay-p/components/TravellingDot.tsx):
    # on the ı as Archivo 800's own tittle, then the top of the last letter 42%
    # across it, then the full stop.
    tit = (x0 + xs[cut] + face.advance("ı") * em / 2, y2 - face.top("i") * em + 0.126 * F / 2)
    perch = (x0 + xs[-1] + face.advance(second[-1]) * em * 0.42, y2 - face.top(second[-1]) * em - S / 2)
    stop = (x0 + pen + 0.035 * F + S / 2, y2 + 0.012 * F - S / 2)

    # The intro, in seconds: sit, hop, perch, drop. Then it lands and turns pink.
    T0, SIT, HOP, PERCH, DROP = 0.15, 1.2, 0.56, 0.32, 0.52
    total = SIT + HOP + PERCH + DROP
    land = T0 + total

    # The landing: a wave runs out from the dot through a field of tiny square
    # dots, which settle at 20% muted (jay-p/components/dotField.ts).
    gap, dot = max(15, round(F * 0.135)), max(1.5, F * 0.0125)
    ox, oy = (stop[0] - gap / 2) % gap, (stop[1] - gap / 2) % gap
    for pid, side, colour in (("fd", dot, MUTED), ("fl", dot * 2.6, ACCENT)):
        c.defs.append(f'<pattern id="{pid}" width="{gap}" height="{gap}" x="{fmt(ox)}" y="{fmt(oy)}" '
                      f'patternUnits="userSpaceOnUse"><rect x="{fmt((gap - side) / 2)}" y="{fmt((gap - side) / 2)}" '
                      f'width="{fmt(side)}" height="{fmt(side)}" fill="{colour}"/></pattern>')
    c.defs.append('<linearGradient id="fv" x2="0" y2="1"><stop offset="0"/><stop offset=".06" stop-color="#fff"/>'
                  '<stop offset=".72" stop-color="#fff"/><stop offset="1"/></linearGradient>')
    c.defs.append('<radialGradient id="rv"><stop offset="0" stop-color="#fff"/><stop offset=".72" stop-color="#fff"/>'
                  '<stop offset=".86"/></radialGradient>')
    c.defs.append('<radialGradient id="rc"><stop offset=".7"/><stop offset=".86" stop-color="#fff"/>'
                  '<stop offset="1"/></radialGradient>')
    far = max(math.hypot(x - stop[0], y - stop[1]) for x in (0, W) for y in (0, H))
    wave = st(c.motion("wave", 1.5, land, "cubic-bezier(.3,.6,.5,1)"))
    box = f'maskUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"'
    c.defs.append(f'<mask id="fade" {box}><rect width="{W}" height="{H}" fill="url(#fv)"/></mask>')
    for mid, grad in (("reveal", "rv"), ("crest", "rc")):
        c.defs.append(f'<mask id="{mid}" {box}><g transform="translate({fmt(stop[0])} {fmt(stop[1])})">'
                      f'<circle r="{fmt(far / 0.72 + gap)}" fill="url(#{grad})"{wave}/></g></mask>')
    c.add(f'<g mask="url(#fade)"><rect width="{W}" height="{H}" fill="url(#fd)" opacity=".2" mask="url(#reveal)"/>'
          f'<rect width="{W}" height="{H}" fill="url(#fl)" mask="url(#crest)" opacity="0"'
          f'{st(c.motion("crest", 1.5, land, "linear"))}/></g>')

    if PORTRAIT:
        c.add(portrait(c, PORTRAIT[0], (616, 40, 224, 340)))

    c.add(c.text(TAGLINE, x0, 56, 15, fill=MUTED, extra=st(c.motion("fade", .6, 0))))
    c.add(f'<g{st(c.motion("rise", .7, .05))}>{c.text(first, x0, y1, F, 800, tracking=TRACK)}</g>')

    # The dot. Final frame: a pink square on the full stop. The groups move it
    # (x linear on the hop, eased out on the drop; y on two arcs), size it (the
    # tittle is 0.164em by 0.126em, the stop 0.15em square) and squash it.
    def at(t: float) -> str:
        return f"{100 * t / total:.2f}%"

    tx, ty = tit[0] - stop[0], tit[1] - stop[1]
    hx, hy = perch[0] - stop[0], perch[1] - stop[1]
    apex = (ty + hy) / 2 - 0.32 * F
    rise, fall = math.sqrt(ty - apex), math.sqrt(hy - apex)
    lift = hy - 0.1 * F
    up, down = math.sqrt(hy - lift), math.sqrt(-lift)
    k_sit, k_hop, k_perch = at(SIT), at(SIT + HOP), at(SIT + HOP + PERCH)
    k_apex, k_lift = at(SIT + HOP * rise / (rise + fall)), at(SIT + HOP + PERCH + DROP * up / (up + down))
    c.keyframes("dx", f"0%,{k_sit}{{transform:translateX({px(tx)});animation-timing-function:linear}}"
                      f"{k_hop},{k_perch}{{transform:translateX({px(hx)});animation-timing-function:{QUAD_OUT}}}"
                      "100%{transform:none}")
    c.keyframes("dy", f"0%,{k_sit}{{transform:translateY({px(ty)});animation-timing-function:{QUAD_OUT}}}"
                      f"{k_apex}{{transform:translateY({px(apex)});animation-timing-function:{QUAD_IN}}}"
                      f"{k_hop},{k_perch}{{transform:translateY({px(hy)});animation-timing-function:{QUAD_OUT}}}"
                      f"{k_lift}{{transform:translateY({px(lift)});animation-timing-function:{QUAD_IN}}}"
                      "100%{transform:none}")
    c.keyframes("ds", f"0%,{k_sit}{{transform:scale({fmt(0.164 / 0.15)},{fmt(0.126 / 0.15)});"
                      f"animation-timing-function:linear}}{k_hop}{{transform:none}}")
    squashes = "".join(
        f'<g{st(f"transform-origin:0 {px(S / 2)}", c.motion("squash", .45, T0 + t, SPRING, "none"))}>'
        for t in (SIT - 0.18, SIT + HOP, total)
    )
    the_dot = (
        f'<g transform="translate({fmt(stop[0])} {fmt(stop[1])})">'
        f'<g{st(c.motion("dx", total, T0, "linear"))}><g{st(c.motion("dy", total, T0, "linear"))}>'
        f'<g{st(c.motion("ds", total, T0, "linear"))}>{squashes}'
        f'<rect x="{fmt(-S / 2)}" y="{fmt(-S / 2)}" width="{fmt(S)}" height="{fmt(S)}" fill="{ACCENT}"'
        f'{st(c.motion("landed", .25, land, "ease-in"))}/>'
        + "</g>" * 3 + "</g></g></g></g>"
    )
    c.add(f'<g{st(c.motion("rise", .7, .15))}>{c.text(second, x0, y2, F, 800, tracking=TRACK)}{the_dot}</g>')

    for i, line in enumerate(wrap(ABOUT, 22, 540)):
        c.add(c.text(line, x0, 352 + i * 29, 22, extra=st(c.motion("lift", .7, .5 + i * .08))))
    return c


def portrait(c: Card, path: Path, box: tuple[int, int, int, int]) -> str:
    """A photo as square dots on a grid, printed row by row."""
    from PIL import Image, ImageOps  # Only needed when there is a portrait.

    x, y, w, h = box
    step = 6
    cols, rows = w // step, h // step
    img = ImageOps.autocontrast(ImageOps.fit(Image.open(path).convert("L"), (cols, rows), Image.Resampling.LANCZOS), cutoff=1)
    out = []
    for r in range(rows):
        cells = []
        for q in range(cols):
            side = step * 0.92 * (img.getpixel((q, r)) / 255) ** 1.2
            if side >= 0.6:
                cx, cy = x + q * step + step / 2, y + r * step + step / 2
                cells.append(f'<rect x="{fmt(cx - side / 2)}" y="{fmt(cy - side / 2)}" width="{fmt(side)}" height="{fmt(side)}"/>')
        if cells:
            out.append(f'<g{st(c.motion("fade", .3, .35 + r * .018, "linear"))}>{"".join(cells)}</g>')
    return f'<g fill="{INK}">{"".join(out)}</g>'


def tile(t: dict, i: int, repo_count: int) -> Card:
    H = 200
    label = t["label"].format(repos=repo_count)
    c = Card(f"tile-{t['slug']}", TILE_W, H, f"{t['word']}. {label}.", TILE_GUTTER)
    d = 0.9 + i * 0.15
    F = 44
    base = H - 36
    pen = FONTS(800).layout(t["word"], F, TRACK)[1]
    ring = c.motion("ring", 7.8, 2.4 + i * 2.6, "linear", "none", "infinite")
    if FONTS(500).width(label, 14) > TILE_W - 24 - 44:
        print(f"warning: tile label {label!r} runs under the arrow", file=sys.stderr)
    c.add(
        f'<g{st(c.motion("swing", .9, d, SPRING), f"transform-origin:0 {H}px")}>',
        f'<rect width="{TILE_W}" height="{H}" fill="{BG}"/>',
        f'<rect x=".5" y=".5" width="{TILE_W - 1}" height="{H - 1}" fill="none" stroke="{RULE}"/>',
        c.text(label, 24, 42, 14, 500, MUTED),
        arrow(c, TILE_W - 35, 26, d + .8),
        c.text(t["word"], 24, base, F, 800, tracking=TRACK),
        stop_square(c, 24 + pen, base, F, d + .6, ring),
        "</g>",
    )
    return c


class Stats:
    """Everything the cards say about the contribution calendar."""

    def __init__(self, contrib: dict, repos: dict):
        self.days = [(date.fromisoformat(d["date"]), d["count"], d["level"]) for d in contrib["days"]]
        self.total = contrib["total"]
        self.repo_count = repos["count"]
        self.repos = {r["name"].lower(): r for r in repos["repos"]}
        counts = [n for _, n, _ in self.days]
        last = len(counts) - 1
        if counts and counts[-1] == 0:  # Today isn't over: an empty today doesn't end the streak.
            last -= 1
        self.streak = 0
        while last >= 0 and counts[last] > 0:
            self.streak += 1
            last -= 1
        self.longest = run = 0
        for n in counts:
            run = run + 1 if n else 0
            self.longest = max(self.longest, run)
        self.active = sum(1 for n in counts if n)
        self.busiest = max(self.days, key=lambda d: (d[1], d[0]))
        end = self.days[-1][0]
        self.months = []
        for back in range(11, -1, -1):
            y, m = divmod(end.year * 12 + end.month - 1 - back, 12)
            first = date(y, m + 1, 1)
            self.months.append((first, sum(n for d, n, _ in self.days if (d.year, d.month) == (first.year, first.month))))


def activity(s: Stats) -> Card:
    H = 360
    d0 = 1.3
    busy_day, busy_n, _ = s.busiest
    c = Card("activity", W, H,
             f"{grouped(s.total)} contributions in the last year. Current streak {s.streak} days, longest "
             f"{s.longest}. {s.active} active days. Busiest day {day_month(busy_day)}, with {busy_n}.")
    c.frame()
    first, last = s.months[0][0], s.months[-1][0]
    c.add(c.text("Contributions per month", 40, 50, 15, 500, MUTED, extra=st(c.motion("fade", .5, d0))),
          c.text(f"{first:%b %Y} to {last:%b %Y}", W - 40, 50, 15, 400, MUTED, anchor="end",
                 extra=st(c.motion("fade", .5, d0))),
          c.count_up(s.total, 40, 130, 64, d0 + .2, 1.1, weight=800, tracking=TRACK),
          c.text("contributions in the last year", 40, 160, 15, 400, TEXT_3, extra=st(c.motion("fade", .5, d0 + .2))))

    for j, (value, label, sub) in enumerate((
        (str(s.streak), "Day streak", f"Longest {s.longest}"),
        (str(s.active), "Active days", f"of {len(s.days)}"),
        (grouped(busy_n), "Busiest day", day_month(busy_day)),
    )):
        x = 420 + j * 140
        c.add(f'<g{st(c.motion("lift", .6, d0 + .3 + j * .08))}>'
              + c.text(value, x, 118, 32, 800, tracking=-0.03)
              + c.text(label, x, 142, 14, 500, MUTED) + c.text(sub, x, 160, 14, 400, FAINT) + "</g>")

    peak = max(n for _, n in s.months) or 1
    best = max(range(12), key=lambda j: (s.months[j][1], j))
    floor, tall, slot, bar = 300, 92, (W - 80) / 12, 40
    for j, (month, n) in enumerate(s.months):
        x = 40 + j * slot + (slot - bar) / 2
        grow = d0 + .55 + j * .05
        if n:
            h = max(2, n / peak * tall)
            lit = j == best and n > 0
            fill = ACCENT if lit else LINE
            light = c.motion("lightbar", .4, grow + .6, "ease-in") if lit else ""
            c.add(f'<rect class="fbb" x="{fmt(x)}" y="{fmt(floor - h)}" width="{bar}" height="{fmt(h)}" fill="{fill}"'
                  f'{st(c.motion("grow", .6, grow), light)}/>')
            if lit:
                c.add(f'<g class="fb"{st(c.motion("pop", .45, grow + .75, SPRING))}>'
                      + c.text(grouped(n), x + bar / 2, floor - h - 10, 15, 700, ACCENT, anchor="middle") + "</g>")
        else:
            c.add(f'<rect x="{fmt(x)}" y="{floor - 2}" width="{bar}" height="2" fill="{RULE}"'
                  f'{st(c.motion("fade", .4, grow))}/>')
        c.add(c.text(f"{month:%b}", x + bar / 2, floor + 26, 13, 400, MUTED, anchor="middle",
                     extra=st(c.motion("fade", .4, grow))))
    return c


def repo(s: Stats, name: str, i: int) -> Card | None:
    r = s.repos.get(name.lower())
    if r is None:
        print(f"warning: featured repo {name!r} isn't in data/repos.json; skipping its card", file=sys.stderr)
        return None
    H = 150
    desc = r["description"]
    desc = desc[:1].upper() + desc[1:]
    c = Card(f"repo-{r['name'].lower()}", W, H, f"{r['name']}: {desc or 'repository'}.")
    c.frame()
    d = 2.0 + i * 0.15
    meta = [r["language"], f"Updated {day_month(date.fromisoformat(r['pushed']))}"]
    if r["homepage"]:
        meta.append(re.sub(r"^https?://|/$", "", r["homepage"]))
    if r["stars"]:
        meta.append(f"{r['stars']} star" + ("s" if r["stars"] != 1 else ""))
    if r["archived"]:
        meta.append("Archived")
    pen = FONTS(800).layout(r["name"], 32, -0.03)[1]
    parts = [c.text(r["name"], 40, 64, 32, 800, tracking=-0.03),
             stop_square(c, 40 + pen, 64, 32, d + .45, c.motion("ring1", 1.4, d + .7, "ease-out", "none")),
             arrow(c, W - 51, 34, d + .7)]
    y = 98
    if desc:
        parts.append(c.text(clip(desc, 17, W - 80), 40, y, 17, 400, TEXT_2))
        y += 28
    x = 40.0
    for item in filter(None, meta):
        parts.append(c.text(item, x, y, 14, 400, MUTED))
        x += FONTS(400).width(item, 14) + 28
    c.add(f'<g{st(c.motion("lift", .7, d))}>{"".join(parts)}</g>')
    return c


def graph(s: Stats) -> Card:
    H = 232
    c = Card("graph", W, H, f"Contribution graph: {grouped(s.total)} contributions in the last year.")
    c.frame()
    d0 = 2.6
    cell, pitch = 12, 15
    start = s.days[0][0]
    offset = (start.weekday() + 1) % 7  # Weeks start on Sunday, as on GitHub.
    cols = (len(s.days) + offset + 6) // 7
    gx, gy = 40 + (W - 80 - (cols * pitch - 3)) / 2, 96
    levels = [TILE_BG] + [blend(BG, ACCENT, a) for a in (.3, .52, .76, 1)]

    c.add(c.text("Contribution graph", 40, 50, 15, 500, MUTED, extra=st(c.motion("fade", .5, d0))))
    legend_x = W - 40 - FONTS(400).width("More", 13)
    c.add(c.text("More", legend_x, 50, 13, 400, MUTED, extra=st(c.motion("fade", .5, d0))))
    for k in range(4, -1, -1):
        legend_x -= 6 + 12 if k == 4 else 15
        c.add(f'<rect x="{fmt(legend_x)}" y="40" width="12" height="12" fill="{levels[k]}"'
              f'{st(c.motion("fade", .5, d0))}/>')
    c.add(c.text("Less", legend_x - 6, 50, 13, 400, MUTED, anchor="end", extra=st(c.motion("fade", .5, d0))))

    last_label = -99.0
    for col in range(cols):
        week = [start.toordinal() - offset + col * 7 + r for r in range(7)]
        firsts = [date.fromordinal(o) for o in week if date.fromordinal(o).day == 1 and start.toordinal() <= o]
        x = gx + col * pitch
        if firsts and x - last_label >= 32 and col < cols - 2:
            c.add(c.text(f"{firsts[0]:%b}", x, 84, 13, 400, MUTED, extra=st(c.motion("fade", .5, d0 + col * .012))))
            last_label = x

    if not STATIC:
        c.rule("p", f".p{{animation:pop .45s {SPRING} both}}")
        c.motion("pop", 0, 0)  # registers the keyframes
    cells = []
    for k, (day, n, level) in enumerate(s.days):
        col, row = divmod(k + offset, 7)
        x, y = gx + col * pitch, gy + row * pitch
        delay = "" if STATIC else f' style="animation-delay:{fmt(d0 + .15 + col * .012 + row * .02)}s"'
        cells.append(f'<rect class="fb p" x="{fmt(x)}" y="{fmt(y)}" width="{cell}" height="{cell}" '
                     f'fill="{levels[level]}"{delay}/>')
        if k == len(s.days) - 1:  # Today: a square ring leaves it now and then.
            ring = c.motion("ring", 7.8, d0 + 1.6, "linear", "none", "infinite")
            cells.append(f'<rect class="fb" x="{fmt(x)}" y="{fmt(y)}" width="{cell}" height="{cell}" fill="none" '
                         f'stroke="{ACCENT}" vector-effect="non-scaling-stroke" opacity="0"{st(ring)}/>')
    c.add(*cells)
    return c


def blend(a: str, b: str, t: float) -> str:
    ca, cb = (tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


# --- README -------------------------------------------------------------------

BLOCK = re.compile(r"<!-- cards:start.*?<!-- cards:end -->", re.S)


def block(cards: dict[str, Card], links: dict[str, str]) -> str:
    def img(c: Card, width: str) -> str:
        tag = f'<img src="cards/{c.slug}.svg" width="{width}" alt="{html.escape(c.alt)}">'
        return f'<a href="{html.escape(links[c.slug])}">{tag}</a>' if c.slug in links else tag

    rows = [img(cards["hero"], "100%")]
    rows.append("".join(img(c, f"{TILE_PCT}%") for slug, c in cards.items() if slug.startswith("tile-")))
    rows.append(img(cards["activity"], "100%"))
    rows += [img(c, "100%") for slug, c in cards.items() if slug.startswith("repo-")]
    rows.append(img(cards["graph"], "100%"))
    return ("<!-- cards:start: written by scripts/render_cards.py. Edit the script, not this block. -->\n"
            '<p align="center">\n' + "\n".join(rows) + "\n</p>\n<!-- cards:end -->")


def with_block(text: str, new: str) -> str:
    return BLOCK.sub(lambda _: new, text) if BLOCK.search(text) else new + "\n\n" + text


def preview(readme: str) -> str:
    """A page that lays the cards out as GitHub does, for screenshots."""
    body = re.sub(r'src="(?!https?:|cards/)', 'src="../', readme)
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Profile preview</title>
<style>
:root{{color-scheme:dark;--bg:#0d1117;--fg:#f0f6fc}}
@media (prefers-color-scheme:light){{:root{{color-scheme:light;--bg:#fff;--fg:#1f2328}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}}
main{{box-sizing:border-box;max-width:880px;margin:0 auto;padding:32px 16px}}
p{{margin:0 0 16px}}img{{max-width:100%;box-sizing:content-box}}
</style>
<main>
{body}
</main>
</html>
"""


# --- Main ---------------------------------------------------------------------


def main() -> None:
    global FONTS
    contrib = json.loads((ROOT / "data" / "contributions.json").read_text(encoding="utf-8"))
    repos = json.loads((ROOT / "data" / "repos.json").read_text(encoding="utf-8"))
    s = Stats(contrib, repos)
    FONTS = Fonts()
    try:
        made = [hero(s)] + [tile(t, i, s.repo_count) for i, t in enumerate(TILES)] + [activity(s)]
        made += [c for i, name in enumerate(FEATURED) if (c := repo(s, name, i))]
        made.append(graph(s))
        OUT.mkdir(parents=True, exist_ok=True)
        for c in made:
            (OUT / f"{c.slug}.svg").write_text(c.svg(), encoding="utf-8", newline="\n")
    finally:
        FONTS.close()

    cards = {c.slug: c for c in made}
    for old in OUT.glob("repo-*.svg"):  # A repo dropped from FEATURED loses its card.
        if old.stem not in cards:
            old.unlink()
    links = {f"tile-{t['slug']}": t["href"] for t in TILES}
    links.update({f"repo-{r.lower()}": s.repos[r.lower()]["url"] for r in FEATURED if r.lower() in s.repos})
    readme_path = ROOT / "README.md"
    readme = with_block(readme_path.read_text(encoding="utf-8") if readme_path.exists() else "", block(cards, links))
    if STATIC:
        page = ROOT / "preview" / "index.html"
        page.write_text(preview(readme), encoding="utf-8", newline="\n")
        print(f"Final frame of {len(made)} cards in {OUT.relative_to(ROOT)}; open {page.relative_to(ROOT)}")
    else:
        readme_path.write_text(readme, encoding="utf-8", newline="\n")
        print(f"Wrote {len(made)} cards to {OUT.relative_to(ROOT)} and the README block")


if __name__ == "__main__":
    main()
