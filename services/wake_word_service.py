"""Recognize LiAo wake phrases from local STT windows.

This is an STT-based prototype, not a trained acoustic wake-word model.
Audio and transcripts stay local when the supplied transcriber is local.
"""

from __future__ import annotations

import asyncio
import math
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace

from hardware.desktop_audio import PCM16Audio


Transcriber = Callable[[PCM16Audio], Awaitable[str | None]]

DEFAULT_ALIASES = (
    "LiAo",
    "Li Ao",
    "Lee Ao",
    "Leo",
    "Lio",
    "Leao",
    "Lee Oh",
    "লিয়াও",
    "লিয়াও",
    "লি আও",
    "লিও",
    "লিয়ো",
    "লিয়ো",
    "利奥",
    "利奧",
)

_GREETINGS = frozenset(
    {
        "hey",
        "hi",
        "hello",
        "okay",
        "ok",
        "হেই",
        "হে",
        "এই",
        "হ্যালো",
    }
)


class WakeWordError(RuntimeError):
    """The local wake-word processing pipeline could not continue."""


@dataclass(frozen=True, slots=True)
class WakeWordDetection:
    """A matched name and its source audio window, when available."""

    transcript: str
    matched_alias: str
    command_text: str
    audio: PCM16Audio | None = None


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Keep Unicode letters, numbers, and combining marks, including Bangla."""
    tokens = []
    start = None

    for index, character in enumerate(text):
        if unicodedata.category(character)[0] in {"L", "M", "N"}:
            if start is None:
                start = index

        elif start is not None:
            tokens.append(
                (
                    text[start:index].casefold(),
                    start,
                    index,
                )
            )
            start = None

    if start is not None:
        tokens.append(
            (
                text[start:].casefold(),
                start,
                len(text),
            )
        )

    return tokens


def _clear(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()


class WakeWordService:
    """Buffer microphone frames and run one local transcription worker.

    Call start(), then feed(frame) for each microphone frame.
    poll() returns a detection without blocking.

    A detection automatically pauses this service. Call resume() after
    the voice turn has finished.

    All public methods must run on the owning event loop. The supplied
    async transcriber must move blocking inference off that loop.
    """

    def __init__(
        self,
        transcribe: Transcriber | None = None,
        *,
        aliases: Iterable[str] = DEFAULT_ALIASES,
        window_seconds: float = 2.0,
        scan_interval_seconds: float = 0.75,
        minimum_audio_seconds: float = 0.75,
    ) -> None:
        if transcribe is not None and not callable(transcribe):
            raise TypeError(
                "transcribe must be an async callable or None"
            )

        for value, name in (
            (window_seconds, "window_seconds"),
            (scan_interval_seconds, "scan_interval_seconds"),
            (minimum_audio_seconds, "minimum_audio_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.25 <= value <= 4
            ):
                raise ValueError(
                    f"{name} must be from 0.25 to 4 seconds"
                )

        if (
            scan_interval_seconds > window_seconds
            or minimum_audio_seconds > window_seconds
        ):
            raise ValueError(
                "scan interval and minimum audio must fit inside the window"
            )

        if isinstance(aliases, (str, bytes)):
            raise TypeError(
                "aliases must be a collection of names"
            )

        prepared = []
        seen = set()

        for alias in aliases:
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias) > 64
            ):
                raise ValueError(
                    "each alias must be a nonempty string "
                    "of at most 64 characters"
                )

            normalized = unicodedata.normalize(
                "NFKC",
                alias,
            ).strip()

            words = tuple(
                token[0]
                for token in _tokens(normalized)
            )

            if not words:
                raise ValueError(
                    "aliases must contain letters or numbers"
                )

            if words not in seen:
                seen.add(words)
                prepared.append((alias, words))

            if len(prepared) > 64:
                raise ValueError(
                    "at most 64 distinct aliases are supported"
                )

        if not prepared:
            raise ValueError(
                "at least one wake-word alias is required"
            )

        # Prefer longer phrases before shorter aliases.
        self._aliases = sorted(
            prepared,
            key=lambda item: len(item[1]),
            reverse=True,
        )

        self._transcribe = transcribe

        self._window_bytes = round(
            window_seconds * 16_000
        ) * 2

        self._interval_bytes = round(
            scan_interval_seconds * 16_000
        ) * 2

        self._minimum_bytes = round(
            minimum_audio_seconds * 16_000
        ) * 2

        self._buffer = bytearray()
        self._since_scan = 0

        self._pending: asyncio.Queue[
            tuple[int, PCM16Audio]
        ] = asyncio.Queue(1)

        self._detections: asyncio.Queue[
            WakeWordDetection
        ] = asyncio.Queue(1)

        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._paused = False
        self._error: WakeWordError | None = None

    @property
    def is_running(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
        )

    @property
    def is_paused(self) -> bool:
        return self._paused

    def match_text(
        self,
        text: str,
    ) -> WakeWordDetection | None:
        """Match a name at the start, optionally after two greeting words.

        Whole-token matching avoids matching names inside unrelated words.
        Broad fuzzy matching is avoided because it increases false wakes.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        if len(text) > 4096:
            raise ValueError(
                "transcript exceeds the text limit"
            )

        text = unicodedata.normalize(
            "NFKC",
            text,
        ).strip()

        words = _tokens(text)
        position = 0

        while (
            position < min(2, len(words))
            and words[position][0] in _GREETINGS
        ):
            position += 1

        for alias, expected in self._aliases:
            candidate = tuple(
                word[0]
                for word in words[
                    position:position + len(expected)
                ]
            )

            if candidate == expected:
                end = words[
                    position + len(expected) - 1
                ][2]

                command = text[end:].lstrip(
                    " \t\r\n,.:;!?—–-"
                )

                return WakeWordDetection(
                    transcript=text,
                    matched_alias=alias,
                    command_text=command,
                )

        return None

    async def start(self) -> None:
        """Start after the shared local STT model has loaded."""
        if self.is_running:
            return

        if self._transcribe is None:
            raise WakeWordError(
                "configure a local async transcriber before start()"
            )

        self._error = None
        self._paused = False
        self._reset()

        self._task = asyncio.create_task(
            self._worker(),
            name="liao-wake-word",
        )

    def _reset(self) -> None:
        self._generation += 1
        self._buffer.clear()
        self._since_scan = 0

        _clear(self._pending)
        _clear(self._detections)

    def pause(self) -> None:
        """Discard pending windows and ignore in-flight results."""
        self._paused = True
        self._reset()

    def resume(self) -> None:
        """Re-arm detection after replying, using fresh microphone audio."""
        if self._error is not None:
            raise self._error

        if not self.is_running:
            raise WakeWordError(
                "wake-word service is not running"
            )

        self._reset()
        self._paused = False

    def feed(self, frame: PCM16Audio) -> None:
        """Buffer a short audio frame without waiting for inference."""
        if self._error is not None:
            raise self._error

        if not self.is_running:
            raise WakeWordError(
                "wake-word service is not running"
            )

        if not isinstance(frame, PCM16Audio):
            raise TypeError(
                "frame must be PCM16Audio"
            )

        if (
            frame.sample_rate_hz != 16_000
            or frame.channels != 1
        ):
            raise ValueError(
                "wake detection requires mono PCM16 at 16000 Hz"
            )

        if (
            not frame.pcm
            or frame.duration_seconds > 0.1
        ):
            raise ValueError(
                "feed microphone frames of at most 100 milliseconds"
            )

        if self._paused:
            return

        self._buffer.extend(frame.pcm)
        del self._buffer[:-self._window_bytes]

        self._since_scan += len(frame.pcm)

        if (
            len(self._buffer) < self._minimum_bytes
            or self._since_scan < self._interval_bytes
        ):
            return

        self._since_scan = 0

        audio = PCM16Audio(
            pcm=bytes(self._buffer),
            sample_rate_hz=16_000,
        )

        # Keep only the newest pending window if inference is slow.
        # This prevents an unbounded transcription backlog.
        _clear(self._pending)

        self._pending.put_nowait(
            (self._generation, audio)
        )

    def poll(self) -> WakeWordDetection | None:
        """Return a detection if ready; call regularly in the capture loop."""
        if self._error is not None:
            raise self._error

        try:
            return self._detections.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _worker(self) -> None:
        try:
            while True:
                generation, audio = await self._pending.get()

                if (
                    self._paused
                    or generation != self._generation
                ):
                    continue

                transcript = await self._transcribe(audio)

                if (
                    self._paused
                    or generation != self._generation
                    or transcript is None
                ):
                    continue

                detection = self.match_text(transcript)

                if detection is not None:
                    self._paused = True
                    self._reset()

                    self._detections.put_nowait(
                        replace(
                            detection,
                            audio=audio,
                        )
                    )

        except asyncio.CancelledError:
            raise

        except Exception:
            # Do not expose recordings or third-party error messages.
            self._error = WakeWordError(
                "local wake-word transcription failed"
            )

            self._paused = True
            self._reset()

    async def stop(self) -> None:
        """Stop this worker without closing the shared STT model."""
        self._paused = True
        self._reset()

        task = self._task
        self._task = None

        if task is not None:
            task.cancel()

            await asyncio.gather(
                task,
                return_exceptions=True,
            )


__all__ = (
    "WakeWordService",
    "WakeWordDetection",
    "WakeWordError",
    "DEFAULT_ALIASES",
)