# Apolo: quem falou o quê (plano)

Hoje o Apolo só sabe de qual **canal** veio cada fala: o microfone é você, o áudio do computador é "Outros". Desde `de34905`, numa conversa a dois o "Outros" já aparece com o nome do participante. Este plano cobre reuniões com **duas ou mais** pessoas do outro lado.

## 1. O que o teste mostrou (Planning 03 - Veredas, 23/11, 13 min)

Rodei o `Others.flac` por dois caminhos:

| | OpenAI `gpt-4o-transcribe-diarize` | Local (sherpa-onnx + pyannote + WeSpeaker) |
|---|---|---|
| Vozes achadas | A = 104 s (Fernando), B = 304 s (Marcos), C = 1 s ("valeu"): **confirmado por você** | na melhor config, L0 = 376 s e L1 = 34 s: misturou quase toda a A na B |
| Coerência | tudo que é A soa como uma pessoa só ("eu comecei a desenvolver", "eu e o Pedro") | não separou as duas pessoas que falaram mais |
| Tempo | ~5,5 min para 13 min de áudio, numa chamada só | ~2,5 min por rodada |
| Texto | pior que o do `gpt-transcribe` ("Crio Felipe", "piais") e picota palavras | n/a |

**Decisão:** a OpenAI separa as vozes. O texto continua vindo do `gpt-transcribe`, como hoje: o diarize só diz **quem** falou **quando**. O caminho local foi descartado e o que foi instalado para ele já foi removido.

Privacidade: o áudio dos "Outros" já vai para a OpenAI na transcrição, então separar as vozes lá não muda o quadro. A novidade são as **amostras de voz** de colegas. Elas ficam só no seu computador e só saem como referência nas chamadas de identificação.

## 2. Como vai funcionar para você

1. Durante a reunião, o áudio dos "Outros" vai sendo separado em vozes aos poucos, por trás. Se quiser, você vai dizendo **quem está na reunião** a qualquer momento da gravação (ver 3.6): o Apolo já tenta pôr os nomes e chega no fim com quase tudo resolvido. Se não disser nada, tudo funciona igual e os nomes saem no fim.
2. Na janela de parada aparece, além do tipo, do título e de quem estava, uma lista assim:

   ```
   Quem é quem
   Voz A  ▶  [Fernando       v]   "eu e o Pedro estamos puxando os cards..."
   Voz B  ▶  [Marcos         v]   "então aqui tem três casos de impedimento..."
   Voz C  ▶  [Não sei        v]   "valeu, valeu"
   ```

   Cada ▶ toca um trecho curto daquela voz. A lista de nomes vem do campo "Quem estava". Vozes que o Apolo já conhece aparecem preenchidas, e você só confere.
3. A nota sai com os nomes na transcrição, e o resumo já recebe as falas de cada um:
   `**[00:03:08] Marcos:** Então aqui tem três casos de impedimento...`
4. Quem você identificou ganha uma amostra de voz guardada. Na próxima reunião com essa pessoa, o nome já vem sugerido.
5. Fechar a janela sem escolher nada mantém o comportamento de hoje: vozes sem nome viram "Voz A", "Voz B", e o resumo nunca fica para trás.

## 3. Arquitetura

### 3.1 Separação durante a reunião, em janelas
Fazer tudo no fim seria lento demais: 13 min de áudio levaram 5,5 min, e uma reunião de 1 h passaria de 20 min. Por isso:

- Um `DiarizeQueue` (irmão do `SttQueue`) junta os trechos do canal `sys` que já estão no spool em **janelas de ~4 min de fala**. Os silêncios entre trechos são cortados, e um mapa guarda o tempo original de cada pedaço.
- Cada janela vai para `gpt-4o-transcribe-diarize` com `response_format="diarized_json"` e `chunking_strategy="auto"`.
- **Mesmas letras entre janelas:** cada janela é separada sozinha, então a "A" de uma janela pode ser a "B" da seguinte. Para evitar isso, a partir da 2ª janela a chamada leva como `known_speaker_references` um trecho de 2 a 10 s de cada voz já vista, com o nome interno (`V1`, `V2`...). Assim a "Voz A" da janela 1 continua sendo a mesma pessoa na janela 5.
- **O limite de 4 é só aqui:** a separação em si acha quantas vozes houver, mas cada chamada aceita no máximo **4 referências**. Com até 4 pessoas falando do outro lado, as letras se mantêm a reunião toda. Com 5 ou mais, vão as 4 que mais falaram, e uma voz extra pode reaparecer com letra nova em outra janela. Essas duplicadas são juntadas na validação.
- O resultado vai para `diar.jsonl` no spool, como o `done.jsonl`: retomar depois de travar não perde nada.
- Sem conexão, a janela espera, como os trechos da transcrição. Se a separação falhar de vez, a nota sai como hoje ("Outros").

