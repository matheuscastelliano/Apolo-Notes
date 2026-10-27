"""Spool em disco, num diretorio temporario."""

from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from apolo.session.spool import STATE_FINISHED, Spool

SR = 16000


def pcm(seconds: float) -> np.ndarray:
    return np.full(int(seconds * SR), 500, dtype=np.int16)


@pytest.fixture
def spool(tmp_path):
    return Spool(tmp_path / "spool", max_pending_hours=2)


def test_create_session_layout(spool):
    s = spool.create_session(preset="geral")
    assert (s.dir / "session.json").is_file()
    assert (s.dir / "seg").is_dir()
    info = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert info["id"] == s.id
    assert info["state"] == "recording"
    assert info["preset"] == "geral"
    assert info["gaps"] == []


def test_two_sessions_same_second_get_distinct_ids(spool):
    a = spool.create_session()
    b = spool.create_session()
    assert a.id != b.id


def test_add_writes_wav_named_by_start(spool):
    s = spool.create_session()
    seg = s.add("mic", 12.3456, pcm(2))
    assert seg.path.name == "000012346_mic.wav"
    assert seg.start == pytest.approx(12.346)
    assert seg.duration == pytest.approx(2.0)
    with wave.open(str(seg.path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2)
        assert w.getnframes() == 2 * SR
    assert not list(s.seg_dir.glob("*.tmp"))


def test_add_rejects_unknown_channel(spool):
    s = spool.create_session()
    with pytest.raises(ValueError):
        s.add("outro", 0, pcm(1))


def test_pending_sorted_by_start_across_channels(spool):
    s = spool.create_session()
    s.add("sys", 30.0, pcm(1))
    s.add("mic", 5.0, pcm(1))
    s.add("sys", 100.0, pcm(1))
    s.add("mic", 12.0, pcm(1.5))
    pending = s.pending()
    assert [(p.channel, p.start) for p in pending] == [
        ("mic", 5.0), ("mic", 12.0), ("sys", 30.0), ("sys", 100.0)
    ]
    assert pending[1].duration == pytest.approx(1.5)
    assert pending[1].end == pytest.approx(13.5)
    assert s.pending_seconds() == pytest.approx(4.5)


def test_same_ms_does_not_overwrite(spool):
    s = spool.create_session()
    a = s.add("mic", 1.0, pcm(1))
    b = s.add("mic", 1.0, pcm(1))
    assert a.path != b.path
    assert len(s.pending()) == 2


def test_pending_ignores_tmp_and_foreign_files(spool):
    s = spool.create_session()
    s.add("mic", 1.0, pcm(1))
    (s.seg_dir / "000002000_mic.tmp").write_bytes(b"pela metade")
    (s.seg_dir / "notas.txt").write_text("x")
    assert len(s.pending()) == 1


def test_mark_done_deletes_audio(spool):
    s = spool.create_session()
    seg = s.add("mic", 1.0, pcm(1))
    s.mark_done(seg)
    assert not seg.path.exists()
    assert s.pending() == []
    s.mark_done(seg)  # de novo: nao e erro


def test_cap_drops_oldest_and_records_gap(tmp_path):
    spool = Spool(tmp_path, max_pending_hours=5 / 3600)  # teto de 5 s
    s = spool.create_session()
    s.add("mic", 0.0, pcm(2))
    s.add("sys", 3.0, pcm(2))
    s.add("mic", 6.0, pcm(2))  # 6 s pendentes > 5 s: sai o de 0 s
    assert [p.start for p in s.pending()] == [3.0, 6.0]
    assert s.gaps() == [{"start": 0.0, "end": 2.0, "channel": "mic", "reason": "spool_full"}]
    # a lacuna esta no disco, para sobreviver a um crash
    reopened = spool.open_session(s.id)
    assert reopened.gaps() == s.gaps()


def test_no_cap_when_disabled(tmp_path):
    s = Spool(tmp_path, max_pending_hours=None).create_session()
    for i in range(5):
        s.add("mic", i * 10.0, pcm(2))
    assert len(s.pending()) == 5
    assert s.gaps() == []


def test_update_and_finish(spool):
    s = spool.create_session()
    s.update(title="Daily", participants=["Ana"])
    s.finish()
    info = spool.open_session(s.id).info
    assert info["title"] == "Daily"
    assert info["state"] == STATE_FINISHED
    assert info["ended_at"]


def test_incomplete_sessions(spool):
    done = spool.create_session()
    done.finish()
    recording = spool.create_session()
    finished_with_queue = spool.create_session()
    finished_with_queue.add("mic", 1.0, pcm(1))
    finished_with_queue.finish()

    ids = [s.id for s in spool.incomplete_sessions()]
    assert ids == sorted([recording.id, finished_with_queue.id])
    assert recording.id not in [s.id for s in spool.incomplete_sessions(exclude=recording.id)]


def test_corrupt_session_json_is_still_recoverable(spool):
    s = spool.create_session()
    s.add("sys", 4.0, pcm(1))
    (s.dir / "session.json").write_text("{ quebrado", encoding="utf-8")
    [found] = spool.incomplete_sessions()
    assert found.id == s.id
    assert [p.start for p in found.pending()] == [4.0]


def test_delete_removes_everything(spool):
    s = spool.create_session()
    s.add("mic", 1.0, pcm(1))
    s.delete()
    assert not s.dir.exists()
    assert spool.sessions() == []
    s.delete()  # de novo: nao e erro


def test_open_missing_session_raises(spool):
    from apolo.errors import SpoolError

    spool.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(SpoolError):
        spool.open_session("nao-existe")
    with pytest.raises(SpoolError):
        spool.open_session("..")


def test_from_config_uses_spool_dir(tmp_path):
    spool = Spool.from_config({"spool_dir": str(tmp_path / "aqui"),
                               "max_pending_audio_hours": 1})
    assert spool.root == tmp_path / "aqui"
    assert spool.max_pending_seconds == 3600
