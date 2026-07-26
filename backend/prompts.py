"""Prompt templates for the two Nano Banana calls.

Built from the user's base prompts, tightened for layout fidelity:
the whole product depends on the stylized image staying pixel-aligned with
the DXF render (room hotspots come from extracted geometry, not the image).
"""

from styles import style_block

# ---------------------------------------------------------------------------
# 1) Stylized top-down view  (input: the raw DXF plan render)
# ---------------------------------------------------------------------------
STYLIZE_TOP_DOWN = """\
Convert this 2D CAD floor plan drawing into a photorealistic top-down \
architectural render of the same floor plan (a realistic "dollhouse" \
top view with furnished rooms, real materials, and soft ambient lighting).

Camera: keep the exact same top-down orthographic view as the input. Do not \
rotate, tilt, zoom, crop, or shift the plan — every wall and room must remain \
at the same position and scale in the image frame as in the input drawing.

Structural fidelity: preserve every wall, door opening, window, and room \
boundary exactly where the drawing shows it. Keep all furniture shown in the \
plan in its exact position and footprint. Do not add rooms, walls, or \
furniture that are not in the drawing, and do not remove or move anything.

Rendering: replace the line-drawing look with realistic textures — flooring, \
wall tops, furniture, subtle shadows. Consistent daylight illumination, \
no harsh shadows that obscure the layout.

{style}

Output: a single clean image, no text, no dimensions, no labels, no \
watermarks, no border, same aspect ratio as the input.\
"""

# ---------------------------------------------------------------------------
# 2) 360 room panorama  (input: room crop from the stylized top-down)
# ---------------------------------------------------------------------------
ROOM_PANO = """\
Create a true 360-degree EQUIRECTANGULAR panorama of the interior of \
{room_name} (approximately {area_m2} square meters), as captured by a \
360 camera (like a Ricoh Theta) standing at the center of the room with \
the lens at 1.6 m height.

MANDATORY equirectangular geometry — the image is invalid without ALL of \
these:
1. 2:1 spherical projection spanning the full 360 degrees horizontally: \
ALL FOUR WALLS of the room are visible across the width of one image.
2. The horizon is a perfectly straight horizontal line across the exact \
vertical middle of the frame.
3. Vertical edges (wall corners, door frames, furniture legs) stay \
perfectly vertical; horizontal edges (floor lines, counter tops, ceiling \
lines) bow visibly with the spherical curvature — strongest near the top \
and bottom of the frame.
4. The floor sweeps across the entire bottom of the frame toward the \
nadir; the ceiling fills the top toward the zenith.
5. The LEFT and RIGHT image edges show the exact same view direction and \
continue each other pixel-for-pixel — a viewer must wrap with no visible \
seam.

This is NOT a regular photo, NOT a wide-angle or fisheye shot of one side \
of the room — every wall, in a single seamless 2:1 spherical image.

The input image is the top-down view of this exact room. Layout fidelity: \
preserve the positions, orientations and spacing of all furniture and \
objects it shows. Keep the room's proportions, wall lengths and \
door/window placements accurate to the plan. Do not add, remove or \
rearrange objects.

{facts}

Style: match the visual style, colors and materials of the input image. \


Consistent lighting all around; no people, no lens flares, no text, no \
watermarks, no borders. Output: one seamless 2:1 equirectangular 360 \
panorama, straight horizon at mid-height, wrap-continuous edges.\
"""
# {style}

def stylize_prompt(style_id):
    return STYLIZE_TOP_DOWN.format(style=style_block(style_id))


def pano_prompt(style_id, room_name, area_m2, brief=""):
    facts = ""
    if brief:
        facts = ("Facts from the CAD drawing (authoritative; in the "
                 f"top-down input, up is north): {brief}")
    return ROOM_PANO.format(style=style_block(style_id),
                            room_name=room_name or "a room",
                            area_m2=round(area_m2 or 12),
                            facts=facts)
