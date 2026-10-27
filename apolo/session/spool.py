r"""Spool: a fila de trechos de audio em disco, uma pasta por sessao.

    <spool_dir>/<sessao>/session.json
    <spool_dir>/<sessao>/seg/<inicio_ms>_<mic|sys>.wav

O audio e temporario (PLANO.md, principio 3): um trecho fica aqui so ate ser
transcrito; `mark_done` apaga o WAV. O que esta em seg/ e, portanto,
exatamente o que falta transcrever - inclusive depois de um crash, que e o
que permite oferecer "retomar a sessao" ao abrir o app de novo.

Teto: se a internet cair por muito tempo, o pendente nao cresce sem limite.
Passou de `max_pending_audio_hours`, o trecho mais antigo e descartado e a
lacuna fica registrada em session.json, para a nota mostrar "trecho nao
transcrito" no lugar certo.

Os WAVs sao escritos num .tmp e renomeados: quem le a fila nunca ve um
arquivo pela metade.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import config as config_module
from ..errors import SpoolError
from ..i18n import t

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
WAV_HEADER = 44  # WAV PCM canonico, como o modulo wave escreve

SESSION_FILE = "session.json"
SEG_DIR = "seg"
_SEG_NAME = re.compile(r"^(\d+)_(mic|sys)\.wav$")

# Estados gravados em session.json. So "finished" conta como completa.
STATE_RECORDING = "recording"
STATE_FINISHED = "finished"


@dataclass(frozen=True)
class SpooledSegment:
    path: Path
    channel: str  # "mic" | "sys"
    start: float  # segundos de sessao
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def start_ms(self) -> int:
        return int(self.path.name.split("_", 1)[0])

    def read_wav(self) -> bytes:
        return self.path.read_bytes()


def wav_bytes(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(sample_rate)
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buffer.getvalue()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class SpoolSession:
    """Uma sessao no spool. Seguro para usar de varias threads."""

    def __init__(self, directory: Path, max_pending_seconds: float | None = None):
        self.dir = directory
        self.seg_dir = directory / SEG_DIR
        self.max_pending_seconds = max_pending_seconds
        self._lock = threading.RLock()
        self.info = self._load_info()

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def state(self) -> str:
        return str(self.info.get("state") or "")

    # ----------------------------------------------------------- session.json

    def _load_info(self) -> dict[str, Any]:
        path = self.dir / SESSION_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("gaps", [])
                return data
        except (OSError, json.JSONDecodeError) as exc:
            if path.exists():
                log.warning("session.json ilegivel em %s: %s", self.dir, exc)
        # Sem session.json (crash logo na criacao) ou corrompido: o que esta
        # em seg/ ainda vale, entao a sessao continua listavel e recuperavel.
        return {"id": self.dir.name, "state": "", "gaps": []}

    def _save_info(self) -> None:
        try:
            _write_json(self.dir / SESSION_FILE, self.info)
        except OSError as exc:
            raise SpoolError(t("err.spool_write"), str(exc)) from exc

    def update(self, **fields: Any) -> None:
        """Mescla campos em session.json (preset, titulo, estado...)."""
        with self._lock:
            self.info.update(fields)
            self._save_info()

    def finish(self) -> None:
        self.update(state=STATE_FINISHED, ended_at=datetime.now().astimezone().isoformat())

    # ---------------------------------------------------------------- trechos

    def add(self, channel: str, start: float, pcm: np.ndarray) -> SpooledSegment:
        """Grava um trecho na fila. Aplica o teto de audio pendente."""
        if channel not in ("mic", "sys"):
            raise ValueError(f"canal invalido: {channel!r}")
        with self._lock:
            start_ms = max(0, int(round(start * 1000)))
            path = self.seg_dir / f"{start_ms:09d}_{channel}.wav"
            while path.exists():  # dois trechos no mesmo ms: empurra 1 ms
                start_ms += 1
                path = self.seg_dir / f"{start_ms:09d}_{channel}.wav"
            data = wav_bytes(pcm)
            try:
                self.seg_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            except OSError as exc:
                raise SpoolError(t("err.spool_write"), str(exc)) from exc
            seg = SpooledSegment(path, channel, start_ms / 1000, len(pcm) / SAMPLE_RATE)
            self._enforce_cap()
            return seg

    def pending(self) -> list[SpooledSegment]:
        """Trechos ainda nao transcritos, por ordem de inicio."""
        out = []
        try:
            entries = list(self.seg_dir.iterdir())
        except OSError:
            return []
        for path in entries:
            m = _SEG_NAME.match(path.name)
            if not m:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue  # apagado agora mesmo por outro worker
            duration = max(0, size - WAV_HEADER) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            out.append(SpooledSegment(path, m.group(2), int(m.group(1)) / 1000, duration))
        out.sort(key=lambda s: (s.start, s.channel))
        return out

    def pending_seconds(self) -> float:
        return sum(s.duration for s in self.pending())

    def mark_done(self, seg: SpooledSegment) -> None:
        """Trecho transcrito: o audio dele nao precisa mais existir."""
        try:
            seg.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Nao consegui apagar %s: %s", seg.path, exc)

    # ---------------------------------------------------------------- lacunas

    def add_gap(self, start: float, end: float, channel: str, reason: str) -> None:
        with self._lock:
            self.info.setdefault("gaps", []).append(
                {"start": round(start, 3), "end": round(end, 3),
                 "channel": channel, "reason": reason}
            )
            self._save_info()

    def gaps(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self.info.get("gaps", []), key=lambda g: g.get("start", 0))

    def _enforce_cap(self) -> None:
        if not self.max_pending_seconds or self.max_pending_seconds <= 0:
            return
        pending = self.pending()
        total = sum(s.duration for s in pending)
        while pending and total > self.max_pending_seconds:
            oldest = pending.pop(0)
            total -= oldest.duration
            log.warning("Spool cheio: descartando %s (%.1fs)", oldest.path.name, oldest.duration)
            self.mark_done(oldest)
            self.add_gap(oldest.start, oldest.end, oldest.channel, "spool_full")

    # ------------------------------------------------------------------ fim

    def is_complete(self) -> bool:
        return self.state == STATE_FINISHED and not self.pending()

    def delete(self) -> None:
        """Apaga a sessao inteira do disco (audio e session.json)."""
        try:
            shutil.rmtree(self.dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SpoolError(t("err.spool_delete"), str(exc)) from exc


class Spool:
    """A pasta raiz do spool, com uma subpasta por sessao."""

    def __init__(self, root: Path, max_pending_hours: float | None = 2.0):
        self.root = Path(root)
        self.max_pending_seconds = (
            float(max_pending_hours) * 3600 if max_pending_hours else None
        )

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "Spool":
        try:
            hours = float(config.get("max_pending_audio_hours") or 0) or None
        except (TypeError, ValueError):
            hours = 2.0
        return cls(config_module.spool_dir(config), hours)

    def create_session(self, **fields: Any) -> SpoolSession:
        now = datetime.now().astimezone()
        base = now.strftime("%Y%m%d-%H%M%S")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            name, n = base, 1
            while True:
                try:
                    (self.root / name).mkdir()
                    break
                except FileExistsError:
                    n += 1
                    name = f"{base}-{n}"
            directory = self.root / name
            (directory / SEG_DIR).mkdir()
        except OSError as exc:
            raise SpoolError(t("err.spool_write"), str(exc)) from exc
        session = SpoolSession(directory, self.max_pending_seconds)
        session.info = {
            "id": name,
            "created_at": now.isoformat(),
            "state": STATE_RECORDING,
            "gaps": [],
            **fields,
        }
        session._save_info()
        return session

    def open_session(self, session_id: str) -> SpoolSession:
        # Id e so um nome de pasta: nada de "..", separador ou caminho
        # absoluto - delete() apaga a pasta inteira.
        directory = self.root / session_id
        if (
            session_id in ("", ".", "..")
            or Path(session_id).name != session_id
            or not directory.is_dir()
            or directory.resolve().parent != self.root.resolve()
        ):
            raise SpoolError(t("err.spool_missing"), session_id)
        return SpoolSession(directory, self.max_pending_seconds)

    def sessions(self) -> list[SpoolSession]:
        try:
            dirs = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return []
        return [SpoolSession(d, self.max_pending_seconds) for d in sorted(dirs)]

    def incomplete_sessions(self, exclude: str | None = None) -> list[SpoolSession]:
        """Sessoes que um crash deixou para tras: nao terminadas ou com fila.

        `exclude` e a sessao em andamento, que obviamente nao conta.
        """
        return [s for s in self.sessions() if s.id != exclude and not s.is_complete()]
