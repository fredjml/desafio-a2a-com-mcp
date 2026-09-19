# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (MBA Engenharia de Software com IA, curso de MCP e A2A): a Hill Valley Tech expõe as cinco salas de reunião como um **servidor MCP** (Streamable HTTP, porta `7301`) e como um **agente A2A** v1.0 (JSON-RPC, porta `7300`) que consome esse servidor por dentro, como host MCP. Quando a sala pedida está ocupada, o servidor MCP responde `input_required` (MRTR) com um `requestState` selado; o agente transforma isso em `TASK_STATE_INPUT_REQUIRED`, guarda o estado ligado à Task e, quando o cliente A2A responde `escolha=<sala>`, repete o `tools/call` com id novo. Tudo em Python, sem LLM: o mesmo pedido produz sempre o mesmo resultado.

```
.
├── README.md
├── dados/         (do starter, não alterado)
├── validador/     (do starter, não alterado)
├── exemplos/wire/ (do starter, não alterado)
├── servidor-mcp/  servidor MCP (app/, tests/, requirements*.txt, constraints.txt)
├── agente/        agente A2A + host MCP (app/, tests/, requirements*.txt, constraints.txt)
└── .github/workflows/validar.yml   CI: validador e testes em Python 3.10 e 3.12
```

## Pré-requisitos

- **Python 3.10 ou superior** para o servidor e o agente (o piso 3.10 é coberto pelo CI, que roda 3.10 e 3.12) e para o validador, que só usa a biblioteca padrão e não tem dependências.
- `git` e `curl` (nos comandos manuais). No Windows use `curl.exe` (não o alias do Windows PowerShell 5.1) e `python`/`py -3` no lugar de `python3`, que é um atalho da Microsoft Store.
- Versões travadas: `mcp==2.2.0`, `a2a-sdk==1.1.4`, `httpx==0.28.1` (`requirements.txt`), e todas as dependências transitivas fixadas em `constraints.txt`.

## Portas e variáveis de ambiente

Tudo vem do ambiente e falha rápido no boot se for inválido. Nenhuma variável tem o segredo como padrão.

| Processo | Variável | Padrão | Descrição |
| --- | --- | --- | --- |
| servidor MCP | `REQUEST_STATE_SECRET` | **obrigatória** | Chave do `requestState`: 32 bytes = **64 caracteres hexadecimais** (mínimo 32 bytes; o placeholder do `.env.example` é recusado). Também é recusado o segredo com menos de 16 bytes distintos, com padrão repetido de até 8 bytes (`deadbeef` x8, `00ff` x16) ou em progressão (`00 01 02 ...`); `secrets.token_hex(32)` nunca cai nessa regra. Nunca vai para o código nem para o repositório. |
| servidor MCP | `MCP_PORT` | `7301` | Porta do endpoint `/mcp`. |
| servidor MCP | `REQUEST_STATE_TTL_S` | `600` | Validade do `requestState` em segundos (aceita 1 a 1800). Só existe para testar expiração; ver "Decisões técnicas". |
| servidor MCP | `DADOS_DIR` | `<raiz>/dados` | Pasta com `salas.json`, `reservas.json` e `politica-de-uso.md`. |
| agente | `A2A_PORT` | `7300` | Porta do endpoint `/a2a` e do card em `/.well-known/agent-card.json`. |
| agente | `MCP_URL` | `http://localhost:7301/mcp` | URL do servidor MCP que o agente consome. |
| agente | `A2A_CARD_HOST` | `127.0.0.1` | Host que aparece no `url` da interface JSON-RPC do Agent Card; também é aceito no header `Host` (além de `localhost`, `127.0.0.1` e `[::1]`). |
| agente | `MCP_TIMEOUT_S` | `10` | Timeout, em segundos, de cada request MCP do agente (0,05 a 120). É o único timeout do host MCP. |
| agente | `A2A_MAX_TASKS` | `1000` | Teto de Tasks retidas em memória (1 a 1000000). Ao exceder, descarta as terminais menos recentes e, se ainda sobrar, as pausadas mais antigas. |
| agente | `A2A_PAUSA_TTL_S` | `660` | Validade do estado de uma Task pausada no agente, em segundos (1 a 86400): os 600 s do `requestState` + 60 s de margem. Se subir `REQUEST_STATE_TTL_S` no servidor, suba este valor também. |

