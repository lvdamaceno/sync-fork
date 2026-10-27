#!/usr/bin/env python3
"""Mantém forks do GitHub sincronizados com o repositório original (upstream).

Para cada par fork/upstream configurado, o script:
  1. compara o branch do fork com o branch do upstream;
  2. se o fork estiver atrasado, sincroniza (fast-forward ou merge);
  3. registra o resultado no log.

Configuração (variáveis de ambiente):
  GITHUB_TOKEN      token com permissão de escrita nos forks (obrigatório)
  FORKS_CONFIG      JSON com a lista de forks (tem prioridade sobre o arquivo)
  FORKS_FILE        caminho do arquivo JSON (padrão: forks.json ao lado do script)
  LOOP              "1" para rodar em loop contínuo (modo Background Worker)
  INTERVAL_SECONDS  intervalo entre execuções no modo loop (padrão: 60)
  TELEGRAM_BOT_TOKEN  token do bot do Telegram (opcional, ativa as notificações)
  TELEGRAM_CHAT_ID    chat/grupo que recebe as notificações
  TELEGRAM_NOTIFY_SYNC  "0" para não avisar syncs bem-sucedidos (padrão: "1")
  NOTIFY_SOURCE       nome exibido no topo das notificações
                      (padrão: nome do serviço no Render, ou "sync-fork")
  UNAVAILABLE_ALERT_AFTER  falhas de conexão seguidas antes de avisar (padrão: 3)
  TOKEN_WARN_DAYS     dias de antecedência para avisar que o token vai vencer (padrão: 7)

Formato de cada item da lista:
  {
    "fork": "meu-usuario/projeto",        # obrigatório ("dono/repo" ou URL do GitHub)
    "upstream": "org-original/projeto",   # opcional (padrão: repo pai do fork)
    "branch": "main",                     # opcional (padrão: branch padrão do fork)
    "upstream_branch": "main"             # opcional (padrão: igual a "branch",
                                          #  ou branch padrão do upstream)
  }
"""

import html
import json
import re
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://api.github.com"
DEFAULT_FORKS_FILE = Path(__file__).with_name("forks.json")

log = logging.getLogger("sync-forks")


