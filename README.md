# Apolo

Notas de reunião geradas localmente, sem bot na chamada. Gêmeo do
[Artemis Dictation](https://github.com/matheuscastelliano/Artemis-Dictation):
o Artemis dita, o Apolo registra.

Você liga pela bandeja quando entra numa reunião (Teams, Meet, qualquer uma).
O Apolo ouve o seu microfone ("Eu") e o áudio do sistema ("Outros"), transcreve
em trechos e, ao parar, escreve uma nota em Markdown com resumo, próximos passos
e a transcrição recolhida, numa pasta à sua escolha (pode ser o vault do Obsidian).

> **Estado:** protótipo, só no Windows por enquanto. O plano completo está em
> [PLANO.md](PLANO.md), e os testes de captura da fase 0, em
> [spike/RESULTADOS.md](spike/RESULTADOS.md).

| Fase | O que entrega | Estado |
|---|---|---|
| 0 | Teste de captura no Windows | ✅ |
| 1 | Esqueleto: bandeja, configurações, presets | ✅ |
| 2 | Pipeline de áudio (mic + loopback, VAD, chunker, spool) | ✅ |
| 3 | Transcrição ao vivo na nota | ✅ |
| 4 | Fim da sessão, resumo, recuperação | ✅ |
| 5 | Configurações completas e polimento | ✅ |
| 6 | Linux | — |
| V0–V4 | Quem falou o quê nos "Outros" ([PLANO_VOZES.md](PLANO_VOZES.md)) | ✅ |

## Rodando (Windows)

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Apolo.cmd
```

Ou `.venv\Scripts\python -m apolo --debug` para ver o log no console.

A API key da OpenAI fica no Gerenciador de Credenciais. Se o Artemis Dictation
já tiver uma salva, o Apolo usa a mesma até você configurar outra.

## No dia a dia

1. **Antes da primeira reunião:** em Configurações ▸ Áudio, clique em
   **Testar por 5 s** e confira que as duas barras se mexem. Em Notas, aponte a
   pasta para o seu vault, se quiser.
2. **Entrou na reunião:** clique no ícone da bandeja (o sol) ▸ **Iniciar
   gravação**, ou use **Iniciar com o tipo de resumo** para escolher já um tipo.
   Espere o indicador sair de "Preparando..." para "Gravando" (com fone
   Bluetooth leva ~1,5 s).
3. **Durante:** a nota `AAAA-MM-DD HHMM - Gravando.md` vai crescendo. Clique no
   indicador para abri-la. O que você escrever em **Minhas anotações** é
   preservado e entra no resumo. Mutou no Teams? **Pausar meu microfone** na
   bandeja. Se quiser, bandeja ▸ **Quem está na reunião...** e vá dizendo quem
   está (opcional, dá para completar depois).
4. **Acabou:** **Parar gravação**. Uma janelinha pergunta o tipo de resumo, um
   título e quem estava, enquanto o resto da transcrição termina. A nota final
   ganha o resumo no topo, a transcrição recolhida e o título no nome do arquivo.
5. **Quem é quem:** com duas ou mais pessoas do outro lado, aparece uma janela
   com cada voz que o Apolo separou: ▶ para ouvir, e você escolhe o nome (ou
   digita um novo). A nota sai com `Marcos:`, `Fernando:` no lugar de
   `Outros:`. Quem você nomeou é reconhecido sozinho nas próximas reuniões; se
   todo mundo for reconhecido, a janela nem aparece.

Use fone: com caixa de som, o microfone também capta os outros e a fala deles
aparece como "Eu".

Sobre os nomes na transcrição:

- **Conversa a dois:** com uma pessoa só em "Quem estava", a fala dela já sai
  com o nome dela, e "Eu" sai com o seu nome (Configurações ▸ Notas ▸ Seu nome).
  As vozes nem são separadas.
- **Três ou mais:** o áudio do computador é separado em vozes em blocos de
  2 minutos durante a reunião (`gpt-4o-transcribe-diarize`, cobrado por minuto
  como a transcrição). Voz que você não nomear sai como "Voz A", "Voz B".
- As amostras de voz (3 a 8 s por pessoa) ficam só neste computador, em
  Configurações ▸ **Vozes**, onde dá para ouvir, renomear e apagar. A mesma aba
  desliga a separação de vozes.

Se algo der errado no meio:

- **Internet caiu:** o áudio espera no disco (até 2 h) e é transcrito quando a
  conexão volta. O indicador mostra "sem conexão".
- **App fechou ou travou:** na próxima vez que abrir, ele oferece terminar a
  gravação que ficou pela metade.
- **O resumo falhou, ou você quer outro tipo:** bandeja ▸ **Resumir de novo...**
  e escolha a nota.

Travas: alerta depois de 10 min sem fala nenhuma, e parada automática em 3 h.
As duas são configuráveis em Notas.

## Arquivos

| Onde | O quê |
|---|---|
| `%APPDATA%\Apolo\config.json` | Configurações (a janela edita tudo) |
| `%APPDATA%\Apolo\presets.json` | Os tipos de resumo (aba Tipos de resumo) |
| `%APPDATA%\Apolo\apolo.log` | Log |
| `%APPDATA%\Apolo\voices\` | Amostras das vozes que você nomeou (aba Vozes) |
| `%LOCALAPPDATA%\Apolo\spool\` | Áudio temporário, apagado quando a nota fica pronta |
| `Documentos\Apolo\` | As notas (padrão; configurável) |

## Para desenvolver

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m apolo --devices
.venv\Scripts\python -m apolo --test-capture 20 --keep-spool
.venv\Scripts\python -m apolo --simulate spike\out\sample.wav --speed 4
```

- `--devices`: lista os mics e as saídas que viram loopback.
- `--test-capture`: grava mic + loopback e mostra onde o detector de pausas
  cortou, sem chamar a API.
- `--simulate`: reunião de mentira a partir de um WAV, que entra como "Outros",
  com transcrição e resumo de verdade.
- O `sample.wav` sintético é gerado por `spike\make_sample.py`.
