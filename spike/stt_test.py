"""Fase 0: o gpt-transcribe aceita languages=["pt","en"]? Como fica o texto?

    .venv\\Scripts\\python spike\\stt_test.py [arquivo.wav]   # padrao: spike/out/sample.wav

Corta o arquivo com o chunker (Silero) e manda cada trecho em tres
variantes de parametros. A API key vem do keyring do Artemis (ou de
OPENAI_API_KEY). Custo: ~1 min de audio por variante, menos de 1 centavo.
"""

from __future__ import annotations

import io
import os
import sys
import time
import wave
from pathlib import Path

import keyring
import numpy as np
import openai

sys.path.insert(0, str(Path(__file__).parent))
from vad_compare import DEFAULT, SR, Chunker, read_wav, silero_frames  # noqa: E402

MODEL = "gpt-transcribe"
KEYWORDS = ["pull request", "deploy", "staging", "code review", "backlog",
            "product owner", "pipeline", "CI", "GitHub Actions", "rollback",
            "hotfix", "blocker", "checkout", "daily"]

VARIANTS = {
    "pt+en": {"languages": ["pt", "en"]},
    "pt": {"languages": ["pt"]},
    "pt+en+kw": {"languages": ["pt", "en"], "keywords": KEYWORDS},
}


def api_key() -> str:
    key = None
    try:
        key = keyring.get_password("ArtemisDictation", "openai_api_key")
    except Exception:
        pass
    key = key or os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Sem API key (keyring do Artemis ou OPENAI_API_KEY).")
    return key


def to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(client: openai.OpenAI, wav: bytes, prompt: str, extras: dict) -> str:
    base = {"model": MODEL, "file": ("audio.wav", wav, "audio/wav"),
            "response_format": "text"}
    if prompt:
        base["prompt"] = prompt
    try:
        r = client.audio.transcriptions.create(**base, **extras)
    except TypeError:
        # SDK sem os parametros novos: manda pelo corpo cru
        r = client.audio.transcriptions.create(**base, extra_body=extras)
    return (r if isinstance(r, str) else getattr(r, "text", "")).strip()


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    pcm = read_wav(path)
    frame_s, probs = silero_frames(pcm)
    ch = Chunker(frame_s)
    for p in probs:
        ch.feed(p)
    ch.flush()

    client = openai.OpenAI(api_key=api_key(), timeout=60, max_retries=0)
    for name, extras in VARIANTS.items():
        print(f"== {name}  {extras if 'keywords' not in extras else {**extras, 'keywords': f'[{len(KEYWORDS)} termos]'}}")
        tail = ""
        for c in ch.chunks:
            seg = pcm[int(c.start * SR):int(c.end * SR)]
            t0 = time.perf_counter()
            try:
                text = transcribe(client, to_wav(seg), tail, extras)
            except openai.APIStatusError as exc:
                print(f"   [{c.start:5.1f}s] ERRO {exc.status_code}: {exc.message[:200]}")
                break
            except Exception as exc:
                print(f"   [{c.start:5.1f}s] ERRO {exc!r}"[:300])
                break
            dt = time.perf_counter() - t0
            print(f"   [{c.start:5.1f}s, {c.end - c.start:4.1f}s audio, {dt:4.1f}s API] {text}")
            tail = text[-200:]
        print()


if __name__ == "__main__":
    main()
