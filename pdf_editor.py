#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF Редактор — настольный редактор PDF для Windows.
Стек: Python 3.9+, PySide6 (Qt 6), PyMuPDF.

Запуск:   python pdf_editor.py [файл.pdf]
Сборка:   см. build_exe.bat
"""
import math
import os
import sys
import tempfile

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
# Встроенные шрифты PDF (Helvetica и т.п.) не умеют кириллицу как следует,
# поэтому берём TTF из системы: на Windows — Arial / Times New Roman / Courier New.
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
_font_cache = {}


def find_font(family, bold=False, italic=False):
    """Путь к TTF-файлу нужного начертания или None."""
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
        path = find_font(family)  # нет жирного/курсива — берём обычный
    _font_cache[key] = path
    return path


def put_text(page, point, text, size, color, family="sans", bold=False, italic=False, rotate=0):
    """Вставляет текст. point — начало базовой линии в координатах страницы (без учёта /Rotate)."""
    path = find_font(family, bold, italic)
    kw = dict(fontsize=size, color=color, rotate=rotate)
    if path:
        fname = "E%s%s%d%d" % (SESSION, {"sans": "A", "serif": "T", "mono": "C"}[family], int(bold), int(italic))
        page.insert_text(point, text, fontname=fname, fontfile=path, **kw)
    else:  # запасной вариант: встроенный шрифт с кириллической кодировкой
        page.insert_text(point, text, fontname=BASE14[family],
                         encoding=fitz.TEXT_ENCODING_CYRILLIC, **kw)


# ---------------------------------------------------------- вспомогательные --
def int_to_rgb(c):
    return ((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0


def qcolor_rgb(q):
    return (q.redF(), q.greenF(), q.blueF())


def dlg_color(rgb):
    """Цвет после круга через QColor (для сравнения «изменилось ли»)."""
    return qcolor_rgb(QColor.fromRgbF(*rgb))


def span_style(span):
    """Определяет семейство/жирность/курсив по шрифту исходного фрагмента."""
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


def shrink_rect(rect, direction=(1, 0), k=0.2):
    """Сужаем область удаления поперёк строки, чтобы не задеть соседние строки."""
    r = fitz.Rect(rect)
    if abs(direction[0]) >= abs(direction[1]):
        d = r.height * k
        r.y0 += d
        r.y1 -= d
    else:
        d = r.width * k
        r.x0 += d
        r.x1 -= d
    return r


def remove_text_only(page):
    """Применяет пометки удаления: убирает только текст, графику и картинки не трогает."""
    try:
        page.apply_redactions(images=IMG_NONE, graphics=ART_NONE)
    except TypeError:
        page.apply_redactions(images=IMG_NONE)


def text_lines(page):
    """Все текстовые строки страницы со стилем первого фрагмента."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            out.append({"bbox": fitz.Rect(ln["bbox"]), "dir": tuple(ln["dir"]),
                        "text": "".join(s["text"] for s in ln["spans"]).strip(),
                        "spans": spans})
    return out


def collect_replacements(page, needle):
    """Находит вхождения needle и для каждого — шрифт, размер, цвет, базовую линию."""
    hits = page.search_for(needle)
    if not hits:
        return [], 0
    spans = []
    for ln in text_lines(page):
        for s in ln["spans"]:
            spans.append((fitz.Rect(s["bbox"]), s, ln["dir"]))
    result, skipped = [], 0
    for h in hits:
        c = fitz.Point((h.x0 + h.x1) / 2, (h.y0 + h.y1) / 2)
        best = None
        for r, s, d in spans:
            if r.contains(c):
                best = (s, d)
                break
        if best is None or abs(best[1][0]) < 0.99:  # только горизонтальные строки
            skipped += 1
            continue
        s, d = best
        result.append({"rect": h, "origin": fitz.Point(h.x0, s["origin"][1]),
                       "size": s["size"], "color": int_to_rgb(s["color"]),
                       "style": span_style(s), "dir": d})
    return result, skipped


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
                 family="sans", bold=False, italic=False, hint=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 260)
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
        for key, label in FAMILIES:
            self.family.addItem(label, key)
        self.family.setCurrentIndex([k for k, _ in FAMILIES].index(family))
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
        self.edit.setFocus()
        self.edit.selectAll()

    def values(self):
        return dict(text=self.edit.toPlainText().rstrip("\n"), size=self.size.value(),
                    color=qcolor_rgb(self.color.color), family=self.family.currentData(),
                    bold=self.bold.isChecked(), italic=self.italic.isChecked())


class ReplaceDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Найти и заменить текст")
        form = QFormLayout(self)
        self.find = QLineEdit()
        self.repl = QLineEdit()
        self.all_pages = QCheckBox("На всех страницах")
        self.all_pages.setChecked(True)
        form.addRow("Найти:", self.find)
        form.addRow("Заменить на:", self.repl)
        form.addRow("", self.all_pages)
        note = QLabel("Поиск без учёта регистра. Пустая замена — просто удалить найденное.")
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
        line = None
        for ln in text_lines(page):
            if ln["bbox"].contains(pt):
                line = ln
                break
        if line is None:
            self.statusBar().showMessage(
                "Под курсором нет текста. Если это скан (картинка), текст изменить нельзя — "
                "используйте «Стереть область» + «Текст».", 6000)
            return
        s0 = line["spans"][0]
        family, bold, italic = span_style(s0)
        dlg = TextDialog(self, "Изменить строку", line["text"], s0["size"], int_to_rgb(s0["color"]),
                         family, bold, italic,
                         hint="Исходный шрифт: %s. Оставьте поле пустым, чтобы удалить строку." % s0["font"])
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        before = dict(text=line["text"], size=round(s0["size"], 1), family=family, bold=bold, italic=italic)
        if all(v[k] == before[k] for k in before) and v["color"] == dlg_color(int_to_rgb(s0["color"])):
            return  # ничего не поменяли
        origin = fitz.Point(s0["origin"])
        direction = line["dir"]

        def fn():
            page.add_redact_annot(shrink_rect(line["bbox"], direction), fill=False)
            remove_text_only(page)
            if v["text"].strip():
                put_text(page, origin, v["text"], v["size"], v["color"], v["family"],
                         v["bold"], v["italic"], rotate=rotate_from_dir(direction))
                self.text_added = True
        self.apply(fn)

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
        if not needle.strip():
            return
        pages = range(len(self.doc)) if dlg.all_pages.isChecked() else [self.cur]
        plan, skipped = {}, 0
        for i in pages:
            items, sk = collect_replacements(self.doc[i], needle)
            skipped += sk
            if items:
                plan[i] = items
        total = sum(len(v) for v in plan.values())
        if not total:
            QMessageBox.information(self, APP_NAME, "Текст «%s» не найден." % needle)
            return

        def fn():
            for i, items in plan.items():
                page = self.doc[i]
                for it in items:
                    page.add_redact_annot(shrink_rect(it["rect"], it["dir"]), fill=False)
                remove_text_only(page)
                if repl:
                    for it in items:
                        fam, b, ital = it["style"]
                        put_text(page, it["origin"], repl, it["size"], it["color"], fam, b, ital)
                    self.text_added = True
        if self.apply(fn, structural=True):
            msg = "Заменено: %d на %d стр." % (total, len(plan))
            if skipped:
                msg += "\nПропущено (вертикальный текст): %d" % skipped
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
