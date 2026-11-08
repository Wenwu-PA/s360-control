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
    from PySide6.QtGui import QColor, QImage, QTransform

    lcd.select_mode("media")
    lcd.position_buttons[0].click()
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
        vw, vh = (320, 240) if position in (90, 270) else (240, 320)
        rw, rh = (160, 320) if angle in (90, 270) else (320, 160)
        if scale == "stretch":
            rx, ry = x * rw / vw, y * rh / vh
        else:
            ratio = (min if scale == "fit" else max)(vw / rw, vh / rh)
            if scale == "fit":
                rx, ry = (x - (vw - rw * ratio) / 2) / ratio, (y - (vh - rh * ratio) / 2) / ratio
            else:
                rx, ry = (x + (rw * ratio - vw) * fx) / ratio, (y + (rh * ratio - vh) * fy) / ratio
        if not (0 <= rx < rw and 0 <= ry < rh):
            return (0, 0, 0)
        sx, sy = {0: (rx, ry), 90: (ry, 160 - rx),
                  180: (320 - rx, 160 - ry), 270: (320 - ry, rx)}[angle]
        return colors[(int(sy >= 80) * 2 + int(sx >= 160) + frame) % 4]

    def verify(image, angle, scale, fx, fy, frame):
        vw, vh = (320, 240) if position in (90, 270) else (240, 320)
        assert (image.width(), image.height()) == (vw, vh)
        for x in (vw // 10, vw * 3 // 10, vw * 7 // 10, vw * 9 // 10):
            for y in (vh // 10, vh * 3 // 10, vh * 7 // 10, vh * 9 // 10):
                want = expected(x, y, angle, scale, fx, fy, frame)
                got = image.pixelColor(x, y).getRgb()[:3]
                assert max(abs(a - b) for a, b in zip(got, want)) < 35, (angle, scale, frame, x, y, got, want)

    lcd.spn_trim.setValue(0)
    lcd.s["device_direction"] = 180
    frame_size = lcd.preview_stack.size()
    assert frame_size.width() * 4 == frame_size.height() * 3
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
            for position, angle in ((0, 0), (90, 0), (180, 0), (270, 0)):
                lcd.position_buttons[position].click()
                frame_size = lcd.preview_stack.size()
                assert frame_size.width() * (3 if position in (90, 270) else 4) == frame_size.height() * (4 if position in (90, 270) else 3)
                assert f"положение {position}°" in lcd.lbl_orientation.text()
                assert lcd.s["screen_pos"] == core.load_settings()["screen_pos"] == position
                assert not hasattr(lcd, "cmb_rot")
                assert "поворот картинки" not in lcd.lbl_orientation.text().lower()
                assert lcd.s["device_direction"] == 180
                for scale in ("fit", "stretch", "fill"):
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData(scale))
                    # Кадрирование всегда по центру после поворота содержимого.
                    focus = 50
                    lcd.changed()
                    loop = QEventLoop()
                    QTimer.singleShot(350, loop.quit)
                    loop.exec()  # Настоящий сигнал timeout обновляет предпросмотр.
                    assert lcd.preview_stack.size() == frame_size
                    assert lcd.lbl_prev.size() == frame_size
                    assert lcd.lbl_prev.pixmap().size() == frame_size
                    preview = lcd.preview_pixmap.toImage()
                    fx = fy = focus / 100
                    verify(preview, angle, scale, fx, fy, 0)
                    preview_params = dict(core.ENC)
                    lcd.send()
                    assert load.call_args.args == (src,)
                    assert core.ENC == preview_params
                    # Получаем тот же результат подготовки, который использовал send().
                    prepared = Path(tmp) / "prepared.mp4"
                    prepared.write_bytes(prepared_data)
                    dimensions = subprocess.check_output([
                        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=s=x:p=0", str(prepared)], text=True, timeout=30).strip()
                    assert dimensions == "240x320", dimensions
                    raw = subprocess.check_output([
                        "ffmpeg", "-v", "error", "-i", str(prepared), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                        timeout=30)
                    size = 240 * 320 * 3
                    assert len(raw) == 24 * size  # 0,4 с при фиксированных 60 кадрах/с.
                    for frame in range(24):
                        image = QImage(raw[frame * size:(frame + 1) * size], 240, 320, 240 * 3,
                                       QImage.Format.Format_RGB888)
                        verify(image.transformed(QTransform().rotate(position)), angle, scale, fx, fy, frame // 12)
    lcd.position_buttons[0].click()
    lcd.src = None


def main():
    with tempfile.TemporaryDirectory(prefix="s360-t3-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QPushButton
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        # Проверки прежних экранов: состояние подключения изолировано от хоста.
        patch.object(gui, "device_access", return_value="ready").start()
        import s360_lcd as core

        app = QApplication([])
        app.setFont(gui.theme.application_font())
        app.setStyleSheet(gui.STYLE)
        window = gui.MainWindow()
        window.show()
        app.processEvents()
        lcd = window.pages[1][1]
        assert window.stack.count() == 4
        assert all(lcd.s[k] == v for k, v in gui.UI_DEFAULT_SETTINGS.items())
        assert lcd.worker is None
        assert not hasattr(lcd, "chk_detach") and not hasattr(lcd, "chk_select")
        assert not hasattr(lcd, "crop_dialog") and not hasattr(lcd, "direction_dialog")
        modes = {b.text(): b for b in lcd.findChildren(QPushButton)}
        modes["Изображение / GIF"].click()
        assert modes["Изображение / GIF"].isChecked()
        assert "Мониторинг" not in modes
        assert modes["Часы"].isEnabled()
        assert "Баннер" not in modes and "Погода" not in modes
        for scale in ("fit", "stretch", "fill"):
            lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData(scale))
            lcd.apply_enc()
            assert core.ENC["focus_x"] == core.ENC["focus_y"] == 0.5
        assert lcd.background_value() == "black"
        assert not hasattr(lcd, "cmb_bg") and not hasattr(lcd, "edt_fps") and not hasattr(lcd, "spn_sec")
        lcd.position_buttons[90].click()
        lcd.apply_enc()
        saved = core.load_settings()
        assert saved["background"] == core.ENC["background"] == "black"
        assert saved["focus_x"] == saved["focus_y"] == 0.5
        assert saved["rotate"] == core.ENC["rotate"] == 0
        assert saved["last_file"] is None and saved["last_upload_time"] is None
        # Старое значение цвета игнорируется при открытии новой страницы.
        saved["background"] = "gray"
        saved["device_direction"] = 180
        saved["rotate"] = 270
        core.save_settings(saved)
        restored = gui.LcdPage(gui.load_ui_settings())
        assert restored.background_value() == "black"
        restored.apply_enc()
        assert restored.s["rotate"] == core.ENC["rotate"] == 0
        assert restored.lbl_orientation.text() == "320 × 240 · положение 90°"
        assert restored.s["device_direction"] == 180
        restored.timer.stop()
        restored.close()
        if "--skip-rotation" not in sys.argv:
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
        detail = "проверка кадров пропущена (--skip-rotation)" if "--skip-rotation" in sys.argv else "GIF/MP4: четыре угла, подписи, кадры, fit/fill/stretch"
        print(f"T3 ok: Qt, вкладки, режимы, чёрный фон, настройки; {detail}; отправок нет")


if __name__ == "__main__":
    main()
