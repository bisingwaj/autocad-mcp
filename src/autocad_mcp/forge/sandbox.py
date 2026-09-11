"""Exécution d'un outil généré dans un processus séparé.

## Pourquoi un processus et non un fil

CPython ne sait pas interrompre un fil. Le serveur en fait déjà l'expérience
avec son fil dédié aux appels AutoCAD: quand une tâche dépasse son délai, il
lève une erreur **et laisse la tâche tourner**. Une limite de temps posée sur
un fil est donc une promesse qu'on ne peut pas tenir, et une boucle sans fin
dans du code généré occuperait le fil pour toujours. Un processus, lui, se tue.

## Pourquoi engendrer et non dupliquer

``fork`` serait plus rapide, mais le serveur exécute déjà des fils pour ses
appels bloquants, et dupliquer un processus multifil est une source classique
d'interblocage: l'enfant hérite de verrous que personne ne relâchera jamais.
On engendre donc un interpréteur neuf. Mesuré à une trentaine de millisecondes
sur cette machine, ce qui est négligeable devant un aller-retour AutoCAD.

## Ce qui traverse la frontière

Du JSON, dans les deux sens. Le parent reconstruit lui-même les opérations à
partir de leur nom de type, puis les revalide avec la fonction du noyau. Aucun
objet ne vient de l'enfant, donc aucun type ne peut être fabriqué par lui.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Final

from ..errors import CadError
from ..model import ops as model_ops
from .budget import Budget
from .errors import ForgeBudgetExceeded, ForgeCheckFailed

#: Attente entre deux scrutations du tube, quand l'enfant n'a rien écrit.
#: Nommée et non écrite en dur: le projet interdit les temporisations de
#: confort, et celle-ci est bornée par le délai mural qui l'entoure.
_POLL_INTERVAL: Final[float] = 0.001

#: Correspondance entre le nom d'une opération et sa classe, construite depuis
#: le modèle. La recopier à la main la ferait diverger au premier ajout.
_OPERATION_TYPES: dict[str, type] = {
    nom: objet
    for nom in dir(model_ops)
    if isinstance(objet := getattr(model_ops, nom), type) and is_dataclass(objet)
}


@dataclass(slots=True)
class SandboxResult:
    """Ce que l'enfant a produit, et à quel prix."""

    operations: list[Any] = field(default_factory=list)
    duration_ms: int = 0
    limits_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": len(self.operations),
            "duration_ms": self.duration_ms,
            "limits_applied": self.limits_applied,
        }


def _rebuild(charge: dict[str, Any]) -> Any:
    """Reconstruit une opération depuis sa description JSON.

    Le champ ``kind`` désigne la classe. Un nom inconnu est refusé: l'enfant ne
    peut donc pas faire construire au parent un type qu'il aurait choisi.
    """
    genre = charge.get("kind")
    classe = _KIND_TO_CLASS.get(str(genre))
    if classe is None:
        raise ForgeCheckFailed(f"Opération de type inconnu: {genre!r}", kind=genre)

    attendus = {f.name for f in fields(classe)}
    valeurs = {c: v for c, v in charge.items() if c in attendus and c != "kind"}

    # Les dataclasses du modèle attendent des tuples, JSON rend des listes.
    for nom, valeur in list(valeurs.items()):
        if isinstance(valeur, list):
            valeurs[nom] = _to_tuple(valeur)

    if "style" in valeurs and isinstance(valeurs["style"], dict):
        valeurs["style"] = model_ops.Style(**valeurs["style"])
    if "attributes" in valeurs:
        valeurs["attributes"] = tuple(
            model_ops.AttributeDef(**a) if isinstance(a, dict) else a
            for a in valeurs["attributes"]
        )
    if "operations" in valeurs:
        valeurs["operations"] = tuple(_rebuild(o) for o in valeurs["operations"])

    try:
        return classe(**valeurs)
    except TypeError as exc:
        raise ForgeCheckFailed(
            f"Opération {genre} mal formée: {exc}", kind=genre
        ) from None


def _to_tuple(valeur: Any) -> Any:
    if isinstance(valeur, list):
        return tuple(_to_tuple(v) for v in valeur)
    return valeur


#: Indexe les classes par la valeur de leur champ ``kind``.
_KIND_TO_CLASS: dict[str, type] = {}
for _classe in _OPERATION_TYPES.values():
    for _champ in fields(_classe):
        if _champ.name == "kind" and isinstance(_champ.default, str):
            _KIND_TO_CLASS[_champ.default] = _classe


