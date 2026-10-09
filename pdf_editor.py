#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF Редактор — настольный редактор PDF для Windows.
Стек: Python 3.9+, PySide6 (Qt 6), PyMuPDF.

Запуск:   python pdf_editor.py [файл.pdf]
Сборка:   см. build_exe.bat
"""
import difflib
import math
import os
import re
import struct
import sys
import tempfile
import zlib

try:
    import pymupdf as fitz
except ImportError:  # старые версии PyMuPDF
    import fitz

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QIcon, QImage,
                           QKeySequence, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QStyle, QToolBar, QVBoxLayout)

APP_NAME = "PDF Редактор"
MAX_UNDO = 25
THUMB_W = 110
ZOOM_MIN, ZOOM_MAX = 0.1, 6.0
SESSION = os.urandom(3).hex()  # уникальные имена шрифтов в рамках сессии

PDF_FILTER = "PDF (*.pdf)"
INSERT_FILTER = "PDF и изображения (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp)"
IMAGE_FILTER = "Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)"

# Константы редактирования (есть не во всех версиях PyMuPDF)
IMG_NONE = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
IMG_PIXELS = getattr(fitz, "PDF_REDACT_IMAGE_PIXELS", 2)
ART_NONE = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
ART_COVERED = getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1)

# ----------------------------------------------------------------- шрифты --
# Порядок выбора шрифта при правке существующего текста:
#   1) исходный шрифт из самого PDF — пишем коды его глифов напрямую (через карту ToUnicode),
#      шрифт не перевкладывается; работает, если все нужные буквы есть в урезанном (subset) шрифте;
#   2) установленный в системе шрифт с тем же именем (Calibri, Times New Roman, Arial…);
#   3) замена по признакам: Arial / Times New Roman / Courier New.
FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/dejavu",
    "/Library/Fonts",
]
FONT_FILES = {
    ("sans", 0, 0): ["arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    ("sans", 1, 0): ["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    ("sans", 0, 1): ["ariali.ttf", "LiberationSans-Italic.ttf", "DejaVuSans-Oblique.ttf"],
    ("sans", 1, 1): ["arialbi.ttf", "LiberationSans-BoldItalic.ttf", "DejaVuSans-BoldOblique.ttf"],
    ("serif", 0, 0): ["times.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"],
    ("serif", 1, 0): ["timesbd.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"],
    ("serif", 0, 1): ["timesi.ttf", "LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf"],
    ("serif", 1, 1): ["timesbi.ttf", "LiberationSerif-BoldItalic.ttf", "DejaVuSerif-BoldItalic.ttf"],
    ("mono", 0, 0): ["cour.ttf", "LiberationMono-Regular.ttf", "DejaVuSansMono.ttf"],
    ("mono", 1, 0): ["courbd.ttf", "LiberationMono-Bold.ttf", "DejaVuSansMono-Bold.ttf"],
    ("mono", 0, 1): ["couri.ttf", "LiberationMono-Italic.ttf", "DejaVuSansMono-Oblique.ttf"],
    ("mono", 1, 1): ["courbi.ttf", "LiberationMono-BoldItalic.ttf", "DejaVuSansMono-BoldOblique.ttf"],
}
FAMILIES = [("sans", "Arial (без засечек)"),
            ("serif", "Times New Roman (с засечками)"),
            ("mono", "Courier New (моноширинный)")]
BASE14 = {"sans": "helv", "serif": "tiro", "mono": "cour"}
ROT_DIR = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}  # /Rotate -> направление строки
_font_cache = {}


def find_font(family, bold=False, italic=False):
    """Путь к TTF нужного начертания (для замены) или None."""
    key = (family, int(bool(bold)), int(bool(italic)))
    if key in _font_cache:
        return _font_cache[key]
    path = None
    for name in FONT_FILES[key]:
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if d and os.path.isfile(p):
                path = p
                break
        if path:
            break
    if path is None and (bold or italic):
        path = find_font(family)
    _font_cache[key] = path
    return path


def norm_font_name(name):
    """'ABCDEF+TimesNewRomanPS-BoldMT', 'Times New Roman Bold' -> 'timesnewromanbold'."""
    name = (name or "").split("+", 1)[-1]
    name = re.sub(r"[-,]?Identity-[HV]$", "", name)
    parts = re.split(r"[\s,\-_]+", name)
    s = "".join(re.sub(r"PS$", "", re.sub(r"MT$", "", p)) for p in parts).lower()
    for w, r in (("oblique", "italic"), ("regular", ""), ("normal", ""), ("book", "")):
        s = s.replace(w, r)
    return s


def _font_file_names(path):
    """Имена шрифта из таблицы 'name' TTF/OTF/TTC (полное, PostScript, семейство+начертание)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] == b"ttcf":
                f.seek(12)
                f.seek(struct.unpack(">I", f.read(4))[0])
                head = f.read(12)
            ntab = struct.unpack(">H", head[4:6])[0]
            dirs = f.read(16 * ntab)
            for i in range(ntab):
                tag, _, off, ln = struct.unpack(">4sIII", dirs[16 * i:16 * i + 16])
                if tag == b"name":
                    f.seek(off)
                    tbl = f.read(ln)
                    break
            else:
                return []
        _, count, soff = struct.unpack(">HHH", tbl[:6])
        found = {}
        for i in range(count):
            pid, eid, lid, nid, ln, off = struct.unpack(">6H", tbl[6 + 12 * i:18 + 12 * i])
            if nid not in (1, 2, 4, 6):
                continue
            raw = tbl[soff + off:soff + off + ln]
            if pid in (0, 3):
                s, score = raw.decode("utf-16-be", "ignore"), (2 if lid == 0x409 else 1)
            elif pid == 1 and eid == 0:
                s, score = raw.decode("latin-1", "ignore"), 0
            else:
                continue
            if nid not in found or score > found[nid][1]:
                found[nid] = (s, score)
        names = [found[k][0] for k in (4, 6) if k in found]
        if 1 in found:
            names.append(found[1][0] + " " + (found[2][0] if 2 in found else ""))
        return names
    except Exception:
        return []


_sys_index = None


def system_font_path(name):
    """Ищет установленный шрифт по имени из PDF. Индекс строится один раз за запуск."""
    global _sys_index
    if _sys_index is None:
        _sys_index = {}
        for d in FONT_DIRS:
            if not d or not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".ttf", ".otf", ".ttc")):
                    p = os.path.join(d, fn)
                    for n in _font_file_names(p):
                        _sys_index.setdefault(norm_font_name(n), p)
    return _sys_index.get(norm_font_name(name))


# ------------------------------------------------- PDF-уровень: CMap, ширины --
def _utf16(hexstr):
    return bytes.fromhex(hexstr).decode("utf-16-be", "ignore")


