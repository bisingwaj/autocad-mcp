"""Validateur du code généré. Le fichier le plus critique du dépôt.

Il décide de ce qu'un modèle de langage a le droit de faire exécuter par ce
serveur. Une modification subtile y ouvre un trou en silence, exactement comme
la branche morte qui a longtemps laissé passer un import placé dans un ``try:``
de haut niveau ailleurs dans ce projet.

**Il n'accepte que ce qu'il connaît.** Le parcours refuse tout nœud absent de
``vocabulary.ALLOWED_NODES``, y compris un nœud qu'une version future de Python
introduirait. Chercher les nœuds interdits, à l'inverse, laisse passer ce à quoi
personne n'a pensé.

## Ce que ce validateur ne peut pas garantir

À dire sans détour, parce que c'est ce qui justifie le bac à sable:

* **la terminaison**, indécidable. Deux boucles bornées imbriquées suffisent à
  un temps arbitraire. Seule une limite de temps réelle coupe ;
* **la mémoire**: ``[0] * n`` alloue sans borne en une instruction ;
* **le sens**: il dit que le code est inoffensif, jamais que le mur est au bon
  endroit ;
* **les bugs de l'interpréteur**.

La frontière réelle est un processus séparé, pas cette analyse.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Final

from . import vocabulary as V
from .errors import ForgeRejected


@dataclass(slots=True)
class AuditReport:
    """Ce que l'audit a constaté, pour l'écran d'approbation."""

    entry_point: str
    node_count: int
    max_depth: int
    line_count: int
    #: Noms du vocabulaire réellement appelés. L'humain qui approuve voit ainsi
    #: la surface employée, pas seulement qu'aucune règle n'a été violée.
    calls: set[str] = field(default_factory=set)
    attributes: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_point": self.entry_point,
            "nodes": self.node_count,
            "depth": self.max_depth,
            "lines": self.line_count,
            "calls": sorted(self.calls),
            "attributes": sorted(self.attributes),
        }


#: Noms que le code généré peut appeler: les natives injectées, plus le
#: vocabulaire métier posé dans son espace d'exécution.
def _callable_names(vocabulary_names: frozenset[str]) -> frozenset[str]:
    return V.BUILTINS | vocabulary_names


#: Modules dont la présence dans le vocabulaire ruinerait tout le dispositif.
#: Le périmètre réel du bac à sable est le vocabulaire offert, pas cette
#: analyse: une fonction impure injectée serait une porte grande ouverte.
FORBIDDEN_IN_VOCABULARY: Final[frozenset[str]] = frozenset(
    {
        "os", "io", "sys", "pathlib", "subprocess", "socket", "ctypes",
        "importlib", "pickle", "shutil", "tempfile", "urllib", "requests",
        "http", "multiprocessing", "threading", "signal", "builtins",
    }
)


def audit(
    source: str,
    *,
    vocabulary_names: frozenset[str] = frozenset(),
) -> tuple[ast.Module, AuditReport]:
    """Valide une source et rend l'arbre **exactement** vérifié.

    L'appelant doit compiler cet arbre, jamais relire le fichier: relire
    rouvrirait la fenêtre entre la vérification et l'usage.

    Args:
        source: le code soumis par le modèle.
        vocabulary_names: noms métier injectés dans l'espace d'exécution, en
            plus des natives.

    Raises:
        ForgeRejected: à la première règle violée, avec sa position.
    """
    _check_text(source)
    tree = _parse(source)
    _check_shape(tree)

    report = AuditReport(
        entry_point=V.ENTRY_POINT,
        node_count=0,
        max_depth=0,
        line_count=source.count("\n") + 1,
    )
    # La récursion se vérifie avant le parcours: une fonction locale qui
    # s'appelle elle-même passerait sinon pour un simple nom lié, et le refus
    # serait rendu au modèle sous une règle qui ne dit pas la vraie cause.
    _check_recursion(tree)

    known = _callable_names(vocabulary_names) | _bound_names(tree)
    _walk(tree, depth=0, report=report, allowed_calls=known, known_names=known)
    return tree, report


def _bound_names(tree: ast.Module) -> frozenset[str]:
    """Noms que le code lie lui-même: variables, paramètres, fonctions locales.

    Sans cette collecte, une fonction définie dans le corps de ``build`` serait
    refusée à son propre appel, et la boucle de correction enverrait le modèle
    sur une fausse piste.
    """
    noms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            noms.add(node.name)
            noms.update(a.arg for a in node.args.args)
            noms.update(a.arg for a in node.args.posonlyargs)
            noms.update(a.arg for a in node.args.kwonlyargs)
        elif isinstance(node, ast.Lambda):
            noms.update(a.arg for a in node.args.args)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            noms.add(node.id)
        elif isinstance(node, ast.comprehension):
            for cible in ast.walk(node.target):
                if isinstance(cible, ast.Name):
                    noms.add(cible.id)
    return frozenset(noms)


