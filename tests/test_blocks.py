"""Bibliothèque de blocs et style de cote du projet.

Deux sujets, un seul fichier, parce qu'ils partagent la même exigence: ce qui
est écrit dans le DXF doit être **relu** pour compter. Un test qui affirme sur
l'objet Python en mémoire ne prouve rien de ce qu'un autre logiciel ouvrira.

Le piège de cotation vérifié ici est documenté dans `docs/reste-a-faire.md`: le
style `EZDXF` livré par la bibliothèque porte `dimlfac = 100`, si bien qu'une
cote de six mètres s'affiche « 600 ». Une cote fausse se lit sans se relire,
c'est pire qu'une cote absente.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import ezdxf
import pytest
from ezdxf.audit import Auditor
from ezdxf.document import Drawing

from autocad_mcp.backends.base import MEASURE_KEYS, BatchResult, EntityFilter
from autocad_mcp.backends.ezdxf_be import PROJECT_DIMSTYLE, EzdxfBackend
from autocad_mcp.errors import InvalidGeometry, InvalidParameter
from autocad_mcp.model.layers import STANDARD_LAYERS
from autocad_mcp.model.ops import (
    AddArc,
    AddBlockRef,
    AddCircle,
    AddDimAligned,
    AddHatch,
    AddLine,
    AddPolyline,
    AddText,
    AttributeDef,
    DefineBlock,
    EnsureLayer,
    Operation,
    OperationBatch,
    Style,
    validate,
)
from autocad_mcp.ops import blocks
from autocad_mcp.units import Defaults, Unit

METERS = Defaults(Unit.METER)


def _run(backend: EzdxfBackend, ops: list[Operation], label: str = "test") -> BatchResult:
    """Exécute un lot et exige qu'il ait réussi entièrement."""
    result = backend.execute(OperationBatch(tuple(ops), label=label))
    assert result.ok, result.failures
    return result


def _reread(backend: EzdxfBackend, tmp_path: Path, name: str) -> Drawing:
    """Enregistre, relit, audite. C'est la seule preuve qui vaille.

    Un document ``ezdxf`` en mémoire peut être incohérent sans que rien ne le
    signale: c'est la relecture qui dit si le fichier s'ouvrira ailleurs.
    """
    path = backend.save(str(tmp_path / name))
    doc = ezdxf.readfile(path)
    auditor = Auditor(doc)
    auditor.run()
    assert not auditor.errors, [str(e) for e in auditor.errors]
    return doc


@pytest.fixture
def backend() -> EzdxfBackend:
    be = EzdxfBackend(unit=Unit.METER)
    be.connect()
    return be


def _simple_block(name: str = "TEST_BLOC") -> DefineBlock:
    return DefineBlock(
        name=name,
        operations=(AddLine(start=(0.0, 0.0, 0.0), end=(1.0, 0.0, 0.0)),),
        attributes=(AttributeDef(tag="REPERE", height=0.15, default="X1"),),
    )


# ---------------------------------------------------------------------------
# Validation du modèle
# ---------------------------------------------------------------------------


def test_une_definition_de_bloc_bien_formee_passe_la_validation() -> None:
    validate(_simple_block())


def test_un_bloc_sans_nom_est_refuse() -> None:
    with pytest.raises(InvalidParameter):
        validate(DefineBlock(name="  ", operations=(AddLine((0, 0, 0), (1, 0, 0)),)))


def test_un_bloc_sans_contenu_est_refuse() -> None:
    """Un bloc vide s'insère sans erreur et ne dessine rien: pire qu'un échec."""
    with pytest.raises(InvalidParameter):
        validate(DefineBlock(name="VIDE"))


def test_un_bloc_ne_peut_pas_contenir_un_autre_bloc() -> None:
    """La table des blocs appartient au document, pas à un bloc."""
    with pytest.raises(InvalidParameter):
        validate(DefineBlock(name="A", operations=(_simple_block("B"),)))


def test_un_bloc_ne_peut_pas_contenir_un_calque() -> None:
    with pytest.raises(InvalidParameter):
        validate(DefineBlock(name="A", operations=(EnsureLayer("WALLS", 7),)))


def test_le_contenu_d_un_bloc_est_valide_comme_le_reste() -> None:
    """Un rayon négatif dans un bloc reste un rayon négatif."""
    with pytest.raises(InvalidGeometry):
        validate(DefineBlock(name="A", operations=(AddCircle((0, 0, 0), -1.0),)))


