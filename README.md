# TikTok Monitor — surveillance de comptes concurrents

Détecte automatiquement les nouvelles vidéos de tes concurrents TikTok, t'envoie
une notification push immédiate, puis des rappels avec les stats à +1h, +3h et +5h.

Coût estimé : **~29€/mois** (plan Starter de tiktokapi.store) + **gratuit** pour
les notifications (ntfy.sh) et l'hébergement (GitHub Actions).

---

## Installation (environ 15 minutes)

### 1. Créer un compte sur tiktokapi.store

1. Va sur https://tiktokapi.store/registration/new et crée un compte.
2. Une fois connecté, prends le plan **Starter à 29€/mois** (nécessaire pour
   surveiller 100+ comptes toutes les 15 min sans te faire limiter).
3. Récupère ta clé API dans le dashboard (elle ressemble à `sk_live_...`).
4. **Endpoint confirmé** : le script utilise `/api/v1/user/posts` (déjà
   configuré et testé — tu n'as rien à vérifier toi-même).

### 2. Créer le topic de notification (ntfy.sh)

1. Choisis un nom de "topic" unique et pas devinable, ex : `teo-tiktok-8f2k1x`
   (n'importe qui connaissant ce nom pourrait voir tes notifs, donc évite un nom trop simple).
2. Installe l'app **ntfy** sur ton téléphone (iOS / Android, gratuite).
3. Dans l'app, abonne-toi à ton topic (le même nom que ci-dessus).
4. C'est tout, pas de compte à créer, pas de configuration côté serveur.

### 3. Mettre le projet sur GitHub

1. Crée un nouveau dépôt **privé** sur GitHub (privé important, pour ne pas exposer
   ta liste de concurrents publiquement).
2. Mets-y tous les fichiers de ce projet (`monitor.py`, `accounts.json`,
   `requirements.txt`, `state.json`, `.github/workflows/monitor.yml`).
3. Dans les paramètres du dépôt → **Settings → Secrets and variables → Actions**,
   ajoute deux secrets :
   - `TIKTOK_API_KEY` = ta clé de tiktokapi.store
   - `NTFY_TOPIC` = le nom de topic choisi à l'étape 2
4. Dans **Settings → Actions → General → Workflow permissions**, coche
   **"Read and write permissions"** (nécessaire pour que le workflow puisse
   committer les mises à jour de `state.json`).

### 4. Configurer tes comptes à surveiller

Édite `accounts.json` et remplace la liste d'exemple par tes vrais pseudos
TikTok (sans le `@`) :

```json
{
  "accounts": [
    "concurrent1",
    "concurrent2",
    "concurrent3"
  ]
}
```

Commit et push. Tu peux en mettre 100+, pas de limite technique côté script.

### 5. Vérifier que ça tourne

1. Va dans l'onglet **Actions** de ton dépôt GitHub.
2. Le workflow "TikTok Monitor" doit apparaître et se lancer automatiquement
   toutes les 15 minutes. Tu peux aussi cliquer sur **"Run workflow"** pour le
   lancer manuellement et vérifier que tout fonctionne.
3. Regarde les logs de l'exécution : tu dois voir une ligne par compte vérifié.
4. Au tout premier lancement, aucune notif n'est envoyée (le script mémorise juste
   l'état actuel de chaque compte comme référence). À partir du 2e lancement,
   toute nouvelle vidéo déclenchera une notif.

---

## Comment ça fonctionne

- Toutes les 15 min, GitHub Actions lance `monitor.py`.
- Le script compare la dernière vidéo connue de chaque compte (stockée dans
  `state.json`) avec la vidéo la plus récente actuelle.
- Nouvelle vidéo → notif push immédiate + programmation de 3 rappels (+1h/+3h/+5h).
- À chaque exécution, le script regarde aussi s'il y a des rappels arrivés à
  échéance et les envoie.
- `state.json` est remis à jour et re-committé automatiquement dans le dépôt —
  c'est la mémoire du système entre deux exécutions.

## Limites à connaître

- **Détection à 15 min près**, pas à la seconde. Largement suffisant pour du
  benchmark concurrentiel.
- L'API tiktokapi.store est un service non-officiel (tiers). En cas de panne de
  leur côté, le script échoue silencieusement pour ce run et retente au suivant
  — regarde les logs Actions de temps en temps pour repérer un souci durable.
- Si un compte concurrent est privé ou n'existe plus, le script le signale dans
  les logs sans planter.

## Pour ajuster la fréquence

Dans `.github/workflows/monitor.yml`, change `*/15 * * * *` — par exemple
`*/30 * * * *` pour toutes les 30 min (réduit un peu la charge, sans intérêt
côté coût puisque le plan est en illimité).
