"""
LiAo — Motor controller
Communicates with a microcontroller (e.g. ESP32/Arduino) over USB serial.

Protocol:
    M <left> <right>         — set motor speeds (-255..255, positive = forward)
    S                        — stop
    RESET                    — zero encoders
    ENC:<m1>:<m2>:<m3>:<m4>  — encoder feedback streamed back from the board
"""
import serial
import threading
import time


class Robot:
    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200):
        self.port = port
        self.baud = baud
        self.enc1 = self.enc2 = self.enc3 = self.enc4 = 0
        self._running = True

        self._serial = serial.Serial(port, baud, timeout=1)
        time.sleep(2)  # wait for the board to boot

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ── Serial reader ──────────────────────────────────────────────────────

    def _read_loop(self):
        while self._running:
            try:
                line = self._serial.readline().decode("utf-8").strip()
                if line.startswith("ENC:"):
                    parts = line.split(":")[1:]
                    if len(parts) == 4:
                        self.enc1, self.enc2, self.enc3, self.enc4 = map(int, parts)
            except Exception:
                pass

    def _send(self, cmd: str):
        self._serial.write(f"{cmd}\n".encode())

    # ── Movement ───────────────────────────────────────────────────────────

    def move(self, left_speed: int, right_speed: int):
        """Raw speed control. Positive = forward."""
        self._send(f"M {left_speed} {right_speed}")

    def forward(self, speed: int = 150):
        self.move(speed, speed)

    def backward(self, speed: int = 150):
        self.move(-speed, -speed)

    def left(self, speed: int = 180):
        self.move(-speed, speed)

    def right(self, speed: int = 180):
        self.move(speed, -speed)

    def stop(self):
        self._send("S")

    # ── Encoders ───────────────────────────────────────────────────────────

    def get_encoders(self) -> tuple[int, int, int, int]:
        return (self.enc1, self.enc2, self.enc3, self.enc4)

    def reset_encoders(self):
        self._send("RESET")
        self.enc1 = self.enc2 = self.enc3 = self.enc4 = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def disconnect(self):
        self._running = False
        self.stop()
        time.sleep(0.3)
        self._serial.close()
