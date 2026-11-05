#!/usr/bin/env python3
"""S360 Control - графический интерфейс (PySide6) для MSI MEG CoreLiquid S360.

Принцип: рабочие функции работают, остальное показано как НЕДОСТУПНОЕ с причиной (без муляжей управления).
  Работает (проверено на железе): загрузка GIF/MP4 на LCD.
  Режимы кулера и свой профиль: работа и изменение оборотов подтверждены владельцем; обратного чтения нет.
  Поворот экрана: через публичный API ядра, 90°/270° экспериментальны.
  Яркость и вкл/выкл: через API ядра, с подтверждением записи во флеш.
  Часы: подтверждённый стиль 1, местное время перед отправкой, без автосинхронизации.
  Недоступно в GUI: другие режимы экрана, чтение RPM/температуры жидкости.
"""
from copy import deepcopy
from html import escape
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QPixmap, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QApplication, QBoxLayout, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
                               QDialog, QDialogButtonBox, QGridLayout, QToolTip, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
                               QScrollArea, QSizePolicy, QStackedWidget, QTabBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

import s360_lcd as core
import theme

UNIT = "s360-feed.service"
COOLING_TITLES = {"silent": "Тихий", "balance": "Сбалансированный", "game": "Игровой", "custom": "Свой профиль"}
OK, BAD, WARN = (theme.COLORS[key] for key in ("good", "error", "warning"))

STYLE = theme.build_qss()


def badge(text, color):
    return f"<span style='color:{color}'>● {text}</span>"


def systemctl(*args):
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def unit_installed():
    return (Path.home() / ".config/systemd/user" / UNIT).exists()


def muted(text):
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


def load_ui_settings():
    settings = core.load_settings()
    try:
        if json.loads(core.SETTINGS.read_text()).get("first_run_done") is True:
            settings["first_run_done"] = True
    except (OSError, ValueError, AttributeError):
        pass
    return settings


def save_ui_settings(settings):
    # Ядро хранит только известные ему поля; GUI добавляет собственный флаг.
    done = settings.get("first_run_done", False)
    core.save_settings(settings)
    if done:
        saved = json.loads(core.SETTINGS.read_text())
        saved["first_run_done"] = True
        core._atomic_json(core.SETTINGS, saved)


def device_access(vid, pid, *, lcd=False, root=Path("/sys/bus/usb/devices")):
    """Только sysfs и проверка прав, без открытия узлов устройства."""
    if not core.usb_present(vid, pid):
        return "missing"
    if not lcd:
        node = core.find_hidraw(vid, pid)
        return "ready" if node and os.access(node, os.R_OK | os.W_OK) else "denied"
    try:
        nodes = []
        for entry in root.iterdir():
            try:
                if (int((entry / "idVendor").read_text(), 16),
                    int((entry / "idProduct").read_text(), 16)) == (vid, pid):
                    bus = int((entry / "busnum").read_text())
                    dev = int((entry / "devnum").read_text())
                    nodes.append(Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}"))
            except (OSError, ValueError):
                continue
        return "ready" if nodes and all(os.access(n, os.R_OK | os.W_OK) for n in nodes) else "denied"
    except OSError:
        return "denied"


class SendWorker(QThread):
    progress = Signal(int, int)
    clock_time = Signal(str)
    done_ok = Signal()
    failed = Signal(str, bool)

    def __init__(self, plan, detach_ok, sha, kind="upload"):
        super().__init__()
        self.plan, self.detach_ok, self.sha, self.kind = plan, detach_ok, sha, kind
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self.kind == "clock":
                now = datetime.datetime.now().astimezone()
                self.plan = core.build_clock_plan(1, now=now.timestamp(),
                                                  tz_offset=int(now.utcoffset().total_seconds()))
                self.clock_time.emit(now.strftime("%d.%m.%Y %H:%M:%S %z"))
            core.do_send(self.plan, self.detach_ok, self.sha,
                         progress=lambda i, n: self.progress.emit(i, n), cancel=lambda: self._cancel, kind=self.kind)
        except core.S360Error as e:
            self.failed.emit(str(e), e.uncertain)
            return
        except Exception as e:                      # непредвиденное: считаем состояние неизвестным
            self.failed.emit(repr(e), True)
            return
        self.done_ok.emit()


class DashboardPage(QWidget):
    configure_lcd = Signal()

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACING[1])
        cpu_box = QGroupBox("Температура процессора")
        cpu_layout = QVBoxLayout(cpu_box)
        self.cpu = QLabel("—")
        self.cpu.setObjectName("temperature")
        cpu_layout.addWidget(self.cpu)
        cpu_layout.addStretch()
        cpu_layout.addWidget(muted("Системный датчик CPU"))
        self.cpu_updated = muted("—")
        cpu_layout.addWidget(self.cpu_updated)
        lay.addWidget(cpu_box, 0, 0, 2, 1)
        box = QGroupBox("Подключение")
        form = QFormLayout(box)
        self.lcd, self.cooler, self.last_lcd, self.svc = (QLabel() for _ in range(4))
        form.addRow("LCD-дисплей", self.lcd)
        form.addRow("Контроллер охлаждения", self.cooler)
        lay.addWidget(box, 0, 1)
        upload_box = QGroupBox("Последнее отправленное")
        upload_form = QVBoxLayout(upload_box)
        self.last_file, self.last_upload = QLabel(), muted("")
        self.last_file.setTextFormat(Qt.TextFormat.PlainText)
        self.last_file.setWordWrap(True)
        self.last_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        upload_form.addWidget(self.last_file)
        upload_form.addWidget(self.last_upload)
        upload_form.addStretch()
        upload_form.addWidget(muted("Хост отправил. Проверь изображение на LCD: приём не подтверждён."))
        lay.addWidget(upload_box, 1, 1)
        lay.setColumnStretch(0, 1)
        lay.setColumnStretch(1, 2)
        lay.setRowStretch(2, 1)
        self.btn_configure = QPushButton("Настроить LCD")
        self.btn_configure.setObjectName("primary")
        self.btn_configure.clicked.connect(self.configure_lcd.emit)
        self.refresh()

    def refresh(self):
        yes = lambda ok: badge("Подключён", OK) if ok else badge("Не найден", BAD)
        self.lcd.setText(yes(core.usb_present(core.VID, core.PID)))
        self.cooler.setText(yes(core.usb_present(core.COOLER_VID, core.COOLER_PID)))
        self.last_file.setText(Path(self.s["last_file"]).name if self.s.get("last_file") else "—")
        when = self.s.get("last_upload_time")
        if when is not None:
            when = datetime.datetime.fromtimestamp(when).strftime("%d.%m.%Y %H:%M:%S")
            self.last_upload.setText(f"{when} — хост отправил; результат на экране не подтверждён")
        else:
            self.last_upload.setText("—")
        st = core.read_state()
        names = {"HOST_COMPLETE_UNCONFIRMED": badge("хост отправил (экран не подтверждён)", WARN),
                 "IN_PROGRESS": badge("не завершена — состояние неизвестно", BAD),
                 "UNCERTAIN": badge("ошибка — состояние неизвестно", BAD),
                 "NOT_STARTED": badge("не началась", OK)}
        if st:
            when = datetime.datetime.fromtimestamp(st.get("time", 0)).strftime("%d.%m %H:%M:%S")
            kind = st.get("kind", "upload")
            tag = (st.get("sha256") or "") if kind == "direction" else (st.get("sha256") or "")[:8]
            what = {"direction": "поворот экрана", "clock": "часы", "brightness": "яркость"}.get(kind, "загрузка")
            self.last_lcd.setText(f"{what}: {names.get(st['status'], st['status'])} — {when}" + (f", {tag}" if tag else ""))
        else:
            self.last_lcd.setText("нет данных")
        if not unit_installed():
            self.svc.setText(badge("не установлен (запусти install.sh)", WARN))
        else:
            active = systemctl("is-active", UNIT)[1] == "active"
            self.svc.setText(badge("работает", OK) if active else badge("остановлен", WARN))
        try:
            self.cpu.setText(f"{core.read_cpu_temp()} °C")
            self.cpu_updated.setText("Обновлено только что")
        except core.S360Error as e:
            self.cpu.setText("—")
            self.cpu_updated.setText("Датчик CPU недоступен")
            self.cpu.setToolTip(str(e))


class LcdPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.src = None
        self.pending_file = None
        self.pending_brightness = None
        self.pending_clock = None
        self.worker = None
        self.tmp = tempfile.TemporaryDirectory()
        page = QVBoxLayout(self)
        page.addWidget(QLabel("Настройка экрана"))
        modes = QHBoxLayout()
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        self.mode_buttons = {}
        for title, mode in (("Изображение / GIF", "media"), ("Мониторинг", "monitor"), ("Часы", "clock")):
            button = QPushButton(title)
            self.mode_buttons[mode] = button
            button.setCheckable(True)
            mode_group.addButton(button)
            button.setChecked(mode == self.s["lcd_mode"])
            if mode == "monitor":
                button.setEnabled(False)
                button.setToolTip("Этот режим экрана не поддерживается")
            else:
                button.clicked.connect(lambda checked=False, mode=mode: self.select_mode(mode))
            modes.addWidget(button)
        page.addLayout(modes)
        page.addWidget(muted("Мониторинг пока не поддерживается"))

        # Прокрутка сохраняет доступ к параметрам при раскрытии и на небольших экранах.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        outer = QHBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        page.addWidget(scroll, 1)

        preview_box = QGroupBox("Предпросмотр")
        self.preview_box = preview_box
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addStretch()
        self.lbl_prev = QLabel("Предпросмотр")
        self.lbl_prev.setObjectName("preview")
        self.lbl_prev.setFixedSize(core.W + 4, core.H + 4)
        self.lbl_prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.lbl_prev, 0, Qt.AlignmentFlag.AlignHCenter)
        self.lbl_orientation = muted("")
        self.lbl_orientation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.lbl_orientation)
        self.lbl_device_orientation = muted("")
        self.update_orientation()
        preview_layout.addWidget(self.lbl_device_orientation)
        preview_layout.addWidget(muted("Первый кадр после программной обработки. Поворот картинки по часовой стрелке "
                                       "применяется к файлу; аппаратный поворот LCD задаётся отдельно."))
        preview_layout.addStretch()
        outer.addWidget(preview_box, 2)

        right = QVBoxLayout()
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Медиафайл")
        form = QFormLayout(box)
        self.btn_pick = QPushButton("Выбрать файл")
        self.lbl_file = muted("Файл не выбран")
        self.lbl_file.setTextFormat(Qt.TextFormat.PlainText)
        self.cmb_scale = QComboBox()
        for title, key in (("Заполнить (с обрезкой)", "fill"), ("Вписать (полосы цвета фона)", "fit"),
                           ("Растянуть (искажает)", "stretch")):
            self.cmb_scale.addItem(title, key)
        self.cmb_scale.setCurrentIndex(max(0, self.cmb_scale.findData(self.s["scale"])))
        self.sld_x, self.sld_y = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        for sld, key in ((self.sld_x, "focus_x"), (self.sld_y, "focus_y")):
            sld.setRange(0, 100)
            sld.setValue(int(float(self.s[key]) * 100))
        self.cmb_rot = QComboBox()
        for deg in (0, 90, 180, 270):
            self.cmb_rot.addItem(f"{deg}°", deg)
        self.cmb_rot.setCurrentIndex(max(0, self.cmb_rot.findData(int(self.s["rotate"]))))
        self.cmb_bg = QComboBox()
        for title, key in (("Чёрный", "black"), ("Белый", "white"), ("Свой цвет", "custom")):
            self.cmb_bg.addItem(title, key)
        background = str(self.s["background"])
        self.custom_background = background if background not in ("black", "white") else "#808080"
        self.cmb_bg.setCurrentIndex(max(0, self.cmb_bg.findData(
            background if background in ("black", "white") else "custom")))
        self.btn_bg = QPushButton("Выбрать цвет…")
        self.bg_sample = QLabel()
        self.bg_sample.setFixedSize(24, 24)
        bg_row = QHBoxLayout()
        bg_row.addWidget(self.cmb_bg, 1)
        bg_row.addWidget(self.bg_sample)
        bg_row.addWidget(self.btn_bg)
        self.spn_trim = QSpinBox()
        self.spn_trim.setRange(0, 3)
        self.spn_trim.setValue(int(self.s["edge_trim"]))
        self.edt_fps = QLineEdit(str(self.s["fps"] or ""))
        self.edt_fps.setPlaceholderText("15 (проверено; пусто = как в GIF)")
        self.spn_sec = QDoubleSpinBox()
        self.spn_sec.setRange(0, 60)
        self.spn_sec.setSingleStep(0.05)
        self.spn_sec.setDecimals(2)
        self.spn_sec.setSpecialValueText("без обрезки")
        self.spn_sec.setValue(float(self.s["max_seconds"] or 0))
        self.chk_select = QCheckBox("Выбор слота перед загрузкой (без него экран не менялся)")
        self.chk_select.setChecked(bool(self.s["with_select"]))
        self.chk_detach = QCheckBox("Разрешить временно отсоединить HID-драйвер от LCD (нужно для передачи)")
        form.addRow(self.lbl_file, self.btn_pick)
        form.addRow("Масштабирование", self.cmb_scale)
        form.addRow("Поворот картинки (программный)", self.cmb_rot)
        form.addRow("Фон прозрачности", bg_row)
        form.addRow("Кадрирование X", self.sld_x)
        form.addRow("Кадрирование Y", self.sld_y)

        self.btn_advanced = QToolButton()
        self.btn_advanced.setText("Дополнительные параметры")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_advanced.setArrowType(Qt.ArrowType.RightArrow)
        form.addRow(self.btn_advanced)
        self.advanced = QWidget()
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addRow("FPS", self.edt_fps)
        advanced_form.addRow("Длительность, с", self.spn_sec)
        advanced_form.addRow("Подрезка края, px", self.spn_trim)
        advanced_form.addRow(muted("На этом экране воспроизводились файлы 20/3, 10 и 15 fps до ~2,5 с, "
                                   "а также GIF 60 FPS / 8,95 с. Это наблюдения, не предел длительности "
                                   "и не гарантия для других файлов."))
        advanced_form.addRow(self.chk_select)
        advanced_form.addRow(self.chk_detach)
        self.cmb_dir = QComboBox()
        for deg in core.DIRECTIONS:
            self.cmb_dir.addItem(f"{deg}°", deg)
        self.cmb_dir.setCurrentIndex(max(0, self.cmb_dir.findData(int(self.s["device_direction"]))))
        self.btn_dir = QPushButton("Применить поворот экрана")
        self.pending_dir = None
        advanced_form.addRow("Поворот LCD (аппаратный, эксперимент)", self.cmb_dir)
        advanced_form.addRow(self.btn_dir)
        advanced_form.addRow(muted("0°/180° подтверждены захватом; 90°/270° — только анализом DLL. Неизвестно, сохранится ли поворот после "
                                   "выключения. Для 90°/270° файлы грузятся в слот R1 — после смены перезагрузи картинку."))
        self.advanced.hide()
        form.addRow(self.advanced)
        self.btn_send = QPushButton("Загрузить на экран")
        self.btn_send.setObjectName("primary")
        form.addRow(self.btn_send)
        pl.addWidget(box)
        right.addWidget(self.panel)
        self.clock_panel = QGroupBox("Часы · стиль №1")
        clock_form = QFormLayout(self.clock_panel)
        clock_form.addRow(muted("Локально выбран режим «Часы». Стиль №1 подтверждён захватом MSI Center."))
        self.lbl_clock_time = muted("")
        self.lbl_clock_status = muted("Часы из этого сеанса ещё не отправлялись. Состояние LCD не считано.")
        self.chk_clock_detach = QCheckBox(self.chk_detach.text())
        self.chk_clock_detach.toggled.connect(self.chk_detach.setChecked)
        self.chk_detach.toggled.connect(self.chk_clock_detach.setChecked)
        self.btn_clock = QPushButton("Включить часы")
        self.btn_clock.setObjectName("primary")
        self.btn_clock.clicked.connect(self.apply_clock)
        clock_form.addRow(self.lbl_clock_time)
        clock_form.addRow(muted("Перед отправкой время будет обновлено из системы. Автосинхронизации нет. "
                               "Выбор режима здесь сохраняется локально и сам по себе не меняет LCD. "
                               "Возврат к медиа — через загрузку файла с выбором слота."))
        clock_form.addRow(self.chk_clock_detach)
        clock_form.addRow(self.btn_clock)
        clock_form.addRow(self.lbl_clock_status)
        right.addWidget(self.clock_panel)
        self.btn_cancel = QPushButton("Остановить")
        self.btn_cancel.setEnabled(False)
        self.prog = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        right.addWidget(self.btn_cancel)
        right.addWidget(self.prog)
        right.addWidget(self.log)

        self.brightness_panel = QGroupBox("Яркость и питание LCD")
        bl = QFormLayout(self.brightness_panel)
        self.chk_lcd = QCheckBox("Включить LCD (локальный выбор)")
        self.chk_lcd.setChecked(bool(self.s["lcd_enabled"]))
        self.sld_brightness = QSlider(Qt.Orientation.Horizontal)
        self.sld_brightness.setRange(0, 100)
        self.sld_brightness.setValue(int(self.s["brightness"]) if self.chk_lcd.isChecked() else 0)
        self.lbl_brightness = muted("")
        self.lbl_brightness_status = muted("Состояние LCD не считано. Подтверждения от устройства нет.")
        self.btn_brightness = QPushButton("Применить яркость / питание")
        bl.addRow(self.chk_lcd)
        bl.addRow("Яркость", self.sld_brightness)
        bl.addRow(self.lbl_brightness)
        bl.addRow(muted("Запись во флеш экрана, не двигай ползунок непрерывно. "
                        "Отправка только по кнопке «Применить» с подтверждением. "
                        "0% = выключение; включение восстанавливает последнюю ненулевую яркость."))
        bl.addRow(self.btn_brightness)
        bl.addRow(self.lbl_brightness_status)
        self.update_brightness_label()
        self.sld_brightness.valueChanged.connect(self.brightness_changed)
        self.chk_lcd.toggled.connect(self.lcd_toggled)
        self.btn_brightness.clicked.connect(self.apply_brightness)
        right.addWidget(self.brightness_panel)
        right.addStretch()
        outer.addLayout(right, 3)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.update_preview)
        self.btn_pick.clicked.connect(self.pick)
        self.btn_send.clicked.connect(self.send)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_dir.clicked.connect(self.apply_direction)
        for w in (self.cmb_scale, self.cmb_rot):
            w.currentIndexChanged.connect(self.changed)
        self.cmb_bg.currentIndexChanged.connect(self.changed)
        self.btn_bg.clicked.connect(self.pick_background)
        self.btn_advanced.toggled.connect(self.toggle_advanced)
        for w in (self.sld_x, self.sld_y):
            w.valueChanged.connect(self.changed)
        self.edt_fps.textChanged.connect(self.changed)
        self.spn_sec.valueChanged.connect(self.changed)
        self.spn_trim.valueChanged.connect(self.changed)
        self.chk_select.toggled.connect(self.changed)
        self.changed()
        self.show_mode()
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock_time)
        self.clock_timer.start(1000)  # Только подпись GUI; команд устройству не отправляет.
        self.update_clock_time()

    def show_mode(self):
        clock = self.s["lcd_mode"] == "clock"
        self.clock_panel.setVisible(clock)
        self.panel.setVisible(not clock)
        self.preview_box.setVisible(not clock)
        self.btn_send.setVisible(not clock)
        self.btn_clock.setVisible(clock)

    def select_mode(self, mode):
        if self.worker is not None:
            return
        self.s["lcd_mode"] = mode
        self.show_mode()
        try:
            save_ui_settings(self.s)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))

    def update_clock_time(self):
        if self.pending_clock is None:
            self.lbl_clock_time.setText("Местное время для LCD: " +
                                        datetime.datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %z"))

    def clock_time_ready(self, text):
        self.pending_clock = text
        self.lbl_clock_time.setText("Время отправки: " + text)
        self.lbl_clock_status.setText("Отправка хостом: стиль №1, " + text + "…")

    def apply_clock(self):
        if self.worker is not None or self.s["lcd_mode"] != "clock":
            return
        if not self.confirm_previous():
            return
        answer = QMessageBox.question(
            self, "Часы на LCD", "Отправить местное время системы и включить часы, стиль №1?\n"
            "Время будет получено непосредственно перед отправкой. Подтверждения от LCD нет.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_clock = ""
        self.lbl_clock_status.setText("Подготовка отправки часов…")
        self.start_worker([], "clock:1", "clock")

    def update_brightness_label(self):
        self.lbl_brightness.setText(
            f"Локально выбрано: {self.sld_brightness.value()}% · "
            f"последняя ненулевая: {self.s['brightness']}%. Не считано из LCD.")

    def brightness_changed(self, value):
        if value > 0:
            self.s["brightness"] = value
        self.s["lcd_enabled"] = value > 0
        self.chk_lcd.blockSignals(True)
        self.chk_lcd.setChecked(value > 0)
        self.chk_lcd.blockSignals(False)
        self.update_brightness_label()
        try:
            save_ui_settings(self.s)
        except OSError as e:
            self.lbl_brightness_status.setText(f"Локальные настройки не сохранены: {e}")

    def lcd_toggled(self, enabled):
        self.sld_brightness.setValue(int(self.s["brightness"]) if enabled else 0)

    def apply_brightness(self):
        if self.worker is not None:
            return
        value = self.sld_brightness.value()
        if not self.confirm_previous():
            return
        answer = QMessageBox.question(
            self, "Яркость и питание LCD",
            f"Отправить яркость {value}%" + (" (выключение LCD)" if value == 0 else "") +
            "?\nЗапись во флеш экрана, не двигай ползунок непрерывно.\n"
            "Подтверждения приёма от LCD нет.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_brightness = value
        self.lbl_brightness_status.setText(f"Отправка хостом: {value}%…")
        self.start_worker(core.build_brightness_plan(value), f"brightness:{value}", "brightness")

    def toggle_advanced(self, expanded):
        self.advanced.setVisible(expanded)
        self.btn_advanced.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def background_value(self):
        return self.custom_background if self.cmb_bg.currentData() == "custom" else self.cmb_bg.currentData()

    def pick_background(self):
        color = QColorDialog.getColor(QColor(self.background_value()), self, "Фон прозрачности")
        if color.isValid():
            self.custom_background = color.name()
            self.cmb_bg.setCurrentIndex(self.cmb_bg.findData("custom"))
            self.changed()

    def update_orientation(self):
        self.lbl_orientation.setText(f"{core.W} × {core.H} px · поворот картинки {self.s['rotate']}°")
        self.lbl_device_orientation.setText(
            f"Аппаратный поворот LCD: {self.s['device_direction']}° (локальная настройка; приём не подтверждён)")

    # --- настройки
    def fps_value(self):
        t = self.edt_fps.text().strip()
        if not t:
            return None
        if not re.fullmatch(r"\d+(\.\d+)?(/\d+)?", t) or t.startswith("0"):
            raise core.S360Error("FPS: число или дробь, например 10 или 20/3")
        return t

    def apply_enc(self):
        core.ENC.update(fps=self.fps_value(), max_seconds=self.spn_sec.value() or None,
                        scale=self.cmb_scale.currentData(), focus_x=self.sld_x.value() / 100.0,
                        focus_y=self.sld_y.value() / 100.0, rotate=int(self.cmb_rot.currentData()),
                        background=self.background_value(),
                        edge_trim=self.spn_trim.value())

    def changed(self, *_):
        self.s["rotate"] = int(self.cmb_rot.currentData())
        self.update_orientation()
        self.btn_bg.setEnabled(self.cmb_bg.currentData() == "custom")
        color = QColor(self.background_value())
        self.bg_sample.setStyleSheet(f"background-color: {color.name()}; border: 1px solid #8b93a3; border-radius: 4px;")
        self.bg_sample.setToolTip(self.background_value())
        fill = self.cmb_scale.currentData() == "fill"
        self.sld_x.setEnabled(fill)
        self.sld_y.setEnabled(fill)
        try:
            self.s.update(fps=self.fps_value(), max_seconds=self.spn_sec.value() or None,
                          scale=self.cmb_scale.currentData(), focus_x=self.sld_x.value() / 100.0,
                          focus_y=self.sld_y.value() / 100.0, with_select=self.chk_select.isChecked(),
                          rotate=int(self.cmb_rot.currentData()),
                          background=self.background_value(),
                          edge_trim=self.spn_trim.value())
            save_ui_settings(self.s)
        except (core.S360Error, OSError):
            pass
        self.timer.start(300)

    # --- предпросмотр
    def pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбор файла", str(Path.home()), "GIF/MP4 (*.gif *.mp4)")
        if path:
            self.src = path
            self.lbl_file.setText(Path(path).name)
            self.lbl_file.setToolTip(path)
            self.update_preview()

    def update_preview(self):
        if not self.src or self.worker is not None:
            return
        try:
            self.apply_enc()
            png = Path(self.tmp.name) / "preview.png"
            core.preview_png(Path(self.src), png)
            self.lbl_prev.setPixmap(QPixmap(str(png)))
        except (core.S360Error, OSError) as e:
            self.lbl_prev.setText("нет предпросмотра")
            self.log.appendPlainText(f"предпросмотр: {e}")

    # --- отправка
    def confirm_previous(self):
        st = core.read_state()
        if st and st["status"] in ("IN_PROGRESS", "UNCERTAIN"):
            r = QMessageBox.question(
                self, "Прошлая операция не завершена",
                f"Прошлая операция: {st['status']}. Состояние экрана неизвестно.\nЯ проверил экран и хочу продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            return r == QMessageBox.StandardButton.Yes
        return True

    def start_worker(self, plan, sha, kind):
        self.pending_file = Path(self.src).name if kind == "upload" else None
        self.btn_send.setEnabled(False)
        self.btn_clock.setEnabled(False)
        self.panel.setEnabled(False)
        self.brightness_panel.setEnabled(False)
        self.clock_panel.setEnabled(False)
        for button in self.mode_buttons.values():
            button.setEnabled(False)
        self.btn_dir.setEnabled(False)
        self.cmb_dir.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.prog.setValue(0)
        self.log.appendPlainText(f"отправка ({kind}): {2 if kind == 'clock' else len(plan)} пакетов, {sha[:16]}")
        self.worker = SendWorker(plan, self.chk_detach.isChecked(), sha, kind)
        self.worker.clock_time.connect(self.clock_time_ready)
        self.worker.progress.connect(self.on_progress)
        self.worker.done_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def send(self):
        if not self.src:
            QMessageBox.information(self, "S360", "Сначала выберите файл.")
            return
        if self.worker is not None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.apply_enc()
            data = core.load_media(Path(self.src))
        except (core.S360Error, OSError) as e:
            QMessageBox.warning(self, "Не удалось подготовить файл", str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        sha = core.sha256(data)
        if not self.confirm_previous():
            return
        slot = core.slot_for_direction(int(self.s["device_direction"]))
        ref = "совпадает с проверенным эталоном" if sha == core.REFERENCE_SHA256 else \
            "НЕ эталонный файл: прошивка принимала файлы такого вида (baseline), но каждый новый — на твой риск"
        dur, frames, rate = core.probe_mp4(data)
        info = f"\nДлительность: {dur:.2f} с, кадров: {frames}, fps: {rate}" if dur is not None else ""
        extra = ("\n\nЭти параметры не проверены на наших файлах (наблюдались до ~2,5 с при 20/3, 10, 15 fps "
                 "и отдельный GIF 60 FPS / 8,95 с): возможно зависание картинки; "
                 "восстановить можно через MSI Center.") if core.beyond_tested(dur, rate) else ""
        r = QMessageBox.question(
            self, "Подтверждение",
            f"Файл: {len(data)} байт{info}\nSHA256: {sha[:16]}…\n{ref}\n\nЭто ПЕРЕЗАПИШЕТ слот на экране "
            f"({slot.decode()}). Отката нет.{extra}\nОтправить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        plan = ([] if not self.chk_select.isChecked() else core.build_select_plan()) + core.build_upload_plan(data, slot)
        self.pending_dir = None
        self.start_worker(plan, sha, "upload")

    def apply_direction(self):
        if self.worker is not None:
            return
        deg = int(self.cmb_dir.currentData())
        if not self.confirm_previous():
            return
        r = QMessageBox.question(
            self, "Поворот экрана (эксперимент)",
            f"Поворот экрана {deg}°: команда, сохранение во флеш и повторный выбор слота S7.\n"
            f"0°/180° подтверждены захватом; 90°/270° — только анализом DLL. "
            f"Неизвестно, сохранится ли поворот после выключения. Вернуть можно командой на 0° или через MSI Center.\n"
            f"Для 90°/270° файлы будут грузиться в слот R1 (по тому же анализу). Отправить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        self.pending_dir = deg
        self.start_worker(core.build_direction_plan(deg), f"direction:{deg}", "direction")

    def cancel(self):
        if self.worker is not None:
            self.worker.cancel()
            self.log.appendPlainText("отмена запрошена (между пакетами)…")

    def on_progress(self, i, n):
        self.prog.setMaximum(n)
        self.prog.setValue(i)

    def finish_ui(self):
        self.worker.wait()
        self.worker = None
        self.btn_send.setEnabled(True)
        self.btn_clock.setEnabled(True)
        self.panel.setEnabled(True)
        self.brightness_panel.setEnabled(True)
        self.clock_panel.setEnabled(True)
        for mode, button in self.mode_buttons.items():
            button.setEnabled(mode != "monitor")
        self.btn_dir.setEnabled(True)
        self.cmb_dir.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.changed()

    def on_done(self):
        self.finish_ui()
        if self.pending_clock is not None:
            self.lbl_clock_status.setText(
                f"Хост отправил: часы, стиль №1, {self.pending_clock}. Приём LCD не подтверждён.")
            self.pending_clock = None
        if self.pending_brightness is not None:
            self.lbl_brightness_status.setText(
                f"Хост отправил: {self.pending_brightness}%. Приём LCD не подтверждён.")
            self.pending_brightness = None
        if self.pending_dir is not None:
            self.s["device_direction"] = self.pending_dir
            self.update_orientation()
            self.pending_dir = None
        elif self.pending_file is not None:
            self.s["last_file"] = self.pending_file
            self.s["last_upload_time"] = datetime.datetime.now().timestamp()
            self.pending_file = None
        try:
            save_ui_settings(self.s)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))
        self.log.appendPlainText("хост отправил все пакеты")
        QMessageBox.information(self, "Готово (хост)", "Все пакеты отправлены. Подтверждения от экрана нет — посмотрите, "
                                "что на нём отображается.")

    def on_failed(self, msg, uncertain):
        if self.pending_clock is not None:
            self.lbl_clock_status.setText("Ошибка отправки часов; состояние LCD неизвестно. " + msg)
            self.pending_clock = None
        if self.pending_brightness is not None:
            self.lbl_brightness_status.setText(
                "Ошибка отправки яркости; состояние LCD неизвестно. Выбор остаётся локальным. " + msg)
            self.pending_brightness = None
        self.pending_dir = None
        self.pending_file = None
        self.finish_ui()
        self.log.appendPlainText(f"ОШИБКА: {msg}")
        QMessageBox.critical(self, "Ошибка", msg + ("\n\nСостояние экрана неизвестно. Повторять автоматически не нужно."
                                                    if uncertain else ""))


class CurveGraph(QWidget):
    changed = Signal(int, int, int)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.values = {"temp": [0] * 4, "duty": [0] * 4}
        self.dragging = None
        self.setMinimumSize(220, 200)

    def plot_rect(self):
        margin = self.fontMetrics().height()
        return QRectF(36, margin + 12, self.width() - 52, self.height() - 3 * margin - 24)

    def point_position(self, index):
        r = self.plot_rect()
        return QPointF(r.left() + self.values["temp"][index] * r.width() / 100,
                       r.bottom() - self.values["duty"][index] * r.height() / 100)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.plot_rect()
        if self.channel == "pump":
            y = r.bottom() - core.PUMP_MIN_DUTY * r.height() / 100
            p.fillRect(QRectF(r.left(), y, r.width(), r.bottom() - y), QColor("#292c33"))
            p.setPen(QColor("#9aa3b2"))
            p.drawText(QPointF(r.left() + 5, y + 16), "ниже 60% нельзя")
        for value in (0, 25, 50, 75, 100):
            x = r.left() + value * r.width() / 100
            y = r.bottom() - value * r.height() / 100
            p.setPen(QColor("#30343b"))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#8b93a3"))
            p.drawText(QPointF(x - 8, r.bottom() + 18), str(value))
            if value in (0, 50, 100):
                p.drawText(QPointF(4, y + 4), str(value))
        p.drawText(QPointF(r.right() - 18, r.bottom() + 18 + self.fontMetrics().height()), "°C")
        p.drawText(QPointF(4, self.fontMetrics().ascent()), "%")
        p.setClipRect(r.adjusted(-6, -6, 6, 6))
        p.setPen(QPen(QColor("#f06470"), 2.5))
        points = [self.point_position(i) for i in range(4)]
        p.drawPolyline(QPolygonF([QPointF(r.left(), points[0].y()), *points,
                                 QPointF(r.right(), points[-1].y())]))
        p.setBrush(QColor("#1a1d23"))
        for point in points:
            p.drawEllipse(point, 5, 5)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            distances = [(self.point_position(i) - event.position()).manhattanLength() for i in range(4)]
            nearest = min(range(4), key=distances.__getitem__)
            if distances[nearest] <= 18:
                self.dragging = nearest

    def mouseMoveEvent(self, event):
        if self.dragging is None:
            return
        r = self.plot_rect()
        temp = round(max(0, min(100, (event.position().x() - r.left()) * 100 / r.width())))
        duty = round(max(0, min(100, (r.bottom() - event.position().y()) * 100 / r.height())))
        if self.channel == "pump":
            duty = max(core.PUMP_MIN_DUTY, duty)
        # Порядок соседних точек по-прежнему проверяет ядро.
        self.changed.emit(self.dragging, temp, duty)
        QToolTip.showText(event.globalPosition().toPoint(), f"{temp}° · {duty}%", self)

    def mouseReleaseEvent(self, event):
        self.dragging = None
        QToolTip.hideText()


class CurveCard(QGroupBox):
    changed = Signal()

    def __init__(self, channel, title, description):
        super().__init__(title)
        self.channel = channel
        layout = QVBoxLayout(self)
        layout.addWidget(muted(description))
        self.graph = CurveGraph(channel)
        layout.addWidget(self.graph)
        self.chips_widget = QWidget()
        chips = QGridLayout(self.chips_widget)
        chips.setContentsMargins(0, 0, 0, 0)
        self.chips = []
        for index in range(4):
            button = QPushButton()
            button.setStyleSheet("padding: 7px 5px;")
            button.clicked.connect(lambda checked=False, i=index: self.edit_point(i))
            chips.addWidget(button, index // 2, index % 2)
            self.chips.append(button)
        layout.addWidget(self.chips_widget)
        self.placeholder = muted("Кривую задаёт сама водянка — программа её не видит")
        self.placeholder.setMinimumHeight(200)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.placeholder)
        self.graph.changed.connect(self.move_point)

    def set_values(self, values):
        self.graph.values = deepcopy(values)
        for i, chip in enumerate(self.chips):
            chip.setText(f'{values["temp"][i]}° · {values["duty"][i]}%')
        self.graph.update()

    def move_point(self, index, temp, duty):
        values = deepcopy(self.graph.values)
        values["temp"][index], values["duty"][index] = temp, duty
        self.set_values(values)
        self.changed.emit()

    def edit_point(self, index):
        dialog = QDialog(self)
        dialog.setWindowTitle("Изменить точку")
        form = QFormLayout(dialog)
        inputs = {}
        for key, title in (("temp", "Температура, °C"), ("duty", "Обороты, %")):
            spin = QSpinBox()
            spin.setRange(-100000, 100000)
            spin.setValue(self.graph.values[key][index])
            form.addRow(title, spin)
            inputs[key] = spin
        if self.channel == "pump":
            form.addRow("", muted("Минимум 60%; меньшее значение будет поднято до 60%."))
        buttons = QDialogButtonBox()
        buttons.addButton("Готово", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            duty = inputs["duty"].value()
            if self.channel == "pump":
                duty = max(core.PUMP_MIN_DUTY, duty)
            self.move_point(index, inputs["temp"].value(), duty)

    def set_custom(self, custom):
        self.graph.setVisible(custom)
        self.chips_widget.setVisible(custom)
        self.placeholder.setVisible(not custom)


class CoolingPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.service_error = ""
        self.profile_error = ""
        page = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.content = QWidget()
        scroll.setWidget(self.content)
        page.addWidget(scroll)
        lay = QVBoxLayout(self.content)
        lay.addWidget(QLabel("Охлаждение"))
        self.mode_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for key, description in (("silent", "Тише всего"), ("balance", "На каждый день"),
                                 ("game", "Максимум охлаждения"), ("custom", "Настрой сам")):
            b = QPushButton(f"{COOLING_TITLES[key]}\n{description}")
            b.setProperty("coolingCard", True)
            b.setCheckable(True)
            self.group.addButton(b)
            self.buttons[key] = b
            self.mode_row.addWidget(b, 1)
        self.buttons.get(self.s["cooling_mode"], self.buttons["balance"]).setChecked(True)
        lay.addLayout(self.mode_row)
        self.hint = muted("")
        lay.addWidget(self.hint)
        self.curves_box = QWidget()
        self.cards_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.curves_box)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards = {}
        for channel, title, description in (
                ("radiator1", "Вентилятор радиатора", "Все три вентилятора вместе"),
                ("waterblock", "Вентилятор водоблока", "Маленький вентилятор на помпе"),
                ("pump", "Помпа", "Минимум 60% — защита процессора")):
            card = CurveCard(channel, title, description)
            self.cards[channel] = card
            self.cards_layout.addWidget(card, 1)
            card.changed.connect(self.update_dirty)
        lay.addWidget(self.curves_box)
        self.curves_status = muted("")
        try:
            curves = core.load_curves() if core.CURVES_FILE.exists() else core.DEFAULT_CURVES
            self.set_curves(curves)
        except (core.S360Error, OSError, AttributeError, TypeError) as e:
            self.profile_error = str(e)
            self.set_curves(core.DEFAULT_CURVES)
            self.curves_status.setText("Не удалось загрузить профиль. Исправь файл и открой вкладку заново. "
                                       "Показан образец; исходный файл не изменён.")
        self.saved_curves = deepcopy(self.curves)
        self.saved_mode = self.mode()
        self.cooler_connection = muted("")
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.dirty = muted("")
        self.service_badge = QLabel()
        self.service_explanation = ""
        actions = QHBoxLayout()
        actions.addWidget(self.service_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addWidget(self.status, 1)
        self.btn_save_curves = QPushButton("Сохранить")
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.setObjectName("primary")
        actions.addWidget(self.btn_save_curves)
        actions.addWidget(self.btn_apply)
        lay.addLayout(actions)
        self.btn_details = QToolButton()
        self.btn_details.setText("Подробности")
        self.btn_details.setCheckable(True)
        self.btn_details.setArrowType(Qt.ArrowType.RightArrow)
        lay.addWidget(self.btn_details)
        self.details = QWidget()
        dl = QVBoxLayout(self.details)
        dl.addWidget(self.cooler_connection)
        dl.addWidget(self.curves_status)
        dl.addWidget(self.dirty)
        dl.addWidget(muted(f"Сервис {UNIT}: режим d0 40/41, температура CPU d0 85 "
                           f"каждые {core.FEED_INTERVAL_S:g} с. Доступ через hidraw.\n"
                           f"Профиль: {core.CURVES_FILE}\nРежим: {core.COOLING_ENV}\n"
                           "Если установленная копия устарела, обнови её через ./install.sh вручную."))
        self.last_mode = muted("")
        dl.addWidget(self.last_mode)
        self.btn_service = QPushButton("Остановить передачу температуры")
        dl.addWidget(self.btn_service)
        self.chk_auto = QCheckBox("Запускать при входе в систему")
        dl.addWidget(self.chk_auto)
        self.btn_reset_curves = QPushButton("Сбросить видимые точки к образцу")
        dl.addWidget(self.btn_reset_curves)
        dl.addWidget(muted("RPM и температура жидкости недоступны: чтение не реализовано.\n"
                           "Поведение после остановки передачи температуры не проверено владельцем."))
        lay.addWidget(self.details)
        self.details.hide()
        self.btn_details.toggled.connect(self.show_details)
        self.btn_save_curves.clicked.connect(self.save_curves)
        self.btn_reset_curves.clicked.connect(self.reset_curves)
        self.btn_apply.clicked.connect(self.apply)
        self.btn_service.clicked.connect(self.stop)
        self.chk_auto.clicked.connect(self.autostart)
        for button in self.buttons.values():
            button.toggled.connect(self.mode_changed)
        lay.addStretch()
        self.mode_changed()
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 960
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        self.cards_layout.setDirection(direction)
        self.mode_row.setDirection(QBoxLayout.Direction.TopToBottom if self.width() < 700
                                   else QBoxLayout.Direction.LeftToRight)

    def show_details(self, checked):
        self.details.setVisible(checked)
        self.btn_details.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def mode(self):
        return next((key for key, b in self.buttons.items() if b.isChecked()), "balance")

    def mode_changed(self):
        custom = self.mode() == "custom"
        for card in self.cards.values():
            card.set_custom(custom)
        self.hint.setText("Тяни точки на графике или нажми на значение под ним и введи число."
                          if custom else "Готовый режим производителя. Для изменения выбери «Свой профиль».")
        self.btn_reset_curves.setEnabled(custom)
        self.update_dirty()

    def update_dirty(self):
        changed = self.mode() != self.saved_mode or self.raw_curves() != self.saved_curves
        self.dirty.setText("Изменения не сохранены" if changed else "")
        self.update_status()

    def update_status(self):
        note = self.curves_status.text() if self.mode() == "custom" else ""
        explanation = "; ".join(text for text in (self.service_explanation, self.dirty.text(), note) if text)
        self.status.setText(explanation)

    def set_service_status(self, title, explanation, color):
        self.service_badge.setText("● " + title)
        self.service_badge.setStyleSheet(f"color: {color}; background: {theme.COLORS['surface']}; border-radius: {theme.RADII[0]}px; padding: {theme.SPACING[0]}px;")
        self.service_explanation = explanation
        self.update_status()

    def set_curves(self, curves):
        self.curves = deepcopy(curves)
        for channel, card in self.cards.items():
            card.set_values(curves[channel])

    def reset_curves(self):
        for channel, card in self.cards.items():
            card.set_values(core.DEFAULT_CURVES[channel])
        self.update_dirty()

    def raw_curves(self):
        curves = deepcopy(self.curves)
        for channel, card in self.cards.items():
            curves[channel] = deepcopy(card.graph.values)
        return curves

    def editor_curves(self):
        try:
            return core.validate_curves(self.raw_curves())
        except core.S360Error as e:
            text = str(e)
            for channel, title in (("radiator1", "Вентилятор радиатора"), ("waterblock", "Вентилятор водоблока"),
                                   ("pump", "Помпа"), ("radiator2", "Служебный канал"),
                                   ("radiator3", "Служебный канал")):
                text = text.replace(channel, title)
            raise core.S360Error(text) from e

    def write_curves(self, curves):
        core.CURVES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=core.CURVES_FILE.parent,
                                         prefix=".curves-", suffix=".json", delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(json.dumps(curves, ensure_ascii=False, indent=2) + "\n")
                f.close()
                temporary.replace(core.CURVES_FILE)
            finally:
                temporary.unlink(missing_ok=True)
        self.saved_curves = deepcopy(curves)

    def save_local(self):
        if self.mode() == "custom":
            if self.profile_error:
                raise core.S360Error("Файл профиля повреждён. Исправь его и открой приложение заново: "
                                     "служебные каналы нельзя заменять образцом.")
            self.write_curves(self.editor_curves())
        self.s["cooling_mode"] = self.mode()
        save_ui_settings(self.s)
        self.saved_mode = self.mode()
        self.update_dirty()

    def save_curves(self):
        try:
            self.save_local()
            self.curves_status.setText("Для отправки нажми «Применить».")
        except (core.S360Error, OSError) as e:
            self.curves_status.setText(f"Профиль не сохранён: {e}")
            QMessageBox.warning(self, "Профиль не сохранён", str(e))
        self.update_status()

    def apply(self):
        self.start_service("restart")

    def start_service(self, command):
        if not unit_installed():
            self.refresh()
            return
        mode = self.mode()
        try:
            if mode == "custom":
                if self.profile_error:
                    raise core.S360Error("Файл профиля повреждён. Исправь его и открой приложение заново.")
                if not core.CURVES_FILE.exists():
                    raise core.S360Error("Профиль ещё не сохранён. Нажми «Сохранить», затем «Применить».")
                # Существующий файл тоже должен быть пригоден для сервиса; не затираем повреждение.
                core.load_curves()
                curves = self.editor_curves()
        except (core.S360Error, OSError, AttributeError, TypeError) as e:
            QMessageBox.warning(self, "Некорректный профиль", str(e))
            return
        answer = QMessageBox.question(self, "Применить охлаждение",
            f"Применить «{COOLING_TITLES[mode]}» и запустить передачу температуры процессора?\n"
            "Работа режимов подтверждена владельцем; обратного чтения настроек нет.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not core.usb_present(core.COOLER_VID, core.COOLER_PID):
            QMessageBox.warning(self, "Устройство не найдено", "Контроллер охлаждения отключён. Режим не применён.")
            self.refresh()
            return
        try:
            if mode == "custom":
                self.write_curves(curves)
            core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            core.COOLING_ENV.write_text(f"MODE={mode}\n")
        except OSError as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            return
        self.service_error = ""
        self.set_service_status("Запускается", "Запускается передача температуры процессора…", WARN)
        code, out = systemctl(command, UNIT)
        if code != 0:
            self.service_error = out or "Не удалось запустить сервис"
            QMessageBox.warning(self, "Сервис не запущен", self.service_error)
        self.s["cooling_mode"] = mode
        try:
            save_ui_settings(self.s)
            self.saved_mode = mode
            self.update_dirty()
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))
        self.refresh()

    def stop(self):
        code, out = systemctl("stop", UNIT)
        if code != 0:
            QMessageBox.warning(self, "Сервис не остановлен", out or "Не удалось остановить")
        else:
            self.service_error = ""
        self.refresh()

    def autostart(self, checked):
        code, out = systemctl("enable" if checked else "disable", UNIT)
        if code != 0:
            QMessageBox.warning(self, "Ошибка", out)
        self.refresh()

    def journal_line(self):
        try:
            result = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "1", "--no-pager"],
                                    capture_output=True, text=True, timeout=10)
            return (result.stdout.strip() or result.stderr.strip() or "В журнале нет записей").splitlines()[-1]
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Журнал недоступен: {e}"

    def refresh(self):
        inst = unit_installed()
        connected = core.usb_present(core.COOLER_VID, core.COOLER_PID)
        self.cooler_connection.setText("Контроллер охлаждения подключён" if connected else "Контроллер охлаждения не найден")
        self.btn_apply.setEnabled(inst)
        self.btn_apply.setToolTip("" if inst else "Сервис не установлен: выполни ./install.sh вручную")
        self.chk_auto.setEnabled(inst)
        self.btn_service.setEnabled(False)
        try:
            last = COOLING_TITLES.get(json.loads(core.COOLING_STATE.read_text())["mode"], "неизвестный")
        except (OSError, ValueError, KeyError, TypeError):
            last = "нет данных"
        self.last_mode.setText(f"Последний отправленный режим: {last}. Обратного чтения с устройства нет.")
        if not inst:
            self.set_service_status("Остановлен", "Сервис не установлен — выполни ./install.sh вручную", WARN)
            self.chk_auto.setChecked(False)
            return
        code, state = systemctl("is-active", UNIT)
        # Restart=on-failure может скрыть падение за состоянием activating (auto-restart).
        restart_error = False
        if state == "activating":
            _, properties = systemctl("show", "--property=SubState,Result", UNIT)
            properties = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
            restart_error = (properties.get("SubState") == "auto-restart"
                             and properties.get("Result") not in (None, "success"))
        self.status.setToolTip("")
        self.chk_auto.setChecked(systemctl("is-enabled", UNIT)[1] == "enabled")
        if state not in ("active", "inactive", "failed", "activating", "reloading", "deactivating"):
            self.set_service_status("Неизвестно", "Статус сервиса неизвестен", WARN)
            self.status.setToolTip(state)
        elif state == "failed" or restart_error or self.service_error:
            self.set_service_status("Ошибка", "Ошибка сервиса: " + (self.service_error or "Передача температуры остановлена")
                                    + "; " + self.journal_line(), BAD)
        elif code == 0 and state == "active":
            self.set_service_status("Работает", "Сервис передачи температуры процессора работает; приём не подтверждён", OK)
            self.btn_service.setEnabled(True)
        elif state in ("activating", "reloading"):
            self.set_service_status("Запускается", "Запускается передача температуры процессора…", WARN)
        elif state in ("inactive", "deactivating"):
            self.set_service_status("Остановлен", "Передача температуры процессора остановлена — охлаждение без неё не проверено", WARN)
        else:
            self.set_service_status("Неизвестно", "Статус сервиса неизвестен", WARN)
            self.status.setToolTip(state)


class MonitoringPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        box = QGroupBox("Доступно")
        form = QFormLayout(box)
        self.cpu = QLabel()
        form.addRow("Температура CPU (hwmon)", self.cpu)
        lay.addWidget(box)
        na = QGroupBox("Недоступно")
        nl = QVBoxLayout(na)
        nl.addWidget(muted("Обороты помпы и вентиляторов, температура жидкости: статус кулера (d0 31) требует запроса "
                           "на запись, а соответствие каналов физическим вентиляторам не сверено с MSI Center — "
                           "показывать эти числа как достоверные нельзя."))
        lay.addWidget(na)
        lay.addStretch()
        self.refresh()

    def refresh(self):
        try:
            self.cpu.setText(f"{core.read_cpu_temp()} °C")
        except core.S360Error as e:
            self.cpu.setText(str(e))


class SettingsPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        lay = QVBoxLayout(self)
        box = QGroupBox("О программе")
        form = QFormLayout(box)
        for k, v in (("Версия", core.VERSION), ("Настройки", str(core.SETTINGS)), ("Журнал LCD", str(core.STATE)),
                     ("Состояние кулера", str(core.COOLING_STATE))):
            lbl = QLabel(v)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(k, lbl)
        lay.addWidget(box)
        btn = QPushButton("Сбросить настройки интерфейса")
        btn.clicked.connect(self.reset)
        lay.addWidget(btn)
        lay.addWidget(muted("Журнал LCD не удаляется кнопкой намеренно: он защищает от повторной записи при неизвестном "
                            "состоянии экрана."))
        lay.addStretch()

    def reset(self):
        try:
            core.save_settings(core.DEFAULT_SETTINGS)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сброшены", str(e))
            return
        QMessageBox.information(self, "S360", "Настройки сброшены. Перезапусти приложение.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("S360 Control")
        self.resize(1040, 720)
        settings = load_ui_settings()
        self.settings = settings
        overview = QTabWidget()
        dashboard = DashboardPage(settings)
        self.dashboard = dashboard
        self.overview_states = QStackedWidget()
        self.overview_states.addWidget(dashboard)
        self.state_heading = QLabel()
        self.state_heading.setObjectName("stateHeading")
        self.state_description = muted("")
        self.state_detail = QLabel()
        self.state_detail.setWordWrap(True)
        self.state_detail.setTextFormat(Qt.TextFormat.PlainText)
        state_page = QWidget()
        state_layout = QVBoxLayout(state_page)
        state_layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox()
        content = QVBoxLayout(box)
        content.setContentsMargins(*([theme.SPACING[2]] * 4))
        content.setSpacing(theme.SPACING[2])
        content.addWidget(self.state_heading)
        content.addWidget(self.state_description)
        content.addWidget(self.state_detail)
        content.addStretch()
        state_layout.addWidget(box, 1)
        self.overview_states.addWidget(state_page)
        overview.addTab(self.overview_states, "Состояние")
        overview.tabBar().hide()
        overview.addTab(MonitoringPage(), "Мониторинг")
        overview.addTab(SettingsPage(settings), "Настройки")
        menu = self.menuBar().addMenu("Дополнительно")
        menu.addAction("Мониторинг", lambda: overview.setCurrentIndex(1))
        menu.addAction("Настройки", lambda: overview.setCurrentIndex(2))
        menu.addAction("Обзор", lambda: overview.setCurrentIndex(0))
        overview.currentChanged.connect(self.tick)
        self.pages = [("Обзор", overview), ("LCD-дисплей", LcdPage(settings)),
                      ("Охлаждение", CoolingPage(settings))]
        self.stack = QStackedWidget()
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for name, page in self.pages:
            self.tabs.addTab(name)
            # LCD уже содержит собственную прокрутку; остальные старые страницы
            # прокручиваются внутри каркаса, не затрагивая нижнюю полосу.
            if isinstance(page, LcdPage):
                self.stack.addWidget(page)
            else:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(page)
                self.stack.addWidget(scroll)
        dashboard.configure_lcd.connect(lambda: self.stack.setCurrentIndex(1))
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[2], theme.SPACING[1])
        layout.setSpacing(theme.SPACING[1])
        product = QLabel("MEG CoreLiquid S360")
        product.setObjectName("product")
        layout.addWidget(product)
        layout.addWidget(self.tabs)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        layout.addWidget(self.page_title)
        layout.addWidget(self.stack, 1)
        self.footer = QWidget()
        self.footer.setObjectName("footer")
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, theme.SPACING[1], 0, 0)
        self.footer_stack = QStackedWidget()
        self.footer_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        footer_layout.addWidget(self.footer_stack)
        lcd, cooling = self.pages[1][1], self.pages[2][1]
        self.connection_status = QLabel()
        self.lcd_status = QLabel("Состояние LCD не считано. Приём не подтверждён.")
        self.lcd_badge = QLabel("● Не считано")
        self.overview_badge = QLabel("● Подключение")
        self.btn_check = QPushButton("Проверить подключение")
        self.btn_check.setObjectName("primary")
        self.btn_check.clicked.connect(self.tick)
        self.btn_first_run = QPushButton("Выбрать файл")
        self.btn_first_run.setObjectName("primary")
        self.btn_first_run.clicked.connect(self.complete_first_run)
        rows = (
            (self.overview_badge, self.connection_status, (dashboard.btn_configure, self.btn_check, self.btn_first_run)),
            (self.lcd_badge, self.lcd_status, (lcd.btn_cancel, lcd.btn_send, lcd.btn_clock)),
            (cooling.service_badge, cooling.status, (cooling.btn_save_curves, cooling.btn_apply)),
        )
        for status_badge, explanation, buttons in rows:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(theme.SPACING[1])
            status_badge.setObjectName("statusBadge")
            explanation.setObjectName("statusText")
            explanation.setWordWrap(False)
            explanation.setMinimumWidth(0)
            explanation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row_layout.addWidget(status_badge)
            row_layout.addWidget(explanation, 1)
            for button in buttons:
                # Переносим сами кнопки: обработчики и подтверждения остаются прежними.
                row_layout.addWidget(button)
            self.footer_stack.addWidget(row)
        lcd.log.textChanged.connect(self.update_lcd_footer)
        lcd.show_mode()
        layout.addWidget(self.footer)
        self.setCentralWidget(root)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self.on_row)
        self.stack.setCurrentIndex(0)
        self.on_row(self.stack.currentIndex())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(2000)

    def on_row(self, row):
        self.tabs.setCurrentIndex(row)
        self.footer_stack.setCurrentIndex(row)
        self.page_title.setText(self.pages[row][0])
        self.tick()

    def update_lcd_footer(self):
        lcd = self.pages[1][1]
        text = lcd.log.toPlainText().splitlines()
        self.lcd_status.setText((text[-1] + "; приём LCD не подтверждён") if text else
                                "Состояние LCD не считано. Приём не подтверждён.")
        self.lcd_status.setToolTip(self.lcd_status.text())
        # Пересказываем только журнал хоста, без утверждения о состоянии экрана.
        self.lcd_badge.setText("● Хост / журнал" if text else "● Не считано")

    def tick(self):
        self.refresh_overview_state()
        page = self.pages[self.stack.currentIndex()][1]
        if isinstance(page, QTabWidget):
            page = page.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()
        self.dashboard.refresh()
        cooling = self.pages[2][1]
        cooling.status.setToolTip(cooling.status.text())

    def refresh_overview_state(self):
        lcd = device_access(core.VID, core.PID, lcd=True)
        cooler = device_access(core.COOLER_VID, core.COOLER_PID)
        if "missing" in (lcd, cooler):
            state = "missing"
            title, heading = "Подключение устройства", "Устройство не найдено"
            description = "Проверь питание и внутренний USB-кабель S360."
            labels = {"missing": "—", "denied": "Нет доступа", "ready": "Подключён"}
            detail = f"LCD-дисплей: {labels[lcd]}\n\nКонтроллер охлаждения: {labels[cooler]}"
            status, explanation = "Не найдено", "S360 не обнаружен полностью"
        elif "denied" in (lcd, cooler):
            state = "denied"
            title, heading = "Подключение устройства", "Нет доступа к USB"
            description = "Установи правило доступа и переподключи кабель USB водянки."
            detail = "В терминале из папки приложения:\n\n./install.sh\n\nПосле установки нажми «Проверить подключение»."
            status, explanation = "Нет доступа", "Приложению нужны права на USB"
        elif not self.settings.get("first_run_done"):
            state = "first"
            title, heading = "Начало работы", "Настрой экран и охлаждение S360"
            description = "Устройство подключено. Начни с изображения на LCD."
            detail = "1. Выбери GIF или MP4\n\n2. Проверь предпросмотр\n\n3. Загрузи файл на LCD"
            status, explanation = "Готово", "Экран и контроллер охлаждения подключены"
        else:
            state = "overview"
            title, heading, description, detail = "Обзор", "", "", ""
            sent = self.settings.get("last_upload_time") is not None
            status = "Отправлено" if sent else "Готово"
            explanation = "Устройство не подтверждает результат" if sent else "Экран и контроллер охлаждения подключены"
        self.overview_state = state
        self.overview_states.setCurrentIndex(0 if state == "overview" else 1)
        self.state_heading.setText(heading)
        self.state_description.setText(description)
        self.state_detail.setText(detail)
        self.dashboard.btn_configure.setVisible(state == "overview")
        self.btn_check.setVisible(state in ("missing", "denied"))
        self.btn_first_run.setVisible(state == "first")
        self.overview_badge.setText(badge(status, BAD if state == "denied" else OK if state in ("first", "overview") else WARN))
        self.connection_status.setText(explanation)
        if self.stack.currentIndex() == 0:
            self.page_title.setText(title)

    def complete_first_run(self):
        self.settings["first_run_done"] = True
        try:
            save_ui_settings(self.settings)
        except OSError as e:
            self.settings["first_run_done"] = False
            QMessageBox.warning(self, "Настройки не сохранены", str(e))
            return
        self.stack.setCurrentIndex(1)
        self.pages[1][1].pick()

    def closeEvent(self, event):
        lcd = self.pages[1][1]
        if lcd.worker is not None:
            QMessageBox.warning(self, "S360", "Идёт передача на LCD. Остановите её и дождитесь завершения.")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("S360 Control")
    app.setDesktopFileName("s360-control")
    app.setFont(theme.application_font())
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
