"""One widget per question type — the Qt twins of geodit-ui's
``runtime/questions/*Question.tsx``.

Each editor shows a question (number, text, required asterisk, "Hidden from
surveyors" tag, error line) and writes back exactly the value shape the web
component writes, through ``EditorContext.on_change``; the form session does
the rest. ``update_state`` applies what the session says — value, visible
options, disabled, error, highlight — without echoing a change back, and never
resets a field the user is typing into to an equivalent value.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, List, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QDate, QDateTime, QRegularExpression, QSize, Qt, QTime
from qgis.PyQt.QtGui import QPixmap, QRegularExpressionValidator
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)

from ...forms.answers import (
    MANUAL_TEXT_MAX_LEN,
    build_manual_value,
    coerce_choice_list,
    coerce_numeric,
    coerce_string,
    file_answer_from_value,
    find_manual_option,
    get_manual_text,
    get_option_label,
    is_manual_value,
    same_answer_value,
    stored_choice_list_length,
)
from ...forms.defaults import is_schema_unique_id
from ...forms.jsnum import as_int_if_integral, is_number, js_number
from ...forms.model import (
    PARAGRAPH_ANSWER_MAX_LEN,
    TEXT_ANSWER_MAX_LEN,
    DefaultType,
    IdentityType,
    Option,
    QType,
    Question,
)
from ...forms.phone import COUNTRIES, DEFAULT_COUNTRY, country_for, dial_code, split_phone, without_trunk_zero
from ...forms.timezone import parse_wall_clock
from ...forms.validation import js_len, text_length_range
from ..theme import Theme, font, line_icon
from ..widgets import Pill

MEDIA_KIND = {
    QType.IMAGE: "image",
    QType.DOCUMENT: "document",
    QType.AUDIO: "audio",
    QType.VIDEO: "video",
}
_MEDIA_ICON = {"image": "image", "document": "file", "audio": "audio", "video": "video", "signature": "pen"}


class EditorContext:
    """What an editor may call: ``on_change(ques_id, value, page_key)`` and the
    form's services (media, map pick) — see ``FeatureFormWindow``."""

    def __init__(self, on_change: Callable[[int, Any, int], None], services, theme: Theme) -> None:
        self.on_change = on_change
        self.services = services
        self.theme = theme


def _field(edit: QLineEdit, placeholder: str = "") -> QLineEdit:
    edit.setPlaceholderText(placeholder)
    edit.setProperty("field", "true")
    return edit


def _fit_max_length(edit: QLineEdit, cap: int, text: str) -> None:
    """Cap typing at ``cap`` without cutting what the field holds or is about
    to show. An HTML ``maxLength`` leaves a longer value alone; Qt cuts it,
    silently (no ``textEdited``), and ``setMaxLength`` moves the cursor, so
    the limit never drops below either text and changes only when it must."""
    limit = max(cap, js_len(edit.text()), js_len(text))
    if edit.maxLength() != limit:
        edit.setMaxLength(limit)


def _tool(icon: str, tooltip: str, theme: Theme, text: str = "") -> QToolButton:
    btn = QToolButton()
    btn.setProperty("variant", "icon")
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setIcon(line_icon(icon, theme.text, 16))
    btn.setIconSize(QSize(16, 16))
    if text:
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    return btn


def _secondary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "secondary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    return btn


