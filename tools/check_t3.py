#!/usr/bin/env python3
"""Проверка T3 на реальных Qt-виджетах без отправки команд устройству.

Запуск: QT_QPA_PLATFORM=offscreen python3 tools/check_t3.py
Настройки и журнал изолированы во временном каталоге.
"""
import os
from pathlib import Path
import sys
import tempfile


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
        for title in ("Мониторинг", "Часы", "Баннер", "Погода"):
            assert not modes[title].isEnabled()
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
        assert restored.lbl_orientation.text() == "240 × 320 px · поворот 180°"
        restored.timer.stop()
        restored.close()
        for index in range(3):
            window.stack.setCurrentIndex(index)
            app.processEvents()
        lcd.timer.stop()
        assert lcd.worker is None and not core.STATE.exists()
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(50, app.quit)
        assert app.exec() == 0
        assert not window.isVisible()
        print("T3 ok: запуск/закрытие Qt, вкладки, режимы, параметры, цвет, настройки; отправок нет")


if __name__ == "__main__":
    main()
