"""Palier macro: un outil décrit en données, sans aucun code exécuté.

Une macro est une liste d'étapes. Chaque étape appelle une fonction du registre
``macro_ops`` avec des arguments construits à partir des paramètres de l'outil.
L'interpréteur est **notre** code, relu, appliqué à des données: l'exécuter dans
le processus du serveur relève de la même classe de risque que décoder du JSON.

## La frontière, à défendre comme une règle du projet

Le langage ci-dessous est délibérément squelettique:

* un seul bloc, ``repeat``, dont le compteur est borné ;
* quatre opérations arithmétiques ;
* aucune conditionnelle, aucune fonction, aucune variable libre.

C'est le seul endroit du dispositif qui peut grossir sans fin. Une
conditionnelle demandée, puis une fonction, puis une variable locale, et l'on
aura écrit un interpréteur Python en JSON, moins bon que Python et sans son bac
à sable. Quand une macro ne suffit pas, l'erreur le dit et renvoie au palier
Python, qui lui s'exécute dans un processus séparé.
"""

from __future__ import annotations

from typing import Any, Final

from ..model.ops import Operation
from ..units import Defaults
from .errors import ForgeRejected
from .macro_ops import CALLABLES

#: Plafond du nombre d'opérations produites par une macro. Repris du plafond de
#: lot des outils MCP: deux plafonds différents finiraient par diverger.
MAX_OPERATIONS: Final[int] = 500

#: Plafond du compteur de ``repeat``, vérifié pendant l'expansion et non après.
MAX_REPEAT: Final[int] = 500

#: Profondeur d'imbrication des blocs ``repeat``.
MAX_NESTING: Final[int] = 3

_ARITHMETIC: Final[frozenset[str]] = frozenset({"add", "sub", "mul", "div"})


def _reject(message: str, rule: str, **details: Any) -> ForgeRejected:
    return ForgeRejected(message, rule=rule, **details)


def _needs_python(manque: str) -> ForgeRejected:
    """Refus qui oriente vers le palier Python.

    Ce n'est pas une erreur de l'appelant mais une limite du langage de macro,
    et le message doit le dire clairement pour que le modèle bascule au lieu
    d'essayer de contourner.
    """
    return ForgeRejected(
        f"Une macro ne sait pas faire cela: {manque}. "
        'Reprenez la définition avec `"tier": "python"` et justifiez-la dans '
        "le champ `reason`.",
        rule="needs_python",
        missing=manque,
    )


def evaluate(
    expression: Any,
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
) -> Any:
    """Réduit une expression de macro à une valeur.

    Les formes admises, et rien d'autre:

    * un littéral: nombre, chaîne, booléen ;
    * ``{"param": "nom"}``: un paramètre de l'outil ;
    * ``{"var": "i"}``: la variable d'un ``repeat`` englobant ;
    * ``{"defaults": "wall_thickness"}``: une valeur par défaut du document,
      ce qui garde les longueurs justes quelle que soit l'unité ;
    * ``{"at": point, "dx": ..., "dy": ...}``: un point décalé ;
    * ``{"add": [a, b]}`` et ses trois sœurs ;
    * une liste, dont chaque élément est lui-même une expression.
    """
    if isinstance(expression, list):
        return [evaluate(e, params, defaults, variables) for e in expression]

    if not isinstance(expression, dict):
        return expression

    if len(expression) == 0:
        raise _reject("Expression vide", "empty_expression")

    if "param" in expression:
        nom = expression["param"]
        if nom not in params:
            raise _reject(
                f"Paramètre inconnu dans la macro: {nom!r}",
                "unknown_param",
                param=nom,
                available=sorted(params),
            )
        return params[nom]

    if "var" in expression:
        nom = expression["var"]
        if nom not in variables:
            raise _reject(
                f"Variable inconnue: {nom!r}. Seule une boucle `repeat` en déclare.",
                "unknown_var",
                var=nom,
                available=sorted(variables),
            )
        return variables[nom]

    if "defaults" in expression:
        nom = expression["defaults"]
        valeur = getattr(defaults, str(nom), None)
        if valeur is None or str(nom).startswith("_"):
            raise _reject(
                f"Valeur par défaut inconnue: {nom!r}",
                "unknown_default",
                name=nom,
                available=["wall_thickness", "door_width", "text_height", "tolerance"],
            )
        return valeur

    if "at" in expression:
        return _offset_point(expression, params, defaults, variables)

    for operation in _ARITHMETIC:
        if operation in expression:
            return _arithmetic(operation, expression[operation], params, defaults, variables)

    raise _needs_python(f"l'expression {sorted(expression)!r} n'existe pas")


def _offset_point(
    expression: dict[str, Any],
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
) -> tuple[float, float]:
    base = evaluate(expression["at"], params, defaults, variables)
    if not isinstance(base, list | tuple) or len(base) < 2:
        raise _reject("`at` attend un point à deux coordonnées", "bad_point", value=base)
    dx = evaluate(expression.get("dx", 0), params, defaults, variables)
    dy = evaluate(expression.get("dy", 0), params, defaults, variables)
    return (float(base[0]) + float(dx), float(base[1]) + float(dy))


def _arithmetic(
    operation: str,
    operandes: Any,
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
) -> float:
    if not isinstance(operandes, list) or len(operandes) != 2:
        raise _reject(
            f"`{operation}` attend exactement deux opérandes", "bad_arity", op=operation
        )
    gauche = float(evaluate(operandes[0], params, defaults, variables))
    droite = float(evaluate(operandes[1], params, defaults, variables))
    if operation == "add":
        return gauche + droite
    if operation == "sub":
        return gauche - droite
    if operation == "mul":
        return gauche * droite
    if droite == 0.0:
        raise _reject("Division par zéro dans la macro", "division_by_zero")
    return gauche / droite