class QuestionEditor(QFrame):
    """The frame every question shares (``BaseQuestion`` on the web)."""

    def __init__(self, question: Question, page_key: int, ctx: EditorContext) -> None:
        super().__init__()
        self.question = question
        self.page_key = page_key
        self.ctx = ctx
        self._updating = False
        self._value: Any = None
        self._disabled = False
        self._editable_applied = False
        self._highlight = False
        self.setObjectName("QuestionEditor")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        self.tag = Pill("Hidden from surveyors", "neutral")
        self.tag.hide()
        outer.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignLeft)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.number = QLabel(f"{question.position}.")
        self.number.setFont(font(1.0, bold=True))
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        head.addWidget(self.number, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.title, 1)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        outer.addLayout(self.body)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        self.build()
        self._set_title(question)
        self._restyle()

    # ------------------------------------------------------------ subclass API
    def build(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def show_value(self, value: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def set_editable(self, editable: bool) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def set_options(self, options: List[Option]) -> None:
        """Choice questions: the options rules leave visible on this tab."""

    # ------------------------------------------------------------ shared
    def emit(self, value: Any) -> None:
        if self._updating:
            return
        self._value = value
        self.ctx.on_change(self.question.id, value, self.page_key)

    def _set_title(self, presented: Question) -> None:
        text = presented.label or f"Question {presented.id}"
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        star = ' <span style="color:#E03131">*</span>' if presented.attr("mandatory") else ""
        self.title.setText(escaped + star)

    def update_state(
        self,
        *,
        presented: Question,
        value: Any,
        options: Optional[List[Option]],
        disabled: bool,
        error: Optional[str],
        highlight: bool,
        author_hidden: bool,
    ) -> None:
        self._updating = True
        try:
            self._set_title(presented)
            self.tag.setVisible(author_hidden)
            if options is not None:
                self.set_options(options)
            if not same_answer_value(value, self._value):
                self._value = value
                self.show_value(value)
            if disabled != self._disabled or not self._editable_applied:
                self._disabled = disabled
                self._editable_applied = True
                self.set_editable(not disabled)
            self.error.setText(error or "")
            self.error.setVisible(bool(error))
            if highlight != self._highlight:
                self._highlight = highlight
                self._restyle()
        finally:
            self._updating = False

    def apply_theme(self, theme: Theme) -> None:
        self.ctx.theme = theme
        self._restyle()

    def _restyle(self) -> None:
        theme = self.ctx.theme
        bg, _ = theme.tone("warning")
        style = f"QFrame#QuestionEditor {{ border-radius: 8px; border: 1px solid {'#E8590C' if self._highlight else 'transparent'}; "
        style += f"background: {bg.name() if self._highlight else 'transparent'}; }}"
        self.setStyleSheet(style)
        _, danger = theme.tone("danger")
        self.error.setStyleSheet(f"color: {danger.name()};")
        self.number.setStyleSheet(f"color: {theme.accent_text.name()};")


# ====================================================================== text
def _hide_stored_default(question: Question) -> bool:
    kind = question.attr("default_type")
    number = float(kind) if is_number(kind) else js_number(kind)
    return number in (DefaultType.CALCULATE, DefaultType.SHAPEFILE)


def _placeholder(question: Question) -> str:
    default = question.attr("default_value")
    if not _hide_stored_default(question) and isinstance(default, str) and default:
        return default
    return "Type your answer"


_IDENTITY_PLACEHOLDER = {
    IdentityType.AADHAAR_CARD: "1234 5678 9012",
    IdentityType.PAN_CARD: "ABCPD1234E",  # not all hex digits: detect-secrets reads those as a key
    IdentityType.DRIVING_LICENSE: "DL01 20191234567",
    IdentityType.VOTER_ID: "ABC1234567",
    IdentityType.PASSPORT: "A1234567",
}


def sanitize_identity(raw: str, identity_type: Any) -> str:
    """``IdentityQuestion``'s input filter, per identity type."""
    if identity_type == IdentityType.AADHAAR_CARD:
        digits = re.sub(r"[^0-9]", "", raw)[:12]
        return re.sub(r"([0-9]{4})(?=[0-9])", r"\1 ", digits)
    if identity_type in (IdentityType.PAN_CARD, IdentityType.VOTER_ID):
        return re.sub(r"[^A-Z0-9]", "", raw.upper())[:10]
    if identity_type == IdentityType.PASSPORT:
        return re.sub(r"[^A-Z0-9]", "", raw.upper())[:8]
    if identity_type == IdentityType.DRIVING_LICENSE:
        return re.sub(r"[^A-Z0-9 -]", "", raw.upper())[:18]
    return raw.upper()


class LineEditor(QuestionEditor):
    """TEXT, EMAIL, IDENTITY and ID: one line, with the web's input filter."""

    def build(self) -> None:
        q = self.question
        self.edit = _field(QLineEdit())
        self.transform: Callable[[str], str] = lambda text: text
        self.computed = False
        self.cap: Optional[int] = None
        if q.q_type == QType.TEXT:
            self.edit.setPlaceholderText(_placeholder(q))
            self.cap = text_length_range(q.attributes)["max"]
        elif q.q_type == QType.EMAIL:
            self.edit.setPlaceholderText("name@example.com")
            self.transform = lambda text: re.sub(r"\s", "", text).lower()
            self.cap = TEXT_ANSWER_MAX_LEN
        elif q.q_type == QType.IDENTITY:
            kind = q.attr("identity_type")
            self.edit.setPlaceholderText(_IDENTITY_PLACEHOLDER.get(kind, "Enter identity"))
            self.transform = lambda text: sanitize_identity(text, kind)
            self.cap = TEXT_ANSWER_MAX_LEN
        elif q.q_type == QType.ID:
            self.computed = is_schema_unique_id(q.attributes)
            if self.computed:
                self.edit.setPlaceholderText("Generated automatically")
                self.edit.setReadOnly(True)
            else:
                self.edit.setPlaceholderText("Enter ID")
                self.cap = 32
                self.transform = lambda text: re.sub(r"[^A-Za-z0-9_-]", "", text)
        if self.cap is not None:
            _fit_max_length(self.edit, self.cap, "")
        self.edit.textEdited.connect(self._edited)
        self.body.addWidget(self.edit)

    def _edited(self, text: str) -> None:
        clean = self.transform(text)
        if clean != text:
            position = self.edit.cursorPosition()
            self.edit.setText(clean)
            self.edit.setCursorPosition(min(position, len(clean)))
        self.emit(clean)

    def show_value(self, value: Any) -> None:
        text = coerce_string(value)
        if self.edit.text() != text:
            if self.cap is not None:
                _fit_max_length(self.edit, self.cap, text)
            self.edit.setText(text)

    def set_editable(self, editable: bool) -> None:
        self.edit.setEnabled(editable and not self.computed)


class ParagraphEditor(QuestionEditor):
    def build(self) -> None:
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(_placeholder(self.question))
        self.edit.setTabChangesFocus(True)
        self.edit.setFixedHeight(96)
        self.edit.textChanged.connect(self._changed)
        self.body.addWidget(self.edit)

    def _changed(self) -> None:
        if self._updating:
            return
        text = self.edit.toPlainText()
        if len(text) > PARAGRAPH_ANSWER_MAX_LEN:  # the textarea's maxLength
            text = text[:PARAGRAPH_ANSWER_MAX_LEN]
            self._updating = True
            self.edit.setPlainText(text)
            self._updating = False
        self.emit(text)

    def show_value(self, value: Any) -> None:
        text = coerce_string(value)
        if self.edit.toPlainText() != text:
            self.edit.setPlainText(text)

    def set_editable(self, editable: bool) -> None:
        self.edit.setReadOnly(not editable)
        self.edit.setEnabled(editable)


class PhoneEditor(QuestionEditor):
    """The E.164 digits without ``+`` (country code first), as the web stores them."""

    def build(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.country = QComboBox()
        for iso, name, code in COUNTRIES:
            self.country.addItem(f"{name} (+{code})", iso)
        self.country.setCurrentIndex(max(0, self.country.findData(DEFAULT_COUNTRY)))
        self.country.setMaximumWidth(170)
        self.digits = _field(QLineEdit(), "Phone number")
        self.digits.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9 ]*")))
        self.country.currentIndexChanged.connect(lambda _i: self._changed())
        self.digits.textEdited.connect(lambda _t: self._changed())
        row.addWidget(self.country)
        row.addWidget(self.digits, 1)
        self.body.addLayout(row)

    def _changed(self) -> None:
        if self._updating:
            return
        national = re.sub(r"[^0-9]", "", self.digits.text())
        if not national:
            self.emit(None)
            return
        # Typed with the trunk 0 ("07911…"): stored without it, as the web does.
        self.emit(without_trunk_zero(dial_code(self.country.currentData() or DEFAULT_COUNTRY) + national))

    def show_value(self, value: Any) -> None:
        text = coerce_string(value)
        if not text:
            self.digits.setText("")
            return
        code, national = split_phone(text)
        if code is None:
            self.digits.setText(text)
            return
        iso = country_for(text, self.country.currentData() or DEFAULT_COUNTRY)
        self.country.setCurrentIndex(max(0, self.country.findData(iso)))
        self.digits.setText(national)

    def set_editable(self, editable: bool) -> None:
        self.country.setEnabled(editable)
        self.digits.setEnabled(editable)


# ====================================================================== numbers
class NumberEditor(QuestionEditor):
    """NUMBER: whole numbers only (no sign, no point — the column can't store
    a negative)."""

    def build(self) -> None:
        self.edit = _field(QLineEdit(), "0")
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]*")))
        self.edit.textEdited.connect(self._edited)
        self.body.addWidget(self.edit)

    def _edited(self, text: str) -> None:
        self.emit(int(text) if text else None)

    def show_value(self, value: Any) -> None:
        text = coerce_string(value)
        if self.edit.text() != text:
            self.edit.setText(text)

    def set_editable(self, editable: bool) -> None:
        self.edit.setEnabled(editable)


def _parse_decimal(text: str) -> Optional[float]:
    if text.strip() == "":
        return None
    number = js_number(text)
    return as_int_if_integral(number) if math.isfinite(number) else None


class DecimalEditor(QuestionEditor):
    """DECIMAL and MEASUREMENT (``useDecimalText``): the text is kept while
    typing ("1." / "1.0" survive), the answer holds the parsed number."""

    def build(self) -> None:
        q = self.question
        minimum = q.attr("minimum")
        allow_negative = q.q_type == QType.DECIMAL and (not is_number(minimum) or minimum < 0)
        pattern = r"-?[0-9]*\.?[0-9]*" if allow_negative else r"[0-9]*\.?[0-9]*"
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = _field(QLineEdit(), "0.00" if q.q_type == QType.DECIMAL else "0")
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(pattern)))
        self.edit.textEdited.connect(lambda text: self.emit(_parse_decimal(text)))
        row.addWidget(self.edit, 1)
        suffix = q.attr("suffix")
        if q.q_type == QType.MEASUREMENT and isinstance(suffix, str) and suffix:
            unit = QLabel(suffix)
            unit.setProperty("kind", "muted")
            row.addWidget(unit)
        self.body.addLayout(row)

    def show_value(self, value: Any) -> None:
        # Adopt an external write (a stored response, a recomputed default) but
        # not the echo of our own: "1.0" parses to the 1 just emitted.
        if _parse_decimal(self.edit.text()) == coerce_numeric(value) and self.edit.text():
            return
        self.edit.setText(coerce_string(value))

    def set_editable(self, editable: bool) -> None:
        self.edit.setEnabled(editable)


