"""Loopback por processo no Windows (10 2004+): o audio que um app toca.

Em vez de ouvir uma saida (o loopback do endpoint), pede ao Windows o audio
que um processo e os filhos dele mandam para qualquer saida. Serve para o
caso da daily de 28/10: o Teams com a integracao de chamada Bluetooth toca
num endpoint escondido ("Hands-Free") que o loopback da saida padrao nao ve.

API: ActivateAudioInterfaceAsync("VAD\\Process_Loopback") com
AUDIOCLIENT_ACTIVATION_PARAMS, depois IAudioClient/IAudioCaptureClient
comuns. So comtypes, sem dependencia nova (o pycaw ja traz o comtypes).

    with ProcessLoopback(pid) as cap:
        frames = cap.read()   # int16 intercalado, RATE Hz, CHANNELS canais
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_void_p, wintypes

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown

RATE = 48000
CHANNELS = 2

_VIRTUAL_DEVICE = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_INCLUDE_TARGET_PROCESS_TREE = 0
_VT_BLOB = 65
_SHAREMODE_SHARED = 0
_STREAMFLAGS_LOOPBACK = 0x00020000
_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_BUFFERFLAGS_SILENT = 0x2
_BUFFER_HNS = 2_000_000  # 200 ms


class _LoopbackParams(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class _ActivationParams(Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropVariant(Structure):
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("blob", _Blob),
    ]


class _WaveFormatEx(Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                  (["out"], POINTER(ctypes.c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], ctypes.c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(ctypes.c_uint32), "pNumFrames")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], ctypes.c_int, "ShareMode"),
                  (["in"], wintypes.DWORD, "StreamFlags"),
                  (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                  (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(_WaveFormatEx), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(ctypes.c_uint32), "n")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "n")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(ctypes.c_uint32), "n")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], ctypes.c_int, "ShareMode"),
                  (["in"], POINTER(_WaveFormatEx), "pFormat"),
                  (["out"], POINTER(POINTER(_WaveFormatEx)), "ppClosest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(_WaveFormatEx)), "pp")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(ctypes.c_longlong), "a"),
                  (["out"], POINTER(ctypes.c_longlong), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "operation")),
    ]


class IAgileObject(IUnknown):
    # Sem metodos: so diz ao COM que o handler pode ser chamado de qualquer thread.
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, operation):
        self.done.set()
        return 0


_mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
_activate = _mmdevapi.ActivateAudioInterfaceAsync
_activate.restype = HRESULT
_activate.argtypes = [
    wintypes.LPCWSTR,
    POINTER(GUID),
    POINTER(_PropVariant),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]


class ProcessLoopback:
    """Captura o audio de `pid` e dos filhos dele. Use numa thread so."""

    def __init__(self, pid: int):
        self.pid = pid
        self._client = None
        self._capture = None
        self._bytes_per_frame = CHANNELS * 2

    def __enter__(self) -> "ProcessLoopback":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # thread ja inicializada (o import do comtypes faz STA na main)
        params = _ActivationParams(
            _ACTIVATION_TYPE_PROCESS_LOOPBACK,
            _LoopbackParams(self.pid, _INCLUDE_TARGET_PROCESS_TREE),
        )
        prop = _PropVariant(vt=_VT_BLOB)
        prop.blob.cbSize = ctypes.sizeof(params)
        prop.blob.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p)

        handler = _Handler()
        handler_ptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()
        iid = IAudioClient._iid_
        _activate(_VIRTUAL_DEVICE, byref(iid), byref(prop), handler_ptr, byref(op))
        if not handler.done.wait(5):
            raise TimeoutError("ActivateAudioInterfaceAsync nao respondeu")
        hr, unknown = op.GetActivateResult()
        if hr != 0:
            raise OSError(f"ativacao falhou: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        client = unknown.QueryInterface(IAudioClient)

        fmt = _WaveFormatEx(
            wFormatTag=1, nChannels=CHANNELS, nSamplesPerSec=RATE,
            nAvgBytesPerSec=RATE * self._bytes_per_frame,
            nBlockAlign=self._bytes_per_frame, wBitsPerSample=16, cbSize=0,
        )
        client.Initialize(
            _SHAREMODE_SHARED,
            _STREAMFLAGS_LOOPBACK | _STREAMFLAGS_AUTOCONVERTPCM | _STREAMFLAGS_SRC_DEFAULT_QUALITY,
            _BUFFER_HNS, 0, byref(fmt), None,
        )
        capture = client.GetService(byref(IAudioCaptureClient._iid_))
        self._capture = capture.QueryInterface(IAudioCaptureClient)
        self._client = client
        client.Start()

    def read(self) -> np.ndarray:
        """Tudo que chegou desde a ultima leitura (int16 intercalado).

        Vazio quando o processo nao esta tocando nada: o loopback por
        processo nao entrega zeros em silencio, so nao entrega.
        """
        parts = []
        while True:
            if self._capture.GetNextPacketSize() == 0:
                break
            data, frames, flags, _, _ = self._capture.GetBuffer()
            if frames:
                if flags & _BUFFERFLAGS_SILENT:
                    parts.append(np.zeros(frames * CHANNELS, dtype=np.int16))
                else:
                    raw = ctypes.string_at(data, frames * self._bytes_per_frame)
                    parts.append(np.frombuffer(raw, dtype=np.int16).copy())
            self._capture.ReleaseBuffer(frames)
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
        self._capture = None
        self._client = None


if __name__ == "__main__":
    import sys

    pid = int(sys.argv[1])
    with ProcessLoopback(pid) as cap:
        end = time.monotonic() + 5
        total = 0
        while time.monotonic() < end:
            total += len(cap.read()) // CHANNELS
            time.sleep(0.05)
    print(f"{total / RATE:.2f} s de audio do processo {pid}")