def parse_tounicode(data):
    """CMap ToUnicode -> {код глифа (bytes): текст}."""
    text = data.decode("latin-1", "ignore")
    m = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            m[bytes.fromhex(src)] = _utf16(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            lo_i, hi_i, n = int(lo, 16), int(hi, 16), len(lo) // 2
            if hi_i - lo_i > 65535:
                continue
            if rest.startswith("["):
                for i, dst in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    m[(lo_i + i).to_bytes(n, "big")] = _utf16(dst)
            else:
                dst = rest[1:-1]
                base, dn = int(dst, 16), max(1, len(dst) // 2)
                for code in range(lo_i, hi_i + 1):
                    try:
                        m[code.to_bytes(n, "big")] = (base + code - lo_i).to_bytes(dn, "big").decode("utf-16-be", "ignore")
                    except OverflowError:
                        break
    return m


def _numbers(s):
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", s)]


def parse_cid_widths(s):
    """Массив /W CID-шрифта: 'c [w1 w2 …]' или 'c1 c2 w'."""
    toks = re.findall(r"\[|\]|[-+]?\d*\.?\d+", s)
    if toks and toks[0] == "[":
        toks = toks[1:-1]
    out, i = {}, 0
    try:
        while i < len(toks):
            c = int(float(toks[i]))
            i += 1
            if toks[i] == "[":
                i += 1
                while toks[i] != "]":
                    out[c] = float(toks[i])
                    c += 1
                    i += 1
                i += 1
            else:
                c2, w = int(float(toks[i])), float(toks[i + 1])
                i += 2
                for j in range(c, min(c2, c + 65535) + 1):
                    out[j] = w
    except (IndexError, ValueError):
        pass
    return out


def _obj_text(doc, kind, value):
    """Значение ключа; если это ссылка — текст самого объекта."""
    if kind == "xref":
        return doc.xref_object(int(value.split()[0]), compressed=True)
    return value


class TTFInfo:
    """Минимальный разбор TrueType: есть ли у глифа контур (таблица loca) и код -> глиф (cmap).
    Нужен потому, что урезанные шрифты часто сохраняют «пустые» глифы-заглушки."""

    def __init__(self, buf):
        self.ok, self.buf, self.cmaps = False, buf, {}
        try:
            if buf[:4] not in (b"\x00\x01\x00\x00", b"true"):
                return  # CFF/OpenType-CFF — не TrueType
            tables = {}
            for i in range(struct.unpack(">H", buf[4:6])[0]):
                tag, _, off, ln = struct.unpack(">4sIII", buf[12 + 16 * i:28 + 16 * i])
                tables[tag] = off
            fmt = struct.unpack(">h", buf[tables[b"head"] + 50:tables[b"head"] + 52])[0]
            self.n = struct.unpack(">H", buf[tables[b"maxp"] + 4:tables[b"maxp"] + 6])[0]
            lo = tables[b"loca"]
            if fmt == 0:
                self.loca = [2 * x for x in struct.unpack(">%dH" % (self.n + 1), buf[lo:lo + 2 * (self.n + 1)])]
            else:
                self.loca = list(struct.unpack(">%dI" % (self.n + 1), buf[lo:lo + 4 * (self.n + 1)]))
            co = tables.get(b"cmap")
            if co is not None:
                for i in range(struct.unpack(">H", buf[co + 2:co + 4])[0]):
                    pid, eid, off = struct.unpack(">HHI", buf[co + 4 + 8 * i:co + 12 + 8 * i])
                    self.cmaps[(pid, eid)] = co + off
            self.ok = True
        except Exception:
            self.ok = False

    def has_outline(self, gid):
        return 0 < gid < self.n and self.loca[gid + 1] > self.loca[gid]

    def gid_for_code(self, code):
        """Код простого TrueType-шрифта -> номер глифа (символьная (3,0) или Mac (1,0) таблица)."""
        for key, cps in (((3, 0), (0xF000 + code, code)), ((1, 0), (code,)), ((3, 1), (code,))):
            if key in self.cmaps:
                for cp in cps:
                    g = self._lookup(self.cmaps[key], cp)
                    if g:
                        return g
        return 0

    def _lookup(self, off, cp):
        b = self.buf
        try:
            fmt = struct.unpack(">H", b[off:off + 2])[0]
            if fmt == 0:
                return b[off + 6 + cp] if cp < 256 else 0
            if fmt == 6:
                first, cnt = struct.unpack(">HH", b[off + 6:off + 10])
                if first <= cp < first + cnt:
                    return struct.unpack(">H", b[off + 10 + 2 * (cp - first):off + 12 + 2 * (cp - first)])[0]
                return 0
            if fmt == 4:
                segx2 = struct.unpack(">H", b[off + 6:off + 8])[0]
                segs = segx2 // 2
                ends = struct.unpack(">%dH" % segs, b[off + 14:off + 14 + segx2])
                starts = struct.unpack(">%dH" % segs, b[off + 16 + segx2:off + 16 + 2 * segx2])
                deltas = struct.unpack(">%dh" % segs, b[off + 16 + 2 * segx2:off + 16 + 3 * segx2])
                ro = off + 16 + 3 * segx2
                ranges = struct.unpack(">%dH" % segs, b[ro:ro + segx2])
                for i in range(segs):
                    if starts[i] <= cp <= ends[i]:
                        if ranges[i] == 0:
                            return (cp + deltas[i]) & 0xFFFF
                        a = ro + 2 * i + ranges[i] + 2 * (cp - starts[i])
                        g = struct.unpack(">H", b[a:a + 2])[0]
                        return (g + deltas[i]) & 0xFFFF if g else 0
        except (struct.error, IndexError):
            pass
        return 0


class GlyphFont:
    """Шрифт, уже встроенный в PDF. Текст пишется кодами его глифов — выглядит как оригинал."""
    kind = "orig"

    def __init__(self, doc, xref, basefont):
        self.xref = xref
        self.label = basefont.split("+", 1)[-1]
        self.rev, self.widths, self.dw, self.nbytes = {}, {}, 1000.0, 1
        try:
            self.ok = self._load(doc)
        except Exception:
            self.ok = False

    def _load(self, doc):
        x = self.xref
        sub = doc.xref_get_key(x, "Subtype")[1]
        if sub == "/Type0":
            if doc.xref_get_key(x, "Encoding")[1] != "/Identity-H":
                return False
            self.nbytes = 2
            m = re.search(r"(\d+)\s+0\s+R", doc.xref_get_key(x, "DescendantFonts")[1])
            if not m:
                return False
            desc = int(m.group(1))
            k, v = doc.xref_get_key(desc, "DW")
            if k in ("int", "float"):
                self.dw = float(v)
            k, v = doc.xref_get_key(desc, "W")
            if k in ("array", "xref"):
                self.widths = parse_cid_widths(_obj_text(doc, k, v))
        elif sub in ("/TrueType", "/Type1", "/MMType1"):
            k, fc = doc.xref_get_key(x, "FirstChar")
            k2, w = doc.xref_get_key(x, "Widths")
            if k not in ("int", "float") or k2 not in ("array", "xref"):
                return False
            first = int(float(fc))
            self.widths = {first + i: v for i, v in enumerate(_numbers(_obj_text(doc, k2, w)))}
            k, v = doc.xref_get_key(x, "FontDescriptor/MissingWidth")
            self.dw = float(v) if k in ("int", "float") else 0.0
        else:
            return False  # Type3 и экзотика
        k, v = doc.xref_get_key(x, "ToUnicode")
        if k != "xref":
            return False
        for code, s in sorted(parse_tounicode(doc.xref_stream(int(v.split()[0]))).items()):
            if len(code) == self.nbytes and len(s) == 1:
                self.rev.setdefault(s, code)
        self._setup_verify(doc, sub)
        return bool(self.rev)

    def _setup_verify(self, doc, sub):
        """Как проверить, что у буквы в урезанном шрифте реально есть контур."""
        self._checked = {}
        try:
            buf = doc.extract_font(self.xref)[3]
        except Exception:
            buf = b""
        if not buf:  # шрифт не встроен — его рисует просмотрщик полным системным шрифтом
            self._verify = lambda code, ch: True
            return
        tt = TTFInfo(buf)
        if tt.ok:
            if sub == "/Type0":
                gid_map = None
                m = re.search(r"(\d+)\s+0\s+R", doc.xref_get_key(self.xref, "DescendantFonts")[1])
                k, v = doc.xref_get_key(int(m.group(1)), "CIDToGIDMap")
                if k == "xref":
                    gid_map = doc.xref_stream(int(v.split()[0]))

                def cid2gid(cid):
                    if gid_map is None:
                        return cid
                    return int.from_bytes(gid_map[2 * cid:2 * cid + 2], "big") if 2 * cid + 2 <= len(gid_map) else 0
                self._verify = lambda code, ch: tt.has_outline(cid2gid(int.from_bytes(code, "big")))
            else:
                self._verify = lambda code, ch: tt.has_outline(tt.gid_for_code(code[0]))
            return
        try:  # CFF / Type1: FreeType строит таблицу символов по именам глифов
            ft = fitz.Font(fontbuffer=buf)
            self._verify = lambda code, ch: ft.has_glyph(ord(ch)) != 0
        except Exception:
            self._verify = lambda code, ch: False

    def _has(self, ch):
        if ch not in self._checked:
            code = self.rev.get(ch)
            self._checked[ch] = code is not None and bool(self._verify(code, ch))
        return self._checked[ch]

    def missing(self, text):
        return sorted({c for c in text if not c.isspace() and not self._has(c)})

    def _tj(self, text):
        """Текст -> куски для оператора TJ. Нет глифа пробела — пробел делаем сдвигом."""
        parts, cur = [], b""
        for ch in text:
            code = self.rev.get(ch)
            if code is None:
                if ch in " \u00a0":
                    code = self.rev.get(" ")
                    if code is None:
                        if cur:
                            parts.append(cur)
                            cur = b""
                        parts.append(-250.0)
                        continue
                else:
                    return None
            cur += code
        if cur:
            parts.append(cur)
        return parts

    def width(self, text, size):
        w, n = 0.0, self.nbytes
        for p in self._tj(text) or []:
            if isinstance(p, float):
                w -= p
            else:
                w += sum(self.widths.get(int.from_bytes(p[i:i + n], "big"), self.dw) for i in range(0, len(p), n))
        return w / 1000.0 * size

    def draw_line(self, page, batch, pt, text, size, color, d):
        parts = self._tj(text)
        tj = " ".join(("%.2f" % p) if isinstance(p, float) else "<%s>" % p.hex() for p in parts)
        batch.add(self.xref, pt * ~page.transformation_matrix, d, size, color, tj)
        return False  # новый шрифт не вкладывался


class FileFont:
    """TTF-файл: установленный шрифт с тем же именем или замена (Arial/Times/Courier)."""

    def __init__(self, path, kind):
        self.path, self.kind = path, kind
        self.font = fitz.Font(fontfile=path)
        self.label = self.font.name

    def missing(self, text):
        return sorted({c for c in text if c not in "\n" and not self.font.has_glyph(ord(c))})

    def width(self, text, size):
        return self.font.text_length(text, fontsize=size)

    def draw_line(self, page, batch, pt, text, size, color, d):
        name = "S%s%06x" % (SESSION, zlib.crc32(self.path.encode("utf-8")) & 0xFFFFFF)
        page.insert_text(pt, text, fontname=name, fontfile=self.path, fontsize=size,
                         color=color, rotate=rotate_from_dir(d))
        return True


class Base14Font:
    """Крайний случай, если в системе вообще нет подходящих TTF."""
    kind = "generic"

    def __init__(self, family):
        self.fn = BASE14[family]
        self.label = {"helv": "Helvetica", "tiro": "Times", "cour": "Courier"}[self.fn]

    def missing(self, text):
        return []

    def width(self, text, size):
        return fitz.get_text_length(text, fontname=self.fn, fontsize=size, encoding=fitz.TEXT_ENCODING_CYRILLIC)

    def draw_line(self, page, batch, pt, text, size, color, d):
        page.insert_text(pt, text, fontname=self.fn, fontsize=size, color=color,
                         rotate=rotate_from_dir(d), encoding=fitz.TEXT_ENCODING_CYRILLIC)
        return False


_file_fonts = {}


def file_font(path, kind):
    key = (path, kind)
    if key not in _file_fonts:
        _file_fonts[key] = FileFont(path, kind)
    return _file_fonts[key]


def generic_font(family, bold=False, italic=False):
    path = find_font(family, bold, italic)
    return file_font(path, "generic") if path else Base14Font(family)


def draw_text(spec, page, batch, pt, text, size, color, d):
    """Многострочный текст от базовой линии pt в направлении d. True — если вкладывался шрифт."""
    normal = fitz.Point(-d[1], d[0])
    embedded = False
    for i, ln in enumerate(text.split("\n")):
        if ln.strip():
            embedded |= bool(spec.draw_line(page, batch, fitz.Point(pt) + normal * (i * size * 1.2),
                                            ln, size, color, d))
    return embedded


def put_text(page, point, text, size, color, family="sans", bold=False, italic=False, rotate=0):
    """Новый текст (инструмент «Текст»). point — базовая линия в координатах без учёта /Rotate."""
    batch = TextBatch(page)
    draw_text(generic_font(family, bold, italic), page, batch, point, text, size, color, ROT_DIR[rotate % 360])
    batch.flush()


class TextBatch:
    """Копит операторы вывода текста исходными шрифтами и дописывает их одним потоком."""

    def __init__(self, page):
        self.page, self.items = page, []

    def add(self, xref, pdf_pt, d, size, color, tj):
        self.items.append((xref, pdf_pt, d, size, color, tj))

    def flush(self):
        if not self.items:
            return
        page, doc = self.page, self.page.parent
        names = {}
        for it in self.items:
            if it[0] not in names:
                names[it[0]] = ensure_font_resource(doc, page, it[0])
        if not page.is_wrapped:
            page.wrap_contents()  # q…Q вокруг старого содержимого: его трансформации нас не заденут
        out = []
        for xref, p, (dx, dy), size, (r, g, b), tj in self.items:
            out.append("q %.4f %.4f %.4f rg BT /%s %.3f Tf %.5f %.5f %.5f %.5f %.3f %.3f Tm [%s] TJ ET Q"
                       % (r, g, b, names[xref], size, dx, -dy, dy, dx, p.x, p.y, tj))
        append_contents(doc, page, ("\n".join(out) + "\n").encode("latin-1"))
        self.items = []


def ensure_font_resource(doc, page, xref):
    """Имя шрифта в ресурсах страницы; если его там нет (шрифт жил в XObject) — добавляем."""
    for f in page.get_fonts(full=True):
        if f[0] == xref and f[6] == 0:
            return f[4]
    name = "R%s%d" % (SESSION, xref)
    k, v = doc.xref_get_key(page.xref, "Resources")
    if k == "xref":
        target, path = int(v.split()[0]), "Font"
    elif k == "dict":
        target, path = page.xref, "Resources/Font"
    else:
        raise RuntimeError("у страницы унаследованные ресурсы — исходный шрифт недоступен")
    k2, v2 = doc.xref_get_key(target, path)
    if k2 == "xref":
        doc.xref_set_key(int(v2.split()[0]), name, "%d 0 R" % xref)
    else:
        doc.xref_set_key(target, path + "/" + name, "%d 0 R" % xref)
    return name


def append_contents(doc, page, data):
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, data, new=True)
    k, v = doc.xref_get_key(page.xref, "Contents")
    if k == "xref" and not doc.xref_is_stream(int(v.split()[0])):  # ссылка на массив потоков
        k, v = "array", doc.xref_object(int(v.split()[0]), compressed=True)
    if k == "xref":
        new = "[%s %d 0 R]" % (v, x)
    elif k == "array":
        new = v.strip()[:-1] + " %d 0 R]" % x
    else:
        new = "%d 0 R" % x
    doc.xref_set_key(page.xref, "Contents", new)


class FontResolver:
    """Выбор шрифта для фрагмента: исходный → системный с тем же именем → замена."""

    def __init__(self, page):
        self.doc = page.parent
        self.glyph_ok = self.doc.xref_get_key(page.xref, "Resources")[0] != "null"
        self.by_name, self.cache = {}, {}
        for f in page.get_fonts(full=True):
            self.by_name.setdefault(norm_font_name(f[3]), []).append((f[0], f[3]))

    def _glyph_font(self, xref, basefont):
        if xref not in self.cache:
            self.cache[xref] = GlyphFont(self.doc, xref, basefont)
        return self.cache[xref]

    def resolve(self, span, text, style=None):
        """-> (шрифт, пояснение). style=(семейство, жирный, курсив) — явная замена."""
        if style:
            return generic_font(*style), ""
        note = ""
        if self.glyph_ok:
            cands = [self._glyph_font(x, b) for x, b in self.by_name.get(norm_font_name(span["font"]), [])]
            cands = [g for g in cands if g.ok]
            for g in cands:
                if g.missing(text):
                    continue
                if len(cands) > 1 and g.missing(span["text"].replace("\ufffd", "")):
                    continue  # несколько одноимённых подмножеств — берём то, где есть исходные буквы
                return g, ""
            if cands:
                note = "в исходном шрифте нет: " + " ".join(cands[0].missing(text)[:12])
        path = system_font_path(span["font"])
        if path:
            f = file_font(path, "system")
            if not f.missing(text):
                return f, note
        return generic_font(*span_style(span)), note


def map_edit_to_spans(span_texts, new_full):
    """Новая строка -> новые тексты фрагментов. Каждая правка (посимвольный дифф) попадает
    в свой фрагмент, форматирование остальных сохраняется. Правка через границу фрагментов
    сливает только их."""
    old = "".join(span_texts)
    if new_full == old:
        return list(span_texts)
    owner = [i for i, t in enumerate(span_texts) for _ in t]
    out = [""] * len(span_texts)
    sm = difflib.SequenceMatcher(None, old, new_full, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i in range(i1, i2):
                out[owner[i]] += old[i]
        elif i2 > i1:  # замена/удаление: текст уходит в первый затронутый фрагмент
            out[owner[i1]] += new_full[j1:j2]
        else:  # вставка продолжает фрагмент слева
            out[owner[i1 - 1] if i1 > 0 else 0] += new_full[j1:j2]
    return out


def plan_line(resolver, line, new_texts, size=None, color=None, style=None):
    """Раскладка изменённой строки: что удалить и что нарисовать.
    Изменённый фрагмент перерисовывается, хвост строки сдвигается на разницу ширины,
    промежутки между фрагментами сохраняются. -> (области удаления, вывод, пояснения)."""
    spans, d = line["spans"], line["dir"]
    O, dv = fitz.Point(spans[0]["origin"]), fitz.Point(d)

    def proj(p):
        return (p[0] - O.x) * d[0] + (p[1] - O.y) * d[1]

    k = next((i for i, (s, t) in enumerate(zip(spans, new_texts)) if s["text"] != t), None)
    if k is None:
        return [], [], []
    def span_end(s):
        r = fitz.Rect(s["bbox"])
        return max(proj(c) for c in (r.tl, r.tr, r.bl, r.br))

    redacts, draws, notes = [], [], []
    shift = 0.0
    prev_end = span_end(spans[k - 1]) if k > 0 else None
    for s, txt in list(zip(spans, new_texts))[k:]:
        changed = txt != s["text"]
        r = fitz.Rect(s["bbox"])
        start, end = proj(s["origin"]), span_end(s)
        if not changed:
            if abs(shift) < 0.3:
                break  # дальше всё стоит на своих местах
            gap = start - prev_end if prev_end is not None else 0.0
            if gap > 1.5 * s["size"] and shift < gap - 0.3 * s["size"]:
                break  # дальше колонка (таблица/табуляция) и места хватает — не двигаем
        prev_end = end
        redacts.append(shrink_rect(r, d))
        new_start = start + shift
        sz = size or s["size"]
        if txt.strip():
            if "\ufffd" in txt:
                raise ValueError("В строке есть символы, которые не удалось прочитать (�). "
                                 "Переписать её без искажений нельзя.")
            spec, note = resolver.resolve(s, txt, style)
            col = color or int_to_rgb(s["color"])
            pt = fitz.Point(s["origin"]) + dv * (new_start - start)
            draws.append((spec, pt, txt, sz, col, d))
            notes.append((spec, note))
            new_end = new_start + spec.width(txt.split("\n")[-1], sz)
        elif txt and not changed:
            new_end = new_start + (end - start)  # пробельный фрагмент — просто промежуток
        else:
            new_end = new_start + len(txt) * sz * 0.25
        shift = new_end - end
    return redacts, draws, notes


def execute_plan(page, redacts, draws):
    """Удаляет старые глифы и рисует новые. True — если в PDF вкладывался новый шрифт."""
    for r in redacts:
        page.add_redact_annot(r, fill=False)
    if redacts:
        remove_text_only(page)
    batch = TextBatch(page)
    embedded = False
    for spec, pt, txt, size, color, d in draws:
        embedded |= draw_text(spec, page, batch, pt, txt, size, color, d)
    batch.flush()
    return embedded


def describe_fonts(notes):
    """Короткий отчёт: каким шрифтом что написано."""
    kinds = {"orig": "исходный", "system": "системный", "generic": "замена"}
    seen, parts = set(), []
    for spec, note in notes:
        key = (spec.kind, spec.label)
        if key in seen:
            continue
        seen.add(key)
        parts.append("%s «%s»%s" % (kinds[spec.kind], spec.label, (" — " + note) if note else ""))
    return "; ".join(parts)


# ---------------------------------------------------------- вспомогательные --
def int_to_rgb(c):
    return ((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0


def qcolor_rgb(q):
    return (q.redF(), q.greenF(), q.blueF())


def dlg_color(rgb):
    """Цвет после круга через QColor (для сравнения «изменилось ли»)."""
    return qcolor_rgb(QColor.fromRgbF(*rgb))


def span_style(span):
    """Семейство/жирность/курсив по шрифту исходного фрагмента (для замены)."""
    name = span.get("font", "").lower()
    flags = span.get("flags", 0)
    bold = bool(flags & 16) or any(w in name for w in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    if flags & 8 or "courier" in name or "mono" in name:
        family = "mono"
    elif flags & 4 or "times" in name or ("serif" in name and "sans" not in name):
        family = "serif"
    else:
        family = "sans"
    return family, bold, italic


def rotate_from_dir(direction):
    """Направление строки (cos, sin) -> угол для insert_text (0/90/180/270)."""
    ang = math.degrees(math.atan2(-direction[1], direction[0]))
    return int(round(ang / 90.0)) * 90 % 360


def shrink_rect(rect, direction=(1, 0), k=0.2, along=0.3):
    """Область удаления чуть уже фрагмента: поперёк строки на 20%, вдоль — на 0.3 pt,
    чтобы не задеть соседние строки и соседние буквы."""
    r = fitz.Rect(rect)
    if abs(direction[0]) >= abs(direction[1]):
        dy, dx = r.height * k, min(along, r.width * 0.1)
    else:
        dx, dy = r.width * k, min(along, r.height * 0.1)
    return fitz.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy)


def remove_text_only(page):
    """Применяет пометки удаления: убирает только текст, графику и картинки не трогает."""
    try:
        page.apply_redactions(images=IMG_NONE, graphics=ART_NONE)
    except TypeError:
        page.apply_redactions(images=IMG_NONE)


TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_MEDIABOX_CLIP


def text_lines(page):
    """Визуальные строки страницы со всеми фрагментами (span) — у каждого свой шрифт/размер/цвет.
    Куски одной строки на общей базовой линии склеиваются (например, после прошлой правки),
    а далеко разнесённые (колонки таблиц) остаются отдельными строками."""
    raw = []
    for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [sp for sp in ln["spans"] if sp["text"]]
            if spans and "".join(sp["text"] for sp in spans).strip():
                raw.append({"dir": (round(ln["dir"][0], 3), round(ln["dir"][1], 3)), "spans": spans})

    def coords(ln):
        dx, dy = ln["dir"]
        o = ln["spans"][0]["origin"]
        c = -dy * o[0] + dx * o[1]  # поперёк строки (базовая линия)
        a0 = min(dx * sp["origin"][0] + dy * sp["origin"][1] for sp in ln["spans"])
        a1 = max(max(dx * x + dy * y for x in (sp["bbox"][0], sp["bbox"][2]) for y in (sp["bbox"][1], sp["bbox"][3]))
                 for sp in ln["spans"])
        return c, a0, a1

    for ln in raw:
        ln["c"], ln["a0"], ln["a1"] = coords(ln)
    raw.sort(key=lambda ln: (ln["dir"], ln["c"]))
    groups = []  # кластеры по базовой линии
    for ln in raw:
        size = min(sp["size"] for sp in ln["spans"])
        g = groups[-1] if groups else None
        if g and g[-1]["dir"] == ln["dir"] and abs(ln["c"] - g[-1]["c"]) < 0.25 * size:
            g.append(ln)
        else:
            groups.append([ln])
    out = []
    for g in groups:
        g.sort(key=lambda ln: ln["a0"])
        cur = None
        for ln in g:
            size = min(sp["size"] for sp in ln["spans"])
            if cur is not None and ln["a0"] - cur["a1"] < 1.5 * size:
                cur["spans"] += ln["spans"]
                cur["a1"] = max(cur["a1"], ln["a1"])
            else:
                cur = dict(ln)
                cur["spans"] = list(ln["spans"])
                out.append(cur)
    for ln in out:
        dx, dy = ln["dir"]
        ln["spans"].sort(key=lambda sp: dx * sp["origin"][0] + dy * sp["origin"][1])
        ln["bbox"] = fitz.Rect()
        for sp in ln["spans"]:
            ln["bbox"] |= fitz.Rect(sp["bbox"])
        ln["raw"] = "".join(sp["text"] for sp in ln["spans"])
    return out


# ------------------------------------------------------------------ диалоги --
class ColorButton(QPushButton):
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.setFixedWidth(46)
        self.set_color(QColor(color))
        self.clicked.connect(self.pick)

    def set_color(self, c):
        self.color = QColor(c)
        self.setStyleSheet("background:%s; border:1px solid #666; min-height:20px;" % self.color.name())

    def pick(self):
        c = QColorDialog.getColor(self.color, self, "Цвет")
        if c.isValid():
            self.set_color(c)


class TextDialog(QDialog):
    def __init__(self, parent, title, text="", size=12.0, color=(0, 0, 0),
                 family="sans", bold=False, italic=False, hint="", original=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 280)
        lay = QVBoxLayout(self)
        if hint:
            lab = QLabel(hint)
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#666;")
            lay.addWidget(lab)
        self.edit = QPlainTextEdit(text)
        lay.addWidget(self.edit)
        form = QFormLayout()
        self.family = QComboBox()
        if original:
            self.family.addItem("Как в оригинале (%s)" % original.split("+", 1)[-1], "orig")
        for key, label in FAMILIES:
            self.family.addItem(label, key)
        self.family.setCurrentIndex(max(0, self.family.findData(family)))
        form.addRow("Шрифт:", self.family)
        row = QHBoxLayout()
        self.size = QDoubleSpinBox()
        self.size.setRange(2, 300)
        self.size.setSingleStep(0.5)
        self.size.setValue(round(size, 1))
        self.bold = QCheckBox("Жирный")
        self.bold.setChecked(bold)
        self.italic = QCheckBox("Курсив")
        self.italic.setChecked(italic)
        self.color = ColorButton(QColor.fromRgbF(*color))
        for w in (self.size, self.bold, self.italic, QLabel("Цвет:"), self.color):
            row.addWidget(w)
        row.addStretch()
        form.addRow("Размер:", row)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.family.currentIndexChanged.connect(self._family_changed)
        self._family_changed()
        self.edit.setFocus()
        self.edit.selectAll()

    def _family_changed(self):
        orig = self.family.currentData() == "orig"  # начертание берётся из оригинала
        self.bold.setEnabled(not orig)
        self.italic.setEnabled(not orig)

    def values(self):
        return dict(text=self.edit.toPlainText().rstrip("\n"), size=round(self.size.value(), 1),
                    color=qcolor_rgb(self.color.color), family=self.family.currentData(),
                    bold=self.bold.isChecked(), italic=self.italic.isChecked())


class ReplaceDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Найти и заменить текст")
        form = QFormLayout(self)
        self.find = QLineEdit()
        self.repl = QLineEdit()
        self.case = QCheckBox("С учётом регистра")
        self.all_pages = QCheckBox("На всех страницах")
        self.all_pages.setChecked(True)
        form.addRow("Найти:", self.find)
        form.addRow("Заменить на:", self.repl)
        form.addRow("", self.case)
        form.addRow("", self.all_pages)
        note = QLabel("Пишется исходным шрифтом документа, если в нём есть нужные буквы.\n"
                      "Остаток строки сдвигается. Пустая замена — удалить найденное.")
        note.setStyleSheet("color:#666;")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


# ----------------------------------------------------------- миниатюры ------
class ThumbList(QListWidget):
    orderChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.ListMode)
        self.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.42)))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSpacing(3)
        self.setUniformItemSizes(True)

    def dropEvent(self, e):
        super().dropEvent(e)
        self.orderChanged.emit()

    def selected_rows(self):
        return sorted(self.row(i) for i in self.selectedItems())