# ---------------------------------------------------------------------------
# Étage 1: le texte, avant même l'analyse syntaxique
# ---------------------------------------------------------------------------


def _reject(
    message: str,
    rule: str,
    node: ast.AST | None = None,
    *,
    line: int | None = None,
    **extra: object,
) -> ForgeRejected:
    """Construit un refus situé. ``line`` explicite l'emporte sur le nœud."""
    return ForgeRejected(
        message,
        rule=rule,
        line=line if line is not None else getattr(node, "lineno", None),
        column=getattr(node, "col_offset", None),
        **extra,
    )


def _check_text(source: str) -> None:
    """Contrôles sur les octets, avant de faire confiance à l'analyseur."""
    if not source.strip():
        raise _reject("Source vide", "source_empty")

    encoded = source.encode("utf-8")
    if len(encoded) > V.MAX_SOURCE_BYTES:
        raise _reject(
            f"Source trop longue: {len(encoded)} octets, maximum {V.MAX_SOURCE_BYTES}",
            "source_too_large",
        )

    lines = source.splitlines()
    if len(lines) > V.MAX_LINES:
        raise _reject(
            f"Trop de lignes: {len(lines)}, maximum {V.MAX_LINES}", "too_many_lines"
        )

    # Caractères bidirectionnels et invisibles: l'attaque vise la relecture
    # humaine de l'écran d'approbation, pas l'interpréteur. Le code lu et le
    # code compilé doivent être le même.
    for index, char in enumerate(source):
        if char in V.BIDI_CHARS:
            ligne = source.count("\n", 0, index) + 1
            raise _reject(
                f"Caractère invisible ou bidirectionnel U+{ord(char):04X}",
                "invisible_char",
                codepoint=f"U+{ord(char):04X}",
                line=ligne,
            )

    # Un cookie d'encodage ferait lire la source dans un autre jeu de
    # caractères que celui qu'on vient de contrôler.
    for contenu in lines[:2]:
        if "coding:" in contenu or "coding=" in contenu:
            raise _reject("Déclaration d'encodage refusée", "encoding_cookie")


def _parse(source: str) -> ast.Module:
    try:
        return ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ForgeRejected(
            f"Erreur de syntaxe: {exc.msg}",
            rule="syntax_error",
            line=exc.lineno,
            column=exc.offset,
        ) from None


# ---------------------------------------------------------------------------
# Étage 2: la forme du module
# ---------------------------------------------------------------------------


def _check_shape(tree: ast.Module) -> None:
    """Un module, une docstring facultative, une fonction ``build``. Rien d'autre.

    Interdire toute instruction au niveau module supprime d'un coup les
    constantes calculées à l'import, les effets de bord au chargement, et les
    gardes conditionnelles qui ont si souvent servi à cacher un import.
    """
    corps = list(tree.body)
    if corps and _is_docstring(corps[0]):
        corps = corps[1:]

    if len(corps) != 1:
        raise _reject(
            f"Le module doit contenir exactement une fonction `{V.ENTRY_POINT}`, "
            f"éventuellement précédée d'une docstring. Trouvé {len(corps)} instructions.",
            "module_shape",
            corps[1] if len(corps) > 1 else None,
        )

    fonction = corps[0]
    if not isinstance(fonction, ast.FunctionDef):
        raise _reject(
            f"Le module doit définir la fonction `{V.ENTRY_POINT}`", "module_shape", fonction
        )
    if fonction.name != V.ENTRY_POINT:
        raise _reject(
            f"Le point d'entrée doit s'appeler `{V.ENTRY_POINT}`, pas `{fonction.name}`",
            "entry_point_name",
            fonction,
        )
    _check_signature(fonction)


def _check_signature(fonction: ast.FunctionDef) -> None:
    """Signature figée: ``build(params, defaults)``.

    Une signature calculable est une signature vérifiable. Un désaccord devient
    une erreur nommée au lieu d'une erreur de type anonyme à l'exécution.
    """
    if fonction.decorator_list:
        raise _reject("Aucun décorateur n'est autorisé", "decorator", fonction)

    args = fonction.args
    if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
        raise _reject(
            f"Signature attendue: `def {V.ENTRY_POINT}(params, defaults)`, "
            "sans arguments variables ni mots-clés seuls",
            "signature",
            fonction,
        )
    noms = [a.arg for a in args.args]
    if noms != ["params", "defaults"]:
        raise _reject(
            f"Signature attendue: `def {V.ENTRY_POINT}(params, defaults)`. Trouvé: "
            f"({', '.join(noms)})",
            "signature",
            fonction,
        )
    if args.defaults:
        raise _reject("Le point d'entrée ne prend pas de valeur par défaut", "signature", fonction)

    corps = fonction.body
    reste = corps[1:] if _is_docstring(corps[0]) else corps
    if not reste:
        raise _reject("Le corps de la fonction est vide", "empty_body", fonction)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


