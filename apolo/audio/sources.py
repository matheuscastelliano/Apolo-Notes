"""As duas fontes de audio da reuniao: o seu mic ("Eu") e o loopback ("Outros").

Escolhas da fase 0 (spike/RESULTADOS.md):
  - Mic: sounddevice, 16 kHz mono int16, igual ao audio.py do Artemis.
  - Loopback: soundcard, 48 kHz estereo, lido em blocos de 100 ms. Em
    silencio absoluto ele entrega zeros em vez de travar (o PyAudioWPatch
    nao entregava nada e foi descartado).

Cada fonte usa duas threads. A de captura (`capture-mic`, `capture-sys`) so
le o driver e anota a hora de chegada; a de entrega (`deliver-mic`,
`deliver-sys`) chama o callback com `(t_monotonic, float32 16 kHz mono)`.
O callback roda VAD, chunker e grava trechos em disco, e isso pode demorar
num pico de CPU; se rodasse na thread de captura, o driver estouraria o
buffer e o audio se perderia (`data discontinuity`, 1o teste real). Nunca
do callback do PortAudio, que nao pode demorar.

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
# Buffer do WASAPI no loopback. O soundcard usa o tamanho do bloco (100 ms)
# se nada for dito, e qualquer travada maior que isso perdia audio.
LOOPBACK_BUFFER_SECONDS = 1.0
# Fila de entrega maior que isso (em blocos) = o processamento nao acompanha.
BACKLOG_WARN_BLOCKS = 50


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
    devices = sd.query_devices()
    # O MME corta os nomes em 31 caracteres; o WASAPI tem o nome inteiro.
    full_names = []
    for api in sd.query_hostapis():
        if "WASAPI" in api.get("name", ""):
            full_names = [devices[i]["name"] for i in api.get("devices", [])
                          if devices[i].get("max_input_channels", 0) > 0]
    result = []
    for index, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) < 1:
            continue
        if hostapi is not None and hostapi >= 0 and dev.get("hostapi") != hostapi:
            continue
        if "Sound Mapper" in dev["name"]:
            continue  # pseudo-dispositivo do MME que so repete o padrao
        name = dev["name"]
        name = next((f for f in full_names if f.startswith(name.rstrip())), name)
        result.append({"index": index, "name": name, "default": index == default_index})
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
    # Nome editado a mao ou de outra versao do driver: aceita trecho do nome.
    for d in devices:
        if d["name"] in name or name in d["name"]:
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
        self.first_block_at: float | None = None  # time.monotonic() do 1o bloco
        self.error: AudioError | None = None
        self.device_name = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivery: threading.Thread | None = None
        self._blocks: queue.Queue[tuple[float, np.ndarray] | None] = queue.Queue()
        self._backlog_warned = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._blocks = queue.Queue()
        self._delivery = threading.Thread(
            target=self._delivery_loop, name=f"deliver-{self.channel}", daemon=True
        )
        self._delivery.start()
        self._thread = threading.Thread(
            target=self._run_safe, name=f"capture-{self.channel}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Para a captura e espera o que ja foi lido ser entregue."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        delivery, self._delivery = self._delivery, None
        if delivery is not None:
            self._blocks.put(None)
            delivery.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _deliver(self, t_arrival: float, block: np.ndarray) -> None:
        """Chamado na thread de captura: so enfileira, para voltar ao driver."""
        if self.first_block_at is None:
            self.first_block_at = t_arrival
            self.first_block.set()
        self._blocks.put((t_arrival, block))
        if not self._backlog_warned and self._blocks.qsize() > BACKLOG_WARN_BLOCKS:
            self._backlog_warned = True
            log.warning("Canal %s: processamento atrasado (%d blocos na fila)",
                        self.channel, self._blocks.qsize())

    def _delivery_loop(self) -> None:
        while True:
            item = self._blocks.get()
            if item is None:
                return
            t_arrival, block = item
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

        blocks: queue.Queue[tuple[float, bytes, str]] = queue.Queue()

        def callback(indata, frames, time_info, status) -> None:
            # Nada de log aqui: o callback do PortAudio nao pode demorar.
            blocks.put((time.monotonic(), bytes(indata), str(status) if status else ""))

        index = resolve_mic(self.device)
        try:
            wanted = index if index is not None else sd.default.device[0]
            self.device_name = next(
                d["name"] for d in _default_hostapi_inputs() if d["index"] == wanted
            )
        except Exception:
            self.device_name = self.device or ""
        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=index,
                blocksize=int(SAMPLE_RATE * BLOCK_SECONDS), callback=callback,
                # Mais buffer no driver: aguenta o callback atrasar num pico
                # de CPU. A latencia nao importa para quem so grava.
                latency="high",
            )
            stream.start()
        except sd.PortAudioError as exc:
            raise AudioError(t("err.mic_busy"), str(exc)) from exc
        except Exception as exc:
            raise AudioError(t("err.mic_open"), str(exc)) from exc

        try:
            while not self._stop.is_set():
                try:
                    t_arrival, raw, status = blocks.get(timeout=0.2)
                except queue.Empty:
                    if not stream.active:
                        raise AudioError(t("err.mic_lost"))
                    continue
                if status:
                    log.warning("mic: %s", status)  # ex.: input overflow
                pcm = np.frombuffer(raw, dtype=np.int16)
                self._deliver(t_arrival, int16_to_float(pcm))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass  # ja estamos encerrando


class SystemSource(_Source):
    """Loopback da saida padrao (ou da do config, por nome) via soundcard.

    Segue a saida padrao (risco R3 do PLANO.md): se voce conecta o fone no
    meio da reuniao e o Windows passa a tocar nele, o loopback antigo ficaria
    ouvindo a caixa muda. A cada DEFAULT_CHECK_SECONDS ele confere a saida
    padrao e, se mudou, reabre o loopback na nova. O buraco de ~0,1 s na
    troca o relogio do pipeline cobre. Com `loopback_device` fixo no config
    nao ha o que seguir.
    """

    channel = "sys"
    DEFAULT_CHECK_SECONDS = 2.0

    def __init__(self, on_block: BlockCallback, device: str | None = None):
        super().__init__(on_block, device)
        self.switches = 0  # quantas vezes trocou de saida no meio

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
        except Exception as exc:
            raise AudioError(t("err.loopback_open"), str(exc)) from exc
        while not self._stop.is_set():
            # Volta aqui so quando a saida padrao muda; o resto sai do metodo.
            self._record(sc, self._speaker(sc))

    def _record(self, sc, speaker) -> None:
        """Grava do loopback de `speaker` ate parar ou a saida padrao mudar."""
        try:
            self.device_name = speaker.name
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception as exc:
            raise AudioError(t("err.loopback_open"), str(exc)) from exc

        numframes = int(LOOPBACK_RATE * BLOCK_SECONDS)
        resampler = Resampler(LOOPBACK_RATE)
        try:
            # `blocksize` aqui e o tamanho do buffer do WASAPI; a leitura
            # continua em blocos de 100 ms (`numframes` no record).
            recorder = loopback.recorder(
                samplerate=LOOPBACK_RATE,
                blocksize=int(LOOPBACK_RATE * LOOPBACK_BUFFER_SECONDS),
            )
            recorder.__enter__()
        except Exception as exc:
            raise AudioError(t("err.loopback_open"), str(exc)) from exc
        next_check = time.monotonic() + self.DEFAULT_CHECK_SECONDS
        try:
            while not self._stop.is_set():
                try:
                    data = recorder.record(numframes=numframes)
                except Exception as exc:
                    raise AudioError(t("err.loopback_lost"), str(exc)) from exc
                t_arrival = time.monotonic()
                self._deliver(t_arrival, resampler.process(data))
                if self.device is None and t_arrival >= next_check:
                    next_check = t_arrival + self.DEFAULT_CHECK_SECONDS
                    if self._default_changed(sc, speaker):
                        return
        finally:
            try:
                recorder.__exit__(None, None, None)
            except Exception:
                pass

    def _default_changed(self, sc, current) -> bool:
        try:
            default = sc.default_speaker()
        except Exception:
            return False  # consulta falhou agora; tenta na proxima
        if default is None or default.name == current.name:
            return False
        log.info("Saida padrao mudou: '%s' -> '%s'; reabrindo o loopback.",
                 current.name, default.name)
        self.switches += 1
        return True
