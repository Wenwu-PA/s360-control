#!/usr/bin/env python3
"""T12: десять проверок каркаса; Qt offscreen, без USB и реальных сервисов."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t12-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QStackedWidget
        import s360_gui as gui
        # Проверки прежних экранов: состояние подключения изолировано от хоста.
        patch.object(gui, "device_access", return_value="ready").start()
        import s360_lcd as core
        import theme

        def forbidden(*args, **kwargs):
            raise AssertionError("Обращение к USB или реальному сервису")

        app = QApplication([])
        app.setFont(theme.application_font())
        app.setStyleSheet(theme.build_qss())
        errors = []
        with (patch.object(core, "usb_present", return_value=False),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(core, "do_send", side_effect=forbidden),
              patch.object(gui, "unit_installed", return_value=False),
              patch.object(gui, "systemctl", side_effect=forbidden),
              patch.object(gui.subprocess, "run", side_effect=forbidden),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "information") as information,
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            assert len(theme.COLORS) == 16 and theme.COLORS["accent"] == "#B72F3D"  # 1
            assert theme.SPACING == (8, 16, 24, 32) and theme.RADII == (4, 8) and theme.BORDER_WIDTH == 1  # 2
            assert app.font().pixelSize() == 14 and all(c in gui.STYLE for c in theme.COLORS.values())  # 3
            window = gui.MainWindow()
            window.settings["first_run_done"] = True
            window.tick()
            window.show()
            app.processEvents()
            assert isinstance(window.stack, QStackedWidget) and window.stack.count() == window.tabs.count() == 4  # 4
            for index, (title, _) in enumerate(window.pages):
                window.tabs.setCurrentIndex(index)
                app.processEvents()
                assert window.stack.currentIndex() == window.footer_stack.currentIndex() == index
                assert window.page_title.text() == title
            window.stack.setCurrentIndex(0)
            assert window.tabs.currentIndex() == 0  # 5: оба направления навигации
            dashboard = window.dashboard
            lcd, cooling = window.pages[1][1], window.pages[2][1]
            assert not window.footer_stack.widget(0).findChildren(gui.QPushButton)
            window.tabs.setCurrentIndex(1)
            assert window.stack.currentIndex() == 1  # 6
            lcd.btn_send.click()
            assert information.called and window.footer.isAncestorOf(lcd.btn_send) and lcd.worker is None  # 7
            lcd.mode_buttons["clock"].click()
            lcd.btn_clock.click()
            assert question.called and lcd.worker is None and lcd.btn_send.isHidden() and not lcd.btn_clock.isHidden()
            lcd.mode_buttons["media"].click()
            assert lcd.btn_clock.isHidden() and not lcd.btn_send.isHidden()  # 8
            window.stack.setCurrentIndex(2)
            assert window.footer.isAncestorOf(cooling.btn_apply) and window.footer.isAncestorOf(cooling.status)
            assert not cooling.btn_apply.isEnabled() and "не установлен" in cooling.status.text()  # 9
            scroll = cooling.findChild(QScrollArea)
            assert isinstance(scroll, QScrollArea) and not scroll.isAncestorOf(window.footer)
            window.close()
            app.processEvents()
            assert not window.isVisible() and not errors and not core.STATE.exists()  # 10
        print("T12 ok: 10 проверок; токены, шрифт, навигация, закреплённая полоса, прежние кнопки и подтверждение; отправок нет")


if __name__ == "__main__":
    main()
