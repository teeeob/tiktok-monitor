#!/usr/bin/env python3
"""
Surveillance de comptes TikTok concurrents.

Ce script est conçu pour être lancé toutes les 30 minutes (via GitHub Actions,
voir .github/workflows/monitor.yml). À chaque exécution il :

  1. Vérifie chaque compte listé dans accounts.json
  2. Si une nouvelle vidéo est détectée -> notification push immédiate
  3. Programme 3 rappels (+1h, +3h, +5h) pour récupérer les stats de la vidéo
  4. Envoie les rappels dont l'échéance est arrivée
  5. Sauvegarde son état dans state.json (committé par le workflow)

L'état (state.json) est ce qui permet au script de "se souvenir" d'une
exécution à l'autre, puisque GitHub Actions ne garde rien entre deux runs.

--------------------------------------------------------------------------
Ce script utilise l'API de tiktokapi.store, endpoint /api/v1/user/posts
(confirmé le 16/07/2026 - renvoie la liste des vidéos d'un compte avec
leurs stats incluses : vues, likes, commentaires, partages).
--------------------------------------------------------------------------
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

API_BASE = "https://tiktokapi.store/api/v1"
API_KEY = os.environ.get("TIKTOK_API_KEY")
NTFY_TOPIC_ALERTS = os.environ.get("NTFY_TOPIC_ALERTS")  # nouvelles vidéos, priorité haute
NTFY_TOPIC_STATS = os.environ.get("NTFY_TOPIC_STATS")    # rappels stats, groupés en digest

STATE_FILE = Path(__file__).parent / "state.json"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"

# Délais des rappels après publication (en heures)
CHECK_OFFSETS_HOURS = [1, 3, 5]

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3

# Cadence maximale autorisée par le plan tiktokapi.store (60 req/min).
# On se cale un peu en dessous par sécurité, et on espace les appels
# nous-mêmes plutôt que de compter sur les 429 + retry.
MAX_REQUESTS_PER_MINUTE = 55
_MIN_INTERVAL = 60.0 / MAX_REQUESTS_PER_MINUTE
_last_request_at = 0.0


def _throttle():
    """Attend le temps nécessaire pour ne jamais dépasser MAX_REQUESTS_PER_MINUTE."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Appels API TikTok (via tiktokapi.store)
# --------------------------------------------------------------------------

def _api_get(path: str, params: dict) -> dict | None:
    """Appelle l'API avec retries. Renvoie None si échec après plusieurs essais."""
    if not API_KEY:
        raise RuntimeError("TIKTOK_API_KEY manquant (variable d'environnement / secret GitHub)")

    headers = {"Authorization": f"Bearer {API_KEY}"}
    url = f"{API_BASE}{path}"

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                log(f"Rate limit atteint sur {path}, pause 5s (essai {attempt}/{MAX_RETRIES})")
                time.sleep(5)
            else:
                log(f"Erreur API {resp.status_code} sur {path}: {resp.text[:200]}")
                time.sleep(2)
        except requests.RequestException as e:
            log(f"Erreur réseau sur {path} (essai {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(2)

    log(f"Échec définitif de l'appel {path} après {MAX_RETRIES} essais")
    return None


def get_latest_videos(handle: str, limit: int = 5) -> list[dict]:
    """
    Récupère les dernières vidéos d'un compte, stats incluses.
    Structure de réponse confirmée le 16/07/2026 sur /api/v1/user/posts.
    """
    # NOTE: le paramètre "count" est une supposition raisonnable (non confirmée
    # explicitement) ; s'il est ignoré par l'API, elle renverra juste son nombre
    # de vidéos par défaut, sans erreur.
    data = _api_get("/user/posts", {"unique_id": handle, "count": limit})
    if not data or data.get("code") != 0:
        return []

    videos = data.get("data", {}).get("videos", [])
    normalized = []
    for v in videos:
        author = v.get("author", {})
        video_id = str(v.get("video_id"))
        author_handle = author.get("unique_id", handle)
        normalized.append({
            "video_id": video_id,
            "url": f"https://www.tiktok.com/@{author_handle}/video/{video_id}",
            "caption": v.get("title") or "",
            "create_time": v.get("create_time"),
            "stats": {
                "views": v.get("play_count"),
                "likes": v.get("digg_count"),
                "comments": v.get("comment_count"),
                "shares": v.get("share_count"),
            },
        })
    return normalized


def get_video_stats_by_id(handle: str, video_id: str) -> dict | None:
    """
    Retrouve les stats d'une vidéo précise en repassant par la liste des
    dernières vidéos du compte (l'API ne fournit pas d'endpoint /video/info
    confirmé, mais /user/posts suffit puisqu'il inclut déjà les stats).

    On demande une fenêtre plus large (20) pour être sûr de retrouver la
    vidéo même si le compte a posté plusieurs fois depuis sa détection.
    """
    videos = get_latest_videos(handle, limit=20)
    for v in videos:
        if v["video_id"] == video_id:
            return v["stats"]
    # La vidéo est sortie de la fenêtre des 20 posts les plus récents
    # (compte très actif) : on ne peut plus récupérer ses stats à jour.
    return None


# --------------------------------------------------------------------------
# Notifications push (ntfy.sh)
# --------------------------------------------------------------------------

def send_push(topic: str, title: str, message: str, url: str | None = None,
               priority: str = "default", markdown: bool = False):
    if not topic:
        raise RuntimeError("Topic ntfy manquant (NTFY_TOPIC_ALERTS / NTFY_TOPIC_STATS)")

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
    }
    if url:
        headers["Click"] = url
    if markdown:
        headers["Markdown"] = "yes"

    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            log(f"Échec envoi notif ntfy.sh ({topic}): {resp.status_code} {resp.text[:200]}")
    except requests.RequestException as e:
        log(f"Erreur réseau ntfy.sh ({topic}): {e}")