def retrying_session():
    """Sessão que repete na hora conexões derrubadas e erros 502/503/504 do servidor.

    Evita que uma conexão keep-alive fechada pelo GitHub entre os ciclos vire um erro.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=(502, 503, 504),
        allowed_methods=None,  # as escritas usadas (merge, fast-forward) podem ser repetidas
        raise_on_status=False,  # esgotadas as tentativas, a resposta segue para o tratamento normal
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class SyncError(Exception):
    """Erro de sync com uma explicação amigável e o que fazer para resolver.

    kind "auth", "rate_limit" e "unavailable" afetam todos os forks; os demais, só um.
    """

    def __init__(self, message, friendly=None, action=None, kind="other"):
        super().__init__(message)
        self.friendly = friendly or message
        self.action = action
        self.kind = kind


GLOBAL_ERRORS = {
    "auth": ("Problema com o token do GitHub", "O token do GitHub voltou a funcionar"),
    "rate_limit": ("Limite da API do GitHub atingido", "O limite da API do GitHub foi liberado"),
    "unavailable": ("GitHub indisponível", "A conexão com o GitHub voltou ao normal"),
}

MAX_COMMITS_LISTED = 10  # commits mostrados na notificação de sync
MAX_COMMIT_TITLE = 120  # caracteres da primeira linha de cada commit
TOKEN_WARN_DAYS = int(os.environ.get("TOKEN_WARN_DAYS", "7"))  # antecedência do aviso de token vencendo

RETRY_LATER = "Nada a fazer: o script tenta de novo sozinho no próximo ciclo."
WORKFLOW_ACTION = (
    "Dê ao token a permissão <b>Workflows: Read and write</b> (fine-grained) "
    "ou o escopo <b>workflow</b> (classic) e atualize o GITHUB_TOKEN no Render."
)


def api_error(status, message, detail):
    if status == 401:
        return SyncError(
            detail,
            "O token do GitHub está errado ou venceu.",
            "Gere um novo token no GitHub e atualize a variável <b>GITHUB_TOKEN</b> "
            "no Render (serviço sync-forks → Environment).",
            "auth",
        )
    if "workflow" in message.lower():
        return SyncError(
            detail,
            "O repositório original alterou arquivos de <code>.github/workflows</code> "
            "e o token não tem permissão para atualizar workflows.",
            WORKFLOW_ACTION,
            "permission",
        )
    if status == 403:
        return SyncError(
            detail,
            "O token do GitHub não tem permissão para acessar ou alterar este repositório.",
            "Confira se o token inclui o fork e tem a permissão <b>Contents: Read and write</b>.",
            "permission",
        )
    if status == 404:
        return SyncError(
            detail,
            "O repositório ou branch não foi encontrado, ou o token não tem acesso a ele.",
            "Confira os nomes no <code>forks.json</code> e se o token inclui o fork "
            "e o repositório original.",
            "not_found",
        )
    if status >= 500:
        return SyncError(detail, f"O GitHub está instável no momento (HTTP {status}).", RETRY_LATER, "unavailable")
    return SyncError(detail, f"O GitHub recusou a operação (HTTP {status}): {message}")


def conflict_error(label):
    return SyncError(
        f"{label}: conflito de merge",
        "O fork tem alterações que conflitam com o repositório original, "
        "então o sync automático não é possível.",
        "Resolva o conflito manualmente (merge local ou botão <b>Sync fork</b> no GitHub). "
        "Depois disso o script volta a sincronizar sozinho.",
        "conflict",
    )


def parse_token_expiration(value):
    """Lê o cabeçalho github-authentication-token-expiration ("2026-12-31 23:59:59 UTC")."""
    try:
        return datetime.strptime(value.replace("UTC", "+0000"), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        log.debug("Validade do token em formato desconhecido: %r", value)
        return None


class GitHub:
    def __init__(self, token):
        self.token_expires_at = None
        self.token_warned_on = None  # dia do último aviso de token vencendo
        self.etags = {}  # path -> (ETag, JSON): respostas 304 não contam no limite da API
        self.session = retrying_session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sync-forks",
            }
        )

    def request(self, method, path, ok=(200,), **kwargs):
        try:
            resp = self.session.request(method, API_URL + path, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise SyncError(
                f"{method} {path} -> {type(exc).__name__}: {exc}",
                "Não foi possível conectar ao GitHub.",
                RETRY_LATER,
                "unavailable",
            ) from exc
        expiration = resp.headers.get("github-authentication-token-expiration")
        if expiration:
            self.token_expires_at = parse_token_expiration(expiration)
        if resp.status_code in ok:
            return resp
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            minutes = max(1, round((reset - time.time()) / 60))
            raise SyncError(
                f"limite da API do GitHub atingido (libera em ~{minutes} min)",
                "O limite de requisições da API do GitHub foi atingido.",
                f"{RETRY_LATER} Se acontecer com frequência, aumente <b>INTERVAL_SECONDS</b> no Render.",
                "rate_limit",
            )
        message = error_message(resp)
        raise api_error(resp.status_code, message, f"{method} {path} -> HTTP {resp.status_code}: {message}")

    def get(self, path):
        cached = self.etags.get(path)
        headers = {"If-None-Match": cached[0]} if cached else {}
        resp = self.request("GET", path, ok=(200, 304), headers=headers)
        if resp.status_code == 304:
            return cached[1]
        data = resp.json()
        if resp.headers.get("ETag"):
            self.etags[path] = (resp.headers["ETag"], data)
        return data


def error_message(resp):
    try:
        return resp.json().get("message", resp.text)
    except ValueError:
        return resp.text


class TelegramNotifier:
    """Envia avisos ao Telegram sem repetir o mesmo erro a cada ciclo."""

    def __init__(self, token, chat_id, notify_sync=True, source="sync-fork", unavailable_after=3):
        self.token = token
        self.chat_id = chat_id
        self.notify_sync = notify_sync
        self.source = source
        self.unavailable_after = unavailable_after
        self.session = retrying_session()
        self.last_errors = {}  # chave -> (assinatura do erro, mensagem de recuperação)
        self.unavailable_streak = 0  # ciclos seguidos sem conseguir falar com o GitHub

    @classmethod
    def from_env(cls):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            if token or chat_id:
                log.warning("Telegram desativado: defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID")
            return None
        source = os.environ.get("NOTIFY_SOURCE") or os.environ.get("RENDER_SERVICE_NAME") or "sync-fork"
        return cls(
            token,
            chat_id,
            os.environ.get("TELEGRAM_NOTIFY_SYNC", "1") != "0",
            source,
            max(1, int(os.environ.get("UNAVAILABLE_ALERT_AFTER", "3"))),
        )

    def send(self, text):
        try:
            resp = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": f"📦 <b>{html.escape(self.source)}</b>\n\n{text}",
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if not resp.ok:
                log.warning("Falha ao notificar no Telegram: HTTP %d %s", resp.status_code, resp.text)
        except requests.RequestException as exc:
            # A mensagem da exceção pode conter a URL com o token, então não é logada.
            log.warning("Falha ao notificar no Telegram: %s", type(exc).__name__)

    def synced(self, fork, detail, commits=(), compare_url=None):
        self.ok(fork)
        if self.notify_sync:
            lines = ["🔄 <b>Fork atualizado</b>", html.escape(detail)]
            if commits:
                lines += ["", "📝 <b>Commits:</b>", *format_commits(commits)]
            if compare_url:
                lines += ["", f'<a href="{html.escape(compare_url)}">Ver alterações no GitHub</a>']
            self.send("\n".join(lines))

    def ok(self, fork):
        self.unavailable_streak = 0
        self._recovered([fork, *GLOBAL_ERRORS])

    def failed(self, key, title, recovered, error, forks=()):
        if key == "unavailable":
            # Quedas isoladas se resolvem sozinhas no ciclo seguinte: só avisa se persistirem.
            self.unavailable_streak += 1
            if self.unavailable_streak < self.unavailable_after:
                return
        else:
            self.unavailable_streak = 0
        if key not in GLOBAL_ERRORS:
            # O GitHub respondeu e aceitou o token: problemas gerais anteriores foram resolvidos.
            self._recovered(GLOBAL_ERRORS)
        signature = (error.kind, error.friendly)
        if self.last_errors.get(key, (None,))[0] == signature:
            return  # mesmo erro já avisado: não repete a cada ciclo
        self.last_errors[key] = (signature, recovered)
        lines = [f"❌ <b>{title}</b>", "", error.friendly]
        if forks:
            lines += ["", f"📁 <b>Forks afetados:</b> {html.escape(', '.join(forks))}"]
        if error.action:
            lines += ["", f"🔧 <b>O que fazer:</b> {error.action}"]
        lines += ["", f"<i>Detalhe técnico:</i> <code>{html.escape(str(error))}</code>"]
        self.send("\n".join(lines))

    def token_expiring(self, expires_at, days):
        when = "hoje" if days <= 0 else f"em {days} dia(s)"
        self.send(
            f"⏳ <b>O token do GitHub vence {when}</b> ({expires_at:%d/%m/%Y})\n\n"
            "🔧 <b>O que fazer:</b> gere um novo token no GitHub e atualize a variável "
            "<b>GITHUB_TOKEN</b> no Render antes do vencimento."
        )

    def _recovered(self, keys):
        for key in keys:
            entry = self.last_errors.pop(key, None)
            if entry:
                self.send(f"✅ {entry[1]}")


def format_commits(commits):
    """Uma linha por commit (os mais recentes), com o título da mensagem e o autor."""
    lines = []
    hidden = len(commits) - MAX_COMMITS_LISTED
    if hidden > 0:
        lines.append(f"<i>… e mais {hidden} commit(s) anterior(es)</i>")
    for commit in commits[-MAX_COMMITS_LISTED:]:
        info = commit.get("commit", {})
        title = (info.get("message") or "").strip().split("\n")[0] or "(sem mensagem)"
        if len(title) > MAX_COMMIT_TITLE:
            title = title[: MAX_COMMIT_TITLE - 1] + "…"
        author = (commit.get("author") or {}).get("login") or info.get("author", {}).get("name")
        line = f"• <code>{commit.get('sha', '')[:7]}</code> {html.escape(title)}"
        if author:
            line += f" — <i>{html.escape(author)}</i>"
        lines.append(line)
    return lines


def normalize_repo(value):
    """Aceita "dono/repo" ou a URL do GitHub (https://github.com/dono/repo[.git])."""
    match = re.fullmatch(
        r"(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?",
        str(value).strip(),
    )
    return f"{match.group(1)}/{match.group(2)}" if match else None


def load_config():
    raw = os.environ.get("FORKS_CONFIG")
    source = "variável FORKS_CONFIG"
    if not raw:
        path = Path(os.environ.get("FORKS_FILE", DEFAULT_FORKS_FILE))
        if not path.exists():
            raise SystemExit(f"Nenhuma configuração encontrada: defina FORKS_CONFIG ou crie {path}")
        raw = path.read_text()
        source = str(path)

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON inválido em {source}: {exc}")

    if isinstance(entries, dict):
        entries = entries.get("forks", [])
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"{source} precisa conter uma lista de forks")

    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"Item inválido em {source}: {entry!r}")
        for key in ("fork", "upstream"):
            if key == "upstream" and not entry.get(key):
                continue
            repo = normalize_repo(entry.get(key, ""))
            if not repo:
                raise SystemExit(f"Item inválido em {source}: {key} {entry.get(key)!r} (use \"dono/repo\" ou a URL)")
            entry[key] = repo
    return entries


