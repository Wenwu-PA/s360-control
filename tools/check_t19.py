#!/usr/bin/env python3
"""T19: восемь проверок; Qt offscreen, временные настройки, без оборудования."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def contrast(first, second):
    def luminance(color):
        values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * w
                   for v, w in zip(values, (.2126, .7152, .0722)))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='s360-t19-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
        os.environ['XDG_STATE_HOME'] = str(Path(tmp) / 'state')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QGroupBox
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        import s360_lcd as core
        import theme

        def forbidden(*args, **kwargs):
            raise AssertionError('Оборудование или реальный сервис')

        app = QApplication([])
        errors = []
        with (patch.object(gui, 'device_access', return_value='ready'),
              patch.object(core, 'usb_present', return_value=True),
              patch.object(core, 'read_cpu_temp', return_value=52) as cpu,
              patch.object(core, 'find_hidraw', side_effect=forbidden),
              patch.object(core, 'do_send', side_effect=forbidden),
              patch.object(core, '_write_reports', side_effect=forbidden),
              patch.object(gui, 'unit_installed', return_value=False),
              patch.object(gui, 'systemctl', side_effect=forbidden),
              patch.object(gui.subprocess, 'run', side_effect=forbidden),
              patch.object(sys, 'excepthook', side_effect=lambda *args: errors.append(args))):
            settings = dict(gui.UI_DEFAULT_SETTINGS, first_run_done=True)
            gui.save_ui_settings(settings)
            window = gui.MainWindow(); window.show(); app.processEvents()
            assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['Мониторинг', 'LCD-дисплей', 'Охлаждение', 'Настройки']
            assert window.stack.currentIndex() == 0 and window.timer.interval() == 2000  # 1
            assert not window.menuBar().actions()
            assert not any(b.text() in ('Обзор', 'Дополнительно') for b in window.findChildren(QPushButton))
            assert not window.footer_stack.widget(0).findChildren(QPushButton)  # 2
            page = window.pages[3][1]
            assert [box.title() for box in page.findChildren(QGroupBox)] == ['Тема']
            assert len(page.findChildren(QPushButton)) == 3
            assert any(label.text() == f'Версия: {core.VERSION}' for label in page.findChildren(QLabel))  # 3
            qss = set()
            for name in ('light', 'oled', 'dark'):
                page.theme_buttons[name].click(); app.processEvents()
                qss.add(app.styleSheet())
                assert theme.COLORS == theme.THEMES[name]
                assert window.settings['theme'] == gui.load_ui_settings()['theme'] == json.loads(core.SETTINGS.read_text())['theme'] == name
                assert page.theme_buttons[name].isChecked()
            assert len(qss) == 3  # 4
            for colors in theme.THEMES.values():
                for foreground, background in (('text', 'surface'), ('secondary', 'surface')):
                    assert contrast(colors[foreground], colors[background]) >= 4.5
                assert contrast('#FFFFFF', colors['accent']) >= 4.5
                assert contrast(colors['inputBorder'], colors['surface']) >= 3
                assert contrast(colors['line'], colors['surface']) >= 3  # 5
            for bad in (dict(fps='20/3', max_seconds=None, background='white'),
                        dict(fps='120', max_seconds=60, background='#abcdef')):
                settings.update(bad, theme='oled')
                gui.save_ui_settings(settings)
                restored = gui.MainWindow()
                lcd = restored.pages[1][1]; lcd.apply_enc()
                assert (lcd.s['fps'], lcd.s['max_seconds'], lcd.s['background']) == ('60', 15, 'black')
                assert (core.ENC['fps'], core.ENC['max_seconds'], core.ENC['background']) == ('60', 15, 'black')
                assert theme.COLORS == theme.THEMES['oled']
                assert not any(hasattr(lcd, field) for field in ('cmb_bg', 'edt_fps', 'spn_sec', 'btn_bg'))
                restored.close()  # 6
            cpu.return_value = 61; window.tick()
            assert window.dashboard.cpu.text() == '61 °C'
            assert 'Подключён' in window.dashboard.lcd.text() and 'Подключён' in window.dashboard.cooler.text()
            assert 'остановлено' in window.dashboard.svc.text()
            core.COOLING_STATE.parent.mkdir(parents=True, exist_ok=True)
            core.COOLING_STATE.write_text(json.dumps({'mode': 'custom'}))
            with (patch.object(gui, 'unit_installed', return_value=True),
                  patch.object(gui, 'systemctl', return_value=(0, 'active'))):
                window.dashboard.refresh()
            assert 'Свой профиль · работает' in window.dashboard.svc.text()  # 7
            window.settings.update(last_file='/tmp/земля.gif', last_upload_time=1000)
            window.tick()
            assert 'земля.gif' in window.dashboard.last_file.text() and 'отправлено' in window.dashboard.last_upload.text()
            assert 'применено' not in window.dashboard.last_upload.text()
            assert not errors and not core.STATE.exists()
            window.close(); app.processEvents()  # 8
        print('T19 ok: 8 проверок; вкладки, настройки, темы, контраст, 60/15/black, мониторинг; отправок нет')


if __name__ == '__main__':
    main()
