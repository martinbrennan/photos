"""Disposable image decoder, isolated from the HTTP server's GIL and memory."""
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def render(path, root):
    path = Path(path)
    root = Path(root).resolve(strict=True)
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('Photo is outside its source archive or is a symbolic link')
    # Reject unusually large images rather than risking memory exhaustion on the NUC.
    with Image.open(path) as image:
        if image.width * image.height > 90_000_000:
            raise ValueError('Image exceeds the 90-megapixel slideshow limit')
        image.draft('RGB', (2560, 2560))
        image.thumbnail((2560, 2560))
        image = ImageOps.exif_transpose(image)
        if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, '#101412')
            background.paste(rgba, mask=rgba.getchannel('A'))
            image = background
        else:
            image = image.convert('RGB')
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=88)
        return output.getvalue()


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        sys.stdout.buffer.write(render(request['path'], request['root']))
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
