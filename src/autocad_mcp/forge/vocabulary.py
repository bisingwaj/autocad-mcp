"""Le périmètre du code généré, exprimé en données.

Ce module ne contient aucune logique: seulement les listes qui définissent ce
qu'un outil généré a le droit d'écrire. Le validateur ``audit.py`` les applique.

Deux principes gouvernent ces listes.

**Liste blanche, jamais liste noire.** Un type de nœud inconnu est refusé par
défaut. Une liste noire laisse passer ce à quoi personne n'a pensé, et c'est
exactement ainsi qu'un trou s'installe en silence.

**Le vocabulaire est le périmètre, pas l'arbre syntaxique.** Chaque fonction
offerte au code généré est une interface qu'un attaquant peut emprunter. Ajouter
une entrée ici est une décision de sûreté, pas une commodité.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from typing import Final

from ..model import ops as model_ops

# ---------------------------------------------------------------------------
# Types de nœuds
# ---------------------------------------------------------------------------

#: Nœuds admis dans le corps d'un outil généré.
ALLOWED_NODES: Final[frozenset[str]] = frozenset(
    {
        # structure
        "Module", "FunctionDef", "arguments", "arg", "Return", "Expr",
        # instructions
        "Assign", "AnnAssign", "AugAssign", "If", "For", "Break", "Continue",
        "Pass", "Assert", "Raise",
        # expressions
        "BoolOp", "NamedExpr", "BinOp", "UnaryOp", "IfExp", "Dict", "Set",
        "ListComp", "SetComp", "DictComp", "GeneratorExp", "comprehension",
        "Compare", "Call", "keyword", "JoinedStr", "FormattedValue", "Constant",
        "Attribute", "Subscript", "Slice", "Starred", "Name", "List", "Tuple",
        "Lambda",
        # opérateurs arithmétiques et logiques
        "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow",
        "USub", "UAdd", "Not", "And", "Or",
        # comparaisons
        "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn",
        # contextes
        "Load", "Store", "Del",
    }
)

#: Nœuds explicitement refusés, chacun avec la raison rendue au modèle.
#: Un message précis vaut mieux qu'un refus muet: le modèle doit pouvoir se
#: corriger sans deviner.
DENIED_NODES: Final[dict[str, str]] = {
    "Import": "le vocabulaire est injecté, un outil généré n'importe rien",
    "ImportFrom": "le vocabulaire est injecté, un outil généré n'importe rien",
    "ClassDef": "une classe crée des points d'exécution différés, inutile ici",
    "Try": "une fonction pure n'a rien à rattraper, et avaler une erreur est interdit",
    "TryStar": "une fonction pure n'a rien à rattraper",
    "ExceptHandler": "une fonction pure n'a rien à rattraper",
    "With": "un gestionnaire de contexte est l'aveu d'une ressource",
    "AsyncWith": "un gestionnaire de contexte est l'aveu d'une ressource",
    "AsyncFunctionDef": "un calcul géométrique n'est pas asynchrone",
    "Await": "un calcul géométrique n'est pas asynchrone",
    "AsyncFor": "un calcul géométrique n'est pas asynchrone",
    "Yield": "un générateur diffère l'exécution, la fonction doit rendre une liste",
    "YieldFrom": "un générateur diffère l'exécution",
    "Global": "écrire hors de la portée locale est un effet de bord",
    "Nonlocal": "écrire hors de la portée locale est un effet de bord",
    "Delete": "supprimer un attribut ou une clé est une mutation",
    "While": "utiliser `for i in range(n)`: une boucle bornée termine toujours",
    "Match": "le filtrage par motif extrait des attributs par nom, surface inutile",
    "MatchAs": "le filtrage par motif est refusé",
    "MatchClass": "le filtrage par motif est refusé",
    "MatchMapping": "le filtrage par motif est refusé",
    "MatchOr": "le filtrage par motif est refusé",
    "MatchSequence": "le filtrage par motif est refusé",
    "MatchSingleton": "le filtrage par motif est refusé",
    "MatchStar": "le filtrage par motif est refusé",
    "MatchValue": "le filtrage par motif est refusé",
    "match_case": "le filtrage par motif est refusé",
    "alias": "aucun import n'est autorisé",
    "withitem": "aucun gestionnaire de contexte n'est autorisé",
    # Les opérations de bits sont refusées pour une raison de mémoire, pas de
    # style: `1 << 10**9` alloue plusieurs gibioctets en une instruction, avant
    # qu'aucun contrôle Python ne puisse s'exécuter. Sur macOS la mémoire n'est
    # pas plafonnable par setrlimit, donc c'est ici la seule barrière réelle.
    "LShift": "les opérations de bits peuvent allouer sans borne, et la géométrie n'en a pas l'usage",
    "RShift": "les opérations de bits sont refusées",
    "BitAnd": "les opérations de bits sont refusées",
    "BitOr": "les opérations de bits sont refusées",
    "BitXor": "les opérations de bits sont refusées",
    "Invert": "les opérations de bits sont refusées",
    "MatMult": "le produit matriciel n'a pas d'usage ici",
    "TypeAlias": "déclarer un type n'a pas d'usage dans une fonction pure",
    "TypeVar": "déclarer un type n'a pas d'usage dans une fonction pure",
    "ParamSpec": "déclarer un type n'a pas d'usage dans une fonction pure",
    "TypeVarTuple": "déclarer un type n'a pas d'usage dans une fonction pure",
}

#: Types hors du sujet: abstraits, modes de compilation autres que ``Module``,
#: ou alias dépréciés que l'analyseur ne produit plus. Classés pour que le test
#: d'exhaustivité soit vrai, jamais rencontrés en pratique.
IRRELEVANT_NODES: Final[frozenset[str]] = frozenset(
    {
        "AST", "mod", "stmt", "expr", "expr_context", "boolop", "operator",
        "unaryop", "cmpop", "excepthandler", "pattern", "type_param",
        "type_ignore", "slice",
        "Expression", "Interactive", "FunctionType", "Suite", "TypeIgnore",
        # alias dépréciés depuis Python 3.8 et 3.9
        "Num", "Str", "Bytes", "NameConstant", "Ellipsis", "_ast_Ellipsis",
        "Index", "ExtSlice", "AugLoad", "AugStore", "Param",
    }
)


def known_ast_types() -> set[str]:
    """Tous les types de nœuds que cette version de Python connaît."""
    return {
        name
        for name in dir(ast)
        if isinstance(getattr(ast, name), type)
        and issubclass(getattr(ast, name), ast.AST)
    }


def unclassified_ast_types() -> set[str]:
    """Types que ce module ne classe nulle part.

    Doit rester vide. Un test l'affirme, de sorte qu'une montée de version de
    Python introduisant une construction nouvelle **casse la suite** au lieu de
    laisser passer un nœud dont personne n'a jugé la sûreté.
    """
    classes = ALLOWED_NODES | set(DENIED_NODES) | IRRELEVANT_NODES
    return known_ast_types() - classes


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------

#: Fonctions natives réellement injectées dans l'espace d'exécution.
#: CPython réinjecte le vrai module des natives quand la clé est absente de
#: l'espace global: ce dictionnaire doit donc être posé explicitement.
BUILTINS: Final[frozenset[str]] = frozenset(
    {
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "frozenset", "int", "isinstance", "len", "list", "map", "max",
        "min", "range", "reversed", "round", "set", "sorted", "str", "sum",
        "tuple", "zip",
        # Classes d'exception: un outil doit pouvoir refuser une entrée
        # absurde. Les instancier n'a aucun effet hors de la pile d'appel.
        "ValueError", "TypeError", "ZeroDivisionError",
    }
)

#: Noms interdits, y compris en écriture: les redéfinir permettrait de les
#: faire passer par la liste blanche.
DENIED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "eval", "exec", "compile", "__import__", "open", "input",
        "getattr", "setattr", "delattr", "hasattr", "vars", "dir",
        "globals", "locals", "breakpoint", "memoryview", "bytearray", "bytes",
        "super", "type", "object", "classmethod", "staticmethod", "property",
        "format", "help", "exit", "quit", "id", "hash", "repr", "print",
        "__builtins__", "__class__", "__name__", "__file__", "__loader__",
        "__spec__", "__doc__", "__dict__", "__globals__",
    }
)

#: Attributs bannis même s'ils venaient à entrer dans la liste blanche par
#: accident. Ils donnent accès à la pile, au code compilé ou aux natives.
DENIED_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame",
        "f_globals", "f_locals", "f_builtins", "f_back", "f_code",
        "tb_frame", "tb_next", "co_consts", "co_names", "co_code",
        "im_func", "func_globals", "__globals__", "__code__", "__closure__",
        "__self__", "__wrapped__", "__reduce__", "__reduce_ex__",
        # `"{0.__class__}".format(x)` parcourt une chaîne d'attributs décrite
        # dans une chaîne de caractères: l'arbre syntaxique n'en voit rien.
        "format", "format_map",
    }
)


def _operation_fields() -> set[str]:
    """Champs des opérations du modèle, lus dans les dataclasses.

    Recopier ces noms à la main les ferait diverger au premier champ ajouté,
    exactement comme la bibliothèque de blocs alimente son propre schéma.
    """
    noms: set[str] = set()
    for nom in dir(model_ops):
        objet = getattr(model_ops, nom)
        if isinstance(objet, type) and hasattr(objet, "__dataclass_fields__"):
            noms.update(f.name for f in fields(objet))
    return noms


#: Méthodes sûres des types natifs, sans effet hors de l'objet local.
SAFE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "append", "extend", "insert", "index", "count", "items", "keys",
        "values", "get", "upper", "lower", "strip", "lstrip", "rstrip",
        "split", "rsplit", "join", "replace", "startswith", "endswith",
        "add", "update", "copy", "sort", "reverse", "pop",
    }
)


def allowed_attrs() -> frozenset[str]:
    """Attributs dont la lecture est permise.

    Union des champs des opérations du modèle et des méthodes sûres, moins les
    attributs bannis. L'ordre compte: le bannissement gagne toujours.
    """
    return frozenset((_operation_fields() | SAFE_METHODS) - DENIED_ATTRS)


# ---------------------------------------------------------------------------
# Bornes statiques
# ---------------------------------------------------------------------------

MAX_SOURCE_BYTES: Final[int] = 16 * 1024
MAX_LINES: Final[int] = 400
MAX_NODES: Final[int] = 2000
MAX_DEPTH: Final[int] = 12
MAX_STRING_LITERAL: Final[int] = 4096
MAX_INT_DIGITS: Final[int] = 20
#: Exposant maximal d'une puissance: ``10 ** 10 ** 10`` gèle l'interpréteur
#: avant que le moindre contrôle ne s'exécute.
MAX_POW_EXPONENT: Final[int] = 8
MAX_COMPREHENSION_GENERATORS: Final[int] = 2

#: Caractères qui font lire une chose à l'humain et compiler l'autre à Python.
#: Ils visent la relecture d'approbation, pas l'interpréteur.
BIDI_CHARS: Final[frozenset[str]] = frozenset(
    "‪‫‬‭‮⁦⁧⁨⁩​‎‏﻿"
)

#: Nom imposé du point d'entrée d'un outil généré.
ENTRY_POINT: Final[str] = "build"
