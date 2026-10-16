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
from pathlib import Path
from urllib.parse import quote

import requests

API_URL = "https://api.github.com"
DEFAULT_FORKS_FILE = Path(__file__).with_name("forks.json")

log = logging.getLogger("sync-forks")


class SyncError(Exception):
    pass


class GitHub:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sync-forks",
            }
        )

    def request(self, method, path, ok=(200,), **kwargs):
        resp = self.session.request(method, API_URL + path, timeout=30, **kwargs)
        if resp.status_code in ok:
            return resp
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            raise SyncError(f"limite da API do GitHub atingido (libera em {max(0, reset - int(time.time()))}s)")
        try:
            message = resp.json().get("message", resp.text)
        except ValueError:
            message = resp.text
        raise SyncError(f"{method} {path} -> HTTP {resp.status_code}: {message}")

    def get(self, path):
        return self.request("GET", path).json()


class TelegramNotifier:
    """Envia avisos ao Telegram sem repetir o mesmo erro a cada ciclo."""

    def __init__(self, token, chat_id, notify_sync=True):
        self.token = token
        self.chat_id = chat_id
        self.notify_sync = notify_sync
        self.session = requests.Session()
        self.last_errors = {}  # fork -> última mensagem de erro já notificada

    @classmethod
    def from_env(cls):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            if token or chat_id:
                log.warning("Telegram desativado: defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID")
            return None
        return cls(token, chat_id, os.environ.get("TELEGRAM_NOTIFY_SYNC", "1") != "0")

    def send(self, text):
        try:
            resp = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
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

    def synced(self, fork, detail):
        self._recovered(fork)
        if self.notify_sync:
            self.send(f"🔄 <b>Fork sincronizado</b>\n{html.escape(detail)}")

    def up_to_date(self, fork):
        self._recovered(fork)

    def failed(self, fork, error):
        if self.last_errors.get(fork) == error:
            return
        self.last_errors[fork] = error
        self.send(f"❌ <b>Falha ao sincronizar</b> {html.escape(fork)}\n{html.escape(error)}")

    def _recovered(self, fork):
        if self.last_errors.pop(fork, None) is not None:
            self.send(f"✅ <b>{html.escape(fork)}</b> voltou a sincronizar normalmente")


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
        raise SyncError(f"{fork} não é um fork")

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

    if status in ("identical", "behind"):
        log.info("%s: atualizado", label)
        return "up_to_date", label

    log.info("%s: %d commit(s) atrás do upstream (%s), sincronizando...", label, missing, status)

    same_as_parent = upstream.lower() == parent["full_name"].lower() and upstream_branch == branch
    if same_as_parent:
        # Endpoint nativo do GitHub (mesmo do botão "Sync fork").
        resp = gh.request(
            "POST", f"/repos/{fork}/merge-upstream", ok=(200, 409, 422), json={"branch": branch}
        )
        if resp.status_code == 409:
            raise SyncError(f"{label}: conflito de merge, resolva manualmente")
        if resp.status_code == 422:
            raise SyncError(f"{label}: não foi possível sincronizar ({resp.json().get('message')})")
        merge_type = resp.json().get("merge_type", "ok")
        log.info("%s: sincronizado (%s)", label, merge_type)
        return "synced", f"{label}: {missing} commit(s) ({merge_type})"

    upstream_sha = gh.get(f"/repos/{upstream}/branches/{quote(upstream_branch, safe='')}")["commit"]["sha"]
    if status == "ahead":
        gh.request(
            "PATCH",
            f"/repos/{fork}/git/refs/heads/{quote(branch, safe='/')}",
            json={"sha": upstream_sha, "force": False},
        )
        log.info("%s: sincronizado (fast-forward para %s)", label, upstream_sha[:7])
        return "synced", f"{label}: {missing} commit(s) (fast-forward)"
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
            raise SyncError(f"{label}: conflito de merge, resolva manualmente")
        log.info("%s: sincronizado (merge)", label)
        return "synced", f"{label}: {missing} commit(s) (merge)"


def run_once(gh, entries, notifier=None):
    results = {"up_to_date": 0, "synced": 0, "failed": 0}
    for entry in entries:
        fork = entry["fork"]
        try:
            result, detail = sync_fork(gh, entry)
        except (SyncError, requests.RequestException) as exc:
            results["failed"] += 1
            log.error("%s: %s", fork, exc)
            if notifier:
                notifier.failed(fork, str(exc))
            continue
        results[result] += 1
        if notifier:
            if result == "synced":
                notifier.synced(fork, detail)
            else:
                notifier.up_to_date(fork)
    log.info(
        "Resumo: %d atualizado(s), %d sincronizado(s), %d falha(s)",
        results["up_to_date"], results["synced"], results["failed"],
    )
    return results["failed"] == 0


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