def expand(
    macro: dict[str, Any],
    params: dict[str, Any],
    defaults: Defaults,
) -> list[Operation]:
    """Déroule une macro en opérations de dessin.

    Raises:
        ForgeRejected: étape mal formée, référence inconnue, plafond dépassé,
            ou capacité qui exige le palier Python.
    """
    etapes = macro.get("steps")
    if not isinstance(etapes, list) or not etapes:
        raise _reject("Une macro doit porter au moins une étape", "empty_macro")

    produites: list[Operation] = []
    _run(etapes, params, defaults, {}, produites, depth=0)
    return produites


def _run(
    etapes: list[Any],
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
    sortie: list[Operation],
    *,
    depth: int,
) -> None:
    if depth > MAX_NESTING:
        raise _reject(
            f"Boucles imbriquées au-delà de {MAX_NESTING} niveaux", "too_nested"
        )

    for index, etape in enumerate(etapes):
        if not isinstance(etape, dict):
            raise _reject(f"Étape {index}: un objet est attendu", "bad_step", step=index)
        if "repeat" in etape:
            _repeat(etape["repeat"], params, defaults, variables, sortie, depth=depth)
        elif "call" in etape:
            _call(etape, params, defaults, variables, sortie, index)
        else:
            raise _needs_python(
                f"l'étape {index} n'est ni un appel ni une répétition"
            )

        if len(sortie) > MAX_OPERATIONS:
            # Vérifié pendant l'expansion et non après: une boucle mal bornée
            # ne doit pas avoir le temps de saturer la mémoire.
            raise _reject(
                f"Une macro ne peut pas produire plus de {MAX_OPERATIONS} opérations",
                "too_many_operations",
                produced=len(sortie),
            )


def _repeat(
    bloc: Any,
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
    sortie: list[Operation],
    *,
    depth: int,
) -> None:
    if not isinstance(bloc, dict):
        raise _reject("`repeat` attend un objet", "bad_repeat")

    brut = evaluate(bloc.get("count", 0), params, defaults, variables)
    try:
        compte = int(brut)
    except (TypeError, ValueError):
        raise _reject(
            f"Le compteur d'une répétition doit être un entier, reçu {brut!r}",
            "bad_count",
            value=brut,
        ) from None

    if not 0 <= compte <= MAX_REPEAT:
        raise _reject(
            f"Compteur hors bornes: attendu entre 0 et {MAX_REPEAT}",
            "count_out_of_range",
            count=compte,
        )

    nom = str(bloc.get("as", "i"))
    corps = bloc.get("steps")
    if not isinstance(corps, list) or not corps:
        raise _reject("Une répétition doit porter des étapes", "empty_repeat")

    for rang in range(compte):
        _run(
            corps,
            params,
            defaults,
            {**variables, nom: rang},
            sortie,
            depth=depth + 1,
        )


def _call(
    etape: dict[str, Any],
    params: dict[str, Any],
    defaults: Defaults,
    variables: dict[str, Any],
    sortie: list[Operation],
    index: int,
) -> None:
    nom = str(etape["call"])
    entree = CALLABLES.get(nom)
    if entree is None:
        raise _needs_python(
            f"la fonction {nom!r} n'est pas offerte aux macros "
            f"(disponibles: {', '.join(sorted(CALLABLES))})"
        )

    arguments = etape.get("with", {})
    if not isinstance(arguments, dict):
        raise _reject(f"Étape {index}: `with` attend un objet", "bad_args", step=index)

    evalues = {
        cle: evaluate(valeur, params, defaults, variables)
        for cle, valeur in arguments.items()
    }

    positionnels = []
    for attendu in entree.positional:
        if attendu not in evalues:
            raise _reject(
                f"`{nom}` exige l'argument `{attendu}`",
                "missing_argument",
                call=nom,
                argument=attendu,
                expected=list(entree.positional),
            )
        positionnels.append(evalues.pop(attendu))

    if entree.wants_defaults:
        positionnels.append(defaults)

    try:
        produites = entree.func(*positionnels, **evalues)
    except TypeError as exc:
        raise _reject(
            f"`{nom}` n'accepte pas ces arguments: {exc}",
            "bad_arguments",
            call=nom,
            given=sorted(arguments),
        ) from None

    sortie.extend(produites)


def contract() -> dict[str, Any]:
    """Décrit le langage de macro, pour le contrat remis au modèle."""
    from .macro_ops import contract as ops_contract

    return {
        "expressions": {
            "littéral": "un nombre, une chaîne ou un booléen",
            "param": '{"param": "largeur"} — un paramètre de l\'outil',
            "var": '{"var": "i"} — la variable d\'une répétition englobante',
            "defaults": '{"defaults": "wall_thickness"} — une valeur du document',
            "at": '{"at": {...}, "dx": 1, "dy": 0} — un point décalé',
            "arithmétique": '{"add": [a, b]}, et sub, mul, div',
        },
        "blocks": {
            "call": '{"call": "rectangle", "with": {...}} — appelle une fonction',
            "repeat": '{"repeat": {"count": ..., "as": "i", "steps": [...]}}',
        },
        "limits": {
            "max_operations": MAX_OPERATIONS,
            "max_repeat": MAX_REPEAT,
            "max_nesting": MAX_NESTING,
        },
        "not_supported": [
            "conditionnelle",
            "fonction définie",
            "trigonométrie et racine",
            "variable libre",
        ],
        "calls": ops_contract(),
    }