def sync_fork(gh, entry):
    fork = entry["fork"]
    repo = gh.get(f"/repos/{fork}")
    if not repo.get("fork"):
        raise SyncError(
            f"{fork} não é um fork",
            "Este repositório não é um fork.",
            "Confira o campo <code>fork</code> no <code>forks.json</code>.",
            "config",
        )

    parent = repo["parent"]
    upstream = entry.get("upstream") or parent["full_name"]
    branch = entry.get("branch") or repo["default_branch"]
    if entry.get("upstream_branch"):
        upstream_branch = entry["upstream_branch"]
    elif entry.get("branch"):
        upstream_branch = branch
    elif upstream.lower() == parent["full_name"].lower():
        upstream_branch = parent["default_branch"]
    else:
        upstream_branch = gh.get(f"/repos/{upstream}")["default_branch"]

    label = f"{fork}@{branch} <- {upstream}@{upstream_branch}"
    summary = f"{fork} ({branch}) recebeu {{}} commit(s) de {upstream} ({upstream_branch}) via {{}}."
    upstream_owner = upstream.split("/")[0]

    # base = branch do fork, head = branch do upstream.
    # "ahead"     -> upstream tem commits que o fork não tem (fork atrasado)
    # "behind"    -> fork tem commits extras, mas já contém tudo do upstream
    # "diverged"  -> os dois têm commits exclusivos
    compare = gh.get(
        f"/repos/{fork}/compare/{quote(branch, safe='')}...{upstream_owner}:{quote(upstream_branch, safe='')}"
    )
    status = compare["status"]
    missing = compare["ahead_by"]
    changes = {"commits": compare.get("commits", []), "compare_url": compare.get("html_url")}

    if status in ("identical", "behind"):
        log.debug("%s: atualizado", label)
        return "up_to_date", label, {}

    log.info("%s: %d commit(s) atrás do upstream (%s), sincronizando...", label, missing, status)

    same_as_parent = upstream.lower() == parent["full_name"].lower() and upstream_branch == branch
    if same_as_parent:
        # Endpoint nativo do GitHub (mesmo do botão "Sync fork").
        resp = gh.request(
            "POST", f"/repos/{fork}/merge-upstream", ok=(200, 409, 422), json={"branch": branch}
        )
        if resp.status_code == 409:
            raise conflict_error(label)
        if resp.status_code == 422:
            raise api_error(422, error_message(resp), f"{label}: HTTP 422: {error_message(resp)}")
        merge_type = resp.json().get("merge_type", "merge")
        log.info("%s: sincronizado (%s)", label, merge_type)
        return "synced", summary.format(missing, merge_type), changes

    upstream_sha = gh.get(f"/repos/{upstream}/branches/{quote(upstream_branch, safe='')}")["commit"]["sha"]
    if status == "ahead":
        gh.request(
            "PATCH",
            f"/repos/{fork}/git/refs/heads/{quote(branch, safe='/')}",
            json={"sha": upstream_sha, "force": False},
        )
        log.info("%s: sincronizado (fast-forward para %s)", label, upstream_sha[:7])
        return "synced", summary.format(missing, "fast-forward"), changes
    else:
        resp = gh.request(
            "POST",
            f"/repos/{fork}/merges",
            ok=(201, 204, 409),
            json={
                "base": branch,
                "head": upstream_sha,
                "commit_message": f"Merge {upstream}@{upstream_branch} into {branch}",
            },
        )
        if resp.status_code == 409:
            raise conflict_error(label)
        log.info("%s: sincronizado (merge)", label)
        return "synced", summary.format(missing, "merge"), changes