def test_deux_attributs_de_meme_etiquette_sont_refuses() -> None:
    """Une étiquette en double rend la nomenclature ambiguë."""
    with pytest.raises(InvalidParameter):
        validate(
            DefineBlock(
                name="A",
                attributes=(
                    AttributeDef(tag="REPERE", height=0.15),
                    AttributeDef(tag="repere", height=0.15),
                ),
            )
        )


@pytest.mark.parametrize("tag", ["", "   ", "MON REPERE"])
def test_une_etiquette_vide_ou_avec_espace_est_refusee(tag: str) -> None:
    with pytest.raises(InvalidParameter):
        validate(DefineBlock(name="A", attributes=(AttributeDef(tag=tag, height=0.15),)))


def test_un_attribut_sans_hauteur_est_refuse() -> None:
    with pytest.raises(InvalidGeometry):
        validate(DefineBlock(name="A", attributes=(AttributeDef(tag="R", height=0.0),)))


def test_une_definition_de_bloc_ne_porte_pas_de_couleur() -> None:
    """Le BLOCK du format DXF n'a qu'un calque: accepter une couleur la perdrait."""
    with pytest.raises(InvalidParameter):
        validate(
            DefineBlock(
                name="A",
                operations=(AddLine((0, 0, 0), (1, 0, 0)),),
                style=Style(layer="0", color=1),
            )
        )


# ---------------------------------------------------------------------------
# La bibliothèque, fonctions pures
# ---------------------------------------------------------------------------


def test_la_bibliotheque_couvre_le_catalogue_annonce() -> None:
    attendu = {
        "door", "window", "toilet", "basin", "shower", "sink",
        "outlet", "switch", "light", "bed_single", "bed_double", "table", "chair",
    }
    assert set(blocks.LIBRARY) == attendu


@pytest.mark.parametrize("key", blocks.LIBRARY)
def test_chaque_bloc_rend_son_calque_puis_sa_definition(key: str) -> None:
    ops = blocks.define(key, METERS)
    assert isinstance(ops[0], EnsureLayer)
    assert isinstance(ops[-1], DefineBlock)
    # Le calque est garanti avant que quoi que ce soit ne s'y dépose.
    assert len(ops) == 2


@pytest.mark.parametrize("key", blocks.LIBRARY)
def test_chaque_bloc_porte_un_repere(key: str) -> None:
    """Sans attribut, une occurrence ne dit rien de plus que sa présence."""
    definition = blocks.define(key, METERS)[-1]
    assert isinstance(definition, DefineBlock)
    tags = [a.tag for a in definition.attributes]
    assert blocks.MARK_TAG in tags
    mark = next(a for a in definition.attributes if a.tag == blocks.MARK_TAG)
    assert mark.default.strip(), "le repère doit proposer une valeur de départ"
    assert mark.height == METERS.attribute_height


@pytest.mark.parametrize(
    ("key", "layer"),
    [
        ("door", "DOORS"),
        ("window", "WINDOWS"),
        ("toilet", "PLUMBING"),
        ("basin", "PLUMBING"),
        ("shower", "PLUMBING"),
        ("sink", "PLUMBING"),
        ("outlet", "ELECTRICAL"),
        ("switch", "ELECTRICAL"),
        ("light", "ELECTRICAL"),
        ("bed_single", "FURNITURE"),
        ("bed_double", "FURNITURE"),
        ("table", "FURNITURE"),
        ("chair", "FURNITURE"),
    ],
)
def test_chaque_bloc_va_sur_le_calque_de_sa_nature(key: str, layer: str) -> None:
    """Le calque vient de model.layers, jamais d'un nom écrit en dur ici."""
    layer_op, definition = blocks.define(key, METERS)
    assert isinstance(layer_op, EnsureLayer)
    assert isinstance(definition, DefineBlock)
    assert layer_op.name == layer
    assert layer_op.color == next(
        s.color for s in STANDARD_LAYERS.values() if s.name == layer
    )
    posed = [op.style.layer for op in definition.operations]
    posed += [a.style.layer for a in definition.attributes]
    assert set(posed) == {layer}, "géométrie et repère sur le calque de la nature"


@pytest.mark.parametrize("key", blocks.LIBRARY)
def test_chaque_bloc_a_un_contenu_geometrique(key: str) -> None:
    definition = blocks.define(key, METERS)[-1]
    assert isinstance(definition, DefineBlock)
    assert definition.operations, "un symbole sans trait ne se voit pas"


@pytest.mark.parametrize("key", blocks.LIBRARY)
def test_chaque_bloc_passe_la_validation_commune(key: str) -> None:
    for op in blocks.define(key, METERS):
        validate(op)


