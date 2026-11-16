"""Versioned recipes and one libvips pipeline for all edited output sizes."""
import json
import math

DEFAULT = {'version': 1, 'gamma': 1.0, 'pop': 0.0, 'drama': {'strength': 0.0, 'saturation': 0.0}}


def recipe(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Unsupported edit parameters')
    gamma = value.get('gamma', 1.0)
    if value.get('version', 1) != 1 or isinstance(gamma, bool) or not isinstance(gamma, (float, int)):
        raise ValueError('Invalid recipe')
    if not math.isfinite(gamma) or not .3 <= gamma <= 2.5:
        raise ValueError('Gamma must be between 0.3 and 2.5')
    pop = value.get('pop', 0)
    if isinstance(pop, bool) or not isinstance(pop, (int, float)) or not math.isfinite(pop) or not 0 <= pop <= 100:
        raise ValueError('Pop must be between 0 and 100')
    drama = value.get('drama', {})
    if not isinstance(drama, dict) or set(drama) - {'strength', 'saturation'}:
        raise ValueError('Invalid Drama parameters')
    drama = dict(strength=drama.get('strength', 0), saturation=drama.get('saturation', 0))
    for name, minimum in [('strength', 0), ('saturation', -100)]:
        number = drama[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not minimum <= number <= 100:
            raise ValueError(f'Drama {name} must be between {minimum} and 100')
        drama[name] = float(number)
    return {'version': 1, 'gamma': float(gamma), 'pop': float(pop), 'drama': drama}


def canonical(value):
    return json.dumps(recipe(value), sort_keys=True, separators=(',', ':'))


def scaled_blur(lightness, radius):
    """Gaussian approximation with bounded convolution cost at large radii."""
    radius = max(.5, radius)
    shrink = max(1, radius / 4)
    reduced = lightness.resize(1 / shrink) if shrink > 1 else lightness
    blurred = reduced.gaussblur(radius / shrink)
    if shrink > 1:
        blurred = blurred.resize(lightness.width / blurred.width,
                                 vscale=lightness.height / blurred.height)
    return blurred


def bounded_detail(lightness, blurred, limit, noise):
    """Soft saturation bounds halos, while a soft gate suppresses tiny noise."""
    delta = (lightness - blurred).copy_memory()
    magnitude = delta.abs()
    return delta * limit / (limit + magnitude) * magnitude / (magnitude + noise)


def local_contrast(image, strength):
    """Enhance Lab lightness; bounded detail limits halos and soft-gates noise.

    Radii track the longest oriented dimension (1600px reference). Reusable
    independently of recipes for future local-contrast effects.
    """
    if not strength:
        return image
    lab = image.colourspace('lab').copy_memory()
    lightness = lab[0].copy_memory()
    scale = max(image.width, image.height) / 1600
    def detail(sigma, limit, noise):
        return bounded_detail(lightness, scaled_blur(lightness, sigma * scale), limit, noise)
    # Threefold slider gain: 33⅓ now matches the original maximum.
    enhanced = lightness + (3 * strength / 100) * (
        .55 * detail(3, 5, 1.5) + .65 * detail(22, 8, 2))
    return enhanced.bandjoin([lab[1], lab[2]]).copy(interpretation='lab').colourspace('srgb')


def drama(image, strength=0, saturation=0, style='natural'):
    """Blend broad local contrast and local tone compression in floating Lab.

    The style argument reserves a single preset boundary for future extensions.
    Saturation scales with strength, and zero strength bypasses even conversion.
    """
    if not strength:
        return image
    if style != 'natural':
        raise ValueError('Unsupported Drama style')
    amount = strength / 100
    lab = image.colourspace('lab').copy_memory()
    lightness = lab[0].copy_memory()
    scale = max(image.width, image.height) / 1600
    broad = scaled_blur(lightness, 60 * scale).copy_memory()
    detail = (.65 * bounded_detail(lightness, scaled_blur(lightness, 3 * scale), 5, 2)
              + 1.2 * bounded_detail(lightness, scaled_blur(lightness, 20 * scale), 9, 2.5)
              + 1.1 * bounded_detail(lightness, broad, 12, 3))
    # Compress the local base toward midtones, preserving detail. The smooth
    # endpoint envelope reduces enhancement near black/white without hard knees.
    envelope = lightness * (100 - lightness) / 2500
    tone = .30 * (50 - broad)
    # Materialize before the three clamp branches to avoid repeating blurs.
    enhanced = (lightness + amount * envelope * (detail + tone)).copy_memory()
    enhanced = (enhanced < 0).ifthenelse(0, (enhanced > 100).ifthenelse(100, enhanced))
    chroma = 1 + amount * saturation / 100
    return enhanced.bandjoin([lab[1] * chroma, lab[2] * chroma]).copy(
        interpretation='lab').colourspace('srgb')


def process(path, edits, edge, source_cache=None):
    """Return sRGB JPEG; edge=0 means original dimensions. Never writes source.

    thumbnail uses loader shrink-on-load (including JPEG) and autorotation.
    libvips gamma uses the reciprocal exponent, so pass 1 / slider gamma:
    output = 255 * (input / 255) ** gamma. Alpha is flattened before gamma.
    """
    import pyvips
    edits = recipe(edits)
    cached = None
    if edge and source_cache:
        import hashlib
        from pathlib import Path
        stat = Path(path).stat()
        key = hashlib.sha256(f'{path}:{stat.st_size}:{stat.st_mtime_ns}:{edge}'.encode()).hexdigest()
        cached = Path(source_cache) / (key + '.v')
    if cached and cached.exists():
        image = pyvips.Image.new_from_file(str(cached))
    elif edge:
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
    if cached and not cached.exists():
        # Lossless, already oriented sRGB pixels; bounded by the server to 2 files.
        image = image.copy_memory()
        temporary = cached.with_suffix('.tmp.v')
        image.write_to_file(str(temporary))
        temporary.replace(cached)
        for old in sorted(cached.parent.glob('[0-9a-f]' * 64 + '.v'), key=lambda p: p.stat().st_mtime_ns)[:-2]:
            old.unlink(missing_ok=True)
    if edits['gamma'] != 1:
        image = image.gamma(exponent=1 / edits['gamma'])
    image = local_contrast(image, edits['pop'])
    image = drama(image, **edits['drama'])
    return image.jpegsave_buffer(Q=88, strip=True)
