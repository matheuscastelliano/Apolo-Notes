"""Fase V0 do PLANO_VOZES.md: testa a separacao de vozes em janelas (descartavel).

Usa a gravacao de uma reuniao real (o FLAC do canal "Outros"), o resultado
da separacao do arquivo inteiro numa chamada so (a referencia, ja conferida
de ouvido) e a nota que o Apolo gerou. Responde a tres perguntas:

  1. Janelas: separando em blocos de N s, com amostras das vozes ja vistas
     como `known_speaker_references`, as letras se mantem de um bloco para
     o outro? Quanto tempo leva cada bloco?
  2. Identificacao: com amostras de voz de OUTRO trecho da reuniao (o papel
     da biblioteca), o modelo acerta o nome de cada voz? E deixa sem nome
     quem nao esta na biblioteca, em vez de chutar?
  3. Casamento: pondo as vozes nas falas da transcricao da nota, quantas
     ficam com uma voz so e quantas ficam mistas?

Uso:
  python spike/vozes_v0.py Others.flac referencia.json nota.md "A=Fernando,B=Marcos"

Gasta API: um diarize por janela (o audio inteiro uma vez) e algumas
chamadas curtas de identificacao. Resultados em spike/out/vozes_v0/.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from apolo.notes.reparse import parse_note  # noqa: E402
from apolo.providers.base import Segment  # noqa: E402
from apolo.secrets_store import require_api_key  # noqa: E402

MODEL = "gpt-4o-transcribe-diarize"
WINDOW = 240.0          # segundos de audio por janela
REF_MIN, REF_MAX = 3.0, 8.0
MIXED = 0.30            # 2a voz acima disso = fala mista
STEP = 0.1              # resolucao da comparacao quadro a quadro
OUT = ROOT / "spike" / "out" / "vozes_v0"


# ----------------------------------------------------------------- audio


def wav_data_url(pcm: np.ndarray, sr: int) -> str:
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


def wav_file(pcm: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def cut(audio: np.ndarray, sr: int, start: float, end: float) -> np.ndarray:
    return audio[int(start * sr):int(end * sr)]


# ------------------------------------------------------------------- API


client = OpenAI(api_key=require_api_key(), timeout=900)


def diarize(pcm: np.ndarray, sr: int, refs: dict[str, np.ndarray] | None = None):
    """Segmentos [(inicio, fim, voz, texto)] e o tempo da chamada."""
    params = dict(
        model=MODEL,
        file=("janela.wav", wav_file(pcm, sr), "audio/wav"),
        response_format="diarized_json",
        chunking_strategy="auto",
        language="pt",
    )
    if refs:
        names = list(refs)
        params["extra_body"] = {
            "known_speaker_names": names,
            "known_speaker_references": [wav_data_url(refs[n], sr) for n in names],
        }
    t0 = time.time()
    result = client.audio.transcriptions.create(**params)
    data = result.model_dump() if hasattr(result, "model_dump") else result
    segs = [(s["start"], s["end"], s["speaker"], s.get("text", "")) for s in data["segments"]]
    return segs, time.time() - t0


# ------------------------------------------------------------ utilidades


def frames(segs) -> dict[int, str]:
    lab = {}
    for a, b, spk, *_ in segs:
        for i in range(int(a / STEP), int(b / STEP)):
            lab[i] = spk
    return lab


def best_clip(segs, speaker: str, after: float = 0.0, before: float = 1e9):
    """O trecho mais longo (>= REF_MIN) da voz, cortado em REF_MAX."""
    mine = [s for s in segs if s[2] == speaker and s[0] >= after and s[1] <= before]
    # Junta falas seguidas da mesma voz: o diarize picota em frases curtas.
    turns: list[list[float]] = []
    for a, b, *_ in sorted(mine):
        if turns and a - turns[-1][1] < 0.6:
            turns[-1][1] = b
        else:
            turns.append([a, b])
    turns = [t for t in turns if t[1] - t[0] >= REF_MIN]
    if not turns:
        return None
    a, b = max(turns, key=lambda t: t[1] - t[0])
    return a, min(b, a + REF_MAX)


def seconds_by_speaker(segs) -> Counter:
    c = Counter()
    for a, b, spk, *_ in segs:
        c[spk] += b - a
    return c


# ------------------------------------------------------- 1. janelas


def run_windows(audio, sr):
    total = len(audio) / sr
    bounds = []
    t = 0.0
    while t < total:
        bounds.append((t, min(total, t + WINDOW)))
        t += WINDOW

    # Janela 1 sozinha: e dela que saem as primeiras referencias.
    a0, b0 = bounds[0]
    first, secs = diarize(cut(audio, sr, a0, b0), sr)
    timings = [secs]
    voices: dict[str, np.ndarray] = {}
    rename: dict[str, str] = {}
    for spk, dur in seconds_by_speaker(first).most_common():
        clip = best_clip(first, spk)
        if clip and len(voices) < 4:
            name = f"V{len(voices) + 1}"
            voices[name] = cut(audio, sr, a0 + clip[0], a0 + clip[1])
            rename[spk] = name
    merged = [(a0 + a, a0 + b, rename.get(s, f"w1:{s}"), txt) for a, b, s, txt in first]
    print(f"  janela 1: {secs:.0f}s, vozes {dict(seconds_by_speaker(first))} -> {rename}")

    # As outras em paralelo, todas com as referencias da janela 1. Ao vivo
    # elas chegariam uma a cada WINDOW segundos; o que importa e o tempo de
    # cada uma.
    def one(i_bounds):
        i, (a, b) = i_bounds
        segs, secs = diarize(cut(audio, sr, a, b), sr, voices)
        return i, a, segs, secs

    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, a, segs, secs in sorted(pool.map(one, enumerate(bounds[1:], start=2))):
            timings.append(secs)
            got = seconds_by_speaker(segs)
            print(f"  janela {i}: {secs:.0f}s, vozes " +
                  ", ".join(f"{k}={v:.0f}s" for k, v in got.most_common()))
            merged += [(a + s0, a + s1, spk if spk in voices else f"w{i}:{spk}", txt)
                       for s0, s1, spk, txt in segs]
    return merged, voices, timings, bounds


def compare_with_reference(windowed, reference):
    """Concordancia quadro a quadro, com UM mapa so para a reuniao toda."""
    fr, fw = frames(reference), frames(windowed)
    both = [i for i in fr if i in fw]
    pairs = Counter((fr[i], fw[i]) for i in both)
    mapping = {}
    for ref_spk in {p[0] for p in pairs}:
        cands = [(n, w) for (r, w), n in pairs.items() if r == ref_spk]
        mapping[ref_spk] = max(cands)[1]
    agree = sum(n for (r, w), n in pairs.items() if mapping.get(r) == w)
    return agree / max(1, len(both)), mapping, pairs


# --------------------------------------------------- 2. identificacao


def identify(audio, sr, reference, names: dict[str, str], voices, mapping):
    """Biblioteca = amostras da 2a metade da reuniao; vozes = da janela 1."""
    half = len(audio) / sr / 2
    library = {}
    for ref_spk, person in names.items():
        clip = best_clip(reference, ref_spk, after=half)
        if clip:
            library[person] = cut(audio, sr, *clip)
    # Audio de teste: um trecho de cada voz anonima, com 1 s de silencio.
    gap = np.zeros(sr, dtype=audio.dtype)
    pieces, spans, t = [], {}, 0.0
    for v, pcm in voices.items():
        spans[v] = (t, t + len(pcm) / sr)
        pieces += [pcm, gap]
        t += len(pcm) / sr + 1.0
    test = np.concatenate(pieces)

    expected = {}
    inverse = {w: r for r, w in mapping.items()}
    for v in voices:
        ref_spk = inverse.get(v)
        expected[v] = names.get(ref_spk, "?")

    def guess(refs):
        segs, secs = diarize(test, sr, refs)
        out = {}
        for v, (a, b) in spans.items():
            c = Counter()
            for s0, s1, spk, _ in segs:
                c[spk] += max(0.0, min(b, s1) - max(a, s0))
            out[v] = c.most_common(1)[0][0] if c else "-"
        return out, secs

    results = {}
    results["todos"] = guess(library)
    # Negativo: so a 1a pessoa na biblioteca. A outra voz tem que voltar
    # SEM esse nome (uma letra), nao com o nome errado.
    only = dict(list(library.items())[:1])
    results[f"so {list(only)[0]}"] = guess(only)
    return expected, results


# ----------------------------------------------------- 3. casamento


def match_note(note_text: str, windowed, voice_names: dict[str, str]):
    parsed = parse_note(note_text)
    others = [e for e in parsed.entries if isinstance(e, Segment) and e.speaker == "others"]
    fw = frames(windowed)
    single, mixed, empty = 0, 0, 0
    preview = []
    for e in others:
        c = Counter(fw[i] for i in range(int(e.start / STEP), int(max(e.end, e.start + 1) / STEP))
                    if i in fw)
        total = sum(c.values())
        if not total:
            empty += 1
            label = "?"
        else:
            (top, n1), *rest = c.most_common()
            share2 = rest[0][1] / total if rest else 0.0
            label = voice_names.get(top, top)
            if share2 > MIXED:
                mixed += 1
                label += "/" + voice_names.get(rest[0][0], rest[0][0])
            else:
                single += 1
        preview.append(f"[{int(e.start)//60:02d}:{int(e.start)%60:02d}] {label}: {e.text[:80]}")
    return dict(falas=len(others), uma_voz=single, mistas=mixed, sem_voz=empty), preview


# ------------------------------------------------------------------ main


def main() -> None:
    flac, ref_json, note_md, names_arg = sys.argv[1:5]
    names = dict(item.split("=") for item in names_arg.split(","))
    OUT.mkdir(parents=True, exist_ok=True)
    audio, sr = sf.read(flac, dtype="float32")
    reference = [(s["start"], s["end"], s["speaker"], s.get("text", ""))
                 for s in json.load(open(ref_json, encoding="utf-8"))["segments"]]
    print(f"Audio: {len(audio)/sr:.0f}s. Referencia (arquivo inteiro): "
          + ", ".join(f"{k}={v:.0f}s" for k, v in seconds_by_speaker(reference).most_common()))

    print(f"\n1. Janelas de {WINDOW:.0f}s com referencias")
    windowed, voices, timings, bounds = run_windows(audio, sr)
    agree, mapping, pairs = compare_with_reference(windowed, reference)
    extra = sorted({w for (_, w) in pairs if not w.startswith("V")})
    print(f"  concorda com a referencia em {agree:.0%} | mapa {mapping}")
    print(f"  vozes fora das referencias: {extra or 'nenhuma'}")
    print(f"  tempo por janela: {', '.join(f'{t:.0f}s' for t in timings)} "
          f"(media {np.mean(timings):.0f}s para {WINDOW:.0f}s de audio)")

    print("\n2. Identificacao pelo nome")
    expected, results = identify(audio, sr, reference, names, voices, mapping)
    print(f"  esperado: {expected}")
    for label, (got, secs) in results.items():
        print(f"  biblioteca '{label}': {got} ({secs:.0f}s)")

    print("\n3. Casamento com a transcricao da nota")
    voice_names = {v: expected.get(v, v) for v in voices}
    stats, preview = match_note(Path(note_md).read_text(encoding="utf-8"), windowed, voice_names)
    print(f"  {stats}")
    print("  primeiras falas:")
    for line in preview[:14]:
        print("   ", line)

    json.dump({"windowed": windowed, "timings": timings, "agree": agree, "mapping": mapping,
               "expected": expected, "identify": {k: v[0] for k, v in results.items()},
               "match": stats, "preview": preview},
              open(OUT / "resultado.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nDetalhes em {OUT / 'resultado.json'}")


if __name__ == "__main__":
    main()
