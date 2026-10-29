"""Local faster-whisper transcription for LiAo's recorded PCM16 speech.

One model can be shared by wake-phrase recognition and command transcription.
Loading and inference run on worker threads. Audio is not sent to an API.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hardware.desktop_audio import PCM16Audio


class LocalSTTError(RuntimeError):
    """The local speech model could not load or transcribe a recording."""


async def _owned_worker(
    action: Callable[[], Any],
) -> Any:
    """Retain model ownership until the worker exits, even on cancellation."""
    task = asyncio.create_task(
        asyncio.to_thread(action)
    )

    try:
        return await asyncio.shield(task)

    except asyncio.CancelledError:
        # Cancelling an asyncio task cannot terminate native inference.
        # Keep the ownership lock until the worker has actually finished.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

        if task.done() and not task.cancelled():
            # Observe any worker failure; cancellation still wins.
            task.exception()

        raise


class LocalSTTClient:
    """Own one multilingual model and serialize access on one event loop.

    Input must be mono PCM16 at 16 kHz, as supplied by DesktopAudio and
    VoiceActivityService.

    Model downloads may require internet on first use. Once cached,
    local_files_only=True prevents model downloads.
    """

    def __init__(
        self,
        *,
        model_name: str = "base",
        language: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 2,
        beam_size: int = 3,
        download_root: str | Path | None = None,
        local_files_only: bool = False,
        max_audio_seconds: float = 60.0,
        max_transcript_chars: int = 4096,
        silence_rms_threshold: float = 0.001,
    ) -> None:
        if (
            not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError(
                "model_name must be a model name or local model directory"
            )

        if (
            language is not None
            and not isinstance(language, str)
        ):
            raise TypeError(
                "language must be a language code or None"
            )

        language = (
            language.strip().lower()
            if language is not None
            else None
        )

        if language in ("", "auto"):
            language = None

        if (
            language is not None
            and re.fullmatch(r"[a-z]{2,3}", language) is None
        ):
            raise ValueError(
                "use a language code such as bn, en, or zh"
            )

        if device not in ("cpu", "cuda", "auto"):
            raise ValueError(
                "device must be cpu, cuda, or auto"
            )

        # Choose CPU explicitly instead of assuming CUDA is installed.
        device = "cpu" if device == "auto" else device

        if compute_type not in (
            "int8",
            "float32",
            "float16",
            "int8_float16",
        ):
            raise ValueError(
                "unsupported compute_type"
            )

        if (
            device == "cpu"
            and compute_type not in ("int8", "float32")
        ):
            raise ValueError(
                "use int8 or float32 for CPU transcription"
            )

        for value, name, upper in (
            (cpu_threads, "cpu_threads", 32),
            (beam_size, "beam_size", 5),
            (
                max_transcript_chars,
                "max_transcript_chars",
                16_384,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(
                    f"{name} must be an integer from 1 to {upper}"
                )

        for value, name, lower, upper in (
            (
                max_audio_seconds,
                "max_audio_seconds",
                1.0,
                120.0,
            ),
            (
                silence_rms_threshold,
                "silence_rms_threshold",
                0.0,
                0.1,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(
                    f"{name} must be from {lower} to {upper}"
                )

        if not isinstance(local_files_only, bool):
            raise TypeError(
                "local_files_only must be a bool"
            )

        if download_root is not None:
            if (
                not isinstance(download_root, (str, Path))
                or not str(download_root).strip()
            ):
                raise ValueError(
                    "download_root must be a directory path or None"
                )

            download_root = str(
                Path(download_root).expanduser()
            )

        self._model_name = model_name.strip()
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._beam_size = beam_size
        self._download_root = download_root
        self._local_files_only = local_files_only

        self._max_audio_seconds = float(
            max_audio_seconds
        )
        self._max_transcript_chars = max_transcript_chars
        self._silence_rms_threshold = float(
            silence_rms_threshold
        )

        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        return self._model is not None

    async def start(self) -> None:
        """Load the model before starting continuous microphone processing."""
        async with self._lock:
            if self._model is not None:
                return

            try:
                self._model = await _owned_worker(
                    self._load_model
                )

            except asyncio.CancelledError:
                raise

            except LocalSTTError:
                raise

            except Exception:
                raise LocalSTTError(
                    "could not load the local speech model; "
                    "check the model name, local files, "
                    "and download connection"
                ) from None

    def _load_model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise LocalSTTError(
                "install faster-whisper for local speech recognition"
            ) from None

        return WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            num_workers=1,
            download_root=self._download_root,
            local_files_only=self._local_files_only,
        )

    async def transcribe(
        self,
        audio: PCM16Audio,
    ) -> str | None:
        """Return recognized words, or None for quiet/unrecognized input."""
        if not isinstance(audio, PCM16Audio):
            raise TypeError(
                "audio must be PCM16Audio"
            )

        if (
            audio.sample_rate_hz != 16_000
            or audio.channels != 1
        ):
            raise ValueError(
                "local STT requires mono PCM16 at 16000 Hz"
            )

        if not audio.pcm:
            raise ValueError(
                "audio must contain recorded samples"
            )

        if audio.duration_seconds > self._max_audio_seconds:
            raise ValueError(
                "recording exceeds the local STT duration limit"
            )

        async with self._lock:
            if self._model is None:
                raise LocalSTTError(
                    "call start() before transcription"
                )

            try:
                return await _owned_worker(
                    lambda: self._transcribe_sync(audio)
                )

            except asyncio.CancelledError:
                raise

            except LocalSTTError:
                raise

            except Exception:
                raise LocalSTTError(
                    "local speech transcription failed"
                ) from None

    def _transcribe_sync(
        self,
        audio: PCM16Audio,
    ) -> str | None:
        import numpy as np

        samples = np.frombuffer(
            audio.pcm,
            dtype="<i2",
        ).astype(np.float32)

        samples *= 1.0 / 32768.0

        rms = float(
            np.sqrt(
                np.mean(samples * samples)
            )
        )

        if (
            rms == 0
            or rms < self._silence_rms_threshold
        ):
            return None

        model = self._model

        if model is None:
            raise LocalSTTError(
                "local speech model is not started"
            )

        segments, _ = model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            # Commands already pass through VoiceActivityService.
            # Avoid another speech filter on short wake-name windows.
            vad_filter=False,
        )

        parts: list[str] = []
        length = 0

        # Segments are lazy: consume them on this worker thread while
        # the model ownership lock is still held.
        for segment in segments:
            if not isinstance(segment.text, str):
                raise LocalSTTError(
                    "speech model returned an invalid text segment"
                )

            part = " ".join(
                segment.text.split()
            )

            if not part:
                continue

            length += len(part) + (1 if parts else 0)

            if length > self._max_transcript_chars:
                raise LocalSTTError(
                    "local transcript exceeds the text limit"
                )

            parts.append(part)

        return " ".join(parts) or None

    async def stop(self) -> None:
        """Release our model reference after outstanding inference finishes."""
        async with self._lock:
            self._model = None

    async def __aenter__(self) -> LocalSTTClient:
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
    "LocalSTTClient",
    "LocalSTTError",
)