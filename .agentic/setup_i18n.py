"""Small built-in English/French dictionary for the Gaston Agentic setup UI.

This is display text only; program state/status codes remain language-neutral.
"""
from __future__ import annotations
from contextvars import ContextVar

CURRENT_LANGUAGE: ContextVar[str] = ContextVar('gaston_language', default='en')

FRENCH = {
    'Git installed': 'Git installé',
    'Python >= 3.10': 'Python >= 3.10',
    'Selected code roots tracked by Git': 'Projets sélectionnés suivis par Git',
    'Git baseline commits': 'Commits Git initiaux',
    'Application stack detected': 'Stack applicative détectée',
    'Scoped Codex + Claude native hooks': 'Hooks Codex + Claude limités aux sessions inscrites',
    'At least one configured test': 'Au moins une commande de test configurée',
    'Docker for Testcontainers': 'Docker pour Testcontainers',
    'Java >= 17': 'Java >= 17',
    'Java build/test runner': 'Outil de build/test Java',
    'Flutter SDK': 'SDK Flutter',
    'Flutter SDK (future app)': 'SDK Flutter (application future)',
    'Java >= 17 for Maestro': 'Java >= 17 pour Maestro',
    'Maestro CLI': 'CLI Maestro',
    'Connected Android/iOS emulator/device': 'Émulateur/appareil Android ou iOS connecté',
    'Real Maestro E2E flows': 'Parcours E2E Maestro réels',
    'Mobile end-to-end coverage': 'Couverture E2E mobile',
    'Flutter integration tests': 'Tests d’intégration Flutter',
    'Patrol test scripts': 'Scénarios de test Patrol',
    'Configure test commands in .agentic/config.json': 'Configurer les commandes de test dans .agentic/config.json',
    'Install Git': 'Installer Git',
    'Install Python 3.10+': 'Installer Python 3.10+',
    'Create an initial commit inside each application Git repository; required for reliable diff review': 'Créer un premier commit dans chaque dépôt applicatif pour fiabiliser la revue des modifications',
    'Choose which Java project to manage on the next ./dev init run': 'Choisir le projet Java à gérer lors du prochain ./dev init',
    'Choose which Flutter project to manage on the next ./dev init run': 'Choisir le projet Flutter à gérer lors du prochain ./dev init',
    'Run setup inside your Java/Flutter application repository, not an empty kit directory': 'Lancer la configuration dans un espace contenant un projet Java ou Flutter',
    'Install JDK 17+ and configure JAVA_HOME': 'Installer JDK 17+ et configurer JAVA_HOME',
    'Install Maven/Gradle or supply mvnw/gradlew': 'Installer Maven/Gradle ou fournir mvnw/gradlew',
    'Install Flutter SDK, add flutter to PATH, run flutter doctor': 'Installer le SDK Flutter, l’ajouter au PATH, puis lancer flutter doctor',
    'Install Flutter SDK when you are ready to create the frontend': 'Installer le SDK Flutter lors de la création du frontend',
    'Install JDK 17+ and configure JAVA_HOME for Maestro': 'Installer JDK 17+ et configurer JAVA_HOME pour Maestro',
    'Approve installation in ./dev init': 'Autoriser pendant ./dev init',
    'Start an emulator/device when ready to run mobile E2E tests': 'Démarrer un émulateur/appareil pour exécuter les tests mobiles E2E',
    'Add real .maestro/*.yaml flows; placeholders do not count': 'Ajouter de vrais parcours .maestro/*.yaml ; les exemples ne suffisent pas',
    'Create Flutter integration_test/ tests': 'Créer des tests Flutter dans integration_test/',
    'Create patrol_test/ scripts': 'Créer des scripts dans patrol_test/',
    'Select an E2E approach before claiming app verification': 'Sélectionner un outil E2E avant de valider l’application',
    'Install Docker (if Testcontainers is your chosen DB test strategy)': 'Installer Docker (si vous utilisez Testcontainers pour la base de données)',
    'Approve installation during ./dev init': 'Autoriser pendant ./dev init',
    'Open codex login flow now?': 'Ouvrir la connexion Codex maintenant ?',
    'Change model choices?': 'Modifier les modèles sélectionnés ?',
    'Install project-local native hooks?': 'Installer les hooks locaux du projet ?',
    'Open claude login flow now?': 'Ouvrir la connexion Claude maintenant ?',
    'Install project-local native hooks for registered sessions only?': 'Installer les hooks locaux uniquement pour les sessions enregistrées ?',
    'Install Codex through npm now?': 'Installer Codex via npm maintenant ?',
    'Download and execute the official Claude Code installer?': 'Télécharger et lancer l’installateur officiel Claude Code ?',
    'Execute this downloaded installer now?': 'Exécuter cet installateur téléchargé maintenant ?',
}


def translate(message: str, language: str | None = None) -> str:
    lang = language or CURRENT_LANGUAGE.get()
    if lang != 'fr':
        return message
    if message in FRENCH:
        return FRENCH[message]
    for prefix, translation in FRENCH.items():
        if prefix.endswith(' ') and message.startswith(prefix):
            return translation + message[len(prefix):]
    if message.endswith(' CLI'):
        return f'CLI {message[:-4]}'
    if message.endswith(' authentication'):
        return f'Authentification {message[:-15]}'
    if message.endswith(' Maestro MCP'):
        return f'MCP Maestro pour {message[:-12]}'
    if message.endswith(' project'):
        return f'Projet {message[:-8]}'
    if message.endswith(' CLI'):
        return f'CLI {message[:-4]}'
    return message
