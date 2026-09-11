"""Rendu d'un document DXF en image PNG.

Ce module existe pour une seule raison: permettre au modèle de *voir* le plan
qu'il vient de dessiner. Sans image, la boucle de correction est aveugle et le
modèle ne peut que croire le message de succès qu'on lui renvoie, ce qui est
exactement le défaut historique du projet.

Trois exigences en découlent.

* **Déterminisme.** Deux rendus du même document produisent la même image. La
  taille, la résolution, les marges et les couleurs sont fixées ici, jamais
  déduites du contenu. C'est ce qui rend possibles les tests de référence.
* **Fidélité ACI.** Les couleurs sont celles de l'index ACI du dessin, sans
  remappage cosmétique. Une entité rouge dans AutoCAD est rouge sur l'image.
* **Lisibilité.** Fond clair, épaisseur de trait minimale d'environ un pixel et
  demi, cadrage centré avec une marge constante.

Le rendu n'utilise jamais ``pyplot``: la figure et son canvas ``Agg`` sont
construits explicitement, donc aucun backend interactif n'est sollicité et aucun
état global de matplotlib n'est modifié durablement par le serveur.
"""

from __future__ import annotations

import io
import random
from typing import TYPE_CHECKING, Any

from ezdxf import bbox as ezdxf_bbox

from .errors import BackendUnavailable, InvalidParameter

if TYPE_CHECKING:  # pragma: no cover - uniquement pour les annotations
    from ezdxf.document import Drawing
    from ezdxf.layouts import Layout

__all__ = ["DEFAULT_HEIGHT", "DEFAULT_WIDTH", "render_png"]

#: Taille par défaut de l'image, en pixels. Assez grande pour qu'un plan de
#: logement reste lisible, assez petite pour rester peu coûteuse à transmettre.
DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 1200

#: Résolution fixe. La taille en pouces de la figure en découle, si bien que
#: l'image fait exactement ``width`` x ``height`` pixels.
DPI = 100

#: Fond clair volontairement non blanc pur: les traits ACI 7, qui s'affichent
#: noirs sur fond clair, restent contrastés et les hachures blanches restent
#: visibles.
BACKGROUND = "#F2F2EF"
#: Couleur de remplacement de l'ACI 7, qui n'a pas de valeur fixe en DXF.
FOREGROUND = "#101010"

#: Marge autour du dessin, en fraction de la plus grande dimension du contenu.
MARGIN = 0.04

#: Épaisseur de trait minimale, en pixels. En dessous, un plan entier se réduit
#: à un gris uniforme une fois dézoomé.
MIN_LINEWEIGHT_PX = 1.5

#: Fenêtre de repli quand le document est vide: un rendu vide reste un rendu
#: valide, il ne doit pas lever.
EMPTY_VIEW = (-1.0, -1.0, 1.0, 1.0)

#: Graine du générateur aléatoire global pendant le rendu.
#:
#: ``ezdxf.render.hatching.pattern_baselines`` décale l'origine des lignes de
#: motif d'une quantité tirée par ``random.random()``, pour éviter qu'une ligne
#: ne tombe exactement sur un sommet du contour. Le déplacement est minuscule,
#: de l'ordre du millième de l'écartement du motif, mais il suffit à faire
#: varier deux ou trois pixels d'un rendu à l'autre, donc à ruiner toute
#: comparaison d'image. On fige donc la graine le temps du rendu, puis on
#: restaure l'état du générateur pour ne rien changer au reste du programme.
RENDER_SEED = 20240101


def _load_matplotlib() -> tuple[Any, Any]:
    """Charge matplotlib en mode strictement hors écran.

    ``matplotlib`` est une dépendance optionnelle du projet, déclarée dans
    l'extra ``render``. Son absence est une indisponibilité de backend, pas une
    erreur de programmation, d'où ``BackendUnavailable``.
    """
    try:
        import matplotlib

        # Verrouille le backend avant tout import de pyplot, y compris celui
        # que fait le module de dessin d'ezdxf: sur macOS, pyplot choisirait
        # sinon un backend graphique.
        matplotlib.use("Agg")
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise BackendUnavailable(
            "Le rendu PNG exige matplotlib",
            remedy="installer l'extra de rendu: pip install 'autocad-mcp[render]'",
        ) from exc
    return Figure, FigureCanvasAgg


def _drawing_config() -> Any:
    """Paramètres de dessin figés, partagés par tous les rendus."""
    from ezdxf.addons.drawing.config import (
        BackgroundPolicy,
        ColorPolicy,
        Configuration,
        HatchPolicy,
        LinePolicy,
        LineweightPolicy,
        TextPolicy,
    )

    return Configuration(
        # Les couleurs viennent de l'ACI du dessin, sans monochrome ni inversion.
        color_policy=ColorPolicy.COLOR,
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color=BACKGROUND,
        custom_fg_color=FOREGROUND,
        # Types de ligne développés exactement, sinon un trait d'axe et un trait
        # plein se ressemblent à l'image.
        line_policy=LinePolicy.ACCURATE,
        hatch_policy=HatchPolicy.NORMAL,
        text_policy=TextPolicy.FILLING,
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        lineweight_scaling=1.0,
        min_lineweight=72.0 * MIN_LINEWEIGHT_PX / DPI,
        # Fixé explicitement: la finesse des arcs ne doit pas dépendre d'un
        # défaut de version.
        circle_approximation_count=128,
    )