def run_once(gh, entries, notifier=None):
    results = {"up_to_date": 0, "synced": 0, "failed": 0}
    for index, entry in enumerate(entries):
        fork = entry["fork"]
        try:
            result, detail, changes = sync_fork(gh, entry)
        except SyncError as exc:
            log.error("%s: %s", fork, exc)
            if exc.kind in GLOBAL_ERRORS:
                # Afeta todos os forks: um único aviso e encerra o ciclo.
                results["failed"] += len(entries) - index
                if notifier:
                    title, recovered = GLOBAL_ERRORS[exc.kind]
                    notifier.failed(exc.kind, title, recovered, exc, [e["fork"] for e in entries])
                break
            results["failed"] += 1
            if notifier:
                notifier.failed(
                    fork,
                    f"Falha ao sincronizar {html.escape(fork)}",
                    f"<b>{html.escape(fork)}</b> voltou a sincronizar normalmente",
                    exc,
                )
            continue
        results[result] += 1
        if notifier:
            if result == "synced":
                notifier.synced(fork, detail, **changes)
            else:
                notifier.ok(fork)
    # Ciclos sem novidade só aparecem com LOG_LEVEL=DEBUG, para os erros não se perderem no log.
    log.log(
        logging.INFO if results["synced"] or results["failed"] else logging.DEBUG,
        "Resumo: %d atualizado(s), %d sincronizado(s), %d falha(s)",
        results["up_to_date"], results["synced"], results["failed"],
    )
    warn_token_expiration(gh, notifier)
    return results["failed"] == 0


