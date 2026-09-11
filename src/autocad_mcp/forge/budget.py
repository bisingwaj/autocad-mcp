"""Budget d'exécution d'un outil généré.

Les limites vivent **dans le processus enfant**, appliquées juste avant qu'il
n'exécute quoi que ce soit. Le parent, lui, ne fait confiance à aucune d'elles:
il garde son propre délai et tue l'enfant s'il le dépasse.

## Ce que cette machine accepte réellement

Mesuré, pas supposé:

| Limite | macOS | Linux | Windows |
|---|---|---|---|
| Temps processeur | oui | oui | absente |
| Taille des fichiers écrits | oui | oui | absente |
| Descripteurs ouverts | oui | oui | absente |
| Taille de pile | oui | oui | partielle |
| **Mémoire** | **refusée** | oui | absente |

``RLIMIT_AS`` et ``RLIMIT_DATA`` échouent sur Darwin avec « current limit
exceeds maximum limit », alors même que la limite dure vaut l'infini: le noyau
les traite en alias de ``RLIMIT_RSS`` et refuse de les poser. **La mémoire n'y
est donc pas plafonnable.** C'est ce qui justifie deux autres décisions: le
refus des opérations de bits dans le validateur, et le fil de garde ci-dessous.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

#: Délai mural par défaut, en secondes. C'est la seule borne qui existe sur
#: toutes les plateformes, donc la seule sur laquelle on puisse compter.
DEFAULT_TIMEOUT: Final[float] = 2.0
MAX_TIMEOUT: Final[float] = 10.0

#: Mémoire tolérée avant que le fil de garde ne coupe.
DEFAULT_MEMORY_MB: Final[int] = 512

#: Taille maximale du verdict rendu par l'enfant.
MAX_RESULT_BYTES: Final[int] = 1024 * 1024

#: Période de scrutation du fil de garde mémoire.
MEMORY_POLL_SECONDS: Final[float] = 0.05

#: Profondeur de récursion de l'interpréteur enfant. La récursion est déjà
#: refusée par le validateur; ceci borne celle des fonctions natives.
RECURSION_LIMIT: Final[int] = 200

#: Descripteurs laissés ouverts dans l'enfant.
MAX_OPEN_FILES: Final[int] = 16


@dataclass(frozen=True, slots=True)
class Budget:
    """Ce qu'un outil généré a le droit de consommer."""

    timeout: float = DEFAULT_TIMEOUT
    memory_mb: int = DEFAULT_MEMORY_MB
    max_result_bytes: int = MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        if not 0 < self.timeout <= MAX_TIMEOUT:
            from .errors import ForgeBudgetExceeded

            raise ForgeBudgetExceeded(
                f"Délai hors bornes: attendu dans ]0, {MAX_TIMEOUT}]",
                timeout=self.timeout,
            )


def maxrss_to_bytes(maxrss: int) -> int:
    """Convertit ``ru_maxrss`` en octets.

    Darwin le rend en **octets**, Linux en **kibioctets**. Se tromper d'un
    facteur mille vingt-quatre rend le garde-fou soit inopérant, soit toujours
    déclenché. D'où cette fonction, et son test.
    """
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def apply_limits(budget: Budget) -> list[str]:
    """Applique dans le processus courant tout ce que la plateforme accepte.

    À n'appeler que depuis un processus enfant sur le point d'exécuter du code
    généré: ces limites ne se relèvent pas.

    Rend la liste des limites réellement posées, pour que le rapport dise ce
    qui protège vraiment plutôt que ce qu'on espérait.
    """
    posees: list[str] = []

    sys.setrecursionlimit(RECURSION_LIMIT)
    posees.append("recursion")

    # Limite le nombre de chiffres d'un entier converti en chaîne, ce qui coupe
    # court aux entiers géants. Absent avant Python 3.11.
    limiteur = getattr(sys, "set_int_max_str_digits", None)
    if limiteur is not None:
        limiteur(4300)
        posees.append("int_digits")

    try:
        import resource
    except ImportError:
        # Windows: aucune de ces limites n'existe. Le délai mural du parent et
        # le validateur restent les seules barrières, et le rapport le dira.
        return posees

    cpu = max(1, int(budget.timeout) + 1)
    for nom, valeur in (
        ("RLIMIT_CPU", (cpu, cpu + 1)),
        # Zéro octet écrit: toute écriture de fichier échoue bruyamment.
        ("RLIMIT_FSIZE", (0, 0)),
        ("RLIMIT_NOFILE", (MAX_OPEN_FILES, MAX_OPEN_FILES)),
        ("RLIMIT_NPROC", (0, 0)),
        ("RLIMIT_AS", (budget.memory_mb * 1024 * 1024,) * 2),
    ):
        cle = getattr(resource, nom, None)
        if cle is None:
            continue
        try:
            resource.setrlimit(cle, valeur)
        except (ValueError, OSError):
            # Attendu sur macOS pour la mémoire, et pour le nombre de processus
            # quand la limite dure est déjà basse. On n'insiste pas: le fil de
            # garde et le délai du parent prennent le relais.
            continue
        posees.append(nom.removeprefix("RLIMIT_").lower())

    return posees


def memory_exceeded(budget: Budget) -> bool:
    """Vrai si le processus courant dépasse son budget mémoire.

    Approximation par ``ru_maxrss``, qui mesure le pic et ne redescend jamais.
    C'est le seul recours là où la limite d'adressage est refusée.
    """
    try:
        import resource
    except ImportError:
        return False
    pic = maxrss_to_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return pic > budget.memory_mb * 1024 * 1024