def test_les_blocs_sont_prefixes_pour_ne_rien_ecraser() -> None:
    """Un plan tiers a souvent déjà un bloc nommé PORTE ou WC."""
    for key in blocks.LIBRARY:
        assert blocks.block_name(key).startswith(blocks.NAME_PREFIX)


def test_une_cle_inconnue_est_une_erreur_immediate() -> None:
    with pytest.raises(InvalidParameter):
        blocks.define("baignoire", METERS)


def test_les_dimensions_suivent_l_unite_du_document() -> None:
    """Un lit deux places fait 1.40 m, que le dessin soit en mètres ou en mm.

    C'est la règle du projet: aucune longueur sans unité. L'ancien code écrivait
    0.1 pour une épaisseur de mur, invisible dans un dessin en millimètres.
    """
    en_metres = blocks.define("bed_double", Defaults(Unit.METER))[-1]
    en_mm = blocks.define("bed_double", Defaults(Unit.MILLIMETER))[-1]
    assert isinstance(en_metres, DefineBlock)
    assert isinstance(en_mm, DefineBlock)

    def largeur(definition: DefineBlock) -> float:
        xs = [
            p[0]
            for op in definition.operations
            if op.kind == "polyline"
            for p in op.points  # type: ignore[attr-defined]
        ]
        return max(xs) - min(xs)

    assert largeur(en_metres) == pytest.approx(1.40)
    assert largeur(en_mm) == pytest.approx(1400.0)
    assert en_mm.attributes[0].height == pytest.approx(150.0)


def test_l_insertion_definit_le_bloc_et_pose_une_occurrence() -> None:
    ops = blocks.insert("chair", (2.0, 3.0), METERS, rotation_deg=90.0, mark="CH-07")
    reference = ops[-1]
    assert isinstance(reference, AddBlockRef)
    assert reference.name == blocks.block_name("chair")
    assert reference.insert == (2.0, 3.0, 0.0)
    # Degrés à la frontière publique, radians dans le modèle.
    assert reference.rotation == pytest.approx(math.pi / 2.0)
    assert reference.attributes == ((blocks.MARK_TAG, "CH-07"),)
    assert reference.style.layer == "FURNITURE"
    assert any(isinstance(op, DefineBlock) for op in ops)


def test_une_echelle_nulle_est_refusee_a_l_insertion() -> None:
    with pytest.raises(InvalidParameter):
        blocks.insert("table", (0.0, 0.0), METERS, scale=0.0)


def test_la_bibliotheque_entiere_se_definit_en_un_appel() -> None:
    ops = blocks.define_library(METERS)
    noms = {op.name for op in ops if isinstance(op, DefineBlock)}
    assert noms == {blocks.block_name(k) for k in blocks.LIBRARY}


# ---------------------------------------------------------------------------
# Exécution par le backend DXF
# ---------------------------------------------------------------------------


def test_un_bloc_defini_n_est_pas_compte_comme_une_entite(backend: EzdxfBackend) -> None:
    """Une définition vit dans la table des blocs, elle n'est pas dessinée.

    La compter parmi les entités créées gonflerait le nombre d'objets et
    ferait désigner une entrée de table là où l'appelant attend quelque chose
    d'effaçable.
    """
    result = _run(backend, [_simple_block()])
    assert result.created == []
    assert result.blocks == ["TEST_BLOC"]
    assert "TEST_BLOC" in backend.document.blocks


def test_le_contenu_du_bloc_est_ecrit_dans_sa_definition(backend: EzdxfBackend) -> None:
    _run(backend, blocks.define("door", METERS))
    block = backend.document.blocks.get(blocks.block_name("door"))
    kinds = sorted(e.dxftype() for e in block if e.dxftype() != "ATTDEF")
    assert kinds == ["ARC", "LINE", "LWPOLYLINE"]
    assert [a.dxf.tag for a in block.attdefs()] == [blocks.MARK_TAG]
    assert {e.dxf.layer for e in block} == {"DOORS"}


def test_le_point_de_base_est_respecte(backend: EzdxfBackend) -> None:
    ops: list[Operation] = [
        DefineBlock(
            name="DECALE",
            base_point=(1.5, -2.0, 0.0),
            operations=(AddLine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),),
        )
    ]
    _run(backend, ops)
    block = backend.document.blocks.get("DECALE")
    assert tuple(block.block.dxf.base_point)[:2] == (1.5, -2.0)


