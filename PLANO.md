# Apolo: plano do protótipo

> Gêmeo do Artemis Dictation: o Artemis dita, o Apolo registra.
> Notas de reunião geradas localmente, sem bot na chamada. Você liga, ele ouve
> o seu microfone e o áudio do sistema, transcreve em trechos e, ao parar,
> escreve uma nota em Markdown com resumo e transcrição.

---

## 1. Princípios

1. **Nada entra na chamada.** Só captura local: microfone + loopback do sistema.
2. **Manual.** Liga e desliga pela bandeja (atalho opcional). Sem detecção automática no protótipo.
3. **O áudio é descartável.** Fica em disco só enquanto é processado e é apagado quando a nota fica pronta. Guardar é opção explícita.
4. **Nada derruba o app** (regra herdada do Artemis). Erro vira aviso no overlay, e a sessão se recupera ou termina de forma limpa.
5. **O provider é trocável.** Só um módulo sabe que é a OpenAI. A interface é em lote (trecho → texto), que encaixa em faster-whisper e Ollama no futuro.
6. **Mesma cara do Artemis**: bandeja (pystray), Tkinter, overlay, presets em JSON, keyring, i18n, iniciar com o sistema.

## 2. Decisões fechadas

| Tema | Decisão |
|---|---|
| Nome | **Apolo** (pacote `apolo`) |
| Relação com o Artemis | App separado. Os módulos são copiados e adaptados, sem lib compartilhada |
| Plataforma | Windows primeiro. Linux numa fase própria no fim |
| Fontes | Mic = "Eu". Loopback do sistema = "Outros". Sem diarização |
| Chunking | Híbrido guiado por VAD, por canal: corta na 1ª pausa depois de ~12 s, força o corte em 30 s, descarta trecho sem fala |
| STT | `gpt-transcribe` com `languages=["pt","en"]`, `prompt` (final do trecho anterior) e `keywords` (glossário) |
| LLM | `gpt-5.6-luna` (configurável por preset) |
| Idioma da saída | O idioma falado na reunião, mantendo os termos em inglês como foram ditos |
| Áudio em disco | Spool temporário (pasta configurável) com teto de 2 h de áudio pendente, apagado no fim. Opção "salvar áudio" (FLAC por canal), desligada por padrão |
| Internet caiu | Os trechos esperam no spool com retry e backoff. Passou do teto: descarta o mais antigo e marca a lacuna na nota |
| Crash ou desligamento | Ao abrir de novo, oferece retomar a sessão incompleta |
| Nota | Uma `.md` por reunião, numa pasta escolhida nas configurações. Frontmatter + resumo + "Minhas anotações" + transcrição recolhida |
| Ao vivo | A nota é escrita durante a reunião: a transcrição vai crescendo dentro do arquivo |
| Preset | Escolhível ao iniciar (submenu da bandeja) e confirmado ao parar (janela). Configuração: "ao parar: perguntar / gerar direto". Padrão: perguntar |
| Travas | Alerta depois de 10 min de silêncio total. Para sozinho em 3 h |

## 3. Arquitetura

### 3.1 Fluxo de dados

```
 ┌──────────────┐   48k estéreo   ┌──────────┐ 16k mono ┌─────┐  fala  ┌─────────┐
 │ Mic (sd)     │───────────────▶│ resample │────────▶│ VAD │──────▶│ chunker │──┐
 └──────────────┘                 └──────────┘          └─────┘       └─────────┘  │
 ┌──────────────┐                 ┌──────────┐          ┌─────┐       ┌─────────┐  │  WAV
 │ Loopback     │───────────────▶│ resample │────────▶│ VAD │──────▶│ chunker │──┤
 └──────────────┘                 └──────────┘          └─────┘       └─────────┘  │
                                                                                   ▼
                                                    ┌───────────────────────────────────┐
                                                    │ spool/<sessão>/seg/*.wav  (fila)  │
                                                    └───────────────────────────────────┘
                                                                   │ 2 workers, retry
                                                                   ▼
                                       ┌─────────────┐   texto  ┌─────────────────────┐
                                       │ Transcriber │────────▶│ done.jsonl (verdade) │
                                       └─────────────┘          └─────────────────────┘
                                                                   │ ordena por início
                                                                   ▼
                                                        ┌───────────────────────┐
                                    (ao parar) ───────▶│ nota .md (ao vivo)     │
                                    Summarizer ───────▶│ nota .md (final)       │
                                                        └───────────────────────┘
```

