#!/usr/bin/env python3
"""S360 Control - графический интерфейс (PySide6) для MSI MEG CoreLiquid S360.

Принцип: рабочие функции работают, остальное показано как НЕДОСТУПНОЕ с причиной (без муляжей управления).
  Работает (проверено на железе): загрузка GIF/MP4 на LCD.
  Режимы кулера и свой профиль: работа и изменение оборотов подтверждены владельцем; обратного чтения нет.
  Поворот экрана доступен через CLI.
  Яркость и вкл/выкл: через API ядра, с подтверждением записи во флеш.
  Часы: подтверждённые стили 1–5, местное время перед отправкой, без автосинхронизации.
  Недоступно в GUI: другие режимы экрана и температура жидкости.
"""
from copy import deepcopy
from html import escape
from fractions import Fraction
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QPixmap, QPainter, QPen, QPolygonF, QIcon
from PySide6.QtWidgets import (QApplication, QBoxLayout, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QGridLayout, QToolTip, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QListWidget, QListWidgetItem, QListView, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
                               QScrollArea, QSizePolicy, QStackedWidget, QTabBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

import s360_lcd as core
import theme
import monitor
import thumbs

UNIT = "s360-feed.service"
COOLING_TITLES = {"silent": "Тихий", "balance": "Сбалансированный", "game": "Игровой", "custom": "Свой профиль"}
OK, BAD, WARN = (theme.COLORS[key] for key in ("good", "error", "warning"))

STYLE = theme.build_qss()
UI_DEFAULT_SETTINGS = dict(core.DEFAULT_SETTINGS, fps="60", max_seconds=15, background="black", theme="dark", clock_style=1, gif_dir="")


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
    saved = {}
    try:
        saved = json.loads(core.SETTINGS.read_text())
    except (OSError, ValueError, AttributeError):
        pass
    if isinstance(saved, dict):
        for key, value in UI_DEFAULT_SETTINGS.items():
            if key not in saved:
                settings[key] = value
    settings.update(rotate=0, fps="60", max_seconds=15, background="black")
    settings["theme"] = saved.get("theme", "dark") if isinstance(saved, dict) else "dark"
    if settings["theme"] not in theme.THEMES:
        settings["theme"] = "dark"
    style = saved.get("clock_style", 1) if isinstance(saved, dict) else 1
    settings["clock_style"] = style if type(style) is int and 1 <= style <= 5 else 1
    folder = saved.get("gif_dir", "") if isinstance(saved, dict) else ""
    settings["gif_dir"] = folder if isinstance(folder, str) else ""
    return settings


def save_ui_settings(settings):
    # Поля интерфейса дописываются после сохранения известных ядру настроек.
    core.save_settings(settings)
    saved = json.loads(core.SETTINGS.read_text())
    saved.update(theme=settings.get("theme", "dark"), clock_style=settings.get("clock_style", 1), gif_dir=settings.get("gif_dir", ""))
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

    def __init__(self, plan, detach_ok, sha, kind="upload", clock_style=1):
        super().__init__()
        self.plan, self.detach_ok, self.sha, self.kind = plan, detach_ok, sha, kind
        self.clock_style = clock_style
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self.kind == "clock":
                now = datetime.datetime.now().astimezone()
                self.plan = core.build_clock_plan(self.clock_style, now=now.timestamp(),
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


class MonitorWorker(QThread):
    result = Signal(dict)
    failed = Signal()

    def __init__(self, kind, parent):
        super().__init__(parent)
        self.kind = kind

    def run(self):
        try:
            result = monitor.read_cooler_status() if self.kind == "cooler" else monitor.read_gpu_temps()
        except Exception:
            self.failed.emit()
        else:
            self.result.emit(result)


def last_sent_path():
    return core.STATE.parent / "last_sent.png"


class ElidedPath(QLabel):
    def __init__(self):
        super().__init__()
        self.full_path = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_path(self, path):
        self.full_path = path
        self.setToolTip(path)
        self.elide()

    def elide(self):
        self.setText(self.fontMetrics().elidedText(self.full_path, Qt.TextElideMode.ElideMiddle, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.elide()


class ThumbWorker(QThread):
    ready = Signal(int, str, str)

    def __init__(self, generation, paths, cache_dir, parent):
        super().__init__(parent)
        self.generation, self.paths, self.cache_dir = generation, paths, cache_dir

    def run(self):
        for path in self.paths:
            if self.isInterruptionRequested():
                break
            try:
                thumbnail = thumbs.ensure_thumb(path, self.cache_dir)
            except Exception:
                thumbnail = None
            self.ready.emit(self.generation, str(path), str(thumbnail) if thumbnail else "")


class MediaLibrary(QWidget):
    file_selected = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.worker = None
        self.generation = 0
        self.pending = None
        self.stopped = False
        self.source = None
        self.items = {}
        cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.cache_dir = cache_root / "s360-control" / "thumbs"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        title = QLabel("Библиотека")
        title.setObjectName("lcdTitle")
        header.addWidget(title)
        header.addStretch()
        self.btn_folder = QPushButton("Папка…")
        self.btn_folder.clicked.connect(self.pick_folder)
        header.addWidget(self.btn_folder)
        layout.addLayout(header)
        self.path_label = ElidedPath()
        layout.addWidget(self.path_label)
        self.note = muted("Выбери папку с GIF — здесь появятся превью")
        layout.addWidget(self.note)
        self.limit_note = muted("Показаны первые 100")
        self.limit_note.hide()
        layout.addWidget(self.limit_note)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(120, 160))
        self.grid.setGridSize(QSize(144, 196))
        self.grid.setSpacing(8)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.grid.setMinimumHeight(208)
        self.grid.itemClicked.connect(lambda item: self.file_selected.emit(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.grid, 1)
        self.open_folder(self.s.get("gif_dir", ""))

    def placeholder(self, text="Превью…"):
        pixmap = QPixmap(120, 160)
        pixmap.fill(QColor(theme.COLORS["control"]))
        painter = QPainter(pixmap)
        painter.setPen(QColor(theme.COLORS["secondary"]))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return QIcon(pixmap)

    def refresh_colors(self):
        for item in self.items.values():
            state = item.data(Qt.ItemDataRole.UserRole + 1)
            if state != "ready":
                item.setIcon(self.placeholder("Не открывается" if state == "error" else "Превью…"))

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка с GIF и MP4", self.s.get("gif_dir") or str(Path.home()))
        if folder:
            self.open_folder(folder, save=True)

    def open_folder(self, folder, *, save=False):
        self.generation += 1
        self.pending = None
        if self.worker is not None:
            self.worker.requestInterruption()
        self.grid.clear()
        self.items.clear()
        valid = bool(folder) and Path(folder).is_dir()
        self.path_label.set_path(str(folder) if valid else "")
        self.path_label.setVisible(valid)
        paths = [path.resolve() for path in thumbs.list_media(folder, limit=101)] if valid else []
        self.limit_note.setVisible(len(paths) > 100)
        self.note.setText("В папке нет GIF или MP4" if valid else "Выбери папку с GIF — здесь появятся превью")
        self.note.setVisible(not paths)
        self.grid.setVisible(bool(paths))
        icon = self.placeholder()
        for path in paths[:100]:
            source = str(path.resolve())
            item = QListWidgetItem(icon, path.name)
            item.setData(Qt.ItemDataRole.UserRole, source)
            item.setData(Qt.ItemDataRole.UserRole + 1, "pending")
            item.setToolTip(source)
            self.grid.addItem(item)
            self.items[source] = item
        self.select_source(self.source)
        if save:
            self.s["gif_dir"] = str(Path(folder).resolve())
            try:
                save_ui_settings(self.s)
            except OSError as e:
                QMessageBox.warning(self, "Настройки не сохранены", str(e))
        if paths:
            self.pending = (self.generation, paths[:100])
            self.start_thumbs()

    def start_thumbs(self):
        if self.worker is not None or self.pending is None or self.stopped:
            return
        generation, paths = self.pending
        self.pending = None
        self.worker = ThumbWorker(generation, paths, self.cache_dir, self)
        self.worker.ready.connect(self.thumb_ready)
        self.worker.finished.connect(self.thumbs_finished)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def thumbs_finished(self):
        self.worker = None
        self.start_thumbs()

    def thumb_ready(self, generation, source, thumbnail):
        if self.stopped or generation != self.generation or source not in self.items:
            return
        item = self.items[source]
        pixmap = QPixmap(thumbnail) if thumbnail else QPixmap()
        ok = not pixmap.isNull()
        item.setData(Qt.ItemDataRole.UserRole + 1, "ready" if ok else "error")
        item.setIcon(QIcon(pixmap) if ok else self.placeholder("Не открывается"))
        item.setToolTip(source if ok else source + "\nне открывается")

    def select_source(self, source):
        self.source = source
        item = self.items.get(str(Path(source).resolve())) if source else None
        self.grid.setCurrentItem(item)
        self.grid.clearSelection()
        if item is not None:
            item.setSelected(True)

    def stop(self):
        self.stopped = True
        self.pending = None
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait()


class DashboardPage(QWidget):
    status_changed = Signal()

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACING[1])
        cpu_box = QGroupBox("Температура процессора")
        cpu_layout = QVBoxLayout(cpu_box)
        self.cpu = QLabel()
        self.cpu.setObjectName("temperature")
        cpu_layout.addWidget(self.cpu)
        cpu_layout.addStretch()
        cpu_layout.addWidget(muted("Системный датчик CPU"))
        self.cpu_updated = muted("")
        cpu_layout.addWidget(self.cpu_updated)
        lay.addWidget(cpu_box, 0, 0, 2, 1)
        box = QGroupBox("Подключение")
        form = QFormLayout(box)
        self.lcd, self.cooler, self.last_lcd, self.svc = (QLabel() for _ in range(4))
        form.addRow("LCD-дисплей", self.lcd)
        form.addRow("Контроллер охлаждения", self.cooler)
        lay.addWidget(box, 0, 1)
        form.addRow("Охлаждение", self.svc)
        self.last_sent_box = QGroupBox("Последнее отправленное")
        upload_box = self.last_sent_box
        upload_row = QHBoxLayout(upload_box)
        self.last_sent = QLabel()
        self.last_sent.setObjectName("preview")
        self.last_sent.hide()
        upload_row.addWidget(self.last_sent, 0, Qt.AlignmentFlag.AlignVCenter)
        upload_form = QVBoxLayout()
        upload_row.addLayout(upload_form, 1)
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
        rpm_box = QGroupBox("Обороты охлаждения")
        rpm_form = QFormLayout(rpm_box)
        self.radiator_rpm, self.pump_rpm = QLabel("—"), QLabel("—")
        rpm_form.addRow("Вентиляторы радиатора", self.radiator_rpm)
        rpm_form.addRow("Помпа", self.pump_rpm)
        lay.addWidget(rpm_box, 2, 1)
        self.gpu_box = QGroupBox("Видеокарта")
        gpu_form = QFormLayout(self.gpu_box)
        self.gpu_values = {}
        for key, title in (("core", "Ядро"), ("hotspot", "Hotspot"), ("vram", "VRAM")):
            label, value = QLabel(title), QLabel()
            gpu_form.addRow(label, value)
            self.gpu_values[key] = (label, value)
        self.gpu_box.hide()
        lay.addWidget(self.gpu_box, 2, 0)
        lay.setRowStretch(2, 0)
        lay.setRowStretch(3, 1)
        self.cooler_error = False
        self._monitor_stopped = False
        self.cooler_worker = MonitorWorker("cooler", self)
        self.cooler_worker.result.connect(self.cooler_ready)
        self.cooler_worker.failed.connect(self.cooler_failed)
        self.gpu_worker = MonitorWorker("gpu", self)
        self.gpu_worker.result.connect(self.gpu_ready)
        self.gpu_worker.failed.connect(lambda: self.gpu_ready({}))
        self.cooler_timer = QTimer(self)
        self.cooler_timer.timeout.connect(self.poll_cooler)
        self.cooler_timer.start(2000)
        self.gpu_timer = QTimer(self)
        self.gpu_timer.timeout.connect(self.poll_gpu)
        self.gpu_timer.start(5000)
        QTimer.singleShot(0, self.poll_cooler)
        QTimer.singleShot(0, self.poll_gpu)
        self.refresh()

    def poll_cooler(self):
        if not self._monitor_stopped and not self.cooler_worker.isRunning():
            self.cooler_worker.start()

    def poll_gpu(self):
        if not self._monitor_stopped and not self.gpu_worker.isRunning():
            self.gpu_worker.start()

    def cooler_ready(self, data):
        self.radiator_rpm.setText(f"{data['rpm']['radiator']} об/мин")
        self.pump_rpm.setText(f"{data['rpm']['pump']} об/мин")
        self.cooler_error = False
        self.status_changed.emit()

    def cooler_failed(self):
        self.radiator_rpm.setText("—")
        self.pump_rpm.setText("—")
        self.cooler_error = True
        self.status_changed.emit()

    def gpu_ready(self, data):
        visible = False
        for key, (label, value) in self.gpu_values.items():
            available = data.get(key) is not None
            label.setVisible(available)
            value.setVisible(available)
            value.setText(f"{data[key]} °C" if available else "")
            visible |= available
        self.gpu_box.setVisible(visible)

    def stop_polling(self):
        self._monitor_stopped = True
        self.cooler_timer.stop()
        self.gpu_timer.stop()
        # Чтение имеет таймаут; завершаем потоки до уничтожения окна.
        self.cooler_worker.wait()
        self.gpu_worker.wait()

    def refresh(self):
        yes = lambda ok: badge("Подключён", OK) if ok else badge("Не найден", BAD)
        self.lcd.setText(yes(core.usb_present(core.VID, core.PID)))
        self.cooler.setText(yes(core.usb_present(core.COOLER_VID, core.COOLER_PID)))
        self.last_file.setText(Path(self.s["last_file"]).name if self.s.get("last_file") else "Изображение ещё не отправлялось")
        when = self.s.get("last_upload_time")
        if when is not None:
            when = datetime.datetime.fromtimestamp(when).strftime("%d.%m.%Y %H:%M:%S")
            self.last_upload.setText(f"{when} · Отправлено")
        else:
            self.last_upload.setText("")
        image = QPixmap()
        try:
            image.loadFromData(last_sent_path().read_bytes())
        except OSError:
            pass
        self.last_sent.setVisible(not image.isNull())
        if not image.isNull():
            image = image.scaled(QSize(160, 160), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.last_sent.setFixedSize(image.size())
            self.last_sent.setPixmap(image)
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
        active = unit_installed() and systemctl("is-active", UNIT)[1] == "active"
        try:
            mode = json.loads(core.COOLING_STATE.read_text()).get("mode")
        except (OSError, ValueError, AttributeError):
            mode = None
        title = COOLING_TITLES.get(mode)
        text = "работает" if active else "остановлено"
        self.svc.setText(badge(f"{title} · {text}" if title else text, OK if active else WARN))
        self.svc.setToolTip("Последний отправленный режим; обратного чтения нет")
        try:
            self.cpu.setText(f"{core.read_cpu_temp()} °C")
            self.cpu_updated.setText("Обновлено только что")
        except core.S360Error as e:
            self.cpu.setText("Датчик недоступен")
            self.cpu_updated.setText("Датчик CPU недоступен")
            self.cpu.setToolTip(str(e))


class LcdPage(QWidget):
    footer_changed = Signal()
    last_sent_changed = Signal()

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.s.update(rotate=0, fps="60", max_seconds=15, background="black")
        self.s.setdefault("clock_style", 1)
        self.s.setdefault("gif_dir", "")
        self.src = None
        self.source_duration = None
        self.pending_file = self.pending_brightness = self.pending_clock = self.pending_dir = None
        self.worker = None
        self.pending_preview = None
        self.preview_source = None
        self.brightness_baseline = int(self.s["brightness"]) if self.s["lcd_enabled"] else 0
        self.transfer_states = {}
        self.tmp = tempfile.TemporaryDirectory()
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(theme.SPACING[1])
        modes = QHBoxLayout()
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        self.mode_buttons = {}
        for title, mode in (("Изображение / GIF", "media"), ("Часы", "clock")):
            button = QPushButton(title)
            button.setProperty("lcdMode", True)
            button.setMinimumWidth(180)
            button.setCheckable(True)
            mode_group.addButton(button)
            button.setChecked(mode == self.s["lcd_mode"])
            button.clicked.connect(lambda checked=False, mode=mode: self.select_mode(mode))
            self.mode_buttons[mode] = button
            modes.addWidget(button)
        modes.addStretch()
        page.addLayout(modes)

        self.brightness_panel = QWidget()
        bl = QHBoxLayout(self.brightness_panel)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(theme.SPACING[1])
        self.chk_lcd = QCheckBox("Экран включён")
        self.chk_lcd.setChecked(bool(self.s["lcd_enabled"]))
        self.sld_brightness = QSlider(Qt.Orientation.Horizontal)
        self.sld_brightness.setRange(0, 100)
        self.sld_brightness.setValue(self.brightness_baseline)
        self.sld_brightness.setMaximumWidth(240)
        self.lbl_brightness = muted("")
        self.lbl_brightness_status = muted("Состояние LCD не считано. Подтверждения от устройства нет.")
        self.lbl_brightness_status.hide()
        self.btn_brightness = QPushButton("Применить")
        for widget in (QLabel("Яркость"), self.sld_brightness, self.lbl_brightness, self.chk_lcd, self.btn_brightness):
            bl.addWidget(widget)
        bl.addStretch()
        page.addWidget(self.brightness_panel)
        self.update_brightness_label()
        self.sld_brightness.valueChanged.connect(self.brightness_changed)
        self.chk_lcd.toggled.connect(self.lcd_toggled)
        self.btn_brightness.clicked.connect(self.apply_brightness)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        outer = QHBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.SPACING[1])
        scroll.setWidget(content)
        page.addWidget(scroll, 1)
        self.preview_box = QGroupBox("Предпросмотр")
        self.preview_box.setProperty("lcdCard", True)
        self.preview_box.setMinimumWidth(224)
        preview_layout = QVBoxLayout(self.preview_box)
        preview_layout.addStretch()
        self.preview_stack = QStackedWidget()
        self.preview_stack.setFixedSize(168, 224)
        self.lbl_prev = QLabel("Выбери файл")
        self.lbl_prev.setObjectName("preview")
        self.lbl_prev.setFixedSize(168, 224)
        self.lbl_prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_preview = QLabel("07 : 24")
        self.clock_preview.setObjectName("clockPreview")
        self.clock_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_preview.setMinimumSize(168, 224)
        self.preview_stack.addWidget(self.lbl_prev)
        self.preview_stack.addWidget(self.clock_preview)
        preview_layout.addWidget(self.preview_stack, 0, Qt.AlignmentFlag.AlignHCenter)
        self.clock_preview_note = muted("Время берётся из системы")
        self.clock_preview_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.clock_preview_note)
        self.lbl_orientation = muted("")
        self.lbl_orientation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.lbl_orientation)
        preview_layout.addStretch()
        outer.addWidget(self.preview_box, 1)
        right = QVBoxLayout()
        self.panel = QGroupBox()
        self.panel.setProperty("lcdCard", True)
        self.panel.setMinimumWidth(420)
        pl = QVBoxLayout(self.panel)
        pl.setSpacing(theme.SPACING[1])
        file_row = QHBoxLayout()
        file_labels = QVBoxLayout()
        self.lbl_file = QLabel("Файл не выбран")
        self.lbl_file.setObjectName("lcdTitle")
        self.lbl_file.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_file.setWordWrap(True)
        self.lbl_file_details = muted("—")
        file_labels.addWidget(self.lbl_file)
        file_labels.addWidget(self.lbl_file_details)
        file_row.addLayout(file_labels, 1)
        self.btn_pick = QPushButton("Выбрать файл")
        file_row.addWidget(self.btn_pick)
        pl.addLayout(file_row)
        controls = QGridLayout()
        self.cmb_scale = QComboBox()
        for title, key in (("Заполнить", "fill"), ("Вписать", "fit"), ("Растянуть", "stretch")):
            self.cmb_scale.addItem(title, key)
        self.cmb_scale.setCurrentIndex(max(0, self.cmb_scale.findData(self.s["scale"])))
        controls.addWidget(muted("Масштаб"), 0, 0)
        controls.addWidget(self.cmb_scale, 1, 0)
        pl.addLayout(controls)
        position_row = QHBoxLayout()
        position_row.addWidget(muted("Положение экрана"))
        self.position_group = QButtonGroup(self)
        self.position_buttons = {}
        for angle in (0, 90, 180, 270):
            button = QPushButton(f"{angle}°")
            button.setCheckable(True)
            button.setToolTip("Как повёрнут экран водянки. Картинка повернётся сама, чтобы выглядеть прямо")
            self.position_group.addButton(button, angle)
            self.position_buttons[angle] = button
            button.setChecked(angle == int(self.s["screen_pos"]))
            position_row.addWidget(button)
        self.position_group.idClicked.connect(self.select_position)
        pl.addLayout(position_row)
        # Подрезка остаётся локальной настройкой, редактора на главном экране нет.
        self.spn_trim = QSpinBox(self)
        self.spn_trim.setRange(0, 3)
        self.spn_trim.setValue(int(self.s["edge_trim"]))
        self.spn_trim.hide()
        self.library = MediaLibrary(self.s)
        self.library.file_selected.connect(self.select_file)
        pl.addWidget(self.library, 1)
        self.btn_send = QPushButton("Загрузить на экран")
        self.btn_send.setObjectName("primary")
        pl.addWidget(self.btn_send)
        right.addWidget(self.panel)
        self.clock_panel = QGroupBox()
        self.clock_panel.setObjectName("clockCard")
        self.clock_panel.setProperty("lcdCard", True)
        self.clock_panel.setMinimumWidth(420)
        clock_form = QVBoxLayout(self.clock_panel)
        clock_form.setSpacing(theme.SPACING[1])
        clock_form.setContentsMargins(24, 24, 24, 24)
        self.clock_heading = QLabel("Часы")
        self.clock_heading.setObjectName("lcdTitle")
        clock_form.addWidget(self.clock_heading)
        style_row = QHBoxLayout()
        self.clock_style_group = QButtonGroup(self)
        self.clock_style_buttons = {}
        for style in range(1, 6):
            button = QPushButton(str(style))
            button.setCheckable(True)
            button.setChecked(style == self.s["clock_style"])
            self.clock_style_group.addButton(button, style)
            self.clock_style_buttons[style] = button
            style_row.addWidget(button)
        self.clock_style_group.idClicked.connect(self.select_clock_style)
        clock_form.addLayout(style_row)
        clock_form.addWidget(muted("Время на экране"))
        self.lbl_clock_time = QLabel()
        self.lbl_clock_time.setObjectName("clockTime")
        self.lbl_clock_time.setWordWrap(True)
        clock_form.addWidget(self.lbl_clock_time)
        clock_note = muted("После отправки часы идут сами. Вернуть картинку можно на вкладке «Изображение / GIF».")
        clock_form.addWidget(clock_note)
        self.lbl_clock_position = muted("Часы пока не поворачиваются под положение экрана")
        clock_form.addWidget(self.lbl_clock_position)
        self.lbl_clock_status = muted("Часы из этого сеанса ещё не отправлялись. Состояние LCD не считано.")
        self.lbl_clock_status.hide()
        self.btn_clock = QPushButton("Показать часы")
        self.btn_clock.setObjectName("primary")
        self.btn_clock.clicked.connect(self.apply_clock)
        clock_form.addStretch()
        clock_form.addWidget(self.btn_clock)
        right.addWidget(self.clock_panel)
        self.btn_cancel = QPushButton("Остановить")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.hide()
        self.prog = QProgressBar()
        self.prog.hide()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.hide()
        right.addWidget(self.btn_cancel)
        right.addWidget(self.prog)
        right.addWidget(self.log)
        outer.addLayout(right, 2)
        for label in self.findChildren(QLabel):
            if label.objectName() == "muted" and label not in (self.lbl_brightness, clock_note):
                label.setProperty("lcdCaption", True)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.update_preview)
        self.btn_pick.clicked.connect(self.pick)
        self.btn_send.clicked.connect(self.send)
        self.btn_cancel.clicked.connect(self.cancel)
        self.cmb_scale.currentIndexChanged.connect(self.changed)
        self.spn_trim.valueChanged.connect(self.changed)
        self.changed()
        self.show_mode()
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock_time)
        self.clock_timer.start(1000)
        self.update_clock_time()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_preview()

    def resize_preview(self):
        width, height = (168, 224) if self.width() < 1100 else (core.W, core.H)
        if self.s["lcd_mode"] != "clock" and int(self.s["screen_pos"]) in (90, 270):
            width, height = height, width
        self.preview_stack.setFixedSize(width, height)
        self.lbl_prev.setFixedSize(width, height)
        self.scale_preview()

    def scale_preview(self):
        if hasattr(self, "preview_pixmap"):
            self.lbl_prev.setPixmap(self.preview_pixmap.scaled(
                self.preview_stack.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def show_mode(self):
        clock = self.s["lcd_mode"] == "clock"
        self.clock_panel.setVisible(clock)
        self.panel.setVisible(not clock)
        self.preview_box.setVisible(True)
        self.preview_stack.setCurrentIndex(1 if clock else 0)
        self.clock_preview_note.setVisible(clock)
        self.update_orientation()
        self.footer_changed.emit()
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

    def select_clock_style(self, style):
        self.s["clock_style"] = style
        try:
            save_ui_settings(self.s)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))

    def update_clock_time(self):
        if self.pending_clock is None:
            now = datetime.datetime.now().astimezone()
            offset = int(now.utcoffset().total_seconds() // 60)
            sign = "+" if offset >= 0 else "−"
            hours, minutes = divmod(abs(offset), 60)
            zone = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
            self.lbl_clock_time.setText(
                f"{now:%H:%M} <span style='font-size:{theme.FONT_SIZES['body']}px; "
                f"color:{theme.COLORS['secondary']}'>из системы · {zone}</span>")

    def clock_time_ready(self, text):
        self.pending_clock = text
        self.lbl_clock_time.setText("Время отправки: " + text)
        self.lbl_clock_status.setText("Отправка…")

    def apply_clock(self):
        if self.worker is not None or self.s["lcd_mode"] != "clock":
            return
        if not self.confirm_previous():
            return
        answer = QMessageBox.question(
            self, "Показать часы?", f"Стиль {self.s['clock_style']} · время из системы.\nКартинка на экране будет заменена.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_clock = ""
        self.lbl_clock_status.setText("Подготовка отправки часов…")
        self.start_worker([], f"clock:{self.s['clock_style']}", "clock")

    def update_brightness_label(self):
        value = self.sld_brightness.value()
        self.lbl_brightness.setText(f"{value}%")
        self.lbl_brightness.setToolTip("Запрошенное значение; состояние LCD не считано")
        self.btn_brightness.setVisible(value != self.brightness_baseline)

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
            ("Выключить экран?" if value == 0 else f"Установить яркость {value}%?") + "\nНастройка сохранится в памяти экрана.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_brightness = value
        self.lbl_brightness_status.setText(f"Отправка хостом: {value}%…")
        self.start_worker(core.build_brightness_plan(value), f"brightness:{value}", "brightness")

    def background_value(self):
        return "black"

    def select_position(self, angle):
        self.s["screen_pos"] = angle
        self.changed()

    def update_orientation(self):
        position = int(self.s["screen_pos"])
        width, height = (core.H, core.W) if position in (90, 270) else (core.W, core.H)
        self.lbl_orientation.setText(f"{width} × {height} · положение {position}°")
        self.lbl_orientation.setToolTip("")
        self.lbl_clock_position.setVisible(position != 0)
        self.resize_preview()

    # --- настройки
    def apply_enc(self):
        core.ENC.update(fps="60", max_seconds=15,
                        scale=self.cmb_scale.currentData(), focus_x=0.5,
                        focus_y=0.5, rotate=0, orient=int(self.s["screen_pos"]),
                        background=self.background_value(),
                        edge_trim=self.spn_trim.value())

    def changed(self, *_):
        self.s["rotate"] = 0
        self.update_orientation()
        try:
            self.s.update(fps="60", max_seconds=15,
                          scale=self.cmb_scale.currentData(), focus_x=0.5,
                          focus_y=0.5, with_select=True,
                          rotate=0,
                          background=self.background_value(),
                          edge_trim=self.spn_trim.value())
            save_ui_settings(self.s)
        except (core.S360Error, OSError):
            pass
        self.timer.start(300)
        self.footer_changed.emit()

    # --- предпросмотр
    def pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбор файла", str(Path.home()), "GIF/MP4 (*.gif *.mp4)")
        if path:
            self.select_file(path)

    def select_file(self, path):
        if self.worker is not None:
            return
        self.src = str(path)
        self.library.select_source(self.src)
        self.transfer_states.pop("media", None)
        self.lbl_file.setText(Path(path).name)
        self.lbl_file.setToolTip(str(path))
        self.update_media_details()
        self.update_orientation()
        self.update_preview()
        self.footer_changed.emit()

    def update_media_details(self):
        self.source_duration = None
        self.lbl_file_details.setText("—")
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                     "-show_entries", "stream=width,height:format=duration", "-of", "json", self.src],
                                    capture_output=True, text=True, timeout=10, check=True)
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            duration = float(data.get("format", {}).get("duration", 0))
            self.source_duration = duration if duration > 0 else None
            timing = f"{duration:.2f}".replace(".", ",") + " с" if duration > 0 else "—"
            self.lbl_file_details.setText(f"{stream['width']}×{stream['height']} · {timing}")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            pass
        self.changed()

    def update_preview(self):
        if not self.src or self.worker is not None:
            return
        self.preview_source = None
        try:
            self.apply_enc()
            png = Path(self.tmp.name) / "preview.png"
            core.preview_png(Path(self.src), png, viewer=True)
            # Загрузка байтов исключает возврат старого кадра из файлового кеша Qt.
            self.preview_pixmap = QPixmap()
            self.preview_pixmap.loadFromData(png.read_bytes())
            self.preview_source = self.src
            self.scale_preview()
        except (core.S360Error, OSError) as e:
            self.lbl_prev.setText("нет предпросмотра")
            self.log.appendPlainText(f"предпросмотр: {e}")

    # --- отправка
    def confirm_previous(self):
        st = core.read_state()
        if st and st["status"] in ("IN_PROGRESS", "UNCERTAIN"):
            r = QMessageBox.question(
                self, "Прошлая операция не завершена",
                "Состояние экрана неизвестно.\nЭкран проверен — продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            return r == QMessageBox.StandardButton.Yes
        return True

    def start_worker(self, plan, sha, kind):
        self.pending_preview = None
        if kind == "upload":
            self.timer.stop()
            self.update_preview()
            if self.preview_source == self.src:
                try:
                    self.pending_preview = (Path(self.tmp.name) / "preview.png").read_bytes()
                except OSError:
                    pass
        self.pending_file = Path(self.src).name if kind == "upload" else None
        self.btn_send.setEnabled(False)
        self.btn_clock.setEnabled(False)
        self.panel.setEnabled(False)
        self.brightness_panel.setEnabled(False)
        self.clock_panel.setEnabled(False)
        for button in self.mode_buttons.values():
            button.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.prog.setValue(0)
        self.log.appendPlainText("Отправка…")
        self.prog.show()
        self.log.show()
        self.btn_cancel.show()
        self.transfer_states[self.s["lcd_mode"]] = ("Отправка", "Хост отправляет; приём LCD не подтверждён")
        self.footer_changed.emit()
        self.worker = SendWorker(plan, True, sha, kind, clock_style=self.s["clock_style"])
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
        dur, frames, rate = core.probe_mp4(data)
        duration = f"{dur:g}".replace(".", ",") + " с" if dur is not None else "длительность неизвестна"
        try:
            above_tested = (dur is not None and dur > 8.95) or (bool(rate) and Fraction(rate) > 60)
        except (ValueError, ZeroDivisionError):
            above_tested = dur is not None and dur > 8.95
        extra = "\nФайл длиннее проверенного — возможен сбой." if above_tested else ""
        r = QMessageBox.question(
            self, "Загрузить на экран?",
            f"{Path(self.src).name} · {duration} · {len(data) / 1024:.0f} КБ\nКартинка на экране будет заменена.{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        plan = core.build_select_plan() + core.build_upload_plan(data, slot)
        self.pending_dir = None
        self.start_worker(plan, sha, "upload")

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
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.hide()
        self.changed()

    def on_done(self):
        self.transfer_states[self.s["lcd_mode"]] = ("Отправлено", "Отправлено")
        self.finish_ui()
        if self.pending_clock is not None:
            self.lbl_clock_status.setText("Отправлено")
            self.lbl_clock_status.setToolTip(f"Время отправки: {self.pending_clock}; приём LCD не подтверждён")
            self.pending_clock = None
        if self.pending_brightness is not None:
            self.brightness_baseline = self.pending_brightness
            self.update_brightness_label()
            self.lbl_brightness_status.setText("Отправлено")
            self.lbl_brightness_status.setToolTip("Хост отправил; приём LCD не подтверждён")
            self.pending_brightness = None
        if self.pending_dir is not None:
            self.s["device_direction"] = self.pending_dir
            self.update_orientation()
            self.pending_dir = None
        elif self.pending_file is not None:
            try:
                target = last_sent_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.pending_preview is not None:
                    temporary = target.with_suffix(".tmp.png")
                    temporary.write_bytes(self.pending_preview)
                    temporary.replace(target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as e:
                QMessageBox.warning(self, "Миниатюра не сохранена", str(e))
            self.s["last_file"] = self.pending_file
            self.s["last_upload_time"] = datetime.datetime.now().timestamp()
            self.pending_file = None
        try:
            save_ui_settings(self.s)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))
        self.pending_preview = None
        self.last_sent_changed.emit()
        self.log.appendPlainText("Отправлено")
        QMessageBox.information(self, "Отправлено", "Хост отправил. Приём экраном не подтверждён.")

    def on_failed(self, msg, uncertain):
        self.pending_preview = None
        self.transfer_states[self.s["lcd_mode"]] = ("Ошибка", f"Ошибка: {msg}")
        if self.pending_clock is not None:
            self.lbl_clock_status.setText("Ошибка: " + msg)
            self.pending_clock = None
        if self.pending_brightness is not None:
            self.lbl_brightness_status.setText(
                "Ошибка: " + msg)
            self.pending_brightness = None
        self.pending_dir = None
        self.pending_file = None
        self.finish_ui()
        self.log.appendPlainText(f"Ошибка: {msg}")
        QMessageBox.critical(self, "Ошибка", msg + ("\nСостояние экрана неизвестно."
                                                    if uncertain else ""))


class CurveGraph(QWidget):
    changed = Signal(int, int, int)
    selected = Signal()

    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.values = {"temp": [0] * 4, "duty": [0] * 4}
        self.dragging = None
        self.active_point = 0
        self.compact = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(220, 150)

    def plot_rect(self):
        margin = self.fontMetrics().height()
        return QRectF(36, margin + 12, self.width() - 52, self.height() - 2 * margin - 24)

    def point_position(self, index):
        r = self.plot_rect()
        return QPointF(r.left() + self.values["temp"][index] * r.width() / 100,
                       r.bottom() - self.values["duty"][index] * r.height() / 100)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.plot_rect()
        p.setPen(QColor(theme.COLORS["secondary"]))
        p.drawText(QPointF(r.left(), self.fontMetrics().ascent()), "Мощность, %")
        p.drawText(QPointF(r.right() - self.fontMetrics().horizontalAdvance("CPU, °C"),
                          self.fontMetrics().ascent()), "CPU, °C")
        for value in (0, 25, 50, 75, 100):
            x = r.left() + value * r.width() / 100
            y = r.bottom() - value * r.height() / 100
            p.setPen(QColor(theme.COLORS["control"]))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(theme.COLORS["secondary"]))
            p.drawText(QPointF(x - 8, r.bottom() + 18), str(value))
            p.drawText(QPointF(4, y + 4), str(value))
        p.setClipRect(r.adjusted(-7, -7, 7, 7))
        p.setPen(QPen(QColor(theme.COLORS["line"]), 1.5))
        points = [self.point_position(i) for i in range(4)]
        p.drawPolyline(QPolygonF(points))
        p.setBrush(QColor(theme.COLORS["line"]))
        for i, point in enumerate(points):
            radius = (3.5 if i == self.active_point and self.hasFocus() else 2.5)
            p.drawEllipse(point, radius, radius)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_compact = self.compact
            self.selected.emit()
            if was_compact:
                return
            self.setFocus()
            distances = [(self.point_position(i) - event.position()).manhattanLength() for i in range(4)]
            nearest = min(range(4), key=distances.__getitem__)
            if distances[nearest] <= 18:
                self.dragging = self.active_point = nearest
                self.update()

    def mouseMoveEvent(self, event):
        if self.dragging is None:
            return
        r = self.plot_rect()
        temp = round(max(0, min(100, (event.position().x() - r.left()) * 100 / r.width())))
        duty = round(max(core.PUMP_MIN_DUTY if self.channel == "pump" else 0,
                         min(100, (r.bottom() - event.position().y()) * 100 / r.height())))
        self.changed.emit(self.dragging, temp, duty)
        i = self.dragging
        QToolTip.showText(event.globalPosition().toPoint(),
                          f'{self.values["temp"][i]}° · {self.values["duty"][i]}%', self)

    def mouseReleaseEvent(self, event):
        self.dragging = None
        QToolTip.hideText()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self.compact:
                self.selected.emit()
                return
            i = self.active_point
            temp, duty = self.values["temp"][i], self.values["duty"][i]
            temp += (event.key() == Qt.Key.Key_Right) - (event.key() == Qt.Key.Key_Left)
            duty += (event.key() == Qt.Key.Key_Up) - (event.key() == Qt.Key.Key_Down)
            self.changed.emit(i, temp, duty)
            event.accept()
        else:
            super().keyPressEvent(event)


