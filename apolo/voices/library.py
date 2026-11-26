r"""A biblioteca de vozes: quem voce ja identificou, com amostras curtas.

    %APPDATA%\Apolo\voices\voices.json
    %APPDATA%\Apolo\voices\<id>\<n>.wav     3 a 8 s, 16 kHz mono

Fica so no seu computador. As amostras saem dele apenas como referencia nas
chamadas que separam as vozes, para a OpenAI dizer qual voz e de quem.
Amostra de voz e dado biometrico (LGPD): a aba Vozes das configuracoes
mostra tudo o que esta guardado e apaga quando voce quiser.

Cada pessoa guarda ate MAX_SAMPLES amostras, de reunioes diferentes; a mais
nova vai na frente e a mais velha sai.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .. import config as config_module
from ..session.spool import SAMPLE_RATE, wav_bytes

log = logging.getLogger(__name__)

LIBRARY_FILE = "voices.json"
MAX_SAMPLES = 3


@dataclass
class Person:
    id: str
    name: str
    samples: list[str] = field(default_factory=list)  # nomes de arquivo, mais nova 1o
    updated: str = ""


def default_root() -> Path:
    return config_module.config_dir() / "voices"


def _slug(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "pessoa"


def same_name(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


class VoiceLibrary:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()
        self._lock = threading.Lock()
        self._people: dict[str, Person] = {}
        self._load()

    # ------------------------------------------------------------ arquivo

    @property
    def _file(self) -> Path:
        return self.root / LIBRARY_FILE

    def _load(self) -> None:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Biblioteca de vozes ilegivel (%s); comecando vazia.", exc)
            return
        for item in data.get("people", []):
            try:
                person = Person(str(item["id"]), str(item["name"]),
                                [str(s) for s in item.get("samples", [])],
                                str(item.get("updated", "")))
            except (KeyError, TypeError):
                continue
            # Amostra apagada a mao some da lista.
            person.samples = [s for s in person.samples if (self.root / person.id / s).exists()]
            self._people[person.id] = person

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"people": [vars(p) for p in self._people.values()]}
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._file)

    # ------------------------------------------------------------ leitura

    def people(self) -> list[Person]:
        with self._lock:
            return sorted(self._people.values(), key=lambda p: p.name.casefold())

    def names(self) -> list[str]:
        return [p.name for p in self.people()]

    def find(self, name: str) -> Person | None:
        with self._lock:
            return next((p for p in self._people.values() if same_name(p.name, name)), None)

    def sample_paths(self, person: Person) -> list[Path]:
        return [self.root / person.id / s for s in person.samples]

    def reference(self, name: str) -> bytes | None:
        """A amostra mais nova da pessoa, em WAV, para usar como referencia."""
        person = self.find(name)
        if person is None:
            return None
        for path in self.sample_paths(person):
            try:
                return path.read_bytes()
            except OSError:
                continue
        return None

    # ------------------------------------------------------------- escrita

    def add_sample(self, name: str, pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Person:
        """Guarda uma amostra nova (int16, 16 kHz). Cria a pessoa se precisar."""
        name = name.strip()
        if not name:
            raise ValueError("nome vazio")
        with self._lock:
            person = next((p for p in self._people.values() if same_name(p.name, name)), None)
            if person is None:
                base = _slug(name)
                pid, n = base, 2
                while pid in self._people:
                    pid, n = f"{base}-{n}", n + 1
                person = Person(pid, name)
                self._people[pid] = person
            folder = self.root / person.id
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename, n = f"{stamp}.wav", 2
            while (folder / filename).exists():
                filename, n = f"{stamp}-{n}.wav", n + 1
            (folder / filename).write_bytes(wav_bytes(pcm, sample_rate))
            person.samples.insert(0, filename)
            for old in person.samples[MAX_SAMPLES:]:
                (folder / old).unlink(missing_ok=True)
            person.samples = person.samples[:MAX_SAMPLES]
            person.updated = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save()
            return person

    def rename(self, person_id: str, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("nome vazio")
        with self._lock:
            person = self._people[person_id]
            other = next((p for p in self._people.values()
                          if p.id != person_id and same_name(p.name, new_name)), None)
            if other is not None:
                # Mesmo nome de alguem que ja existe: junta as amostras.
                for sample in person.samples:
                    shutil.move(str(self.root / person.id / sample),
                                str(self.root / other.id / sample))
                combined = person.samples + other.samples
                for extra in combined[MAX_SAMPLES:]:
                    (self.root / other.id / extra).unlink(missing_ok=True)
                other.samples = combined[:MAX_SAMPLES]
                shutil.rmtree(self.root / person.id, ignore_errors=True)
                del self._people[person_id]
            else:
                person.name = new_name
            self._save()

    def delete_sample(self, person_id: str, sample: str) -> None:
        """Apaga uma amostra so (a de alguem que ganhou o nome errado).
        Sem amostra nenhuma, a pessoa sai da biblioteca."""
        with self._lock:
            person = self._people.get(person_id)
            if person is None or sample not in person.samples:
                return
            (self.root / person.id / sample).unlink(missing_ok=True)
            person.samples.remove(sample)
            if not person.samples:
                del self._people[person_id]
                shutil.rmtree(self.root / person_id, ignore_errors=True)
            self._save()

    def delete(self, person_id: str) -> None:
        with self._lock:
            self._people.pop(person_id, None)
            shutil.rmtree(self.root / person_id, ignore_errors=True)
            self._save()

    def delete_all(self) -> None:
        with self._lock:
            self._people = {}
            shutil.rmtree(self.root, ignore_errors=True)
