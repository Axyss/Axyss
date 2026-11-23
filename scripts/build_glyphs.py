"""Build scripts/glyphs.json: outlines, advances and kerning for the faces readme.py draws with.

Only needed to change the fonts; the daily workflow never runs it.

    pip install fonttools uharfbuzz
    python scripts/build_glyphs.py SpaceGrotesk[wght].ttf JetBrainsMono[wght].ttf scripts/glyphs.json

The variable fonts come from github.com/google/fonts (ofl/spacegrotesk, ofl/jetbrainsmono).
"""
import io, json, sys, unicodedata
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.basePen import BasePen
import uharfbuzz as hb

ASCII = ''.join(chr(c) for c in range(0x20, 0x7f))
LATIN1 = ''.join(chr(c) for c in range(0xa1, 0x100) if unicodedata.category(chr(c))[0] in 'LNPS')
PUNCT = '‘’“”‚„–—…•·→←↗×'
FACES = {
    # name: (font index, weight, charset, kerning)
    'sans':      (0, 500, ASCII + LATIN1 + PUNCT, True),
    'sans-bold': (0, 700, ASCII, True),
    'mono':      (1, 400, ASCII + LATIN1 + PUNCT, False),
    'mono-bold': (1, 700, ASCII, False),
}


class RelPen(BasePen):
    """Relative, integer-rounded SVG path data in y-down font units."""
    def __init__(self, gs):
        super().__init__(gs); self.d = []; self.cur = (0, 0); self.start = (0, 0)
    def _pt(self, p):
        return round(p[0]), -round(p[1])
    def _rel(self, *pts):
        out, (cx, cy) = [], self.cur
        for p in pts:
            x, y = self._pt(p); out.append((x - cx, y - cy))
        return out
    def _fmt(self, cmd, rels):
        s = cmd
        for dx, dy in rels:
            for v in (dx, dy):
                t = str(v)
                s += t if (t.startswith('-') or s[-1].isalpha()) else ' ' + t
        self.d.append(s)
    def _moveTo(self, p):
        self._fmt('m', self._rel(p)); self.cur = self.start = self._pt(p)
    def _lineTo(self, p):
        x, y = self._pt(p); dx, dy = x - self.cur[0], y - self.cur[1]
        if dx == 0 and dy == 0: return
        if dy == 0: self.d.append('h' + str(dx))
        elif dx == 0: self.d.append('v' + str(dy))
        else: self._fmt('l', [(dx, dy)])
        self.cur = (x, y)
    def _qCurveToOne(self, p1, p2):
        self._fmt('q', self._rel(p1, p2)); self.cur = self._pt(p2)
    def _curveToOne(self, p1, p2, p3):
        self._fmt('c', self._rel(p1, p2, p3)); self.cur = self._pt(p3)
    def _closePath(self):
        self.d.append('z'); self.cur = self.start
    def path(self):
        return ''.join(self.d)


def build(path, weight, chars, kern):
    font = instancer.instantiateVariableFont(TTFont(path), {'wght': weight})
    buf = io.BytesIO(); font.save(buf); blob = buf.getvalue()
    font = TTFont(io.BytesIO(blob))
    gs, cmap, hmtx, glyf = font.getGlyphSet(), font.getBestCmap(), font['hmtx'], font['glyf']
    out = {'upm': font['head'].unitsPerEm, 'cap': font['OS/2'].sCapHeight, 'x': font['OS/2'].sxHeight,
           'cmap': {}, 'glyphs': {}, 'kern': {}}

    def add(name):
        if name in out['glyphs']: return
        g = glyf[name]
        if g.isComposite() and not any(hasattr(c, 'transform') for c in g.components):
            # keep plain-offset composites (accented letters) as references: much smaller
            for c in g.components: add(c.glyphName)
            out['glyphs'][name] = [hmtx[name][0], [[c.glyphName, c.x, -c.y] for c in g.components]]
        else:
            pen = RelPen(gs); gs[name].draw(pen)
            out['glyphs'][name] = [hmtx[name][0], pen.path()]

    for ch in chars:
        name = cmap.get(ord(ch))
        if name is None: continue
        out['cmap'][ch] = name; add(name)

    if kern:
        face = hb.Face(blob); hbf = hb.Font(face)
        feats = {'kern': True, 'liga': False, 'clig': False, 'calt': False, 'dlig': False}
        keys = [c for c in ASCII if c in out['cmap']]
        for a in keys:
            row = {}
            for b in keys:
                buf = hb.Buffer(); buf.add_str(a + b); buf.guess_segment_properties()
                hb.shape(hbf, buf, feats)
                k = buf.glyph_positions[0].x_advance - hmtx[out['cmap'][a]][0]
                if k: row[b] = k
            if row: out['kern'][a] = row
    return out


if __name__ == '__main__':
    sans, mono, dest = sys.argv[1:4]
    data = {'_license': 'Glyph outlines from Space Grotesk and JetBrains Mono, '
                        'both under the SIL Open Font License 1.1 (openfontlicense.org).'}
    for name, (idx, w, chars, kern) in FACES.items():
        data[name] = build([sans, mono][idx], w, chars, kern)
        print(name, len(data[name]['cmap']), 'chars', len(data[name]['glyphs']), 'glyphs',
              sum(len(v) for v in data[name]['kern'].values()), 'kern pairs')
    with open(dest, 'w') as f:
        json.dump(data, f, separators=(',', ':'), ensure_ascii=False)
