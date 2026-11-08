#!/usr/bin/env python3
"""T20: десять проверок; настоящие Qt/потоки, чтение и отправка подменены."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='s360-t20-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
        os.environ['XDG_STATE_HOME'] = str(Path(tmp) / 'state')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
        import s360_gui as gui
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError('Реальное оборудование или сервис')

        app = QApplication([])
        main_thread = threading.get_ident()
        release = threading.Event()
        errors, rpm_threads, gpu_threads, sends = [], [], [], []
        rpm_error = False
        gpu_data = {'vendor': 'amd', 'core': 45, 'hotspot': 57, 'vram': 52}

        def rpm_read():
            rpm_threads.append(threading.get_ident())
            assert release.wait(3), 'GUI заблокировал обработку событий'
            if rpm_error:
                raise core.S360Error('кулер не ответил')
            return {'rpm': {'radiator': 444, 'waterblock': 0, 'pump': 2255}}

        def gpu_read():
            gpu_threads.append(threading.get_ident())
            return dict(gpu_data)

        def fake_send(plan, detach_ok, sha, progress, cancel, kind):
            assert threading.get_ident() != main_thread and kind == 'clock'
            sends.append((plan, sha))
            progress(len(plan), len(plan))

        def wait_for(predicate):
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if predicate() else None)
            timer.start(5)
            QTimer.singleShot(4000, loop.quit)
            loop.exec()
            timer.stop()
            app.processEvents()
            assert predicate(), 'Ожидаемое событие не наступило'

        with ExitStack() as stack:
            access = stack.enter_context(patch.object(gui, 'device_access', return_value='ready'))
            stack.enter_context(patch.object(gui, 'unit_installed', return_value=False))
            stack.enter_context(patch.object(gui, 'systemctl', side_effect=forbidden))
            stack.enter_context(patch.object(gui.subprocess, 'run', side_effect=forbidden))
            stack.enter_context(patch.object(gui.monitor, 'read_cooler_status', side_effect=rpm_read))
            stack.enter_context(patch.object(gui.monitor, 'read_gpu_temps', side_effect=gpu_read))
            stack.enter_context(patch.object(core, 'usb_present', return_value=True))
            stack.enter_context(patch.object(core, 'read_cpu_temp', return_value=51))
            stack.enter_context(patch.object(core, 'find_hidraw', side_effect=forbidden))
            stack.enter_context(patch.object(core, '_write_reports', side_effect=forbidden))
            stack.enter_context(patch.object(core, 'do_send', side_effect=fake_send))
            build = stack.enter_context(patch.object(core, 'build_clock_plan', wraps=core.build_clock_plan))
            question = stack.enter_context(patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No))
            warning = stack.enter_context(patch.object(QMessageBox, 'warning'))
            stack.enter_context(patch.object(QMessageBox, 'information'))
            stack.enter_context(patch.object(QMessageBox, 'critical'))
            stack.enter_context(patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args)))
            window = gui.MainWindow(); window.show()
            dashboard, lcd, cooling = window.dashboard, window.pages[1][1], window.pages[2][1]
            assert window.tabs.tabText(0) == window.page_title.text() == 'Мониторинг'
            assert window.stack.currentIndex() == 0 and window.overview_states.currentWidget() is dashboard
            assert not hasattr(window, 'btn_first_run') and not gui.load_ui_settings().get('first_run_done')  # 1
            QTimer.singleShot(30, release.set)
            wait_for(lambda: dashboard.radiator_rpm.text() == '444 об/мин')
            assert dashboard.pump_rpm.text() == '2255 об/мин' and dashboard.cpu.text() == '51 °C'
            assert rpm_threads and all(t != main_thread for t in rpm_threads + gpu_threads)
            assert dashboard.cooler_timer.interval() == window.timer.interval() == 2000
            assert dashboard.gpu_timer.interval() == 5000  # 2
            rpm_error = True
            before = lcd.log.toPlainText()
            for _ in range(2):
                dashboard.poll_cooler()
                wait_for(lambda: not dashboard.cooler_worker.isRunning())
                assert dashboard.radiator_rpm.text() == dashboard.pump_rpm.text() == '—'
                assert 'Нет ответа от кулера' in window.overview_badge.text()
            assert lcd.log.toPlainText() == before and not warning.called  # 3
            rpm_error = False
            dashboard.poll_cooler()
            wait_for(lambda: dashboard.radiator_rpm.text() == '444 об/мин')
            assert 'Нет ответа от кулера' not in window.overview_badge.text()
            labels = [label.text() for label in dashboard.findChildren(QLabel)]
            assert not any('жидкост' in text.lower() or text == 'Вентилятор водоблока' for text in labels)
            assert not dashboard.gpu_box.isHidden()
            assert [dashboard.gpu_values[k][1].text() for k in ('core', 'hotspot', 'vram')] == ['45 °C', '57 °C', '52 °C']  # 4
            for data in ({'core': 47, 'hotspot': None, 'vram': None},
                         {'core': None, 'hotspot': 60, 'vram': None},
                         {'core': None, 'hotspot': None, 'vram': None}):
                gpu_data = data
                dashboard.poll_gpu()
                wait_for(lambda: not dashboard.gpu_worker.isRunning())
                for key, pair in dashboard.gpu_values.items():
                    assert all(widget.isHidden() == (data[key] is None) for widget in pair)
                    assert pair[1].text() == (f'{data[key]} °C' if data[key] is not None else '')
                assert dashboard.gpu_box.isHidden() == all(v is None for v in data.values())  # 5
            lcd.mode_buttons['clock'].click()
            assert list(lcd.clock_style_buttons) == [1, 2, 3, 4, 5]
            assert lcd.clock_preview.text() == '07 : 24' and lcd.clock_preview_note.text() == 'Время берётся из системы'
            for style, button in lcd.clock_style_buttons.items():
                button.click()
                assert lcd.s['clock_style'] == gui.load_ui_settings()['clock_style'] == style and button.isChecked()
                assert json.loads(core.SETTINGS.read_text())['clock_style'] == style
                assert len(sends) == style - 1  # выбор стиля сам по себе не отправляет
                question.return_value = QMessageBox.StandardButton.Yes
                lcd.btn_clock.click()
                wait_for(lambda: lcd.worker is None)
                assert build.call_args.args == (style,) and sends[-1][1] == f'clock:{style}'
                assert lcd.lbl_clock_status.text() == 'Отправлено'
            assert len(sends) == build.call_count == 5  # 6
            restored = gui.LcdPage(gui.load_ui_settings())
            assert restored.clock_style_buttons[5].isChecked()
            restored.timer.stop(); restored.clock_timer.stop(); restored.close()  # 7
            lcd.mode_buttons['media'].click(); lcd.src = '/tmp/globe.gif'
            question.return_value = QMessageBox.StandardButton.No
            with (patch.object(core, 'load_media', return_value=bytes(22 * 1024)),
                  patch.object(core, 'build_upload_plan', side_effect=forbidden)):
                for duration, rate, risky in ((4.5, '60', False), (8.95, '60', False), (8.96, '60', True), (2, '61', True)):
                    with patch.object(core, 'probe_mp4', return_value=(duration, 1, rate)):
                        lcd.send()
                    title, text = question.call_args.args[1:3]
                    assert title == 'Загрузить на экран?' and len(text.splitlines()) == 2 + risky
                    assert text.startswith('globe.gif · ') and ' · 22 КБ\nКартинка на экране будет заменена.' in text
                    assert ('Файл длиннее проверенного — возможен сбой.' in text) == risky
                    assert not any(word in text for word in ('эталон', 'SHA', core.SLOT_NAME.decode(), 'HID', 'Отката нет'))  # 8
            lcd.mode_buttons['clock'].click(); lcd.btn_clock.click()
            assert len(question.call_args.args[2].splitlines()) <= 2
            lcd.sld_brightness.setValue(42); lcd.btn_brightness.click()
            assert len(question.call_args.args[2].splitlines()) <= 2
            cooling.buttons['balance'].click()
            with patch.object(gui, 'unit_installed', return_value=True):
                cooling.apply()
            assert len(question.call_args.args[2].splitlines()) <= 2
            for source in ('s360_gui.py', 'theme.py'):
                assert 'эталон' not in (Path(__file__).resolve().parents[1] / source).read_text().lower()  # 9
            assert not errors and not core.STATE.exists()
            window.close(); app.processEvents()
            assert not dashboard.cooler_timer.isActive() and not dashboard.gpu_timer.isActive()
            assert not dashboard.cooler_worker.isRunning() and not dashboard.gpu_worker.isRunning()  # 10
        print('T20 ok: 10 проверок; мониторинг, потоки, ошибки, GPU, пять стилей, короткие диалоги; отправок нет')


if __name__ == '__main__':
    main()
