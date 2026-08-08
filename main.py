# -*- coding: utf-8 -*-
"""
PySide6-based main entry. Replaces the original tkinter-based launcher.
Creates a QFollowApp that exposes the attributes/methods expected by other modules
(using a lightweight Var wrapper to emulate tkinter Variable.get()/set()).

This is an initial integration: it instantiates your NewUi (新Ui.py -> LivelyWindow)
and wires basic logging, AI chat send, preview updates, and engine start/stop.

Note: Many features from the original FollowApp are preserved as attributes so
other modules (capture, detector, tracking, etc.) can access them. Further
adaptation may be needed for full parity.
"""

import sys
import threading
import queue
import time
import os

# Ensure PySide6 is available
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap

# Import your provided Qt UI
from 新Ui import LivelyWindow

# Import modules that expect an 'app' object
from pythonosc import udp_client

# Minimal Var shim to emulate tkinter Variable.get()/set()
class Var:
    def __init__(self, value=None):
        self._v = value
    def get(self):
        return self._v
    def set(self, v):
        self._v = v
    def __repr__(self):
        return f"Var({self._v!r})"

class QFollowApp:
    def __init__(self, window: LivelyWindow):
        self.window = window
        self.log_queue = queue.Queue(maxsize=500)
        self.frame_queue = queue.Queue(maxsize=2)
        self.detect_result_queue = queue.Queue(maxsize=8)
        self.chat_send_queue = queue.Queue(maxsize=20)
        self.wd_queue = queue.Queue(maxsize=2)
        self.depth_queue = queue.Queue(maxsize=1)

        # Device detection
        try:
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            self.device = "cpu"

        # OSC client
        self.osc = udp_client.SimpleUDPClient("127.0.0.1", 9000)

        # State variables (Var wrappers for .get()/.set() compatibility)
        self.auto_switch_mouse_osc = Var(True)
        self.enable_yolo = Var(False)
        self.enable_move = Var(True)
        self.auto_adjust = Var(False)
        self.enable_yaw = Var(True)
        self.lock_view = Var(True)
        self.show_preview = Var(True)
        self.use_mouse_control = Var(True)
        self.invert_mouse = Var(False)
        self.pause_tracking = Var(False)
        self.enable_keyboard_sim = Var(False)
        self.overlay_preview = Var(False)
        self.enable_wd_tagger = Var(False)

        # Numeric parameters
        self.YOLO_IMGSZ = 320
        self.MIN_AREA_RATIO = 0.01
        self.SMOOTH = 0.65
        self.PREVIEW_SCALE = 0.4

        # Tracking state placeholders
        self.current_capture_width = 1920
        self.current_capture_height = 1080
        self.center_x = self.current_capture_width / 2
        self.center_y = self.current_capture_height / 2

        # Simplified replacements for many attributes used elsewhere
        self.frame_count = 0
        self.fps = 0
        self.last_fps_update = time.time()

        # AI controller placeholder
        self.ai_controller = None

        # For depth/wd modules
        self.wd_tags = []
        self.current_depth_map = None

        # Thread control
        self.running_yolo = False
        self.running = True

        # connect UI signals
        self._connect_ui()

        # Start UI timers
        self._start_timers()

        # Start background workers
        threading.Thread(target=self.chat_send_loop, daemon=True).start()

    # ----------------- UI Integration -----------------
    def _connect_ui(self):
        # Map UI widgets to expected names
        try:
            self.log_box = self.window.log_text
            self.preview_label = self.window.preview_label
            self.ai_chat_box = self.window.ai_chat
            self.ai_input_entry = self.window.ai_input
            # Buttons
            self.btn_run = self.window.btn_start
            self.btn_mark = self.window.btn_mark

            # Bind actions
            self.btn_run.clicked.connect(self._on_toggle_engine)
            self.window.broadcast_edit.returnPressed.connect(self._on_broadcast)
            self.window.broadcast_edit.returnPressed.connect(lambda: self.log("[广播] " + self.window.broadcast_edit.text()))

            # AI input send
            self.window.ai_input.returnPressed.connect(self._on_send_ai)
            # Memory/show actions can be connected later
        except Exception as e:
            print(f"[QFollowApp] UI mapping failed: {e}")

    def _start_timers(self):
        # UI update timer to flush log queue and preview
        self.ui_timer = QTimer()
        self.ui_timer.setInterval(50)
        self.ui_timer.timeout.connect(self._update_ui_loop)
        self.ui_timer.start()

        # Mouse/keyboard check timer (replacement for root.after loop)
        self.mouse_timer = QTimer()
        self.mouse_timer.setInterval(50)
        self.mouse_timer.timeout.connect(self.mouse_check_tick)
        self.mouse_timer.start()

    # ----------------- Logging & UI helpers -----------------
    def log(self, msg_raw):
        try:
            self.log_queue.put_nowait(msg_raw)
        except queue.Full:
            pass

    def _update_ui_loop(self):
        # Flush logs
        while not self.log_queue.empty():
            try:
                text = self.log_queue.get_nowait()
            except queue.Empty:
                break
            try:
                # Append to QTextEdit
                self.log_box.append(text)
            except Exception:
                print(text)

        # Preview update (if frames present)
        if not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get_nowait()
                # frame is expected as a numpy array (H,W,3) in RGB order
                import numpy as np
                if hasattr(self.preview_label, 'setPixmap'):
                    h, w = frame.shape[:2]
                    if frame.dtype != np.uint8:
                        frame = (frame * 255).astype('uint8')
                    img = QImage(frame.data, w, h, frame.strides[0], QImage.Format_RGB888)
                    pix = QPixmap.fromImage(img).scaled(self.preview_label.width(), self.preview_label.height(), Qt.KeepAspectRatio)
                    self.preview_label.setPixmap(pix)
            except Exception as e:
                # If preview fails, just ignore
                print(f"[Preview] update failed: {e}")

    # ----------------- Chat -----------------
    def _on_send_ai(self):
        text = self.ai_input_entry.text().strip()
        if not text:
            return
        # Append to chat UI
        self.ai_chat_box.append(f"You > {text}")
        self.ai_chat_box.append("#4100 > ")
        self.ai_input_entry.clear()
        # Send to AI controller if available
        if self.ai_controller:
            self.ai_controller.send_message(text)

    def _on_broadcast(self):
        txt = self.window.broadcast_edit.text().strip()
        if txt:
            ok, tip = (True, f"广播: {txt}")
            self.log(tip)

    # ----------------- Engine controls -----------------
    def _on_toggle_engine(self):
        # Toggle enable_yolo Var
        cur = self.enable_yolo.get()
        self.enable_yolo.set(not cur)
        if self.enable_yolo.get():
            self.start_yolo()
        else:
            self.stop_yolo()

    def start_yolo(self):
        if self.running_yolo:
            return
        # Minimal start: set flag and start capture/detect threads if available
        self.running_yolo = True
        self.log("✅ YOLO 启动 （简化模式）")
        # Start capture and detect loops if the modules are present
        try:
            from yolo_capture import capture_loop
            from yolo_detector import detect_loop
            self.capture_thread = threading.Thread(target=capture_loop, args=(self,), daemon=True)
            self.detect_thread = threading.Thread(target=detect_loop, args=(self,), daemon=True)
            self.capture_thread.start()
            self.detect_thread.start()
        except Exception as e:
            self.log(f"启动推理线程失败: {e}")
            self.running_yolo = False

    def stop_yolo(self):
        self.running_yolo = False
        self.log("❌ YOLO 已停止")

    # ----------------- Mouse / Keyboard -----------------
    def mouse_check_tick(self):
        # Simplified: do nothing here or could implement cursor detection
        pass

    # ----------------- Chat send loop -----------------
    def chat_send_loop(self):
        SAFE_CHAT_DELAY = 1.8
        while self.running:
            try:
                text = self.chat_send_queue.get(timeout=0.2)
                try:
                    # send via OSC
                    self.osc.send_message('/chatbox/input', [text, True, True])
                    self.log(f"[游戏提示] {text}")
                except Exception as e:
                    self.log(f"[聊天发送失败] {e}")
                time.sleep(SAFE_CHAT_DELAY)
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"[聊天线程异常] {e}")
                time.sleep(1)

    # ----------------- Shutdown -----------------
    def stop(self):
        self.running = False
        self.running_yolo = False


def main():
    app = QApplication(sys.argv)
    window = LivelyWindow()
    window.show()

    qapp = QFollowApp(window)

    # Try to initialize AI module if available
    try:
        from ai_chat_controller import AIChatController
        def ai_log(msg):
            qapp.log(f"[AI System] {msg}")
        def ai_ui_update(text):
            try:
                qapp.ai_chat_box.append(text)
            except Exception:
                pass
        qapp.ai_controller = AIChatController(log_callback=ai_log, ui_update_callback=ai_ui_update, db_dir="data", device=qapp.device)
        qapp.log("[AI] 模块初始化成功")
    except Exception as e:
        qapp.log(f"[AI] 模块未加载: {e}")

    sys.exit(app.exec())

if __name__ == '__main__':
    main()
