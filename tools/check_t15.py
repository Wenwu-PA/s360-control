#!/usr/bin/env python3
"""T15: десять проверок охлаждения, Qt offscreen; USB и сервисы запрещены."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def check_visible_text(app, window, page, background):
    """Контраст реальных глифов по grab(); пустой редактор обязан провалить проверку."""
    from PySide6.QtGui import QColor

    def luminance(color):
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))

    bg = luminance(QColor(background))

    def capture(spin):
        spin.clearFocus()
        spin.lineEdit().deselect()
        app.processEvents()
        image = spin.grab().toImage()
        # Только внутренность редактора: без рамки, стрелок, фокуса и курсора.
        rect = spin.lineEdit().geometry().intersected(spin.rect()).adjusted(2, 2, -2, -2)
        return image, rect

    def rendered(spin, empty, snapshot=None):
        image, rect = capture(spin) if snapshot is None else snapshot
        ratio = image.devicePixelRatio()
        light = 0
        for y in range(round(rect.top() * ratio), round((rect.bottom() + 1) * ratio)):
            for x in range(round(rect.left() * ratio), round((rect.right() + 1) * ratio)):
                value = luminance(image.pixelColor(x, y))
                light += ((max(value, bg) + .05) / (min(value, bg) + .05) >= 4.5
                          and image.pixelColor(x, y) != empty.pixelColor(x, y))
        assert light >= 12, (spin.accessibleName(), spin.size(), rect, light)

    for width in (1200, 960):
        window.resize(width, 800)
        for channel, card in page.cards.items():
            page.select_component(channel)
            app.processEvents()
            app.processEvents()
            assert card.chips_widget.isVisible()
            for index, inputs in enumerate(card.chips):
                for key, spin in inputs.items():
                    assert spin.value() == card.graph.values[key][index]
                    value = spin.value()
                    original = capture(spin)
                    spin.clear()  # Суффикс остаётся: он тоже не должен выдать пустое поле за число.
                    empty, _ = capture(spin)
                    rendered(spin, empty, original)
                    try:
                        rendered(spin, empty)
                    except AssertionError:
                        pass
                    else:
                        raise AssertionError('Пустое поле прошло проверку отрисовки')
                    finally:
                        spin.setValue(value)
                    rendered(spin, empty)


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='s360-t15-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
        os.environ['XDG_STATE_HOME'] = str(Path(tmp) / 'state')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QKeyEvent, QMouseEvent
        from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QScrollArea
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError('USB или настоящий сервис')

        app = QApplication([])
        app.setFont(gui.theme.application_font())
        app.setStyleSheet(gui.STYLE)
        errors = []
        with (patch.object(gui, 'device_access', return_value='ready'),
              patch.object(gui, 'unit_installed', return_value=False),
              patch.object(gui, 'systemctl', side_effect=forbidden),
              patch.object(gui.subprocess, 'run', side_effect=forbidden),
              patch.object(core, 'usb_present', return_value=False),
              patch.object(core, 'find_hidraw', side_effect=forbidden),
              patch.object(core, 'do_send', side_effect=forbidden),
              patch.object(core, '_write_reports', side_effect=forbidden),
              patch.object(QMessageBox, 'warning') as warning,
              patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args))):
            window = gui.MainWindow()
            window.resize(1200, 800)
            window.show()
            window.stack.setCurrentIndex(2)
            app.processEvents()
            page = window.pages[2][1]
            assert tuple(page.cards) == ('radiator1', 'waterblock', 'pump')
            for mode in ('silent', 'balance', 'game'):
                page.buttons[mode].click()
                assert not page.preset_box.isHidden() and page.curves_box.isHidden()
                assert page.preset_title.text() == gui.COOLING_TITLES[mode]
                assert sum(label.text().count('—') for label in page.preset_box.findChildren(QLabel)) == 3
            assert not core.CURVES_FILE.exists()  # 1: готовые кривые не выдуманы
            page.buttons['custom'].click()
            app.processEvents()
            assert not page.curves_box.isHidden() and page.preset_box.isHidden()
            assert page.component_tabs.isHidden()
            assert all(card.isVisible() for card in page.cards.values())
            assert not page.cards['radiator1'].chips_widget.isHidden()
            assert all(page.cards[ch].chips_widget.isHidden() for ch in ('waterblock', 'pump'))
            assert page.cards_layout.getItemPosition(page.cards_layout.indexOf(page.cards['radiator1'])) == (0, 0, 2, 1)  # 2
            compact = page.cards['pump'].graph
            pos = compact.plot_rect().center()
            app.sendEvent(compact, QMouseEvent(QMouseEvent.Type.MouseButtonPress, pos,
                          compact.mapToGlobal(pos.toPoint()), Qt.MouseButton.LeftButton,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            assert page.active_channel == 'pump' and not page.cards['pump'].chips_widget.isHidden()
            assert page.cards['radiator1'].graph.compact  # 3: выбор компактного графика
            pump = page.cards['pump']
            graph = pump.graph
            start = graph.point_position(0)
            target = QPointF(start.x(), graph.plot_rect().bottom() + 100)
            for kind, pos, button, buttons in (
                (QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                (QMouseEvent.Type.MouseMove, target, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                (QMouseEvent.Type.MouseButtonRelease, target, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
                app.sendEvent(graph, QMouseEvent(kind, pos, graph.mapToGlobal(pos.toPoint()), button,
                              buttons, Qt.KeyboardModifier.NoModifier))
            assert graph.values['duty'][0] == 60 and graph.dragging is None and not warning.called  # 4
            pump.chips[0]['duty'].setValue(1)
            assert pump.chips[0]['duty'].minimum() == pump.chips[0]['duty'].value() == 60
            assert graph.values['duty'][0] == 60 and not warning.called
            pump.chips[0]['duty'].setValue(72)
            pump.chips[0]['duty'].lineEdit().setText('1%')
            pump.chips[0]['duty'].interpretText()
            assert graph.values['duty'][0] == 60
            assert all('Минимум' not in label.text() and 'нельзя' not in label.text()
                       for label in pump.findChildren(QLabel))  # 5: без сообщения
            page.select_component('radiator1')
            card = page.cards['radiator1']
            card.chips[1]['temp'].setValue(0)
            card.chips[1]['duty'].setValue(0)
            assert card.graph.values['temp'][1] == card.graph.values['temp'][0] + 1
            assert card.graph.values['duty'][1] == card.graph.values['duty'][0]
            card.graph.active_point = 0
            old = card.graph.values['duty'][0]
            app.sendEvent(card.graph, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
            assert card.graph.values['duty'][0] == old - 1
            core.validate_curves(page.raw_curves())  # 6: порядок точек и шаг клавиатуры
            curves = page.raw_curves()
            curves['radiator2']['duty'][0] = 27
            curves['radiator3']['temp'][0] = 34
            page.set_curves(curves)
            page.reset_curves()
            page.save_curves()
            saved = core.load_curves()
            assert all(saved[ch] == curves[ch] for ch in ('radiator2', 'radiator3'))  # 7
            for width, narrow in ((1099, True), (1100, False), (960, True), (1200, False), (960, True)):
                window.resize(width, 640)
                app.processEvents()
                assert page.component_tabs.isVisible() == narrow
                assert sum(card.isVisible() for card in page.cards.values()) == (1 if narrow else 3)
            page.component_tabs.setCurrentIndex(1)
            app.processEvents()
            assert page.active_channel == 'waterblock' and page.cards['waterblock'].isVisible()
            assert not page.cards['waterblock'].chips_widget.isHidden()  # 8: вкладки и смена ширины
            scroll = page.findChild(QScrollArea)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            assert window.footer.isAncestorOf(page.btn_apply) and window.footer.isAncestorOf(page.btn_save_curves)
            assert not scroll.isAncestorOf(window.footer)
            assert page.btn_apply.isVisible() and page.btn_apply.mapTo(window, page.btn_apply.rect().bottomRight()).y() < window.height()
            assert scroll.horizontalScrollBar().maximum() == 0  # 9: закреплённое действие, без горизонтальной прокрутки
            assert not any(label.text() == 'Охлаждение' for label in page.findChildren(QLabel))
            assert not hasattr(page, 'btn_details') and not hasattr(page, 'details') and not hasattr(page, 'chk_auto')
            assert page.btn_reset_curves.text() == 'Сбросить до стандартных'
            for name in ('dark', 'light', 'oled'):
                window.pages[3][1].theme_buttons[name].click()
                check_visible_text(app, window, page, gui.theme.COLORS['control'])
            assert not errors and not core.STATE.exists()
            window.close()
            app.processEvents()  # 10: служебные действия доступны, лишние элементы убраны
        print('T15 ok: 10 проверок; preset, выбор редактора, мышь/числа/клавиатура, каналы, узкое окно; отправок нет')


if __name__ == '__main__':
    main()
