# Fase 2 (pipeline de áudio): status da pausa

Branch `fase2-audio`, worktree `.claude/worktrees/fase2-audio` (o `.venv` dela é uma junção para o `.venv` da raiz; **não apague recursivamente**).

## Pronto (escrito, só com smoke test manual; ainda sem pytest)

- `apolo/audio/resample.py`: `to_mono16k` (de uma vez), `Resampler` (streaming com `soxr.ResampleStream`), conversões int16/float.
- `apolo/audio/vad.py`: `Vad`, um wrapper do `pysilero-vad` com janelas de 512 amostras, sobra entre blocos e histerese de 0,5 para entrar e 0,35 para sair. `process(pcm_int16) -> [(janela, e_fala)]`.
- `apolo/audio/chunker.py`: `Chunker` (puro) e `Chunk` (canal, start, end, speech, pcm int16, `level_dbfs()`). Regras: alvo 12 s mais pausa de 0,3 s, pausa longa de 1,5 s, máximo de 30 s, fala mínima de 0,4 s, margem de 0,2 s, mínimo de 1 s completado com o áudio real seguinte. `resync(t)` fecha o trecho aberto quando o relógio salta. `from_config` lê `chunk_*`.
- `apolo/audio/pipeline.py`: `ChannelPipeline` (novo, faz a cola entre a fonte, o VAD e o chunker). Usa o relógio monotônico e faz o resync quando a deriva passa de 0,5 s.
- `apolo/audio/sources.py`: `MicSource` (sounddevice, que entrega pela thread `capture-mic` via fila), `SystemSource` (loopback do soundcard em 48 kHz, blocos de 100 ms, com resample), `list_devices()`, `resolve_mic()`, evento `first_block` e `error`. Os avisos `SoundcardRuntimeWarning` vão para o log. **Ainda não rodou com hardware.**
- `apolo/session/spool.py`: `Spool` e `SpoolSession`, com o layout `<sessão>/session.json` + `seg/<inicio_ms:09d>_<mic|sys>.wav`. Inclui `add`, `pending` (ordenado), `mark_done` (apaga o WAV), teto de horas pendentes (descarta o mais antigo e registra uma lacuna `spool_full`), `incomplete_sessions(exclude)`, `delete`, `update` e `finish`.
- `apolo/errors.py`: `SpoolError` adicionado no fim.

## Pela metade

- **`apolo/config.py`**: faltam as chaves novas em `DEFAULT_CONFIG`, ao lado de `chunk_*`: `chunk_short_pause_seconds: 0.3`, `chunk_long_pause_seconds: 1.5`, `chunk_min_speech_seconds: 0.4`, `chunk_pad_seconds: 0.2`. O chunker já lê essas chaves com os mesmos valores padrão, então funciona sem elas.
- **`apolo/i18n.py`**: faltam as chaves usadas no código, que hoje caem para o próprio nome da chave: `err.vad_load`, `err.mic_list`, `err.mic_open`, `err.mic_busy`, `err.mic_lost`, `err.loopback_list`, `err.loopback_open`, `err.loopback_lost`, `err.audio_unexpected`, `err.spool_write`, `err.spool_delete`, `err.spool_missing`, mais as da CLI. Elas vão **no fim** dos blocos `en` e `pt`.

## Próximos passos (em ordem)

1. Chaves no `config.py` e no `i18n.py` (acima).
2. CLI: criar `apolo/audio/cli.py` com `cmd_devices`, `cmd_test_capture(seconds, keep_spool)` e `cmd_from_wav(path)`, e em `apolo/__main__.py` adicionar `--devices`, `--test-capture [s]` (nargs='?', const=20), `--from-wav arquivo` e `--keep-spool`, despachando antes de `claim_single_instance`.
   - `--from-wav`: ler o WAV com `wave`, passar por `to_mono16k`, alimentar o `ChannelPipeline("mic", config, t0=0)` em blocos de 100 ms com `t_arrival = fim_do_bloco_em_s` e imprimir os trechos (início, duração, fala, nível).
   - `--test-capture`: o mesmo `t0 = monotonic()` para os dois canais, `MicSource` e `SystemSource` com o callback que alimenta o pipeline do canal e grava em `Spool.create_session()`. Imprimir "Preparando..." até os dois `first_block` (timeout de ~5 s) e depois "Gravando". Imprimir cada trecho quando é cortado. No fim, dar `close()` nos dois, listar o pendente e apagar a sessão (a não ser com `--keep-spool`).
3. Testes em `tests/`: `test_chunker.py` (fala e silêncio sintéticos com janelas de 512: corte em pausa longa, corte na 1ª pausa depois do alvo, corte forçado em 30 s, descarte de fala < 0,4 s, margem de 0,2 s, mínimo de 1 s, resync sem sobreposição) e `test_spool.py` (com `tmp_path`: add/pending ordenado, mark_done, teto com lacuna, incomplete_sessions, delete, session.json corrompido). Rodar com `.venv\Scripts\python -m pytest`.
4. Validar: `spike\make_sample.py` gera `spike\out\sample.wav` (dá para copiar o da raiz); `--from-wav spike\out\sample.wav` deve dar ~4 trechos de 6–13 s, como no spike. Depois, `--test-capture 20` com mic e loopback.
5. Commit final (sem merge na `main`) e remover este arquivo.
