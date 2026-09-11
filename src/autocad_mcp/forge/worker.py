"""Programme du processus enfant. **Jamais importé par le serveur.**

Il reçoit une source déjà validée, l'exécute dans un espace de noms restreint,
et rend les opérations produites sous forme de JSON.

## Trois détails qui décident du bon fonctionnement

**L'espace des natives doit être posé explicitement.** CPython réinjecte le
vrai module ``builtins`` quand la clé est absente de l'espace global. Un bac à
sable qui se contente de ne pas fournir ``__builtins__`` n'en est pas un.

**Le verdict ne passe pas par la sortie standard.** Elle est redirigée vers le
vide, pour deux raisons: dans le serveur, elle porte le protocole, et avec une
limite de taille de fichier à zéro, la purge des tampons à la fermeture échoue
et emporte toute la sortie. Le verdict part donc par un descripteur dédié, en
une écriture directe, suivie d'une sortie qui court-circuite les tampons.

**Les opérations traversent en JSON, jamais en ``pickle``.** Désérialiser un
``pickle`` *est* une exécution de code: ce serait rendre au parent le pouvoir
qu'on vient de retirer à l'enfant. Le parent reconstruit les dataclasses
lui-même et les revalide.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import fields, is_dataclass
from typing import Any

from .budget import Budget, apply_limits


def _serialize(valeur: Any) -> Any:
    """Réduit une opération à des types JSON, sans y laisser d'objet vivant."""
    if is_dataclass(valeur) and not isinstance(valeur, type):
        return {f.name: _serialize(getattr(valeur, f.name)) for f in fields(valeur)}
    if isinstance(valeur, dict):
        return {str(c): _serialize(v) for c, v in valeur.items()}
    if isinstance(valeur, list | tuple):
        return [_serialize(v) for v in valeur]
    if isinstance(valeur, bool | int | float | str) or valeur is None:
        return valeur
    raise TypeError(f"Type non sérialisable rendu par l'outil: {type(valeur).__name__}")


def build_namespace() -> dict[str, Any]:
    """Espace de noms offert au code généré.

    Tout ce qu'il peut toucher est ici. Rien d'autre n'existe pour lui, et
    chaque entrée ajoutée ici élargit le périmètre réel du bac à sable.
    """
    import builtins
    import math

    from .. import geometry
    from ..errors import InvalidGeometry, InvalidParameter
    from ..model import layers, ops
    from ..ops import architecture, blocks, primitives
    from . import vocabulary as V

    natives = {nom: getattr(builtins, nom) for nom in V.BUILTINS}

    espace: dict[str, Any] = {
        # Sans cette clé, CPython réinjecte le module complet des natives.
        "__builtins__": natives,
        **natives,
    }

    for module in (geometry, math):
        for nom in dir(module):
            if not nom.startswith("_"):
                espace.setdefault(nom, getattr(module, nom))

    for module in (ops, primitives, architecture, blocks, layers):
        for nom in dir(module):
            if not nom.startswith("_"):
                espace.setdefault(nom, getattr(module, nom))

    espace["InvalidGeometry"] = InvalidGeometry
    espace["InvalidParameter"] = InvalidParameter
    return espace


def vocabulary_names() -> frozenset[str]:
    """Noms que le validateur doit accepter, calculés depuis l'espace réel.

    Le validateur et l'exécuteur lisent ainsi la même liste: un nom accepté à
    la validation existera à l'exécution, et réciproquement.
    """
    return frozenset(n for n in build_namespace() if not n.startswith("__"))


def run(source: str, params: dict[str, Any], unit: str, budget: Budget) -> dict[str, Any]:
    """Exécute une source validée et rend le verdict.

    N'est appelée que dans le processus enfant, après application des limites.
    """
    from ..units import Defaults, parse_unit

    espace = build_namespace()
    module = compile(source, "<outil>", "exec")
    exec(module, espace)

    fonction = espace.get("build")
    if not callable(fonction):
        return {"ok": False, "error": "le module ne définit pas `build`"}

    operations = fonction(params, Defaults(parse_unit(unit)))

    if not isinstance(operations, list):
        return {
            "ok": False,
            "error": f"`build` doit rendre une liste, pas {type(operations).__name__}",
        }

    return {"ok": True, "operations": [_serialize(op) for op in operations]}


def _emit(canal: int, charge: dict[str, Any], limite: int) -> None:
    """Écrit le verdict et quitte sans passer par les tampons.

    Avec une limite de taille de fichier à zéro, la purge de la sortie standard
    échoue à la fermeture de l'interpréteur et le résultat serait perdu. Les
    tubes échappent à cette limite, d'où l'écriture directe.
    """
    octets = json.dumps(charge, ensure_ascii=False, default=str).encode("utf-8")
    if len(octets) > limite:
        octets = json.dumps(
            {"ok": False, "error": f"verdict trop volumineux: {len(octets)} octets"}
        ).encode("utf-8")
    os.write(canal, octets)
    os._exit(0)


def main() -> None:
    """Point d'entrée du processus enfant.

    Attend sur l'entrée standard un objet JSON portant la source, les
    paramètres, l'unité et le budget. Rend son verdict sur le descripteur
    passé en premier argument.
    """
    canal = int(sys.argv[1])
    requete = json.loads(sys.stdin.read())
    budget = Budget(
        timeout=float(requete.get("timeout", 2.0)),
        memory_mb=int(requete.get("memory_mb", 512)),
    )

    # Ni lue ni écrite par le code généré: l'entrée est close, et les deux
    # sorties partent vers le vide. Dans le serveur, la sortie standard porte
    # le protocole, et un enfant qui en hériterait pourrait forger des trames.
    vide = os.open(os.devnull, os.O_RDWR)
    os.dup2(vide, 0)
    os.dup2(vide, 1)
    os.dup2(vide, 2)

    posees = apply_limits(budget)

    try:
        verdict = run(requete["source"], requete["params"], requete["unit"], budget)
    except BaseException as exc:
        verdict = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }

    verdict["limits_applied"] = posees
    _emit(canal, verdict, budget.max_result_bytes)


if __name__ == "__main__":
    main()