- **Timestamps** vêm do relógio monotônico da sessão, não da contagem de amostras. O loopback pode não entregar amostras quando nada está tocando.
- **Ordem na nota:** os resultados chegam fora de ordem (dois workers, dois canais). O writer só publica até o início do trecho pendente mais antigo, então a nota nunca fica embaralhada. O preço é um atraso pequeno.
- **`done.jsonl`** guarda cada trecho transcrito (`start`, `end`, `speaker`, `text`). A nota é sempre *renderizada* a partir dele, e isso permite recuperar após crash e "resumir de novo".

### 3.2 Máquina de estados da sessão

```
IDLE ──iniciar──▶ RECORDING ──parar──▶ DRAINING ──fila vazia──▶ FINALIZING ──▶ IDLE
                    │  ▲                (termina de             (janela de parada,
            pausar  │  │ retomar         transcrever)            resumo, nota final,
            meu mic ▼  │                                         limpa o spool)
                 MIC_PAUSED
```

`OFFLINE` é um *flag* dentro de RECORDING/DRAINING, não um estado: a captura continua e só a fila espera.

### 3.3 Threads

| Thread | Função |
|---|---|
| main | Tkinter: overlay, configurações, janela de parada |
| tray | pystray (como no Artemis) |
| hotkeys | pynput (Win) / evdev (Linux), opcional |
| `capture-mic`, `capture-sys` | leitura → resample → VAD → chunker → spool |
| `stt-0`, `stt-1` | consomem o spool, chamam o Transcriber, gravam em `done.jsonl` |
| `writer` | renderiza a nota ao vivo (um só escritor, com lock) |
| `summary` | resumo no FINALIZING |

Tudo que mexe em UI volta para a main por `root.after(0, ...)`, igual ao Artemis.

### 3.4 Interface do provider

```python
class Transcriber(Protocol):
    def transcribe(self, wav: bytes, *, prompt: str = "",
                   keywords: list[str] | None = None,
                   languages: list[str] | None = None) -> str: ...

class Summarizer(Protocol):
    def summarize(self, request: SummaryRequest) -> SummaryResult: ...
    # SummaryRequest: transcrição formatada, anotações do usuário, participantes,
    #                 meu nome, data, duração, system_prompt do preset
    # SummaryResult:  title (para o nome do arquivo), tags, body_markdown
```

`OpenAIProvider` implementa as duas e reaproveita a degradação de parâmetros do Artemis. O resumo pede saída estruturada (JSON com `title`, `tags`, `body_markdown`) e, se o modelo recusar o formato, cai para Markdown puro com o título na 1ª linha.

## 4. Estrutura do projeto

```
apolo/
  __main__.py            # entrada + CLI (--settings, --set-key, --devices, --test-capture, --from-wav, --debug)
  app.py                 # AppController: liga UI ↔ sessão
  config.py              # %APPDATA%\Apolo\config.json + presets.json      (do Artemis)
  secrets_store.py       # keyring, serviço "Apolo"                         (do Artemis)
  errors.py              # ApoloError & cia                                 (do Artemis)
  i18n.py                # pt/en/es, novas chaves                           (do Artemis)
  startup.py, startup_win.py, linux.py                                      (do Artemis)
  hotkeys.py, hotkeys_win.py, hotkeys_evdev.py                              (do Artemis)
  presets.py             # SummaryPreset
  audio/
    sources.py           # MicSource (sounddevice), SystemSource (loopback), list_devices()
    resample.py          # soxr → 16 kHz mono int16
    vad.py               # wrapper do VAD escolhido na fase 0
    chunker.py           # regras de corte (mín/alvo/máx), sem I/O, testável
    recorder.py          # opção "salvar áudio": FLAC contínuo por canal
  session/
    session.py           # máquina de estados, travas (silêncio, 3 h)
    spool.py             # layout em disco, teto de 2 h, recuperação, limpeza
    workers.py           # pool de STT, retry/backoff, flag offline
    transcript.py        # Segment, ordenação, junção de falas seguidas
  notes/
    render.py            # Markdown ao vivo e final (callout | details)
    writer.py            # escrita atômica, preserva "Minhas anotações", renomeia
  providers/
    base.py              # Protocols + dataclasses
    openai_provider.py
  ui/
    tray.py, overlay.py, icon.py                                            (adaptados)
    settings_window.py   # abas: Geral, Áudio, Notas, Presets
    stop_dialog.py       # tipo, título, participantes
tests/                   # pytest só para a lógica pura: chunker, render, spool, ordenação
spike/                   # fase 0, descartável
requirements.txt
README.md / README.pt-BR.md
```