class CounterEditor(QuestionEditor):
    def build(self) -> None:
        q = self.question
        minimum = q.attr("minimum")
        self.minimum = max(minimum if is_number(minimum) else 0, 0)
        maximum = q.attr("maximum")
        self.maximum = maximum if is_number(maximum) else None
        row = QHBoxLayout()
        row.setSpacing(6)
        theme = self.ctx.theme
        self.minus = _tool("minus", "Decrease", theme)
        self.plus = _tool("plus", "Increase", theme)
        self.edit = _field(QLineEdit())
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]*")))
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.setMaximumWidth(96)
        self.minus.clicked.connect(lambda: self._set_next(self._current() - 1))
        self.plus.clicked.connect(self._increment)
        self.edit.textEdited.connect(self._typed)
        row.addWidget(self.minus)
        row.addWidget(self.edit)
        row.addWidget(self.plus)
        row.addStretch(1)
        self.body.addLayout(row)

    def _numeric(self) -> Optional[float]:
        return coerce_numeric(self._value)

    def _current(self) -> float:
        number = self._numeric()
        return number if number is not None else self.minimum

    def _set_next(self, value: float) -> None:
        n = max(self.minimum, value)
        if self.maximum is not None and self.maximum > 0:
            n = min(self.maximum, n)
        n = as_int_if_integral(float(n)) if isinstance(n, float) else n
        self.emit(n)
        self._refresh_buttons()

    def _increment(self) -> None:
        if self._numeric() is None:
            self._set_next(self.minimum)
        else:
            self._set_next(self._current() + 1)

    def _typed(self, text: str) -> None:
        if not text:
            self.emit(None)
        else:
            self._set_next(int(text))
            self.show_value(self._value)
        self._refresh_buttons()

    def show_value(self, value: Any) -> None:
        number = coerce_numeric(value)
        text = "" if number is None else coerce_string(value)
        if self.edit.text() != text:
            self.edit.setText(text)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        unset = self._numeric() is None
        current = self._current()
        editable = not self._disabled
        self.minus.setEnabled(editable and not unset and current > self.minimum)
        self.plus.setEnabled(
            editable and (unset or not (self.maximum is not None and self.maximum > 0 and current >= self.maximum))
        )

    def set_editable(self, editable: bool) -> None:
        self.edit.setEnabled(editable)
        self._refresh_buttons()


