"""OpenAI-compatible TTS: wav/pcm-only backends (e.g. Ollama "phonon") reject mp3 AND opus.

The existing opus->mp3 fallback cannot help them, so ``_generate_openai_tts`` retries once as
wav and ffmpeg-transcodes into the requested container, then remembers the base URL so later
calls skip the doomed request.
"""

import shutil
from pathlib import Path

import pytest

from tools import tts_tool, tts_tool_openai


class _Reject(Exception):
    status_code = 400

    def __init__(self, fmt: str):
        super().__init__(f"Error code: 400 - response_format \"{fmt}\" is not supported, use wav or pcm")


class _Resp:
    def __init__(self, src: Path):
        self._src = src

    def stream_to_file(self, path: str) -> None:
        shutil.copyfile(self._src, path)


class _Client:
    def __init__(self, wav: Path, calls: list):
        class _Speech:
            def create(self, **kw):
                calls.append(kw["response_format"])
                if kw["response_format"] not in ("wav", "pcm"):
                    raise _Reject(kw["response_format"])
                return _Resp(wav)

        class _Audio:
            speech = _Speech()

        self.audio = _Audio()
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def wav(tmp_path):
    import struct
    import wave
    p = tmp_path / "src.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(struct.pack("<h", 0) * 2400)
    return p


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
@pytest.mark.parametrize("ext,magic", [("mp3", None), ("ogg", b"OggS")])
def test_wav_only_backend_transcodes_and_is_remembered(tmp_path, monkeypatch, wav, ext, magic):
    monkeypatch.setattr(tts_tool_openai, "_WAV_ONLY_BASE_URLS", set())
    calls: list = []
    client = _Client(wav, calls)
    monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: (lambda **kw: client))
    base = "http://127.0.0.1:11434/v1"
    kwargs = dict(api_key="x", base_url=base, model="phonon", voice="Zoey")

    out = tmp_path / f"a.{ext}"
    assert tts_tool_openai._generate_openai_tts("hi", str(out), {}, **kwargs) == str(out)
    data = out.read_bytes()
    assert data and (magic is None or data.startswith(magic))
    assert calls[-1] == "wav" and calls[0] != "wav"
    assert base in tts_tool_openai._WAV_ONLY_BASE_URLS

    calls.clear()
    out2 = tmp_path / f"b.{ext}"
    tts_tool_openai._generate_openai_tts("hi", str(out2), {}, **kwargs)
    assert calls == ["wav"]  # no wasted mp3/opus request once remembered
    assert client.closed


def test_non_format_errors_still_propagate(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_tool_openai, "_WAV_ONLY_BASE_URLS", set())

    class _Boom(Exception):
        status_code = 500

    class _C:
        class audio:
            class speech:
                @staticmethod
                def create(**kw):
                    raise _Boom("server down")

        def close(self):
            pass

    monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: (lambda **kw: _C()))
    with pytest.raises(_Boom):
        tts_tool_openai._generate_openai_tts("hi", str(tmp_path / "x.mp3"), {}, api_key="x",
                                              base_url="http://h/v1", model="m", voice="v")
    assert not tts_tool_openai._WAV_ONLY_BASE_URLS
