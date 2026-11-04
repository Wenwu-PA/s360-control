#!/usr/bin/env python3
"""Проверка T3 на реальных Qt-виджетах без отправки команд устройству.

Запуск: QT_QPA_PLATFORM=offscreen python3 tools/check_t3.py
Настройки и журнал изолированы во временном каталоге.
"""
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from unittest.mock import patch


def check_rotation(app, lcd, core, tmp):
    """Проверяем пиксели предпросмотра и всех кадров подготовленного GIF/MP4.

    Эталон вычисляется геометрически, независимо от фильтра ffmpeg.
    send() проходит настоящую подготовку файла, но останавливается до отправки.
    """
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QColor, QImage

    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for frame in range(2):
        image = QImage(320, 160, QImage.Format.Format_RGB32)
        for y in range(160):
            for x in range(320):
                index = (int(y >= 80) * 2 + int(x >= 160) + frame) % 4
                image.setPixelColor(x, y, QColor(*colors[index]))
        assert image.save(str(Path(tmp) / f"frame{frame}.png"))
    gif = Path(tmp) / "orientation.gif"
    subprocess.run(["ffmpeg", "-v", "error", "-framerate", "5", "-i", str(Path(tmp) / "frame%d.png"),
                    "-frames:v", "2", "-loop", "0", str(gif)], check=True, timeout=30)
    mp4 = Path(tmp) / "orientation.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(gif), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(mp4)], check=True, timeout=30)

    def expected(x, y, angle, scale, fx, fy, frame):
        rw, rh = (160, 320) if angle in (90, 270) else (320, 160)
        if scale == "stretch":
            rx, ry = x * rw / 240, y * rh / 320
        else:
            ratio = (min if scale == "fit" else max)(240 / rw, 320 / rh)
            if scale == "fit":
                rx, ry = (x - (240 - rw * ratio) / 2) / ratio, (y - (320 - rh * ratio) / 2) / ratio
            else:
                rx, ry = (x + (rw * ratio - 240) * fx) / ratio, (y + (rh * ratio - 320) * fy) / ratio
        if not (0 <= rx < rw and 0 <= ry < rh):
            return (0, 0, 0)
        sx, sy = {0: (rx, ry), 90: (ry, 160 - rx),
                  180: (320 - rx, 160 - ry), 270: (320 - ry, rx)}[angle]
        return colors[(int(sy >= 80) * 2 + int(sx >= 160) + frame) % 4]

    def verify(image, angle, scale, fx, fy, frame):
        assert (image.width(), image.height()) == (240, 320)
        for x in (24, 72, 168, 216):
            for y in (32, 96, 224, 288):
                want = expected(x, y, angle, scale, fx, fy, frame)
                got = image.pixelColor(x, y).getRgb()[:3]
                assert max(abs(a - b) for a, b in zip(got, want)) < 35, (angle, scale, frame, x, y, got, want)

    lcd.cmb_bg.setCurrentIndex(lcd.cmb_bg.findData("black"))
    lcd.spn_trim.setValue(0)
    lcd.edt_fps.setText("5")
    lcd.spn_sec.setValue(0)
    lcd.s["device_direction"] = 180
    lcd.cmb_dir.setCurrentIndex(lcd.cmb_dir.findData(90))
    load_media = core.load_media
    prepared_data = None

    def capture_media(*args, **kwargs):
        nonlocal prepared_data
        prepared_data = load_media(*args, **kwargs)
        return prepared_data

    with (patch.object(lcd, "confirm_previous", return_value=False),
          patch.object(lcd, "start_worker", side_effect=AssertionError("Отправка запрещена")),
          patch.object(core, "load_media", side_effect=capture_media) as load):
        for src in (gif, mp4):
            lcd.src = str(src)
            for angle in (0, 90, 180, 270):
                lcd.cmb_rot.setCurrentIndex(lcd.cmb_rot.findData(angle))
                assert lcd.lbl_orientation.text() == f"240 × 320 px · поворот картинки {angle}°"
                assert "LCD: 180°" in lcd.lbl_device_orientation.text()
                for scale in ("fit", "stretch", "fill"):
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData(scale))
                    # X/Y относятся к уже повёрнутому кадру; проверяем и края кадрирования.
                    focus = 0 if angle in (0, 90) else 100
                    lcd.sld_x.setValue(focus)
                    lcd.sld_y.setValue(focus)
                    lcd.changed()
                    loop = QEventLoop()
                    QTimer.singleShot(350, loop.quit)
                    loop.exec()  # Настоящий сигнал timeout обновляет предпросмотр.
                    preview = lcd.lbl_prev.pixmap().toImage()
                    fx = fy = focus / 100
                    verify(preview, angle, scale, fx, fy, 0)
                    preview_params = dict(core.ENC)
                    lcd.send()
                    assert load.call_args.args == (src,)
                    assert core.ENC == preview_params
                    # Получаем тот же результат подготовки, который использовал send().
                    prepared = Path(tmp) / "prepared.mp4"
                    prepared.write_bytes(prepared_data)
                    raw = subprocess.check_output([
                        "ffmpeg", "-v", "error", "-i", str(prepared), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                        timeout=30)
                    size = 240 * 320 * 3
                    assert len(raw) == 2 * size
                    for frame in range(2):
                        image = QImage(raw[frame * size:(frame + 1) * size], 240, 320, 240 * 3,
                                       QImage.Format.Format_RGB888)
                        verify(image, angle, scale, fx, fy, frame)
    lcd.src = None


def main():
    with tempfile.TemporaryDirectory(prefix="s360-t3-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication, QColorDialog, QPushButton
        import s360_gui as gui
        import s360_lcd as core

        app = QApplication([])
        app.setStyleSheet(gui.STYLE)
        window = gui.MainWindow()
        window.show()
        app.processEvents()
        lcd = window.pages[1][1]
        assert window.stack.count() == 3
        assert lcd.s == core.DEFAULT_SETTINGS
        assert lcd.worker is None
        assert not lcd.chk_detach.isChecked()
        assert lcd.advanced.isHidden()
        lcd.btn_advanced.click()
        assert not lcd.advanced.isHidden()
        lcd.btn_advanced.click()
        assert lcd.advanced.isHidden()
        modes = {b.text(): b for b in lcd.findChildren(QPushButton)}
        modes["Изображение / GIF"].click()
        assert modes["Изображение / GIF"].isChecked()
        for title in ("Мониторинг", "Часы"):
            assert not modes[title].isEnabled()
        assert "Баннер" not in modes and "Погода" not in modes
        for scale in ("fit", "stretch", "fill"):
            lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData(scale))
            assert lcd.sld_x.isEnabled() == (scale == "fill")
            assert lcd.sld_y.isEnabled() == (scale == "fill")
        for background in ("white", "black", "custom"):
            lcd.cmb_bg.setCurrentIndex(lcd.cmb_bg.findData(background))
            assert lcd.btn_bg.isEnabled() == (background == "custom")

        def close_color_dialog(accept):
            dialog = next(w for w in app.topLevelWidgets() if isinstance(w, QColorDialog))
            dialog.setCurrentColor(QColor("#123456" if accept else "#abcdef"))
            dialog.accept() if accept else dialog.reject()

        QTimer.singleShot(0, lambda: close_color_dialog(True))
        lcd.btn_bg.click()
        assert lcd.background_value() == "#123456"
        assert lcd.bg_sample.toolTip() == "#123456"
        QTimer.singleShot(0, lambda: close_color_dialog(False))
        lcd.btn_bg.click()
        assert lcd.background_value() == "#123456"
        lcd.sld_x.setValue(23)
        lcd.sld_y.setValue(78)
        lcd.cmb_rot.setCurrentIndex(lcd.cmb_rot.findData(90))
        lcd.apply_enc()
        saved = core.load_settings()
        assert saved["background"] == core.ENC["background"] == "#123456"
        assert saved["focus_x"] == 0.23 and saved["focus_y"] == 0.78
        assert saved["rotate"] == core.ENC["rotate"] == 90
        assert saved["last_file"] is None and saved["last_upload_time"] is None
        # Старое именованное значение цвета сохраняется при открытии новой страницы.
        saved["background"] = "gray"
        saved["device_direction"] = 180
        restored = gui.LcdPage(saved)
        assert restored.background_value() == "gray"
        assert restored.lbl_orientation.text() == "240 × 320 px · поворот картинки 90°"
        assert "LCD: 180°" in restored.lbl_device_orientation.text()
        restored.timer.stop()
        restored.close()
        check_rotation(app, lcd, core, tmp)
        assert lcd.s["cooling_mode"] == core.DEFAULT_SETTINGS["cooling_mode"]
        assert not core.COOLING_ENV.exists()
        for index in range(3):
            window.stack.setCurrentIndex(index)
            app.processEvents()
        lcd.timer.stop()
        assert lcd.worker is None and not core.STATE.exists()
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(50, app.quit)
        assert app.exec() == 0
        assert not window.isVisible()
        print("T3 ok: Qt, вкладки, режимы, цвет, настройки; GIF/MP4: четыре угла, подписи, кадры, fit/fill/stretch; отправок нет")


if __name__ == "__main__":
    main()
