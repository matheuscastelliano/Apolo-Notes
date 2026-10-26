"""Fase 0: gera um WAV de "reuniao" sintetico, sem tocar nem capturar nada.

    .venv\\Scripts\\python spike\\make_sample.py

Usa a voz do Windows (System.Speech) gravando direto em arquivo. As falas
tem pausas curtas (respiro), medias (troca de assunto) e uma longa
(silencio), para exercitar o VAD e o chunker. Saida: spike/out/sample.wav
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import soxr

OUT = Path(__file__).parent / "out"
SR = 16000

# (fala, pausa depois em segundos)
SCRIPT = [
    ("Bom dia, pessoal. Vamos começar pela daily.", 0.4),
    ("Ontem eu terminei o pull request da feature de login e subi o deploy no staging.", 1.2),
    ("O code review ficou com a Ana, ela deve aprovar hoje de manhã.", 0.3),
    ("Hoje vou revisar o backlog com o product owner e ajustar o pipeline de CI no GitHub Actions.", 2.5),
    ("Sobre o bug do checkout: a gente decidiu fazer um rollback e abrir um hotfix até sexta-feira.", 0.6),
    ("Alguém tem algum blocker?", 8.0),
    ("Beleza, então fechou. Valeu, galera, até amanhã.", 1.0),
]


def tts_to_wav(text: str, path: Path) -> None:
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'pt-BR' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
        f"$s.SetOutputToWaveFile('{path}');"
        f"$s.Speak('{text}');"
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def read_mono16k(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert width == 2, "esperava PCM16"
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return soxr.resample(x, sr, SR) if sr != SR else x


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(0)
    parts = []
    timeline = []
    t = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        for i, (text, pause) in enumerate(SCRIPT):
            p = Path(tmp) / f"{i}.wav"
            tts_to_wav(text, p)
            speech = read_mono16k(p)
            parts.append(speech)
            timeline.append((t, t + len(speech) / SR, text))
            t += len(speech) / SR
            # ruido de fundo baixo (-60 dBFS) no lugar do silencio digital
            parts.append(rng.normal(0, 0.001, int(pause * SR)).astype(np.float32))
            t += pause
    x = np.concatenate(parts)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(OUT / "sample.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"{OUT / 'sample.wav'}  ({len(x) / SR:.1f}s)")
    for a, b, text in timeline:
        print(f"  {a:5.1f}-{b:5.1f}s  {text}")


if __name__ == "__main__":
    main()