# ------------------------------------------------------------ просмотр -----
class PageView(QGraphicsView):
    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        self.setScene(QGraphicsScene(self))
        self.pix_item = QGraphicsPixmapItem()
        self.pix_item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.pix_item)
        self.setBackgroundBrush(QColor(96, 96, 100))
        self.setAlignment(Qt.AlignCenter)
        self.temp = None
        self.start = None
        self.points = []

    def set_page_pixmap(self, pm, w, h):
        self.pix_item.setPixmap(pm)
        self.scene().setSceneRect(QRectF(0, 0, w, h))

    def _pdf_point(self, e):
        sp = self.mapToScene(e.position().toPoint())
        z = self.ed.zoom
        return fitz.Point(sp.x() / z, sp.y() / z), sp

    def mousePressEvent(self, e):
        tool = self.ed.tool
        if not self.ed.doc or tool == "hand" or e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        p, sp = self._pdf_point(e)
        if tool in ("text", "edit"):
            self.ed.on_click(tool, p)
            return
        self.start = sp
        if tool == "pen":
            self.points = [p]
            pen = QPen(self.ed.draw_color, max(1.0, self.ed.pen_width * self.ed.zoom))
            pen.setCapStyle(Qt.RoundCap)
            self.temp = self.scene().addPath(QPainterPath(sp), pen)
        else:
            colors = {"highlight": QColor(255, 230, 0, 90), "erase": QColor(255, 255, 255, 170),
                      "image": QColor(80, 140, 255, 50), "rect": QColor(0, 0, 0, 0)}
            pen = QPen(self.ed.draw_color if tool == "rect" else QColor(30, 30, 30), 1, Qt.DashLine)
            self.temp = self.scene().addRect(QRectF(sp, sp), pen, QBrush(colors.get(tool)))

    def mouseMoveEvent(self, e):
        if self.temp is None:
            return super().mouseMoveEvent(e)
        p, sp = self._pdf_point(e)
        if self.ed.tool == "pen":
            path = self.temp.path()
            path.lineTo(sp)
            self.temp.setPath(path)
            self.points.append(p)
        else:
            self.temp.setRect(QRectF(self.start, sp).normalized())

    def mouseReleaseEvent(self, e):
        if self.temp is None:
            return super().mouseReleaseEvent(e)
        p, sp = self._pdf_point(e)
        self.scene().removeItem(self.temp)
        self.temp = None
        z = self.ed.zoom
        if self.ed.tool == "pen":
            self.points.append(p)
            pts, self.points = self.points, []
            self.ed.on_drag("pen", pts)
        else:
            r = fitz.Rect(self.start.x() / z, self.start.y() / z, sp.x() / z, sp.y() / z)
            r.normalize()
            self.ed.on_drag(self.ed.tool, r)

    def contextMenuEvent(self, e):
        if not self.ed.doc:
            return
        sp = self.mapToScene(e.pos())
        self.ed.annot_menu(fitz.Point(sp.x() / self.ed.zoom, sp.y() / self.ed.zoom), e.globalPos())

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.ed.zoom_by(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
            return
        vb = self.verticalScrollBar()
        dy = e.angleDelta().y()
        if dy < 0 and vb.value() >= vb.maximum() and self.ed.go_page(self.ed.cur + 1):
            QTimer.singleShot(0, lambda: vb.setValue(vb.minimum()))
            return
        if dy > 0 and vb.value() <= vb.minimum() and self.ed.go_page(self.ed.cur - 1):
            QTimer.singleShot(0, lambda: vb.setValue(vb.maximum()))
            return
        super().wheelEvent(e)


# ------------------------------------------------------------ главное окно --
TOOLS = [
    ("hand", "Рука", "H", "Прокрутка страницы мышью. Правый клик по аннотации — удалить её."),
    ("text", "Текст", "T", "Щёлкните там, где должен начинаться новый текст."),
    ("edit", "Изменить текст", "E", "Щёлкните по строке существующего текста, чтобы изменить или удалить её."),
    ("highlight", "Маркер", "M", "Выделите текст или область — она будет подсвечена."),
    ("rect", "Рамка", "R", "Нарисуйте прямоугольную рамку."),
    ("pen", "Карандаш", "P", "Рисуйте от руки."),
    ("erase", "Стереть область", "D", "Выделите область: всё содержимое под ней будет удалено и закрашено белым."),
    ("image", "Картинка", "I", "Выделите область, куда вставить изображение (пропорции сохраняются)."),
]


class Editor(QMainWindow):
    def __init__(self, path=None):
        super().__init__()
        self.doc = None
        self.path = None
        self.password = None
        self.cur = 0
        self.zoom = 1.25
        self.tool = "hand"
        self.undo_stack, self.redo_stack = [], []
        self.dirty = False
        self.text_added = False
        self.draw_color = QColor(220, 30, 30)
        self.pen_width = 2.0
        self.last_text = dict(size=12.0, color=(0, 0, 0), family="sans", bold=False, italic=False)
        self._thumb_gen = 0
        self._thumb_queue = []

        self.view = PageView(self)
        self.thumbs = ThumbList()
        self.thumbs.currentRowChanged.connect(self._thumb_row_changed)
        self.thumbs.orderChanged.connect(self._thumb_reordered)
        self.thumbs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.thumbs.customContextMenuRequested.connect(self._thumb_menu)

        split = QSplitter()
        split.addWidget(self.thumbs)
        split.addWidget(self.view)
        split.setStretchFactor(1, 1)
        split.setSizes([THUMB_W + 90, 1000])
        self.setCentralWidget(split)

        self.page_label = QLabel()
        self.zoom_label = QLabel()
        self.hint_label = QLabel()
        sb = self.statusBar()
        sb.addWidget(self.hint_label, 1)
        sb.addPermanentWidget(self.page_label)
        sb.addPermanentWidget(self.zoom_label)

        self._build_actions()
        self.setAcceptDrops(True)
        self.resize(1300, 900)
        self.set_tool("hand")
        if path:
            self.open_path(path)
        self.update_ui()

    # ------------------------------------------------------------ actions --
    def _act(self, text, slot, shortcut=None, icon=None, tip=None):
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        S = QStyle.StandardPixmap
        A = self._act
        self.a_open = A("Открыть…", self.open_dialog, QKeySequence.Open, S.SP_DialogOpenButton)
        self.a_save = A("Сохранить", self.save, QKeySequence.Save, S.SP_DialogSaveButton)
        self.a_save_as = A("Сохранить как…", self.save_as, "Ctrl+Shift+S")
        self.a_exit = A("Выход", self.close, "Ctrl+Q")
        self.a_undo = A("Отменить", self.undo, QKeySequence.Undo, S.SP_ArrowBack)
        self.a_redo = A("Повторить", self.redo, "Ctrl+Y", S.SP_ArrowForward)
        self.a_replace = A("Найти и заменить…", self.find_replace, "Ctrl+H")
        self.a_rot_l = A("Повернуть влево", lambda: self.rotate_pages(-90), "Ctrl+L")
        self.a_rot_r = A("Повернуть вправо", lambda: self.rotate_pages(90), "Ctrl+R")
        self.a_del = A("Удалить страницы", self.delete_pages, "Ctrl+Del", S.SP_TrashIcon)
        self.a_blank = A("Вставить пустую страницу", self.insert_blank)
        self.a_insert = A("Вставить из файла (PDF/картинки)…", self.insert_file, "Ctrl+I")
        self.a_extract = A("Сохранить выбранные страницы как PDF…", self.extract_pages)
        self.a_up = A("Переместить выше", lambda: self.move_page(-1), "Ctrl+Up")
        self.a_down = A("Переместить ниже", lambda: self.move_page(1), "Ctrl+Down")
        self.a_zoom_in = A("Увеличить", lambda: self.zoom_by(1.25), QKeySequence.ZoomIn)
        self.a_zoom_out = A("Уменьшить", lambda: self.zoom_by(0.8), QKeySequence.ZoomOut)
        self.a_fit_w = A("По ширине", self.fit_width, "Ctrl+1")
        self.a_fit_p = A("Страница целиком", self.fit_page, "Ctrl+0")
        self.a_prev = A("Предыдущая страница", lambda: self.go_page(self.cur - 1), "PgUp", S.SP_ArrowUp)
        self.a_next = A("Следующая страница", lambda: self.go_page(self.cur + 1), "PgDown", S.SP_ArrowDown)
        self.a_first = A("Первая страница", lambda: self.go_page(0), "Ctrl+Home")
        self.a_last = A("Последняя страница", lambda: self.go_page(len(self.doc) - 1 if self.doc else 0), "Ctrl+End")
        self.doc_actions = [self.a_save, self.a_save_as, self.a_replace, self.a_rot_l, self.a_rot_r,
                            self.a_del, self.a_blank, self.a_insert, self.a_extract, self.a_up,
                            self.a_down, self.a_zoom_in, self.a_zoom_out, self.a_fit_w, self.a_fit_p,
                            self.a_prev, self.a_next, self.a_first, self.a_last]

        mb = self.menuBar()
        m = mb.addMenu("Файл")
        m.addActions([self.a_open, self.a_save, self.a_save_as])
        m.addSeparator()
        m.addAction(self.a_exit)
        m = mb.addMenu("Правка")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addAction(self.a_replace)
        self.page_menu = mb.addMenu("Страницы")
        self.page_menu.addActions([self.a_rot_l, self.a_rot_r, self.a_up, self.a_down])
        self.page_menu.addSeparator()
        self.page_menu.addActions([self.a_blank, self.a_insert, self.a_extract])
        self.page_menu.addSeparator()
        self.page_menu.addAction(self.a_del)
        m = mb.addMenu("Вид")
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_fit_w, self.a_fit_p])
        m.addSeparator()
        m.addActions([self.a_prev, self.a_next, self.a_first, self.a_last])
        self.tools_menu = mb.addMenu("Инструменты")

        tb = QToolBar("Основное")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addActions([self.a_open, self.a_save])
        tb.addSeparator()
        tb.addActions([self.a_undo, self.a_redo])
        tb.addSeparator()
        tb.addActions([self.a_prev, self.a_next])
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.go_page(v - 1))
        tb.addWidget(self.page_spin)
        tb.addSeparator()
        for a in (self.a_zoom_out, self.a_zoom_in, self.a_fit_w):
            tb.addAction(a)
        tb.addSeparator()
        tb.addActions([self.a_rot_l, self.a_rot_r, self.a_del])
        # короткие подписи на панели, полные — в меню и подсказках
        for a, short in ((self.a_zoom_out, "−"), (self.a_zoom_in, "+"), (self.a_rot_l, "↺ 90°"),
                         (self.a_rot_r, "↻ 90°"), (self.a_del, "Удалить стр.")):
            a.setIconText(short)
        for a in (self.a_prev, self.a_next):
            tb.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(tb)
        self.addToolBarBreak()

        ttb = QToolBar("Инструменты")
        ttb.setObjectName("tools")
        group = QActionGroup(self)
        self.tool_actions = {}
        for key, label, sc, tip in TOOLS:
            a = QAction(label, self, checkable=True)
            a.setShortcut(QKeySequence(sc))
            a.setToolTip("%s (%s)\n%s" % (label, sc, tip))
            a.triggered.connect(lambda _=False, k=key: self.set_tool(k))
            group.addAction(a)
            ttb.addAction(a)
            self.tools_menu.addAction(a)
            self.tool_actions[key] = a
        ttb.addSeparator()
        ttb.addWidget(QLabel(" Цвет: "))
        self.color_btn = ColorButton(self.draw_color)
        self.color_btn.clicked.connect(lambda: setattr(self, "draw_color", self.color_btn.color))
        ttb.addWidget(self.color_btn)
        ttb.addWidget(QLabel("  Толщина: "))
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 20)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(self.pen_width)
        self.width_spin.valueChanged.connect(lambda v: setattr(self, "pen_width", v))
        ttb.addWidget(self.width_spin)
        self.addToolBar(ttb)

    def set_tool(self, key):
        self.tool = key
        self.tool_actions[key].setChecked(True)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag if key == "hand" else QGraphicsView.NoDrag)
        cur = {"hand": Qt.OpenHandCursor, "text": Qt.IBeamCursor, "edit": Qt.IBeamCursor}.get(key, Qt.CrossCursor)
        self.view.viewport().setCursor(cur)
        self.hint_label.setText(next(t[3] for t in TOOLS if t[0] == key))

    # ------------------------------------------------------------- файлы --
    def _load_bytes(self, data):
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass and self.password:
            doc.authenticate(self.password)
        return doc

    def maybe_save(self):
        if not self.dirty:
            return True
        r = QMessageBox.question(self, APP_NAME, "Сохранить изменения в документе?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def open_dialog(self):
        if not self.maybe_save():
            return
        start = os.path.dirname(self.path) if self.path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PDF", start, PDF_FILTER + ";;Все файлы (*)")
        if path:
            self.open_path(path)

    def open_path(self, path):
        try:
            with open(path, "rb") as f:  # читаем в память: файл не блокируется и его можно перезаписать
                data = f.read()
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as ex:
            QMessageBox.critical(self, APP_NAME, "Не удалось открыть файл:\n%s" % ex)
            return False
        password = None
        if doc.needs_pass:
            while True:
                pw, ok = QInputDialog.getText(self, "Пароль", "Документ защищён паролем:", QLineEdit.Password)
                if not ok:
                    return False
                if doc.authenticate(pw):
                    password = pw
                    break
        global SESSION  # новые имена шрифтов: не путаться с уже усечёнными (subset) в файле
        SESSION = os.urandom(3).hex()
        self.doc, self.path, self.password = doc, os.path.abspath(path), password
        self.cur = 0
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = self.text_added = False
        self.fit_width(render=False)
        self.refresh(full=True)
        return True

    def save(self):
        if not self.doc:
            return False
        if not self.path:
            return self.save_as()
        return self._write(self.path)

    def save_as(self):
        if not self.doc:
            return False
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить как", self.path or "document.pdf", PDF_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if self._write(path):
            self.path = os.path.abspath(path)
            self.update_ui()
            return True
        return False

    def _write(self, path):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        tmp = None
        try:
            out = self.doc
            if self.text_added:
                # Сабсет шрифтов делаем на копии: целый Arial весит ~1 МБ, подмножество — десятки КБ
                try:
                    out = self._load_bytes(self.doc.tobytes())
                    out.subset_fonts()
                except Exception:
                    out = self.doc
            fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(os.path.abspath(path)))
            os.close(fd)
            out.save(tmp, garbage=3, deflate=True)
            os.replace(tmp, path)  # атомарная замена: исходник не испортится при сбое
            tmp = None
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, "Не удалось сохранить:\n%s" % ex)
            return False
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        QApplication.restoreOverrideCursor()
        self.dirty = False
        self.statusBar().showMessage("Сохранено: %s" % path, 4000)
        self.update_ui()
        return True

    def closeEvent(self, e):
        if self.maybe_save():
            e.accept()
        else:
            e.ignore()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        pdfs = [u for u in urls if u.lower().endswith(".pdf")]
        if pdfs and self.maybe_save():
            self.open_path(pdfs[0])

    # ----------------------------------------------------- изменения/undo --
    def apply(self, fn, structural=False):
        """Выполняет изменение документа с точкой отката. При ошибке документ восстанавливается."""
        if not self.doc:
            return False
        snap = self.doc.tobytes()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            fn()
        except Exception as ex:
            self.doc = self._load_bytes(snap)
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, "Операция не выполнена:\n%s" % ex)
            self.refresh(full=True)
            return False
        QApplication.restoreOverrideCursor()
        self.undo_stack.append(snap)
        del self.undo_stack[:-MAX_UNDO]
        self.redo_stack.clear()
        self.dirty = True
        self.refresh(full=structural)
        return True

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.doc.tobytes())
            self.doc = self._load_bytes(self.undo_stack.pop())
            self.dirty = True
            self.refresh(full=True)

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.doc.tobytes())
            self.doc = self._load_bytes(self.redo_stack.pop())
            self.dirty = True
            self.refresh(full=True)

    # ----------------------------------------------------------- отрисовка --
    def refresh(self, full=False):
        if self.doc:
            self.cur = max(0, min(self.cur, len(self.doc) - 1))
            if full:
                self.rebuild_thumbs()
            else:
                self.render_thumb(self.cur)
            self.render_page()
        else:
            self.thumbs.clear()
            self.view.set_page_pixmap(QPixmap(), 0, 0)
        self.update_ui()

    def render_page(self):
        page = self.doc[self.cur]
        dpr = self.view.devicePixelRatioF()
        m = self.zoom * dpr
        pix = page.get_pixmap(matrix=fitz.Matrix(m, m), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        self.view.set_page_pixmap(pm, page.rect.width * self.zoom, page.rect.height * self.zoom)

    def rebuild_thumbs(self):
        self._thumb_gen += 1
        n = len(self.doc)
        ph = QPixmap(THUMB_W, int(THUMB_W * 1.3))
        ph.fill(QColor(235, 235, 235))
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for i in range(n):
            it = QListWidgetItem(QIcon(ph), str(i + 1))
            it.setData(Qt.UserRole, i)
            self.thumbs.addItem(it)
        self.thumbs.setCurrentRow(self.cur)
        self.thumbs.blockSignals(False)
        self._thumb_queue = sorted(range(n), key=lambda i: abs(i - self.cur))
        gen = self._thumb_gen
        QTimer.singleShot(0, lambda: self._thumb_tick(gen))

    def _thumb_tick(self, gen):
        if gen != self._thumb_gen or not self.doc:
            return
        for _ in range(6):
            if not self._thumb_queue:
                return
            self.render_thumb(self._thumb_queue.pop(0))
        QTimer.singleShot(0, lambda: self._thumb_tick(gen))

    def render_thumb(self, i):
        if not self.doc or i >= len(self.doc) or i >= self.thumbs.count():
            return
        page = self.doc[i]
        s = min(THUMB_W / page.rect.width, THUMB_W * 1.42 / page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        self.thumbs.item(i).setIcon(QIcon(QPixmap.fromImage(img)))

    def update_ui(self):
        has = self.doc is not None
        for a in self.doc_actions + list(self.tool_actions.values()):
            a.setEnabled(has)
        self.a_undo.setEnabled(bool(self.undo_stack))
        self.a_redo.setEnabled(bool(self.redo_stack))
        name = os.path.basename(self.path) if self.path else "без имени"
        self.setWindowTitle(("%s%s — %s" % (name, " *" if self.dirty else "", APP_NAME)) if has else APP_NAME)
        n = len(self.doc) if has else 0
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.setValue(self.cur + 1)
        self.page_spin.setSuffix(" / %d" % n)
        self.page_spin.blockSignals(False)
        self.page_label.setText(" Стр. %d из %d " % (self.cur + 1, n) if has else "")
        self.zoom_label.setText(" %d%% " % round(self.zoom * 100))

    # ---------------------------------------------------------- навигация --
    def go_page(self, i):
        if not self.doc or not (0 <= i < len(self.doc)) or i == self.cur:
            return False
        self.cur = i
        self.thumbs.blockSignals(True)
        self.thumbs.setCurrentRow(i)
        self.thumbs.scrollToItem(self.thumbs.item(i))
        self.thumbs.blockSignals(False)
        self.render_page()
        self.update_ui()
        return True

    def _thumb_row_changed(self, row):
        if row >= 0:
            self.go_page(row)

    def zoom_by(self, k):
        if self.doc:
            self.set_zoom(self.zoom * k)

    def set_zoom(self, z, render=True):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if render and self.doc:
            self.render_page()
        self.update_ui()

    def fit_width(self, render=True):
        if self.doc:
            w = self.view.viewport().width() - 30
            self.set_zoom(w / self.doc[self.cur].rect.width, render)

    def fit_page(self):
        if self.doc:
            r = self.doc[self.cur].rect
            vp = self.view.viewport()
            self.set_zoom(min((vp.width() - 30) / r.width, (vp.height() - 30) / r.height))

    # ------------------------------------------------------------ страницы --
    def selected_pages(self):
        rows = self.thumbs.selected_rows()
        return rows if rows else [self.cur]

    def rotate_pages(self, delta):
        pages = self.selected_pages()

        def fn():
            for i in pages:
                p = self.doc[i]
                p.set_rotation((p.rotation + delta) % 360)
        self.apply(fn, structural=True)
        self._reselect(pages)

    def delete_pages(self):
        pages = self.selected_pages()
        if len(pages) >= len(self.doc):
            QMessageBox.information(self, APP_NAME, "Нельзя удалить все страницы документа.")
            return
        if QMessageBox.question(self, APP_NAME, "Удалить страниц: %d?" % len(pages)) != QMessageBox.Yes:
            return

        def fn():
            self.doc.delete_pages(pages)
            self.cur = min(pages[0], len(self.doc) - 1)
        self.apply(fn, structural=True)

    def insert_blank(self):
        r = self.doc[self.cur].rect

        def fn():
            self.doc.new_page(pno=self.cur + 1, width=r.width, height=r.height)
            self.cur += 1
        self.apply(fn, structural=True)

    def insert_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Вставить после текущей страницы", "", INSERT_FILTER)
        if not paths:
            return
        sources = []
        for path in paths:
            try:
                src = fitz.open(path)
                if src.needs_pass:
                    pw, ok = QInputDialog.getText(self, "Пароль", "Пароль для %s:" % os.path.basename(path),
                                                  QLineEdit.Password)
                    if not ok or not src.authenticate(pw):
                        continue
                if not src.is_pdf:  # картинка -> одностраничный PDF
                    src = fitz.open("pdf", src.convert_to_pdf())
                sources.append(src)
            except Exception as ex:
                QMessageBox.warning(self, APP_NAME, "Пропущен %s:\n%s" % (os.path.basename(path), ex))
        if not sources:
            return

        def fn():
            pos = self.cur + 1
            for src in sources:
                self.doc.insert_pdf(src, start_at=pos)
                pos += len(src)
            self.cur += 1
        self.apply(fn, structural=True)

    def extract_pages(self):
        pages = self.selected_pages()
        base = os.path.splitext(os.path.basename(self.path or "document.pdf"))[0]
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить %d стр. как" % len(pages),
                                              "%s_страницы.pdf" % base, PDF_FILTER)
        if not path:
            return
        try:
            new = fitz.open()
            for i in pages:
                new.insert_pdf(self.doc, from_page=i, to_page=i)
            new.save(path, garbage=3, deflate=True)
            self.statusBar().showMessage("Сохранено страниц: %d → %s" % (len(pages), path), 5000)
        except Exception as ex:
            QMessageBox.critical(self, APP_NAME, "Не удалось сохранить:\n%s" % ex)

    def move_page(self, step):
        i, j = self.cur, self.cur + step
        if not (0 <= j < len(self.doc)):
            return
        order = list(range(len(self.doc)))
        order[i], order[j] = order[j], order[i]

        def fn():
            self.doc.select(order)
            self.cur = j
        self.apply(fn, structural=True)

    def _thumb_reordered(self):
        order = [self.thumbs.item(r).data(Qt.UserRole) for r in range(self.thumbs.count())]
        if order == list(range(len(self.doc))):
            return
        new_cur = order.index(self.cur) if self.cur in order else 0

        def fn():
            self.doc.select(order)
            self.cur = new_cur
        self.apply(fn, structural=True)

    def _reselect(self, rows):
        if len(rows) > 1:
            for r in rows:
                if r < self.thumbs.count():
                    self.thumbs.item(r).setSelected(True)

    def _thumb_menu(self, pos):
        if not self.doc:
            return
        self.page_menu.exec(self.thumbs.mapToGlobal(pos))

    # ------------------------------------------------------------ инструменты --
    def _page(self):
        return self.doc[self.cur]

    def on_click(self, tool, disp_pt):
        page = self._page()
        if tool == "text":
            dlg = TextDialog(self, "Новый текст", **self.last_text)
            if dlg.exec() != QDialog.Accepted:
                return
            v = dlg.values()
            self.last_text = {k: v[k] for k in ("size", "color", "family", "bold", "italic")}
            if not v["text"].strip():
                return
            # верх текста — там, где щёлкнули; базовая линия ниже на ~0.85 кегля
            base = fitz.Point(disp_pt.x, disp_pt.y + v["size"] * 0.85) * page.derotation_matrix

            def fn():
                put_text(page, base, v["text"], v["size"], v["color"], v["family"],
                         v["bold"], v["italic"], rotate=page.rotation)
                self.text_added = True
            self.apply(fn)
        elif tool == "edit":
            self.edit_line_at(disp_pt * page.derotation_matrix)

    def edit_line_at(self, pt):
        page = self._page()
        hits = [ln for ln in text_lines(page) if ln["bbox"].contains(pt)]
        line = min(hits, key=lambda ln: abs(pt - (ln["bbox"].tl + ln["bbox"].br) / 2)) if hits else None
        if line is None:
            self.statusBar().showMessage(
                "Под курсором нет текста. Если это скан (картинка), текст изменить нельзя — "
                "используйте «Стереть область» + «Текст».", 6000)
            return
        spans, raw = line["spans"], line["raw"]
        i0 = next(i for i, sp in enumerate(spans) if sp["text"].strip())
        s0 = spans[i0]
        family, bold, italic = span_style(s0)
        lead, trail = raw[:len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
        hint = ("Если не менять шрифт, размер и цвет — переписан будет только изменённый фрагмент, "
                "остальное форматирование строки сохранится. Пустое поле — удалить строку.")
        if "\ufffd" in raw:
            hint += "\n⚠ В строке есть нечитаемые символы (�) — они не сохранятся."
        dlg = TextDialog(self, "Изменить строку", raw.strip(), s0["size"], int_to_rgb(s0["color"]),
                         "orig", bold, italic, hint=hint, original=s0["font"])
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        same_look = (v["family"] == "orig" and v["size"] == round(s0["size"], 1)
                     and v["color"] == dlg_color(int_to_rgb(s0["color"])))
        if same_look and v["text"] == raw.strip():
            return  # ничего не поменяли
        resolver = FontResolver(page)
        n = len(spans)
        try:
            if not v["text"].strip():
                plan = plan_line(resolver, line, [""] * n)
            elif same_look and "\n" not in v["text"]:
                new_texts = map_edit_to_spans([sp["text"] for sp in spans], lead + v["text"] + trail)
                plan = plan_line(resolver, line, new_texts)
            else:  # другой стиль или несколько строк — вся строка одним стилем
                new_texts = [""] * n
                new_texts[i0] = v["text"]
                style = None if v["family"] == "orig" else (v["family"], v["bold"], v["italic"])
                plan = plan_line(resolver, line, new_texts, size=v["size"], color=v["color"], style=style)
        except ValueError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))
            return

        def fn():
            if execute_plan(page, plan[0], plan[1]):
                self.text_added = True
        if self.apply(fn) and plan[2]:
            self.statusBar().showMessage("Шрифт: " + describe_fonts(plan[2]), 10000)

    def on_drag(self, tool, data):
        page = self._page()
        dm = page.derotation_matrix
        if tool == "pen":
            pts = [tuple(p * dm) for p in data]
            if len(pts) < 2:
                return
            color, width = qcolor_rgb(self.draw_color), self.pen_width

            def fn():
                a = page.add_ink_annot([pts])
                a.set_colors(stroke=color)
                a.set_border(width=width)
                a.update()
            self.apply(fn)
            return

        disp_rect = data
        tiny = disp_rect.width < 4 or disp_rect.height < 4
        if tool == "image":
            if tiny:
                disp_rect = fitz.Rect(disp_rect.x0, disp_rect.y0, disp_rect.x0 + 200, disp_rect.y0 + 150)
            path, _ = QFileDialog.getOpenFileName(self, "Выберите изображение", "", IMAGE_FILTER)
            if not path:
                return
            r = disp_rect * dm

            def fn():
                page.insert_image(r, filename=path, keep_proportion=True, rotate=page.rotation)
            self.apply(fn)
            return
        if tiny:
            return
        r = disp_rect * dm
        if tool == "highlight":
            words = [fitz.Rect(w[:4]) for w in page.get_text("words", clip=r)]

            def fn():
                a = page.add_highlight_annot(words if words else r)
                a.update()
        elif tool == "rect":
            color, width = qcolor_rgb(self.draw_color), self.pen_width

            def fn():
                a = page.add_rect_annot(r)
                a.set_colors(stroke=color)
                a.set_border(width=width)
                a.update()
        elif tool == "erase":
            def fn():
                page.add_redact_annot(r, fill=(1, 1, 1))
                try:
                    page.apply_redactions(images=IMG_PIXELS, graphics=ART_COVERED)
                except TypeError:
                    page.apply_redactions(images=IMG_PIXELS)
        else:
            return
        self.apply(fn)

    def annot_menu(self, disp_pt, global_pos):
        page = self._page()
        pt = disp_pt * page.derotation_matrix
        hit = [a for a in page.annots() if a.rect.contains(pt)]
        if not hit:
            return
        xref = hit[-1].xref
        menu = QMenu(self)
        act = menu.addAction("Удалить аннотацию")
        if menu.exec(global_pos) == act:
            def fn():
                p = self._page()
                for a in p.annots():
                    if a.xref == xref:
                        p.delete_annot(a)
                        break
            self.apply(fn)

    def find_replace(self):
        dlg = ReplaceDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        needle, repl = dlg.find.text(), dlg.repl.text()
        if not needle:
            return
        rx = re.compile(re.escape(needle), 0 if dlg.case.isChecked() else re.IGNORECASE)
        pages = range(len(self.doc)) if dlg.all_pages.isChecked() else [self.cur]
        plans, total, notes = [], 0, []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for i in pages:
                page = self.doc[i]
                resolver, redacts, draws = None, [], []
                for line in text_lines(page):
                    texts = [sp["text"] for sp in line["spans"]]
                    cnt = len(rx.findall(line["raw"]))
                    if not cnt:
                        continue
                    if resolver is None:
                        resolver = FontResolver(page)
                    new_texts = [rx.sub(lambda m: repl, t) for t in texts]
                    if sum(len(rx.findall(t)) for t in texts) != cnt:  # вхождение на стыке фрагментов
                        new_texts = map_edit_to_spans(texts, rx.sub(lambda m: repl, line["raw"]))
                    r, d, nt = plan_line(resolver, line, new_texts)
                    redacts += r
                    draws += d
                    notes += nt
                    total += cnt
                if redacts:
                    plans.append((page, redacts, draws))
        except ValueError as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, str(ex))
            return
        QApplication.restoreOverrideCursor()
        if not total:
            QMessageBox.information(self, APP_NAME, "Текст «%s» не найден." % needle)
            return

        def fn():
            for page, redacts, draws in plans:
                if execute_plan(page, redacts, draws):
                    self.text_added = True
        if self.apply(fn, structural=True):
            msg = "Заменено: %d на %d стр." % (total, len(plans))
            if notes:
                msg += "\n\nШрифт: " + describe_fonts(notes).replace("; ", "\n")
            QMessageBox.information(self, APP_NAME, msg)


def main():
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    path = next((a for a in sys.argv[1:] if os.path.isfile(a)), None)
    w = Editor(path)
    w.show()
    if path:
        QTimer.singleShot(50, w.fit_width)  # окно уже имеет реальный размер
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