# ntfy convertit tout message dépassant ~4096 octets en pièce jointe .txt
# (illisible directement dans la notif). On se garde une bonne marge de
# sécurité et on découpe en plusieurs notifs si besoin plutôt que de risquer
# ce problème.
MAX_DIGEST_BYTES = 3500


def send_digest(topic: str, lines: list[str], label: str):
    """Envoie une liste de lignes en un minimum de notifications, en
    découpant automatiquement si ça dépasse la limite de taille de ntfy."""
    if not lines:
        return

    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line.encode("utf-8")) + 2  # +2 pour le séparateur "\n\n"
        if current and current_len + line_len > MAX_DIGEST_BYTES:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        title = f"📊 {len(lines)} {label}"
        if total > 1:
            title += f" ({i}/{total})"
        send_push(topic, title=title, message="\n\n".join(chunk), priority="low", markdown=True)


def fmt_num(n) -> str:
    if n is None:
        return "?"
    return f"{n:,}".replace(",", " ")


def fmt_delta(current, previous) -> str:
    """Formate une variation signée, ex: '+1 200' ou '-40'. Vide si pas de référence."""
    if current is None or previous is None:
        return ""
    d = current - previous
    sign = "+" if d >= 0 else ""
    return f" ({sign}{fmt_num(d)})"


def compute_engagement_rate(stats: dict) -> str | None:
    """(likes + commentaires + partages) / vues, en %."""
    views = stats.get("views")
    if not views:
        return None
    likes = stats.get("likes") or 0
    comments = stats.get("comments") or 0
    shares = stats.get("shares") or 0
    rate = (likes + comments + shares) / views * 100
    return f"{rate:.1f}".replace(".", ",") + "%"


def format_stats(stats: dict | None, previous: dict | None = None) -> str:
    """
    Compose le texte de la notif de rappel.
    `previous` = stats du check précédent (pour calculer la croissance
    entre deux rappels). None au tout premier check -> pas de delta affiché.
    """
    if not stats:
        return "stats indisponibles pour le moment"

    parts = []
    if stats.get("views") is not None:
        parts.append(f"{fmt_num(stats['views'])} vues{fmt_delta(stats.get('views'), (previous or {}).get('views'))}")
    if stats.get("likes") is not None:
        parts.append(f"{fmt_num(stats['likes'])} likes{fmt_delta(stats.get('likes'), (previous or {}).get('likes'))}")
    if stats.get("comments") is not None:
        parts.append(f"{fmt_num(stats['comments'])} com.{fmt_delta(stats.get('comments'), (previous or {}).get('comments'))}")
    if stats.get("shares") is not None:
        parts.append(f"{fmt_num(stats['shares'])} partages{fmt_delta(stats.get('shares'), (previous or {}).get('shares'))}")

    engagement = compute_engagement_rate(stats)
    if engagement:
        parts.append(f"engagement {engagement}")

    return " · ".join(parts) if parts else "stats indisponibles"


# --------------------------------------------------------------------------
# État persistant
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_video": {}, "pending": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_accounts() -> list[str]:
    data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    return data.get("accounts", [])


# --------------------------------------------------------------------------
# Logique principale
# --------------------------------------------------------------------------

