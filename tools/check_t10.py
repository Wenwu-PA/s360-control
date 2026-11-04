#!/usr/bin/env python3
"""T10: десять проверок, Qt offscreen; оборудование и настоящие сервисы запрещены."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='s360-t10-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
        os.environ['XDG_STATE_HOME'] = str(Path(tmp) / 'state')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QMessageBox
        import s360_gui as gui
        import s360_lcd as core
        state, calls, errors = 'inactive', [], []

        def forbidden(*args, **kwargs):
            raise AssertionError('Обращение к устройству или реальному сервису')

        def service(command, *args):
            nonlocal state
            assert args[-1] == gui.UNIT
            if command == "show":
                return (0, "SubState=auto-restart\nResult=exit-code") if state == "auto-restart" else (0, "SubState=start\nResult=success")
            if command == 'is-active':
                return (0 if state == 'active' else 3), ('activating' if state == 'auto-restart' else state)
            if command == 'is-enabled':
                return 1, 'disabled'
            assert command in ('restart', 'stop')
            calls.append(command)
            state = 'active' if command == 'restart' else 'inactive'
            return 0, ''

        app = QApplication([])
        app.setStyleSheet(gui.STYLE)
        with (patch.object(gui, 'systemctl', side_effect=service),
              patch.object(gui, 'unit_installed', return_value=True),
              patch.object(gui.CoolingPage, 'journal_line', return_value='Последняя ошибка из журнала'),
              patch.object(core, 'usb_present', return_value=True),
              patch.object(core, 'find_hidraw', side_effect=forbidden),
              patch.object(core, '_write_reports', side_effect=forbidden),
              patch.object(core, 'do_send', side_effect=forbidden),
              patch.object(gui.subprocess, 'run', side_effect=forbidden),
              patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
              patch.object(QMessageBox, 'warning') as warning,
              patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.stack.setCurrentIndex(2)
            window.show()
            app.processEvents()
            page = window.pages[2][1]
            assert window.isVisible() and not errors  # 1: запуск настоящего GUI
            assert tuple(page.cards) == ('radiator1', 'waterblock', 'pump')
            assert all(not c.placeholder.isHidden() and c.graph.isHidden() for c in page.cards.values())  # 2
            page.buttons['custom'].click()
            page.apply()
            assert not calls and 'Сохранить' in warning.call_args.args[2]  # 3: нет файла — нет запуска
            page.save_curves()
            # Служебные каналы специально отличаются от образца.
            curves = core.load_curves()
            curves['radiator2']['duty'][0] = 27
            curves['radiator3']['temp'][0] = 34
            page.write_curves(curves)
            page.set_curves(curves)
            graph = page.cards['radiator1'].graph
            start = graph.point_position(0)
            target = start + QPointF(-graph.plot_rect().width() / 100, 0)
            for kind, pos, button, buttons in (
                (QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                (QMouseEvent.Type.MouseMove, target, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                (QMouseEvent.Type.MouseButtonRelease, target, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
                app.sendEvent(graph, QMouseEvent(kind, pos, graph.mapToGlobal(pos.toPoint()), button, buttons,
                                                Qt.KeyboardModifier.NoModifier))
            assert graph.values['temp'][0] == 47 and graph.dragging is None  # 4: реальное перетаскивание
            assert page.dirty.text() == 'Изменения не сохранены'
            page.save_curves()
            saved = core.load_curves()
            assert all(saved[ch] == curves[ch] for ch in ('radiator2', 'radiator3')) and not calls  # 5
            page.cards['pump'].move_point(0, 59, 59)
            page.apply()
            assert not calls and '60%' in warning.call_args.args[2]
            assert page.cards['pump'].graph.values['duty'][0] == 59  # 6: ошибка не меняет введённое
            page.reset_curves()
            assert all(page.raw_curves()[ch] == curves[ch] for ch in ('radiator2', 'radiator3'))
            page.apply()
            assert calls == ['restart'] and core.load_curves() == page.editor_curves()
            assert core.COOLING_ENV.read_text() == 'MODE=custom\n' and 'работает' in page.status.text()  # 7
            for state in ('activating', 'auto-restart', 'failed', 'inactive', 'unknown'):
                page.refresh()
                expected = {'activating': 'Запускается', 'failed': 'Последняя ошибка из журнала',
                            'auto-restart': 'Последняя ошибка из журнала', 'inactive': 'остановлена', 'unknown': 'неизвестен'}[state]
                assert expected in page.status.text()  # 8: честные статусы
            core.CURVES_FILE.write_text('{broken')
            before = len(calls)
            page.apply()
            assert len(calls) == before and core.CURVES_FILE.read_text() == '{broken'  # 9
            page.buttons['balance'].click()
            assert all(c.graph.isHidden() and not c.placeholder.isHidden() for c in page.cards.values())
            page.apply()
            assert core.COOLING_ENV.read_text() == 'MODE=balance\n' and len(calls) == before + 1
            window.close()
            app.processEvents()
            assert not errors and not window.isVisible()  # 10: возврат в preset и закрытие
        print('T10 ok: 10 проверок; Qt offscreen, три графика, drag, служебные каналы, валидация, сервис; отправок нет')


if __name__ == '__main__':
    main()
