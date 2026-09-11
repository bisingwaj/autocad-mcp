"""Verrous d'architecture.

Ces tests ne vérifient pas un comportement mais une propriété structurelle.
Chacun correspond à une règle dont la violation a un coût élevé et une
détection tardive.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "autocad_mcp"

#: Seul fichier autorisé à dépendre de l'automatisation Windows.
COM_MODULE = SRC / "backends" / "acad_com.py"

WINDOWS_ONLY = {"win32com", "pythoncom", "win32api", "win32gui"}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


def _is_type_checking(test: ast.expr) -> bool:
    """Reconnaît la garde ``if TYPE_CHECKING:`` sous ses deux écritures."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_level_imports(body: list[ast.stmt]) -> set[str]:
    """Modules importés par du code qui s'exécute au CHARGEMENT du fichier.

    Descend dans tout ce qui tourne à l'import: conditions, gestion d'erreur,
    gestionnaires de contexte, boucles, corps de classe. S'arrête aux corps de
    fonction, où un import paresseux est au contraire le mécanisme qui garde le
    backend AutoCAD chargeable sur macOS.

    Une garde ``if TYPE_CHECKING:`` est ignorée: elle est fausse à l'exécution.

    La version précédente ne regardait que ``tree.body`` et portait une branche
    morte sur ``ast.If`` dont la boucle interne ne faisait que ``continue``.
    Elle laissait donc passer un import placé dans un ``try:`` de haut niveau,
    c'est-à-dire exactement la forme qu'on écrit pour rendre une dépendance
    facultative.
    """
    found: set[str] = set()
    for node in body:
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue  # import paresseux, autorisé
        elif isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                found |= _module_level_imports(node.body)
            found |= _module_level_imports(node.orelse)
        elif isinstance(node, ast.Try):
            for bloc in (node.body, node.orelse, node.finalbody):
                found |= _module_level_imports(bloc)
            for handler in node.handlers:
                found |= _module_level_imports(handler.body)
        elif isinstance(node, ast.With | ast.AsyncWith | ast.For | ast.AsyncFor | ast.While):
            found |= _module_level_imports(node.body)
            found |= _module_level_imports(getattr(node, "orelse", []))
        elif isinstance(node, ast.ClassDef):
            # Le corps d'une classe s'exécute à l'import, comme le module.
            found |= _module_level_imports(node.body)
    return found


