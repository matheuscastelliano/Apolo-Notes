# Fase 0: resultados do teste de captura (Windows)

Máquina: Windows 11, Python 3.13.7, AirPods Pro (Bluetooth) como saída e mic padrão.

## Já respondido

| Pergunta | Resultado |
|---|---|
| Saída padrão = saída de comunicação? (R2) | **Sim** nesta máquina: as duas são "AirPods do Mat". O Windows 11 mostra uma saída só para o AirPods (sem endpoint "Hands-Free" separado). |
| As libs enxergam o loopback? | `soundcard` e `PyAudioWPatch` enxergam as duas saídas (Realtek e AirPods), 48 kHz estéreo. |
| Loopback com áudio tocando | As duas entregam continuamente (blocos de 100 ms, intervalo máx. 0,1 s) e com níveis idênticos. Na 1ª execução o `PyAudioWPatch` teve um buraco de 5,6 s. O `soundcard` não teve nenhum. |
| Loopback em **silêncio absoluto** (R1) | **`soundcard` entrega zeros continuamente** (-120 dBFS, blocos regulares, intervalo máx. 0,13 s), sem travar. **`PyAudioWPatch` não entrega nada** (0 blocos em 11 s). Confirma o R1 para o `PyAudioWPatch` e fecha a escolha do `soundcard`. |
| VAD | **Silero (`pysilero-vad`)**: 2,4% de um núcleo, cortou nas pausas, descartou 100% do silêncio longo e não perdeu nenhuma palavra na borda. **WebRTC**: 0,8% de CPU, mas marcou ruído como fala (91% do áudio enviado contra 74% do Silero), incluindo um trecho só de ruído, que é exatamente o que gera alucinação. → **Silero.** O pacote roda em ggml, sem torch e sem onnxruntime. |
| `gpt-transcribe` com `languages=["pt","en"]` (R9) | **Aceito.** Texto fiel, termos em inglês preservados. Sem `keywords` saiu "stage" em vez de "staging". **Com `keywords` saiu perfeito.** Latência de 0,7 a 1,9 s por trecho de 6 a 13 s (a 1ª chamada levou 5,8 s, cold start). |
| Chunker (rascunho em `vad_compare.py`) | 57 s de "reunião" viraram 4 trechos (6–13 s), cortados em pausas. Os 8 s de silêncio não foram enviados. |

## Testes 2 a 4 (rodados por você)

| Teste | Resultado |
|---|---|
| 2. Mic do AirPods + loopback (HFP) | **O loopback sobrevive à troca para HFP.** Tem um soluço no primeiro segundo (troca de perfil do Bluetooth): o `soundcard` emite 1 aviso de `data discontinuity` e o `PyAudioWPatch` fica 1,3 s sem entregar. Depois disso, a voz sintética foi captada inteira. No app, o aviso vai para o log e não interrompe nada. |
| 3. Chamada real | **O loopback pega a chamada:** 51 s de fala em 60 s. O chunker gerou 3 trechos (25,8 s, 16,8 s e 14,3 s). Com fala contínua e sem pausa de 0,3 s, o trecho passa do alvo de 12 s, como esperado. |
| 4. Caixa do notebook | A voz sintética **não apareceu** no mic (piso de -78 dB, só 0,6 s parecido com fala, aos 19 s). **Inconclusivo** até confirmar que o mic capta voz (teste 5) e qual mic estava como padrão. |
| Mic (2, 3 e 4) | ⚠️ **Nenhuma fala detectada no mic em nenhum dos três testes** (piso de -80 dBFS, só cliques isolados). Ou não houve fala, ou o mic não está chegando. → teste 5. |

| 5. Só o mic (contando de 1 a 10) | **O mic funciona:** -24 a -33 dBFS, Silero detectou 6,1 s de fala, e o STT devolveu "Dois, três, quatro, ... dez, onze." Nos testes 2–4 provavelmente só não houve fala. ⚠️ O **"um" se perdeu**: o stream do mic leva **~1–1,4 s para começar a entregar** (troca do Bluetooth para HFP). Nos testes 2–5, o 1º segundo do mic é sempre `---`. |

