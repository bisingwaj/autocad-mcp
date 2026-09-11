"""Erreurs de la forge.

Elles dérivent de ``CadError`` afin que ``ToolHandlers.call`` les convertisse
déjà en réponse d'outil, sans qu'une ligne du noyau ne change.

Chaque erreur porte de quoi se corriger: la règle violée, l'endroit exact, et
quand c'est possible le geste à faire. Un refus muet obligerait le modèle à
deviner, ce qui est la meilleure façon de le faire tourner en rond.
"""

from __future__ import annotations

from typing import Any

from ..errors import CadError


class ForgeDisabled(CadError):
    """La forge n'est pas activée sur ce serveur."""

    code = "forge_disabled"


class ForgeRejected(CadError):
    """Le code ou la spécification a été refusé avant toute exécution.

    Porte la règle violée et sa position dans la source soumise.
    """

    code = "forge_rejected"

    def __init__(
        self,
        message: str,
        *,
        rule: str,
        line: int | None = None,
        column: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, rule=rule, line=line, column=column, **details)
        self.rule = rule
        self.line = line
        self.column = column


class ForgeCheckFailed(CadError):
    """L'outil s'exécute mais ne tient pas ce qu'il annonce."""

    code = "forge_check_failed"


class ForgeBudgetExceeded(CadError):
    """L'exécution a dépassé son budget de temps, de mémoire ou de taille."""

    code = "forge_budget_exceeded"


class ForgeExhausted(CadError):
    """Le plafond d'essais est atteint. L'outil part en quarantaine.

    Son code et son journal sont conservés: « abandon consigné » veut dire
    consigné, pas effacé.
    """

    code = "forge_exhausted"


class ForgeAlreadyTried(CadError):
    """Ce code exact a déjà été soumis et a déjà échoué.

    Refusé sans consommer d'essai et sans rien exécuter, avec l'erreur
    précédente rejouée. C'est le garde-fou contre la répétition à l'identique.
    """

    code = "forge_already_tried"


class ForgePendingApproval(CadError):
    """L'outil est forgé et testé, mais attend l'accord de l'utilisateur.

    L'approbation se donne au terminal, jamais par un outil MCP: si elle en
    était un, le modèle pourrait être persuadé de l'appeler lui-même et la
    barrière ne serait qu'un décor.
    """

    code = "forge_pending_approval"
