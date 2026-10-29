"""Collect one utterance from PCM16 frames using an offline WebRTC VAD.

This service detects speech boundaries. It does not recognize words,
detect LiAo's name, open a microphone, or make network requests.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from hardware.desktop_audio import PCM16Audio


CaptureReason = Literal[
    "silence",
    "max_duration",
    "no_speech",
    "too_short",
    "cancelled",
]


class VoiceActivityError(RuntimeError):
    """Voice activity detection could not continue."""


@dataclass(frozen=True, slots=True)
class SpeechCaptureResult:
    """A completed capture.

    audio is None when no usable utterance exists.
    speech_seconds is the duration classified as speech by the detector.
    """

    audio: PCM16Audio | None
    reason: CaptureReason
    speech_seconds: float


class VoiceActivityService:
    """Collect speech after a wake phrase activates a voice turn.

    Call start(), then feed(frame) for each microphone frame.

    feed() returns None while waiting or recording. It returns a
    SpeechCaptureResult when the capture finishes.

    Call start() again for a new turn. All methods belong on one event loop.
    """

    def __init__(
        self,
        *,
        sample_rate_hz: int = 16_000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        silence_seconds: float = 0.9,
        start_timeout_seconds: float = 8.0,
        max_record_seconds: float = 30.0,
        start_speech_seconds: float = 0.12,
        min_speech_seconds: float = 0.24,
        pre_roll_seconds: float = 0.3,
    ) -> None:
        if (
            isinstance(sample_rate_hz, bool)
            or not isinstance(sample_rate_hz, int)
            or sample_rate_hz not in (8000, 16000, 32000, 48000)
        ):
            raise ValueError(
                "sample_rate_hz must be 8000, 16000, 32000, or 48000"
            )

        if (
            isinstance(frame_ms, bool)
            or not isinstance(frame_ms, int)
            or frame_ms not in (10, 20, 30)
        ):
            raise ValueError(
                "frame_ms must be 10, 20, or 30"
            )

        if (
            isinstance(aggressiveness, bool)
            or not isinstance(aggressiveness, int)
            or not 0 <= aggressiveness <= 3
        ):
            raise ValueError(
                "aggressiveness must be an integer from 0 to 3"
            )

        durations = {
            "silence_seconds": (
                silence_seconds,
                0.2,
                3.0,
            ),
            "start_timeout_seconds": (
                start_timeout_seconds,
                1.0,
                60.0,
            ),
            "max_record_seconds": (
                max_record_seconds,
                1.0,
                120.0,
            ),
            "start_speech_seconds": (
                start_speech_seconds,
                0.02,
                0.5,
            ),
            "min_speech_seconds": (
                min_speech_seconds,
                0.02,
                2.0,
            ),
            "pre_roll_seconds": (
                pre_roll_seconds,
                0.02,
                1.0,
            ),
        }

        for name, (value, lower, upper) in durations.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(
                    f"{name} must be from {lower} to {upper}"
                )

        if min_speech_seconds < start_speech_seconds:
            raise ValueError(
                "min_speech_seconds must not be shorter "
                "than start_speech_seconds"
            )

        if pre_roll_seconds < start_speech_seconds:
            raise ValueError(
                "pre_roll_seconds must cover start_speech_seconds"
            )

        minimum_recording_room = (
            pre_roll_seconds
            + silence_seconds
            + min_speech_seconds
        )

        if max_record_seconds < minimum_recording_room:
            raise ValueError(
                "max_record_seconds must leave room for "
                "speech, pre-roll, and silence"
            )

        self.sample_rate_hz = sample_rate_hz
        self.frame_ms = frame_ms

        self._mode = aggressiveness
        self._frame_bytes = (
            sample_rate_hz * frame_ms // 1000 * 2
        )
        self._frame_seconds = frame_ms / 1000

        self._silence_limit = math.ceil(
            silence_seconds / self._frame_seconds
        )
        self._wait_limit = math.ceil(
            start_timeout_seconds / self._frame_seconds
        )
        self._record_limit = math.floor(
            max_record_seconds / self._frame_seconds
        )
        self._start_limit = math.ceil(
            start_speech_seconds / self._frame_seconds
        )
        self._speech_minimum = math.ceil(
            min_speech_seconds / self._frame_seconds
        )

        # Retain a short quiet tail instead of cutting tightly at the
        # final voiced frame.
        self._trailing_padding = math.ceil(
            0.2 / self._frame_seconds
        )

        self._pre_roll: deque[tuple[bytes, bool]] = deque(
            maxlen=math.ceil(
                pre_roll_seconds / self._frame_seconds
            )
        )

        self._vad: Any = None
        self._active = False
        self._recording = False

        self._total_frames = 0
        self._consecutive_speech = 0
        self._speech_frames = 0
        self._quiet_frames = 0

        self._recorded: list[bytes] = []
        self._last_result: SpeechCaptureResult | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_recording(self) -> bool:
        return self._active and self._recording

    @property
    def last_result(self) -> SpeechCaptureResult | None:
        return self._last_result

    def start(self) -> None:
        """Begin a fresh capture, discarding any unfinished previous turn."""
        try:
            import webrtcvad
        except ImportError:
            raise VoiceActivityError(
                "install webrtcvad-wheels for offline speech detection"
            ) from None

        try:
            self._vad = webrtcvad.Vad(self._mode)
        except Exception as exc:
            raise VoiceActivityError(
                "could not initialize the speech detector"
            ) from exc

        self._clear_capture()
        self._total_frames = 0
        self._last_result = None
        self._active = True

    def _clear_capture(self) -> None:
        self._recording = False
        self._consecutive_speech = 0
        self._speech_frames = 0
        self._quiet_frames = 0

        self._pre_roll.clear()
        self._recorded.clear()

    def feed(
        self,
        frame: PCM16Audio,
    ) -> SpeechCaptureResult | None:
        """Consume one frame matching the configured rate and duration."""
        if not self._active or self._vad is None:
            raise VoiceActivityError(
                "call start() before feeding audio"
            )

        if not isinstance(frame, PCM16Audio):
            raise TypeError(
                "frame must be PCM16Audio"
            )

        if (
            frame.sample_rate_hz != self.sample_rate_hz
            or frame.channels != 1
        ):
            raise ValueError(
                "frame must be mono PCM16 "
                "at the configured sample rate"
            )

        if len(frame.pcm) != self._frame_bytes:
            raise ValueError(
                f"each frame must contain exactly "
                f"{self.frame_ms} milliseconds"
            )

        try:
            voiced = bool(
                self._vad.is_speech(
                    frame.pcm,
                    self.sample_rate_hz,
                )
            )
        except Exception as exc:
            self._active = False

            raise VoiceActivityError(
                "speech detection failed"
            ) from exc

        self._total_frames += 1

        if not self._recording:
            self._pre_roll.append(
                (frame.pcm, voiced)
            )

            self._consecutive_speech = (
                self._consecutive_speech + 1
                if voiced
                else 0
            )

            if self._consecutive_speech >= self._start_limit:
                self._recording = True

                # Include buffered frames so the beginning of the word
                # is retained while speech onset is being confirmed.
                self._recorded = [
                    pcm
                    for pcm, _ in self._pre_roll
                ]

                self._speech_frames = sum(
                    flag
                    for _, flag in self._pre_roll
                )

                self._pre_roll.clear()

            elif self._total_frames >= self._wait_limit:
                return self._finish(
                    "no_speech",
                    include_audio=False,
                )

            return None

        self._recorded.append(frame.pcm)

        if voiced:
            self._speech_frames += 1
            self._quiet_frames = 0
        else:
            self._quiet_frames += 1

        if len(self._recorded) >= self._record_limit:
            if self._speech_frames < self._speech_minimum:
                return self._finish(
                    "too_short",
                    include_audio=False,
                )

            return self._finish("max_duration")

        if self._quiet_frames >= self._silence_limit:
            if self._speech_frames >= self._speech_minimum:
                return self._finish(
                    "silence",
                    trim_silence=True,
                )

            # A brief burst is not a usable utterance. Keep waiting
            # without extending the overall no-speech timeout.
            self._clear_capture()

            if self._total_frames >= self._wait_limit:
                return self._finish(
                    "no_speech",
                    include_audio=False,
                )

        return None

    def _finish(
        self,
        reason: CaptureReason,
        *,
        include_audio: bool = True,
        trim_silence: bool = False,
    ) -> SpeechCaptureResult:
        audio = None

        speech_seconds = (
            self._speech_frames * self._frame_seconds
        )

        if include_audio and self._recorded:
            keep = len(self._recorded)

            if trim_silence:
                keep -= max(
                    0,
                    self._quiet_frames
                    - self._trailing_padding,
                )

            audio = PCM16Audio(
                pcm=b"".join(self._recorded[:keep]),
                sample_rate_hz=self.sample_rate_hz,
            )

        result = SpeechCaptureResult(
            audio=audio,
            reason=reason,
            speech_seconds=speech_seconds,
        )

        self._clear_capture()
        self._active = False
        self._last_result = result

        return result

    def cancel(self) -> SpeechCaptureResult:
        """Discard the current utterance without sending its audio to STT."""
        return self._finish(
            "cancelled",
            include_audio=False,
        )


__all__ = (
    "VoiceActivityService",
    "VoiceActivityError",
    "SpeechCaptureResult",
)