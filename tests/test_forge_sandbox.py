"""Le bac à sable tient-il vraiment ses promesses.

Ces tests exécutent du code hostile. Ils sont écrits **sans supposer que le
validateur a fait son travail**: c'est tout l'intérêt de la défense en
profondeur, chaque barrière doit tenir seule.

Ils sont plus lents que le reste de la suite, puisque chacun engendre un
processus. Les délais sont réduits au strict nécessaire.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from autocad_mcp.forge.budget import Budget, maxrss_to_bytes
from autocad_mcp.forge.errors import ForgeBudgetExceeded, ForgeCheckFailed
from autocad_mcp.forge.sandbox import run_sandboxed

#: Délai court: ces tests n'ont rien à calculer, seulement à être coupés.
BREF = Budget(timeout=0.6)


def build(corps: str) -> str:
    indente = "\n".join("    " + ligne for ligne in corps.strip().splitlines())
    return f'def build(params, defaults):\n    """Essai."""\n{indente}\n'


# ---------------------------------------------------------------------------
# Ce qui doit fonctionner
# ---------------------------------------------------------------------------


def test_un_outil_correct_rend_ses_operations() -> None:
    source = build(
        "return wall_network([(0.0,0.0),(6.0,0.0),(6.0,4.0),(0.0,4.0)],"
        " defaults, closed=True)"
    )
    resultat = run_sandboxed(source, {}, unit="m")
    genres = [op.kind for op in resultat.operations]
    assert genres.count("polyline") == 2
    assert "ensure_layer" in genres


def test_les_parametres_traversent_la_frontiere() -> None:
    source = build("return [AddLine((0.0,0.0,0.0), (params['x'], 0.0, 0.0))]")
    resultat = run_sandboxed(source, {"x": 7.5}, unit="m")
    assert resultat.operations[0].end[0] == pytest.approx(7.5)


def test_l_unite_du_document_est_respectee() -> None:
    """Un mur par défaut doit mesurer la même épaisseur physique partout."""
    source = build("return wall_network([(0.0,0.0),(1000.0,0.0)], defaults)")
    en_mm = run_sandboxed(source, {}, unit="mm")
    contour = next(op for op in en_mm.operations if op.kind == "polyline")
    hauteurs = {round(p[1], 6) for p in contour.points}
    assert hauteurs == {-100.0, 100.0}, "20 cm d'épaisseur en millimètres"


def test_le_rapport_dit_quelles_limites_protegent() -> None:
    """Le rapport doit refléter la plateforme, pas les intentions.

    Sur macOS la limite de mémoire est refusée par le noyau: le dire est plus
    utile que de laisser croire qu'elle s'applique.
    """
    resultat = run_sandboxed(build("return []"), {})
    assert "recursion" in resultat.limits_applied
    if sys.platform != "win32":
        assert "fsize" in resultat.limits_applied
    if sys.platform == "darwin":
        assert "as" not in resultat.limits_applied


def test_deux_executions_donnent_le_meme_resultat() -> None:
    """Le déterminisme est ce qui rend un outil testable."""
    source = build("return [AddCircle((1.0,2.0,0.0), 3.0)]")
    a = run_sandboxed(source, {})
    b = run_sandboxed(source, {})
    assert a.operations == b.operations


# ---------------------------------------------------------------------------
# Épuisement de ressources: le parent doit survivre
# ---------------------------------------------------------------------------


def test_une_boucle_sans_fin_est_tuee() -> None:
    """Un fil ne se tue pas, un processus si. C'est toute la raison d'être
    de ce module."""
    with pytest.raises(ForgeBudgetExceeded) as info:
        run_sandboxed(build("while True:\n    pass"), {}, budget=BREF)
    assert "délai" in info.value.message


def test_une_allocation_massive_n_emporte_pas_le_parent() -> None:
    """Sur macOS la mémoire n'est pas plafonnable: seul le délai coupe."""
    with pytest.raises((ForgeBudgetExceeded, ForgeCheckFailed)):
        run_sandboxed(build("x = [0] * (4*1024*1024*1024)\nreturn []"), {}, budget=BREF)
    # Le parent est vivant, donc il peut enchaîner.
    assert run_sandboxed(build("return []"), {}).operations == []


def test_une_recursion_profonde_ne_fait_pas_tomber_le_parent() -> None:
    source = build("def f(n):\n    return f(n+1)\nreturn [f(0)]")
    with pytest.raises((ForgeCheckFailed, ForgeBudgetExceeded)):
        run_sandboxed(source, {}, budget=BREF)


# ---------------------------------------------------------------------------
# Évasions: l'espace de noms tient même sans le validateur
# ---------------------------------------------------------------------------


