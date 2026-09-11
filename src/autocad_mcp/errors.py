"""Exceptions typées du serveur.

Le code historique avalait les erreurs dans des blocs ``except`` nus et renvoyait
malgré tout un succès. Le modèle croyait alors avoir dessiné alors que rien
n'existait, et ne pouvait pas se corriger. Toute erreur passe désormais par une
de ces classes, et remonte jusqu'à la réponse MCP.
"""

from __future__ import annotations

from typing import Any


class CadError(Exception):
    """Racine de toutes les erreurs du projet."""

    #: Code stable destiné au modèle, indépendant du texte du message.
    code = "cad_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload


class NotConnected(CadError):
    """Aucun document CAO n'est disponible pour exécuter l'opération."""

    code = "not_connected"


class BackendUnavailable(CadError):
    """Le backend demandé ne peut pas fonctionner sur cette machine."""

    code = "backend_unavailable"


class UnsupportedOperation(CadError):
    """Le backend ne sait pas exécuter cette opération.

    Levée explicitement plutôt qu'ignorée en silence, afin que l'écart entre
    backends soit visible au lieu de produire un dessin incomplet.
    """

    code = "unsupported_operation"


class InvalidGeometry(CadError):
    """Géométrie dégénérée ou incohérente.

    Couvre les segments de longueur nulle, les rayons négatifs, les polygones
    à moins de trois sommets et les points confondus.
    """

    code = "invalid_geometry"


class InvalidParameter(CadError):
    """Paramètre d'outil absent, hors domaine ou du mauvais type."""

    code = "invalid_parameter"


class EntityNotFound(CadError):
    """Aucune entité ne porte le handle demandé."""

    code = "entity_not_found"


class OperationFailed(CadError):
    """L'exécution a échoué côté backend.

    Le message porte la cause d'origine. Ne jamais convertir ceci en succès.
    """

    code = "operation_failed"


class ConfirmationRequired(CadError):
    """Opération destructrice appelée sans confirmation explicite."""

    code = "confirmation_required"
