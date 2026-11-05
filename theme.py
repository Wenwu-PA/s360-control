"""Токены дизайна v2 и общая тема Qt. Без зависимостей от ядра приложения."""

COLORS = {
    "background": "#141619", "surface": "#1C1F24", "control": "#292D34",
    "hover": "#353A43", "border": "#484F59", "text": "#F2F3F5",
    "secondary": "#B7BEC8", "disabled": "#959DA8", "accent": "#B72F3D",
    "accentHover": "#C43948", "accentPressed": "#982735", "line": "#E66A75",
    "good": "#95C7AC", "warning": "#DEC18C", "error": "#F28E96",
}
FONT_FAMILIES = ("Inter", "Noto Sans")
FONT_SIZES = {"heading": 24, "title": 16, "body": 14, "caption": 12}
LINE_HEIGHTS = {"heading": 32, "title": 24, "body": 20, "caption": 16}
SPACING = (8, 16, 24, 32)
RADII = (4, 8)
BORDER_WIDTH = 1


def application_font():
    """Qt выбирает установленный Inter, затем Noto Sans, затем системный шрифт."""
    from PySide6.QtGui import QFont, QFontDatabase
    installed = set(QFontDatabase.families())
    font = QFont()
    font.setFamilies([name for name in FONT_FAMILIES if name in installed] + [font.family()])
    font.setPixelSize(FONT_SIZES["body"])
    return font


def build_qss():
    c = COLORS
    small, medium = RADII
    gap, space, _, _ = SPACING
    return f"""
* {{ font-size: {FONT_SIZES['body']}px; }}
QWidget {{ background: {c['background']}; color: {c['text']}; }}
QLabel {{ background: transparent; }}
QLabel#product {{ font-size: {FONT_SIZES['body']}px; font-weight: 600; }}
QLabel#temperature {{ font-size: {LINE_HEIGHTS['heading']}px; font-weight: 600; }}
QLabel#stateHeading {{ font-size: {FONT_SIZES['title']}px; font-weight: 600; }}
QLabel#pageTitle {{ font-size: {FONT_SIZES['heading']}px; font-weight: 600; }}
QTabWidget::pane, QScrollArea {{ border: none; }}
QTabBar {{ border-bottom: {BORDER_WIDTH}px solid {c['border']}; }}
QTabBar::tab {{ color: {c['secondary']}; padding: {gap}px {space}px; border-bottom: 1px solid transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom-color: {c['accent']}; }}
QTabBar::tab:hover {{ background: {c['hover']}; }}
QWidget#footer {{ border-top: {BORDER_WIDTH}px solid {c['border']}; }}
QLabel#statusBadge {{ background: {c['surface']}; color: {c['secondary']}; border-radius: {small}px; padding: {gap}px; }}
QLabel[status="good"] {{ color: {c['good']}; }}
QLabel[status="warning"] {{ color: {c['warning']}; }}
QLabel[status="error"] {{ color: {c['error']}; }}
QLabel#statusText, QLabel#muted {{ color: {c['secondary']}; }}
QLabel#statusText, QLabel#statusBadge {{ font-size: {FONT_SIZES['caption']}px; }}
QGroupBox {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: {medium}px; margin-top: {space}px; padding: {space}px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {space}px; color: {c['secondary']}; }}
QPushButton, QToolButton {{ background: {c['control']}; border: 1px solid {c['border']}; border-radius: {small}px; padding: {gap}px {space}px; }}
QPushButton:hover, QToolButton:hover {{ background: {c['hover']}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {c['background']}; }}
QPushButton:checked {{ border-color: {c['accent']}; }}
QPushButton[coolingCard="true"] {{ text-align: left; padding: {space}px; }}
QPushButton#primary {{ background: {c['accent']}; border-color: {c['accent']}; }}
QPushButton#primary:hover {{ background: {c['accentHover']}; }}
QPushButton#primary:pressed {{ background: {c['accentPressed']}; }}
QPushButton:disabled, QToolButton:disabled, QPushButton#primary:disabled {{ background: {c['control']}; color: {c['disabled']}; border-color: {c['border']}; }}
QWidget:disabled {{ color: {c['disabled']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: {c['control']}; border: 1px solid {c['border']}; border-radius: {small}px; padding: {gap}px; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c['text']}; }}
QProgressBar {{ background: {c['control']}; border: none; border-radius: {small}px; text-align: center; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: {small}px; }}
QSlider::groove:horizontal {{ background: {c['border']}; height: 4px; }}
QSlider::sub-page:horizontal {{ background: {c['line']}; }}
QSlider::handle:horizontal {{ background: {c['text']}; width: 12px; margin: -4px 0; border-radius: 6px; }}
QSlider::handle:horizontal:disabled {{ background: {c['disabled']}; }}
QPushButton[lcdMode="true"] {{ text-align: left; border-radius: {medium}px; padding: 12px {space}px; }}
QPushButton[lcdLink="true"] {{ background: transparent; border: none; color: {c['secondary']}; padding: {gap}px 0; }}
QGroupBox[lcdCard="true"] {{ margin-top: 0; padding: {space}px; }}
QGroupBox[lcdCard="true"]::title {{ subcontrol-origin: padding; color: {c['text']}; font-size: {FONT_SIZES['title']}px; }}
QGroupBox#lcdTile {{ background: {c['control']}; margin-top: 0; padding: {gap}px; }}
QLineEdit#tileValue, QDoubleSpinBox#tileValue {{ font-size: {FONT_SIZES['heading']}px; font-weight: 600; background: transparent; border: none; padding: 0; }}
QLabel[lcdCaption="true"] {{ font-size: {FONT_SIZES['caption']}px; }}
QLabel#lcdTitle {{ font-size: {FONT_SIZES['title']}px; font-weight: 600; }}
QLabel#clockTime {{ font-size: {FONT_SIZES['heading']}px; font-weight: 600; }}
QLabel#clockStyle {{ background: {c['control']}; border: 1px solid {c['accent']}; border-radius: {medium}px; padding: 12px 18px; }}
QLabel#clockPreview {{ background: {c['background']}; font-size: 72px; font-weight: 600; border-radius: {medium}px; }}
QLabel#preview {{ background: {c['background']}; border: 1px solid {c['border']}; border-radius: {medium}px; }}
QLabel#warn {{ background: {c['surface']}; color: {c['warning']}; border: 1px solid {c['border']}; border-radius: {medium}px; padding: {gap}px; }}
QToolTip {{ background: {c['control']}; color: {c['text']}; border: 1px solid {c['border']}; }}
"""
