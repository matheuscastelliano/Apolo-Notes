"""Separa o canal "Outros" em vozes durante a reuniao, em janelas.

    <spool>/<sessao>/vox/chunk_<inicio_ms>.wav   trechos de fala ainda sem janela
    <spool>/<sessao>/vox/clip_<voz>.wav          a amostra de cada voz (3 a 8 s)
    <spool>/<sessao>/vox/voices.json             vozes vistas: tempo e amostra
    <spool>/<sessao>/diar.jsonl                  quem falou quando, em tempo de sessao

Cada trecho de fala dos "Outros" que o VAD corta vem para ca tambem (o do
spool e apagado assim que e transcrito). Quando juntam `window_seconds` de
fala, os trechos viram uma janela: um WAV so, com 0,4 s de silencio entre
eles, que vai para o diarize. Separar no fim levaria minutos (fase V0:
~0,4 x a duracao do audio); assim, no Parar, so falta a ultima janela.

Mesmas letras de uma janela para outra: a partir da 2a, a chamada leva a
amostra das vozes ja vistas como referencia, com o nome interno ("V1",
"V2"...). A API aceita no maximo 4: vao as 4 que falaram por ultimo. Voz que
volta com outra letra vira uma voz nova, e voce junta as duas na validacao.

Falhas: passageira (rede, 5xx) tenta de novo com backoff, sem perder audio.
Qualquer outra descarta aquela janela e segue - aquele trecho sai na nota
como "Outros". Separar vozes nunca derruba a reuniao.

Tudo fica no disco do spool: depois de um crash, o que ja foi separado esta
em diar.jsonl e o que faltava continua em vox/.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import re
import threading
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from ..errors import ApoloError, CredentialsError, TransientError
from ..providers.base import DiarSegment, Diarizer
from ..session.spool import SAMPLE_RATE, wav_bytes

log = logging.getLogger(__name__)

VOX_DIR = "vox"
TIMELINE_FILE = "diar.jsonl"
VOICES_FILE = "voices.json"

JOIN_SILENCE = 0.4
REF_MIN, REF_MAX = 3.0, 8.0
MAX_REFS = 4
# Falas da mesma voz com menos que isso entre elas contam como uma so, na
# hora de escolher a amostra.
TURN_GAP = 0.6

_CHUNK = re.compile(r"^chunk_(\d+)\.wav$")


@dataclass
class VoiceInfo:
    id: str
    seconds: float = 0.0
    last_seen: float = 0.0  # tempo de sessao da ultima fala
    clip_start: float = 0.0  # tempo de sessao do inicio da amostra
    clip_seconds: float = 0.0  # 0 = ainda sem amostra


@dataclass(frozen=True)
class _Chunk:
    start: float
    duration: float
    path: Path


def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class VoiceTracker:
    def __init__(
        self,
        diarizer: Diarizer,
        directory: Path,
        *,
        window_seconds: float = 120.0,
        languages: list[str] | None = None,
        on_change: Callable[[], None] | None = None,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
    ):
        self.diarizer = diarizer
        self.session_dir = Path(directory)
        self.dir = self.session_dir / VOX_DIR
        self.window_seconds = float(window_seconds)
        self.languages = list(languages or []) or None
        self.on_change = on_change or (lambda: None)
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending: list[_Chunk] = []
        self._windows: queue.Queue = queue.Queue()
        self._busy = 0  # janelas na fila ou sendo processadas
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timeline: list[DiarSegment] = []
        self.voices: dict[str, VoiceInfo] = {}
        # Com um participante so, separar nao serve para nada: a sessao pausa.
        self.paused = False
        self.failed: ApoloError | None = None
        self.windows_done = 0
        self._load()

    # --------------------------------------------------------- disco

    def _load(self) -> None:
        try:
            lines = (self.session_dir / TIMELINE_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                d = json.loads(line)
                self._timeline.append(DiarSegment(float(d["start"]), float(d["end"]), str(d["voice"])))
            except (ValueError, KeyError, TypeError):
                continue
        try:
            data = json.loads((self.dir / VOICES_FILE).read_text(encoding="utf-8"))
            for item in data:
                info = VoiceInfo(**{k: item[k] for k in VoiceInfo.__dataclass_fields__ if k in item})
                self.voices[info.id] = info
        except (OSError, ValueError, TypeError):
            pass
        try:
            names = sorted(p.name for p in self.dir.iterdir())
        except OSError:
            names = []
        for name in names:
            m = _CHUNK.match(name)
            if m:
                path = self.dir / name
                try:
                    frames = max(0, path.stat().st_size - 44) // 2
                except OSError:
                    continue
                self._pending.append(_Chunk(int(m.group(1)) / 1000, frames / SAMPLE_RATE, path))

    def clip_path(self, voice: str) -> Path:
        return self.dir / f"clip_{voice}.wav"

    def clip_pcm(self, voice: str) -> np.ndarray | None:
        try:
            return read_pcm(self.clip_path(voice))
        except (OSError, wave.Error, EOFError):
            return None

    # --------------------------------------------------------- entrada

    def add(self, start: float, pcm: np.ndarray) -> None:
        """Um trecho de fala dos "Outros" (int16, 16 kHz). Thread de captura."""
        start_ms = max(0, int(round(start * 1000)))
        path = self.dir / f"chunk_{start_ms:09d}.wav"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav_bytes(pcm))
            tmp.replace(path)
        except OSError as exc:
            log.error("Vozes: nao consegui guardar o trecho: %s", exc)
            return
        with self._lock:
            self._pending.append(_Chunk(start_ms / 1000, len(pcm) / SAMPLE_RATE, path))
            full = not self.paused and sum(c.duration for c in self._pending) >= self.window_seconds
        if full:
            self._close_window()

    def _close_window(self) -> None:
        with self._lock:
            if not self._pending:
                return
            window, self._pending = sorted(self._pending, key=lambda c: c.start), []
            self._busy += 1
        self._windows.put(window)

    def flush(self) -> None:
        """Fim da gravacao: o que sobrou vira a ultima janela."""
        self._close_window()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = bool(paused)
            full = not self.paused and sum(c.duration for c in self._pending) >= self.window_seconds
        if full:
            self._close_window()

    # ---------------------------------------------------------- estado

    def timeline(self) -> list[DiarSegment]:
        with self._lock:
            return list(self._timeline)

    def busy(self) -> bool:
        with self._lock:
            return self._busy > 0

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._idle:
            while self._busy:
                if self._stop.is_set():
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._idle.wait(0.2 if remaining is None else min(0.2, remaining))
        return True

    # ----------------------------------------------------------- ciclo

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voices", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                window = self._windows.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(window)
            except Exception:
                log.exception("Falha inesperada na separacao de vozes")
            finally:
                with self._idle:
                    self._busy -= 1
                    self._idle.notify_all()
                try:
                    self.on_change()
                except Exception:
                    log.exception("Erro no aviso de mudanca das vozes")

    # ----------------------------------------------------------- janela

    def _process(self, window: list[_Chunk]) -> None:
        pieces, parts, offset = [], [], 0.0
        gap = np.zeros(int(JOIN_SILENCE * SAMPLE_RATE), dtype=np.int16)
        for chunk in window:
            try:
                pcm = read_pcm(chunk.path)
            except (OSError, wave.Error, EOFError):
                continue
            seconds = len(pcm) / SAMPLE_RATE
            pieces.append((offset, chunk.start, seconds))
            parts += [pcm, gap]
            offset += seconds + JOIN_SILENCE
        if not pieces or self.failed is not None:
            self._discard(window)
            return
        audio = np.concatenate(parts)
        refs = self._references()

        failures = 0
        while True:
            if self._stop.is_set():
                return  # os trechos ficam em vox/ para a proxima vez
            try:
                started = time.monotonic()
                segs = self.diarizer.diarize(
                    wav_bytes(audio), references=refs or None, languages=self.languages
                )
                break
            except TransientError as exc:
                failures += 1
                delay = exc.retry_after or min(self.backoff_max,
                                               self.backoff_initial * 2 ** (failures - 1))
                log.info("Vozes: %s; tentando de novo em %.0fs", exc.message, delay)
                self._stop.wait(delay)
            except CredentialsError as exc:
                log.error("Vozes: parando a separacao: %s", exc)
                self.failed = exc
                self._discard(window)
                return
            except ApoloError as exc:
                log.warning("Vozes: janela de %.0fs sem separacao: %s", offset, exc)
                self._discard(window)
                return
        log.info("Vozes: janela de %.0fs separada em %.0fs (%d refs)",
                 offset, time.monotonic() - started, len(refs))
        self._apply(segs, pieces, audio, refs)
        self._discard(window)
        self.windows_done += 1

    def _references(self) -> dict[str, bytes]:
        with self._lock:
            known = [v for v in self.voices.values() if v.clip_seconds > 0]
        known.sort(key=lambda v: -v.last_seen)
        refs = {}
        for v in known[:MAX_REFS]:
            try:
                refs[v.id] = self.clip_path(v.id).read_bytes()
            except OSError:
                continue
        return refs

    def _new_id(self) -> str:
        numbers = [int(v[1:]) for v in self.voices if v[1:].isdigit()]
        return f"V{max(numbers, default=0) + 1}"

    def voice_list(self) -> list[VoiceInfo]:
        """Copia das vozes vistas, por ordem de id."""
        with self._lock:
            items = [VoiceInfo(**asdict(v)) for v in self.voices.values()]
        return sorted(items, key=lambda v: (len(v.id), v.id))

    def _apply(self, segs: list[DiarSegment], pieces, audio: np.ndarray, refs: dict) -> None:
        with self._lock:
            mapped = self._apply_locked(segs, pieces, audio, refs)
            voices = [asdict(v) for v in self.voices.values()]
        try:
            with (self.session_dir / TIMELINE_FILE).open("a", encoding="utf-8") as f:
                for seg in mapped:
                    f.write(json.dumps({"start": seg.start, "end": seg.end,
                                        "voice": seg.speaker}) + "\n")
                f.flush()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / (VOICES_FILE + ".tmp")
            tmp.write_text(json.dumps(voices), encoding="utf-8")
            tmp.replace(self.dir / VOICES_FILE)
        except OSError as exc:
            log.error("Vozes: nao consegui gravar o resultado: %s", exc)

    def _apply_locked(self, segs, pieces, audio, refs) -> list[DiarSegment]:
        rename: dict[str, str] = {}
        local: list[tuple[float, float, str]] = []  # tempo da janela
        for s in segs:
            if s.speaker in refs:
                vid = s.speaker
            else:
                vid = rename.get(s.speaker)
                if vid is None:
                    vid = self._new_id()
                    rename[s.speaker] = vid
                    self.voices[vid] = VoiceInfo(vid)
            local.append((s.start, s.end, vid))

        mapped: list[DiarSegment] = []
        for a, b, vid in local:
            for off, sess_start, seconds in pieces:
                lo, hi = max(a, off), min(b, off + seconds)
                if hi - lo > 0.05:
                    mapped.append(DiarSegment(round(sess_start + lo - off, 3),
                                              round(sess_start + hi - off, 3), vid))
        for seg in mapped:
            info = self.voices.setdefault(seg.speaker, VoiceInfo(seg.speaker))
            info.seconds += seg.end - seg.start
            info.last_seen = max(info.last_seen, seg.end)
        self._update_clips(local, pieces, audio)
        self._timeline.extend(mapped)
        return mapped

    def _update_clips(self, local, pieces, audio: np.ndarray) -> None:
        """Amostra de cada voz: a fala continua mais longa, de 3 a 8 s."""
        turns: dict[str, list[list[float]]] = {}
        for a, b, vid in sorted(local):
            mine = turns.setdefault(vid, [])
            if mine and a - mine[-1][1] < TURN_GAP:
                mine[-1][1] = max(mine[-1][1], b)
            else:
                mine.append([a, b])
        for vid, spans in turns.items():
            info = self.voices[vid]
            best = None
            for a, b in spans:
                # A amostra tem que ser audio de verdade, num trecho so (sem
                # o silencio que junta os trechos da janela).
                for off, sess_start, seconds in pieces:
                    lo, hi = max(a, off), min(b, off + seconds)
                    if hi - lo >= REF_MIN and (best is None or hi - lo > best[1] - best[0]):
                        best = (lo, hi, off, sess_start)
            if best is None:
                continue
            lo, hi, off, sess_start = best
            hi = min(hi, lo + REF_MAX)
            if hi - lo <= info.clip_seconds:
                continue
            clip = audio[int(lo * SAMPLE_RATE):int(hi * SAMPLE_RATE)]
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.clip_path(vid).write_bytes(wav_bytes(clip))
            except OSError as exc:
                log.error("Vozes: nao consegui guardar a amostra de %s: %s", vid, exc)
                continue
            info.clip_seconds = round(hi - lo, 2)
            info.clip_start = round(sess_start + lo - off, 3)

    def _discard(self, window: list[_Chunk]) -> None:
        for chunk in window:
            try:
                chunk.path.unlink(missing_ok=True)
            except OSError:
                pass


def concat_wav(clips: list[np.ndarray], gap_seconds: float = 1.0) -> tuple[bytes, list[tuple[float, float]]]:
    """Junta amostras com silencio entre elas. Devolve o WAV e o intervalo de cada uma."""
    gap = np.zeros(int(gap_seconds * SAMPLE_RATE), dtype=np.int16)
    parts, spans, t = [], [], 0.0
    for clip in clips:
        seconds = len(clip) / SAMPLE_RATE
        spans.append((t, t + seconds))
        parts += [clip.astype(np.int16, copy=False), gap]
        t += seconds + gap_seconds
    pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    return wav_bytes(pcm), spans


def wav_to_pcm(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