# ---------------------------------------------------------------------------
# Étage 3: le parcours, liste blanche stricte
# ---------------------------------------------------------------------------


def _walk(
    node: ast.AST,
    *,
    depth: int,
    report: AuditReport,
    allowed_calls: frozenset[str],
    known_names: frozenset[str],
) -> None:
    """Parcours récursif. Tout nœud inconnu est refusé."""
    report.node_count += 1
    report.max_depth = max(report.max_depth, depth)

    if report.node_count > V.MAX_NODES:
        raise _reject(
            f"Code trop complexe: plus de {V.MAX_NODES} nœuds", "too_many_nodes", node
        )
    if depth > V.MAX_DEPTH:
        raise _reject(
            f"Imbrication trop profonde: plus de {V.MAX_DEPTH} niveaux", "too_deep", node
        )

    nom = type(node).__name__
    if nom in V.DENIED_NODES:
        raise _reject(
            f"`{nom}` est refusé: {V.DENIED_NODES[nom]}", "denied_node", node, node_type=nom
        )
    if nom not in V.ALLOWED_NODES:
        # Un nœud ni autorisé ni refusé est forcément nouveau: on refuse.
        raise _reject(
            f"`{nom}` n'est pas dans le vocabulaire autorisé", "unknown_node", node, node_type=nom
        )

    _check_node(node, report=report, allowed_calls=allowed_calls, known_names=known_names)

    for enfant in ast.iter_child_nodes(node):
        _walk(
            enfant,
            depth=depth + 1,
            report=report,
            allowed_calls=allowed_calls,
            known_names=known_names,
        )


def _check_node(
    node: ast.AST,
    *,
    report: AuditReport,
    allowed_calls: frozenset[str],
    known_names: frozenset[str],
) -> None:
    """Règles propres à chaque type de nœud."""
    if isinstance(node, ast.Name):
        _check_name(node, known_names)
    elif isinstance(node, ast.Attribute):
        _check_attribute(node, report)
    elif isinstance(node, ast.Call):
        _check_call(node, report, allowed_calls)
    elif isinstance(node, ast.Constant):
        _check_constant(node)
    elif isinstance(node, ast.BinOp):
        _check_binop(node)
    elif isinstance(node, ast.FunctionDef):
        if node.decorator_list:
            raise _reject("Aucun décorateur n'est autorisé", "decorator", node)
    elif isinstance(node, ast.Lambda):
        if node.args.vararg or node.args.kwarg:
            raise _reject("Arguments variables refusés", "lambda_args", node)
    elif isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        if len(node.generators) > V.MAX_COMPREHENSION_GENERATORS:
            raise _reject(
                f"Compréhension à {len(node.generators)} niveaux: maximum "
                f"{V.MAX_COMPREHENSION_GENERATORS}. Une triple imbrication est cubique.",
                "comprehension_depth",
                node,
            )
    elif isinstance(node, ast.JoinedStr):
        _check_fstring(node)


def _check_name(node: ast.Name, known_names: frozenset[str]) -> None:
    if node.id in V.DENIED_NAMES:
        raise _reject(f"Le nom `{node.id}` est interdit", "denied_name", node, name=node.id)
    if node.id.startswith("__") and node.id.endswith("__"):
        raise _reject(
            f"Les noms spéciaux sont interdits: `{node.id}`", "dunder_name", node, name=node.id
        )
    # Un nom lu mais jamais lié ni offert par le vocabulaire échouerait à
    # l'exécution. Le refuser ici rend l'erreur lisible et immédiate, au lieu
    # d'une erreur de nom au milieu d'un dessin à moitié produit.
    if isinstance(node.ctx, ast.Load) and node.id not in known_names:
        raise _reject(
            f"`{node.id}` n'existe pas: ni variable locale, ni fonction du vocabulaire",
            "unknown_name",
            node,
            name=node.id,
        )


def _check_attribute(node: ast.Attribute, report: AuditReport) -> None:
    attr = node.attr
    if attr.startswith("__") and attr.endswith("__"):
        raise _reject(
            f"Les attributs spéciaux sont interdits: `.{attr}`", "dunder_attr", node, attr=attr
        )
    if attr in V.DENIED_ATTRS:
        raise _reject(f"L'attribut `.{attr}` est interdit", "denied_attr", node, attr=attr)
    if attr not in V.allowed_attrs():
        raise _reject(
            f"L'attribut `.{attr}` n'est pas dans le vocabulaire autorisé",
            "unknown_attr",
            node,
            attr=attr,
        )
    report.attributes.add(attr)