### 3.2 Casar vozes com as falas da transcrição
Os trechos do `gpt-transcribe` têm ~12 s e podem pegar duas pessoas. Na v1:
- Cada trecho fica com a voz que ocupa **a maior parte** do tempo dele.
- Se a segunda voz passar de ~30% do trecho, ele é marcado como misto. Na nota sai a voz principal, e o resumo recebe as duas ("Marcos/Fernando").
- Melhoria futura, se incomodar: cortar os trechos do canal `sys` em pausas mais curtas.

### 3.3 Identificar quem é quem
Com os nomes de "Quem está" (durante a gravação, 3.6) ou de "Quem estava" (no Parar):
- Para cada nome que já tem amostra na biblioteca, o Apolo monta um áudio curto (~30 s) com um trecho de cada voz da reunião e faz uma chamada de diarize passando as amostras da biblioteca como referências. Com mais de 4 conhecidos, são várias chamadas curtas de 4 em 4: aqui o limite só custa uma chamada a mais, não deixa ninguém de fora. A resposta sugere o nome de cada voz.
- Voz sem amostra na biblioteca fica como pendência para você resolver na janela de parada.
- Sugestão é só sugestão: você confirma na janela de parada.
- **Cada voz é identificada uma vez só.** Quando todas as pessoas que você informou já têm voz com nome, as chamadas de identificação param. A **separação de cada janela** continua, porque é ela que diz quem falou cada frase daquele trecho. Mas, daí em diante, cada janela já vai com as amostras **com nome** como referência, e a resposta volta com os nomes direto, sem adivinhar de novo.
- **Com até 4 pessoas** do outro lado, tudo cabe numa chamada por janela.
- **Com 5 ou mais**, cada janela leva as 4 que falaram por último. Se aparecer alguém fora dessas 4, só essa voz passa por uma identificação curta contra os nomes que sobraram. Ninguém é identificado de novo à toa.
- Com 1 participante só, nada disso roda: vale a regra do `de34905`.

### 3.4 Biblioteca de vozes
- `%APPDATA%\Apolo\voices\voices.json` + `voices\<id>\*.wav`. Cada pessoa tem nome e até 3 amostras de 3 a 8 s, escolhidas entre os trechos mais limpos de reuniões diferentes. Amostras novas substituem as mais antigas.
- Nova aba **Vozes** nas Configurações: lista de pessoas, ▶ para ouvir, renomear, apagar uma pessoa, apagar tudo.
- Opção geral **"Identificar quem falou"** (liga/desliga). Desligada, nada é separado nem guardado.

### 3.6 "Quem está" durante a gravação (opcional)
- Novo item **"Quem está na reunião..."** no menu da bandeja e no clique direito do indicador: abre uma janelinha com o mesmo campo de nomes da janela de parada. Dá para abrir várias vezes e ir completando, por exemplo quando alguém entra atrasado.
- Os nomes informados já vêm preenchidos em "Quem estava" no Parar.
- Com **1 pessoa** informada, a separação nem roda: vale a regra do `de34905`. Isso resolve o gasto à toa nas 1:1.
- Com 2 ou mais, a identificação (3.3) começa assim que houver voz e nome, sem esperar o fim.
- Sem nada informado, comportamento padrão: separa durante a reunião e identifica no fim.

### 3.5 Mudanças no que já existe
- `Segment` ganha `voice: str | None` (ex.: `"V2"`), e a sessão ganha um mapa `voice -> nome`. O `done.jsonl` antigo continua sendo lido.
- `render.speaker_labels` passa a olhar a voz primeiro: nome confirmado > "Voz A" > regra de 1 participante > "Outros".
- `reparse` entende nomes e "Voz X" de volta, para o "Resumir de novo".
- Prompt do resumo: quando há nomes, a regra "'Outros' pode ser várias pessoas" vira "cada fala já vem com o nome de quem falou".
- Tocar áudio usa o `sounddevice`, que já é dependência e funciona no Linux também.

## 4. Fases

Como no plano principal: cada fase termina com algo testável, com um commit por fase.

### Fase V0: validação com a gravação da Planning 03 (descartável, em `spike/`)
- ~~Você confirma quem é a Voz A e a Voz B~~: A = Fernando, B = Marcos (23/11).
- Rodar a gravação em janelas de 4 min **com** referências entre janelas e medir se as letras se mantêm e quanto tempo leva cada janela.
- Fazer a identificação (3.3) com amostras cortadas da própria reunião e ver se acerta os nomes.
- Casar com a transcrição da nota (3.2) e contar quantos trechos ficam mistos.

**Pronto quando:** as letras se mantêm entre janelas, a identificação acerta e a última janela fica pronta em menos de ~1 min depois do Parar.

