"""Onde esta o audio da call? Rode DURANTE uma chamada (de teste) do Teams.

    .venv\\Scripts\\python spike\\call_diag.py [--seconds 60] [--process ms-teams.exe]

Na daily de 28/10 o canal "Outros" saiu vazio: com a integracao de chamada
Bluetooth do Windows ativa, o Teams parece tocar num endpoint escondido
("Headset (AirPods ... Hands-Free)") que o loopback da saida padrao nao ve.
Este script ouve, ao mesmo tempo e uma vez por segundo:

  - o loopback de CADA saida ativa (reenumerando a cada 2 s, para pegar o
    endpoint Hands-Free se ele aparecer no meio da call);
  - o loopback por processo de cada ms-teams.exe (o audio do app, seja qual
    for a saida em que ele toca).

e mostra o nivel de cada um em dBFS. Nada e gravado em disco nem enviado.
No fim, o resumo diz qual fonte teve som.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import warnings
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).parent))
from devices import windows_default_roles  # noqa: E402
from proc_loopback import CHANNELS, ProcessLoopback  # noqa: E402

RATE = 48000
SIGNAL_DBFS = -60.0  # acima disso conta como "tem som"


class Meter:
    """Soma de quadrados desde a ultima leitura, por fonte."""

    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self.sumsq = 0.0
        self.count = 0
        self.seconds_with_signal = 0
        self.seconds_with_data = 0
        self.peak = -200.0
        self.error = ""

    def add(self, samples: np.ndarray) -> None:
        if not len(samples):
            return
        x = samples.astype(np.float64)
        with self.lock:
            self.sumsq += float(np.sum(x * x))
            self.count += len(x)

    def take(self) -> float | None:
        """dBFS do ultimo intervalo, ou None se nada chegou."""
        with self.lock:
            sumsq, count = self.sumsq, self.count
            self.sumsq, self.count = 0.0, 0
        if not count:
            return None
        db = 10 * np.log10(sumsq / count + 1e-20)
        self.seconds_with_data += 1
        self.peak = max(self.peak, db)
        if db > SIGNAL_DBFS:
            self.seconds_with_signal += 1
        return db


def endpoint_states() -> dict[str, str]:
    """Todas as saidas que o Windows conhece, com o estado (Active, NotPresent...)."""
    from pycaw.utils import AudioUtilities

    states = {}
    enum = AudioUtilities.GetDeviceEnumerator()
    coll = enum.EnumAudioEndpoints(0, 0xF)  # render, todos os estados
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i in range(coll.GetCount()):
            dev = AudioUtilities.CreateDevice(coll.Item(i))
            states[dev.FriendlyName or dev.id] = str(dev.state).split(".")[-1]
    return states


def run_speaker(sc, speaker, meter: Meter, stop: threading.Event) -> None:
    # O soundcard so inicializa o COM na thread que o importou.
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0)
    try:
        mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        with mic.recorder(samplerate=RATE, blocksize=RATE) as rec:
            while not stop.is_set():
                data = rec.record(numframes=RATE // 10)
                meter.add(np.asarray(data, dtype=np.float32).reshape(-1))
    except Exception as exc:
        meter.error = repr(exc)


def run_process(pid: int, meter: Meter, stop: threading.Event) -> None:
    try:
        with ProcessLoopback(pid) as cap:
            while not stop.is_set():
                frames = cap.read()
                meter.add(frames.astype(np.float32) / 32768.0)
                time.sleep(0.05)
    except Exception as exc:
        meter.error = repr(exc)


def root_pids(names: list[str]) -> dict[int, str]:
    """PIDs dos processos com esses nomes cujo pai nao esta entre eles.

    O loopback por processo inclui a arvore de filhos; pegar so as raizes
    evita contar o mesmo audio duas vezes.
    """
    wanted = {n.lower() for n in names}
    matched = {}
    for proc in psutil.process_iter(["pid", "name", "ppid"]):
        if (proc.info["name"] or "").lower() in wanted:
            matched[proc.info["pid"]] = (proc.info["name"], proc.info["ppid"])
    return {pid: name for pid, (name, ppid) in matched.items() if ppid not in matched}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--process", action="append", default=None,
                    help="nome do executavel (repetivel). Padrao: ms-teams.exe")
    args = ap.parse_args()
    processes = args.process or ["ms-teams.exe"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import soundcard as sc

    print("Papeis padrao do Windows:")
    for role, name in windows_default_roles().items():
        print(f"  {role:<20} {name}")
    states = endpoint_states()
    print("Saidas conhecidas:")
    for name, state in states.items():
        print(f"  [{state:<10}] {name}")
    print()

    stop = threading.Event()
    meters: list[Meter] = []
    opened_speakers: set[str] = set()
    opened_pids: set[int] = set()

    def scan() -> None:
        nonlocal states
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            speakers = sc.all_speakers()
        for spk in speakers:
            if spk.name in opened_speakers:
                continue
            opened_speakers.add(spk.name)
            meter = Meter(f"saida: {spk.name}")
            meters.append(meter)
            threading.Thread(target=run_speaker, args=(sc, spk, meter, stop), daemon=True).start()
            print(f"  + ouvindo a saida '{spk.name}'")
        for pid, name in root_pids(processes).items():
            if pid in opened_pids:
                continue
            opened_pids.add(pid)
            meter = Meter(f"processo: {name} [{pid}]")
            meters.append(meter)
            threading.Thread(target=run_process, args=(pid, meter, stop), daemon=True).start()
            print(f"  + ouvindo o processo {name} [{pid}]")
        now = endpoint_states()
        for name, state in now.items():
            if states.get(name) != state:
                print(f"  ! saida '{name}': {states.get(name, 'nova')} -> {state}")
        states = now

    def rescan_loop() -> None:
        # Fora do laco principal: listar processos no Windows chega a levar
        # varios segundos e travaria a medicao.
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0)
        while not stop.wait(2):
            try:
                scan()
            except Exception as exc:
                print(f"  ! busca falhou: {exc!r}")

    print("Procurando saidas e processos (pode levar uns 15 s)...")
    scan()
    if not any(m.name.startswith("processo") for m in meters):
        print(f"  (nenhum {', '.join(processes)} rodando: abra o Teams e a call antes)")
    print(f"\nNivel por segundo em dBFS (--- = nada chegou; > {SIGNAL_DBFS:.0f} = som)\n")
    threading.Thread(target=rescan_loop, daemon=True).start()

    start = time.monotonic()
    try:
        for second in range(1, args.seconds + 1):
            time.sleep(max(0.0, start + second - time.monotonic()))
            cells = []
            for m in list(meters):
                db = m.take()
                label = m.name.split(": ", 1)[1][:28]
                cells.append(f"{label}: {'---' if db is None else f'{db:6.1f}'}")
            print(f"{second:4d}s  " + " | ".join(cells))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

    print("\nResumo (segundos com som / segundos com dados, pico):")
    for m in meters:
        extra = f"  ERRO: {m.error}" if m.error else ""
        peak = f"{m.peak:.1f} dBFS" if m.seconds_with_data else "-"
        print(f"  {m.name:<60} {m.seconds_with_signal:4d} / {m.seconds_with_data:<4d} pico {peak}{extra}")
    print("\nCopie tudo isto e mande de volta.")


if __name__ == "__main__":
    main()
