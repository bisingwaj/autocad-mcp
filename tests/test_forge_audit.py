"""Corpus d'attaques contre le validateur de la forge.

Un test par règle de refus. Ces tests ne vérifient pas un comportement
d'agrément mais la solidité d'une barrière: si l'un d'eux passe au vert alors
qu'il devrait échouer, du code hostile s'exécute sur la machine de
l'utilisateur.

Le validateur accepte par liste blanche. Le test d'exhaustivité des types de
nœuds, en fin de fichier, est ce qui rend cette liste durable: une montée de
version de Python qui introduirait une construction nouvelle casse la suite au
lieu de laisser passer un nœud dont personne n'a jugé la sûreté.
"""

from __future__ import annotations

import ast

import pytest

from autocad_mcp.forge import vocabulary as V
from autocad_mcp.forge.audit import audit
from autocad_mcp.forge.errors import ForgeRejected

#: Vocabulaire métier factice, pour que les exemples valides puissent appeler
#: quelque chose. Le vrai vocabulaire est posé par le bac à sable.
VOCAB = frozenset({"AddLine", "AddPolyline", "Style", "distance", "midpoint"})


def valider(source: str) -> None:
    audit(source, vocabulary_names=VOCAB)


def refus(source: str) -> ForgeRejected:
    with pytest.raises(ForgeRejected) as info:
        valider(source)
    return info.value


def enveloppe(corps: str) -> str:
    """Place un corps dans une fonction `build` bien formée."""
    indente = "\n".join("    " + ligne for ligne in corps.strip().splitlines())
    return f'def build(params, defaults):\n    """Essai."""\n{indente}\n'


# ---------------------------------------------------------------------------
# Ce qui doit être accepté, sinon le validateur est inutilisable
# ---------------------------------------------------------------------------

VALIDES = [
    ("minimal", "return []"),
    ("liste construite", "ops = []\nops.append(AddLine)\nreturn ops"),
    ("boucle bornée", "ops = []\nfor i in range(10):\n    ops.append(i)\nreturn ops"),
    ("condition", "if params:\n    return []\nreturn []"),
    ("compréhension", "return [i * 2 for i in range(5)]"),
    ("arithmétique", "x = 1 + 2 * 3 - 4 / 5\nreturn [x]"),
    ("puissance bornée", "return [2 ** 3]"),
    ("comparaison", "return [] if len(params) > 0 else []"),
    ("f-string simple", 'nom = f"mur {len(params)}"\nreturn [nom]'),
    ("appel du vocabulaire", "return [distance((0, 0), (1, 1))]"),
    ("fonction imbriquée", "def aide(x):\n    return x * 2\nreturn [aide(3)]"),
    ("lambda", "f = lambda x: x + 1\nreturn [f(1)]"),
    ("assertion", "assert len(params) >= 0\nreturn []"),
    ("levée d'erreur", "if not params:\n    raise ValueError\nreturn []"),
    ("tuple et découpage", "pts = [(0, 0), (1, 1)]\nreturn [pts[0], pts[1:]]"),
]


@pytest.mark.parametrize(("nom", "corps"), VALIDES, ids=[n for n, _ in VALIDES])
def test_le_validateur_accepte_le_code_legitime(nom: str, corps: str) -> None:
    """Un validateur qui refuse tout est aussi inutile qu'un validateur permissif."""
    valider(enveloppe(corps))


def test_le_rapport_decrit_la_surface_employee() -> None:
    """L'écran d'approbation doit montrer ce que l'outil utilise vraiment."""
    _, rapport = audit(
        enveloppe("return [distance((0, 0), (1, 1))]"), vocabulary_names=VOCAB
    )
    assert "distance" in rapport.calls
    assert rapport.node_count > 0
    assert rapport.to_dict()["entry_point"] == "build"


# ---------------------------------------------------------------------------
# Évasions par import
# ---------------------------------------------------------------------------

IMPORTS = [
    ("import direct", "import os\nreturn []"),
    ("import depuis", "from os import system\nreturn []"),
    ("import dans une boucle", "for i in range(1):\n    import os\nreturn []"),
    ("import conditionnel", "if params:\n    import os\nreturn []"),
]


