import rumps
import pyautogui
import threading
import time
import random
from pynput import keyboard

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

HOLD_LEFT = 11         # 왼쪽 방향키 유지 시간
HOLD_RIGHT = 10        # 오른쪽 방향키 유지 시간
REST_MIN = 0.2         # 쉬는 시간 최소
REST_MAX = 0.5         # 쉬는 시간 최대


def hold_with_modifier(direction, seconds, app, stop_event):
    pyautogui.keyDown("shift")
    pyautogui.keyDown("space")
    pyautogui.keyDown(direction)
    start = time.time()
    while not stop_event.is_set() and (time.time() - start < seconds):
        remaining = int(seconds - (time.time() - start)) + 1
        app.title = f"{'←' if direction == 'left' else '→'} {remaining}초"
        stop_event.wait(0.5)
    pyautogui.keyUp(direction)
    pyautogui.keyUp("space")
    pyautogui.keyUp("shift")


class CdShiftMacroApp(rumps.App):
    def __init__(self):
        super().__init__("🐶", quit_button=None)
        self.running = False
        self.thread = None
        self.start_left = True  # True: 좌 먼저 / False: 우 먼저

        self.toggle_item = rumps.MenuItem("▶ 시작", callback=self.toggle)
        self.status_item = rumps.MenuItem("대기 중...", callback=None)
        self.status_item.set_callback(None)

        self.pattern_a = rumps.MenuItem("패턴 A: 좌 → 우 → 좌...  [1]", callback=self.set_pattern_a)
        self.pattern_b = rumps.MenuItem("패턴 B: 우 → 좌 → 우...  [2]", callback=self.set_pattern_b)
        self.pattern_a.state = True

        quit_item = rumps.MenuItem("종료", callback=self.quit_app)

        self.menu = [
            self.toggle_item,
            self.status_item,
            None,
            self.pattern_a,
            self.pattern_b,
            None,
            rumps.MenuItem("단축키: 1=패턴A  2=패턴B  3=정지", callback=None),
            None,
            quit_item,
        ]

        self._stop_event = threading.Event()

        self._hotkey_listener = keyboard.Listener(on_press=self._on_hotkey)
        self._hotkey_listener.daemon = True
        self._hotkey_listener.start()

    def _on_hotkey(self, key):
        try:
            ch = key.char
        except AttributeError:
            return
        if ch == '1':
            self.set_pattern_a(None)
            self._switch_pattern()
        elif ch == '2':
            self.set_pattern_b(None)
            self._switch_pattern()
        elif ch == '3':
            if self.running:
                self.stop()

    def _switch_pattern(self):
        if self.running:
            self.stop()
            if self.thread:
                self.thread.join(timeout=1.0)
        self.start()

    def set_pattern_a(self, _):
        self.start_left = True
        self.pattern_a.state = True
        self.pattern_b.state = False

    def set_pattern_b(self, _):
        self.start_left = False
        self.pattern_a.state = False
        self.pattern_b.state = True

    def toggle(self, _):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        self._stop_event.clear()
        self.running = True
        self.toggle_item.title = "⏹ 정지"
        self.thread = threading.Thread(target=self.pattern_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._stop_event.set()
        pyautogui.keyUp("shift")
        pyautogui.keyUp("space")
        self.title = "🐶"
        self.toggle_item.title = "▶ 시작"
        self.status_item.title = "대기 중..."

    def pattern_loop(self):
        icons = {"left": "←", "right": "→"}
        cycle = 1

        while self.running:
            directions = ["left", "right"] if self.start_left else ["right", "left"]
            for direction in directions:
                if self._stop_event.is_set():
                    break

                icon = icons[direction]
                self.status_item.title = f"Shift+Space+{icon} 홀드 중 ({cycle}사이클)"
                hold_with_modifier(direction, HOLD_LEFT if direction == "left" else HOLD_RIGHT, self, self._stop_event)

                if self._stop_event.is_set():
                    break

                rest = random.uniform(REST_MIN, REST_MAX)
                self.title = "💤"
                self.status_item.title = f"쉬는 중 ({rest:.1f}초)..."
                self._stop_event.wait(rest)

            cycle += 1

    def quit_app(self, _):
        self.running = False
        self._stop_event.set()
        pyautogui.keyUp("shift")
        pyautogui.keyUp("space")
        rumps.quit_application()


if __name__ == "__main__":
    CdShiftMacroApp().run()