def _imported_modules(path: Path) -> set[str]:
    """Modules importés au chargement du fichier."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _module_level_imports(tree.body)


def _imports_of(source: str) -> set[str]:
    """Applique le détecteur à une source écrite dans le test."""
    return _module_level_imports(ast.parse(source).body)


# Le détecteur est lui-même un dispositif de sûreté: il est testé comme tel,
# et sa logique de parcours est réutilisée par le validateur de la forge.
DETECTEUR = [
    ("import nu", "import os", {"os"}),
    ("depuis", "from os import path", {"os"}),
    ("relatif ignoré", "from . import voisin", set()),
    ("dans une fonction", "def f():\n    import os", set()),
    ("dans une méthode", "class C:\n    def f(self):\n        import os", set()),
    ("corps de classe", "class C:\n    import os", {"os"}),
    ("sous condition", "if x:\n    import os", {"os"}),
    ("dans le sinon", "if x:\n    pass\nelse:\n    import os", {"os"}),
    ("garde de typage", "if TYPE_CHECKING:\n    import os", set()),
    ("garde qualifiée", "if typing.TYPE_CHECKING:\n    import os", set()),
    ("sinon d'une garde", "if TYPE_CHECKING:\n    pass\nelse:\n    import os", {"os"}),
    ("dans un try", "try:\n    import os\nexcept ImportError:\n    pass", {"os"}),
    ("dans un except", "try:\n    pass\nexcept ImportError:\n    import os", {"os"}),
    ("dans un finally", "try:\n    pass\nfinally:\n    import os", {"os"}),
    ("dans un with", "with ouvrir() as f:\n    import os", {"os"}),
    ("dans une boucle", "for x in y:\n    import os", {"os"}),
    ("imbriqué en profondeur", "if a:\n    try:\n        import os\n    except E:\n        pass", {"os"}),
]


@pytest.mark.parametrize(
    ("source", "attendu"),
    [(src, exp) for _, src, exp in DETECTEUR],
    ids=[nom for nom, _, _ in DETECTEUR],
)
def test_le_detecteur_d_imports_voit_ce_qui_s_execute(source: str, attendu: set[str]) -> None:
    """Régression: un import dans un `try:` de haut niveau passait inaperçu.

    L'ancienne version ne lisait que le corps du module et portait une branche
    morte sur les conditions. C'est la forme même qu'on écrit pour rendre une
    dépendance facultative, donc exactement celle qu'il fallait attraper.
    """
    assert _imports_of(source) == attendu


def test_le_backend_com_importe_bien_windows_mais_paresseusement() -> None:
    """Le contrepoint du test précédent, sur le seul fichier concerné.

    Sans cette vérification, supprimer par mégarde tout l'appel à `win32com`
    laisserait la suite verte alors que le backend ne piloterait plus rien.
    """
    source = COM_MODULE.read_text(encoding="utf-8")
    assert not _imports_of(source) & WINDOWS_ONLY, "l'import doit rester paresseux"
    assert "win32com" in source, "le backend doit bien piloter AutoCAD quelque part"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_aucun_import_windows_au_niveau_module(path: Path) -> None:
    """Le paquet doit rester chargeable sur macOS.

    Le code historique importait `win32com` en tête de fichier, ce qui rendait
    le projet entier inutilisable hors Windows et donc intestable.
    """
    offending = _imported_modules(path) & WINDOWS_ONLY
    assert not offending, (
        f"{path.name} importe {offending} au niveau module. "
        "Utiliser un import paresseux dans la méthode concernée."
    )


def test_le_paquet_s_importe_sur_cette_plateforme() -> None:
    import autocad_mcp

    assert autocad_mcp.__version__


def test_backend_com_importable_hors_windows() -> None:
    """La classe doit se charger partout, seule sa connexion doit échouer."""
    from autocad_mcp.backends.acad_com import AcadComBackend

    assert AcadComBackend.name == "autocad"


@pytest.mark.skipif(sys.platform == "win32", reason="comportement propre aux autres plateformes")
def test_connexion_autocad_echoue_proprement_hors_windows() -> None:
    """Une erreur typée, jamais un ImportError brut remontant au client."""
    from autocad_mcp.backends.acad_com import AcadComBackend
    from autocad_mcp.errors import BackendUnavailable

    with pytest.raises(BackendUnavailable):
        AcadComBackend().connect()


LOGIC_LAYERS = ["ops", "model"]


@pytest.mark.parametrize("layer", LOGIC_LAYERS)
def test_la_logique_metier_ignore_les_backends(layer: str) -> None:
    """La logique décide quoi dessiner, elle n'exécute pas.

    C'est la règle qui rend le projet testable sans AutoCAD.
    """
    violations = []
    for path in (SRC / layer).rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "backends" in text and "import" in text:
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")) and "backends" in stripped:
                    violations.append(f"{path.name}:{lineno} {stripped}")
    assert not violations, "La logique métier importe un backend: " + "; ".join(violations)


def test_geometry_ne_depend_de_rien_du_projet() -> None:
    """Le module géométrique doit rester une bibliothèque pure.

    Seul `errors` est toléré, pour signaler les géométries dégénérées.
    """
    path = SRC / "geometry.py"
    if not path.exists():
        pytest.skip("geometry.py pas encore écrit")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
    }
    assert internal <= {"errors", "units", "model.ops"}, (
        f"geometry.py dépend de {internal - {'errors', 'units', 'model.ops'}}"
    )


def test_aucun_except_nu_dans_le_paquet() -> None:
    """Les blocs `except:` sans classe masquent les erreurs.

    Le code historique en comptait vingt-huit, ce qui rendait tout diagnostic
    impossible.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "Blocs except nus: " + ", ".join(offenders)


def test_aucun_sleep_de_confort() -> None:
    """L'ancien code dormait une seconde en dur après chaque opération.

    Un rectangle coûtait quatre secondes, une maison plus d'une minute. Une
    temporisation reste légitime quand elle répond à un signal réel, par
    exemple un refus d'appel par AutoCAD, mais elle doit alors être portée par
    une constante nommée et réglable. Un littéral numérique dans l'appel
    signale au contraire un délai choisi au jugé.

    L'analyse porte sur l'arbre syntaxique, donc ni les commentaires ni les
    docstrings ne peuvent déclencher de faux positif.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_sleep = (
                isinstance(func, ast.Attribute) and func.attr == "sleep"
            ) or (isinstance(func, ast.Name) and func.id == "sleep")
            if not is_sleep or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int | float):
                offenders.append(f"{path.name}:{node.lineno} time.sleep({arg.value})")
    assert not offenders, (
        "Temporisation en dur, utiliser une constante nommée: " + "; ".join(offenders)
    )
