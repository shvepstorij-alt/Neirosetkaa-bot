"""Conservative local low-detail screen guard, not an OCR/legibility proof.

Only already-normalized images are inspected. A negative flag means merely that
this very narrow severe-low-detail condition was not detected; it does not mean
text is readable or that a model's transcription is true.
"""
import io

from PIL import Image, ImageChops, ImageFilter, ImageStat


MAX_IMAGES = 3
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 16_000_000
MAX_DIMENSION = 1600
MEAN_LIMIT = 0.02
STRONG_EDGE_LIMIT = 0.0001


class VisualQualityError(ValueError):
    """Controlled diagnostic; never contains image data or decoder errors."""


def _measure(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if (image.format != 'JPEG' or width < 1 or height < 1
                    or max(width, height) > MAX_DIMENSION):
                raise VisualQualityError('Этап: локальная проверка фото. Ожидается нормализованный JPEG размером до 1600 пикселей по стороне.')
            image.load()
            gray = image.convert('L')
            difference = ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(1)))
            mean = ImageStat.Stat(difference).mean[0]
            histogram = difference.histogram()
            strong = sum(histogram[3:]) / (width * height)
            flagged = mean < MEAN_LIMIT and strong < STRONG_EDGE_LIMIT
            return {'severely_low_detail': flagged,
                    'mean_highpass': round(mean, 6),
                    'strong_edge_fraction': round(strong, 8),
                    'width': width, 'height': height}
    except VisualQualityError:
        raise
    except Exception:
        raise VisualQualityError('Этап: локальная проверка фото. Изображение не удалось декодировать; качество не установлено.') from None


def assess_images(images):
    """Return at most 3 indexed metric records, never bytes/captions/file IDs.

    Indices are 1-based positions in the supplied batch. Unknown or malformed
    input raises VisualQualityError instead of being labelled clear. Empty
    batches return an empty list. The caller decides how to request a clearer
    fragment; this function neither sends messages nor calls an external API.
    """
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise VisualQualityError('Этап: локальная проверка фото. Требуется список не более трёх нормализованных изображений.')
    records = []
    total = 0
    for index, picture in enumerate(images, 1):
        raw = picture.get('data') if isinstance(picture, dict) else None
        if (not isinstance(raw, bytes) or not raw.startswith(b'\xff\xd8')
                or not 0 < len(raw) <= MAX_FILE_BYTES):
            raise VisualQualityError('Этап: локальная проверка фото. Нет корректного нормализованного JPEG или превышен лимит 8 МБ; качество не установлено.')
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise VisualQualityError('Этап: локальная проверка фото. Превышен общий лимит 16 МБ; качество не установлено.')
        records.append({'index': index, **_measure(raw)})
    return records