def test_redefinir_un_bloc_existant_ne_l_ecrase_pas(backend: EzdxfBackend) -> None:
    """Sémantique de garantie, comme EnsureLayer.

    Un plan qui insère dix fois la même porte redéfinit dix fois le bloc. Il ne
    doit ni échouer, ni écraser les occurrences déjà posées.
    """
    _run(backend, [_simple_block()])
    avant = len(list(backend.document.blocks.get("TEST_BLOC")))
    result = _run(
        backend,
        [
            DefineBlock(
                name="TEST_BLOC",
                operations=(AddCircle((0.0, 0.0, 0.0), 5.0),),
                attributes=(AttributeDef(tag="AUTRE", height=0.15),),
            )
        ],
    )
    assert result.blocks == ["TEST_BLOC"]
    assert len(list(backend.document.blocks.get("TEST_BLOC"))) == avant


def test_un_bloc_dont_le_contenu_echoue_ne_laisse_aucune_definition(
    backend: EzdxfBackend,
) -> None:
    """Rien de partiel: un bloc à moitié écrit s'insérerait sans erreur."""
    mauvais = DefineBlock(
        name="PARTIEL",
        operations=(
            AddLine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            AddCircle((0.0, 0.0, 0.0), -3.0),
        ),
    )
    result = backend.execute(OperationBatch((mauvais,), label="partiel"))
    assert not result.ok
    assert result.failures[0]["operation"] == "define_block"
    assert "PARTIEL" not in backend.document.blocks
    assert result.blocks == []


def test_une_occurrence_porte_un_handle_reel(backend: EzdxfBackend) -> None:
    """Jamais de handle inventé: c'est le bug historique du projet."""
    result = _run(backend, blocks.insert("light", (1.0, 2.0), METERS))
    assert len(result.created) == 1
    handle = result.created[0].handle
    entity = backend.document.entitydb.get(handle)
    assert entity is not None
    assert entity.dxftype() == "INSERT"
    assert entity.dxf.handle == handle


def test_un_attribut_inconnu_du_bloc_fait_echouer_l_insertion(
    backend: EzdxfBackend,
) -> None:
    """``add_auto_attribs`` perdrait la valeur en silence."""
    _run(backend, blocks.define("table", METERS))
    result = backend.execute(
        OperationBatch(
            (
                AddBlockRef(
                    name=blocks.block_name("table"),
                    insert=(0.0, 0.0, 0.0),
                    attributes=(("INCONNU", "x"),),
                    style=Style(layer="FURNITURE"),
                ),
            ),
            label="attribut-faux",
        )
    )
    assert not result.ok
    assert result.failures[0]["code"] == InvalidParameter.code


def test_le_plan_meuble_se_relit_avec_ses_attributs(
    backend: EzdxfBackend, tmp_path: Path
) -> None:
    """Preuve de bout en bout: écrit, relu, audité, attributs valorisés."""
    ops: list[Operation] = []
    for index, key in enumerate(("door", "toilet", "outlet", "bed_double")):
        ops += blocks.insert(key, (index * 3.0, 0.0), METERS, mark=f"{key[:2].upper()}{index}")
    _run(backend, ops, label="meuble")

    doc = _reread(backend, tmp_path, "meuble.dxf")
    inserts = list(doc.modelspace().query("INSERT"))
    assert len(inserts) == 4
    releve = {
        i.dxf.name: (i.dxf.layer, [(a.dxf.tag, a.dxf.text) for a in i.attribs])
        for i in inserts
    }
    assert releve[blocks.block_name("door")] == ("DOORS", [(blocks.MARK_TAG, "DO0")])
    assert releve[blocks.block_name("toilet")] == ("PLUMBING", [(blocks.MARK_TAG, "TO1")])
    assert releve[blocks.block_name("outlet")] == ("ELECTRICAL", [(blocks.MARK_TAG, "OU2")])
    assert releve[blocks.block_name("bed_double")] == ("FURNITURE", [(blocks.MARK_TAG, "BE3")])


def test_la_bibliotheque_entiere_produit_un_dxf_sain(
    backend: EzdxfBackend, tmp_path: Path
) -> None:
    ops: list[Operation] = []
    for index, key in enumerate(blocks.LIBRARY):
        ops += blocks.insert(key, (index * 4.0, 0.0), METERS)
    result = _run(backend, ops, label="planche")
    assert len(result.created) == len(blocks.LIBRARY)
    assert len(result.blocks) == len(blocks.LIBRARY)

    doc = _reread(backend, tmp_path, "planche.dxf")
    definis = {b.name for b in doc.blocks if b.name.startswith(blocks.NAME_PREFIX)}
    assert definis == {blocks.block_name(k) for k in blocks.LIBRARY}
    for name in definis:
        block = doc.blocks.get(name)
        assert [a.dxf.tag for a in block.attdefs()] == [blocks.MARK_TAG]


# ---------------------------------------------------------------------------
# Cotation: le piège du facteur cent
# ---------------------------------------------------------------------------