def run_sandboxed(
    source: str,
    params: dict[str, Any],
    *,
    unit: str = "m",
    budget: Budget | None = None,
) -> SandboxResult:
    """Exécute une source **déjà validée** et rend les opérations produites.

    Args:
        source: code de l'outil, tel que ``audit`` l'a accepté.
        params: paramètres déjà coercés.
        unit: unité du document, pour les valeurs par défaut.
        budget: limites. Celles par défaut si absent.

    Raises:
        ForgeBudgetExceeded: délai dépassé, l'enfant a été tué.
        ForgeCheckFailed: l'outil a échoué, ou a rendu autre chose que des
            opérations reconnues.
    """
    limites = budget or Budget()
    lecture, ecriture = os.pipe()
    depart = time.monotonic()

    requete = json.dumps(
        {
            "source": source,
            "params": params,
            "unit": unit,
            "timeout": limites.timeout,
            "memory_mb": limites.memory_mb,
        }
    )

    processus = subprocess.Popen(
        [sys.executable, "-I", "-m", "autocad_mcp.forge.worker", str(ecriture)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(ecriture,),
        # Environnement vide: ni clé d'API, ni jeton, ni rien d'hérité. Seul le
        # chemin d'import est conservé, sans quoi l'enfant ne se chargerait pas.
        env={"PYTHONPATH": os.pathsep.join(sys.path[1:] or [""])},
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    os.close(ecriture)

    try:
        if processus.stdin is not None:
            processus.stdin.write(requete.encode("utf-8"))
            processus.stdin.close()

        brut = _read_until_exit(processus, lecture, limites)
    finally:
        os.close(lecture)
        if processus.poll() is None:
            processus.kill()
            processus.wait(timeout=1.0)

    duree = int((time.monotonic() - depart) * 1000)

    if not brut:
        raise ForgeCheckFailed(
            "L'outil n'a rendu aucun verdict: il a probablement épuisé sa mémoire "
            "ou son temps processeur",
            exit_code=processus.returncode,
            duration_ms=duree,
        )

    verdict = json.loads(brut.decode("utf-8"))
    if not verdict.get("ok"):
        raise ForgeCheckFailed(
            str(verdict.get("error", "échec sans message")),
            error_type=verdict.get("error_type"),
            duration_ms=duree,
        )

    operations = [_rebuild(o) for o in verdict.get("operations", [])]
    for index, operation in enumerate(operations):
        # Même validation que les vrais backends: le bac à sable produit des
        # données, le noyau décide si elles sont acceptables.
        try:
            model_ops.validate(operation)
        except CadError as exc:
            # Reformulée sous le code de la forge, en gardant la cause: c'est
            # l'outil généré qui est en défaut, pas le serveur, et le modèle
            # doit pouvoir le distinguer pour se corriger au bon endroit.
            raise ForgeCheckFailed(
                f"Opération {index} refusée: {exc.message}",
                operation_index=index,
                kind=operation.kind,
                cause=exc.code,
            ) from None

    return SandboxResult(
        operations=operations,
        duration_ms=duree,
        limits_applied=list(verdict.get("limits_applied", [])),
    )


def _read_until_exit(
    processus: subprocess.Popen[bytes], canal: int, budget: Budget
) -> bytes:
    """Lit le verdict, en tuant l'enfant au-delà du délai.

    Le parent ne se repose sur aucune limite du système: elles n'existent pas
    toutes partout, et la mémoire n'est pas plafonnable sur macOS. Le délai
    mural est la seule barrière présente sur toutes les plateformes.
    """
    fin = time.monotonic() + budget.timeout
    morceaux: list[bytes] = []
    total = 0

    os.set_blocking(canal, False)
    while True:
        try:
            bloc = os.read(canal, 65536)
        except BlockingIOError:
            bloc = b""
        else:
            if not bloc and processus.poll() is not None:
                break
            morceaux.append(bloc)
            total += len(bloc)
            if total > budget.max_result_bytes:
                processus.kill()
                raise ForgeBudgetExceeded(
                    f"Verdict au-delà de {budget.max_result_bytes} octets",
                    limit_bytes=budget.max_result_bytes,
                )

        if time.monotonic() > fin:
            processus.kill()
            processus.wait(timeout=1.0)
            raise ForgeBudgetExceeded(
                f"L'outil a dépassé son délai de {budget.timeout} s et a été arrêté",
                timeout=budget.timeout,
                remedy="borner les boucles, ou réduire le nombre d'opérations produites",
            )
        if not bloc:
            time.sleep(_POLL_INTERVAL)

    return b"".join(morceaux)
