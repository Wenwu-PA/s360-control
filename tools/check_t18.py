#!/usr/bin/env python3
"""T18: сохранение упрощённого LCD и управления сервисом после T19."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='s360-t18-') as tmp:
        os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
        os.environ['XDG_STATE_HOME'] = str(Path(tmp) / 'state')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError('Оборудование или реальный сервис')

        app = QApplication([])
        with (patch.object(gui, 'device_access', return_value='ready'),
              patch.object(core, 'usb_present', return_value=False),
              patch.object(core, 'read_cpu_temp', return_value=50),
              patch.object(core, 'find_hidraw', side_effect=forbidden),
              patch.object(core, 'do_send', side_effect=forbidden),
              patch.object(core, '_write_reports', side_effect=forbidden),
              patch.object(gui, 'unit_installed', return_value=False),
              patch.object(gui, 'systemctl', side_effect=forbidden)):
            settings = dict(gui.UI_DEFAULT_SETTINGS, rotate=270, screen_pos=90)
            gui.save_ui_settings(settings)
            window = gui.MainWindow()
            lcd, cooling = window.pages[1][1], window.pages[2][1]
            lcd.apply_enc()
            assert lcd.s['rotate'] == core.ENC['rotate'] == 0
            assert core.ENC['orient'] == 90 and '320 × 240' in lcd.lbl_orientation.text()
            assert not hasattr(lcd, 'cmb_rot') and not hasattr(cooling, 'chk_auto')
            assert not cooling.btn_service.isVisible() and not cooling.btn_apply.isEnabled()
            assert not core.STATE.exists() and not core.COOLING_ENV.exists()
            window.close(); app.processEvents()
        print('T18 ok: положение экрана, rotate=0, отсутствие старых контролов; отправок нет')


if __name__ == '__main__':
    main()