def warn_token_expiration(gh, notifier):
    """Avisa uma vez por dia quando o token do GitHub está perto de vencer."""
    if not gh.token_expires_at:
        return  # token sem validade (ou a resposta não informou)
    now = datetime.now(timezone.utc)
    days = (gh.token_expires_at - now).days
    if days >= TOKEN_WARN_DAYS or gh.token_warned_on == now.date():
        return
    gh.token_warned_on = now.date()
    log.warning("O token do GitHub vence em %d dia(s) (%s)", days, gh.token_expires_at.date())
    if notifier:
        notifier.token_expiring(gh.token_expires_at, days)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Defina a variável de ambiente GITHUB_TOKEN")

    entries = load_config()
    gh = GitHub(token)
    notifier = TelegramNotifier.from_env()
    if notifier:
        log.info("Notificações do Telegram ativadas")

    if os.environ.get("LOOP") == "1" or "--loop" in sys.argv:
        interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
        log.info("Modo loop: verificando %d fork(s) a cada %ds", len(entries), interval)
        while True:
            started = time.monotonic()
            run_once(gh, entries, notifier)
            time.sleep(max(0, interval - (time.monotonic() - started)))

    sys.exit(0 if run_once(gh, entries, notifier) else 1)


if __name__ == "__main__":
    main()