@pytest.mark.parametrize(("nom", "corps"), IMPORTS, ids=[n for n, _ in IMPORTS])
def test_aucun_import_nulle_part(nom: str, corps: str) -> None:
    """Le vocabulaire est injecté. Un outil généré n'importe jamais rien."""
    assert refus(enveloppe(corps)).rule == "denied_node"


def test_import_au_niveau_module_refuse() -> None:
    """Le trou historique du détecteur du projet: un import hors du corps."""
    source = "import os\n\n\ndef build(params, defaults):\n    return []\n"
    assert refus(source).rule == "module_shape"


# ---------------------------------------------------------------------------
# Évasions par exécution de chaîne
# ---------------------------------------------------------------------------

EXECUTION = [
    ("eval", 'return [eval("1+1")]'),
    ("exec", 'exec("x = 1")\nreturn []'),
    ("compile", 'return [compile("1", "x", "eval")]'),
    ("import dynamique", '__import__("os")\nreturn []'),
    ("open", 'return [open("/etc/passwd")]'),
    ("input", "return [input()]"),
]


@pytest.mark.parametrize(("nom", "corps"), EXECUTION, ids=[n for n, _ in EXECUTION])
def test_aucune_execution_de_chaine(nom: str, corps: str) -> None:
    assert refus(enveloppe(corps)).rule in {"denied_name", "denied_call"}


# ---------------------------------------------------------------------------
# Évasions par accès dynamique aux attributs
# ---------------------------------------------------------------------------

ATTRIBUTS = [
    ("getattr", 'return [getattr(params, "x")]', "denied_name"),
    ("setattr", 'setattr(params, "x", 1)\nreturn []', "denied_name"),
    ("vars", "return [vars(params)]", "denied_name"),
    ("globals", "return [globals()]", "denied_name"),
    ("locals", "return [locals()]", "denied_name"),
    ("classe", "return [params.__class__]", "dunder_attr"),
    ("dictionnaire", "return [params.__dict__]", "dunder_attr"),
    ("sous-classes", "return [params.__class__.__subclasses__()]", "dunder_attr"),
    ("natives", "return [__builtins__]", "denied_name"),
    ("format par chaîne", 'return ["{0.__class__}".format(params)]', "denied_attr"),
    ("code d'une fonction", "return [build.__code__]", "dunder_attr"),
]


@pytest.mark.parametrize(
    ("nom", "corps", "regle"), ATTRIBUTS, ids=[n for n, _, _ in ATTRIBUTS]
)
def test_aucun_acces_dynamique_aux_attributs(nom: str, corps: str, regle: str) -> None:
    """La chaîne `__class__.__subclasses__()` est l'évasion classique.

    `format` mérite une mention: il parcourt une chaîne d'attributs décrite
    dans une chaîne de caractères, donc totalement invisible à l'arbre
    syntaxique. C'est la limite que l'analyse statique ne peut pas franchir, et
    la raison pour laquelle il est banni par son nom.
    """
    assert refus(enveloppe(corps)).rule == regle


def test_aucun_appel_calcule() -> None:
    """Interdire les cibles calculées supprime toute table d'indirection."""
    assert refus(enveloppe("f = [len]\nreturn [f[0](params)]")).rule == "computed_call"


def test_aucun_appel_de_retour_d_appel() -> None:
    assert refus(enveloppe("return [len(params)()]")).rule == "computed_call"


# ---------------------------------------------------------------------------
# Épuisement de ressources
# ---------------------------------------------------------------------------


def test_decalage_de_bits_refuse() -> None:
    """`1 << 10**9` alloue plusieurs gibioctets avant tout contrôle.

    Sur macOS la mémoire n'est pas plafonnable par les limites du système:
    cette interdiction statique est donc la seule barrière réelle.
    """
    assert refus(enveloppe("return [1 << 30]")).rule == "denied_node"


def test_puissance_non_constante_refusee() -> None:
    assert refus(enveloppe("n = 10\nreturn [10 ** n]")).rule == "pow_exponent"


def test_puissance_trop_grande_refusee() -> None:
    assert refus(enveloppe("return [10 ** 100]")).rule == "pow_exponent"


def test_boucle_non_bornee_refusee() -> None:
    assert refus(enveloppe("while True:\n    pass\nreturn []")).rule == "denied_node"