def _dimension_text(doc: Drawing) -> list[str]:
    """Textes réellement dessinés par les cotes, lus dans leur bloc de rendu.

    C'est ce que l'usager voit. ``DIMENSION.dxf.text`` vaut ``<>``, marque de
    substitution: l'affirmer ne prouverait rien.
    """
    out = []
    for dim in doc.modelspace().query("DIMENSION"):
        block = doc.blocks.get(dim.dxf.geometry)
        out += [e.text for e in block if e.dxftype() == "MTEXT"]
    return out


def test_une_cote_de_six_metres_affiche_six(
    backend: EzdxfBackend, tmp_path: Path
) -> None:
    """Le piège documenté: avec le style EZDXF, six mètres s'affichent « 600 ».

    Le style du projet porte ``dimlfac = 1``, donc le nombre affiché est la
    mesure réelle dans l'unité du document.
    """
    _run(
        backend,
        [
            EnsureLayer("DIMENSIONS", 9, "Cotation"),
            AddDimAligned(
                p1=(0.0, 0.0), p2=(6.0, 0.0), location=(3.0, -1.0),
                style=Style(layer="DIMENSIONS"),
            ),
        ],
        label="cote",
    )
    doc = _reread(backend, tmp_path, "cote.dxf")
    dim = doc.modelspace().query("DIMENSION")[0]
    assert dim.dxf.dimstyle == PROJECT_DIMSTYLE
    assert dim.get_measurement() == pytest.approx(6.0)
    assert _dimension_text(doc) == ["6.00"]


def test_le_style_de_cote_du_projet_ne_deforme_pas_la_mesure(
    backend: EzdxfBackend,
) -> None:
    _run(backend, [AddDimAligned(p1=(0.0, 0.0), p2=(1.0, 0.0), location=(0.5, -1.0))])
    style = backend.document.dimstyles.get(PROJECT_DIMSTYLE)
    assert style.dxf.dimlfac == 1.0
    assert style.dxf.dimscale == 1.0
    # Zéros conservés: c'est ce qui fait lire « 6.00 » et non « 6 ».
    assert style.dxf.dimzin == 0
    assert style.dxf.dimdec == Defaults.DIM_DECIMALS


def test_le_style_livre_par_ezdxf_n_est_plus_utilise(backend: EzdxfBackend) -> None:
    """Il reste dans le document, mais aucune cote du projet ne s'y réfère."""
    _run(backend, [AddDimAligned(p1=(0.0, 0.0), p2=(6.0, 0.0), location=(3.0, -1.0))])
    doc = backend.document
    assert doc.dimstyles.get("EZDXF").dxf.dimlfac == 100.0
    for dim in doc.modelspace().query("DIMENSION"):
        assert dim.dxf.dimstyle == PROJECT_DIMSTYLE


@pytest.mark.parametrize(
    ("unit", "longueur", "attendu"),
    [
        (Unit.METER, 6.0, "6.00"),
        (Unit.MILLIMETER, 6000.0, "6000.00"),
        (Unit.CENTIMETER, 600.0, "600.00"),
    ],
)
def test_la_cote_affiche_la_mesure_dans_l_unite_du_document(
    tmp_path: Path, unit: Unit, longueur: float, attendu: str
) -> None:
    """Six mètres, dans les trois unités. Le nombre affiché est la mesure."""
    be = EzdxfBackend(unit=unit)
    be.connect()
    defaults = Defaults(unit)
    _run(
        be,
        [
            AddDimAligned(
                p1=(0.0, 0.0),
                p2=(longueur, 0.0),
                location=(longueur / 2.0, -defaults.text_height * 4.0),
            )
        ],
        label=f"cote-{unit.value}",
    )
    doc = _reread(be, tmp_path, f"cote_{unit.value}.dxf")
    assert _dimension_text(doc) == [attendu]


@pytest.mark.parametrize("unit", [Unit.MILLIMETER, Unit.METER, Unit.FOOT])
def test_l_habillage_de_la_cote_suit_l_echelle_du_dessin(unit: Unit) -> None:
    """Aucune longueur en dur: tout vient de units.Defaults.

    Sans cela, le texte d'une cote mesure un mètre de haut dans un plan en
    mètres, ou devient invisible dans un plan en millimètres.
    """
    be = EzdxfBackend(unit=unit)
    be.connect()
    _run(be, [AddDimAligned(p1=(0.0, 0.0), p2=(1.0, 0.0), location=(0.5, -1.0))])
    defaults = Defaults(unit)
    style = be.document.dimstyles.get(PROJECT_DIMSTYLE)
    assert style.dxf.dimtxt == pytest.approx(defaults.text_height)
    assert style.dxf.dimasz == pytest.approx(defaults.dim_arrow_size)
    assert style.dxf.dimexe == pytest.approx(defaults.dim_extension)
    assert style.dxf.dimexo == pytest.approx(defaults.dim_offset)
    assert style.dxf.dimgap == pytest.approx(defaults.dim_text_gap)