class RatingEditor(QuestionEditor):
    def build(self) -> None:
        raw = self.question.attr("max_rating")
        self.max_rating = max(1, int(raw) if is_number(raw) else 5)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.stars: List[QToolButton] = []
        for star in range(1, self.max_rating + 1):
            btn = QToolButton()
            btn.setProperty("variant", "icon")
            btn.setToolTip(f"{star} out of {self.max_rating}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setIconSize(QSize(22, 22))
            btn.clicked.connect(lambda _c=False, s=star: self.emit(s))
            row.addWidget(btn)
            self.stars.append(btn)
        row.addStretch(1)
        self.body.addLayout(row)
        self._paint(0)

    def _paint(self, current: float) -> None:
        theme = self.ctx.theme
        for i, btn in enumerate(self.stars, start=1):
            filled = i <= current
            btn.setIcon(line_icon("star", theme.accent if filled else theme.border, 22, filled=filled))

    def emit(self, value: Any) -> None:
        super().emit(value)
        self._paint(coerce_numeric(value) or 0)

    def show_value(self, value: Any) -> None:
        self._paint(coerce_numeric(value) or 0)

    def set_editable(self, editable: bool) -> None:
        for btn in self.stars:
            btn.setEnabled(editable)


# ====================================================================== date
_NULL_DATETIME = QDateTime(QDate(1752, 9, 14), QTime(0, 0))


class _DateTimeEdit(QDateTimeEdit):
    """Starts from "now" when the user first acts on an empty field — a click
    (the calendar button too) or a key — instead of the sentinel that stands
    for no value. Focus alone (Tab, the window coming back after a map pick)
    never answers the question."""

    def _start_from_now(self) -> None:
        if self.dateTime() == _NULL_DATETIME and not self.isReadOnly() and self.isEnabled():
            now = QDateTime.currentDateTime()
            self.setDateTime(QDateTime(now.date(), QTime(now.time().hour(), now.time().minute())))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._start_from_now()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Escape):
            self._start_from_now()
        super().keyPressEvent(event)


