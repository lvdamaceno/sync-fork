# sync-forks

Verifica se seus forks no GitHub estão atualizados com o repositório original e, se não estiverem, faz o sync automaticamente.

## Configuração dos forks

Edite `forks.json` (ou coloque o mesmo JSON na variável de ambiente `FORKS_CONFIG`, que tem prioridade):

```json
[
  { "fork": "seu-usuario/projeto-a", "upstream": "org-original/projeto-a", "branch": "main" },
  { "fork": "seu-usuario/projeto-b" }
]
```

| Campo             | Obrigatório | Padrão                                   |
|-------------------|-------------|------------------------------------------|
| `fork`            | sim         | — (`dono/repo` ou URL do GitHub)         |
| `upstream`        | não         | repositório pai do fork                  |
| `branch`          | não         | branch padrão do fork                    |
| `upstream_branch` | não         | igual a `branch` (ou o padrão do upstream) |

## Token do GitHub

Crie um token em https://github.com/settings/tokens:

- **Fine-grained**: acesso aos forks com `Contents: Read and write` (+ `Workflows: Read and write` se o upstream altera `.github/workflows`).
- **Classic**: escopos `repo` e `workflow`.

## Notificações no Telegram (opcional)

1. No Telegram, fale com o [@BotFather](https://t.me/BotFather), use `/newbot` e copie o token.
2. Mande qualquer mensagem para o bot (ou adicione-o a um grupo e mande uma mensagem lá).
3. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id` (em grupos ele é negativo).
4. Defina as variáveis de ambiente `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.

Avisos enviados:

- 🔄 fork sincronizado (desative com `TELEGRAM_NOTIFY_SYNC=0`);
- ❌ falha ao sincronizar (ex.: conflito). O mesmo erro só é avisado uma vez, não a cada minuto;
- ✅ o fork voltou a sincronizar depois de uma falha.

## Rodar localmente

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GITHUB_TOKEN=ghp_xxx .venv/bin/python sync_forks.py          # roda uma vez
GITHUB_TOKEN=ghp_xxx .venv/bin/python sync_forks.py --loop   # roda a cada 60s
```

## Deploy no Render

1. Suba esta pasta para um repositório no GitHub.
2. No Render: **New > Blueprint** e selecione o repositório. O `render.yaml` cria um **Background Worker** que roda `python sync_forks.py --loop` e verifica os forks a cada `INTERVAL_SECONDS` (padrão: 60s).
3. No painel do Render, informe a variável de ambiente `GITHUB_TOKEN` (e, opcionalmente, `FORKS_CONFIG`). O token nunca deve ser commitado no repositório.

Falhas de sync (ex.: conflito de merge) aparecem como `ERROR` nos logs do serviço; o processo continua rodando e tenta novamente no próximo ciclo.
