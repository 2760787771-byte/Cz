# ui_builder.py
# When using the PySide6 UI (新Ui.py), the old tkinter-based ui_builder is not used.
# Keep a minimal shim so imports from other modules don't fail if they import build_ui.

def build_ui(app):
    # No-op: UI is provided by 新Ui.py (LivelyWindow) when running in PySide6 mode.
    print("[ui_builder] build_ui shim called — using PySide6 NewUi instead.")