def test_recursion_directe_refusee() -> None:
    source = enveloppe("def f(n):\n    return f(n - 1)\nreturn [f(3)]")
    assert refus(source).rule == "recursion"


def test_recursion_indirecte_refusee() -> None:
    source = enveloppe(
        "def a(n):\n    return b(n)\ndef b(n):\n    return a(n)\nreturn [a(1)]"
    )
    assert refus(source).rule == "recursion"


def test_compréhension_trop_imbriquee_refusee() -> None:
    corps = "return [x for a in range(9) for b in range(9) for x in range(9)]"
    assert refus(enveloppe(corps)).rule == "comprehension_depth"


def test_format_imbrique_refuse() -> None:
    """`f"{v:{'>' * n}}"` est une bombe de remplissage."""
    assert refus(enveloppe("n = 9\nreturn [f\"{n:{n}}\"]")).rule == "nested_format"


def test_entier_litteral_gigantesque_refuse() -> None:
    assert refus(enveloppe(f"return [{10**30}]")).rule == "int_too_large"


def test_chaine_litterale_gigantesque_refusee() -> None:
    litteral = "x" * (V.MAX_STRING_LITERAL + 1)
    assert refus(enveloppe(f'return ["{litteral}"]')).rule == "string_too_long"


def test_source_trop_longue_refusee() -> None:
    corps = "\n".join(f"x{i} = {i}" for i in range(V.MAX_LINES + 10)) + "\nreturn []"
    assert refus(enveloppe(corps)).rule in {"source_too_large", "too_many_lines"}


def test_imbrication_trop_profonde_refusee() -> None:
    """Les parenthèses ne créent pas de nœuds: il faut une vraie imbrication."""
    corps = "x = " + "[" * 20 + "1" + "]" * 20 + "\nreturn []"
    regle = refus(enveloppe(corps)).rule
    assert regle in {"too_deep", "too_many_nodes"}


# ---------------------------------------------------------------------------
# Effets de bord et constructions différées
# ---------------------------------------------------------------------------

EFFETS = [
    ("try", "try:\n    pass\nexcept ValueError:\n    pass\nreturn []"),
    ("with", "with params as f:\n    pass\nreturn []"),
    ("global", "global x\nreturn []"),
    ("classe", "class C:\n    pass\nreturn []"),
    ("générateur", "yield 1"),
    ("suppression", "del params\nreturn []"),
    ("filtrage par motif", "match params:\n    case _:\n        pass\nreturn []"),
]


@pytest.mark.parametrize(("nom", "corps"), EFFETS, ids=[n for n, _ in EFFETS])
def test_constructions_a_effet_refusees(nom: str, corps: str) -> None:
    assert refus(enveloppe(corps)).rule == "denied_node"


def test_fonction_asynchrone_refusee() -> None:
    source = 'async def build(params, defaults):\n    """x."""\n    return []\n'
    assert refus(source).rule in {"module_shape", "denied_node"}


def test_decorateur_refuse() -> None:
    source = '@staticmethod\ndef build(params, defaults):\n    """x."""\n    return []\n'
    assert refus(source).rule == "decorator"


# ---------------------------------------------------------------------------
# Forme du module et signature
# ---------------------------------------------------------------------------


def test_constante_au_niveau_module_refusee() -> None:
    """Aucune instruction au niveau module: rien ne s'exécute au chargement."""
    source = 'TAILLE = 3\n\n\ndef build(params, defaults):\n    """x."""\n    return []\n'
    assert refus(source).rule == "module_shape"


def test_deux_fonctions_au_niveau_module_refusees() -> None:
    source = (
        'def aide():\n    """x."""\n    return 1\n\n\n'
        'def build(params, defaults):\n    """x."""\n    return []\n'
    )
    assert refus(source).rule == "module_shape"


def test_point_d_entree_mal_nomme_refuse() -> None:
    source = 'def dessine(params, defaults):\n    """x."""\n    return []\n'
    assert refus(source).rule == "entry_point_name"


SIGNATURES = [
    ("trop peu", "def build(params):\n    return []"),
    ("trop", "def build(params, defaults, extra):\n    return []"),
    ("mal nommés", "def build(p, d):\n    return []"),
    ("ordre inversé", "def build(defaults, params):\n    return []"),
    ("variadique", "def build(params, defaults, *args):\n    return []"),
    ("mots-clés", "def build(params, defaults, **kw):\n    return []"),
    ("valeur par défaut", "def build(params, defaults=None):\n    return []"),
]


