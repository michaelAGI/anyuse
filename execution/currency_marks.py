"""Currency marks by standard: a currency symbol (Unicode category Sc) or an ISO 4217 code next to a number.

A price is a number with a currency mark beside it. Which characters are currency symbols is the Unicode character
database's answer (category ``Sc``: $ € £ ¥ ₹ ₽ ₺ ₫ ฿ ₩ ₪ ₴ ...), and which three letters are currency codes is ISO
4217's: neither needs a hand-written list of symbols, and neither knows a site. A symbol may carry letters in front
of it that are part of it (``R$``, ``US$``, ``HK$``). A code counts in capitals, as the standard writes it, and only
right next to an amount: after a number ("12 USD"), or before one written as an amount, with decimals or grouped
thousands ("USD 19.99", "IDR 150.000"). A capital word that happens to spell a code beside a count, a year or a
model number ("TOP 10", "CAD 2024", "GEL 250ML", "AMD 5800X3D") is no currency.

Standards data only, no reading of a page's wording: the extractor and the verifier may both use it and stay
independent of each other.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

#: ISO 4217 alphabetic codes in use (the funds, precious metals and the test and no-currency codes left out)
ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP
CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS
INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU
MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD
XOF XPF YER ZAR ZMW ZWG
""".split())

#: how far (in characters) a mark may stand from its number: a space, a no-break or thin space
GAP = 1
_SPACES = "    "


def is_symbol(ch: str) -> bool:
    """Is ``ch`` a currency symbol (Unicode general category ``Sc``)?"""
    return len(ch) == 1 and unicodedata.category(ch) == "Sc"


def _digit_near(text: str, start: int, end: int) -> bool:
    """Is a digit right before ``start`` or right after ``end``, past at most :data:`GAP` space characters?"""
    j, gap = start - 1, 0
    while j >= 0 and text[j] in _SPACES and gap < GAP:
        j, gap = j - 1, gap + 1
    if j >= 0 and text[j].isdigit():
        return True
    k, gap = end, 0
    while k < len(text) and text[k] in _SPACES and gap < GAP:
        k, gap = k + 1, gap + 1
    return k < len(text) and text[k].isdigit()


_CODE = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")
#: a number written as an amount, right after a code: decimals ("19.99", "1.299,00", "1'299.-") or grouped thousands
#: ("150.000", "1 299"), and no letter glued to its end (a unit or a model: "250ML", "5800X3D")
_AMOUNT_AFTER = re.compile(r"[ \u00a0\u2009\u202f]?(?:\d{1,3}(?:[.,' \u00a0\u2009\u202f]\d{3})+(?:[.,](?:\d{1,2}|-{1,2}|–))?|"
                           r"\d+[.,](?:\d{1,2}|-{1,2}|–))(?![\w])")


def _code_beside_amount(text: str, start: int, end: int) -> bool:
    """Does the code at ``text[start:end]`` follow a number, or precede one written as an amount?"""
    j = start - 1
    if j >= 0 and text[j] in _SPACES:
        j -= 1
    if j >= 0 and text[j].isdigit():
        return True
    return bool(_AMOUNT_AFTER.match(text, end))


def marks_in(text: str) -> List[Tuple[int, str]]:
    """``[(position, mark), ...]`` in text order: every run of currency symbols (with the capital letters directly in
    front of it, ``R$``) that stands next to a number, and every ISO 4217 code that stands next to an amount."""
    s = str(text or "")
    out: List[Tuple[int, str]] = []
    i = 0
    while i < len(s):
        if is_symbol(s[i]):
            j = i
            while j < len(s) and is_symbol(s[j]):
                j += 1
            k = i
            while k > 0 and i - k < 3 and s[k - 1].isalpha() and s[k - 1].isupper() and s[k - 1].isascii():
                k -= 1
            if k > 0 and s[k - 1].isalpha():
                k = i                                  # letters of a longer word in front: not part of the symbol
            if _digit_near(s, k, j):
                out.append((k, s[k:j]))
            i = j
            continue
        i += 1
    for m in _CODE.finditer(s):
        if m.group(0) in ISO_4217 and _code_beside_amount(s, m.start(), m.end()):
            out.append((m.start(), m.group(0)))
    return sorted(out)


def code_of(mark: str) -> Optional[str]:
    """The ISO 4217 code a mark is (``"EUR"`` for ``EUR``), else None: a symbol alone does not say which currency it
    is (``$``, ``¥`` and ``kr`` stand for several), the page's own currency field does."""
    m = str(mark or "").strip()
    return m if m in ISO_4217 else None


__all__ = ["ISO_4217", "is_symbol", "marks_in", "code_of"]