O agente **não tem segredo**: o `requestState` é opaco para ele. O arquivo `servidor-mcp/.env.example` só documenta os nomes; o servidor não lê arquivos `.env`, então exporte as variáveis no terminal.

## Como rodar

São **dois processos** (o servidor MCP e o agente), cada um no seu terminal, mais um terceiro terminal para o validador e os comandos manuais. Os blocos abaixo cobrem bash (Linux/macOS) e PowerShell (Windows). Nenhum comando exige ativar o venv: chamamos o Python do venv pelo caminho.

### 1. Clonar

```bash
git clone https://github.com/fredjml/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

(o mesmo comando vale no PowerShell)

### 2. Criar os dois venvs e instalar (uma vez)

bash (Linux/macOS):

```bash
(cd servidor-mcp && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -c constraints.txt)
(cd agente       && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -c constraints.txt)
```

PowerShell (Windows):

```powershell
Set-Location servidor-mcp
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt
Set-Location ..\agente
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt
Set-Location ..
```

### 3. Gerar e exportar o `REQUEST_STATE_SECRET` (só o servidor MCP precisa)

A chave é gerada na hora, fica só na memória do terminal do servidor e **nunca** deve ser escrita em arquivo versionado, colada em chat ou impressa. O comando abaixo já a exporta sem mostrá-la.

bash:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

PowerShell:

```powershell
$env:REQUEST_STATE_SECRET = (python -c "import secrets; print(secrets.token_hex(32))")
```

Alternativas equivalentes para gerar os 64 hex: `py -3 -c "import secrets; print(secrets.token_hex(32))"` (Windows) ou `node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"`.

### 4. Subir os dois processos (dois terminais)

**Terminal 1, servidor MCP** (deixe o stderr visível: é a evidência de método, id e `traceparent` de cada request):

```bash
cd servidor-mcp
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/python -m app
```

```powershell
Set-Location servidor-mcp
$env:REQUEST_STATE_SECRET = (python -c "import secrets; print(secrets.token_hex(32))")
.\.venv\Scripts\python.exe -m app
```

**Terminal 2, agente** (não precisa do segredo; o MCP é contatado sob demanda, então o boot não exige o servidor no ar):

```bash
cd agente
.venv/bin/python -m app
```

```powershell
Set-Location agente
.\.venv\Scripts\python.exe -m app
```

Cada processo imprime uma linha JSON `"evento": "boot"` no stderr quando está escutando (servidor em `127.0.0.1` e `::1`, porta 7301; agente em 7300).

### 5. Rodar o validador

