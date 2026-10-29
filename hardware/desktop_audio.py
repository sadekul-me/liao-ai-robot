"""Continuous PC microphone input and PCM16 playback for LiAo.

One consumer reads microphone frames for wake-word and speech detection.
Microphone frames are discarded during playback and briefly afterwards.

This module does not perform wake-word detection, STT, VAD, or TTS.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


class DesktopAudioError(RuntimeError):
    """A PC audio device or stream could not complete an operation."""


@dataclass(frozen=True, slots=True)
class PCM16Audio:
    """Signed little-endian PCM16 samples without a WAV or MP3 header."""

    pcm: bytes
    sample_rate_hz: int
    channels: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.pcm, bytes):
            raise TypeError("pcm must be bytes")

        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or not 8_000 <= self.sample_rate_hz <= 48_000
        ):
            raise ValueError(
                "sample_rate_hz must be an integer from 8000 to 48000"
            )

        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels not in (1, 2)
        ):
            raise ValueError("channels must be 1 or 2")

        if len(self.pcm) % (2 * self.channels):
            raise ValueError("pcm must contain complete audio frames")

        if self.duration_seconds > 120:
            raise ValueError("one audio clip must not exceed 120 seconds")

    @property
    def frame_count(self) -> int:
        return len(self.pcm) // (2 * self.channels)

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate_hz


async def _blocking(
    action: Callable[..., Any],
    *args: Any,
) -> Any:
    """Keep ownership of a worker until it exits, even on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(action, *args))

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class DesktopAudio:
    """Own one microphone stream and one speaker playback worker.

    Use all async methods on one event loop.

    Device values may be PortAudio indices, device names, or None for
    the system default. Input is always mono PCM16. Output uses the
    sample rate and channels declared by its audio clip.
    """

    def __init__(
        self,
        *,
        input_device: int | str | None = None,
        output_device: int | str | None = None,
        sample_rate_hz: int = 16_000,
        frame_ms: int = 20,
        queue_frames: int = 100,
        playback_guard_seconds: float = 0.2,
    ) -> None:
        PCM16Audio(b"", sample_rate_hz)

        if (
            isinstance(frame_ms, bool)
            or not isinstance(frame_ms, int)
            or frame_ms not in (10, 20, 30)
        ):
            raise ValueError("frame_ms must be 10, 20, or 30")

        if sample_rate_hz * frame_ms % 1000:
            raise ValueError(
                "sample rate must produce a whole number of samples per frame"
            )

        if (
            isinstance(queue_frames, bool)
            or not isinstance(queue_frames, int)
            or not 2 <= queue_frames <= 200
        ):
            raise ValueError(
                "queue_frames must be an integer from 2 to 200"
            )

        if (
            isinstance(playback_guard_seconds, bool)
            or not isinstance(playback_guard_seconds, (int, float))
            or not math.isfinite(playback_guard_seconds)
            or not 0 <= playback_guard_seconds <= 2
        ):
            raise ValueError(
                "playback_guard_seconds must be from 0 to 2"
            )

        self.sample_rate_hz = sample_rate_hz
        self.frame_ms = frame_ms

        self._input_device = input_device
        self._output_device = output_device
        self._samples_per_frame = sample_rate_hz * frame_ms // 1000

        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            queue_frames
        )

        self._guard = float(playback_guard_seconds)
        self._ignore_until = 0.0
        self._generation = 0

        self._started = False
        self._reading = False
        self._input_error: str | None = None

        self._sd: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._play_task: asyncio.Task[None] | None = None

        self._playing = threading.Event()
        self._input_paused = threading.Event()
        self._stop_output = threading.Event()
        self._output_lock = threading.Lock()

        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    async def start(self) -> None:
        """Validate devices and begin continuous microphone capture."""
        async with self._lifecycle_lock:
            if self._started:
                return

            if self._reading:
                raise DesktopAudioError(
                    "close the previous frame iterator before restarting"
                )

            if sys.byteorder != "little":
                raise DesktopAudioError(
                    "this adapter requires a little-endian platform"
                )

            try:
                import sounddevice as sd
            except ImportError:
                raise DesktopAudioError(
                    "install sounddevice for PC audio"
                ) from None

            self._sd = sd
            self._loop = asyncio.get_running_loop()
            self._generation += 1

            self._input_error = None
            self._ignore_until = 0.0
            self._input_paused.clear()
            self._discard_input()

            try:
                await _blocking(
                    self._open_input,
                    self._generation,
                )
            except BaseException as exc:
                await _blocking(self._close_input)

                if isinstance(exc, asyncio.CancelledError):
                    raise

                if not isinstance(exc, Exception):
                    raise

                raise DesktopAudioError(
                    "PC audio startup failed; "
                    "check device selection and sample rate"
                ) from exc

            self._started = True

    def _open_input(self, generation: int) -> None:
        """Open the input stream on a worker thread."""
        self._sd.check_input_settings(
            device=self._input_device,
            samplerate=self.sample_rate_hz,
            channels=1,
            dtype="int16",
        )

        self._sd.query_devices(
            self._output_device,
            "output",
        )

        loop = self._loop

        def callback(data, frames, timing, status) -> None:
            if (
                self._playing.is_set()
                or self._input_paused.is_set()
                or loop is None
            ):
                return

            valid = not status and frames == self._samples_per_frame
            raw = bytes(data)

            # PortAudio invokes this callback outside the asyncio loop.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    self._accept_input,
                    generation,
                    raw,
                    valid,
                )

        self._input_stream = self._sd.RawInputStream(
            device=self._input_device,
            samplerate=self.sample_rate_hz,
            channels=1,
            dtype="int16",
            blocksize=self._samples_per_frame,
            callback=callback,
        )

        self._input_stream.start()

    def _accept_input(
        self,
        generation: int,
        data: bytes,
        valid: bool,
    ) -> None:
        """Accept frames on the event loop, never on the callback thread."""
        if (
            not self._started
            or generation != self._generation
            or self.is_playing
            or self._input_paused.is_set()
            or self._loop is None
            or self._loop.time() < self._ignore_until
            or self._input_error is not None
        ):
            return

        if not valid or len(data) != self._samples_per_frame * 2:
            self._input_error = (
                "microphone stream reported missing or invalid frames"
            )
        elif self._queue.full():
            self._input_error = (
                "microphone consumer is too slow; audio queue overflowed"
            )
        else:
            self._queue.put_nowait(data)
            return

        # Report lost audio rather than silently producing a broken recording.
        self._discard_input()
        self._queue.put_nowait(None)

    def _discard_input(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def pause_input(self) -> None:
        """Discard input while STT or AI processes an already recorded turn.

        Call this on the owning event loop before waiting for a slow model
        or network request.
        """
        self._input_paused.set()
        self._discard_input()

        if self._input_error is not None or not self._started:
            self._queue.put_nowait(None)

    def resume_input(self) -> None:
        """Resume capture after processing, on the owning event loop."""
        if not self._started:
            raise DesktopAudioError("PC audio is not started")

        if self._input_error is not None:
            raise DesktopAudioError(self._input_error)

        self._discard_input()
        self._input_paused.clear()

    async def frames(self) -> AsyncIterator[PCM16Audio]:
        """Yield small mono frames to one wake-word/VAD consumer."""
        if not self._started:
            raise DesktopAudioError("PC audio is not started")

        if self._reading:
            raise DesktopAudioError(
                "only one microphone frame consumer is supported"
            )

        self._reading = True

        try:
            while self._started:
                if self._input_error is not None:
                    raise DesktopAudioError(self._input_error)

                raw = await self._queue.get()

                if self._input_error is not None:
                    raise DesktopAudioError(self._input_error)

                if raw is None or not self._started:
                    return

                if (
                    not self.is_playing
                    and not self._input_paused.is_set()
                ):
                    yield PCM16Audio(
                        pcm=raw,
                        sample_rate_hz=self.sample_rate_hz,
                    )
        finally:
            self._reading = False

    async def play(self, audio: PCM16Audio) -> None:
        """Play decoded PCM16 and discard microphone input until finished."""
        if not isinstance(audio, PCM16Audio) or not audio.pcm:
            raise ValueError(
                "audio must be a nonempty PCM16Audio clip"
            )

        if not self._started:
            raise DesktopAudioError("PC audio is not started")

        if self._play_task is not None:
            raise DesktopAudioError("speaker is already playing")

        self._playing.set()
        self._stop_output.clear()
        self._discard_input()

        worker = asyncio.create_task(
            asyncio.to_thread(self._play_clip, audio)
        )
        self._play_task = worker

        try:
            await asyncio.shield(worker)

        except asyncio.CancelledError:
            await self.stop_playback()
            raise

        except Exception as exc:
            raise DesktopAudioError(
                "PC speaker playback failed"
            ) from exc

        finally:
            await asyncio.gather(
                worker,
                return_exceptions=True,
            )

            self._discard_input()

            if self._input_error is not None or not self._started:
                self._queue.put_nowait(None)

            if self._loop is not None:
                self._ignore_until = self._loop.time() + self._guard

            self._play_task = None
            self._playing.clear()

    def _play_clip(self, audio: PCM16Audio) -> None:
        """Own the output stream entirely within its playback worker."""
        stream = self._sd.RawOutputStream(
            device=self._output_device,
            samplerate=audio.sample_rate_hz,
            channels=audio.channels,
            dtype="int16",
        )

        try:
            with self._output_lock:
                self._output_stream = stream

            if self._stop_output.is_set():
                return

            stream.start()

            block_bytes = 1024 * 2 * audio.channels

            for offset in range(0, len(audio.pcm), block_bytes):
                if self._stop_output.is_set():
                    return

                underflowed = stream.write(
                    audio.pcm[offset:offset + block_bytes]
                )

                if underflowed:
                    raise DesktopAudioError(
                        "PC speaker output underflowed"
                    )

            if not self._stop_output.is_set():
                # Drain the final samples before reporting completion.
                stream.stop()

        finally:
            with self._output_lock:
                self._output_stream = None

            with suppress(Exception):
                stream.abort()

            stream.close()

    async def stop_playback(self) -> None:
        """Interrupt this adapter's output and wait for its worker."""
        self._stop_output.set()

        with self._output_lock:
            stream = self._output_stream

        if stream is not None:
            with suppress(Exception):
                await _blocking(stream.abort)

        worker = self._play_task

        if worker is not None:
            await asyncio.gather(
                worker,
                return_exceptions=True,
            )

    def _close_input(self) -> None:
        stream = self._input_stream
        self._input_stream = None

        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()

    async def stop(self) -> None:
        """Stop capture, unblock the consumer, and release the devices."""
        async with self._lifecycle_lock:
            self._started = False
            self._generation += 1

            self._discard_input()
            self._queue.put_nowait(None)

            try:
                await self.stop_playback()
            finally:
                await _blocking(self._close_input)

    async def __aenter__(self) -> DesktopAudio:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type,
        exc,
        traceback,
    ) -> None:
        await self.stop()


__all__ = (
    "DesktopAudio",
    "DesktopAudioError",
    "PCM16Audio",
)