def test_le_style_de_cote_n_est_cree_qu_une_fois(backend: EzdxfBackend) -> None:
    for index in range(3):
        _run(
            backend,
            [AddDimAligned(p1=(0.0, 0.0), p2=(float(index + 1), 0.0), location=(0.5, -1.0))],
            label=f"cote-{index}",
        )
    noms = [s.dxf.name for s in backend.document.dimstyles]
    assert noms.count(PROJECT_DIMSTYLE) == 1


def test_la_cote_dessine_bien_sa_geometrie(backend: EzdxfBackend, tmp_path: Path) -> None:
    """Sans rendu, une cote reste invisible partout sauf dans AutoCAD."""
    _run(
        backend,
        [
            AddText((3.0, 1.0, 0.0), "MUR", 0.25, style=Style(layer="ANNOTATION")),
            AddDimAligned(p1=(0.0, 0.0), p2=(6.0, 0.0), location=(3.0, -1.0)),
        ],
        label="rendu-cote",
    )
    doc = _reread(backend, tmp_path, "rendu.dxf")
    dim = doc.modelspace().query("DIMENSION")[0]
    block = doc.blocks.get(dim.dxf.geometry)
    kinds = {e.dxftype() for e in block}
    # Lignes d'attache et de cote, tirets d'extrémité, et le texte.
    assert "LINE" in kinds
    assert "MTEXT" in kinds
    assert "INSERT" in kinds, "les tirets d'extrémité sont des occurrences de bloc"


# ---------------------------------------------------------------------------
# Repères maintenus horizontaux
# ---------------------------------------------------------------------------
#
# Défaut constaté au rendu d'un logement complet: l'évier posé à cent
# quatre-vingts degrés affichait « E1 » à l'envers, le WC posé à quatre-vingt-dix
# degrés couchait « W1 » sur le flanc. Un attribut suit la rotation du bloc qui
# le porte, c'est le comportement du format. Mais un repère est une annotation:
# il est fait pour être lu. Un plan dont les repères se lisent la tête en bas
# n'est pas livrable.


def _marked_block(name: str, *, upright: bool) -> DefineBlock:
    """Bloc minimal dont le repère est posé hors de la géométrie."""
    return DefineBlock(
        name=name,
        operations=(AddLine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),),
        attributes=(
            AttributeDef(
                tag=blocks.MARK_TAG,
                height=0.15,
                default="R",
                position=(0.5, 0.8),
                halign="center",
                valign="middle",
                keep_upright=upright,
            ),
        ),
    )


def _place(backend: EzdxfBackend, name: str, degrees: float, scale_x: float = 1.0) -> Any:
    """Insère une occurrence et rend son unique attribut."""
    result = _run(
        backend,
        [
            AddBlockRef(
                name=name,
                insert=(0.0, 0.0, 0.0),
                scale=(scale_x, 1.0, 1.0),
                rotation=math.radians(degrees),
                attributes=((blocks.MARK_TAG, "R1"),),
            )
        ],
        label=f"pose-{degrees}",
    )
    entity = backend.document.entitydb.get(result.created[0].handle)
    return entity.attribs[0]


def test_un_repere_est_maintenu_horizontal_par_defaut() -> None:
    """Le défaut vrai est le bon: un repère est fait pour être lu."""
    assert AttributeDef(tag="R", height=0.15).keep_upright is True


@pytest.mark.parametrize("degrees", [0.0, 90.0, 180.0, 270.0, 45.0, -120.0])
def test_le_repere_reste_horizontal_quelle_que_soit_la_rotation(
    backend: EzdxfBackend, degrees: float
) -> None:
    _run(backend, [_marked_block("DROIT", upright=True)])
    attrib = _place(backend, "DROIT", degrees)
    assert attrib.dxf.rotation == pytest.approx(0.0)
    assert tuple(attrib.dxf.extrusion) == (0.0, 0.0, 1.0)


