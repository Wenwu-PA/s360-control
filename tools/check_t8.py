#!/usr/bin/env python3
"""T8: настоящий Qt, временные файлы; USB и системные действия запрещены."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t8-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        # Проверки прежних экранов: состояние подключения изолировано от хоста.
        patch.object(gui, "device_access", return_value="ready").start()
        import s360_lcd as core

        connected, state, fail = True, "inactive", False
        calls, errors = [], []

        def forbidden(*args, **kwargs):
            raise AssertionError("Реальный USB или сервис запрещён")

        def service(*args):
            nonlocal state
            if args[0] == "is-active":
                return (0 if state == "active" else 3), state
            if args[0] == "is-enabled":
                return 1, "disabled"
            assert args[0] in ("start", "restart", "stop", "enable", "disable")
            calls.append(args[0])
            if fail and args[0] == "restart":
                return 1, "ошибка systemctl"
            if args[0] in ("enable", "disable"):
                return 0, ""
            if args[0] != "stop":
                assert core.COOLING_ENV.read_text() == "MODE=custom\n"
                assert core.load_curves() == page.editor_curves()
                state = "active"
            else:
                state = "inactive"
            return 0, ""

        app = QApplication([])
        app.setFont(gui.theme.application_font())
        app.setStyleSheet(gui.STYLE)
        with (patch.object(gui, "systemctl", side_effect=service),
              patch.object(gui, "unit_installed", return_value=True) as installed,
              patch.object(core, "usb_present", side_effect=lambda *args: connected),
              patch.object(core, "validate_curves", wraps=core.validate_curves) as validate,
              patch.object(core, "do_send", side_effect=forbidden),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(gui.subprocess, "run", side_effect=forbidden),
              patch.object(gui.CoolingPage, "journal_line", return_value="Ошибка из журнала"),
              patch.dict(sys.modules, {"usb.core": None, "usb.util": None}),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "warning") as warning,
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.show()
            window.stack.setCurrentIndex(2)
            app.processEvents()
            page = window.pages[2][1]
            page.buttons["custom"].click()
            assert not page.curves_box.isHidden()
            assert page.editor_curves() == core.DEFAULT_CURVES
            assert len(page.cards) == 3
            assert all(len(card.chips) == 4 for card in page.cards.values())
            assert not core.CURVES_FILE.exists() and not calls
            page.btn_apply.click()  # Нет файла — нет запуска.
            assert not question.called and not calls
            page.btn_save_curves.click()
            original = core.CURVES_FILE.read_text()
            page.btn_apply.click()  # Отмена ничего не применяет.
            assert question.called and not core.COOLING_ENV.exists()
            warning.reset_mock()
            page.cards["pump"].move_point(0, 59, 59)
            page.btn_save_curves.click()
            assert page.cards["pump"].graph.values["duty"][0] == 60 and not warning.called
            question.return_value = QMessageBox.StandardButton.Yes
            page.btn_reset_curves.click()
            for channel, key, index, value in (("radiator1", "temp", 1, 48),
                                               ("radiator1", "duty", 1, 1)):
                values = page.cards[channel].graph.values
                page.cards[channel].move_point(index, value if key == "temp" else values["temp"][index],
                                                value if key == "duty" else values["duty"][index])
                page.btn_save_curves.click()
                core.validate_curves(page.raw_curves())
                assert not warning.called
                page.btn_reset_curves.click()
            page.cards["pump"].move_point(1, 68, 72)
            page.btn_save_curves.click()
            saved = core.CURVES_FILE.read_text()
            assert core.load_curves()["pump"]["duty"] == [60, 72, 89, 100]
            assert validate.called and not calls and not core.COOLING_ENV.exists()
            restored = gui.CoolingPage(core.load_settings())
            assert restored.editor_curves() == page.editor_curves()
            restored.close()
            page.btn_reset_curves.click()
            assert page.editor_curves() == core.DEFAULT_CURVES
            assert core.CURVES_FILE.read_text() == saved  # Сброс пока только в редакторе.
            page.cards["radiator1"].move_point(0, 47, 21)
            page.btn_apply.click()
            assert calls == ["enable", "restart"] and core.load_curves() == page.editor_curves()
            assert core.load_curves()["radiator1"]["temp"][0] == 47
            assert core.load_settings()["cooling_mode"] == "custom"
            assert "не подтверждён" in page.status.text()
            page.btn_service.click()
            page.btn_apply.click()
            assert calls[-4:] == ["stop", "disable", "enable", "restart"]
            restored = gui.CoolingPage(core.load_settings())
            assert restored.mode() == "custom" and not restored.curves_box.isHidden()
            restored.close()
            fail = True
            page.btn_apply.click()
            assert warning.call_args.args[1] == "Сервис не запущен"
            fail = False
            state = "failed"
            page.refresh()
            assert "Ошибка сервиса" in page.status.text()
            connected = False
            page.refresh()
            assert not hasattr(page, "cooler_connection")
            count = len(calls)
            page.btn_apply.click()
            assert len(calls) == count and warning.call_args.args[1] == "Устройство не найдено"
            connected = True
            with patch.object(Path, "replace", side_effect=OSError("нет доступа")):
                saved = core.CURVES_FILE.read_text()
                page.btn_save_curves.click()
                assert core.CURVES_FILE.read_text() == saved
                page.btn_apply.click()
            assert len(calls) == count and warning.call_args.args[1] == "Ошибка"
            with patch.object(Path, "write_text", side_effect=OSError("cooling.env недоступен")):
                page.btn_apply.click()
            assert len(calls) == count and warning.call_args.args[1] == "Ошибка"
            with patch.object(core, "save_settings", side_effect=OSError("settings недоступен")):
                page.btn_apply.click()
            assert warning.call_args.args[1] == "Настройки не сохранены"
            for contents in ("не JSON", json.dumps({"pump": None}),
                             json.dumps({channel: None for channel in core.CHANNELS})):
                core.CURVES_FILE.write_text(contents)
                broken = gui.CoolingPage(core.load_settings())
                assert "Не удалось загрузить" in broken.curves_status.text()
                assert broken.editor_curves() == core.DEFAULT_CURVES
                assert core.CURVES_FILE.read_text() == contents
                broken.close()
            installed.return_value = False
            page.refresh()
            assert not page.btn_apply.isEnabled() and page.btn_save_curves.isEnabled()
            assert not core.STATE.exists() and not errors, errors
            window.close()
            app.processEvents()
            assert not window.isVisible()
        print("T8 ok: Qt, 3×4 точки, валидация ядра, JSON, сброс, custom-сервис, ошибки, USB отключён; отправок нет")


if __name__ == "__main__":
    main()