class CurveCard(QGroupBox):
    changed = Signal()
    selected = Signal()

    def __init__(self, channel, title):
        super().__init__(title)
        self.channel = channel
        self.setProperty("lcdCard", True)
        layout = QVBoxLayout(self)
        self.graph = CurveGraph(channel)
        self.graph.selected.connect(self.selected)
        layout.addWidget(self.graph, 1)
        self.chips_widget = QWidget()
        chips = QGridLayout(self.chips_widget)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(8)
        self.chips = []
        for index in range(4):
            chips.addWidget(muted(f"Точка {index + 1}"), 0, index)
            pair = QWidget()
            row = QVBoxLayout(pair)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            inputs = {}
            for key, suffix in (("temp", " °C"), ("duty", " %")):
                spin = QSpinBox()
                spin.setRange(core.PUMP_MIN_DUTY if channel == "pump" and key == "duty" else 0, 100)
                spin.setSuffix(suffix)
                spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                text_width = spin.fontMetrics().horizontalAdvance("100 °C") + 16
                spin.setMinimumWidth(text_width + 16)
                # Qt/QSS с NoButtons может оставить внутреннему редактору ширину 1 px.
                spin.lineEdit().setMinimumWidth(text_width)
                spin.setProperty("curveInput", True)
                spin.setAccessibleName(f"{title}, точка {index + 1}, {'температура' if key == 'temp' else 'мощность'}")
                spin.setKeyboardTracking(False)
                spin.setCorrectionMode(QSpinBox.CorrectionMode.CorrectToNearestValue)
                spin.valueChanged.connect(lambda value, i=index, k=key: self.input_changed(i, k, value))
                row.addWidget(spin)
                inputs[key] = spin
            chips.addWidget(pair, 1, index)
            self.chips.append(inputs)
        layout.addWidget(self.chips_widget)
        self.graph.changed.connect(self.move_point)

    def set_values(self, values):
        self.graph.values = deepcopy(values)
        for i, inputs in enumerate(self.chips):
            for key, spin in inputs.items():
                spin.blockSignals(True)
                spin.setValue(values[key][i])
                spin.blockSignals(False)
        self.graph.update()

    def input_changed(self, index, key, value):
        temp, duty = self.graph.values["temp"][index], self.graph.values["duty"][index]
        self.move_point(index, value if key == "temp" else temp, value if key == "duty" else duty)

    def move_point(self, index, temp, duty):
        values = deepcopy(self.graph.values)
        t, d = values["temp"], values["duty"]
        temp = max(t[index - 1] + 1 if index else 0,
                   min(t[index + 1] - 1 if index < 3 else 100, temp))
        duty = max(d[index - 1] if index else 0, core.PUMP_MIN_DUTY if self.channel == "pump" else 0,
                   min(d[index + 1] if index < 3 else 100, duty))
        values["temp"][index], values["duty"][index] = temp, duty
        self.set_values(values)
        self.changed.emit()

    def set_compact(self, compact):
        self.graph.compact = compact
        self.graph.setMinimumHeight(120 if compact else 200)
        self.chips_widget.setVisible(not compact)


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
        self.mode_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for key, description in (("silent", "Меньше шума"), ("balance", "Повседневная работа"),
                                 ("game", "Больше охлаждения"), ("custom", "3 независимых канала")):
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
        self.preset_box = QGroupBox()
        preset = QVBoxLayout(self.preset_box)
        self.preset_title = QLabel()
        preset.addWidget(self.preset_title)
        preset.addWidget(muted("Встроенный режим охлаждения"))
        preset_body = QHBoxLayout()
        preset_body.addWidget(muted("Вентилятор радиатора     —\n\nВентилятор водоблока     —\n\nПомпа     —"), 1)
        preset_body.addWidget(muted("Кривые встроенного режима недоступны для чтения.\n"
                                   "Для редактирования выбери «Свой профиль»."), 1)
        preset.addLayout(preset_body, 1)
        self.preset_box.setMinimumHeight(300)
        lay.addWidget(self.preset_box)
        self.component_tabs = QTabBar()
        for title in ("Радиатор", "Водоблок", "Помпа"):
            self.component_tabs.addTab(title)
        lay.addWidget(self.component_tabs)
        self.curves_box = QWidget()
        self.cards_layout = QGridLayout(self.curves_box)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards = {}
        self.active_channel = "radiator1"
        for channel, title in (("radiator1", "Вентилятор радиатора"),
                               ("waterblock", "Вентилятор водоблока"), ("pump", "Помпа")):
            card = CurveCard(channel, title)
            self.cards[channel] = card
            card.changed.connect(self.update_dirty)
            card.selected.connect(lambda ch=channel: self.select_component(ch))
        self.component_tabs.currentChanged.connect(lambda i: self.select_component(tuple(self.cards)[i]))
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
        self.btn_service = QPushButton("Остановить")
        self.btn_service.setToolTip("Остановить передачу температуры и отключить автозапуск")
        actions.insertWidget(1, self.btn_service)
        self.btn_reset_curves = QPushButton("Сбросить до стандартных")
        lay.addWidget(self.btn_reset_curves, 0, Qt.AlignmentFlag.AlignLeft)
        # Эти локальные сообщения объединяются в одну строку состояния внизу.
        for label in (self.curves_status, self.dirty):
            label.setParent(self)
            label.hide()
        self.btn_save_curves.clicked.connect(self.save_curves)
        self.btn_reset_curves.clicked.connect(self.reset_curves)
        self.btn_apply.clicked.connect(self.apply)
        self.btn_service.clicked.connect(self.stop)
        for button in self.buttons.values():
            button.toggled.connect(self.mode_changed)
        lay.addStretch()
        self.mode_changed()
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.layout_components()

    def select_component(self, channel):
        self.active_channel = channel
        self.component_tabs.blockSignals(True)
        self.component_tabs.setCurrentIndex(tuple(self.cards).index(channel))
        self.component_tabs.blockSignals(False)
        self.layout_components()

    def layout_components(self):
        narrow = self.window().width() < 1100
        custom = self.mode() == "custom"
        self.component_tabs.setVisible(narrow and custom)
        for card in self.cards.values():
            self.cards_layout.removeWidget(card)
        small_row = 0
        for channel, card in self.cards.items():
            active = channel == self.active_channel
            card.set_compact(not active)
            card.setVisible(custom and (active or not narrow))
            if active:
                self.cards_layout.addWidget(card, 0, 0, 2, 1)
            else:
                self.cards_layout.addWidget(card, small_row, 1)
                small_row += 1
        self.cards_layout.setColumnStretch(0, 2)
        self.cards_layout.setColumnStretch(1, 0 if narrow else 1)

    def mode(self):
        return next((key for key, b in self.buttons.items() if b.isChecked()), "balance")

    def mode_changed(self):
        custom = self.mode() == "custom"
        self.curves_box.setVisible(custom)
        self.preset_box.setVisible(not custom)
        self.preset_title.setText(COOLING_TITLES[self.mode()])
        self.hint.setText("Выбери компонент. Точки можно перетаскивать или вводить числом." if custom else "")
        self.hint.setVisible(custom)
        self.layout_components()
        self.btn_reset_curves.setVisible(custom)
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
            f"Включить «{COOLING_TITLES[mode]}»?\nПередача температуры запустится автоматически.",
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
        if not self.autostart(True):
            self.refresh()
            return
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
            self.autostart(False)
        self.refresh()

    def autostart(self, checked):
        code, out = systemctl("enable" if checked else "disable", UNIT)
        if code != 0:
            QMessageBox.warning(self, "Ошибка", out)
            return False
        return True

    def journal_line(self):
        try:
            result = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "1", "--no-pager"],
                                    capture_output=True, text=True, timeout=10)
            return (result.stdout.strip() or result.stderr.strip() or "В журнале нет записей").splitlines()[-1]
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Журнал недоступен: {e}"

    def refresh(self):
        inst = unit_installed()
        self.btn_apply.setEnabled(inst)
        self.btn_apply.setToolTip("" if inst else "Сервис не установлен: выполни ./install.sh вручную")
        self.btn_service.setVisible(False)
        self.btn_service.setEnabled(False)
        if not inst:
            self.set_service_status("Остановлен", "Сервис не установлен — выполни ./install.sh вручную", WARN)
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
        if state not in ("active", "inactive", "failed", "activating", "reloading", "deactivating"):
            self.set_service_status("Ошибка", "Статус сервиса неизвестен", WARN)
            self.status.setToolTip(state)
        elif state == "failed" or restart_error or self.service_error:
            self.set_service_status("Ошибка", "Ошибка сервиса: " + self.journal_line()[:240]
                                    + ". Если установленная копия устарела: ./install.sh", BAD)
        elif code == 0 and state == "active":
            self.set_service_status("Работает", "Сервис передачи температуры процессора работает; приём не подтверждён", OK)
            self.btn_service.setEnabled(True)
            self.btn_service.setVisible(True)
        elif state in ("activating", "reloading"):
            self.set_service_status("Запускается", "Запускается передача температуры процессора…", WARN)
        elif state in ("inactive", "deactivating"):
            self.set_service_status("Остановлен", "Передача температуры процессора остановлена", WARN)
        else:
            self.set_service_status("Ошибка", "Статус сервиса неизвестен", WARN)
            self.status.setToolTip(state)


