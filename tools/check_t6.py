#!/usr/bin/env python3
"""T6: Qt offscreen, временные настройки, подмена только границы USB/сервиса.

Физическая отправка запрещена. Запуск: python3 tools/check_t6.py
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t6-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        # Проверки прежних экранов: состояние подключения изолировано от хоста.
        patch.object(gui, "device_access", return_value="ready").start()
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError("Физическое оборудование и реальные сервисы запрещены")

        calls, errors = [], []
        failure = None
        release = threading.Event()
        main_thread = threading.get_ident()

        def fake_send(plan, detach_ok, sha, progress, cancel, kind):
            assert threading.get_ident() != main_thread
            assert release.wait(3), "Qt не продолжил обработку событий"
            value = int(sha.split(":")[1])
            assert kind == "brightness" and plan == core.build_brightness_plan(value)
            calls.append((value, detach_ok))
            if failure:
                raise failure
            for index in range(len(plan)):
                progress(index + 1, len(plan))

        def settle(page):
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if page.worker is None else None)
            timer.start(5)
            QTimer.singleShot(4000, loop.quit)
            loop.exec()
            timer.stop()
            assert page.worker is None, "SendWorker не завершился"

        app = QApplication([])
        app.setFont(gui.theme.application_font())
        app.setStyleSheet(gui.STYLE)
        with (patch.object(core, "do_send", side_effect=fake_send),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(gui, "systemctl", side_effect=forbidden),
              patch.object(gui, "unit_installed", return_value=False),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "warning") as warning,
              patch.object(QMessageBox, "information"),
              patch.object(QMessageBox, "critical") as critical,
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.show()
            app.processEvents()
            page = window.pages[1][1]
            assert page.s["fps"] == "60" and page.s["max_seconds"] == 15 and page.background_value() == "black"
            assert (page.sld_brightness.minimum(), page.sld_brightness.maximum()) == (0, 100)
            assert page.chk_lcd.isChecked() and page.sld_brightness.value() == 100
            assert "не считано" in page.lbl_brightness_status.text()
            # Весь диапазон и сборка планов без передачи; настройки локальны.
            for value in range(101):
                page.sld_brightness.setValue(value)
                plan = core.build_brightness_plan(value)
                assert int.from_bytes(plan[0][1][9:13], "little") == value
                assert plan[1:] == core.build_save_plan()
                assert f"{value}%" in page.lbl_brightness.text()
                assert page.chk_lcd.isChecked() == (value > 0)
            assert not calls and page.worker is None
            page.sld_brightness.setValue(37)
            page.chk_lcd.click()
            assert page.sld_brightness.value() == 0 and page.s["brightness"] == 37
            assert core.load_settings()["lcd_enabled"] is False
            restored = gui.LcdPage(core.load_settings())
            assert restored.sld_brightness.value() == 0 and restored.s["brightness"] == 37
            restored.chk_lcd.click()
            assert restored.sld_brightness.value() == 37
            restored.timer.stop()
            restored.close()
            page.chk_lcd.click()
            assert page.sld_brightness.value() == 37
            page.btn_brightness.click()  # отказ от подтверждения
            assert question.call_count == 1 and not calls and page.worker is None
            question.return_value = QMessageBox.StandardButton.Yes
            with patch.object(page, "confirm_previous", return_value=False):
                page.btn_brightness.click()
            assert not calls
            # Настоящий SendWorker; Qt продолжает работать, другие записи заблокированы.
            for value in (37, 0, 100):
                page.sld_brightness.setValue(value)
                release.clear()
                page.btn_brightness.click()
                assert page.worker.kind == "brightness"
                assert not page.panel.isEnabled() and not page.brightness_panel.isEnabled()
                count = len(calls)
                page.apply_brightness()
                assert not hasattr(page, "direction_dialog")
                assert len(calls) == count
                QTimer.singleShot(10, release.set)
                settle(page)
                assert calls[-1] == (value, True)
                assert page.lbl_brightness_status.text() == "Отправлено"
                assert "не подтверждён" in page.lbl_brightness_status.toolTip()
                assert page.panel.isEnabled() and page.brightness_panel.isEnabled()
                assert page.prog.value() == len(core.build_brightness_plan(value))
            # Ошибки доступа, отключения, частичной записи и неожиданные исключения.
            for error in (core.S360Error("нет доступа к USB"), core.S360Error("LCD не найден"),
                          core.S360Error("короткая запись", uncertain=True), OSError("USB отключён")):
                failure = error
                page.sld_brightness.setValue(54)
                page.btn_brightness.click()
                settle(page)
                assert critical.called and "Ошибка: " in page.lbl_brightness_status.text()
                assert page.pending_brightness is None and page.panel.isEnabled()
                assert core.load_settings()["brightness"] == 54
                assert core.load_settings()["last_file"] is None
            with patch.object(core, "save_settings", side_effect=OSError("нет доступа к settings.json")):
                page.sld_brightness.setValue(55)
            assert "не сохранены" in page.lbl_brightness_status.text()
            assert core.load_settings()["brightness"] == 54
            assert not core.STATE.exists()  # ни реальной отправки, ни изменения журнала
            assert page.s["device_direction"] == core.DEFAULT_SETTINGS["device_direction"]
            assert not warning.called and not errors, errors
            page.timer.stop()
            QTimer.singleShot(0, window.close)
            QTimer.singleShot(50, app.quit)
            assert app.exec() == 0 and not window.isVisible()
        print("T6 ok: 0–100%, питание, подтверждения, настройки, SendWorker, ошибки; физической отправки нет")


if __name__ == "__main__":
    main()
