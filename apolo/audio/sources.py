"""As duas fontes de audio da reuniao: o seu mic ("Eu") e o loopback ("Outros").

Escolhas da fase 0 (spike/RESULTADOS.md):
  - Mic: sounddevice, 16 kHz mono int16, igual ao audio.py do Artemis.
  - Loopback: soundcard, 48 kHz estereo, lido em blocos de 100 ms. Em
    silencio absoluto ele entrega zeros em vez de travar (o PyAudioWPatch
    nao entregava nada e foi descartado).

Cada fonte roda na sua thread (`capture-mic`, `capture-sys`) e entrega
blocos `(t_monotonic, float32 16 kHz mono)` pelo callback, sempre a partir
dessa thread - nunca do callback do PortAudio, que nao pode demorar.

`first_block` e setado quando o 1o bloco chega. O mic do AirPods leva ~1 a
1,4 s para comecar a entregar (troca do Bluetooth para HFP); a UI so deve
dizer "Gravando" depois que as duas fontes setarem esse evento.

Regra do app: nada aqui levanta excecao para fora da thread. Um erro vira
`source.error` (AudioError com mensagem amigavel) e a thread termina.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import warnings
from typing import Callable

import numpy as np

from ..errors import AudioError
from ..i18n import t
from .resample import SAMPLE_RATE, Resampler, int16_to_float

log = logging.getLogger(__name__)

BlockCallback = Callable[[float, np.ndarray], None]

BLOCK_SECONDS = 0.1
LOOPBACK_RATE = 48000


# ------------------------------------------------------------ dispositivos


def _default_hostapi_inputs() -> list[dict]:
    """Entradas do host API padrao do sounddevice (MME no Windows).

    O sounddevice lista o mesmo mic uma vez por host API (MME, DirectSound,
    WASAPI...). Ficamos com o padrao, que e o que o Artemis usa, para que o
    nome salvo no config aponte sempre para o mesmo lugar.
    """
    import sounddevice as sd

    hostapi = sd.default.hostapi
    default_index = sd.default.device[0]
    result = []
    for index, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) < 1:
            continue
        if hostapi is not None and hostapi >= 0 and dev.get("hostapi") != hostapi:
            continue
        result.append({"index": index, "name": dev["name"], "default": index == default_index})
    return result


def list_devices() -> dict[str, list[dict]]:
    """{"inputs": [...], "outputs": [...]}, cada item {name, default}.

    Por nome, nao por indice: indices mudam quando voce pluga hardware.
    `outputs` sao as saidas cujo loopback pode virar o canal "Outros".
    """
    try:
        inputs = [{"name": d["name"], "default": d["default"]}
                  for d in _default_hostapi_inputs()]
    except Exception as exc:  # PortAudio pode falhar de varias formas
        raise AudioError(t("err.mic_list"), str(exc)) from exc
    try:
        sc = _soundcard()
        default = sc.default_speaker()
        default_name = default.name if default is not None else None
        outputs = [{"name": s.name, "default": s.name == default_name}
                   for s in sc.all_speakers()]
    except Exception as exc:
        raise AudioError(t("err.loopback_list"), str(exc)) from exc
    return {"inputs": inputs, "outputs": outputs}


def resolve_mic(name: str | None) -> int | None:
    """Nome salvo no config -> indice do sounddevice. None = padrao."""
    if not name:
        return None
    devices = _default_hostapi_inputs()
    for d in devices:
        if d["name"] == name:
            return d["index"]
    # O MME corta nomes em 31 caracteres; aceita prefixo/trecho do nome.
    for d in devices:
        if name.startswith(d["name"]) or d["name"] in name or name in d["name"]:
            return d["index"]
    log.warning("Mic '%s' nao encontrado; usando o padrao.", name)
    return None


# ------------------------------------------------------- avisos do soundcard


_warnings_hooked = False
_warnings_lock = threading.Lock()


def _soundcard():
    """Importa o soundcard com os avisos dele indo para o log."""
    global _warnings_hooked
    with _warnings_lock:
        if not _warnings_hooked:
            _warnings_hooked = True
            original = warnings.showwarning

            def showwarning(message, category, filename, lineno, file=None, line=None):
                # `data discontinuity in recording`: o WASAPI perdeu um
                # pedaco (tipico na troca de perfil do Bluetooth). Vai para o
                # log e a gravacao segue; o relogio da sessao cobre o buraco.
                if category.__name__ == "SoundcardRuntimeWarning":
                    log.warning("soundcard: %s", message)
                    return
                original(message, category, filename, lineno, file, line)

            warnings.showwarning = showwarning
    import soundcard

    return soundcard


# ------------------------------------------------------------------ fontes


class _Source:
    channel = ""

    def __init__(self, on_block: BlockCallback, device: str | None = None):
        self.on_block = on_block
        self.device = device
        self.first_block = threading.Event()
        self.error: AudioError | None = None
        self.device_name = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_safe, name=f"capture-{self.channel}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _deliver(self, t_arrival: float, block: np.ndarray) -> None:
        self.first_block.set()
        try:
            self.on_block(t_arrival, block)
        except Exception:
            log.exception("Erro processando bloco do canal %s", self.channel)

    def _run_safe(self) -> None:
        try:
            self._run()
        except AudioError as exc:
            self.error = exc
            log.error("Canal %s: %s", self.channel, exc)
        except Exception as exc:
            self.error = AudioError(t("err.audio_unexpected"), repr(exc))
            log.exception("Canal %s parou", self.channel)

    def _run(self) -> None:
        raise NotImplementedError


class MicSource(_Source):
    """Microfone padrao (ou o do config, por nome) via sounddevice."""

    channel = "mic"

    def _run(self) -> None:
        import sounddevice as sd

        blocks: queue.Queue[tuple[float, bytes]] = queue.Queue()

        def callback(indata, frames, time_info, status) -> None:
            if status:
                log.debug("mic: %s", status)
            blocks.put((time.monotonic(), bytes(indata)))

        index = resolve_mic(self.device)
        try:
            info = sd.query_devices(index if index is not None else sd.default.device[0])
            self.device_name = info["name"]
        except Exception:
            self.device_name = self.device or ""
        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=index,
                blocksize=int(SAMPLE_RATE * BLOCK_SECONDS), callback=callback,
            )
            stream.start()
        except sd.PortAudioError as exc:
            raise AudioError(t("err.mic_busy"), str(exc)) from exc
        except Exception as exc:
            raise AudioError(t("err.mic_open"), str(exc)) from exc

        try:
            while not self._stop.is_set():
                try:
                    t_arrival, raw = blocks.get(timeout=0.2)
                except queue.Empty:
                    if not stream.active:
                        raise AudioError(t("err.mic_lost"))
                    continue
                pcm = np.frombuffer(raw, dtype=np.int16)
                self._deliver(t_arrival, int16_to_float(pcm))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass  # ja estamos encerrando


class SystemSource(_Source):
    """Loopback da saida padrao (ou da do config, por nome) via soundcard."""

    channel = "sys"

    def _speaker(self, sc):
        if self.device:
            for s in sc.all_speakers():
                if s.name == self.device:
                    return s
            log.warning("Saida '%s' nao encontrada; usando a padrao.", self.device)
        speaker = sc.default_speaker()
        if speaker is None:
            raise AudioError(t("err.loopback_open"), "sem saida padrao")
        return speaker

    def _run(self) -> None:
        try:
            sc = _soundcard()
            speaker = self._speaker(sc)
            self.device_name = speaker.name
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except AudioError:
            raise
        except Exception as exc:
            raise AudioError(t("err.loopback_open"), str(exc)) from exc

        numframes = int(LOOPBACK_RATE * BLOCK_SECONDS)
        resampler = Resampler(LOOPBACK_RATE)
        try:
            recorder = loopback.recorder(samplerate=LOOPBACK_RATE, blocksize=numframes)
            recorder.__enter__()
        except Exception as exc:
            raise AudioError(t("err.loopback_open"), str(exc)) from exc
        try:
            while not self._stop.is_set():
                try:
                    data = recorder.record(numframes=numframes)
                except Exception as exc:
                    raise AudioError(t("err.loopback_lost"), str(exc)) from exc
                t_arrival = time.monotonic()
                self._deliver(t_arrival, resampler.process(data))
        finally:
            try:
                recorder.__exit__(None, None, None)
            except Exception:
                pass