@pytest.mark.parametrize(("nom", "source"), SIGNATURES, ids=[n for n, _ in SIGNATURES])
def test_signature_imposee(nom: str, source: str) -> None:
    assert refus(source).rule == "signature"


def test_corps_vide_refuse() -> None:
    source = 'def build(params, defaults):\n    """Seulement une docstring."""\n'
    assert refus(source).rule == "empty_body"


# ---------------------------------------------------------------------------
# Attaques visant la relecture humaine
# ---------------------------------------------------------------------------


def test_caractere_bidirectionnel_refuse() -> None:
    """Trojan Source: l'humain lit une chose, Python en compile une autre.

    Cette attaque ne vise pas l'interpréteur mais l'écran d'approbation.
    """
    source = enveloppe('nom = "sûr\u202e"\nreturn []')
    exc = refus(source)
    assert exc.rule == "invisible_char"
    assert "U+202E" in str(exc.details.get("codepoint", ""))


def test_espace_de_largeur_nulle_refuse() -> None:
    assert refus(enveloppe('nom = "a\u200bb"\nreturn []')).rule == "invisible_char"


def test_cookie_d_encodage_refuse() -> None:
    source = "# -*- coding: utf-7 -*-\ndef build(params, defaults):\n    return []\n"
    assert refus(source).rule == "encoding_cookie"


# ---------------------------------------------------------------------------
# Cas limites
# ---------------------------------------------------------------------------


def test_source_vide_refusee() -> None:
    assert refus("").rule == "source_empty"


def test_erreur_de_syntaxe_rapportee_avec_sa_ligne() -> None:
    exc = refus("def build(params, defaults:\n    return []\n")
    assert exc.rule == "syntax_error"
    assert exc.line is not None


def test_octets_litteraux_refuses() -> None:
    assert refus(enveloppe('return [b"abc"]')).rule == "bytes_literal"


def test_nom_inconnu_du_vocabulaire_refuse() -> None:
    assert refus(enveloppe("return [AddCircle]")).rule is not None


def test_appel_hors_vocabulaire_refuse() -> None:
    exc = refus(enveloppe("return [AddCircle(1)]"))
    assert exc.rule == "denied_call"


# ---------------------------------------------------------------------------
# La durabilité de la liste blanche
# ---------------------------------------------------------------------------