@pytest.mark.parametrize("degrees", [90.0, 180.0, 270.0])
def test_redresser_le_repere_ne_le_deplace_pas(
    backend: EzdxfBackend, degrees: float
) -> None:
    """Seule l'orientation change. Le repère doit rester où la pose l'a mis.

    La preuve est comparative: le même bloc, redressé et non redressé, doit
    ancrer son texte exactement au même point du dessin.
    """
    _run(backend, [_marked_block("DROIT", upright=True)])
    _run(backend, [_marked_block("BRUT", upright=False)])
    droit = _place(backend, "DROIT", degrees)
    brut = _place(backend, "BRUT", degrees)

    def anchor(attrib: Any) -> tuple[float, float]:
        point = attrib.ocs().to_wcs(attrib.dxf.align_point)
        return (round(float(point.x), 9), round(float(point.y), 9))

    assert anchor(droit) == anchor(brut)
    assert brut.dxf.rotation == pytest.approx(degrees if degrees <= 180.0 else degrees - 360.0)


def test_une_occurrence_retournee_redresse_aussi_son_repere(
    backend: EzdxfBackend,
) -> None:
    """Une échelle négative retourne le texte par la direction d'extrusion.

    Remettre la rotation à zéro ne suffirait pas: le repère resterait en miroir,
    et son point d'ancrage, exprimé dans un repère objet inversé, sauterait de
    l'autre côté de l'axe si l'on rétablissait l'extrusion sans le convertir.
    """
    _run(backend, [_marked_block("DROIT", upright=True)])
    _run(backend, [_marked_block("BRUT", upright=False)])
    droit = _place(backend, "DROIT", 0.0, scale_x=-1.0)
    brut = _place(backend, "BRUT", 0.0, scale_x=-1.0)

    assert tuple(brut.dxf.extrusion) == (0.0, 0.0, -1.0), "le miroir brut inverse l'extrusion"
    assert tuple(droit.dxf.extrusion) == (0.0, 0.0, 1.0)
    assert droit.dxf.rotation == pytest.approx(0.0)
    assert droit.dxf.text_generation_flag == 0
    # Position conservée, exprimée dans le repère du dessin des deux côtés.
    attendu = brut.ocs().to_wcs(brut.dxf.align_point)
    obtenu = droit.ocs().to_wcs(droit.dxf.align_point)
    assert (obtenu.x, obtenu.y) == pytest.approx((attendu.x, attendu.y))


def test_le_comportement_brut_du_format_reste_accessible(backend: EzdxfBackend) -> None:
    """``keep_upright=False`` rend la rotation héritée, sans correction."""
    _run(backend, [_marked_block("BRUT", upright=False)])
    attrib = _place(backend, "BRUT", 90.0)
    assert attrib.dxf.rotation == pytest.approx(90.0)


@pytest.mark.parametrize("key", blocks.LIBRARY)
def test_tous_les_reperes_de_la_bibliotheque_sont_redresses(
    backend: EzdxfBackend, key: str
) -> None:
    _run(backend, blocks.insert(key, (0.0, 0.0), METERS, rotation_deg=180.0))
    insert_entity = backend.document.modelspace().query("INSERT")[0]
    attrib = insert_entity.attribs[0]
    assert attrib.dxf.rotation == pytest.approx(0.0)


def test_la_consigne_survit_a_l_enregistrement(
    backend: EzdxfBackend, tmp_path: Path
) -> None:
    """L'intention est portée par la définition, pas par l'occurrence.

    Elle est donc écrite dans le fichier: une occurrence posée après relecture
    doit encore être redressée, sinon un plan repris le lendemain régresserait.
    """
    _run(backend, blocks.define("sink", METERS))
    doc = _reread(backend, tmp_path, "consigne.dxf")

    relu = EzdxfBackend(str(tmp_path / "consigne.dxf"), unit=Unit.METER)
    relu.connect()
    assert blocks.block_name("sink") in relu.document.blocks
    attrib = _place(relu, blocks.block_name("sink"), 180.0)
    assert attrib.dxf.rotation == pytest.approx(0.0)
    # Le marqueur est bien dans le fichier, pas seulement en mémoire.
    attdef = next(iter(doc.blocks.get(blocks.block_name("sink")).attdefs()))
    assert attdef.has_xdata("AUTOCAD_MCP")


# ---------------------------------------------------------------------------
# Mesures publiées dans EntityInfo.extra
# ---------------------------------------------------------------------------
#
# ``ops.query`` lit exactement deux clés, ``length`` et ``area``, déclarées par
# ``base.MEASURE_KEYS``. Le moteur doit les publier là où il sait les calculer,
# et se taire partout ailleurs: une mesure absente est rapportée comme manquante,
# ce qui est honnête ; une mesure fausse se lit sans se relire.


