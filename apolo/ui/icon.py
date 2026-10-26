"""O desenho do icone do Apolo, em um lugar so.

Mesma ideia do Artemis: desenhado em codigo, um PNG por estado. O simbolo e
outro de proposito - um sol (Apolo) no lugar do microfone - porque os dois
apps ficam lado a lado na bandeja e precisam ser distinguiveis de relance.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

# A cor de cada estado do app. "idle" e o cinza neutro da barra.
COLORS = {
    "idle": (120, 120, 128),
    "preparing": (245, 165, 36),
    "recording": (229, 72, 77),
    "processing": (245, 165, 36),
    "done": (48, 164, 108),
    "error": (229, 72, 77),
}


def make_icon(color: tuple[int, int, int], size: int = 128) -> Image.Image:
    """Um sol claro sobre o disco da cor do estado.

    Desenhado em 4x e reduzido no fim, como no Artemis: na bandeja isto
    aparece com uns 16-22 px, e sem o supersampling os raios serrilham.
    """
    scale = 4
    box = size * scale
    image = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = box * 0.04
    draw.ellipse([pad, pad, box - pad, box - pad], fill=(*color, 255))

    ink = (255, 255, 255, 255)
    cx = cy = box / 2
    core = box * 0.15
    draw.ellipse([cx - core, cy - core, cx + core, cy + core], fill=ink)

    # Oito raios curtos, com ponta arredondada.
    stroke = box * 0.065
    inner, outer = box * 0.24, box * 0.34
    for i in range(8):
        angle = math.pi / 4 * i
        x0, y0 = cx + inner * math.cos(angle), cy + inner * math.sin(angle)
        x1, y1 = cx + outer * math.cos(angle), cy + outer * math.sin(angle)
        draw.line([x0, y0, x1, y1], fill=ink, width=int(stroke))
        for x, y in ((x0, y0), (x1, y1)):
            r = stroke / 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill=ink)

    return image.resize((size, size), Image.LANCZOS)
