#!/usr/bin/env python3
"""T21: 10 проверок, настоящие Qt/потоки, без устройства; масштабы 100% и 150%."""
from contextlib import ExitStack
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix='s360-t21-') as tmp:
        tmp = Path(tmp)
        for variable, folder in (('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')):
            os.environ[variable] = str(tmp / folder)
        from PySide6.QtCore import QEventLoop, QTimer, Qt
        from PySide6.QtGui import QImage, QColor
        from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
        import s360_gui as gui
        import s360_lcd as core

        app = QApplication([])
        main_thread = threading.get_ident()
        release = threading.Event()
        calls, sends, errors, windows = [], [], [], []
        fail_send = False
        fixtures = {}
        for name, size in (('portrait', (240, 320)), ('landscape', (320, 240)), ('thumb', (120, 160))):
            image = QImage(*size, QImage.Format.Format_RGB32)
            image.fill(QColor('#B72F3D'))
            target = tmp / (name + '.png')
            assert image.save(str(target))
            fixtures[name] = target.read_bytes()
        folder = tmp / 'library'; folder.mkdir()
        for name in ('a.gif', 'B.MP4', 'broken.gif', 'text.txt', '.hidden.gif'):
            (folder / name).write_bytes(b'fixture')
        (folder / 'nested').mkdir(); (folder / 'nested' / 'hidden.mp4').write_bytes(b'fixture')

        def forbidden(*args, **kwargs):
            raise AssertionError('Реальное устройство, сервис или преобразование ядра из потока миниатюр')

        def thumbnail(source, cache_dir):
            assert threading.get_ident() != main_thread
            calls.append((str(source), dict(core.ENC)))
            assert release.wait(3), 'Окно не обрабатывает события при открытии папки'
            if Path(source).name == 'broken.gif':
                return None
            target = Path(cache_dir) / (Path(source).name + '.png')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fixtures['thumb'])
            return target

        def preview(source, target, *, viewer):
            assert threading.get_ident() == main_thread and viewer is True
            target.write_bytes(fixtures['landscape' if core.ENC['orient'] in (90, 270) else 'portrait'])

        def send(plan, detach_ok, sha, progress, cancel, kind):
            assert threading.get_ident() != main_thread
            sends.append(kind)
            if fail_send:
                raise core.S360Error('проверка ошибки', uncertain=True)
            progress(len(plan), len(plan))

        def wait_for(predicate):
            loop = QEventLoop(); timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if predicate() else None)
            timer.start(5); QTimer.singleShot(4000, loop.quit); loop.exec(); timer.stop()
            app.processEvents()
            assert predicate(), 'Ожидаемое событие не наступило'

        with ExitStack() as stack:
            for boundary in (
                patch.object(gui, 'device_access', return_value='ready'),
                patch.object(gui, 'unit_installed', return_value=False),
                patch.object(gui, 'systemctl', side_effect=forbidden),
                patch.object(gui.monitor, 'read_cooler_status', return_value={'rpm': {'radiator': 444, 'pump': 2255}}),
                patch.object(gui.monitor, 'read_gpu_temps', return_value={}),
                patch.object(core, 'usb_present', return_value=True),
                patch.object(core, 'read_cpu_temp', return_value=50),
                patch.object(core, 'find_hidraw', side_effect=forbidden),
                patch.object(core, '_write_reports', side_effect=forbidden),
                patch.object(core, 'do_send', side_effect=send),
                patch.object(core, 'build_vf', side_effect=forbidden),
                patch.object(core, 'preview_png', side_effect=preview),
                patch.object(core, 'load_media', return_value=b'prepared'),
                patch.object(core, 'probe_mp4', return_value=(4.5, 270, '60')),
                patch.object(gui.thumbs, 'ensure_thumb', side_effect=thumbnail),
                patch.object(gui.subprocess, 'run', side_effect=forbidden),
                patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
                patch.object(QMessageBox, 'information'), patch.object(QMessageBox, 'warning'),
                patch.object(QMessageBox, 'critical'),
                patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args)),
            ):
                stack.enter_context(boundary)
            try:
                window = gui.MainWindow(); windows.append(window); window.show(); window.stack.setCurrentIndex(1)
                lcd = window.pages[1][1]; library = lcd.library
                assert library.note.text() == 'Выбери папку с GIF — здесь появятся превью' and library.grid.isHidden()
                assert window.dashboard.last_sent_box.title() == 'Последнее отправленное' and window.dashboard.last_sent.isHidden()  # 1
                before = dict(core.ENC)
                library.open_folder(os.path.relpath(folder), save=True)
                assert [library.grid.item(i).text() for i in range(library.grid.count())] == ['a.gif', 'B.MP4', 'broken.gif']
                assert all(item.data(Qt.ItemDataRole.UserRole + 1) == 'pending' and not item.icon().isNull() for item in library.items.values())
                QTimer.singleShot(30, release.set)
                wait_for(lambda: library.worker is None)
                assert core.ENC == before and all(snapshot == before for _, snapshot in calls)
                assert 'не открывается' in library.items[str(folder / 'broken.gif')].toolTip()
                assert library.cache_dir == tmp / 'cache' / 's360-control' / 'thumbs'  # 2
                with patch.object(lcd, 'update_media_details'):
                    item = library.grid.item(0)
                    library.grid.itemClicked.emit(item)
                    assert lcd.src == str(folder / 'a.gif') and lcd.lbl_file.text() == 'a.gif'
                    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(folder / 'B.MP4'), '')):
                        lcd.btn_pick.click()
                assert library.grid.selectedItems() == [library.items[str(folder / 'B.MP4')]] and not sends  # 3
                assert gui.load_ui_settings()['gif_dir'] == str(folder)
                restored = gui.MainWindow(); windows.append(restored)
                wait_for(lambda: restored.pages[1][1].library.worker is None)
                assert restored.pages[1][1].library.grid.count() == 3
                restored.close()  # 4
                many = tmp / 'many'; many.mkdir()
                for i in range(105): (many / f'{i:03}.gif').write_bytes(b'fixture')
                library.open_folder(str(many))
                assert library.grid.count() == 100 and not library.limit_note.isHidden()
                wait_for(lambda: library.worker is None)
                library.open_folder(str(tmp / 'missing'))
                assert library.grid.count() == 0 and library.grid.isHidden() and not library.note.isHidden()  # 5
                for position, fixture in ((90, 'landscape'), (0, 'portrait')):
                    lcd.position_buttons[position].click()
                    lcd.send(); wait_for(lambda: lcd.worker is None)
                    assert gui.last_sent_path().read_bytes() == fixtures[fixture]
                    shown = window.dashboard.last_sent
                    assert not shown.isHidden() and shown.width() * (3 if position == 90 else 4) == shown.height() * (4 if position == 90 else 3)
                    assert window.dashboard.last_file.text() == 'B.MP4' and 'Отправлено' in window.dashboard.last_upload.text()  # 6
                original = gui.last_sent_path().read_bytes()
                fail_send = True
                lcd.position_buttons[90].click(); lcd.send(); wait_for(lambda: lcd.worker is None)
                assert gui.last_sent_path().read_bytes() == original
                fail_send = False
                lcd.mode_buttons['clock'].click(); lcd.btn_clock.click(); wait_for(lambda: lcd.worker is None)
                assert gui.last_sent_path().read_bytes() == original  # 7
                release.clear()
                library.open_folder(str(folder))
                wait_for(lambda: library.worker is not None and library.worker.isRunning())
                library.open_folder(str(tmp / 'missing'))
                release.set(); wait_for(lambda: library.worker is None)
                assert library.grid.count() == 0 and not library.items  # 8
                app.processEvents()
                heading = lcd.clock_heading.geometry()
                assert lcd.clock_panel.rect().contains(heading) and heading.top() >= 16
                assert heading.height() >= lcd.clock_heading.fontMetrics().height()
                assert lcd.lbl_clock_time.geometry().top() > heading.bottom()
                assert list(lcd.clock_style_buttons) == [1, 2, 3, 4, 5]  # 9
                imports = [alias.name for node in ast.parse((root / 's360_gui.py').read_text()).body if isinstance(node, ast.Import) for alias in node.names]
                local = [name for name in imports if (root / (name + '.py')).exists()]
                installer = (root / 'install.sh').read_text()
                assert set(local) == {'s360_lcd', 'theme', 'monitor', 'thumbs'}
                for name in local:
                    assert f'install -Dm644 "$HERE/{name}.py" "$APP/{name}.py"' in installer
                assert not errors and not core.STATE.exists()  # 10
            finally:
                release.set()
                for window in windows:
                    window.close()
                app.processEvents()
        print(f'T21 ok: 10 проверок; библиотека, фон, настройки, последняя отправка, часы, установка; масштаб {os.environ.get("QT_SCALE_FACTOR", "1")}; отправок нет')
    if os.environ.get('QT_SCALE_FACTOR') != '1.5':
        env = dict(os.environ, QT_SCALE_FACTOR='1.5')
        subprocess.run([sys.executable, str(Path(__file__).resolve())], env=env, check=True)


if __name__ == '__main__':
    main()
