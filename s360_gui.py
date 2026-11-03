#!/usr/bin/env python3
"""S360 Control - графический интерфейс (PySide6) для MSI MEG CoreLiquid S360.

Принцип: рабочие функции работают, остальное показано как НЕДОСТУПНОЕ с причиной (без муляжей управления).
  Работает (проверено на железе): загрузка GIF/MP4 на LCD.
  Экспериментально (на этом устройстве не проверено): режимы кулера Silent/Balance/Game + передача температуры CPU.
  Недоступно: яркость, поворот, вкл/выкл и режимы экрана, чтение RPM/температуры жидкости.
"""
import datetime
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

import s360_lcd as core

UNIT = "s360-feed.service"
OK, BAD, WARN = "#4ade80", "#f87171", "#fbbf24"

STYLE = """
* { font-size: 14px; }
QMainWindow, QWidget { background: #14161a; color: #e6e8eb; }
QListWidget { background: #1a1d23; border: none; border-radius: 12px; padding: 8px; outline: 0; }
QListWidget::item { padding: 10px 14px; border-radius: 8px; margin: 2px 0; }
QListWidget::item:selected { background: #2b2f3a; color: #ffffff; }
QGroupBox { background: #1a1d23; border: 1px solid #262a33; border-radius: 12px; margin-top: 18px; padding: 14px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #9aa3b2; }
QPushButton { background: #2b2f3a; border: none; border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { background: #363b48; }
QPushButton:checked { background: #5b44d6; }
QPushButton:disabled, QComboBox:disabled, QSlider:disabled, QCheckBox:disabled { color: #5b6270; }
QPushButton#primary { background: #5b44d6; }
QPushButton#primary:hover { background: #6b55e6; }
QPushButton#primary:disabled { background: #2b2f3a; }
QLineEdit, QComboBox, QDoubleSpinBox, QPlainTextEdit { background: #20242c; border: 1px solid #2d323d; border-radius: 8px; padding: 6px; }
QProgressBar { background: #20242c; border: none; border-radius: 6px; height: 12px; text-align: center; }
QProgressBar::chunk { background: #5b44d6; border-radius: 6px; }
QLabel#preview { background: #000000; border: 1px solid #2d323d; border-radius: 10px; }
QLabel#warn { background: #2a2314; border: 1px solid #5c4a1a; border-radius: 10px; padding: 10px; color: #fbbf24; }
QLabel#muted { color: #8b93a3; }
"""


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


class SendWorker(QThread):
    progress = Signal(int, int)
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
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        box = QGroupBox("Устройства и состояние")
        form = QFormLayout(box)
        self.lcd, self.cooler, self.last_lcd, self.svc, self.cpu = (QLabel() for _ in range(5))
        for lbl in (self.lcd, self.cooler, self.last_lcd, self.svc, self.cpu):
            lbl.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("LCD-контроллер 0db0:9ba6", self.lcd)
        form.addRow("Кулер 0db0:6a05", self.cooler)
        form.addRow("Последняя загрузка на LCD", self.last_lcd)
        form.addRow("Сервис температуры (кулер)", self.svc)
        form.addRow("Температура CPU", self.cpu)
        lay.addWidget(box)
        lay.addWidget(muted("Статус «хост отправил» означает только, что USB-пакеты ушли. Подтверждения от экрана в протоколе нет "
                            "— результат смотри глазами."))
        lay.addStretch()
        self.refresh()

    def refresh(self):
        yes = lambda ok: badge("найден", OK) if ok else badge("не найден", BAD)
        self.lcd.setText(yes(core.usb_present(core.VID, core.PID)))
        self.cooler.setText(yes(core.usb_present(core.COOLER_VID, core.COOLER_PID)))
        st = core.read_state()
        names = {"HOST_COMPLETE_UNCONFIRMED": badge("хост отправил (экран не подтверждён)", WARN),
                 "IN_PROGRESS": badge("не завершена — состояние неизвестно", BAD),
                 "UNCERTAIN": badge("ошибка — состояние неизвестно", BAD),
                 "NOT_STARTED": badge("не началась", OK)}
        if st:
            when = datetime.datetime.fromtimestamp(st.get("time", 0)).strftime("%d.%m %H:%M:%S")
            kind = st.get("kind", "upload")
            tag = (st.get("sha256") or "") if kind == "direction" else (st.get("sha256") or "")[:8]
            what = "поворот экрана" if kind == "direction" else "загрузка"
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
        except core.S360Error as e:
            self.cpu.setText(badge(str(e), WARN))


class LcdPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.src = None
        self.worker = None
        self.tmp = tempfile.TemporaryDirectory()
        outer = QHBoxLayout(self)

        left = QVBoxLayout()
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Картинка / GIF на экран")
        form = QFormLayout(box)
        self.btn_pick = QPushButton("Выбрать GIF или MP4…")
        self.lbl_file = muted("файл не выбран")
        self.cmb_scale = QComboBox()
        for title, key in (("Заполнить (с обрезкой)", "fill"), ("Вписать (чёрные полосы)", "fit"),
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
        self.cmb_bg.setEditable(True)
        self.cmb_bg.addItems(["black", "white", "gray"])
        self.cmb_bg.setCurrentText(str(self.s["background"]))
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
        form.addRow(self.btn_pick)
        form.addRow(self.lbl_file)
        form.addRow("Масштаб", self.cmb_scale)
        form.addRow("Поворот картинки", self.cmb_rot)
        form.addRow("Фон (прозрачность, полосы)", self.cmb_bg)
        form.addRow("Подрезка края, px", self.spn_trim)
        form.addRow("Обрезка: X", self.sld_x)
        form.addRow("Обрезка: Y", self.sld_y)
        form.addRow("FPS", self.edt_fps)
        form.addRow("Длительность", self.spn_sec)
        form.addRow(muted("Проверено на этом экране: 20/3, 10 и 15 fps, анимация до ~2,5 с (37 кадров). Больше — "
                          "пробуй ступенями; при сбое восстановление — через MSI Center."))
        form.addRow(self.chk_select)
        form.addRow(self.chk_detach)
        pl.addWidget(box)
        self.btn_send = QPushButton("Отправить на LCD")
        self.btn_send.setObjectName("primary")
        pl.addWidget(self.btn_send)
        left.addWidget(self.panel)
        self.btn_cancel = QPushButton("Остановить")
        self.btn_cancel.setEnabled(False)
        self.prog = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        left.addWidget(self.btn_cancel)
        left.addWidget(self.prog)
        left.addWidget(self.log)

        dbox = QGroupBox("Поворот экрана (эксперимент)")
        dl = QFormLayout(dbox)
        self.cmb_dir = QComboBox()
        for deg in core.DIRECTIONS:
            self.cmb_dir.addItem(f"{deg}°", deg)
        self.cmb_dir.setCurrentIndex(max(0, self.cmb_dir.findData(int(self.s["device_direction"]))))
        self.btn_dir = QPushButton("Применить поворот экрана")
        self.pending_dir = None
        dl.addRow("Направление", self.cmb_dir)
        dl.addRow(self.btn_dir)
        dl.addRow(muted("Команда из анализа DLL MSI, в твоих захватах её нет. Неизвестно, сохранится ли поворот после "
                        "выключения. Для 90°/270° файлы грузятся в слот R1 — после смены перезагрузи картинку."))
        left.addWidget(dbox)

        na = QGroupBox("Недоступно: нет подтверждённого протокола")
        nl = QFormLayout(na)
        for label, widget in (("Включение/выключение", QCheckBox("Экран включён")), ("Яркость", QSlider(Qt.Orientation.Horizontal)),
                              ("Режимы экрана", QComboBox())):
            widget.setEnabled(False)
            nl.addRow(label, widget)
        nl.addRow(muted("Яркость и поворот известны только из статического анализа DLL (не из захватов), включение и режимы "
                        "(часы, монитор, погода, баннер) не найдены совсем. Нужны USBPcap-захваты MSI Center — см. README. "
                        "Поворот самой картинки (выше) делается программно и работает без этого."))
        left.addWidget(na)
        left.addStretch()
        outer.addLayout(left, 3)

        right = QVBoxLayout()
        self.lbl_prev = QLabel("предпросмотр")
        self.lbl_prev.setObjectName("preview")
        self.lbl_prev.setFixedSize(core.W + 4, core.H + 4)
        self.lbl_prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.lbl_prev)
        right.addWidget(muted("Первый кадр после масштабирования, 240×320 (как в эталонном файле). Реальная ориентация "
                              "на экране не проверялась."))
        right.addStretch()
        outer.addLayout(right, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.update_preview)
        self.btn_pick.clicked.connect(self.pick)
        self.btn_send.clicked.connect(self.send)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_dir.clicked.connect(self.apply_direction)
        for w in (self.cmb_scale, self.cmb_rot):
            w.currentIndexChanged.connect(self.changed)
        self.cmb_bg.currentTextChanged.connect(self.changed)
        for w in (self.sld_x, self.sld_y):
            w.valueChanged.connect(self.changed)
        self.edt_fps.textChanged.connect(self.changed)
        self.spn_sec.valueChanged.connect(self.changed)
        self.spn_trim.valueChanged.connect(self.changed)
        self.chk_select.toggled.connect(self.changed)
        self.changed()

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
                        background=self.cmb_bg.currentText().strip() or "black",
                        edge_trim=self.spn_trim.value())

    def changed(self, *_):
        fill = self.cmb_scale.currentData() == "fill"
        self.sld_x.setEnabled(fill)
        self.sld_y.setEnabled(fill)
        try:
            self.s.update(fps=self.fps_value(), max_seconds=self.spn_sec.value() or None,
                          scale=self.cmb_scale.currentData(), focus_x=self.sld_x.value() / 100.0,
                          focus_y=self.sld_y.value() / 100.0, with_select=self.chk_select.isChecked(),
                          rotate=int(self.cmb_rot.currentData()),
                          background=self.cmb_bg.currentText().strip() or "black",
                          edge_trim=self.spn_trim.value())
            core.save_settings(self.s)
        except (core.S360Error, OSError):
            pass
        self.timer.start(300)

    # --- предпросмотр
    def pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбор файла", str(Path.home()), "GIF/MP4 (*.gif *.mp4)")
        if path:
            self.src = path
            self.lbl_file.setText(path)
            self.update_preview()

    def update_preview(self):
        if not self.src or self.worker is not None:
            return
        try:
            self.apply_enc()
            png = Path(self.tmp.name) / "preview.png"
            core.preview_png(Path(self.src), png)
            self.lbl_prev.setPixmap(QPixmap(str(png)))
        except core.S360Error as e:
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
        self.panel.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.prog.setValue(0)
        self.log.appendPlainText(f"отправка ({kind}): {len(plan)} пакетов, {sha[:16]}")
        self.worker = SendWorker(plan, self.chk_detach.isChecked(), sha, kind)
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
        except core.S360Error as e:
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
        extra = ("\n\nВЫШЕ проверенного (до 15 fps и ~2,5 с): возможно зависание картинки; "
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
            f"Будет отправлена команда cmd 1a: {deg}°.\nОна взята из анализа DLL MSI и в твоих захватах не встречалась. "
            f"Неизвестно, сохранится ли поворот после выключения. Вернуть можно командой на 0° или через MSI Center.\n"
            f"Для 90°/270° файлы будут грузиться в слот R1 (по тому же анализу). Отправить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        self.pending_dir = deg
        self.start_worker(core.build_direction_plan(deg), f"direction:{deg}", "direction")

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
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
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        box = QGroupBox("Устройства и состояние")
        form = QFormLayout(box)
        self.lcd, self.cooler, self.last_lcd, self.svc, self.cpu = (QLabel() for _ in range(5))
        for lbl in (self.lcd, self.cooler, self.last_lcd, self.svc, self.cpu):
            lbl.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("LCD-контроллер 0db0:9ba6", self.lcd)
        form.addRow("Кулер 0db0:6a05", self.cooler)
        form.addRow("Последняя загрузка на LCD", self.last_lcd)
        form.addRow("Сервис температуры (кулер)", self.svc)
        form.addRow("Температура CPU", self.cpu)
        lay.addWidget(box)
        lay.addWidget(muted("Статус «хост отправил» означает только, что USB-пакеты ушли. Подтверждения от экрана в протоколе нет "
                            "— результат смотри глазами."))
        lay.addStretch()
        self.refresh()

    def refresh(self):
        yes = lambda ok: badge("найден", OK) if ok else badge("не найден", BAD)
        self.lcd.setText(yes(core.usb_present(core.VID, core.PID)))
        self.cooler.setText(yes(core.usb_present(core.COOLER_VID, core.COOLER_PID)))
        st = core.read_state()
        names = {"HOST_COMPLETE_UNCONFIRMED": badge("хост отправил (экран не подтверждён)", WARN),
                 "IN_PROGRESS": badge("не завершена — состояние неизвестно", BAD),
                 "UNCERTAIN": badge("ошибка — состояние неизвестно", BAD),
                 "NOT_STARTED": badge("не началась", OK)}
        if st:
            when = datetime.datetime.fromtimestamp(st.get("time", 0)).strftime("%d.%m %H:%M:%S")
            kind = st.get("kind", "upload")
            tag = (st.get("sha256") or "") if kind == "direction" else (st.get("sha256") or "")[:8]
            what = "поворот экрана" if kind == "direction" else "загрузка"
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
        except core.S360Error as e:
            self.cpu.setText(badge(str(e), WARN))


class LcdPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.src = None
        self.worker = None
        self.tmp = tempfile.TemporaryDirectory()
        outer = QHBoxLayout(self)

        left = QVBoxLayout()
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Картинка / GIF на экран")
        form = QFormLayout(box)
        self.btn_pick = QPushButton("Выбрать GIF или MP4…")
        self.lbl_file = muted("файл не выбран")
        self.cmb_scale = QComboBox()
        for title, key in (("Заполнить (с обрезкой)", "fill"), ("Вписать (чёрные полосы)", "fit"),
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
        self.cmb_bg.setEditable(True)
        self.cmb_bg.addItems(["black", "white", "gray"])
        self.cmb_bg.setCurrentText(str(self.s["background"]))
        self.spn_trim = QSpinBox()
        self.spn_trim.setRange(0, 3)
        self.spn_trim.setValue(int(self.s["edge_trim"]))
        self.edt_fps = QLineEdit(str(self.s["fps"] or ""))
        self.edt_fps.setPlaceholderText("20/3 (проверено)")
        self.spn_sec = QDoubleSpinBox()
        self.spn_sec.setRange(0, 60)
        self.spn_sec.setSingleStep(0.05)
        self.spn_sec.setDecimals(2)
        self.spn_sec.setSpecialValueText("без обрезки")
        self.spn_sec.setValue(float(self.s["max_seconds"] or 0))
        self.chk_select = QCheckBox("Выбор слота перед загрузкой (без него экран не менялся)")
        self.chk_select.setChecked(bool(self.s["with_select"]))
        self.chk_detach = QCheckBox("Разрешить временно отсоединить HID-драйвер от LCD (нужно для передачи)")
        form.addRow(self.btn_pick)
        form.addRow(self.lbl_file)
        form.addRow("Масштаб", self.cmb_scale)
        form.addRow("Поворот картинки", self.cmb_rot)
        form.addRow("Фон (прозрачность, полосы)", self.cmb_bg)
        form.addRow("Подрезка края, px", self.spn_trim)
        form.addRow("Обрезка: X", self.sld_x)
        form.addRow("Обрезка: Y", self.sld_y)
        form.addRow("FPS", self.edt_fps)
        form.addRow("Длительность", self.spn_sec)
        form.addRow(muted("Проверено на этом экране: 20/3 fps и до 1,95 с. Для плавности повышай ступенями (README); "
                          "при сбое восстановление — через MSI Center."))
        form.addRow(self.chk_select)
        form.addRow(self.chk_detach)
        pl.addWidget(box)
        self.btn_send = QPushButton("Отправить на LCD")
        self.btn_send.setObjectName("primary")
        pl.addWidget(self.btn_send)
        left.addWidget(self.panel)
        self.btn_cancel = QPushButton("Остановить")
        self.btn_cancel.setEnabled(False)
        self.prog = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        left.addWidget(self.btn_cancel)
        left.addWidget(self.prog)
        left.addWidget(self.log)

        dbox = QGroupBox("Поворот экрана (эксперимент)")
        dl = QFormLayout(dbox)
        self.cmb_dir = QComboBox()
        for deg in core.DIRECTIONS:
            self.cmb_dir.addItem(f"{deg}°", deg)
        self.cmb_dir.setCurrentIndex(max(0, self.cmb_dir.findData(int(self.s["device_direction"]))))
        self.btn_dir = QPushButton("Применить поворот экрана")
        self.pending_dir = None
        dl.addRow("Направление", self.cmb_dir)
        dl.addRow(self.btn_dir)
        dl.addRow(muted("Команда из анализа DLL MSI, в твоих захватах её нет. Неизвестно, сохранится ли поворот после "
                        "выключения. Для 90°/270° файлы грузятся в слот R1 — после смены перезагрузи картинку."))
        left.addWidget(dbox)

        na = QGroupBox("Недоступно: нет подтверждённого протокола")
        nl = QFormLayout(na)
        for label, widget in (("Включение/выключение", QCheckBox("Экран включён")), ("Яркость", QSlider(Qt.Orientation.Horizontal)),
                              ("Режимы экрана", QComboBox())):
            widget.setEnabled(False)
            nl.addRow(label, widget)
        nl.addRow(muted("Яркость и поворот известны только из статического анализа DLL (не из захватов), включение и режимы "
                        "(часы, монитор, погода, баннер) не найдены совсем. Нужны USBPcap-захваты MSI Center — см. README. "
                        "Поворот самой картинки (выше) делается программно и работает без этого."))
        left.addWidget(na)
        left.addStretch()
        outer.addLayout(left, 3)

        right = QVBoxLayout()
        self.lbl_prev = QLabel("предпросмотр")
        self.lbl_prev.setObjectName("preview")
        self.lbl_prev.setFixedSize(core.W + 4, core.H + 4)
        self.lbl_prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.lbl_prev)
        right.addWidget(muted("Первый кадр после масштабирования, 240×320 (как в эталонном файле). Реальная ориентация "
                              "на экране не проверялась."))
        right.addStretch()
        outer.addLayout(right, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.update_preview)
        self.btn_pick.clicked.connect(self.pick)
        self.btn_send.clicked.connect(self.send)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_dir.clicked.connect(self.apply_direction)
        for w in (self.cmb_scale, self.cmb_rot):
            w.currentIndexChanged.connect(self.changed)
        self.cmb_bg.currentTextChanged.connect(self.changed)
        for w in (self.sld_x, self.sld_y):
            w.valueChanged.connect(self.changed)
        self.edt_fps.textChanged.connect(self.changed)
        self.spn_sec.valueChanged.connect(self.changed)
        self.spn_trim.valueChanged.connect(self.changed)
        self.chk_select.toggled.connect(self.changed)
        self.changed()

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
                        background=self.cmb_bg.currentText().strip() or "black",
                        edge_trim=self.spn_trim.value())

    def changed(self, *_):
        fill = self.cmb_scale.currentData() == "fill"
        self.sld_x.setEnabled(fill)
        self.sld_y.setEnabled(fill)
        try:
            self.s.update(fps=self.fps_value(), max_seconds=self.spn_sec.value() or None,
                          scale=self.cmb_scale.currentData(), focus_x=self.sld_x.value() / 100.0,
                          focus_y=self.sld_y.value() / 100.0, with_select=self.chk_select.isChecked(),
                          rotate=int(self.cmb_rot.currentData()),
                          background=self.cmb_bg.currentText().strip() or "black",
                          edge_trim=self.spn_trim.value())
            core.save_settings(self.s)
        except (core.S360Error, OSError):
            pass
        self.timer.start(300)

    # --- предпросмотр
    def pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбор файла", str(Path.home()), "GIF/MP4 (*.gif *.mp4)")
        if path:
            self.src = path
            self.lbl_file.setText(path)
            self.update_preview()

    def update_preview(self):
        if not self.src or self.worker is not None:
            return
        try:
            self.apply_enc()
            png = Path(self.tmp.name) / "preview.png"
            core.preview_png(Path(self.src), png)
            self.lbl_prev.setPixmap(QPixmap(str(png)))
        except core.S360Error as e:
            self.lbl_prev.setText("нет предпросмотра")
            self.log.appendPlainText(f"предпросмотр: {e}")

    # --- отправка
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
        except core.S360Error as e:
            QMessageBox.warning(self, "Не удалось подготовить файл", str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        sha = core.sha256(data)
        st = core.read_state()
        if st and st["status"] in ("IN_PROGRESS", "UNCERTAIN"):
            r = QMessageBox.question(
                self, "Прошлая операция не завершена",
                f"Прошлая загрузка: {st['status']}. Состояние экрана неизвестно.\nЯ проверил экран и хочу продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        ref = "совпадает с проверенным эталоном" if sha == core.REFERENCE_SHA256 else \
            "НЕ эталонный файл: совместимость с прошивкой проверена только на параметрах 20/3 fps, 1.95 с, baseline"
        r = QMessageBox.question(
            self, "Подтверждение",
            f"Файл: {len(data)} байт\nSHA256: {sha[:16]}…\n{ref}\n\nЭто ПЕРЕЗАПИШЕТ слот S7/R0 на экране "
            f"({core.SLOT_NAME.decode()}). Отката нет.\nОтправить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        plan = ([] if not self.chk_select.isChecked() else core.build_select_plan()) + core.build_upload_plan(data)
        self.panel.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.prog.setValue(0)
        self.log.appendPlainText(f"отправка: {len(plan)} пакетов, SHA256 {sha[:12]}…")
        self.worker = SendWorker(plan, self.chk_detach.isChecked(), sha)
        self.worker.progress.connect(self.on_progress)
        self.worker.done_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

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
        self.panel.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.changed()

    def on_done(self):
        self.finish_ui()
        if self.pending_dir is not None:
            self.s["device_direction"] = self.pending_dir
            core.save_settings(self.s)
            self.pending_dir = None
        self.log.appendPlainText("хост отправил все пакеты")
        QMessageBox.information(self, "Готово (хост)", "Все пакеты отправлены. Подтверждения от экрана нет — посмотрите, "
                                "что на нём отображается.")

    def on_failed(self, msg, uncertain):
        self.pending_dir = None
        self.finish_ui()
        self.log.appendPlainText(f"ОШИБКА: {msg}")
        QMessageBox.critical(self, "Ошибка", msg + ("\n\nСостояние экрана неизвестно. Повторять автоматически не нужно."
                                                    if uncertain else ""))


class CoolingPage(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.s = settings
        lay = QVBoxLayout(self)
        warn = QLabel("ЭКСПЕРИМЕНТАЛЬНО. Протокол взят из стороннего драйвера (sarzeaud/coreliquid_driver) и совпадает по форме "
                      "с отчётами d0 40/41/85 в захвате, но на ЭТОМ устройстве не проверен. Режим работает только вместе с "
                      "передачей температуры CPU (сервис). Если сервис остановить, поведение помпы и вентиляторов без неё "
                      "не проверено — следи за температурой.")
        warn.setObjectName("warn")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        box = QGroupBox("Режим охлаждения")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, label in core.COOLING_LABELS.items():
            b = QPushButton(label)
            b.setCheckable(True)
            self.group.addButton(b)
            self.buttons[key] = b
            row.addWidget(b)
        self.buttons.get(self.s["cooling_mode"], self.buttons["balance"]).setChecked(True)
        for label, why in (("Customize", "свои кривые: не реализовано"), ("GI Mode", "не включено (нет проверки)")):
            b = QPushButton(label)
            b.setEnabled(False)
            b.setToolTip(why)
            row.addWidget(b)
        bl.addLayout(row)
        self.btn_apply = QPushButton("Применить и запустить передачу температуры")
        self.btn_apply.setObjectName("primary")
        self.btn_stop = QPushButton("Остановить сервис")
        self.chk_auto = QCheckBox("Запускать сервис при входе в систему")
        for w in (self.btn_apply, self.btn_stop, self.chk_auto):
            bl.addWidget(w)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.RichText)
        bl.addWidget(self.status)
        lay.addWidget(box)
        lay.addWidget(muted("Чтения режима с устройства нет: показывается последний отправленный. Яркость вентиляторов и "
                            "RPM не читаются."))
        lay.addStretch()
        self.btn_apply.clicked.connect(self.apply)
        self.btn_stop.clicked.connect(self.stop)
        self.chk_auto.clicked.connect(self.autostart)
        self.refresh()

    def mode(self):
        for key, b in self.buttons.items():
            if b.isChecked():
                return key
        return "balance"

    def apply(self):
        mode = self.mode()
        r = QMessageBox.question(
            self, "Подтверждение (эксперимент)",
            f"Будет отправлен режим «{core.COOLING_LABELS[mode]}» (d0 40 + d0 41), затем сервис каждые "
            f"{core.FEED_INTERVAL_S:g} с будет слать температуру CPU (d0 85).\nПротокол на этом устройстве не проверен. "
            f"Следи за температурой. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            core.COOLING_ENV.write_text(f"MODE={mode}\n")
        except OSError as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            return
        code, out = systemctl("restart", UNIT)
        if code != 0:
            QMessageBox.warning(self, "Сервис не запущен", out or "systemctl вернул ошибку")
        self.s["cooling_mode"] = mode
        core.save_settings(self.s)
        self.refresh()

    def stop(self):
        systemctl("stop", UNIT)
        self.refresh()

    def autostart(self, checked):
        code, out = systemctl("enable" if checked else "disable", UNIT)
        if code != 0:
            QMessageBox.warning(self, "Ошибка", out)
        self.refresh()

    def refresh(self):
        inst = unit_installed()
        for w in (self.btn_apply, self.btn_stop, self.chk_auto):
            w.setEnabled(inst)
        if not inst:
            self.status.setText(badge("сервис не установлен — запусти install.sh", WARN))
            return
        active = systemctl("is-active", UNIT)[1] == "active"
        enabled = systemctl("is-enabled", UNIT)[1] == "enabled"
        self.chk_auto.setChecked(enabled)
        try:
            last = core.COOLING_LABELS.get(__import__("json").loads(core.COOLING_STATE.read_text())["mode"], "?")
        except (OSError, ValueError, KeyError):
            last = "нет данных"
        self.status.setText(f"Сервис: {badge('работает', OK) if active else badge('остановлен', WARN)} "
                            f"&nbsp; Последний отправленный режим: {last}")


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
        core.save_settings(core.DEFAULT_SETTINGS)
        QMessageBox.information(self, "S360", "Настройки сброшены. Перезапусти приложение.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("S360 Control")
        self.resize(1040, 720)
        settings = core.load_settings()
        self.pages = [("Dashboard", DashboardPage()), ("LCD Display", LcdPage(settings)),
                      ("Cooling", CoolingPage(settings)), ("Monitoring", MonitoringPage()),
                      ("Settings", SettingsPage(settings))]
        side = QListWidget()
        side.setFixedWidth(190)
        self.stack = QStackedWidget()
        for name, page in self.pages:
            side.addItem(name)
            self.stack.addWidget(page)
        side.currentRowChanged.connect(self.on_row)
        side.setCurrentRow(1)
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(14, 14, 14, 14)
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(2000)

    def on_row(self, row):
        self.stack.setCurrentIndex(row)
        self.tick()

    def tick(self):
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

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
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