def _measured(backend: EzdxfBackend, operation: Operation) -> dict[str, Any]:
    """Mesures publiées pour une opération isolée."""
    result = _run(backend, [operation], label="mesure")
    info = backend.query(EntityFilter(handles=(result.created[0].handle,)))[0]
    return {k: v for k, v in info.extra.items() if k in MEASURE_KEYS}


def test_une_ligne_publie_sa_longueur(backend: EzdxfBackend) -> None:
    """Triangle 3-4-5: la longueur vaut cinq, pas la somme des projections."""
    assert _measured(backend, AddLine((0.0, 0.0, 0.0), (3.0, 4.0, 0.0))) == {"length": 5.0}


def test_un_cercle_publie_circonference_et_aire(backend: EzdxfBackend) -> None:
    mesures = _measured(backend, AddCircle((0.0, 0.0, 0.0), 3.0))
    assert mesures["length"] == pytest.approx(2.0 * math.pi * 3.0)
    assert mesures["area"] == pytest.approx(math.pi * 9.0)


def test_un_arc_publie_sa_longueur_developpee_et_aucune_aire(
    backend: EzdxfBackend,
) -> None:
    """La longueur d'un arc, pas celle de sa corde. Et un arc n'a pas d'aire."""
    quart = AddArc((0.0, 0.0, 0.0), 2.0, 0.0, math.pi / 2.0)
    mesures = _measured(backend, quart)
    assert mesures["length"] == pytest.approx(math.pi)  # 2 * (2π/4)
    assert mesures["length"] != pytest.approx(2.0 * math.sqrt(2.0))  # la corde
    assert "area" not in mesures


def test_un_contour_ferme_publie_perimetre_et_aire(backend: EzdxfBackend) -> None:
    rectangle = AddPolyline(((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)), closed=True)
    assert _measured(backend, rectangle) == {"length": 14.0, "area": 12.0}


def test_une_polyligne_ouverte_n_a_pas_d_aire(backend: EzdxfBackend) -> None:
    """Une surface suppose un contour fermé. Sinon rien n'est publié."""
    mesures = _measured(backend, AddPolyline(((0.0, 0.0), (6.0, 0.0), (6.0, 8.0))))
    assert mesures == {"length": 14.0}


def test_les_mesures_sont_des_flottants_du_langage(backend: EzdxfBackend) -> None:
    """``ezdxf.math.area`` rend un flottant numpy, que ``json`` refuse.

    La mesure remonte jusqu'à la réponse MCP: un type exotique y échouerait.
    """
    rectangle = AddPolyline(((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)), closed=True)
    mesures = _measured(backend, rectangle)
    assert all(type(v) is float for v in mesures.values())
    json.dumps(mesures)


@pytest.mark.parametrize(
    "operation",
    [
        AddText((0.0, 0.0, 0.0), "SANS MESURE", 0.25),
        AddHatch((((0.0, 0.0), (4.0, 0.0), (4.0, 3.0)),)),
    ],
    ids=["text", "hatch"],
)
def test_ce_qui_n_est_pas_mesurable_ne_publie_rien(
    backend: EzdxfBackend, operation: Operation
) -> None:
    """La hachure n'a pas d'aire fiable: ``ezdxf`` n'en expose pas.

    Sa seule approche, ``triangulate()``, exige une tolérance d'approximation et
    dérive sur les bords courbes. On ne publie donc rien plutôt qu'un à-peu-près
    présenté comme une mesure.
    """
    assert _measured(backend, operation) == {}


def test_les_mesures_sont_dans_l_unite_du_document() -> None:
    """Aucune conversion: la mesure est dans l'unité du dessin, telle quelle."""
    be = EzdxfBackend(unit=Unit.MILLIMETER)
    be.connect()
    mesures = _measured(be, AddLine((0.0, 0.0, 0.0), (3000.0, 4000.0, 0.0)))
    assert mesures == {"length": 5000.0}


def test_les_totaux_de_ops_query_sont_desormais_justes(backend: EzdxfBackend) -> None:
    """Le crochet posé côté logique est rempli côté moteur.

    Sans les clés publiées ici, ``total_length`` rapportait tout comme manquant.
    """
    from autocad_mcp.ops import query

    _run(
        backend,
        [
            AddLine((0.0, 0.0, 0.0), (3.0, 4.0, 0.0)),
            AddPolyline(((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)), closed=True),
        ],
        label="totaux",
    )
    entities = backend.query()
    longueur = query.total_length(entities)
    aire = query.total_area(entities)
    assert longueur.total == pytest.approx(19.0)  # 5 + 14
    assert longueur.missing == 0
    assert aire.total == pytest.approx(12.0)
    assert aire.missing == 1, "la ligne n'a pas d'aire, et le dit"