## 5. A nota

**Nome:** `2026-10-26 1430 - <Título>.md`. Durante a gravação fica `2026-10-26 1430 - Gravando.md` e é renomeada no fim (com sufixo se o nome já existir).

```markdown
---
title: Alinhamento de escopo com cliente X
date: 2026-10-26T14:30:00-03:00
duration: 47min
preset: cliente
participants: [João, Ana]
tags: [reuniao, cliente]
app: apolo
---
# Alinhamento de escopo com cliente X

> 26/10/2026 14:30 · 47 min · Cliente · com João, Ana

## Resumo
...

## Decisões
- ...

## Próximos passos
- [ ] **João**: enviar proposta revisada (até 30/10)
- [ ] **Matheus**: validar estimativa com o time

## Minhas anotações
<!-- apolo:notas:inicio -->
(o que você escrever aqui durante a reunião é preservado e usado no resumo)
<!-- apolo:notas:fim -->

## Transcrição
> [!note]- Transcrição completa (47 min)
> **[00:00:12] Eu:** Bom dia, pessoal, vamos começar pelo escopo?
>
> **[00:00:20] Outros:** Bom dia! Então, sobre a integração...
>
> *[00:31:40–00:33:05] trecho não transcrito (sem conexão)*
```

- As seções entre "Resumo" e "Próximos passos" vêm do preset.
- Durante a gravação a transcrição fica aberta (`[!note]+`) e o resumo diz "gerando ao parar". No fim ela fecha (`[!note]-`).
- Configuração `transcript_block`: `callout` (Obsidian, padrão) ou `details` (`<details>` HTML para VS Code/GitHub).
- Falas seguidas do mesmo falante com menos de ~3 s de intervalo viram um parágrafo só.
- O nome em `my_name` (configuração) é passado ao LLM como "Eu", para que as tarefas apareçam com o seu nome.

## 6. Configuração e presets

### `config.json` (padrões)

```jsonc
{
  "mic_device": null,            // null = padrão; senão o nome (não o índice)
  "loopback_device": null,       // null = saída padrão; ver risco R2
  "stt_model": "gpt-transcribe",
  "text_model": "gpt-5.6-luna",
  "languages": ["pt", "en"],
  "keywords": [],                // glossário global
  "my_name": "",
  "notes_dir": "~/Documents/Apolo",
  "spool_dir": null,             // null = %LOCALAPPDATA%\Apolo\spool
  "save_audio": false,
  "audio_dir": null,
  "default_preset": "geral",
  "on_stop": "ask",              // ask | auto
  "transcript_block": "callout", // callout | details
  "chunk_min_seconds": 1.0,
  "chunk_target_seconds": 12,
  "chunk_max_seconds": 30,
  "stt_workers": 2,
  "request_timeout": 60,
  "max_pending_audio_hours": 2,
  "silence_alert_minutes": 10,
  "max_session_hours": 3,
  "hotkey": null,                // opcional, ex.: "<ctrl>+<alt>+m"
  "overlay_mode": "always",
  "ui_language": "auto",
  "start_with_system": false
}
```

### `presets.json`: os 4 iniciais