def _content_extents(layout: Layout) -> tuple[float, float, float, float]:
    """Boîte englobante du contenu, ou fenêtre de repli si le dessin est vide."""
    box = ezdxf_bbox.extents(layout, fast=False)
    if not box.has_data:
        return EMPTY_VIEW
    return (
        float(box.extmin.x),
        float(box.extmin.y),
        float(box.extmax.x),
        float(box.extmax.y),
    )


def _view_window(
    extents: tuple[float, float, float, float],
    width: int,
    height: int,
    margin: float,
) -> tuple[float, float, float, float]:
    """Fenêtre de visualisation centrée, au rapport d'aspect exact de l'image.

    Le calcul est fait ici plutôt que laissé à l'autoscale de matplotlib: c'est
    la condition pour que deux rendus successifs du même dessin donnent le même
    cadrage au pixel près.
    """
    xmin, ymin, xmax, ymax = extents
    span_x = xmax - xmin
    span_y = ymax - ymin
    # Un dessin plat, par exemple une unique ligne horizontale, a une hauteur
    # nulle: on lui prête l'autre dimension pour éviter une division par zéro.
    if span_x <= 0.0 and span_y <= 0.0:
        span_x = span_y = 1.0
    elif span_x <= 0.0:
        span_x = span_y
    elif span_y <= 0.0:
        span_y = span_x

    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    needed_x = span_x * (1.0 + 2.0 * margin)
    needed_y = span_y * (1.0 + 2.0 * margin)

    target_ratio = width / height
    if needed_x / needed_y > target_ratio:
        view_x = needed_x
        view_y = needed_x / target_ratio
    else:
        view_y = needed_y
        view_x = needed_y * target_ratio
    return (
        center_x - view_x / 2.0,
        center_y - view_y / 2.0,
        center_x + view_x / 2.0,
        center_y + view_y / 2.0,
    )


def render_png(
    doc: Drawing,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    *,
    background: str = BACKGROUND,
    foreground: str = FOREGROUND,
    margin: float = MARGIN,
    dpi: int = DPI,
) -> bytes:
    """Rend l'espace objet de ``doc`` en PNG et rend les octets de l'image.

    Args:
        doc: document ``ezdxf`` déjà construit.
        width: largeur de l'image en pixels.
        height: hauteur de l'image en pixels.
        background: couleur de fond au format ``#RRGGBB``. Une couleur claire
            fait afficher l'ACI 7 en noir, comme dans AutoCAD.
        foreground: couleur de remplacement de l'ACI 7.
        margin: marge autour du contenu, en fraction de sa plus grande dimension.
        dpi: résolution. La taille en pixels reste ``width`` x ``height``.

    Raises:
        InvalidParameter: dimensions non strictement positives ou marge négative.
        BackendUnavailable: matplotlib n'est pas installé.
    """
    if width <= 0 or height <= 0:
        raise InvalidParameter(
            f"Dimensions d'image invalides: {width}x{height}", valid="entiers strictement positifs"
        )
    if margin < 0.0:
        raise InvalidParameter(f"Marge négative: {margin}", valid="fraction >= 0")
    if dpi <= 0:
        raise InvalidParameter(f"Résolution invalide: {dpi}", valid="entier strictement positif")

    figure_cls, canvas_cls = _load_matplotlib()
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.properties import LayoutProperties

    msp = doc.modelspace()

    figure = figure_cls(figsize=(width / dpi, height / dpi), dpi=dpi)
    canvas_cls(figure)
    # L'axe occupe toute la figure: la marge est gérée en coordonnées dessin,
    # donc elle reste proportionnelle au contenu et non à la taille de l'image.
    axes = figure.add_axes((0.0, 0.0, 1.0, 1.0))

    layout_properties = LayoutProperties.from_layout(msp)
    layout_properties.set_colors(background, foreground)

    out = MatplotlibBackend(axes, adjust_figure=False)
    random_state = random.getstate()
    random.seed(RENDER_SEED)
    try:
        try:
            Frontend(RenderContext(doc), out, _drawing_config()).draw_layout(
                msp, finalize=True, layout_properties=layout_properties
            )
        finally:
            random.setstate(random_state)

        # ``finalize`` réactive l'autoscale de matplotlib: on le désarme et on
        # impose le cadrage calculé, sinon la taille apparente du dessin
        # dépendrait de l'ordre des entités.
        axes.set_autoscale_on(False)
        xmin, ymin, xmax, ymax = _view_window(_content_extents(msp), width, height, margin)
        axes.set_xlim(xmin, xmax)
        axes.set_ylim(ymin, ymax)
        axes.set_facecolor(background)
        figure.set_facecolor(background)

        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            facecolor=background,
            edgecolor="none",
            # Surtout pas bbox_inches="tight": le recadrage automatique
            # changerait la taille de l'image d'un dessin à l'autre.
            metadata={"Software": "autocad-mcp"},
        )
    finally:
        figure.clf()
    return buffer.getvalue()