EVASIONS = [
    ("ouvrir un fichier", "return [open('/etc/passwd').read()]"),
    ("ecrire un fichier", "open('/tmp/forge-evasion.txt','w').write('x')\nreturn []"),
    ("importer un module", "import os\nreturn [os.getcwd()]"),
    ("import dynamique", "return [__import__('os').getcwd()]"),
    ("lancer un processus", "import subprocess\nreturn [subprocess.run(['ls'])]"),
    ("construire un objet", "return [object()]"),
    ("remonter aux natives", "return [().__class__.__base__.__subclasses__()]"),
    ("evaluer une chaine", "return [eval('1+1')]"),
    ("lire l'environnement", "import os\nreturn [os.environ]"),
]


@pytest.mark.parametrize(("nom", "corps"), EVASIONS, ids=[n for n, _ in EVASIONS])
def test_l_espace_de_noms_bloque_les_evasions(nom: str, corps: str) -> None:
    """Chaque barrière doit tenir seule.

    Ces sources ne passeraient pas le validateur, et c'est le but: on vérifie
    ici ce qui se passerait s'il avait un trou.
    """
    with pytest.raises((ForgeCheckFailed, ForgeBudgetExceeded)):
        run_sandboxed(build(corps), {}, budget=BREF)


def test_aucun_fichier_n_a_ete_ecrit() -> None:
    """Contrôle direct, après les tentatives d'écriture ci-dessus."""
    assert not Path("/tmp/forge-evasion.txt").exists()


def test_la_sortie_standard_de_l_enfant_est_isolee(capsys: pytest.CaptureFixture[str]) -> None:
    """Dans le serveur, la sortie standard porte le protocole.

    Un enfant qui en hériterait pourrait forger des trames et se répondre à
    lui-même. La sienne part vers le vide.
    """
    run_sandboxed(build("return []"), {})
    capture = capsys.readouterr()
    assert capture.out == ""


# ---------------------------------------------------------------------------
# Ce que l'enfant rend est revalidé par le noyau
# ---------------------------------------------------------------------------


def test_un_retour_qui_n_est_pas_une_liste_est_refuse() -> None:
    with pytest.raises(ForgeCheckFailed) as info:
        run_sandboxed(build("return 42"), {})
    assert "liste" in info.value.message


def test_une_operation_invalide_est_refusee_par_le_noyau() -> None:
    """Le bac à sable produit des données, le noyau décide si elles valent.

    Un rayon négatif traverse la frontière sans encombre, puis tombe sur la
    même validation que les vrais backends.
    """
    with pytest.raises(ForgeCheckFailed):
        run_sandboxed(build("return [AddCircle((0.0,0.0,0.0), -5.0)]"), {})


def test_un_type_d_operation_inconnu_est_refuse() -> None:
    """L'enfant ne peut pas faire construire au parent un type de son choix."""
    with pytest.raises(ForgeCheckFailed):
        run_sandboxed(build("return [{'kind': 'AddMalware', 'x': 1}]"), {})


def test_un_verdict_trop_volumineux_est_refuse() -> None:
    source = build("return [AddCircle((0.0,0.0,0.0), 1.0) for _ in range(200000)]")
    with pytest.raises((ForgeBudgetExceeded, ForgeCheckFailed)):
        run_sandboxed(source, {}, budget=Budget(timeout=3.0))


# ---------------------------------------------------------------------------
# Le budget lui-même
# ---------------------------------------------------------------------------


def test_la_conversion_de_memoire_suit_la_plateforme() -> None:
    """Darwin rend des octets, Linux des kibioctets.

    Se tromper d'un facteur mille vingt-quatre rend le garde-fou soit
    inopérant, soit toujours déclenché.
    """
    attendu = 1000 if sys.platform == "darwin" else 1000 * 1024
    assert maxrss_to_bytes(1000) == attendu


def test_un_delai_hors_bornes_est_refuse() -> None:
    with pytest.raises(ForgeBudgetExceeded):
        Budget(timeout=0.0)
    with pytest.raises(ForgeBudgetExceeded):
        Budget(timeout=999.0)


def test_le_vocabulaire_annonce_correspond_a_l_espace_reel() -> None:
    """Le validateur et l'exécuteur doivent lire la même liste.

    Sinon un nom accepté à la validation manquerait à l'exécution, ou
    l'inverse, et l'erreur rendue au modèle serait incompréhensible.
    """
    from autocad_mcp.forge.worker import build_namespace, vocabulary_names

    espace = build_namespace()
    for nom in vocabulary_names():
        assert nom in espace


def test_les_natives_dangereuses_sont_absentes_de_l_espace() -> None:
    from autocad_mcp.forge import vocabulary as V
    from autocad_mcp.forge.worker import build_namespace

    espace = build_namespace()
    for interdit in ("open", "eval", "exec", "__import__", "compile", "getattr"):
        assert interdit not in espace, f"{interdit} ne doit pas être offert"
        assert interdit not in espace["__builtins__"], f"{interdit} dans les natives"
    assert not set(espace["__builtins__"]) - V.BUILTINS
