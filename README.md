# Apolo

Notas de reunião geradas localmente, sem bot na chamada. Gêmeo do
[Artemis Dictation](https://github.com/matheuscastelliano/Artemis-Dictation):
o Artemis dita, o Apolo registra.

Você liga pela bandeja quando entra numa reunião (Teams, Meet, qualquer uma).
O Apolo ouve o seu microfone ("Eu") e o áudio do sistema ("Outros"), transcreve
em trechos e, ao parar, escreve uma nota em Markdown com resumo, próximos passos
e a transcrição recolhida, numa pasta à sua escolha (pode ser o vault do Obsidian).

> **Estado:** protótipo em construção. O plano completo está em [PLANO.md](PLANO.md),
> e os testes de captura da fase 0, em [spike/RESULTADOS.md](spike/RESULTADOS.md).

| Fase | O que entrega | Estado |
|---|---|---|
| 0 | Teste de captura no Windows | ✅ |
| 1 | Esqueleto: bandeja, configurações, presets | ✅ |
| 2 | Pipeline de áudio (mic + loopback, VAD, chunker, spool) | ✅ |
| 3 | Transcrição ao vivo na nota | ✅ |
| 4 | Fim da sessão, resumo, recuperação | ✅ |
| 5 | Configurações completas e polimento | — |
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

Configuração: `%APPDATA%\Apolo\config.json` e `presets.json` (os tipos de resumo).