class SettingsPage(QWidget):
    theme_changed = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        lay = QVBoxLayout(self)
        box = QGroupBox("Тема")
        row = QHBoxLayout(box)
        group = QButtonGroup(self)
        self.theme_buttons = {}
        for name, title in (("dark", "Тёмная"), ("light", "Светлая"), ("oled", "OLED")):
            button = QPushButton(title)
            button.setCheckable(True)
            button.setChecked(name == self.s["theme"])
            group.addButton(button)
            row.addWidget(button)
            self.theme_buttons[name] = button
            button.clicked.connect(lambda checked=False, name=name: self.choose_theme(name))
        lay.addWidget(box)
        lay.addWidget(muted(f"Версия: {core.VERSION}"))
        lay.addStretch()

    def choose_theme(self, name):
        self.s["theme"] = name
        self.theme_changed.emit(name)
        try:
            save_ui_settings(self.s)
        except OSError as e:
            QMessageBox.warning(self, "Настройки не сохранены", str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        theme.install_russian_translation(QApplication.instance())
        self.setWindowTitle("S360 Control")
        self.resize(1040, 720)
        settings = load_ui_settings()
        self.settings = settings
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
        actions = QHBoxLayout()
        self.btn_check = QPushButton("Проверить подключение")
        self.btn_check.clicked.connect(self.tick)
        actions.addWidget(self.btn_check)
        actions.addStretch()
        content.addLayout(actions)
        self.overview_states.addWidget(state_page)
        self.pages = [("Мониторинг", self.overview_states), ("LCD-дисплей", LcdPage(settings)),
                      ("Охлаждение", CoolingPage(settings)), ("Настройки", SettingsPage(settings))]
        self.pages[3][1].theme_changed.connect(self.apply_theme)
        self.stack = QStackedWidget()
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for name, page in self.pages:
            self.tabs.addTab(name)
            # LCD и охлаждение содержат собственную прокрутку; остальные страницы
            # прокручиваются внутри каркаса, не затрагивая нижнюю полосу.
            if isinstance(page, (LcdPage, CoolingPage)):
                self.stack.addWidget(page)
            else:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(page)
                self.stack.addWidget(scroll)
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
        dashboard.status_changed.connect(self.refresh_overview_state)
        rows = (
            (self.overview_badge, self.connection_status, ()),
            (self.lcd_badge, self.lcd_status, (lcd.btn_cancel, lcd.btn_send, lcd.btn_clock)),
            (cooling.service_badge, cooling.status, (cooling.btn_save_curves, cooling.btn_apply)),
            (QLabel("● Настройки"), QLabel("Тема применяется сразу"), ()),
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
            if status_badge is cooling.service_badge:
                row_layout.addWidget(cooling.btn_service)
            row_layout.addWidget(explanation, 1)
            for button in buttons:
                # Переносим сами кнопки: обработчики и подтверждения остаются прежними.
                row_layout.addWidget(button)
            self.footer_stack.addWidget(row)
        lcd.last_sent_changed.connect(self.dashboard.refresh)
        lcd.log.textChanged.connect(self.update_lcd_footer)
        lcd.footer_changed.connect(self.update_lcd_footer)
        lcd.show_mode()
        layout.addWidget(self.footer)
        self.setCentralWidget(root)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self.on_row)
        self.stack.setCurrentIndex(0)
        self.apply_theme(settings["theme"])
        self.on_row(self.stack.currentIndex())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(2000)

    def apply_theme(self, name):
        global OK, BAD, WARN, STYLE
        theme.select_theme(name)
        OK, BAD, WARN = (theme.COLORS[key] for key in ("good", "error", "warning"))
        STYLE = theme.build_qss()
        QApplication.instance().setStyleSheet(STYLE)
        self.pages[1][1].update_clock_time()
        self.pages[2][1].refresh()
        self.update_lcd_footer()
        self.tick()
        for graph in self.findChildren(CurveGraph):
            graph.update()
        self.pages[1][1].preview_stack.update()
        self.pages[1][1].library.refresh_colors()

    def on_row(self, row):
        self.tabs.setCurrentIndex(row)
        self.footer_stack.setCurrentIndex(row)
        self.page_title.setText(self.pages[row][0])
        self.tick()

    def update_lcd_footer(self):
        lcd = self.pages[1][1]
        mode = lcd.s["lcd_mode"]
        fallback = (("Не отправлено", "Время возьмётся из системы при отправке") if mode == "clock" else
                    ("Готово", "Файл подготовлен к отправке") if lcd.src else ("Готово", "Выбери GIF или MP4"))
        title, text = lcd.transfer_states.get(mode, fallback)
        self.lcd_badge.setText(badge(title, BAD if title == "Ошибка" else WARN if title == "Не отправлено" else OK))
        self.lcd_status.setText(text)
        self.lcd_status.setToolTip(text)

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
            labels = {"missing": "Не найден", "denied": "Нет доступа", "ready": "Подключён"}
            detail = f"LCD-дисплей: {labels[lcd]}\n\nКонтроллер охлаждения: {labels[cooler]}"
            status, explanation = "Не найдено", "S360 не обнаружен полностью"
        elif "denied" in (lcd, cooler):
            state = "denied"
            title, heading = "Подключение устройства", "Нет доступа к USB"
            description = "Установи правило доступа и переподключи кабель USB водянки."
            detail = "В терминале из папки приложения:\n\n./install.sh\n\nПосле установки нажми «Проверить подключение»."
            status, explanation = "Нет доступа", "Приложению нужны права на USB"
        else:
            state = "overview"
            title, heading, description, detail = "Мониторинг", "", "", ""
            sent = self.settings.get("last_upload_time") is not None
            status = "Отправлено" if sent else "Готово"
            explanation = "Устройство не подтверждает результат" if sent else "Экран и контроллер охлаждения подключены"
        if self.dashboard.cooler_error:
            status, explanation = "Нет ответа от кулера", "Нет ответа от кулера"
        self.overview_state = state
        self.overview_states.setCurrentIndex(0 if state == "overview" else 1)
        self.state_heading.setText(heading)
        self.state_description.setText(description)
        self.state_detail.setText(detail)
        self.btn_check.setVisible(state in ("missing", "denied"))
        self.overview_badge.setText(badge(status, BAD if state == "denied" else WARN if self.dashboard.cooler_error else OK if state == "overview" else WARN))
        self.connection_status.setText(explanation)
        if self.stack.currentIndex() == 0:
            self.page_title.setText(title)

    def closeEvent(self, event):
        lcd = self.pages[1][1]
        if lcd.worker is not None:
            QMessageBox.warning(self, "S360", "Идёт передача на LCD. Остановите её и дождитесь завершения.")
            event.ignore()
            return
        self.timer.stop()
        self.dashboard.stop_polling()
        lcd.library.stop()
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
