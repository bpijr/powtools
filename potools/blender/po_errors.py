"""Located refusals: every export/import refusal names archive, section, model set, mesh and field.

Pure Python (no bpy). Codecs keep raising their own ValueError subclasses; the Blender side
adds where the problem is with `located(...)` or the `context(...)` manager, which rewrites
the message in place and keeps the exception type (so callers' `except EffectFormatError`
and message checks still work). Nested contexts merge: the innermost value of a part wins.

    with po_errors.context(archive=path, section=0):
        with po_errors.context(model_set=1, mesh="Body:skin"):
            raise ValueError("new vertices must inherit a source vertex")
    # -> "glassjoe.dict · section 0 · model set 1 · mesh 'Body:skin': new vertices must ..."
"""
import contextlib
import os

PARTS = ("archive", "section", "model_set", "mesh", "field")
LABELS = {"archive": "Archive", "section": "Section", "model_set": "Model set", "mesh": "Mesh",
          "field": "Field"}


class Refusal(ValueError):
    """A refusal raised by the Blender tools themselves (codecs keep their own types)."""

    def __init__(self, reason, **where):
        super().__init__(reason)
        _stamp(self, reason, where)


def _clean(where):
    out = {}
    for k in PARTS:
        v = where.get(k)
        if v is None or v == "": continue
        out[k] = os.path.basename(str(v)) if k == "archive" else v
    unknown = set(where) - set(PARTS)
    if unknown: raise TypeError("Unknown location part(s): %s" % sorted(unknown))
    return out


def describe(where):
    """'glassjoe.dict · section 0 · model set 1 · mesh 'Body' · field UV0' (known parts only)."""
    w = _clean(where); bits = []
    if "archive" in w: bits.append(str(w["archive"]))
    if "section" in w: bits.append("section %s" % w["section"])
    if "model_set" in w: bits.append("model set %s" % w["model_set"])
    if "mesh" in w: bits.append("mesh '%s'" % w["mesh"])
    if "field" in w: bits.append("field %s" % w["field"])
    return " · ".join(bits)


def format_message(reason, **where):
    text = describe(where)
    return "%s: %s" % (text, reason) if text else str(reason)


def _stamp(ex, reason, where):
    merged = dict(getattr(ex, "po_where", {}))
    for k, v in _clean(where).items():
        merged.setdefault(k, v)          # inner (already stamped) values win
    try:
        ex.po_reason = str(reason); ex.po_where = merged
        ex.args = (format_message(ex.po_reason, **merged),) + tuple(ex.args[1:])
    except (AttributeError, TypeError):
        pass                              # exotic ValueError subclasses keep their own text
    return ex


def located(ex, **where):
    """Add location parts to an existing exception (in place) and return it."""
    return _stamp(ex, getattr(ex, "po_reason", ex.args[0] if ex.args else str(ex)), where)


@contextlib.contextmanager
def context(**where):
    """Stamp any ValueError (the codec refusal family) raised inside with these parts."""
    _clean(where)
    try:
        yield
    except ValueError as ex:
        located(ex, **where)
        raise


def parts(ex):
    """(reason, {part: value}) for UI display; plain exceptions give ({}, message)."""
    return getattr(ex, "po_reason", str(ex)), dict(getattr(ex, "po_where", {}))


def as_record(ex):
    """JSON-safe record kept in the scene metadata for the Validation and export panel."""
    reason, where = parts(ex)
    return {"reason": reason, "where": {k: (v if isinstance(v, (int, str)) else str(v)) for k, v in where.items()},
            "message": str(ex), "type": type(ex).__name__}


def lines(record):
    """Plain-language rows: every part is named, unknown ones are shown as not applicable."""
    where = record.get("where", {})
    rows = ["%s: %s" % (LABELS[k], where[k]) if k in where else "%s: -" % LABELS[k] for k in PARTS]
    return [record.get("reason", "")] + rows
