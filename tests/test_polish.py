"""Fase 5: pausar meu mic, travas de silencio e de duracao, loopback que segue
a saida padrao."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from apolo.audio import sources as sources_module
from apolo.session import session as session_module
from apolo.session.session import RECORDING, MeetingSession
from test_session import PRESET, FakePipeline, FakeSource, FakeTranscriber, speech

pytestmark = pytest.mark.usefixtures("portuguese")


class SlowSource(FakeSource):
    """Entrega um bloco por vez, so quando o teste manda (`feed`)."""

    def __init__(self, channel, on_block):
        super().__init__(channel, on_block, [])
        self._queue = []
        self._n = 0
        self._cv = threading.Condition()

    def feed(self, block):
        with self._cv:
            self._queue.append(block)
            self._cv.notify()

    def start(self):
        t_start = time.monotonic()

        def run():
            self.first_block.set()
            while not self._stop.is_set():
                with self._cv:
                    self._cv.wait(0.05)
                    blocks, self._queue = self._queue, []
                for block in blocks:
                    self._n += len(block)
                    self.on_block(t_start + self._n / 16000, block)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()


def live_session(config, monkeypatch):
    monkeypatch.setattr(session_module, "ChannelPipeline", FakePipeline)
    sources = {}

    def factory(channel, on_block, cfg):
        sources[channel] = SlowSource(channel, on_block)
        return sources[channel]

    s = MeetingSession(config, PRESET, FakeTranscriber(), on_status=lambda st: None,
                       source_factory=factory)
    s.start()
    assert s.phase == RECORDING
    return s, sources


def wait_for(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_paused_mic_is_not_transcribed(config, monkeypatch):
    s, sources = live_session(config, monkeypatch)
    s.set_mic_paused(True)
    sources["mic"].feed(speech())
    sources["sys"].feed(speech())
    assert wait_for(lambda: s.queue.transcribed == 1 and s.queue.pending_count() == 0)
    s.set_mic_paused(False)
    sources["mic"].feed(speech())
    assert wait_for(lambda: s.queue.transcribed == 2)
    s.stop()
    speakers = [seg.speaker for seg in s.store.segments()]
    assert speakers == ["others", "me"]  # a fala com o mic pausado sumiu


def test_silence_alert_and_duration_limit(config, monkeypatch):
    config["silence_alert_minutes"] = 10
    config["max_session_hours"] = 3
    s, sources = live_session(config, monkeypatch)
    t0 = s.started_at
    s.check_limits(now=t0 + 9 * 60)
    assert s.silent_minutes == 0
    s.check_limits(now=t0 + 12 * 60)
    assert s.silent_minutes == 12
    # Alguem falou: o alerta zera.
    sources["sys"].feed(speech())
    assert wait_for(lambda: s._last_speech is not None)
    s.check_limits(now=s._last_speech + 60)
    assert s.silent_minutes == 0
    assert not s.limit_reached
    s.check_limits(now=t0 + 3 * 3600)
    assert s.limit_reached
    s.stop()


def test_limits_off_with_zero(config, monkeypatch):
    config["silence_alert_minutes"] = 0
    config["max_session_hours"] = 0
    s, _ = live_session(config, monkeypatch)
    s.check_limits(now=s.started_at + 10 * 3600)
    assert s.silent_minutes == 0 and not s.limit_reached
    s.stop()


# ------------------------------------------- idioma dos presets padrao


def test_default_presets_follow_the_language_unless_edited():
    from apolo import config as config_module
    from apolo.presets import SummaryPreset

    english = [SummaryPreset.from_dict(d) for d in config_module.default_presets("en")]
    english[1].system_prompt += "\nAlso list the risks."  # o usuario editou o daily
    english.append(SummaryPreset(id="meu", name="Meu tipo", system_prompt="x"))

    out = config_module.retranslate_presets(english, "en", "pt")
    by_id = {p.id: p for p in out}
    assert by_id["geral"].name == "Geral" and "Resumo" in by_id["geral"].system_prompt
    assert by_id["cliente"].name == "Cliente"
    assert by_id["daily"].system_prompt.endswith("Also list the risks.")  # intocado
    assert by_id["meu"].name == "Meu tipo"
    assert [p.id for p in out] == ["geral", "daily", "cliente", "1on1", "meu"]


# ------------------------------------------------- loopback segue a saida


class FakeSpeaker:
    def __init__(self, name):
        self.name = name


class FakeRecorder:
    def __init__(self, sc, name):
        self.sc, self.name = sc, name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        time.sleep(0.005)
        self.sc.recorded.append(self.name)
        return np.zeros((numframes, 2), dtype=np.float32)


class FakeLoopback:
    def __init__(self, sc, name):
        self.sc, self.name = sc, name

    def recorder(self, samplerate, blocksize):
        return FakeRecorder(self.sc, self.name)


class FakeSoundcard:
    def __init__(self):
        self.default = FakeSpeaker("Caixa do notebook")
        self.recorded = []

    def default_speaker(self):
        return self.default

    def all_speakers(self):
        return [self.default]

    def get_microphone(self, id, include_loopback=False):
        return FakeLoopback(self, id)


def test_loopback_follows_the_default_output(monkeypatch):
    sc = FakeSoundcard()
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    monkeypatch.setattr(sources_module.SystemSource, "DEFAULT_CHECK_SECONDS", 0.0)
    blocks = []
    src = sources_module.SystemSource(lambda t, b: blocks.append(b))
    src.start()
    assert wait_for(lambda: len(sc.recorded) > 3)
    sc.default = FakeSpeaker("Fone Bluetooth")  # conectou o fone no meio
    assert wait_for(lambda: src.device_name == "Fone Bluetooth")
    src.stop()
    assert src.switches == 1
    assert src.error is None
    assert sc.recorded[-1] == "Fone Bluetooth"


def test_fixed_loopback_device_does_not_follow(monkeypatch):
    sc = FakeSoundcard()
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    monkeypatch.setattr(sources_module.SystemSource, "DEFAULT_CHECK_SECONDS", 0.0)
    src = sources_module.SystemSource(lambda t, b: None, device="Caixa do notebook")
    src.start()
    assert wait_for(lambda: len(sc.recorded) > 3)
    sc.default = FakeSpeaker("Fone Bluetooth")
    time.sleep(0.1)
    src.stop()
    assert src.switches == 0 and src.device_name == "Caixa do notebook"


def test_slow_processing_does_not_block_the_capture(monkeypatch):
    # O callback (VAD, chunker, disco) trava 0,3 s: a leitura do driver segue
    # e nada se perde - os blocos esperam na fila e chegam todos.
    sc = FakeSoundcard()
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    delivered = []
    gate = threading.Event()

    def on_block(t, block):
        gate.wait(0.3)
        delivered.append(block)

    src = sources_module.SystemSource(on_block)
    src.start()
    assert wait_for(lambda: len(sc.recorded) > 20)  # leu ~20 blocos durante a 1a trava
    gate.set()
    src.stop()
    assert len(delivered) == len(sc.recorded)


def test_loopback_asks_for_a_one_second_driver_buffer(monkeypatch):
    sc = FakeSoundcard()
    sizes = []
    original = FakeLoopback.recorder

    def recorder(self, samplerate, blocksize):
        sizes.append(blocksize)
        return original(self, samplerate, blocksize)

    monkeypatch.setattr(FakeLoopback, "recorder", recorder)
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    src = sources_module.SystemSource(lambda t, b: None)
    src.start()
    assert wait_for(lambda: sc.recorded)
    src.stop()
    assert sizes == [48000]


# ------------------------------- loopback de todas as saidas (daily de 28/10)


class LevelRecorder(FakeRecorder):
    """Cada saida toca um nivel constante, para dar para ver a soma."""

    def record(self, numframes):
        time.sleep(0.005)
        self.sc.recorded.append(self.name)
        return np.full((numframes, 2), self.sc.levels.get(self.name, 0.0), dtype=np.float32)


class LevelLoopback(FakeLoopback):
    def recorder(self, samplerate, blocksize):
        return LevelRecorder(self.sc, self.name)


class TwoOutputs(FakeSoundcard):
    def __init__(self):
        super().__init__()
        self.extra = [FakeSpeaker("Headset (Hands-Free)")]
        self.levels = {"Caixa do notebook": 0.0, "Headset (Hands-Free)": 0.25}

    def all_speakers(self):
        return [self.default, *self.extra]

    def get_microphone(self, id, include_loopback=False):
        return LevelLoopback(self, id)


def test_loopback_mixes_the_other_active_outputs(monkeypatch):
    # A saida padrao esta muda; a call toca numa saida que nao e a padrao.
    sc = TwoOutputs()
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    monkeypatch.setattr(sources_module.SystemSource, "DEFAULT_CHECK_SECONDS", 0.0)
    blocks = []
    src = sources_module.SystemSource(lambda t, b: blocks.append(b))
    src.start()
    assert wait_for(lambda: len(blocks) > 30 and blocks[-1].max() > 0.2)
    assert src.extra_names == ["Headset (Hands-Free)"]
    assert src.device_name == "Caixa do notebook"

    # A saida extra sumiu (a call acabou): para de ouvir e o canal volta a zero.
    sc.extra = []
    assert wait_for(lambda: not src.extra_names)
    n = len(blocks)
    assert wait_for(lambda: len(blocks) > n + 30)
    assert not np.any(blocks[-1])
    src.stop()
    assert src.error is None


def test_fixed_loopback_device_does_not_mix(monkeypatch):
    sc = TwoOutputs()
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    monkeypatch.setattr(sources_module.SystemSource, "DEFAULT_CHECK_SECONDS", 0.0)
    blocks = []
    src = sources_module.SystemSource(lambda t, b: blocks.append(b), device="Caixa do notebook")
    src.start()
    assert wait_for(lambda: len(blocks) > 30)
    src.stop()
    assert src.extra_names == [] and not any(np.any(b) for b in blocks)


def test_failed_extra_output_is_not_retried_in_a_loop(monkeypatch):
    sc = TwoOutputs()
    opened = []

    def get_microphone(id, include_loopback=False):
        opened.append(id)
        if id == "Headset (Hands-Free)":
            raise RuntimeError("Error 0x88890004")
        return LevelLoopback(sc, id)

    monkeypatch.setattr(sc, "get_microphone", get_microphone)
    monkeypatch.setattr(sources_module, "_soundcard", lambda: sc)
    monkeypatch.setattr(sources_module.SystemSource, "DEFAULT_CHECK_SECONDS", 0.0)
    src = sources_module.SystemSource(lambda t, b: None)
    src.start()
    assert wait_for(lambda: len(sc.recorded) > 50)
    src.stop()
    assert src.error is None  # a saida padrao seguiu gravando
    assert opened.count("Headset (Hands-Free)") == 1


def test_others_silent_alert(config, monkeypatch):
    s, sources = live_session(config, monkeypatch)
    t0 = s.started_at
    # So voce falou, e bastante: depois de 2 min sem ninguem, avisa.
    sources["mic"].feed(speech(25.0))
    assert wait_for(lambda: s._my_speech_since_others >= 25.0)
    s.check_limits(now=t0 + 60)
    assert not s.others_silent
    s.check_limits(now=t0 + 130)
    assert s.others_silent
    # Os outros apareceram: o aviso some.
    sources["sys"].feed(speech())
    assert wait_for(lambda: s._last_others_speech is not None)
    s.check_limits(now=s._last_others_speech + 1)
    assert not s.others_silent
    s.stop()


def test_others_silent_needs_my_speech(config, monkeypatch):
    # Reuniao que ainda nao comecou (ninguem fala): nao e problema de saida.
    s, _ = live_session(config, monkeypatch)
    s.check_limits(now=s.started_at + 600)
    assert not s.others_silent
    s.stop()
