# Gaston Agentic — Installation & utilisation

Une seule conversation pour piloter vos travaux de développement.

## Deux sessions IA

- **Engineering Lead** — Analyse, planifie, délègue, examine le code, pilote les tests et réalise les petites corrections.
- **Builder** — Implémente les changements importants. Le Lead vérifie les résultats de manière indépendante.

## Installation unique

Git et Python 3.10+ requis. Installez Gaston une fois par ordinateur, hors de vos projets.

Dans un terminal (PowerShell sous Windows) :

```sh
mkdir ~/tools
cd ~/tools
git clone https://github.com/GAUSST/gaston-agentic.git
cd gaston-agentic
```

**Linux / macOS**

```bash
python3 .agentic/central_install.py
```

**Windows — PowerShell**

```powershell
py -3 .agentic/central_install.py
```

Ouvrez un nouveau terminal, placez-vous à la racine du projet :

```sh
dev init
```

Dans un terminal interactif, `dev init` ouvre directement l’assistant complet de configuration (fournisseurs, rôles IA, modèles, outils et vérifications). Aucun argument `--setup` n’est nécessaire. Validez avec `GO`. Contexte du projet dans `.gaston/`.

## Commencer à développer

```sh
dev
```

Décrivez une fonctionnalité, un bug, une capture, un test ou une règle au Lead. Aucune conversation séparée avec le Builder n’est nécessaire.

Le Builder travaille sur une tâche délimitée pendant que le Lead reste disponible pour les inspections et les tests indépendants.

## Cycle de développement

1. **Comprendre** — Analyser les besoins et l’architecture existante.
2. **Planifier** — Définir le périmètre et les critères d’acceptation.
3. **Déléguer** — Confier une tâche délimitée au Builder.
4. **Développer** — Implémenter les changements et corrections.
5. **Examiner** — Contrôler le code et la documentation.
6. **Valider** — Exécuter les tests pertinents.
7. **Corriger** — Retester ; le Lead traite les défauts récurrents.
8. **Rendre compte** — Résumer changements, preuves et blocages.

Répéter jusqu’à satisfaction des critères ou décision du développeur. Un test indisponible n’est pas un test réussi.

## Gérer

| Commande | Utilisation |
| --- | --- |
| `dev sessions` | Consulter les sessions, fournisseurs et activités récentes. |
| `dev models` | Modifier les modèles, le raisonnement et l’Advisor du Lead. |
| `dev init` | Reprendre ou modifier la configuration du projet. |
| `dev uninstall` | Retirer Gaston du projet après confirmation ; conserver ou effacer son contexte. Le code applicatif est préservé. |

## Mise à jour

Fermez les sessions Gaston actives avant la mise à jour.

```sh
dev update
```

Mise à jour signée sur confirmation ; contexte conservé. Pour revenir à la version précédente si elle est disponible :

```sh
dev update --rollback
```

## Continuité du projet

Conservez vos décisions et votre architecture dans les documents du projet. Demandez au Lead d’enregistrer les sources approuvées.

`.gaston/local/PROJECT_CONTEXT.md` : index généré automatiquement. Ne le modifiez pas manuellement. Les documents approuvés restent la référence.

## Outils de vérification

Backend : Maven/Gradle et tests du projet. Base : PostgreSQL isolé. API : requêtes HTTP réelles. Web : Playwright. Mobile : Maestro et tests du projet.

## Principes d’ingénierie

- Code simple, efficace et maintenable ; respecter le périmètre.
- Aucune donnée inventée ni hypothèse métier silencieuse.
- Vérifier le comportement réel ; distinguer succès, échecs et blocages.
- Protéger les données, secrets et opérations financières.
- Autorisation requise pour les actions sensibles ou destructrices.

**Objectif :** Le Builder implémente ; le Lead coordonne, remet en question et valide de manière indépendante.
