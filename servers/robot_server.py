"""
Saras — Robot MCP Server
Exposes basic robot hardware as tools for Claude Code.

Tools:
  move(direction, speed, duration)   — drive motors
  stop()                             — emergency stop
  get_encoders()                     — odometry
  reset_encoders()                   — reset odometry
  capture_image()                    — camera snapshot -> /tmp/frame.jpg

Usage:
  python3 servers/robot_server.py
"""
import subprocess
import sys
import time
import threading
from pathlib import Path

# Ensure project root is on path so hardware.* imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP
from hardware.motor import Robot

mcp = FastMCP("saras-robot")

# ── Hardware singleton (lazy-initialised on first tool call) ──────────────
_robot: Robot | None = None


def _get_robot() -> Robot:
    global _robot
    if _robot is None:
        _robot = Robot()
    return _robot


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def move(direction: str, speed: int = 150, duration: float = 0.0) -> str:
    """
    Drive the robot.

    Args:
        direction: "forward" | "backward" | "left" | "right"
        speed: motor speed 0-255 (default 150 for forward/backward, 180 for turns)
        duration: seconds to move then auto-stop (0 = continuous until stop() called)
    """
    r = _get_robot()
    direction = direction.lower()
    dispatch = {
        "forward":  r.forward,
        "backward": r.backward,
        "left":     r.left,
        "right":    r.right,
    }
    if direction not in dispatch:
        return f"Unknown direction '{direction}'. Use: forward / backward / left / right"

    if direction in ("forward", "backward") and speed < 150:
        speed = 150
    if direction in ("left", "right") and speed < 180:
        speed = 180

    dispatch[direction](speed)

    if duration > 0:
        time.sleep(duration)
        r.stop()
        return f"Moved {direction} at speed {speed} for {duration}s then stopped"

    return f"Moving {direction} at speed {speed} (call stop() to halt)"


@mcp.tool()
def stop() -> str:
    """Stop all motors immediately."""
    _get_robot().stop()
    return "Motors stopped"


@mcp.tool()
def get_encoders() -> dict:
    """
    Read encoder counts from all 4 motors.
    Use these to estimate distance travelled since last reset.

    Returns: {"m1": int, "m2": int, "m3": int, "m4": int}
    """
    e = _get_robot().get_encoders()
    return {"m1": e[0], "m2": e[1], "m3": e[2], "m4": e[3]}


@mcp.tool()
def reset_encoders() -> str:
    """Reset all encoder counts to zero. Call before a new movement segment."""
    _get_robot().reset_encoders()
    return "Encoders reset to zero"


@mcp.tool()
def capture_image() -> str:
    """
    Capture a snapshot from the Pi camera.

    Saves to /tmp/frame.jpg. After calling this, use Read("/tmp/frame.jpg")
    to view the image.
    """
    out_path = "/tmp/frame.jpg"
    subprocess.run(
        ["rpicam-jpeg", "-o", out_path, "--nopreview", "-t", "500"],
        check=True, timeout=6, capture_output=True,
    )
    return f"Image saved to {out_path}\nUse Read('{out_path}') to view it."


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