No terminal 3, na raiz do clone:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
echo "exit=$?"
```

```powershell
python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301   # ou: py -3 validador/validar.py ...
"exit=$LASTEXITCODE"
```

O esperado é `resumo: 36 passaram, 0 falharam, de 36 verificacoes` e exit code `0`. A primeira linha imprime o trace-id que o validador usa no header `traceparent`; procure esse valor no stderr do servidor MCP.

> **Atenção: processos recém-iniciados a cada rodada.** As reservas vivem na memória do servidor e mudam o resultado da rodada seguinte, então rodar o validador duas vezes seguidas **sem reiniciar** produz falsos negativos. Reinicie os dois processos antes de cada rodada.

### 6. Parar e reiniciar

Pare cada processo com `Ctrl+C` no seu terminal e suba de novo pelo passo 4. Para reiniciar **só o servidor MCP mantendo o mesmo segredo** (necessário no passo 12 abaixo), use o mesmo terminal do servidor: a variável `REQUEST_STATE_SECRET` continua exportada nele, então basta repetir o último comando do terminal 1. Para conferir que nada ficou escutando:

```bash
ss -ltn | grep -E ':(7300|7301) ' || echo "portas livres"
```

```powershell
Get-NetTCPConnection -LocalPort 7300,7301 -State Listen -ErrorAction SilentlyContinue
```

(sem saída = portas livres).

### 7. Verificação manual, como o avaliador

Reinicie os dois processos (estado zerado) e, no terminal 3, na raiz do clone, defina os auxiliares. Cada arquivo de `exemplos/wire/*.json` é um envelope `{descricao, request:{url,metodo,headers,body}, response}`: o que se envia é **`.request.body`** (com `.request.headers`), nunca o arquivo inteiro. A função `corpo` extrai só o corpo.

PowerShell:

```powershell
$A2A = 'http://localhost:7300/a2a'; $MCP = 'http://localhost:7301/mcp'
$TP  = 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'
$MCPH = '-H','Content-Type: application/json','-H','Accept: application/json, text/event-stream','-H','MCP-Protocol-Version: 2026-07-28','-H','Mcp-Method: tools/call','-H','Mcp-Name: reservar_sala'
function corpo($arq) { python -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['request']['body']))" $arq }
function a2a { $corpo = if ($args) { $args } else { $input }; $corpo | curl.exe -s -X POST $A2A -H 'Content-Type: application/json' -H $TP --data-binary '@-' }   # aceita o corpo por pipe ou como argumento
```

bash:

```bash
A2A=http://localhost:7300/a2a; MCP=http://localhost:7301/mcp
TP='traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'
MCPH=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: reservar_sala')
corpo() { python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['request']['body']))" "$1"; }
a2a() { curl -s -X POST "$A2A" -H 'Content-Type: application/json' -H "$TP" --data-binary @-; }
```

**Passo 3, Agent Card** (comparar com `exemplos/wire/07-a2a-agent-card.json`: `supportedInterfaces[0]` com `protocolBinding` `JSONRPC`, `protocolVersion` `1.0`, `url` terminando em `/a2a`, e a skill `reservar-sala`):

```bash
curl -s http://localhost:7300/.well-known/agent-card.json | python3 -m json.tool
```

```powershell
curl.exe -s http://localhost:7300/.well-known/agent-card.json | python -m json.tool
```

**Passo 7, sala ocupada** (garagem, 14h às 15h de 03/11/2026, corpo do wire 08). A Task fica em `TASK_STATE_INPUT_REQUIRED` e `status.message` traz exatamente `alternativas: sala-fusca, sala-mirante`:

```bash
corpo exemplos/wire/08-a2a-send-message.json | a2a | python3 -m json.tool
```

```powershell
corpo exemplos/wire/08-a2a-send-message.json | a2a | python -m json.tool
```

Guarde o `result.task.id` da resposta em `TASK` (`TASK=task-...` no bash, `$TASK = 'task-...'` no PowerShell).

**Passo 8, continuação `escolha=` e `GetTask`** (a continuação usa o mesmo formato do wire 10; a Task termina `TASK_STATE_COMPLETED` com o artifact `reserva` em `sala-mirante` e `politica` `2026-11-01`):

```bash
echo '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"msg-manual-1","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"'"$TASK"'"}}}' | a2a | python3 -m json.tool
echo '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"'"$TASK"'"}}' | a2a | python3 -m json.tool
```

```powershell
a2a ('{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"msg-manual-1","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"' + $TASK + '"}}}') | python -m json.tool
a2a ('{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"' + $TASK + '"}}') | python -m json.tool
```

**Passo 9, recusa:** repita o passo 7 (agora só `alternativas: sala-fusca`, porque o passo 8 reservou o mirante), guarde o novo `TASK` e envie o mesmo JSON do passo 8 com `"escolha=recusar"` no lugar de `"escolha=sala-mirante"` e outro `messageId`. Resultado: `TASK_STATE_CANCELED`.

**Passo 10, sala inexistente:** `TASK_STATE_FAILED` com `Sala inexistente: sala-inexistente` no `status.message` e no `history`:

```bash
echo '{"jsonrpc":"2.0","id":4,"method":"SendMessage","params":{"message":{"messageId":"msg-manual-3","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-inexistente inicio=2026-11-03T09:00:00-03:00 fim=2026-11-03T10:00:00-03:00 responsavel=Doc"}]}}}' | a2a | python3 -m json.tool
```

```powershell
a2a '{"jsonrpc":"2.0","id":4,"method":"SendMessage","params":{"message":{"messageId":"msg-manual-3","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-inexistente inicio=2026-11-03T09:00:00-03:00 fim=2026-11-03T10:00:00-03:00 responsavel=Doc"}]}}}' | python -m json.tool
```

**Passos 11 e 12, `requestState` direto no MCP.** Reinicie os dois processos antes (o passo 8 ocupou o mirante). Primeiro pegue o conflito (wire 03) e guarde a resposta em arquivo; ela traz a chave de `inputRequests` (atribuída pelo servidor) e o `requestState`:

```bash
corpo exemplos/wire/03-tools-call-conflito-input-required.json | curl -s -X POST "$MCP" "${MCPH[@]}" --data-binary @- > /tmp/conflito.json
```

```powershell
corpo exemplos/wire/03-tools-call-conflito-input-required.json | curl.exe -s -X POST $MCP @MCPH --data-binary '@-' > "$env:TEMP\conflito.json"
```

O auxiliar `retry` monta o retry do wire 04 (id novo, `inputResponses` com a **chave recebida**, `requestState` recebido). Com um terceiro argumento qualquer, troca **um caractere no meio** do `requestState`:

```bash
retry() { python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["result"]; b=json.load(open(sys.argv[2]))["request"]["body"]; p=b["params"]; p["inputResponses"]={next(iter(r["inputRequests"])): next(iter(p["inputResponses"].values()))}; s=r["requestState"]; p["requestState"]=(s[:30]+("A" if s[30]!="A" else "B")+s[31:]) if len(sys.argv)>3 else s; print(json.dumps(b))' /tmp/conflito.json exemplos/wire/04-tools-call-retry.json "$@"; }
```

```powershell
function retry { python -c "import json,sys; r=json.load(open(sys.argv[1]))['result']; b=json.load(open(sys.argv[2]))['request']['body']; p=b['params']; p['inputResponses']={next(iter(r['inputRequests'])): next(iter(p['inputResponses'].values()))}; s=r['requestState']; p['requestState']=(s[:30]+('A' if s[30]!='A' else 'B')+s[31:]) if len(sys.argv)>3 else s; print(json.dumps(b))" "$env:TEMP\conflito.json" exemplos/wire/04-tools-call-retry.json @args }
```

- **Passo 11, adulterado:** resposta `error.code` `-32602` (HTTP 400, nunca 500), mensagem `Invalid or expired requestState`.

  ```bash
  retry adulterar | curl -s -i -X POST "$MCP" "${MCPH[@]}" --data-binary @-
  ```

  ```powershell
  retry adulterar | curl.exe -s -i -X POST $MCP @MCPH --data-binary '@-'
  ```

- **Passo 12, restart do MCP entre o pedido e o retry:** pare **só o servidor MCP** (`Ctrl+C` no terminal 1) e suba de novo com o **mesmo** `REQUEST_STATE_SECRET` (o mesmo terminal já o tem exportado). Envie o retry **sem alterar** o estado: `resultType` `complete`, `reservado` `true`.

  ```bash
  retry | curl -s -i -X POST "$MCP" "${MCPH[@]}" --data-binary @-
  ```

  ```powershell
  retry | curl.exe -s -i -X POST $MCP @MCPH --data-binary '@-'
  ```

**Passo 13, cliente sem capability de elicitation** (o corpo do wire 06 traz `clientCapabilities: {}`): HTTP `400`, `error.code` `-32021` e `error.data.requiredCapabilities`. Reinicie os dois processos antes, para o conflito da garagem existir de novo:

```bash
corpo exemplos/wire/06-erro-32021-sem-elicitation.json | curl -s -i -X POST "$MCP" "${MCPH[@]}" --data-binary @-
```

```powershell
corpo exemplos/wire/06-erro-32021-sem-elicitation.json | curl.exe -s -i -X POST $MCP @MCPH --data-binary '@-'
```

**Passos 5 e 6, no stderr do servidor MCP.** Cada request HTTP vira uma linha JSON com `method`, `id`, `traceparent`, `mcp_name` e `client`. Depois de uma rodada do validador, procure o trace-id que ele imprimiu: o primeiro `tools/call` do agente (`client` = `agente-central-de-salas`) vem **depois** de um `tools/list`, e o par de `tools/call` de uma reserva que passou pela pausa tem `id` diferente no retry. Para poder pesquisar depois, suba o servidor gravando uma cópia do stderr (a extensão `.stderr.txt` já é ignorada pelo git):

```bash
.venv/bin/python -m app 2>&1 | tee servidor-mcp.stderr.txt
```

```powershell
.\.venv\Scripts\python.exe -m app 2>&1 | Tee-Object -FilePath servidor-mcp.stderr.txt
```

### 8. Testes e qualidade (opcional)

O CI (`.github/workflows/validar.yml`) roda o validador e os testes em Python 3.10 e 3.12. Localmente, em cada pasta (servidor primeiro: os testes do agente sobem o servidor real com `servidor-mcp/.venv`):

```bash
cd servidor-mcp && .venv/bin/python -m pip install -r requirements-dev.txt -c constraints.txt && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy --strict app && .venv/bin/python -m bandit -q -r app
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.txt; .\.venv\Scripts\python.exe -m pytest -q; .\.venv\Scripts\python.exe -m ruff check .; .\.venv\Scripts\python.exe -m mypy --strict app; .\.venv\Scripts\python.exe -m bandit -q -r app
```

O CI roda também `mypy --strict app` e `bandit -q -r app` em cada pasta. Os testes do agente sobem o servidor MCP real com o venv do servidor: **sem `servidor-mcp/.venv` a suíte do agente falha** (não pula em silêncio). Só a variável explícita `AGENTE_TESTS_SEM_SERVIDOR=1` os pula, conscientemente.

## Onde a ponte acontece

**Servidor MCP.** O conflito nasce em `servidor-mcp/app/mrtr.py`: o resolvedor `escolha_de_sala` (criado por `criar_resolvedor`) devolve `Elicit(...)` quando o intervalo está ocupado e há alternativa; o SDK então responde `resultType: input_required` com uma única `inputRequests` (form mode, `sala` restrita ao `enum` das alternativas) e o `requestState` selado. O selo é montado em `servidor-mcp/app/server.py:criar_servidor` (`RequestStateSecurity`). O servidor não guarda nada entre a pergunta e o retry.

**Agente, do `input_required` para `TASK_STATE_INPUT_REQUIRED`.** Em `agente/app/tasks.py`, `ExecutorReservas._nova` chama o MCP por `McpHost.chamar_ferramenta` (`agente/app/mcp_host.py`, que usa `session.call_tool(..., allow_input_required=True)` e por isso recebe o `InputRequiredResult` **cru**, sem callback). Se o resultado é um `InputRequiredResult`, `ExecutorReservas._pausar` o traduz: `extrair_pergunta` lê a chave, o `enum` e o `requestState` **recebidos**; `PausedRegistry.guardar` grava um `PausedState` por `task_id` (com o `requestState` opaco, a chave de `inputRequests`, os argumentos originais e o trace-id); e `_Saida.pausou` publica `TaskState.TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: a, b, c` (`texto_alternativas`). O `requestState` nunca aparece em resposta A2A nem em log.

**Agente, o `requestState` volta ao servidor no retry.** Quando chega o `SendMessage` com `escolha=<sala>` (mesma Task), `ExecutorReservas._continuacao` lê o `PausedState`, valida a escolha contra as opções recebidas e chama `McpHost.chamar_ferramenta(estado.tool_name, dict(estado.original_arguments), trace_id=..., input_responses={estado.input_request_key: resposta}, request_state=estado.request_state)`. O `McpHost` repassa `input_responses` e `request_state` a `session.call_tool`, e o SDK do cliente emite um `tools/call` com **id JSON-RPC novo** (o do retry difere do inicial), a mesma chave em `inputResponses` e o `requestState` ecoado sem alteração. `escolha=recusar` vira `ElicitResult(action="decline")` e a Task termina em `TASK_STATE_CANCELED`.

```mermaid
sequenceDiagram
    participant C as Cliente A2A
    participant A as Agente 7300
    participant M as Servidor MCP 7301
    C->>A: SendMessage "reservar sala=sala-garagem ..." (traceparent)
    A->>A: cria a Task (SUBMITTED, WORKING)
    A->>M: tools/list (id 1)
    A->>M: resources/read politica://uso (id 2)
    A->>M: tools/call reservar_sala (id 3)
    M-->>A: input_required: inputRequests[K] + requestState S
    Note over A: PausedState por Task: K, S (opaco), enum, argumentos, trace-id
    A-->>C: Task INPUT_REQUIRED, "alternativas: sala-fusca, sala-mirante"
    C->>A: SendMessage taskId + "escolha=sala-mirante"
    A->>M: tools/call reservar_sala (id 4, NOVO) + inputResponses[K] + requestState S
    M->>M: verifica selo, validade e vínculo; reconstrói o pedido do próprio estado
    M-->>A: complete, reservado true
    A-->>C: Task COMPLETED, artifact "reserva" com a politica
    Note over M: requestState adulterado ou expirado: erro -32602
```

A ponte aceita **várias rodadas**: se o retry devolve outro `input_required` (o servidor revalida a cada rodada e as alternativas podem ter mudado), o agente atualiza chave, `enum` e `requestState` do `PausedState` e mantém a Task pausada. O agente não tem regra de sala: conflito, política e alternativas são decisão do servidor; o agente só traduz protocolo.

## Decisões técnicas

**Proteção do `requestState`.** `servidor-mcp/app/server.py:criar_servidor` monta o `MCPServer` com `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)`, do SDK `mcp` 2.2.0 (`mcp/server/request_state.py`): o estado é cifrado e autenticado com **AES-256-GCM**, com a chave derivada por **HKDF-SHA256** do segredo. O selo é vinculado ao **método**, ao **nome da tool** e ao **digest dos argumentos**, então mexer nos argumentos do retry invalida o estado. Adulterado, expirado ou de outra chave, o estado é rejeitado com **`-32602` "Invalid or expired requestState"** (HTTP 400, nunca 500). Sem `keys=`, o SDK usaria uma **chave process-local aleatória**: o estado não sobreviveria a um restart e `REQUEST_STATE_SECRET` seria ignorado sem aviso. Por isso a configuração é sempre explícita, o boot falha sem o segredo (ausente, placeholder, não hexadecimal, menor que 32 bytes ou sem aleatoriedade: menos de 16 bytes distintos, padrão repetido de até 8 bytes ou progressão aritmética) e há teste de restart do servidor. O segredo só entra por variável de ambiente e nunca é impresso, nem o valor nem o tamanho.

**Por quanto tempo vale.** 600 s (10 min, dentro da faixa de 5 a 30 min do enunciado). `REQUEST_STATE_TTL_S` (1 a 1800 s) existe só para os testes de expiração. O estado é reutilizável dentro da validade: **não há nonce**, e o servidor não guarda estado nenhum; repetir um estado antigo reexecuta a revalidação (se a sala já foi reservada, o servidor pergunta de novo em vez de reservar em duplicidade). O `tools/call` **nunca é repetido pelo host** por falha de transporte (só leituras são refeitas uma vez), para não duplicar reserva.

**Elicit/Resolve no servidor.** A reserva usa `Elicit`/`Resolve` do SDK: o resolvedor decide entre `Elicit` (conflito com alternativa), `SemConflito` (livre), recusa do usuário (`decline`/`cancel` concluem sem reservar e sem `isError`) e erro de execução (`isError` com o texto exato, sem o prefixo `Error executing tool`). Sem alternativa não há elicitation. `responsavel` é obrigatório, de 1 a 200 caracteres (só espaços conta como vazio); violação vira `isError` com `Formato invalido: responsavel deve ter de 1 a 200 caracteres` (texto próprio; não é uma das cinco mensagens do enunciado) e não gera pergunta nem reserva. Sem `elicitation.form` declarado, o servidor responde `-32021` com `data.requiredCapabilities` e HTTP 400 e não reserva nada.

**Onde ficam as Tasks.** Em **memória do agente**: o `TaskStore` do a2a-sdk (`InMemoryTaskStore`, especializado em `TaskStoreCoerente`) guarda a Task, e o estado da ponte fica em `PausedRegistry`, um **dict por `task_id` fora do `TaskStore`**, para nunca vazar em `GetTask`. Reiniciar o agente perde as Tasks (a persistência não é exigida). A memória é limitada, sem threads nem timers:

- **Teto de Tasks** (`A2A_MAX_TASKS`, padrão 1000): ao exceder, o `TaskStoreCoerente` descarta primeiro as terminais menos recentemente atualizadas e, se ainda sobrar, as pausadas mais antigas (levando junto o `PausedState`). Tasks em execução e a que acabou de ser salva nunca são descartadas. `GetTask` de uma Task descartada devolve "não encontrada".
- **Expurgo de pausas vencidas** (`A2A_PAUSA_TTL_S`, padrão 660 s = 600 s do `requestState` + 60 s de margem): o `PausedState` vale `A2A_PAUSA_TTL_S` a partir da última rodada. A varredura é **preguiçosa**: roda a cada `SendMessage`, sem thread. Continuar uma Task cuja pausa venceu termina em `TASK_STATE_FAILED` com "Estado expirado; reenvie o pedido..." e **sem chamar o MCP** (o estado já não vale no servidor). Se o `requestState` expirar antes no servidor, a retomada termina em `TASK_STATE_FAILED` com a mensagem do servidor (`-32602`).

**Falha de transporte na continuação (não perde a pausa).** Se o MCP estiver fora do ar (conexão recusada ou timeout) quando o usuário envia `escolha=<v>`, a Task **permanece** `TASK_STATE_INPUT_REQUIRED`, o `PausedState` fica **intacto** e a mensagem é `Servidor MCP indisponivel; reenvie escolha=<v>`. O usuário reenvia quando o servidor voltar: o `requestState` ainda vale por até 10 min. O retry de `tools/call` pode até ter chegado ao servidor; ele revalida a cada rodada e não duplica a reserva. Só terminam em `TASK_STATE_FAILED` o erro de protocolo (`requestState` inválido/expirado, `-32602`) e o `isError` da tool. Na primeira chamada (sem pausa) uma falha de transporte continua terminando a Task em `TASK_STATE_FAILED` com erro claro.

**Camada A2A (`a2a-sdk` 1.1.4).**
- `ContextoComVersaoPadrao` injeta `A2A-Version: 1.0` quando o header falta (o validador não o envia; sem isso o SDK assume 0.3 e recusa tudo com `-32009`). Outra versão explícita continua sendo recusada.
- Na continuação o validador não manda `contextId`; `resolver_context_id` usa o `context_id` da Task guardada (o gerado pelo SDK faria o `TaskManager` recusar o evento).
- `GetTask` devolve `result.task`, idêntico ao wire 09 (`DespachanteComGetTaskNoWire` sobrescreve `_handle_get_task`, método interno do SDK; a versão está travada e um teste confere o formato). Decisão consciente: o validador aceita as duas formas, mas o wire 09 usa `result.task`. **Um bump de `a2a-sdk` exige rerodar os testes (em especial os e2e)**, porque este método e o `TaskStoreCoerente` dependem de comportamento interno do SDK.
- `ListTasks` está **desabilitado** (responde `-32601`): fica fora do contrato do desafio (só `SendMessage` e `GetTask`) e enumerava todas as Tasks, com histórico, a qualquer cliente.
- **Proteção de entrada** (`agente/app/seguranca.py`, no molde do servidor MCP, porque loopback não protege contra o navegador do usuário): `POST /a2a` exige `Content-Type: application/json` (400); `Host` só de loopback (`localhost`, `127.0.0.1`, `[::1]`, com ou sem porta) ou `A2A_CARD_HOST` (421); `Origin` ausente é aceito e, presente, só de loopback (403); corpo acima de 4 MiB é recusado com 413, contado em blocos, sem carregar tudo em memória; JSON inválido ou aninhado demais (ex.: 100 KB de `[`) devolve `400` com `-32700` bem-formado, sem traceback nem texto interno no corpo. O validador (`urllib`, `Host: localhost:7300`, sem `Origin`, `application/json`) passa sem mudanças e o `GET` do card segue sem `Content-Type`.
- `SendMessage` com `contextId` explícito que não bate com o da Task indicada por `taskId` é recusado (`-32602`, A2A v1.0), sem tocar no `PausedState` nem chamar o MCP; `contextId` ausente continua aceito (vale o da Task). Texto vazio (ou só espaços, ou parte sem `text`) e `role` diferente de `ROLE_USER` viram `TASK_STATE_FAILED` com mensagem clara (numa Task pausada, mantêm a pausa), sem traceback.
- `TaskStoreCoerente.save` normaliza o `history` (o SDK duplicava a mensagem do agente na continuação e carimbava um `contextId` novo na mensagem do usuário).
- Ids do retry e recriação do Client (`McpHost.evitar_ids`): um `Client` MCP novo recomeça os ids JSON-RPC em 1. O host só recria o Client em falha de **transporte** (erro de protocolo devolvido pelo servidor e erro lógico como ferramenta ausente ou resposta inválida **não** o recriam). Se o Client foi recriado entre a pausa e a retomada, o host "queima" ids com `tools/list` até o retry não poder coincidir com o id da chamada inicial (id novo é regra do enunciado). Se não der para garantir, o retry **não é enviado** e a Task falha com mensagem clara.
- Erros do host por tipo (`MCPError`, `httpx.HTTPError`, `OSError`, `TimeoutError`): um bug de programação não vira "servidor indisponível"; o executor loga o traceback no stderr (sem o `requestState`) e a Task termina em `TASK_STATE_FAILED` com "Erro interno do agente". Cancelar a execução marca a Task como `TASK_STATE_FAILED` antes de repropagar.
- Estado terminal é definitivo: `SendMessage` numa Task terminal é recusado com erro.

**Host MCP sem sessão.** O agente usa `Client(url, mode="2026-07-28")`: nenhum `initialize`, nenhum `Mcp-Session-Id`. Chama `list_tools()` explicitamente antes do primeiro `tools/call` (o SDK só listaria depois) e lê `politica://uso` a cada Task para extrair a versão. Todo request leva `_meta` com versão, capabilities e `traceparent`. O trace-id da Task é o do header `traceparent` do primeiro `SendMessage` (ou um gerado) e fica fixo nela; cada request MCP leva um span-id novo, e um `traceparent` diferente na continuação não o troca.

**Rede e log.** Servidor e agente escutam em `127.0.0.1` **e** `::1` (dois sockets), nunca em todas as interfaces; só IPv4 faria `localhost` custar cerca de 2 s por request em clientes que tentam `::1` primeiro, e `host="::"` no Windows escuta só IPv6. O log é um **wrapper ASGI próprio** que escreve uma linha JSON no **stderr** por request HTTP: método, id, `traceparent`, `Mcp-Name` e `clientInfo` no servidor; método, id, trace-id e `A2A-Version` no agente. O log do middleware do SDK não serve (registra um `tools/list` fantasma a cada `tools/call`) e o access log do uvicorn vai para stdout sem esses campos. O log sai em ASCII (`ensure_ascii`), então `U+0085`, `U+2028` e controles chegam escapados e nenhuma linha se quebra em duas, nem com `splitlines()`. JSON aninhado demais no request não derruba o log nem o processo. Nunca entram no log `requestState`, `inputResponses`, argumentos das tools nem o segredo.

**Python 3.10 como piso.** `requires-python = ">=3.10"`, `ruff target-version = "py310"` e `mypy python_version = "3.10"`; o CI roda a suíte e o validador em 3.10 e 3.12.

### Limitações reais do SDK (com evidência)

**O cliente `mcp` 2.2.0 só declara `elicitation` se houver `elicitation_callback`, e sempre como `{"form": {}, "url": {}}`.** O `Client` deriva `clientCapabilities` da existência do callback, em `mcp/client/session.py` (linhas 634 a 638 do pacote instalado no venv do agente):

```python
        elicitation = (
            types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
            if self._elicitation_callback is not _default_elicitation_callback
            else None
        )
```

e o carimbo de cada request sobrescreve `_meta["io.modelcontextprotocol/clientCapabilities"]` com esse valor (`_make_modern_stamp`, `stamp`, linhas 161 a 166 do mesmo arquivo). Não há como declarar só `elicitation.form` sem callback, nem sem API privada. Como o enunciado exige que o agente declare a capability de elicitation em form mode e, ao mesmo tempo, veja o `input_required` cru, o agente registra um **callback de fachada** (`McpHost._fachada_elicitation`, em `agente/app/mcp_host.py`) só para o SDK anunciar a capability. A fachada **nunca é invocada**: o agente chama `session.call_tool(..., allow_input_required=True)` e recebe o `InputRequiredResult` cru; só o auto-driver `Client.call_tool` (proibido aqui) a acionaria. Há teste que prova (o contador `invocacoes_da_fachada` fica em 0 em todos os fluxos, inclusive pausa e retomada). O servidor aceita a declaração `{form, url}`, porque só exige `elicitation.form`. Se um dia a fachada for invocada, ela responde erro em vez de escolher pelo usuário.

Nota sobre o servidor: `{}` e `{"elicitation": {}}` **não** contam como capability de form (o SDK aceitaria a segunda); a checagem própria em `servidor-mcp/app/mrtr.py:declarou_elicitation_form` exige `elicitation.form`.

## Saída do validador

Última execução: 2026-09-19, a partir de um clone limpo, com os dois processos recém-iniciados (servidor MCP e agente, segredo gerado na hora), Windows 11 + PowerShell 7, Python 3.12.10, `mcp` 2.2.0, `a2a-sdk` 1.1.4. Duas rodadas seguidas, com reinício dos processos entre elas, deram a mesma saída (exceto o trace-id, aleatório a cada execução); exit code `0` nas duas. A prova em Python 3.10 (o piso do enunciado) vai pelo CI (`.github/workflows/validar.yml`), que roda o validador em 3.10 e 3.12.

```text
trace-id desta execucao: 125d49878d83ab321e34b9825146e993
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Exit code: `0`
