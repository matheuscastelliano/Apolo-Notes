"""Fase 0: o que cada lib enxerga como entrada, saida e loopback.

    .venv\\Scripts\\python spike\\devices.py

A pergunta principal: a saida "padrao" e a saida "de comunicacao" sao o mesmo
dispositivo? O Teams costuma tocar na de comunicacao; se forem diferentes, o
loopback da saida padrao ouve o lugar errado.
"""

from __future__ import annotations

import sys
import warnings


def windows_default_roles() -> dict[str, str]:
    """Nome do dispositivo padrao por papel (console x communications)."""
    from comtypes import CLSCTX_ALL  # noqa: F401  (pycaw depende de comtypes)
    from pycaw.utils import AudioUtilities

    enumerator = AudioUtilities.GetDeviceEnumerator()
    roles = {}
    # EDataFlow: 0 = render (saida), 1 = capture (entrada)
    # ERole: 0 = console, 2 = communications
    for flow, flow_name in ((0, "saida"), (1, "entrada")):
        for role, role_name in ((0, "padrao"), (2, "comunicacao")):
            try:
                dev = enumerator.GetDefaultAudioEndpoint(flow, role)
                roles[f"{flow_name} {role_name}"] = AudioUtilities.CreateDevice(
                    dev
                ).FriendlyName
            except Exception as exc:
                roles[f"{flow_name} {role_name}"] = f"<erro: {exc}>"
    return roles


def main() -> None:
    print("=" * 70)
    print("Papeis padrao do Windows (pycaw)")
    print("=" * 70)
    if sys.platform == "win32":
        roles = windows_default_roles()
        for k, v in roles.items():
            print(f"  {k:<20} {v}")
        same = roles.get("saida padrao") == roles.get("saida comunicacao")
        print(f"\n  saida padrao == saida comunicacao? {'SIM' if same else 'NAO  <-- atencao'}")

    print("\n" + "=" * 70)
    print("soundcard")
    print("=" * 70)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import soundcard as sc

        print(f"  speaker padrao: {sc.default_speaker().name}")
        print(f"  mic padrao:     {sc.default_microphone().name}")
        print("  saidas:")
        for s in sc.all_speakers():
            print(f"    - {s.name}  [{s.channels} ch]")
        print("  entradas (inclui loopback):")
        for m in sc.all_microphones(include_loopback=True):
            tag = "LOOPBACK" if m.isloopback else "mic"
            print(f"    - [{tag:<8}] {m.name}  [{m.channels} ch]")

    print("\n" + "=" * 70)
    print("PyAudioWPatch (WASAPI)")
    print("=" * 70)
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        print(f"  saida padrao WASAPI: {p.get_device_info_by_index(wasapi['defaultOutputDevice'])['name']}")
        try:
            lb = p.get_default_wasapi_loopback()
            print(f"  loopback padrao:     {lb['name']}  [{lb['maxInputChannels']} ch, {int(lb['defaultSampleRate'])} Hz]")
        except Exception as exc:
            print(f"  loopback padrao:     <erro: {exc}>")
        print("  loopbacks:")
        for lb in p.get_loopback_device_info_generator():
            print(f"    - {lb['name']}  [{lb['maxInputChannels']} ch, {int(lb['defaultSampleRate'])} Hz]")
    finally:
        p.terminate()

    print("\n" + "=" * 70)
    print("sounddevice (mic, como no Artemis)")
    print("=" * 70)
    import sounddevice as sd

    default_in = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        api = sd.query_hostapis(d["hostapi"])["name"]
        mark = "*" if i == default_in else " "
        print(f"  {mark} [{i:>2}] {d['name']}  ({api}, {int(d['default_samplerate'])} Hz)")


if __name__ == "__main__":
    main()
