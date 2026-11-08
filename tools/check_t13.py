#!/usr/bin/env python3
"""T13: десять проверок состояний, только временные файлы и Qt offscreen."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="s360-t13-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
        import s360_gui as gui
        # Опрос мониторинга изолирован: hidraw и nvidia-smi не вызываются.
        patch.object(gui.monitor, "read_cooler_status", return_value={"rpm": {"radiator": 444, "pump": 2255}}).start()
        patch.object(gui.monitor, "read_gpu_temps", return_value={}).start()
        import s360_lcd as core

        def forbidden(*args, **kwargs):
            raise AssertionError("USB, открытие устройства или реальный сервис")

        app = QApplication([])
        app.setStyleSheet(gui.STYLE)
        errors = []
        with (patch.object(core, "do_send", side_effect=forbidden),
              patch.object(core, "_write_reports", side_effect=forbidden),
              patch.object(gui, "unit_installed", return_value=False),
              patch.object(gui.subprocess, "run", side_effect=forbidden),
              patch.object(core, "read_cpu_temp", return_value=48),
              patch.object(core, "usb_present", return_value=False) as present,
              patch.object(core, "find_hidraw", return_value=Path("/dev/hidraw-test")) as hid,
              patch.object(gui.os, "access", return_value=True) as access,
              patch.object(QFileDialog, "getOpenFileName", return_value=("", "")),
              patch.object(QMessageBox, "warning") as warning,
              patch.object(sys, "excepthook", side_effect=lambda *a: errors.append(a))):
            assert gui.device_access(core.COOLER_VID, core.COOLER_PID) == "missing"
            hid.assert_not_called(); access.assert_not_called()  # 1
            present.return_value = True
            assert gui.device_access(core.COOLER_VID, core.COOLER_PID) == "ready"
            access.assert_called_with(Path("/dev/hidraw-test"), os.R_OK | os.W_OK)  # 2
            access.return_value = False
            assert gui.device_access(core.COOLER_VID, core.COOLER_PID) == "denied"  # 3
            root = Path(tmp) / "sysfs"
            node = root / "1-2"; node.mkdir(parents=True)
            for key, value in {"idVendor": f"{core.VID:x}", "idProduct": f"{core.PID:x}", "busnum": "1", "devnum": "23"}.items():
                (node / key).write_text(value)
            access.return_value = True
            assert gui.device_access(core.VID, core.PID, lcd=True, root=root) == "ready"
            access.assert_called_with(Path("/dev/bus/usb/001/023"), os.R_OK | os.W_OK)  # 4
            with patch.object(gui, "device_access", return_value="missing") as state:
                window = gui.MainWindow(); window.show(); app.processEvents()
                assert window.overview_state == "missing" and window.page_title.text() == "Подключение устройства"
                before = state.call_count; window.btn_check.click()
                assert state.call_count > before and not gui.load_ui_settings().get("first_run_done")  # 5
                state.return_value = "denied"; window.tick()
                assert window.overview_state == "denied" and "./install.sh" in window.state_detail.text()  # 6
                state.return_value = "ready"; window.tick()
                assert window.overview_state == "overview" and window.overview_states.currentWidget() is window.dashboard
                assert not hasattr(window, "btn_first_run") and window.page_title.text() == "Мониторинг"  # 7
                window.stack.setCurrentIndex(1)
                window.stack.setCurrentIndex(0)
                assert window.overview_state == "overview" and window.dashboard.cpu.text() == "48 °C"  # 8
                gui.save_ui_settings(window.settings)
                restored = gui.MainWindow()
                assert restored.overview_state == "overview" and restored.dashboard.last_file.text() == "Изображение ещё не отправлялось"  # 9
                with patch.object(core, "read_cpu_temp", side_effect=core.S360Error("нет датчика")):
                    window.tick()
                assert window.dashboard.cpu.text() == "Датчик недоступен" and not core.STATE.exists() and not errors
                restored.close(); window.close(); app.processEvents()  # 10
        print("T13 ok: 10 проверок; права без открытия, состояния, повторная проверка, мониторинг без первого запуска, сохранение; отправок нет")


if __name__ == "__main__":
    main()