def _check_call(node: ast.Call, report: AuditReport, allowed_calls: frozenset[str]) -> None:
    """La cible d'un appel doit être un nom, jamais une expression calculée.

    Cette seule règle supprime `f()()`, `table[clef]()` et toute table
    d'indirection: il n'existe plus aucun moyen de choisir la fonction appelée
    à l'exécution.
    """
    cible = node.func
    if isinstance(cible, ast.Name):
        # Un nom explicitement banni garde sa propre règle: le modèle doit
        # savoir qu'il a touché un interdit, pas seulement qu'il a manqué le
        # vocabulaire.
        if cible.id in V.DENIED_NAMES:
            raise _reject(
                f"Le nom `{cible.id}` est interdit", "denied_name", node, name=cible.id
            )
        if cible.id not in allowed_calls:
            raise _reject(
                f"`{cible.id}` n'est pas appelable depuis un outil généré",
                "denied_call",
                node,
                name=cible.id,
            )
        report.calls.add(cible.id)
        return
    if isinstance(cible, ast.Attribute):
        # L'attribut est déjà contrôlé par _check_attribute lors du parcours.
        report.calls.add(cible.attr)
        return
    raise _reject(
        "La cible d'un appel doit être un nom simple, jamais une expression calculée",
        "computed_call",
        node,
    )


def _check_constant(node: ast.Constant) -> None:
    valeur = node.value
    if isinstance(valeur, str) and len(valeur) > V.MAX_STRING_LITERAL:
        raise _reject(
            f"Chaîne littérale trop longue: {len(valeur)} caractères", "string_too_long", node
        )
    if (
        isinstance(valeur, int)
        and not isinstance(valeur, bool)
        and len(str(abs(valeur))) > V.MAX_INT_DIGITS
    ):
        raise _reject(
            f"Entier littéral trop grand: {len(str(abs(valeur)))} chiffres",
            "int_too_large",
            node,
        )
    if isinstance(valeur, bytes):
        raise _reject("Les littéraux d'octets sont refusés", "bytes_literal", node)
    if valeur is ...:
        raise _reject("Les points de suspension sont refusés", "ellipsis_literal", node)


def _check_binop(node: ast.BinOp) -> None:
    """L'exposant d'une puissance doit être une petite constante entière.

    ``10 ** 10 ** 10`` gèle l'interpréteur avant que la moindre limite ne
    puisse s'appliquer.
    """
    if not isinstance(node.op, ast.Pow):
        return
    droite = node.right
    if not isinstance(droite, ast.Constant) or not isinstance(droite.value, int):
        raise _reject(
            "L'exposant d'une puissance doit être une constante entière", "pow_exponent", node
        )
    if isinstance(droite.value, bool) or not 0 <= droite.value <= V.MAX_POW_EXPONENT:
        raise _reject(
            f"Exposant hors bornes: attendu entre 0 et {V.MAX_POW_EXPONENT}",
            "pow_exponent",
            node,
            exponent=droite.value,
        )


def _check_fstring(node: ast.JoinedStr) -> None:
    """Un format imbriqué permet une bombe de remplissage: ``f"{v:{'>' * n}}"``."""
    for partie in node.values:
        if isinstance(partie, ast.FormattedValue) and partie.format_spec is not None:
            for sous in ast.walk(partie.format_spec):
                if isinstance(sous, ast.FormattedValue):
                    raise _reject(
                        "Un format de chaîne ne peut pas être calculé", "nested_format", node
                    )


# ---------------------------------------------------------------------------
# Étage 4: terminaison
# ---------------------------------------------------------------------------


def _check_recursion(tree: ast.Module) -> None:
    """Refuse tout cycle dans le graphe d'appel des fonctions définies.

    Avec les boucles non bornées déjà interdites, il ne reste comme source de
    non-terminaison que des boucles bornées imbriquées. La terminaison n'est
    pas garantie pour autant, seulement rendue improbable: c'est la limite de
    temps du bac à sable qui tranche.
    """
    definies: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            appels = {
                enfant.func.id
                for enfant in ast.walk(node)
                if isinstance(enfant, ast.Call) and isinstance(enfant.func, ast.Name)
            }
            definies[node.name] = appels

    visite: set[str] = set()
    pile: set[str] = set()

    def descendre(nom: str) -> None:
        if nom in pile:
            raise ForgeRejected(
                f"Appel récursif détecté sur `{nom}`: une fonction pure ne doit pas "
                "se rappeler elle-même, la terminaison ne serait plus contrôlable",
                rule="recursion",
            )
        if nom in visite:
            return
        pile.add(nom)
        for appele in definies.get(nom, ()):
            if appele in definies:
                descendre(appele)
        pile.discard(nom)
        visite.add(nom)

    for nom in definies:
        descendre(nom)
