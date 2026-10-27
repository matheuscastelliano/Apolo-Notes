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
   bandeja.
4. **Acabou:** **Parar gravação**. Uma janelinha pergunta o tipo de resumo, um
   título e quem estava, enquanto o resto da transcrição termina. A nota final
   ganha o resumo no topo, a transcrição recolhida e o título no nome do arquivo.

Use fone: com caixa de som, o microfone também capta os outros e a fala deles
aparece como "Eu".

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
