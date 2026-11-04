#!/usr/bin/env python3
"""T4: реальные Qt-виджеты, изолированные настройки, сервис и USB не вызываются.

Запуск: QT_QPA_PLATFORM=offscreen python3 tools/check_t4.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="s360-t4-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        import s360_lcd as core

        installed, state, enabled = False, "inactive", False
        fail_command = None
        calls, errors = [], []

        def service(*args):
            nonlocal state, enabled
            assert args[-1] == gui.UNIT
            command = args[0]
            if command == "is-active":
                return (0 if state == "active" else 3), state
            if command == "is-enabled":
                return (0, "enabled") if enabled else (1, "disabled")
            assert command in ("start", "restart", "stop", "enable", "disable")
            calls.append(command)
            if command == fail_command:
                return 1, "ошибка systemctl"
            if command in ("start", "restart"):
                assert core.COOLING_ENV.read_text() in ("MODE=silent\n", "MODE=balance\n", "MODE=game\n")
                state = "active"
            elif command == "stop":
                state = "inactive"
            else:
                enabled = command == "enable"
            return 0, ""

        def forbidden(*args, **kwargs):
            raise AssertionError("Попытка обращения к оборудованию или реальному сервису")

        app = QApplication([])
        app.setStyleSheet(gui.STYLE)
        with (patch.object(gui, "systemctl", side_effect=service),
              patch.object(gui, "unit_installed", side_effect=lambda: installed),
              patch.object(gui.subprocess, "run", side_effect=forbidden),
              patch.object(core, "do_send", side_effect=forbidden),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "warning") as warning,
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.show()
            app.processEvents()
            page = window.pages[2][1]
            for index in range(3):
                window.stack.setCurrentIndex(index)
                app.processEvents()
            assert page.mode() == "balance"
            assert not page.btn_apply.isEnabled() and not page.btn_service.isEnabled()
            assert not page.chk_auto.isEnabled() and "не установлен" in page.status.text()
            assert not calls and not core.COOLING_ENV.exists()

            installed = True
            page.refresh()
            assert page.btn_service.text() == "Запустить" and page.btn_service.isEnabled()
            for mode in gui.COOLING_TITLES:
                page.buttons[mode].click()
                assert page.mode() == mode
                assert sum(b.isChecked() for b in page.buttons.values()) == 1
            # Отмена обоих подтверждений ничего не записывает и не запускает.
            page.btn_service.click()
            page.btn_apply.click()
            assert question.call_count == 2 and not calls and not core.COOLING_ENV.exists()
            question.return_value = QMessageBox.StandardButton.Yes
            for mode in gui.COOLING_TITLES:
                page.buttons[mode].click()
                page.btn_apply.click()
                assert calls[-1] == "restart" and core.COOLING_ENV.read_text() == f"MODE={mode}\n"
                assert core.load_settings()["cooling_mode"] == mode
                assert page.btn_service.text() == "Остановить"
                assert "не подтверждён" in page.status.text()
            # Сохранённый выбор не выдаётся за прочитанный режим устройства.
            assert "нет данных" in page.last_mode.text()
            core.COOLING_STATE.parent.mkdir(parents=True, exist_ok=True)
            core.COOLING_STATE.write_text(json.dumps({"mode": "silent"}))
            page.refresh()
            assert "Тихий" in page.last_mode.text() and page.mode() == "game"
            core.COOLING_STATE.write_text("invalid json")
            page.refresh()
            assert "нет данных" in page.last_mode.text()

            page.btn_service.click()
            assert calls[-1] == "stop" and page.btn_service.text() == "Запустить"
            page.btn_service.click()
            assert calls[-1] == "start" and page.btn_service.text() == "Остановить"
            for expected in (True, False):
                page.chk_auto.click()
                assert enabled == expected and page.chk_auto.isChecked() == expected
            fail_command = "enable"
            page.chk_auto.click()
            assert not page.chk_auto.isChecked() and warning.call_args.args[1] == "Ошибка"
            fail_command = "stop"
            page.btn_service.click()
            assert page.btn_service.text() == "Остановить"
            assert warning.call_args.args[1] == "Сервис не остановлен"
            fail_command = "restart"
            page.btn_apply.click()
            assert warning.call_args.args[1] == "Сервис не запущен"
            fail_command = None
            state = "failed"
            page.refresh()
            assert "Ошибка сервиса" in page.status.text() and page.btn_service.isEnabled()
            state = "Failed to connect to bus"
            page.refresh()
            assert "неизвестен" in page.status.text() and not page.btn_service.isEnabled()
            state = "inactive"
            page.refresh()
            count = len(calls)
            with patch.object(Path, "write_text", side_effect=OSError("нет доступа")):
                page.btn_apply.click()
            assert len(calls) == count and warning.call_args.args[1] == "Ошибка"
            with patch.object(core, "save_settings", side_effect=OSError("нет доступа")):
                page.btn_apply.click()
            assert calls[-1] == "restart" and warning.call_args.args[1] == "Настройки не сохранены"
            # Повторное открытие использует локально сохранённый выбор.
            restored = gui.CoolingPage(core.load_settings())
            assert restored.mode() == "game"
            restored.close()
            assert not core.STATE.exists() and window.pages[1][1].worker is None
            window.pages[1][1].timer.stop()
            QTimer.singleShot(0, window.close)
            QTimer.singleShot(50, app.quit)
            assert app.exec() == 0 and not window.isVisible()
            assert not errors, errors
        print("T4 ok: Qt запуск/закрытие, вкладки, режимы, подтверждения, сервис, ошибки, настройки; отправок нет")


if __name__ == "__main__":
    main()
