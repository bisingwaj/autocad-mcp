"""La forge: outils de dessin créés à la volée, sous contrôle.

Le modèle décrit un outil, la forge le valide, l'exécute dans un bac à sable,
le teste, et rend l'erreur réelle pour qu'il se corrige.

**Principe directeur: le bac à sable produit des données, le noyau exécute.**
Le code généré ne reçoit jamais un backend, ni un chemin de fichier, ni une
connexion COM. Il rend une liste d'opérations que le processus parent
reconstruit lui-même et revalide avec ``model.ops.validate``, la même fonction
que les vrais backends. Tout le reste n'est que défense en profondeur autour de
cette propriété.

Ce module d'entrée n'importe rien de lourd: le validateur, le bac à sable et le
registre se chargent à la demande, afin que la forge désactivée ne coûte rien.
"""

from __future__ import annotations

__all__ = ["FORGE_OFF", "ForgeError"]

#: Valeur de ``Config.forge_mode`` qui laisse la forge entièrement absente du
#: catalogue. C'est le défaut: une capacité présente par défaut est une
#: capacité qu'une consigne injectée peut atteindre.
FORGE_OFF = "off"


class ForgeError(Exception):
    """Racine des erreurs de la forge.

    Définie ici plutôt que dans ``errors.py`` pour que le noyau ne bouge pas.
    ``forge.errors`` en dérive les cas précis.
    """
