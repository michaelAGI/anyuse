"""Independent document-format projections; no app calls or app-name dispatch."""
import json
import plistlib


def fen_labels(raw, key, labels):
    """Read standard FEN placement from a property list and enumerate all 64 squares.

    The supplied vocabulary is reviewed interface data. This checks piece
    placement only, not legal move generation, whose turn it is, or engine state.
    """
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024 or not isinstance(key, str) or not key:
        raise ValueError("invalid bounded position document")
    value = plistlib.loads(raw)
    fen = value.get(key) if isinstance(value, dict) else None
    if not isinstance(fen, str) or len(fen) > 200 or len(fen.split()) != 6:
        raise ValueError("position document needs standard six-field FEN")
    if not isinstance(labels, dict) or set(labels) != set("PNBRQKpnbrqk") | {"empty", "occupied"}:
        raise ValueError("position reference needs a complete reviewed label vocabulary")
    for name, template in labels.items():
        if not isinstance(template, str) or not template or len(template) > 120 or "\0" in template:
            raise ValueError("invalid position label")
        if name not in {"empty", "occupied"} and ("{" in template or "}" in template):
            raise ValueError("piece names must be literal labels")
        if name in {"empty", "occupied"} and any(c in template.replace("{square}", "").replace("{piece}", "") for c in "{}"):
            raise ValueError("unknown square label placeholder")
    if labels["empty"].count("{square}") != 1 or "{piece}" in labels["empty"] or labels["occupied"].count("{square}") != 1 or labels["occupied"].count("{piece}") != 1:
        raise ValueError("square labels need explicit piece/square placeholders")
    placement, side, castling, ep, half, full = fen.split()
    if (side not in {"w", "b"} or (castling != "-" and any(c not in "KQkq" for c in castling)) or
            (ep != "-" and (len(ep) != 2 or ep[0] not in "abcdefgh" or ep[1] not in "36")) or
            not half.isdecimal() or not full.isdecimal() or int(full) < 1):
        raise ValueError("invalid FEN fields")
    ranks = placement.split("/")
    if len(ranks) != 8:
        raise ValueError("position needs eight ranks")
    seen, kings = [], {"K": 0, "k": 0}
    for rank, encoded in zip(range(8, 0, -1), ranks):
        pieces = []
        for token in encoded:
            if token in "12345678":
                pieces.extend([None] * int(token))
            elif token in "PNBRQKpnbrqk":
                pieces.append(token)
                if token in kings:
                    kings[token] += 1
            else:
                raise ValueError("invalid FEN piece")
        if len(pieces) != 8:
            raise ValueError("position ranks must contain eight squares")
        for file, piece in zip("abcdefgh", pieces):
            square = f"{file}{rank}"
            template = labels["empty" if piece is None else "occupied"]
            seen.append(template.format(square=square, piece=labels.get(piece, "")))
    if kings != {"K": 1, "k": 1}:
        raise ValueError("position must have one king of each color")
    return json.dumps(sorted(seen), ensure_ascii=False, separators=(",", ":"))
