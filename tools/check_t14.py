#!/usr/bin/env python3
"""T14: десять проверок LCD v2.1, Qt offscreen; без устройства и сервисов."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t14-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QLabel, QDialogButtonBox
        import s360_gui as gui
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError("Оборудование или реальный сервис")

        app = QApplication([])
        app.setStyleSheet(gui.STYLE)
        errors = []
        with (patch.object(gui, "device_access", return_value="ready"),
              patch.object(core, "usb_present", return_value=False),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "do_send", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(gui, "unit_installed", return_value=False),
              patch.object(gui, "systemctl", side_effect=forbidden),
              patch.object(gui.subprocess, "run", side_effect=forbidden),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "warning"),
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.show(); window.stack.setCurrentIndex(1); app.processEvents()
            lcd = window.pages[1][1]
            assert gui.load_ui_settings()["max_seconds"] == lcd.spn_sec.value() == 15
            assert lcd.background_value() == "black"
            assert not any("Проверено на этом экране" in label.text() for label in lcd.findChildren(QLabel))
            assert not hasattr(lcd, "crop_dialog") and not hasattr(lcd, "direction_dialog")
            assert set(lcd.mode_buttons) == {"media", "clock"} and lcd.edt_fps.text() == "60"
            assert not hasattr(lcd, "advanced") and not hasattr(lcd, "chk_detach") and not hasattr(lcd, "chk_select")
            assert lcd.prog.isHidden() and lcd.log.isHidden() and lcd.btn_brightness.isHidden()  # 1
            with patch.object(lcd, "start_worker", side_effect=forbidden):
                lcd.sld_brightness.setValue(42)
                assert not lcd.btn_brightness.isHidden()
                lcd.btn_brightness.click()
                assert "42%" in question.call_args.args[2] and lcd.pending_brightness is None  # 2
            lcd.mode_buttons["clock"].click()
            assert lcd.preview_stack.currentIndex() == 1 and lcd.lbl_orientation.text() == "Примерный вид, стиль 1"
            assert "из системы · UTC" in lcd.lbl_clock_time.text()
            assert "Не отправлено" in window.lcd_badge.text() and "при отправке" in window.lcd_status.text()  # 3
            with patch.object(lcd, "start_worker") as start:
                question.return_value = QMessageBox.StandardButton.Yes
                lcd.btn_clock.click()
                assert start.call_args.args == ([], "clock:1", "clock")
                assert "На время передачи драйвер HID временно отключится" in question.call_args.args[2]
                lcd.pending_clock = None
                lcd.btn_brightness.click()
                assert start.call_args.args[1:] == ("brightness:42", "brightness")
                lcd.pending_brightness = None  # 4
            lcd.mode_buttons["media"].click()
            class Probe:
                stdout = json.dumps({"streams": [{"width": 500, "height": 400}], "format": {"duration": "8.95"}})
            with (patch.object(QFileDialog, "getOpenFileName", return_value=(str(Path(tmp) / "globe.gif"), "")),
                  patch.object(gui.subprocess, "run", return_value=Probe()),
                  patch.object(lcd, "update_preview")):
                lcd.btn_pick.click()
            assert lcd.lbl_file_details.text() == "500×400 · 8,95 с" and lcd.s["max_seconds"] == 15
            assert not lcd.lbl_full_gif.isHidden() and "globe.gif" in lcd.lbl_orientation.text()  # 5
            lcd.spn_sec.setValue(3)
            assert lcd.lbl_full_gif.isHidden() and lcd.s["max_seconds"] == 3
            lcd.spn_sec.setValue(8.95)
            assert not lcd.lbl_full_gif.isHidden()
            lcd.spn_sec.setValue(0)
            lcd.apply_enc()
            assert core.ENC["focus_x"] == core.ENC["focus_y"] == .5  # 6
            lcd.s["with_select"] = False
            with (patch.object(core, "load_media", return_value=b"prepared"),
                  patch.object(core, "probe_mp4", return_value=(8.95, 537, "60")),
                  patch.object(core, "build_select_plan", return_value=["select"]) as select,
                  patch.object(core, "build_upload_plan", return_value=["upload"]),
                  patch.object(lcd, "start_worker") as start):
                question.return_value = QMessageBox.StandardButton.No
                lcd.btn_send.click(); assert not start.called
                question.return_value = QMessageBox.StandardButton.Yes
                lcd.btn_send.click()
                assert start.call_args.args[0] == ["select", "upload"] and select.call_count == 1
                assert "На время передачи драйвер HID временно отключится" in question.call_args.args[2]  # 7
            with (patch.object(core, "load_media", return_value=b"prepared"),
                  patch.object(lcd, "start_worker", side_effect=forbidden)):
                question.return_value = QMessageBox.StandardButton.No
                for duration, rate, warning in ((8.95, "60", False), (3, "30", False),
                                                 (8.96, "60", True), (2, "60001/1000", True)):
                    with patch.object(core, "probe_mp4", return_value=(duration, 100, rate)):
                        lcd.send()
                    assert ("выше проверенного" in question.call_args.args[2]) == warning  # 8
            saved = gui.load_ui_settings(); saved["fps"] = "20/3"; saved["edge_trim"] = 2
            saved.update(max_seconds=None, background="white", focus_x=.23, focus_y=.78)
            gui.save_ui_settings(saved)
            restored = gui.LcdPage(gui.load_ui_settings())
            assert restored.edt_fps.text() == "20/3" and restored.spn_trim.value() == 2 and restored.spn_trim.isHidden()
            assert restored.spn_sec.value() == 0 and restored.background_value() == "white"
            restored.apply_enc()
            assert core.ENC["focus_x"] == core.ENC["focus_y"] == .5
            restored.timer.stop(); restored.close()  # 9
            lcd.mode_buttons["clock"].click()
            lcd.transfer_states["clock"] = ("Отправлено", "Хост отправил; приём LCD не подтверждён")
            lcd.footer_changed.emit()
            assert "Отправлено" in window.lcd_badge.text() and "не подтверждён" in window.lcd_status.text()
            assert not errors and not core.STATE.exists()
            lcd.timer.stop(); window.timer.stop(); window.close(); app.processEvents()  # 10
            # Стандартные кнопки и штатный qtbase_ru, и запасные ручные подписи.
            for fallback in (False, True):
                if fallback:
                    from PySide6.QtCore import QTranslator
                    if isinstance(app._s360_translation, QTranslator):
                        app.removeTranslator(app._s360_translation)
                    else:
                        app.removeEventFilter(app._s360_translation)
                    del app._s360_translation
                    with patch.object(QTranslator, "load", return_value=False):
                        gui.theme.install_russian_translation(app)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No |
                                           QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Close)
                buttons.show(); app.processEvents()
                assert [buttons.button(getattr(QDialogButtonBox.StandardButton, name)).text().replace("&", "")
                        for name in ("Yes", "No", "Cancel", "Close")] == ["Да", "Нет", "Отмена", "Закрыть"]
                buttons.close()
                message = QMessageBox()
                message.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                                           QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Close)
                message.show(); app.processEvents()
                assert [message.button(getattr(QMessageBox.StandardButton, name)).text().replace("&", "")
                        for name in ("Yes", "No", "Cancel", "Close")] == ["Да", "Нет", "Отмена", "Закрыть"]
                message.close()
            with patch.object(QMessageBox, "information"):
                window.pages[0][1].widget(2).reset()
            assert gui.load_ui_settings() == gui.UI_DEFAULT_SETTINGS

        from check_t3 import check_rotation
        check_rotation(app, lcd, core, tmp)
        lcd.timer.stop()
        print("T14 ok: 10 проверок; режимы, явное применение, подтверждения, метаданные, плитки, русские кнопки, сохранение, поворот кадров; отправок нет")


if __name__ == "__main__":
    main()