### Conclusões da fase 0

- **Captura confirmada no Windows:** `sounddevice` (mic) + `soundcard` (loopback). O `PyAudioWPatch` sai: não entrega nada em silêncio e teve buracos de 1,3 s e 5,6 s.
- **Ajuste para a fase 2:** o app só mostra "Gravando" **depois que os dois streams entregarem o 1º bloco**. Assim você não fala achando que já está gravando. O overlay fica em "Preparando..." nesse ~1 s.
- **Ajuste para a fase 2:** os avisos de `data discontinuity` do `soundcard` vão para o log e não interrompem a gravação.
- **R4 (caixa → mic) continua em aberto:** no teste 4 o mic padrão provavelmente ainda era o do AirPods (piso de -78 dB, igual aos outros testes com AirPods). Isso não bloqueia nada, porque o protótipo recomenda fone. Refazer quando for usar a caixa, trocando **também** a entrada para o mic do notebook.

```bat
:: 5. Só o mic: conte de 1 a 10 em voz alta
.venv\Scripts\python spike\capture.py --no-tts --seconds 10 --tag _mic
```

## Roteiro original

O loopback captou **fala real** durante os testes (o VAD deu 100% de fala, mesmo sem a voz sintética). Parecia uma chamada ou um vídeo tocando. Por isso:
- **Apaguei os WAVs do loopback** que tinham esse áudio e não mandei nada dele para a API.
- O teste de "loopback em silêncio absoluto" (R1) ficou sem resposta.

Rode da pasta `Meet Member`, um de cada vez:

```bat
:: 1. Silêncio absoluto (R1): feche tudo que toca som (chamada, vídeo, música)
.venv\Scripts\python spike\capture.py --no-mic --no-tts --seconds 10 --tag _silencio

:: 2. Mic + loopback juntos (abrir o mic do AirPods troca o Bluetooth para HFP)
::    Toca uma voz sintética nos AirPods. Fale algo por cima dela.
.venv\Scripts\python spike\capture.py --tag _hfp

:: 3. Chamada real no Teams/Meet, 60 s, sem voz sintética
.venv\Scripts\python spike\capture.py --no-tts --seconds 60 --tag _teams

:: 4. Igual ao 2, mas com a caixa do notebook como saída e mic (risco R4, eco)
.venv\Scripts\python spike\capture.py --tag _caixa
```

O que olhar em cada saída:
1. **Silêncio:** as colunas ficam em `-120` (zeros), `---` (nada chegou) ou o script trava? E o "intervalo max"?
2. **HFP:** a coluna `mic` tem a sua voz, e as colunas de loopback continuam com a voz sintética (não `---`)?
3. **Teams:** o loopback pega os outros participantes? Ouça `spike\out\loop_soundcard_teams.wav`.
4. **Caixa:** a voz sintética aparece **também** na coluna `mic`? Se sim, o R4 se confirma.

Depois de ouvir, apague os WAVs (`del spike\out\*_teams.wav` etc.). Eles têm voz de outras pessoas.

## Decisões provisórias

- **Loopback:** `soundcard`, que funcionou nas duas execuções e é a mesma lib no Linux. `PyAudioWPatch` fica de reserva.
- **Mic:** `sounddevice`, como no Artemis.
- **VAD:** `pysilero-vad`, limiares 0,5 para entrar em fala e 0,35 para sair.
- **Chunker:** alvo 12 s, pausa curta 0,3 s, pausa longa 1,5 s, máximo 30 s, fala mínima 0,4 s, margem 0,2 s.
- **STT:** `languages=["pt","en"]` + `keywords` (glossário global + do preset) + `prompt` com os últimos ~200 caracteres.
