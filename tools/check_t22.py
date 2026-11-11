#!/usr/bin/env python3
"""T22: 10 проверок Qt, очереди рендера и кадров GIF/MP4; USB запрещён."""
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='s360-t22-') as directory:
        tmp = Path(directory)
        for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')]:
            os.environ[key] = str(tmp / folder)
        from PySide6.QtCore import QEvent, QEventLoop, QPointF, QTimer, Qt
        from PySide6.QtGui import QColor, QImage, QMouseEvent, QTransform
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        import s360_lcd as core
        import pan

        app = QApplication([])
        main_thread = threading.get_ident()
        release = threading.Event(); release.set()
        calls, errors, sends = [], [], []
        fail_send = False
        image = QImage(400, 200, QImage.Format.Format_RGB32)
        for y in range(200):
            for x in range(400):
                image.setPixelColor(x, y, QColor(255 if x < 200 else 0, 255 if y < 100 else 0, 0))
        fixture = tmp / 'source.png'; assert image.save(str(fixture))
        gif, mp4 = tmp / 'source.gif', tmp / 'source.mp4'
        for target in (gif, mp4):
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-loop', '1', '-i', str(fixture),
                            '-t', '0.2', '-r', '10', '-pix_fmt', 'yuv420p', str(target)], check=True)
        png_bytes = fixture.read_bytes()

        def forbidden(*args, **kwargs):
            raise AssertionError('Реальное оборудование или сервис')

        def wait_for(predicate):
            loop, timer = QEventLoop(), QTimer()
            timer.timeout.connect(lambda: loop.quit() if predicate() else None)
            timer.start(5); QTimer.singleShot(10000, loop.quit); loop.exec(); timer.stop()
            app.processEvents()
            assert predicate(), 'Ожидаемое событие не наступило'

        def idle():
            return lcd.preview_worker is None and not lcd.timer.isActive() and lcd.pending_upload is None and lcd.worker is None

        def preview(source, target, *, viewer, enc):
            assert threading.get_ident() != main_thread and viewer
            calls.append((str(source), deepcopy(enc)))
            assert release.wait(5), 'GUI заблокирован'
            target.write_bytes(png_bytes)

        def send(plan, detach_ok, sha, progress, cancel, kind):
            assert threading.get_ident() != main_thread
            sends.append(kind)
            if fail_send:
                raise core.S360Error('Проверка ошибки', uncertain=True)
            progress(len(plan), len(plan))

        def mouse(kind, x, y):
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(lcd.lbl_prev, event)

        with ExitStack() as stack:
            for boundary in [
                patch.object(gui, 'device_access', return_value='ready'),
                patch.object(gui, 'unit_installed', return_value=False),
                patch.object(gui, 'systemctl', side_effect=forbidden),
                patch.object(gui.monitor, 'read_cooler_status', return_value={'rpm': {'radiator': 444, 'pump': 2255}}),
                patch.object(gui.monitor, 'read_gpu_temps', return_value={}),
                patch.object(core, 'usb_present', return_value=False),
                patch.object(core, 'read_cpu_temp', return_value=50),
                patch.object(core, 'find_hidraw', side_effect=forbidden),
                patch.object(core, '_write_reports', side_effect=forbidden),
                patch.object(core, 'do_send', side_effect=send),
                patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
                patch.object(QMessageBox, 'information'), patch.object(QMessageBox, 'warning'),
                patch.object(QMessageBox, 'critical'),
                patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args)),
            ]:
                stack.enter_context(boundary)
            window = gui.MainWindow(); lcd = window.pages[1][1]
            window.show(); window.stack.setCurrentIndex(1); app.processEvents()
            try:
                with patch.object(core, 'preview_png', side_effect=preview):
                    lcd.select_file(gif); wait_for(idle)
                    before = deepcopy(core.ENC)
                    x, y = lcd.lbl_prev.width() / 2, lcd.lbl_prev.height() / 2
                    mouse(QEvent.Type.MouseButtonPress, x, y)
                    assert lcd.lbl_prev.cursor().shape() == Qt.CursorShape.ClosedHandCursor
                    mouse(QEvent.Type.MouseMove, x + 20, y + 20)
                    want = pan.drag('fill', .5, .5, 20 * 240 / lcd.lbl_prev.width(),
                                    20 * 320 / lcd.lbl_prev.height(), 400, 200, 240, 320)
                    assert (lcd.s['focus_x'], lcd.s['focus_y']) == want and want[1] == .5
                    mouse(QEvent.Type.MouseButtonRelease, x + 20, y + 20); wait_for(idle)  # 1
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData('fit')); wait_for(idle)
                    lcd.drag_preview(0, 20)
                    assert lcd.s['focus_y'] > .5 and lcd.s['focus_x'] == .5
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData('stretch')); wait_for(idle)
                    lcd.drag_preview(20, 20)
                    assert (lcd.s['focus_x'], lcd.s['focus_y']) == (.5, .5)
                    assert not lcd.lbl_prev.pannable and lcd.pan_note.isHidden()  # 2
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData('fill')); wait_for(idle)
                    lcd.drag_preview(20, 0); lcd.select_file(mp4); wait_for(idle)
                    assert (lcd.s['focus_x'], lcd.s['focus_y']) == (.5, .5)
                    lcd.drag_preview(20, 0)
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData('fit')); wait_for(idle)
                    assert (lcd.s['focus_x'], lcd.s['focus_y']) == (.5, .5)  # 3
                    lcd.drag_preview(0, 20)
                    mouse(QEvent.Type.MouseButtonDblClick, x, y); wait_for(idle)
                    assert (lcd.s['focus_x'], lcd.s['focus_y']) == (.5, .5)
                    assert lcd.lbl_prev.cursor().shape() == Qt.CursorShape.OpenHandCursor  # 4
                    release.clear(); calls.clear()
                    lcd.drag_preview(0, 1)
                    wait_for(lambda: len(calls) == 1)
                    for _ in range(20):
                        lcd.drag_preview(0, 1)
                    assert len(calls) == 1
                    latest = lcd.encoding_settings()
                    release.set(); wait_for(idle)
                    assert len(calls) == 2 and calls[-1][1] == latest
                    assert lcd.preview_request == (lcd.src, latest)  # 5
                    assert core.ENC == before
                    saved = gui.load_ui_settings()
                    assert (saved['focus_x'], saved['focus_y']) == (lcd.s['focus_x'], lcd.s['focus_y'])  # 6
                # Настоящие ffmpeg: превью и первый кадр подготовленного файла совпадают.
                for source, angle, scale in [(gif, 0, 'fill'), (mp4, 90, 'fit')]:
                    lcd.select_file(source); lcd.position_buttons[angle].click()
                    lcd.cmb_scale.setCurrentIndex(lcd.cmb_scale.findData(scale)); wait_for(idle)
                    lcd.drag_preview(20, 15); wait_for(idle)
                    expected = lcd.encoding_settings()
                    prepared = []
                    original = core.load_media

                    def load(path):
                        assert core.ENC == expected
                        data = original(path); prepared.append(data); return data

                    with patch.object(core, 'load_media', side_effect=load):
                        lcd.send(); wait_for(idle)
                    assert gui.last_sent_path().read_bytes() == lcd.preview_bytes
                    sent = tmp / 'sent.mp4'; sent.write_bytes(prepared[0])
                    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(sent), '-frames:v', '1',
                                                   '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
                    actual = QImage(raw, 240, 320, 240 * 3, QImage.Format.Format_RGB888).copy()
                    actual = actual.transformed(QTransform().rotate(angle))
                    shown = QImage.fromData(lcd.preview_bytes)
                    assert actual.size() == shown.size()
                    for px in range(25, shown.width(), 50):
                        for py in range(25, shown.height(), 50):
                            a, b = actual.pixelColor(px, py), shown.pixelColor(px, py)
                            assert max(abs(a.red()-b.red()), abs(a.green()-b.green()), abs(a.blue()-b.blue())) < 20, (source.name, angle, scale, px, py, a.getRgb(), b.getRgb(), expected)
                    assert gui.load_ui_settings()['last_file_path'] == str(source.resolve())
                    assert not window.dashboard.last_sent.isHidden()  # 7
                previous = gui.last_sent_path().read_bytes()
                fail_send = True
                lcd.send(); wait_for(idle)
                assert gui.last_sent_path().read_bytes() == previous  # 8
                fail_send = False
                gui.last_sent_path().unlink()
                fallback_threads = []
                original_thumb = gui.thumbs.ensure_thumb

                def thumbnail(*args):
                    fallback_threads.append(threading.get_ident())
                    return original_thumb(*args)

                with patch.object(gui.thumbs, 'ensure_thumb', side_effect=thumbnail):
                    window.dashboard.refresh()
                    wait_for(lambda: window.dashboard.last_thumb_worker is None)
                    assert not window.dashboard.last_sent.isHidden()
                    assert fallback_threads and all(t != main_thread for t in fallback_threads)
                lcd.s['last_file_path'] = str(tmp / 'missing.gif'); window.dashboard.refresh()
                assert window.dashboard.last_sent.isHidden() and window.dashboard.last_file.text()  # 9
                release.clear()
                with patch.object(core, 'preview_png', side_effect=preview):
                    lcd.update_preview(); wait_for(lambda: lcd.preview_worker.isRunning())
                    release.set(); window.close(); app.processEvents()
                    assert not lcd.preview_worker or not lcd.preview_worker.isRunning()
                assert not errors and not core.STATE.exists(), [(kind.__name__, str(error)) for kind, error, tb in errors]  # 10
            finally:
                release.set(); window.close(); app.processEvents()
        print('T22 ok: 10 проверок; drag, сброс, очередь, ENC, кадры GIF/MP4, последняя отправка; USB не открыт')


if __name__ == '__main__':
    main()
