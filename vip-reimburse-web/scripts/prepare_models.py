"""Build-time download and smoke check of the application's actual OCR models."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from ocr import _paddle_document


def main():
    with tempfile.TemporaryDirectory(prefix='model-check-') as directory:
        path = Path(directory) / 'synthetic.png'
        image = Image.new('RGB', (600, 180), 'white')
        ImageDraw.Draw(image).text((20, 60), 'Total Amount: 106.00', fill='black', font=ImageFont.load_default(size=32))
        image.save(path)
        text, _ = _paddle_document(path, 'image/png')
        if not text.strip():
            raise RuntimeError('OCR model smoke check returned no text')
    print('OCR models are cached and inference completed.')


if __name__ == '__main__':
    main()
