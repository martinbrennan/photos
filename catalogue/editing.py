"""Versioned recipes and one libvips pipeline for all edited output sizes."""
import json
import math

DEFAULT = {'version': 1, 'gamma': 1.0}


def recipe(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Unsupported edit parameters')
    gamma = value.get('gamma', 1.0)
    if value.get('version', 1) != 1 or isinstance(gamma, bool) or not isinstance(gamma, (float, int)):
        raise ValueError('Invalid recipe')
    if not math.isfinite(gamma) or not .3 <= gamma <= 2.5:
        raise ValueError('Gamma must be between 0.3 and 2.5')
    return {'version': 1, 'gamma': float(gamma)}


def canonical(value):
    return json.dumps(recipe(value), sort_keys=True, separators=(',', ':'))


def process(path, edits, edge):
    """Return sRGB JPEG; edge=0 means original dimensions. Never writes source.

    thumbnail uses loader shrink-on-load (including JPEG) and autorotation.
    libvips gamma uses the reciprocal exponent, so pass 1 / slider gamma:
    output = 255 * (input / 255) ** gamma. Alpha is flattened before gamma.
    """
    import pyvips
    edits = recipe(edits)
    if edge:
        image = pyvips.Image.thumbnail(str(path), edge, height=edge, size='down',
                                       export_profile='srgb')
    else:
        image = pyvips.Image.new_from_file(str(path), access='sequential').autorot()
        if image.get_typeof('icc-profile-data'):
            image = image.icc_transform('srgb', embedded=True)
        else:
            image = image.colourspace('srgb')
    if image.hasalpha():
        image = image.flatten(background=[16, 20, 18])
    if edits['gamma'] != 1:
        image = image.gamma(exponent=1 / edits['gamma'])
    return image.jpegsave_buffer(Q=88, strip=True)
