"""PC-only MCP tools. This module never imports or opens physical hardware."""

from __future__ import annotations

import asyncio
import math


class SimulatedRobot:
    """Track simulated movement without claiming real motion or encoder data."""

    def __init__(self) -> None:
        self.moving = False
        self._generation = 0

    async def move(self, direction: str, speed: int = 150,
                   duration: float = 0.5) -> dict[str, object]:
        """Simulate a bounded move; duration must be 0.05 to 1 second."""
        direction = direction.strip().lower()
        if direction not in {"forward", "backward", "left", "right"}:
            raise ValueError("unsupported direction")
        if isinstance(speed, bool) or not isinstance(speed, int) or not 0 <= speed <= 255:
            raise ValueError("speed must be an integer from 0 to 255")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or not 0.05 <= duration <= 1):
            raise ValueError("duration must be 0.05 to 1 second; continuous motion is disabled")
        if self.moving:
            raise ValueError("a simulated movement is already active")
        self._generation += 1
        generation = self._generation
        self.moving = True
        try:
            await asyncio.sleep(duration)
            return {"simulated": True, "direction": direction, "speed": speed,
                    "duration": duration,
                    "status": "completed" if generation == self._generation else "interrupted",
                    "physical_motion": False}
        finally:
            if generation == self._generation:
                self.moving = False

    def stop(self) -> dict[str, object]:
        """Stop simulated movement. This is not a physical emergency stop."""
        self._generation += 1
        self.moving = False
        return {"simulated": True, "status": "stopped", "physical_motion": False}

    def get_encoders(self) -> dict[str, object]:
        """Report that no real encoder measurements exist in this PC mode."""
        return {"simulated": True, "available": False,
                "reason": "No physical encoders are connected; no distance was measured."}

    def reset_encoders(self) -> dict[str, object]:
        """Acknowledge a simulated reset; no real encoder counters are modified."""
        return {"simulated": True, "status": "no encoder counters to reset"}

    def capture_image(self) -> dict[str, object]:
        """Report camera unavailable. Webcam support is not part of this test."""
        return {"simulated": True, "available": False,
                "reason": "PC camera capture is not implemented. No image was captured."}


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    robot = SimulatedRobot()
    server = FastMCP("saras-pc-simulation")
    # Keep stdout exclusively for MCP messages.
    for tool in (robot.move, robot.stop, robot.get_encoders,
                 robot.reset_encoders, robot.capture_image):
        server.tool()(tool)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
