#!/usr/bin/env python3
"""T7: настоящие Qt и SendWorker, изолированные настройки, USB-граница подменена.

Запуск: python3 tools/check_t7.py. Физическая отправка запрещена.
"""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t7-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError("Физическая отправка и реальные сервисы запрещены")

        calls, errors = [], []
        failure = None
        release = threading.Event()
        main_thread = threading.get_ident()
        real_datetime = datetime.datetime

        class Clock(real_datetime):
            current = real_datetime(2026, 11, 4, 12, 34, 56, tzinfo=datetime.timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        def fake_send(plan, detach_ok, sha, progress, cancel, kind):
            assert threading.get_ident() != main_thread
            assert release.wait(3), "GUI не обработал событие таймера"
            assert kind == "clock" and sha == "clock:1"
            calls.append((plan, detach_ok))
            if failure:
                raise failure
            if cancel():
                raise core.S360Error("отменено пользователем", uncertain=True)
            for i in range(len(plan)):
                progress(i + 1, len(plan))

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
        app.setStyleSheet(gui.STYLE)
        with (patch.object(core, "do_send", side_effect=fake_send),
              patch.object(core, "find_hidraw", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.dict(sys.modules, {"usb.core": None, "usb.util": None}),
              patch.object(gui, "systemctl", side_effect=forbidden),
              patch.object(gui, "unit_installed", return_value=False),
              patch.object(core, "build_clock_plan", wraps=core.build_clock_plan) as build,
              patch.object(gui.datetime, "datetime", Clock),
              patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
              patch.object(QMessageBox, "warning") as warning,
              patch.object(QMessageBox, "information"),
              patch.object(QMessageBox, "critical") as critical,
              patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.show()
            app.processEvents()
            window.stack.setCurrentIndex(1)
            page = window.pages[1][1]
            assert page.s["lcd_mode"] == "media" and page.clock_panel.isHidden()
            page.mode_buttons["clock"].click()
            assert page.mode_buttons["clock"].isChecked() and not page.clock_panel.isHidden()
            assert page.panel.isHidden() and page.preview_box.isHidden()
            assert "№1" in page.clock_panel.title()
            assert "Местное время" in page.lbl_clock_time.text()
            assert core.load_settings()["lcd_mode"] == "clock"
            restored = gui.LcdPage(core.load_settings())
            assert restored.mode_buttons["clock"].isChecked()
            assert not restored.clock_panel.isHidden() and restored.worker is None
            restored.timer.stop()
            restored.clock_timer.stop()
            restored.close()
            loop = QEventLoop()
            QTimer.singleShot(1100, loop.quit)
            loop.exec()  # Настоящий таймер обновляет только подпись, без синхронизации LCD.
            assert not calls and not build.called  # запуск, выбор и таймер не отправляют
            page.btn_clock.click()  # отмена подтверждения
            assert question.called and not build.called and page.worker is None
            question.return_value = QMessageBox.StandardButton.Yes
            with patch.object(page, "confirm_previous", return_value=False):
                page.btn_clock.click()
            assert not build.called

            # Время меняется во время подтверждения: в backend попадёт новое значение.
            for offset in (10800, -18000):
                os.environ["TZ"] = "Etc/GMT-3" if offset > 0 else "Etc/GMT+5"
                time.tzset()
                tz = datetime.timezone(datetime.timedelta(seconds=offset))
                Clock.current = real_datetime(2026, 11, 4, 1, 2, 3, tzinfo=tz)
                page.update_clock_time()
                def approve(*args, **kwargs):
                    Clock.current = real_datetime(2026, 11, 5, 4, 5, 6, tzinfo=tz)
                    return QMessageBox.StandardButton.Yes
                question.side_effect = approve
                page.chk_clock_detach.setChecked(True)
                assert page.chk_detach.isChecked()
                release.clear()
                page.btn_clock.click()
                assert page.worker.kind == "clock"
                assert not page.panel.isEnabled() and not page.brightness_panel.isEnabled()
                assert not page.clock_panel.isEnabled()
                assert not page.mode_buttons["media"].isEnabled()
                page.apply_clock()
                page.apply_brightness()
                page.apply_direction()
                window.close()
                assert window.isVisible() and warning.called  # закрытие во время записи запрещено
                QTimer.singleShot(10, release.set)
                settle(page)
                # now() возвращает местное время системы для того же момента.
                local = Clock.current.astimezone()
                expected_offset = int(local.utcoffset().total_seconds())
                assert expected_offset == offset
                assert build.call_args.args == (1,)
                assert build.call_args.kwargs == {"now": local.timestamp(), "tz_offset": expected_offset}
                assert calls[-1][0] == core.build_clock_plan(1, now=local.timestamp(), tz_offset=expected_offset)
                assert calls[-1][1] is True
                assert "Приём LCD не подтверждён" in page.lbl_clock_status.text()
                assert local.strftime("%d.%m.%Y %H:%M:%S %z") in page.lbl_clock_status.text()
                assert page.pending_clock is None and page.prog.value() == 2
                assert page.mode_buttons["media"].isEnabled() and not page.mode_buttons["monitor"].isEnabled()
            question.side_effect = None
            assert len(calls) == 2  # параллельные действия не запустились
            for error in (core.S360Error("нет доступа к USB"), core.S360Error("LCD не найден"),
                          core.S360Error("короткая запись", uncertain=True), OSError("USB отключён")):
                failure = error
                page.btn_clock.click()
                settle(page)
                assert critical.called and "Ошибка отправки часов" in page.lbl_clock_status.text()
                assert str(error) in page.lbl_clock_status.text()
                assert page.pending_clock is None and page.clock_panel.isEnabled()
            failure = None
            release.clear()
            page.btn_clock.click()
            page.btn_cancel.click()
            QTimer.singleShot(10, release.set)
            settle(page)
            assert "отменено" in page.lbl_clock_status.text()
            page.mode_buttons["media"].click()
            assert page.clock_panel.isHidden() and not page.panel.isHidden()
            assert not page.preview_box.isHidden() and core.load_settings()["lcd_mode"] == "media"
            with patch.object(core, "save_settings", side_effect=OSError("нет доступа к settings.json")):
                page.mode_buttons["clock"].click()
            assert warning.call_args.args[1] == "Настройки не сохранены"
            assert core.load_settings()["lcd_mode"] == "media"
            assert page.s["last_file"] is None and page.s["last_upload_time"] is None
            assert page.s["lcd_enabled"] and page.s["brightness"] == 100
            assert not core.STATE.exists() and not errors, errors
            page.timer.stop()
            page.clock_timer.stop()
            QTimer.singleShot(0, window.close)
            QTimer.singleShot(50, app.quit)
            assert app.exec() == 0 and not window.isVisible()
        print("T7 ok: выбор, местное время перед отправкой, настройки, поток Qt, ошибки, отмена; физической отправки нет")


if __name__ == "__main__":
    main()