def check_new_videos(state: dict, accounts: list[str]):
    for handle in accounts:
        videos = get_latest_videos(handle, limit=1)
        if not videos:
            log(f"@{handle}: aucune vidéo récupérée (compte privé, inexistant, ou erreur API)")
            continue

        latest = videos[0]
        previous_id = state["last_video"].get(handle)

        if previous_id is None:
            # Premier passage sur ce compte : on mémorise sans notifier
            # (sinon on spam une notif pour l'historique entier au 1er run)
            state["last_video"][handle] = latest["video_id"]
            log(f"@{handle}: référence initiale enregistrée (vidéo {latest['video_id']})")
            continue

        if latest["video_id"] != previous_id:
            log(f"@{handle}: NOUVELLE VIDÉO détectée -> {latest['video_id']}")
            state["last_video"][handle] = latest["video_id"]

            send_push(
                NTFY_TOPIC_ALERTS,
                title=f"🔴 @{handle} vient de poster",
                message=latest["caption"][:200] or "(pas de légende)",
                url=latest["url"],
                priority="high",
            )

            # Stats au moment de la détection : servent de référence pour
            # calculer la croissance affichée au rappel +1h.
            initial_stats = latest["stats"]

            # On ancre les rappels sur la VRAIE heure de publication de la
            # vidéo (create_time renvoyé par l'API), pas sur le moment où le
            # script s'en aperçoit. Comme ça, même si GitHub Actions tourne
            # en retard, les rappels +1h/+3h/+5h restent calculés par rapport
            # à l'heure réelle de mise en ligne, pas à un retard de découverte.
            create_time = latest.get("create_time")
            try:
                posted_at = datetime.fromtimestamp(int(create_time), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                # Timestamp absent ou invalide : à défaut, on utilise l'heure
                # actuelle (moins précis, mais évite de planter).
                posted_at = datetime.now(timezone.utc)

            state["pending"].append({
                "handle": handle,
                "video_id": latest["video_id"],
                "url": latest["url"],
                "detected_at": posted_at.isoformat(),
                "checks": [{"offset_h": h, "done": False} for h in CHECK_OFFSETS_HOURS],
                "last_stats": initial_stats,
            })


def process_pending_checks(state: dict):
    now = datetime.now(timezone.utc)
    still_pending = []
    digest_lines = []  # une ligne par rappel envoyé dans ce passage

    for item in state["pending"]:
        detected_at = datetime.fromisoformat(item["detected_at"])
        all_done = True

        for check in item["checks"]:
            if check["done"]:
                continue
            due_at = detected_at + timedelta(hours=check["offset_h"])
            if now >= due_at:
                stats = get_video_stats_by_id(item["handle"], item["video_id"])
                real_elapsed_h = (now - detected_at).total_seconds() / 3600
                stats_text = format_stats(stats, item.get("last_stats"))
                digest_lines.append(
                    f"**@{item['handle']} +{check['offset_h']}h** (réel {real_elapsed_h:.1f}h)\n"
                    f"{stats_text}\n"
                    f"[▶ Voir la vidéo]({item['url']})"
                )
                if stats:
                    item["last_stats"] = stats  # référence pour le prochain rappel
                check["done"] = True
                log(f"@{item['handle']}: rappel +{check['offset_h']}h envoyé ({item['video_id']})")
            else:
                all_done = False

        if not all_done:
            still_pending.append(item)
        else:
            log(f"@{item['handle']}: tous les rappels envoyés pour {item['video_id']}, on retire du suivi")

    state["pending"] = still_pending

    if digest_lines:
        send_digest(NTFY_TOPIC_STATS, digest_lines, label="rappel(s) stats disponible(s)")


def main():
    if not API_KEY:
        log("ERREUR: la variable d'environnement TIKTOK_API_KEY n'est pas définie.")
        sys.exit(1)
    if not NTFY_TOPIC_ALERTS:
        log("ERREUR: la variable d'environnement NTFY_TOPIC_ALERTS n'est pas définie.")
        sys.exit(1)
    if not NTFY_TOPIC_STATS:
        log("ERREUR: la variable d'environnement NTFY_TOPIC_STATS n'est pas définie.")
        sys.exit(1)

    accounts = load_accounts()
    if not accounts:
        log("Aucun compte dans accounts.json, rien à faire.")
        return

    state = load_state()

    log(f"Vérification de {len(accounts)} compte(s)...")
    check_new_videos(state, accounts)

    log(f"Traitement de {len(state['pending'])} vidéo(s) en attente de rappel...")
    process_pending_checks(state)

    save_state(state)
    log("Terminé.")


if __name__ == "__main__":
    main()
