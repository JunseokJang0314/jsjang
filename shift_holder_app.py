import rumps
import pyautogui
import threading
import time
import random

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

HOLD_MIN_ODD = 25
HOLD_MAX_ODD = 30
HOLD_MIN_EVEN = 7
HOLD_MAX_EVEN = 12


def press_key(key, hold=None):
    if hold is None:
        hold = random.uniform(0.08, 0.22)
    pyautogui.keyDown(key)
    time.sleep(hold)
    pyautogui.keyUp(key)
    time.sleep(random.uniform(0.1, 0.3))


class ShiftHolderApp(rumps.App):
    def __init__(self):
        super().__init__("⇧", quit_button=None)
        self.running = False
        self.thread = None
        self.reverse = False  # False: 좌/우우좌, True: 우/좌좌우

        self.toggle_item = rumps.MenuItem("▶ 시작", callback=self.toggle)
        self.status_item = rumps.MenuItem("대기 중...", callback=None)
        self.status_item.set_callback(None)

        self.pattern_a = rumps.MenuItem("패턴 A: 홀수=좌 / 짝수=우", callback=self.set_pattern_a)
        self.pattern_b = rumps.MenuItem("패턴 B: 홀수=우 / 짝수=좌", callback=self.set_pattern_b)
        self.pattern_a.state = True

        quit_item = rumps.MenuItem("종료", callback=self.quit_app)

        self.menu = [
            self.toggle_item,
            self.status_item,
            None,
            self.pattern_a,
            self.pattern_b,
            None,
            quit_item,
        ]

    def set_pattern_a(self, _):
        self.reverse = False
        self.pattern_a.state = True
        self.pattern_b.state = False

    def set_pattern_b(self, _):
        self.reverse = True
        self.pattern_a.state = False
        self.pattern_b.state = True

    def toggle(self, _):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        self.running = True
        self.toggle_item.title = "⏹ 정지"
        self.thread = threading.Thread(target=self.pattern_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        pyautogui.keyUp("shift")
        self.title = "⇧"
        self.toggle_item.title = "▶ 시작"
        self.status_item.title = "대기 중..."

    def pattern_loop(self):
        round_num = 1
        while self.running:
            # 1. Shift 홀드 + 카운트다운
            if round_num % 2 == 1:
                hold_seconds = random.randint(HOLD_MIN_ODD, HOLD_MAX_ODD)
            else:
                hold_seconds = random.randint(HOLD_MIN_EVEN, HOLD_MAX_EVEN)
            start = time.time()
            while self.running:
                elapsed = time.time() - start
                remaining = hold_seconds - elapsed
                if remaining <= 0:
                    break
                self.title = f"{int(remaining)+1}초"
                self.status_item.title = f"Shift 홀드 중 ({round_num}회차)"
                pyautogui.keyDown("shift")
                time.sleep(random.uniform(0.4, 0.7))

            if not self.running:
                break

            # 2. Shift 놓기 + 랜덤 키
            pyautogui.keyUp("shift")
            time.sleep(random.uniform(0.3, 1.5))
            extra_key = "z"
            self.title = f"[{extra_key}]"
            self.status_item.title = f"{extra_key} 입력 중 ({round_num}회차)"
            press_key(extra_key)
            time.sleep(random.uniform(0.2, 0.5))

            # 3. 패턴에 따라 방향키 입력
            if not self.reverse:
                # 패턴 A: 홀수=좌 / 짝수=우
                if round_num % 2 == 1:
                    self.title = "←"
                    self.status_item.title = f"← 입력 중 ({round_num}회차)"
                    press_key("left")
                else:
                    self.title = "→"
                    self.status_item.title = f"→ 입력 중 ({round_num}회차)"
                    press_key("right")
            else:
                # 패턴 B: 홀수=우 / 짝수=좌
                if round_num % 2 == 1:
                    self.title = "→"
                    self.status_item.title = f"→ 입력 중 ({round_num}회차)"
                    press_key("right")
                else:
                    self.title = "←"
                    self.status_item.title = f"← 입력 중 ({round_num}회차)"
                    press_key("left")

            round_num += 1

    def quit_app(self, _):
        self.running = False
        pyautogui.keyUp("shift")
        rumps.quit_application()


if __name__ == "__main__":
    ShiftHolderApp().run()
