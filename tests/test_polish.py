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
