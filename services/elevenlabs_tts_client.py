"""ElevenLabs speech synthesis returning LiAo's in-memory PCM16 audio.

Requests raw 24 kHz mono PCM directly. No SDK, MP3 decoder, or temporary
audio file is needed. Generation uses the account's API credits.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import wave
from collections.abc import Mapping
from io import BytesIO

import aiohttp

from hardware.desktop_audio import PCM16Audio


_BASE_URL = "https://api.elevenlabs.io/v1"
_SAMPLE_RATE_HZ = 24_000
_OUTPUT_FORMAT = "pcm_24000"


class ElevenLabsTTSError(RuntimeError):
    """A sanitized synthesis error without keys or provider responses."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ElevenLabsTTSClient:
    """Own an async HTTP session and serialize speech generation.

    Start once, call synthesize(text) for replies, then stop on shutdown.
    The caller owns speaker playback.

    Requests are not automatically retried. Use the instance on one
    event loop.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_v3",
        timeout_seconds: float = 60.0,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        language_code: str | None = None,
        max_text_chars: int = 2000,
        max_audio_seconds: float = 120.0,
    ) -> None:
        for value, name in (
            (api_key, "api_key"),
            (voice_id, "voice_id"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value.strip().lower().startswith(
                    ("replace", "your_")
                )
            ):
                raise ValueError(
                    f"set a real ElevenLabs {name}"
                )

        api_key = api_key.strip()
        voice_id = voice_id.strip()

        if (
            not api_key.isprintable()
            or any(
                character.isspace()
                for character in api_key
            )
        ):
            raise ValueError(
                "api_key must not contain whitespace or control characters"
            )

        if re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}",
            voice_id,
        ) is None:
            raise ValueError(
                "voice_id must be an ID from your ElevenLabs account"
            )

        if (
            not isinstance(model_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}",
                model_id,
            ) is None
        ):
            raise ValueError(
                "model_id must be a valid model identifier"
            )

        for value, name, lower, upper in (
            (
                timeout_seconds,
                "timeout_seconds",
                1.0,
                120.0,
            ),
            (
                stability,
                "stability",
                0.0,
                1.0,
            ),
            (
                similarity_boost,
                "similarity_boost",
                0.0,
                1.0,
            ),
            (
                max_audio_seconds,
                "max_audio_seconds",
                1.0,
                120.0,
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

        if (
            isinstance(max_text_chars, bool)
            or not isinstance(max_text_chars, int)
            or not 1 <= max_text_chars <= 5000
        ):
            raise ValueError(
                "max_text_chars must be an integer from 1 to 5000"
            )

        if language_code is not None:
            if not isinstance(language_code, str):
                raise TypeError(
                    "language_code must be a language code or None"
                )

            language_code = (
                language_code.strip().lower() or None
            )

            if (
                language_code is not None
                and re.fullmatch(
                    r"[a-z]{2,3}",
                    language_code,
                ) is None
            ):
                raise ValueError(
                    "use a language code such as bn, en, or zh"
                )

        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._timeout = float(timeout_seconds)

        self._stability = float(stability)
        self._similarity = float(similarity_boost)
        self._language = language_code

        self._max_text_chars = max_text_chars
        self._max_audio_bytes = (
            math.floor(
                max_audio_seconds * _SAMPLE_RATE_HZ
            )
            * 2
        )

        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> ElevenLabsTTSClient:
        """Read a loaded environment mapping.

        This method does not open the .env file itself.
        """
        values = (
            os.environ
            if environment is None
            else environment
        )

        try:
            timeout = float(
                values.get(
                    "ELEVENLABS_TIMEOUT_SECONDS",
                    "60",
                )
            )
        except (TypeError, ValueError):
            raise ValueError(
                "ELEVENLABS_TIMEOUT_SECONDS must be a number"
            ) from None

        return cls(
            api_key=values.get(
                "ELEVENLABS_API_KEY",
                "",
            ),
            voice_id=values.get(
                "ELEVENLABS_VOICE_ID",
                "",
            ),
            model_id=values.get(
                "ELEVENLABS_MODEL_ID",
                "eleven_v3",
            ),
            timeout_seconds=timeout,
            language_code=(
                values.get("ELEVENLABS_LANGUAGE_CODE")
                or None
            ),
        )

    @property
    def is_started(self) -> bool:
        return (
            self._session is not None
            and not self._session.closed
        )

    async def start(self) -> None:
        """Create the session without generating or charging for speech."""
        async with self._lock:
            if self.is_started:
                return

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self._timeout
                )
            )

    async def synthesize(
        self,
        text: str,
    ) -> PCM16Audio:
        """Generate one bounded speech clip without playing it."""
        if not isinstance(text, str):
            raise TypeError(
                "text must be a string"
            )

        text = text.strip()

        if not text or "\x00" in text:
            raise ValueError(
                "text must contain words without null characters"
            )

        if len(text) > self._max_text_chars:
            raise ValueError(
                "reply exceeds the configured TTS text limit"
            )

        async with self._lock:
            session = self._session

            if session is None or session.closed:
                raise ElevenLabsTTSError(
                    "call start() before speech synthesis"
                )

            payload = {
                "text": text,
                "model_id": self._model_id,
                "voice_settings": {
                    "stability": self._stability,
                    "similarity_boost": self._similarity,
                },
            }

            if self._language is not None:
                payload["language_code"] = self._language

            try:
                async with session.post(
                    (
                        f"{_BASE_URL}/text-to-speech/"
                        f"{self._voice_id}"
                    ),
                    params={
                        "output_format": _OUTPUT_FORMAT
                    },
                    json=payload,
                    headers={
                        "xi-api-key": self._api_key
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise self._http_error(
                            response.status
                        )

                    content_type = response.headers.get(
                        "Content-Type",
                        "application/octet-stream",
                    )

                    content_type = (
                        content_type
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )

                    if not (
                        content_type.startswith("audio/")
                        or content_type
                        == "application/octet-stream"
                    ):
                        raise ElevenLabsTTSError(
                            "speech provider returned a non-audio response"
                        )

                    pcm = bytearray()

                    async for chunk in (
                        response.content.iter_chunked(8192)
                    ):
                        pcm.extend(chunk)

                        if len(pcm) > self._max_audio_bytes:
                            raise ElevenLabsTTSError(
                                "generated speech exceeds "
                                "the audio duration limit"
                            )

                    if not pcm:
                        raise ElevenLabsTTSError(
                            "speech provider returned empty audio"
                        )

                    if len(pcm) % 2:
                        raise ElevenLabsTTSError(
                            "speech provider returned "
                            "an incomplete PCM16 frame"
                        )

                    if pcm.startswith(
                        (
                            b"RIFF",
                            b"ID3",
                            b"OggS",
                            b"fLaC",
                        )
                    ):
                        raise ElevenLabsTTSError(
                            "speech provider did not return "
                            "the requested raw PCM format"
                        )

                    return PCM16Audio(
                        pcm=bytes(pcm),
                        sample_rate_hz=_SAMPLE_RATE_HZ,
                    )

            except asyncio.CancelledError:
                raise

            except ElevenLabsTTSError:
                raise

            except asyncio.TimeoutError:
                raise ElevenLabsTTSError(
                    "ElevenLabs speech request timed out",
                    retryable=True,
                ) from None

            except aiohttp.ClientError:
                raise ElevenLabsTTSError(
                    "could not reach ElevenLabs; "
                    "check the network connection",
                    retryable=True,
                ) from None

            except Exception:
                raise ElevenLabsTTSError(
                    "ElevenLabs speech generation failed"
                ) from None

    @staticmethod
    def _http_error(
        status: int,
    ) -> ElevenLabsTTSError:
        hints = {
            400: (
                "check the voice, model, language, "
                "and voice settings"
            ),
            401: (
                "check the API key, account quota, "
                "and API permissions"
            ),
            402: (
                "check your remaining credits "
                "and account billing"
            ),
            403: (
                "check voice/model access "
                "and plan permissions"
            ),
            404: (
                "check the voice ID and its "
                "availability to your account"
            ),
            422: (
                "the provider rejected the "
                "speech request parameters"
            ),
            429: (
                "rate or concurrency limit reached; "
                "try later"
            ),
        }

        return ElevenLabsTTSError(
            (
                f"ElevenLabs HTTP {status}: "
                + hints.get(
                    status,
                    "speech request failed",
                )
            ),
            status_code=status,
            retryable=status in (
                408,
                429,
                500,
                502,
                503,
                504,
            ),
        )

    async def synthesize_wav(
        self,
        text: str,
    ) -> bytes:
        """Return PCM WAV bytes without writing an audio file."""
        audio = await self.synthesize(text)

        output = BytesIO()

        with wave.open(output, "wb") as writer:
            writer.setnchannels(audio.channels)
            writer.setsampwidth(2)
            writer.setframerate(audio.sample_rate_hz)
            writer.writeframes(audio.pcm)

        return output.getvalue()

    async def stop(self) -> None:
        """Close the session after the active request exits."""
        async with self._lock:
            session = self._session
            self._session = None

            if session is not None:
                await session.close()

    async def __aenter__(self) -> ElevenLabsTTSClient:
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
    "ElevenLabsTTSClient",
    "ElevenLabsTTSError",
)