class DateTimeEditor(QuestionEditor):
    """The clock time the surveyor saw: ``YYYY-MM-DDTHH:mm:ss``, no zone."""

    def build(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = _DateTimeEdit()
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("dd MMM yyyy, HH:mm")
        self.edit.setMinimumDateTime(_NULL_DATETIME)
        self.edit.setSpecialValueText("Pick a date and time")
        self.edit.setDateTime(_NULL_DATETIME)
        self.edit.dateTimeChanged.connect(self._changed)
        self.clear = _tool("x", "Clear", self.ctx.theme)
        self.clear.clicked.connect(lambda: self.edit.setDateTime(_NULL_DATETIME))
        row.addWidget(self.edit, 1)
        row.addWidget(self.clear)
        self.body.addLayout(row)

    def _changed(self, value: QDateTime) -> None:
        if self._updating:
            return
        self.emit(None if value == _NULL_DATETIME else value.toString("yyyy-MM-ddTHH:mm:ss"))

    def show_value(self, value: Any) -> None:
        wc = parse_wall_clock(value)
        target = (
            QDateTime(QDate(wc.year, wc.month, wc.day), QTime(wc.hour, wc.minute, wc.second)) if wc else _NULL_DATETIME
        )
        if self.edit.dateTime() != target:
            self.edit.setDateTime(target)

    def set_editable(self, editable: bool) -> None:
        self.edit.setEnabled(editable)
        self.clear.setEnabled(editable)


# ====================================================================== choices
class DropdownEditor(QuestionEditor):
    """The option's id as a string, or ``_manual_:<text>`` for "Enter Manually"."""

    def build(self) -> None:
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.activated.connect(self._picked)
        self.manual = _field(QLineEdit(), "Type your answer")
        _fit_max_length(self.manual, MANUAL_TEXT_MAX_LEN, "")
        self.manual.hide()
        self.manual.textEdited.connect(lambda text: self.emit(build_manual_value(text)))
        self.body.addWidget(self.combo)
        self.body.addWidget(self.manual)
        self._options: List[Option] = list(self.question.opt_list)
        self._fill()

    def _fill(self) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("Select an option", None)
        for option in self._options:
            self.combo.addItem(get_option_label(option), str(option.id))
        self.combo.blockSignals(False)
        self._select(self._value)

    def set_options(self, options: List[Option]) -> None:
        if [o.id for o in options] != [o.id for o in self._options]:
            self._options = list(options)
            self._fill()

    def _manual_option(self) -> Optional[Option]:
        return find_manual_option(self._options)

    def _picked(self, index: int) -> None:
        data = self.combo.itemData(index)
        if data is None:
            return
        manual = self._manual_option()
        if manual is not None and data == str(manual.id):
            self.manual.setText("")
            self.manual.show()
            self.emit(build_manual_value(""))
            self.manual.setFocus()
            return
        self.manual.hide()
        self.emit(data)

    def _select(self, value: Any) -> None:
        current = coerce_string(value)
        manual = self._manual_option()
        if is_manual_value(current):
            index = self.combo.findData(str(manual.id)) if manual is not None else -1
            self.manual.setVisible(True)
            if self.manual.text() != get_manual_text(current):
                _fit_max_length(self.manual, MANUAL_TEXT_MAX_LEN, get_manual_text(current))
                self.manual.setText(get_manual_text(current))
        else:
            index = self.combo.findData(current) if current else -1
            self.manual.hide()
        self.combo.setCurrentIndex(index if index >= 0 else 0)

    def show_value(self, value: Any) -> None:
        self._select(value)

    def set_editable(self, editable: bool) -> None:
        self.combo.setEnabled(editable)
        self.manual.setEnabled(editable)


class MultipleChoiceEditor(QuestionEditor):
    """A list of option ids, a ``_manual_:<text>`` entry last."""

    def build(self) -> None:
        self.box = QVBoxLayout()
        self.box.setSpacing(4)
        self.body.addLayout(self.box)
        self.manual = _field(QLineEdit(), "Type your answer")
        self.manual.hide()
        self.manual.textEdited.connect(self._manual_typed)
        self.body.addWidget(self.manual)
        self.checks: List[QCheckBox] = []
        self._options: List[Option] = list(self.question.opt_list)
        self._fill()

    def _fill(self) -> None:
        for check in self.checks:
            self.box.removeWidget(check)
            check.deleteLater()
        self.checks = []
        for option in self._options:
            check = QCheckBox(get_option_label(option))
            check.setProperty("option_id", str(option.id))
            check.clicked.connect(lambda _c=False, oid=str(option.id): self._toggle(oid))
            self.box.addWidget(check)
            self.checks.append(check)
        self._sync(self._value)
        self.set_editable(not self._disabled)

    def set_options(self, options: List[Option]) -> None:
        if [o.id for o in options] != [o.id for o in self._options]:
            self._options = list(options)
            self._fill()

    def _parts(self):
        raw = coerce_choice_list(self._value)
        manual_entry = next((v for v in raw if is_manual_value(v)), None)
        selected = [v for v in raw if not is_manual_value(v)]
        return selected, manual_entry

    def _toggle(self, option_id: str) -> None:
        selected, manual_entry = self._parts()
        manual_text = get_manual_text(manual_entry)
        manual = find_manual_option(self._options)
        if manual is not None and option_id == str(manual.id):
            if manual_entry is not None:
                self.emit(selected)
            else:
                self.emit([*selected, build_manual_value(manual_text)])
                self.manual.setFocus()
        else:
            nxt = [s for s in selected if s != option_id] if option_id in selected else [*selected, option_id]
            self.emit([*nxt, build_manual_value(manual_text)] if manual_entry is not None else nxt)
        self._sync(self._value)

    def _manual_typed(self, text: str) -> None:
        selected, _ = self._parts()
        self.emit([*selected, build_manual_value(text)])

    def _sync(self, value: Any) -> None:
        raw = coerce_choice_list(value)
        manual_entry = next((v for v in raw if is_manual_value(v)), None)
        picks = [v for v in raw if not is_manual_value(v)]
        selected = set(picks)
        manual = find_manual_option(self._options)
        for check in self.checks:
            oid = check.property("option_id")
            is_manual = manual is not None and oid == str(manual.id)
            check.setChecked(manual_entry is not None if is_manual else oid in selected)
        # The whole list is stored in one varchar(255), so the typed text gets
        # what the other picks leave (validation has the exact rule).
        budget = TEXT_ANSWER_MAX_LEN - stored_choice_list_length([*picks, build_manual_value("")])
        text = get_manual_text(manual_entry)
        _fit_max_length(self.manual, min(MANUAL_TEXT_MAX_LEN, max(0, budget)), text)
        self.manual.setVisible(manual_entry is not None)
        if manual_entry is not None and self.manual.text() != text:
            self.manual.setText(text)

    def show_value(self, value: Any) -> None:
        self._sync(value)

    def set_editable(self, editable: bool) -> None:
        for check in self.checks:
            check.setEnabled(editable)
        self.manual.setEnabled(editable)


# ====================================================================== location
class LocationEditor(QuestionEditor):
    """``{lat, lng}`` in WGS 84 degrees."""

    def build(self) -> None:
        theme = self.ctx.theme
        grid = QHBoxLayout()
        grid.setSpacing(6)
        number = QRegularExpressionValidator(QRegularExpression(r"-?[0-9]*\.?[0-9]*"))
        self.lat = _field(QLineEdit(), "Latitude")
        self.lng = _field(QLineEdit(), "Longitude")
        for edit in (self.lat, self.lng):
            edit.setValidator(number)
            edit.textEdited.connect(lambda _t: self._typed())
            grid.addWidget(edit, 1)
        self.body.addLayout(grid)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.pick = _tool("crosshair", "Click a point on the map", theme, "Pick on map")
        self.here = _tool("pin", "Use this feature's location", theme, "Feature location")
        self.clear = _tool("x", "Clear the location", theme)
        self.pick.clicked.connect(lambda: self.ctx.services.pick_point(self._picked))
        self.here.clicked.connect(self._feature_location)
        self.clear.clicked.connect(self._cleared)
        for btn in (self.pick, self.here, self.clear):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

    def _typed(self) -> None:
        lat, lng = _parse_decimal(self.lat.text()), _parse_decimal(self.lng.text())
        if lat is None and lng is None:
            self.emit(None)
        elif lat is not None and lng is not None:
            self.emit({"lat": lat, "lng": lng})

    def _picked(self, lat: float, lng: float) -> None:
        value = {"lat": round(lat, 7), "lng": round(lng, 7)}
        if not sip.isdeleted(self):  # the page may have been rebuilt while the map was clicked
            self.show_value(value)
        self.emit(value)

    def _feature_location(self) -> None:
        point = self.ctx.services.feature_location()
        if point is not None:
            self._picked(*point)

    def _cleared(self) -> None:
        self.show_value(None)
        self.emit(None)

    def show_value(self, value: Any) -> None:
        if isinstance(value, dict) and is_number(value.get("lat")) and is_number(value.get("lng")):
            lat, lng = coerce_string(value["lat"]), coerce_string(value["lng"])
        else:
            lat = lng = ""
        if _parse_decimal(self.lat.text()) != coerce_numeric(lat) or not self.lat.text():
            self.lat.setText(lat)
        if _parse_decimal(self.lng.text()) != coerce_numeric(lng) or not self.lng.text():
            self.lng.setText(lng)

    def set_editable(self, editable: bool) -> None:
        for widget in (self.lat, self.lng, self.pick, self.clear):
            widget.setEnabled(editable)
        self.here.setEnabled(editable and self.ctx.services.feature_location() is not None)


# ====================================================================== media
class MediaEditor(QuestionEditor):
    """IMAGE / DOCUMENT / AUDIO / VIDEO / SIGNATURE: ``{url, key, name}`` once
    uploaded (a stored answer reads back as the bare key); ``None`` removes it."""

    def build(self) -> None:
        theme = self.ctx.theme
        self.kind = MEDIA_KIND.get(self.question.q_type, "signature")
        card = QFrame()
        card.setProperty("card", "true")
        row = QHBoxLayout(card)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(8)
        self.preview = QLabel()
        # A signature is a wide strip: give it the room a photo gets in height.
        self.preview.setFixedSize(160 if self.kind == "signature" else 96, 72)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview.mousePressEvent = lambda _e: self._open()  # type: ignore[method-assign]
        info = QVBoxLayout()
        info.setSpacing(2)
        self.name = QLabel()
        self.name.setWordWrap(True)
        self.status = QLabel()
        self.status.setProperty("kind", "muted")
        self.status.setWordWrap(True)
        info.addWidget(self.name)
        info.addWidget(self.status)
        info.addStretch(1)
        row.addWidget(self.preview)
        row.addLayout(info, 1)
        self.body.addWidget(card)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        if self.kind == "signature":
            self.choose = _tool("pen", "Draw a signature", theme, "Draw signature")
        else:
            self.choose = _tool("upload", "Choose a file to upload", theme, "Choose file…")
        self.open_btn = _tool("external", "Open the file", theme, "Open")
        self.remove = _tool("x", "Remove the file", theme, "Remove")
        self.choose.clicked.connect(self._choose)
        self.open_btn.clicked.connect(self._open)
        self.remove.clicked.connect(lambda: self.emit(None))
        for btn in (self.choose, self.open_btn, self.remove):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        self.body.addLayout(buttons)
        self._busy = False
        self._show(None)

    def _file(self):
        return file_answer_from_value(self._value)

    def _show(self, value: Any) -> None:
        file = file_answer_from_value(value)
        theme = self.ctx.theme
        icon = line_icon(_MEDIA_ICON[self.kind], theme.muted, 28).pixmap(28, 28)
        self.preview.setPixmap(icon)
        if file is None or (not file["key"] and not file["url"]):
            self.name.setText("No file" if self.kind != "signature" else "Not signed")
            self.status.setText("")
        else:
            self.name.setText(file["name"] or "file")
            self.status.setText("")
            if self.kind in ("image", "signature"):
                self.ctx.services.thumbnail(value, self._thumbnail_ready)
        self._refresh_buttons()

    def _thumbnail_ready(self, value: Any, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or sip.isdeleted(self) or not same_answer_value(value, self._value):
            return
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.width(),
                self.preview.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _refresh_buttons(self) -> None:
        has_file = self._file() is not None
        editable = not self._disabled and not self._busy
        self.choose.setEnabled(editable)
        self.remove.setEnabled(editable and has_file)
        self.open_btn.setEnabled(has_file and not self._busy)

    def _choose(self) -> None:
        self.ctx.services.choose_media(self.kind, self.question, self._upload_started, self._uploaded)

    def _upload_started(self, text: str) -> None:
        self._busy = True
        self.status.setText(text)
        self._refresh_buttons()

    def _uploaded(self, value: Any, error: Optional[str]) -> None:
        # The page may have been rebuilt during the upload: the answer still
        # goes to the form, only the widgets are gone.
        alive = not sip.isdeleted(self)
        self._busy = False
        if error:
            if alive:
                self.status.setText(error)
                self._refresh_buttons()
            return
        self._value = value
        self.ctx.on_change(self.question.id, value, self.page_key)
        if alive:
            self._show(value)

    def _open(self) -> None:
        if self._file() is not None:
            self.ctx.services.open_media(self._value)

    def show_value(self, value: Any) -> None:
        self._show(value)

    def set_editable(self, editable: bool) -> None:
        self._disabled = not editable
        self._refresh_buttons()


class UnsupportedEditor(QuestionEditor):
    def build(self) -> None:
        self.note = QLabel(f"Unsupported question type ({self.question.q_type}).")
        self.note.setProperty("kind", "muted")
        self.body.addWidget(self.note)

    def show_value(self, value: Any) -> None:
        self.note.setText(f"Unsupported question type ({self.question.q_type}). Stored: {coerce_string(value)}")

    def set_editable(self, editable: bool) -> None:
        pass


_EDITORS = {
    QType.TEXT: LineEditor,
    QType.EMAIL: LineEditor,
    QType.IDENTITY: LineEditor,
    QType.ID: LineEditor,
    QType.PARAGRAPH: ParagraphEditor,
    QType.PHONE: PhoneEditor,
    QType.NUMBER: NumberEditor,
    QType.DECIMAL: DecimalEditor,
    QType.MEASUREMENT: DecimalEditor,
    QType.COUNTER: CounterEditor,
    QType.RATING: RatingEditor,
    QType.DATETIME: DateTimeEditor,
    QType.DROPDOWN: DropdownEditor,
    QType.MULTIPLE_CHOICE: MultipleChoiceEditor,
    QType.LOCATION: LocationEditor,
    QType.IMAGE: MediaEditor,
    QType.DOCUMENT: MediaEditor,
    QType.AUDIO: MediaEditor,
    QType.VIDEO: MediaEditor,
    QType.SIGNATURE: MediaEditor,
}


def create_editor(question: Question, page_key: int, ctx: EditorContext) -> QuestionEditor:
    return _EDITORS.get(question.q_type, UnsupportedEditor)(question, page_key, ctx)