| id | Seções do corpo |
|---|---|
| `geral` | Resumo · Decisões · Próximos passos · Pontos em aberto |
| `daily` | Resumo · Por pessoa (fez / vai fazer / impedimentos) · Impedimentos · Próximos passos |
| `cliente` | Contexto · Necessidades e dores do cliente · O que foi acordado · Compromissos (nossos / deles) · Riscos · Próximos passos · Rascunho de e-mail de follow-up |
| `1on1` | Temas · Feedbacks · Combinados · Para acompanhar na próxima |

Campos: `id`, `name`, `description`, `system_prompt`, `keywords`, `languages` (null = global), `text_model` (null = global). Criar um preset novo é só editar o JSON ou usar a aba Presets.

## 7. Fases

Cada fase termina com algo que você consegue testar. Eu paro no fim de cada uma para você validar antes de seguir. Um commit por fase.

### Fase 0: teste de captura no Windows (descartável, em `spike/`)
O maior risco técnico vem primeiro.
- Listar entradas e saídas, incluindo o **dispositivo de comunicação padrão**.
- Gravar 20 s de mic + loopback **ao mesmo tempo** com `soundcard` e mostrar o nível de cada um. Salvar os WAVs para ouvir.
- Comportamento do loopback **sem nada tocando**: trava, devolve zeros ou pula?
- Mesmo teste com `PyAudioWPatch` se o `soundcard` tiver problema.
- Teste numa chamada real (Teams/Meet) com fone com fio e com Bluetooth.
- Comparar VAD: `pysilero-vad` (sem torch) vs `webrtcvad-wheels`: qualidade em pt-BR e CPU.
- Mandar um trecho ao `gpt-transcribe` com `languages=["pt","en"]` e confirmar se a lista com 2 idiomas é aceita.

**Pronto quando:** sabemos qual lib de captura e qual VAD usar, e o loopback pega o áudio do Teams de forma confiável.

### Fase 1: esqueleto
- `git init`, `requirements.txt`, estrutura de pastas.
- Copiar e adaptar do Artemis: config, secrets_store, errors, i18n, startup, hotkeys, tray, overlay, icon.
- Configurações com a aba Geral (API key, modelos, idioma da UI, iniciar com o sistema).
- Bandeja com Iniciar ▸ preset / Parar / Configurações / Sair. Iniciar ainda não faz nada.

**Pronto quando:** `python -m apolo` abre na bandeja, salva a API key e as configurações.

### Fase 2: pipeline de áudio (sem API)
- `sources.py`, `resample.py`, `vad.py`, `chunker.py`, `spool.py`.
- CLI `--test-capture`: grava e mostra os trechos cortados (canal, início, duração).
- CLI `--from-wav arquivo.wav`: alimenta o chunker a partir de um arquivo, para desenvolver sem reunião.
- Testes do chunker com áudio sintético (fala/silêncio gerados).

**Pronto quando:** numa reunião real, os trechos do spool fazem sentido (cortes em pausas, nada de silêncio puro, ≤ 30 s).

### Fase 3: transcrição ao vivo
- `providers/base.py` + `openai_provider.py` (transcribe).
- Workers com retry/backoff exponencial, flag offline, teto de 2 h com lacuna marcada.
- `done.jsonl`, ordenação e escrita ao vivo da nota (seção de transcrição + "Minhas anotações").
- Overlay: tempo gravando · trechos pendentes · "sem conexão".
- **Validar o risco R5** (editar a nota no Obsidian enquanto o Apolo escreve).

**Pronto quando:** dá para acompanhar a transcrição crescendo no Obsidian. Desligar o Wi-Fi por 2 min e religar não perde nada.

### Fase 4: fim da sessão e resumo
- `stop_dialog.py` (preset, título, participantes) e `on_stop = auto`.
- `summarize()` com os 4 presets, saída estruturada e fallback.
- Render final: frontmatter, título, renomear o arquivo, fechar o callout.
- Limpeza do spool. Opção `save_audio` (FLAC por canal).
- **Recuperação:** sessão incompleta ao abrir → "Retomar processamento?".
- Bandeja: **"Resumir de novo…"**, com a nota escolhida + preset, refeito a partir do `done.jsonl` ou da própria nota.

**Pronto quando:** uma reunião real de ponta a ponta gera uma nota boa, e matar o processo no meio permite retomar.

