#!/usr/bin/env python3
"""pan.py - математика сдвига картинки перетаскиванием на предпросмотре (без Qt, без ffmpeg).
Пространство кадра: «кадр зрителя» vw x vh в пикселях (240x320 при положении 0°/180°, 320x240 при 90°/270°).
Режим «Заполнить»: картинка шире/выше кадра, окно обрезки двигается: focus_x/focus_y 0..1 (0 = левый/верхний край).
Режим «Вписать»: картинка меньше кадра по одной оси, она сама двигается по свободному месту: 0 = к левому/верхнему краю.
Перетаскивание «берёт картинку»: ведёшь вправо — картинка уходит вправо."""


def span(mode, sw, sh, vw, vh):
    """(по x, по y): для fill — на сколько масштабированная картинка выходит за кадр; для fit — свободное место в кадре; иначе 0."""
    if sw <= 0 or sh <= 0:
        return 0.0, 0.0
    if mode == "fill":
        s = max(vw / sw, vh / sh)
        return max(0.0, sw * s - vw), max(0.0, sh * s - vh)
    if mode == "fit":
        s = min(vw / sw, vh / sh)
        return max(0.0, vw - sw * s), max(0.0, vh - sh * s)
    return 0.0, 0.0


def can_pan(mode, sw, sh, vw, vh):
    sx, sy = span(mode, sw, sh, vw, vh)
    return sx >= 1.0, sy >= 1.0


def drag(mode, fx, fy, dx, dy, sw, sh, vw, vh):
    """Новые (focus_x, focus_y) после смещения мыши на dx, dy пикселей кадра (вправо/вниз положительные). Оси без запаса не меняются."""
    sx, sy = span(mode, sw, sh, vw, vh)
    sign = -1.0 if mode == "fill" else 1.0
    clamp = lambda v: min(1.0, max(0.0, v))
    nfx = clamp(fx + sign * dx / sx) if sx >= 1.0 else fx
    nfy = clamp(fy + sign * dy / sy) if sy >= 1.0 else fy
    return nfx, nfy


def selftest():
    assert span("fill", 400, 200, 240, 320) == (400.0, 0.0)          # альбомная -> окно двигается по x
    assert span("fit", 400, 200, 240, 320) == (0.0, 200.0)           # альбомная в вертикальном кадре -> свободно по y
    assert span("fill", 200, 400, 240, 320)[0] == 0.0 and span("fill", 200, 400, 240, 320)[1] > 0   # высокая -> по y
    assert span("stretch", 400, 200, 240, 320) == (0.0, 0.0) and span("fill", 0, 0, 1, 1) == (0.0, 0.0)
    assert drag("fill", .5, .5, 100, 0, 400, 200, 240, 320) == (0.25, 0.5)      # картинку повели вправо -> окно влево
    assert drag("fill", .5, .5, 100, 999, 400, 200, 240, 320) == (0.25, 0.5)    # по оси без запаса не двигается
    assert drag("fit", .5, .5, 0, 50, 400, 200, 240, 320) == (0.5, 0.75)        # картинку повели вниз -> ниже
    assert drag("fill", .1, .5, -9999, 0, 400, 200, 240, 320)[0] == 1.0         # зажим 0..1
    assert drag("fit", .5, .9, 0, 9999, 400, 200, 240, 320)[1] == 1.0
    print("pan selftest ok")


if __name__ == "__main__":
    selftest()