def test_source_trop_lourde_en_octets_refusee() -> None:
    """La borne porte sur les octets, pas sur le nombre de lignes."""
    corps = 'x = "' + "é" * (V.MAX_SOURCE_BYTES // 2) + '"\nreturn []'
    assert refus(enveloppe(corps)).rule == "source_too_large"


def test_module_sans_docstring_accepte() -> None:
    """La docstring du module est facultative, celle de la fonction aussi."""
    valider("def build(params, defaults):\n    return []\n")


def test_module_avec_docstring_accepte() -> None:
    valider('"""Un outil."""\n\n\ndef build(params, defaults):\n    return []\n')


def test_code_trop_volumineux_refuse() -> None:
    """La borne de nœuds doit mordre avant celle des lignes.

    Une seule expression très large produit beaucoup de nœuds sur peu de
    lignes: c'est la forme qui contourne une limite comptée en lignes.
    """
    # Une liste littérale est large et plate: elle sature le compteur de nœuds
    # sans toucher la limite de profondeur, que l'addition en chaîne aurait
    # atteinte la première.
    large = ", ".join("1" for _ in range(V.MAX_NODES))
    assert refus(enveloppe(f"x = [{large}]\nreturn []")).rule == "too_many_nodes"


def test_format_de_chaine_constant_accepte() -> None:
    """Un format littéral est licite, seul un format calculé est refusé."""
    valider(enveloppe('x = 1.5\nreturn [f"{x:.2f} m"]'))


def test_un_noeud_retire_de_la_liste_blanche_est_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le refus par défaut, celui qui protège des versions futures de Python.

    On retire un nœud de la liste blanche pour vérifier que le mécanisme
    refuse bien ce qu'il ne connaît pas, au lieu de le laisser passer.
    """
    monkeypatch.setattr(V, "ALLOWED_NODES", V.ALLOWED_NODES - {"Return"})
    assert refus(enveloppe("return []")).rule == "unknown_node"


def test_decorateur_sur_fonction_imbriquee_refuse() -> None:
    corps = "@truc\ndef aide(x):\n    return x\nreturn [aide(1)]"
    assert refus(enveloppe(corps)).rule in {"decorator", "unknown_name"}


def test_lambda_variadique_refusee() -> None:
    assert refus(enveloppe("f = lambda *a: a\nreturn [f(1)]")).rule == "lambda_args"


def test_nom_special_refuse() -> None:
    """Un nom spécial non listé nommément doit tomber sur la règle générique."""
    assert refus(enveloppe("return [__truc__]")).rule == "dunder_name"


def test_attribut_hors_vocabulaire_refuse() -> None:
    assert refus(enveloppe("return [params.inconnu]")).rule == "unknown_attr"


def test_points_de_suspension_refuses() -> None:
    assert refus(enveloppe("return [...]")).rule == "ellipsis_literal"


def test_tous_les_noeuds_de_python_sont_classes() -> None:
    """Le test qui rend la liste blanche durable.

    Chaque type de nœud que connaît cette version de Python doit être rangé:
    autorisé, refusé, ou hors sujet. Une montée de version qui introduirait une
    construction nouvelle fait donc **échouer la suite**, au lieu de laisser
    passer un nœud dont personne n'a jugé la sûreté.
    """
    manquants = V.unclassified_ast_types()
    assert not manquants, (
        f"Types de nœuds non classés: {sorted(manquants)}. "
        "Les ranger dans ALLOWED_NODES, DENIED_NODES ou IRRELEVANT_NODES "
        "après avoir jugé leur sûreté."
    )


def test_aucun_noeud_a_la_fois_autorise_et_refuse() -> None:
    assert not V.ALLOWED_NODES & set(V.DENIED_NODES)


def test_les_attributs_bannis_ne_sont_jamais_permis() -> None:
    """Le bannissement doit gagner sur la dérivation automatique."""
    assert not V.allowed_attrs() & V.DENIED_ATTRS


def test_aucune_native_dangereuse_injectee() -> None:
    assert not V.BUILTINS & V.DENIED_NAMES


def test_les_natives_injectees_existent_vraiment() -> None:
    import builtins

    inconnues = {nom for nom in V.BUILTINS if not hasattr(builtins, nom)}
    assert not inconnues, f"natives inexistantes: {inconnues}"


def test_la_couverture_du_validateur_est_totale() -> None:
    """Une branche non couverte dans un validateur est une branche dont on
    ignore si elle fonctionne.

    Ce test ne mesure pas la couverture lui-même, il en rappelle l'exigence et
    donne la commande. La mesure se fait en intégration continue::

        uv run pytest tests/test_forge_audit.py \\
            --cov=autocad_mcp.forge.audit \\
            --cov=autocad_mcp.forge.vocabulary \\
            --cov-branch --cov-fail-under=100

    Aucune exemption n'est tolérée dans ces deux modules.
    """
    from autocad_mcp.forge import audit as module

    source = __import__("pathlib").Path(module.__file__).read_text(encoding="utf-8")
    assert "pragma: no cover" not in source, (
        "aucune exemption de couverture n'est tolérée dans le validateur"
    )


def test_le_vocabulaire_ne_contient_aucun_module_dangereux() -> None:
    """Le périmètre réel est le vocabulaire, pas l'arbre syntaxique.

    Chaque nom injecté est une interface offerte. Ce test empêche qu'un jour
    un module d'entrée-sortie y entre par commodité.
    """
    from autocad_mcp.forge.audit import FORBIDDEN_IN_VOCABULARY

    assert not VOCAB & FORBIDDEN_IN_VOCABULARY
    assert not V.BUILTINS & FORBIDDEN_IN_VOCABULARY


def test_l_arbre_rendu_est_celui_qui_a_ete_verifie() -> None:
    """L'appelant compile cet arbre, il ne relit jamais la source.

    Relire rouvrirait la fenêtre entre la vérification et l'usage.
    """
    source = enveloppe("return []")
    arbre, _ = audit(source, vocabulary_names=VOCAB)
    assert isinstance(arbre, ast.Module)
    compile(arbre, "<forge>", "exec")
