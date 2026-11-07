"""Detection-only projection of reviewed public token quantities, never values."""
import re

_TOKEN_QUOTA=re.compile(r'(?<![\w:=])\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?'
                        r'\s*(?:[kKmM]|тыс\.?|тысяч[аи]?|млн\.?)?\s+'
                        r'(?:токен(?:ов|а|ы)?|tokens?)(?!\w)',re.I)


def detection_copy(text):
    """Caller must already possess checked public-evidence provenance.

    «8192 токена» is a measurement; «токен: VALUE» is not changed. Nothing in
    the stored answer or original intake is changed by this projection.
    """
    return _TOKEN_QUOTA.sub('[публичная квота]',str(text))