**Resultado (23/11, `spike/vozes_v0.py`):**
- **Janelas:** as letras se mantiveram. Com as amostras da janela 1, as janelas 2 a 4 devolveram V1 (Marcos) e V2 (Fernando) direto, e a reunião toda bateu **88%** com a separação do arquivo inteiro. Só o "valeu" do fim voltou como voz nova.
- **Tempo:** ~100 s por janela de 4 min (0,4× o tempo do áudio). A última janela depois do Parar passaria de 1 min. → **Janelas de 2 min** (~50 s para a última).
- **Identificação com a biblioteca completa:** acertou os dois nomes, em 12 s.
- **Identificação com a biblioteca incompleta (só o Fernando): ERROU.** Deu o nome "Fernando" ao Marcos e deixou o Fernando de verdade como voz nova. O modelo **distribui** os nomes que recebe entre as vozes, não confere se a voz é mesmo daquela pessoa. → Regra: sugestão automática só quando **todas** as vozes da reunião têm amostra na biblioteca. Se faltar alguém, todas as sugestões aparecem como "a confirmar" no ▶.
- **Casamento com a nota:** das 43 falas de "Outros", 29 ficaram com uma voz só, 8 mistas e 6 sem voz (falas curtas que o diarize não marcou). O número é pessimista: o teste usou os parágrafos já juntados da nota, com o fim estimado. No app, os trechos de ~12 s têm início e fim exatos. Fala sem voz herda a voz do vizinho mais próximo.

**Ponta a ponta (23/11, depois da V4):** a Planning 03 inteira pelo app (`python -m apolo --simulate others.wav --speed 4 --people "Marcos, Fernando, Camila"`), com API de verdade e uma biblioteca de teste com amostras do Marcos e do Fernando tiradas da 2ª metade da reunião. Resultado:
- 4 janelas de ~130 s de fala, separadas em 62 a 73 s cada, já com as 2 vozes como referência a partir da 2ª.
- Identificação automática certa (Voz A = Fernando, Voz B = Marcos), com a conferência ao contrário batendo.
- Todas as 36 falas dos "Outros" na nota saíram com nome. Conferindo por amostragem com a referência, estavam certas, inclusive a de 01:39 que a V0 tinha errado com os parágrafos da nota. O resumo já atribuiu as tarefas por nome.
- Em tempo real, cada janela (~65 s) fica pronta antes da próxima encher (2 min). No Parar, sobram até ~65 s da última janela e ~20 s da identificação.
- Falta testar numa reunião de verdade: as janelas "Quem está" e "Quem é quem" na tela, ▶ tocando, e alguém que não está na biblioteca.

### Fase V1: modelo e nota
`Segment.voice`, mapa de nomes, `speaker_labels`, `reparse`, prompt do resumo e o módulo da biblioteca (`voices.py`), tudo com testes. Ainda sem chamar a API.

### Fase V2: separação ao vivo
`DiarizeQueue`, janelas, referências entre janelas, `diar.jsonl`, retomar, falhas e sem conexão. Provider `diarize()` no `openai_provider.py`, com testes usando um provider falso.

### Fase V3: janela de parada
Janelinha "Quem está na reunião..." durante a gravação (3.6). Na janela de parada, seção "Quem é quem" com ▶, sugestões, "Não sei", juntar duas vozes que são a mesma pessoa e salvar amostras na biblioteca. O resumo espera os nomes, com prazo, e cai para "Voz A/B" se você fechar a janela.

### Fase V4: configurações e polimento
Aba Vozes, opção liga/desliga, aviso de privacidade, README e o teste real numa reunião com 3+ pessoas.

## 5. Riscos e limites

- **4 referências por chamada** no diarize. Afeta só manter as letras entre janelas com 5+ vozes (3.1). Para os nomes (3.3), resolve com mais chamadas curtas.
- O diarize **não aceita prompt** nem os "termos". Isso não afeta o texto, que continua vindo do `gpt-transcribe`.
- **Custo:** cada minuto do canal "Outros" é enviado **duas vezes no total**: uma para o `gpt-transcribe` (o texto, como hoje) e uma para o diarize (quem falou). As janelas são pedaços diferentes da reunião, então nada é reenviado. Numa reunião de 1 h, são 60 min transcritos + até 60 min separados (menos, porque os silêncios são cortados), mais alguns segundos de referência por janela. Usar só o diarize evitaria a segunda chamada, mas o texto dele saiu bem pior no teste. Conferir o preço por minuto do modelo na página de preços da OpenAI antes da V2.
- Fala sobreposta e áudio comprimido do Teams podem embaralhar vozes parecidas. A validação no fim existe por causa disso.
- Bluetooth no modo chamada (HFP) degrada o áudio, ver a memória do daily de 28/10. Vale medir na V0 se piora a separação.
- **Pessoas na mesma sala, num computador só:** o diarize separa normalmente, mas as vozes podem soar parecidas pelo mesmo microfone.
- Amostras de voz são **dado biométrico** pela LGPD. Ficam só no seu computador, com opção de apagar, e saem apenas como referência nas chamadas.

## 6. Decisões em aberto

1. **Reunião sem "Quem está" que acaba sendo 1:1:** a separação já rodou e é descartada. Aceitar esse gasto ou só separar quando "Quem está" tiver 2+ nomes? Na segunda opção, sem "Quem está" a separação toda fica para o fim, que é mais lento. Decidir na V0, medindo o custo.
2. **Janela de 4 min:** menor deixa o fim mais rápido, maior dá mais contexto ao modelo. Decidir na V0 com números.