### Fase 5: configurações completas e polimento
- Abas Áudio (dispositivos + **medidor de nível ao vivo**, teste de 5 s), Notas (pastas, formato do bloco, `my_name`), Presets (editor como o do Artemis).
- Travas: alerta de silêncio e parada em 3 h. Atalho global opcional e "pausar meu mic".
- Reabrir o loopback ao trocar o dispositivo de saída no meio da reunião (R3).
- Strings pt/en/es, README.

**Pronto quando:** dá para usar no dia a dia sem editar JSON na mão.

### Fase 6: Linux
- Loopback pelo monitor do PulseAudio/PipeWire (`soundcard` usa libpulse, que funciona com pipewire-pulse).
- Autostart e atalho evdev como no Artemis. O atalho fica opcional por causa do conflito com o *grab* do Artemis (R7).
- Bandeja no GNOME (AppIndicator), aproveitando o diagnóstico do Artemis.

**Pronto quando:** o mesmo teste de ponta a ponta da fase 4 passa na sua distro.

## 8. Riscos

| # | Risco | Mitigação |
|---|---|---|
| R1 | O loopback WASAPI não entrega amostras em silêncio e a lib trava | Fase 0 testa. Timestamps pelo relógio. Leitura com timeout |
| R2 | O Teams toca no **dispositivo de comunicação**, não no padrão, e o loopback ouve o lugar errado | Fase 0 verifica. Configuração `loopback_device` + medidor de nível nas configurações |
| R3 | Trocar o fone no meio da reunião deixa o stream preso no dispositivo antigo | Detectar erro ou silêncio anormal e reabrir no novo padrão (fase 5) |
| R4 | Caixa de som em vez de fone: o mic capta os outros e as falas aparecem duplicadas como "Eu" | Protótipo: recomendar fone. Backlog: AEC com o loopback como referência |
| R5 | Editar a nota no Obsidian enquanto o Apolo reescreve o arquivo dá conflito ou perda de texto | Fase 3 testa. Se der problema, a transcrição ao vivo vai para `<nota>.live.md` e a nota principal só é escrita no fim |
| R6 | O loopback pega notificações, YouTube etc. | Aceito no protótipo. Backlog: captura por processo |
| R7 | Linux: o atalho evdev com *grab* briga com o do Artemis | Atalho opcional. A bandeja é o caminho principal |
| R8 | Você está mutado no Teams, mas o Apolo continua transcrevendo seu mic | "Pausar meu mic" na bandeja e no atalho |
| R9 | A API não aceita `languages` com 2 valores | Usar só `pt` e reforçar os termos em inglês por `keywords` |
| R10 | Política da empresa sobre mandar conteúdo de reunião para a OpenAI | Decisão sua. O overlay deixa claro quando está gravando |

## 9. Custo estimado

- STT a US$ 0,0045/min (nota de set/2026 no provider do Artemis). Com VAD, paga-se só a fala: **~US$ 0,23 por hora de reunião**.
- Resumo: 1 h ≈ 15k tokens de entrada, centavos com `gpt-5.6-luna`.
- Uso típico (~22 h/mês): **~US$ 5–6/mês**.

## 10. Backlog (depois do protótipo)

- Janela de transcrição ao vivo própria
- Sugerir gravação quando o mic for aberto pelo Teams/Zoom/navegador
- Captura só do processo da reunião (Win process loopback / nó PipeWire)
- Cancelamento de eco (WebRTC AEC) para usar sem fone
- Providers locais: faster-whisper (STT) e Ollama/llama.cpp (resumo)
- Diarização dos "Outros"
- Resumo map-reduce para reuniões muito longas
- Empacotamento (instalador)

## 11. Dependências previstas

```
openai
sounddevice          # mic (como no Artemis)
soundcard            # loopback, ou PyAudioWPatch no Windows (decide na fase 0)
numpy
soxr                 # resample
pysilero-vad         # ou webrtcvad-wheels (decide na fase 0)
soundfile            # FLAC para a opção "salvar áudio"
pystray, Pillow, keyring
pynput; sys_platform == "win32"
evdev;  sys_platform == "linux"
pytest               # dev
```
