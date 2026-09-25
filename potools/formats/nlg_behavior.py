"""Definition directories, compiled-script metadata and combo property graphs.

BE records; see BEHAVIOR_FORMAT.md for corpus and static executable evidence. Script
instructions and unestablished record words are opaque. Serialized address-looking
words are never followed as file offsets. Ownership comes from directories, ordered
records and checked counts. Only two existing AnimProperties float fields have an
editor, with deliberately narrow offline bounds; no runtime compatibility is claimed.
"""
import hashlib
import math
import struct
from collections import defaultdict

from nlg_hash import string_to_hash


class BehaviorFormatError(ValueError):
    pass


def _require(ok, message):
    if not ok: raise BehaviorFormatError(message)


def _words(raw):
    _require(len(raw) % 4 == 0, "Record is not a whole number of words.")
    return struct.unpack(f">{len(raw) // 4}I", raw)


def _text(raw):
    _require(b"\0" in raw, "Definition string is not terminated.")
    text, tail = raw.split(b"\0", 1)
    _require(not any(tail) and all(32 <= c < 127 for c in text), "Unsupported definition string/padding.")
    return text.decode("ascii")


def _hash(raw): return hashlib.sha256(raw).hexdigest()


INTERPRETERS = {string_to_hash(s, True): s for s in (
    "BehaviourScriptInterpreter", "ComboScriptInterpreter", "CharacterActionScript",
    "CharacterReactionScript", "FightHudTips")}
PROPERTY_TYPES = {1: "word (integer/hash/enum not distinguished)", 2: "float32", 3: "property group"}
FLOAT_FIELDS = {"TimeScale": (0.0, 3.0), "BlendAmount": (0.0, 0.5)}
ANIM_PROPERTIES = string_to_hash("AnimProperties", True)


def is_definition_archive(a):
    for i, c in enumerate(a.chunks):
        if c[2] == 0xE000 and a._chunk_block(i) < 0: return True
        if c[2] == 0x5000 and i < a.num_file_entries and c[4] < len(a.chunks):
            j = c[4]
            if a.chunks[j][2] == 0x5001 and a._chunk_block(j) >= 0:
                raw = a.get_chunk_bytes(j)
                if len(raw) >= 8 and struct.unpack_from(">I", raw, 4)[0] in INTERPRETERS: return True
    return False


class Definitions:
    """Pure, lossless read-only view; owned data chunks are disjoint and exhaustive.

    Public dictionaries are technical inspection data, not an encoding interface.
    Unknown words retain their raw unsigned representation and original chunk hash.
    """
    def __init__(self, archive, names=None):
        self.archive = archive; self.names = dict(names or {})
        self.owner = {}; self.scripts = []; self.combos = []; self.directories = []
        self.properties = {}; self.groups = {}; self.unknown_tags = set()
        if not is_definition_archive(archive): return
        a = archive
        for i, c in enumerate(a.chunks):
            if a._chunk_block(i) >= 0: continue
            if c[2] not in (0x0001, 0xE000, 0x5000, 0x5020):
                raise BehaviorFormatError(f"Unsupported definition directory {i}.")
            first, count = c[4], c[3]
            _require(first >= a.num_file_entries and first + count <= len(a.chunks),
                     f"Directory {i} has an out-of-bounds record range.")
            self.directories.append({"record": i, "type": c[2], "first": first, "count": count,
                                     "flags_raw": c[:2]})
        for entry in self.directories:
            if entry["type"] == 0x5000: self.scripts.append(self._script(entry))
            elif entry["type"] == 0xE000: self.combos.append(self._combo(entry))
        _require(set(self.owner) == set(a.find_chunks()), "Definition data is not wholly owned by known directories.")
        self._resolve_imports()

    def name(self, value): return self.names.get(value, f"{value:08X}")

    def _own(self, chunk, owner):
        _require(chunk not in self.owner, f"Definition chunk {chunk} has overlapping ownership.")
        self.owner[chunk] = owner

    def _record(self, chunk, kind, owner, size=None):
        a = self.archive
        _require(0 <= chunk < len(a.chunks) and a._chunk_block(chunk) >= 0 and a.chunks[chunk][2] == kind,
                 f"Expected definition record 0x{kind:04X} at chunk {chunk}.")
        raw = a.get_chunk_bytes(chunk)
        _require(size is None or len(raw) == size, f"Unsupported size for chunk {chunk}.")
        self._own(chunk, owner)
        return raw

    def _script(self, entry):
        first, end = entry["first"], entry["first"] + entry["count"]
        owner = f"script {first}"
        raw = self._record(first, 0x5001, owner)
        _require(len(raw) >= 32, "Truncated compiled script header.")
        header = _words(raw[:32]); zero, interpreter, module, count, pool, code, strings, extra = header
        _require(interpreter in INTERPRETERS and zero == 0 and len(raw) == 32 + pool + code + strings
                 and pool % 4 == 0 and code % 2 == 0, "Unsupported compiled script layout.")
        constants = _words(raw[32:32 + pool]); string_data = raw[32 + pool + code:]
        string_rows = []; pos = 0
        while pos < len(string_data):
            stop = string_data.find(b"\0", pos)
            _require(stop >= pos, "Unterminated script string pool.")
            string_rows.append({"offset": pos, "text": string_data[pos:stop].decode("latin1")})
            pos = stop + 1
        script = {"chunk": first, "name_hash": module, "name": self.name(module),
                  "interpreter_hash": interpreter, "interpreter": INTERPRETERS[interpreter],
                  "header_unknown_raw": extra, "constant_bytes": pool, "code_bytes": code,
                  "code_offset": 32 + pool, "code_sha256": _hash(raw[32 + pool:32 + pool + code]),
                  "constants": [{"offset": i * 4, "word_raw": w,
                                 "registry_match": self.names.get(w), "status": "untyped constant"}
                                for i, w in enumerate(constants)],
                  "strings": string_rows, "classes": [], "imports": [], "debug": [],
                  "status": "Metadata decoded; instructions opaque"}
        k = first + 1
        for _ in range(count):
            b = self._record(k, 0x5002, owner)
            _require(len(b) >= 12, "Truncated script class.")
            ch, functions, layout = _words(b[:12]); variables = layout & 0xFFFF
            _require(len(b) == 12 + 8 * variables, "Script variable count does not match its record.")
            f = self._record(k + 1, 0x5013, owner, 12 * functions)
            cls = {"chunk": k, "name_hash": ch, "name": self.name(ch), "storage_bytes": layout >> 16,
                   "variables": [], "functions": []}
            for o in range(12, len(b), 8):
                type_hash, packed = _words(b[o:o + 8])
                cls["variables"].append({"record_offset": o, "type_hash": type_hash,
                                         "type_name": self.name(type_hash), "storage_offset": packed >> 16,
                                         "index": packed & 0xFFFF})
            for o in range(0, len(f), 12):
                h, pc, flags = _words(f[o:o + 12])
                _require(pc * 2 < code, "Script function starts outside the code span.")
                cls["functions"].append({"chunk": k + 1, "record_offset": o, "name_hash": h,
                                         "name": self.name(h), "code_unit_offset": pc,
                                         "code_byte_offset": 2 * pc, "signature_raw": flags})
            script["classes"].append(cls); k += 2
        if k < end - 1:
            data = _words(self._record(k, 0x5011, owner)); pos = 0
            def word():
                nonlocal pos
                _require(pos < len(data), "Truncated script import directory.")
                value = data[pos]; pos += 1; return value
            modules = word()
            _require(modules <= len(data) // 2, "Invalid script import count.")
            for _ in range(modules):
                h, classes = word(), word(); imp = {"name_hash": h, "name": self.name(h), "classes": []}
                _require(classes <= len(data) // 2, "Invalid imported class count.")
                for _ in range(classes):
                    h, n = word(), word()
                    _require(n <= len(data) - pos, "Truncated imported function list.")
                    imp["classes"].append({"name_hash": h, "name": self.name(h),
                                           "function_hashes": [word() for _ in range(n)]})
                script["imports"].append(imp)
            _require(pos == len(data), "Trailing script import words."); k += 1
        _require(k == end - 1 and self.archive.chunks[k][2] == 0x5020,
                 "Script directory does not end at its debug directory.")
        debug = next((d for d in self.directories if d["record"] == k), None)
        _require(debug is not None and debug["count"] in (0, 4), "Unsupported script debug directory.")
        for j, kind in enumerate((0x5021, 0x5022, 0x5023, 0x5024)[:debug["count"]]):
            ri = debug["first"] + j; b = self._record(ri, kind, owner)
            script["debug"].append({"chunk": ri, "type": kind, "bytes": len(b), "sha256": _hash(b)})
            if kind == 0x5022:
                _require(len(b) >= 4, "Truncated debug name directory.")
                n = _words(b[:4])[0]; base = 4 + n * 8
                _require(base <= len(b), "Debug names extend outside their chunk.")
                for o in range(4, base, 8):
                    h, offset = _words(b[o:o + 8]); start = base + offset; stop = b.find(b"\0", start)
                    _require(start < len(b) and stop >= start, "Invalid debug name offset.")
                    self.names.setdefault(h, b[start:stop].decode("latin1"))
        return script

    def _combo(self, entry):
        first, end = entry["first"], entry["first"] + entry["count"]
        k = first; owner = f"combo {first}"
        def take(kind, size=None):
            nonlocal k
            _require(k < end, "Combo ends before its declared children.")
            ri = k; raw = self._record(k, kind, owner, size); k += 1
            return ri, raw
        def group(depth=0):
            _require(depth <= 64, "Property nesting exceeds the supported depth.")
            ri, raw = take(0xE013); w = _words(raw)
            _require(len(w) >= 2 and len(w) == 2 + w[1], "Property group child count mismatch.")
            _require(all(x == 0xDEADBEEF for x in w[2:]), "Unknown property group placeholder layout.")
            result = {"chunk": ri, "unrelocated_word": w[0], "properties": []}
            self.groups[ri] = result
            for _ in range(w[1]):
                pi, b = take(0xE014, 12); h, tag, value = _words(b)
                prop = {"chunk": pi, "name_hash": h, "name": self.name(h), "tag": tag,
                        "type": PROPERTY_TYPES.get(tag, "opaque tag"), "word_raw": value}
                self.properties[pi] = prop
                if tag == 2:
                    v = struct.unpack_from(">f", b, 8)[0]
                    prop["value"] = v if math.isfinite(v) else None
                elif tag == 3: prop["group"] = group(depth + 1)
                elif tag != 1: self.unknown_tags.add(tag)
                result["properties"].append(prop)
            return result
        ri, raw = take(0xE001); h = _words(raw)
        _require(len(h) >= 6 and len(h) == 6 + h[0], "Combo node count mismatch.")
        _, raw = take(0xE002)
        combo = {"chunk": ri, "name": _text(raw), "header_raw": h, "nodes": []}
        serial = 0
        for index in range(h[0]):
            ri, raw = take(0xE010, 20); hd = _words(raw)
            _, raw = take(0xE011, 44); meta = _words(raw)
            _, raw = take(0xE012); name = _text(raw)
            _require(hd[4] == index and hd[2] == string_to_hash(name, True), "Combo node index/name hash mismatch.")
            tri, raw = take(0xE030)
            _require(len(raw) == 68 * meta[7] and meta[7] == meta[8], "Combo transition count mismatch.")
            transitions = [_words(raw[o:o + 68]) for o in range(0, len(raw), 68)]
            node = {"chunk": ri, "index": index, "name_hash": hd[2], "name": name,
                    "header_raw": hd, "metadata_raw": meta, "properties": group(), "transitions": []}
            for i, row in enumerate(transitions):
                ei, raw = take(0xE031)
                _require(row[16] == serial, "Combo transition serial index mismatch.")
                edge = {"chunk": ei, "record_chunk": tri, "record_offset": i * 68,
                        "serial": serial, "target_name": _text(raw), "record_raw": row,
                        "properties": group() if row[12] else None}
                node["transitions"].append(edge); serial += 1
            ai, raw = take(0xE015, 4 * meta[5]); node["animation_chunk"] = ai
            node["animation_hashes"] = list(_words(raw))
            _, raw = take(0xE015, 4 * meta[5]); node["animation_aux_raw"] = _words(raw)
            combo["nodes"].append(node)
        _require(k == end, "Unowned records at the end of the combo directory.")
        targets = defaultdict(list)
        for node in combo["nodes"]: targets[node["name"]].append(node["index"])
        for node in combo["nodes"]:
            for edge in node["transitions"]:
                edge["target_nodes"] = targets[edge["target_name"]]
                edge["resolution"] = "local name match" if edge["target_nodes"] else "unresolved (no local name match)"
        return combo

    def _resolve_imports(self):
        modules = defaultdict(list)
        for s in self.scripts: modules[s["name_hash"]].append(s)
        for s in self.scripts:
            s["name"] = self.name(s["name_hash"])
            for c in s["classes"]:
                c["name"] = self.name(c["name_hash"])
                for f in c["functions"]: f["name"] = self.name(f["name_hash"])
            for imp in s["imports"]:
                matches = modules[imp["name_hash"]]; imp["local_modules"] = [m["chunk"] for m in matches]
                for c in imp["classes"]:
                    classes = [x for m in matches for x in m["classes"] if x["name_hash"] == c["name_hash"]]
                    functions = {f["name_hash"] for x in classes for f in x["functions"]}
                    c["resolved_function_hashes"] = [h for h in c["function_hashes"] if h in functions]

    def summary(self):
        nodes = [n for c in self.combos for n in c["nodes"]]
        return {"scripts": len(self.scripts), "classes": sum(len(s["classes"]) for s in self.scripts),
                "functions": sum(len(c["functions"]) for s in self.scripts for c in s["classes"]),
                "combos": len(self.combos), "nodes": len(nodes),
                "transitions": sum(len(n["transitions"]) for n in nodes),
                "unresolved_transitions": sum(not e["target_nodes"] for n in nodes for e in n["transitions"]),
                "property_groups": len(self.groups), "properties": len(self.properties),
                "unknown_property_tags": sorted(self.unknown_tags), "owned_chunks": len(self.owner),
                "runtime_verified": False}


def editable_fields(definitions, combo, node):
    """Existing, unique DOL-confirmed floats directly inside the node's AnimProperties."""
    _require(type(combo) is int and 0 <= combo < len(definitions.combos), "Invalid combo index.")
    nodes = definitions.combos[combo]["nodes"]
    _require(type(node) is int and 0 <= node < len(nodes), "Invalid node index.")
    props = nodes[node]["properties"]["properties"]
    groups = [p for p in props if p["name_hash"] == ANIM_PROPERTIES]
    _require(len(groups) == 1 and groups[0]["tag"] == 3, "AnimProperties must be one existing property group.")
    fields = {}
    for name, (lo, hi) in FLOAT_FIELDS.items():
        matches = [p for p in groups[0]["group"]["properties"] if p["name_hash"] == string_to_hash(name, True)]
        if len(matches) != 1: continue
        p = matches[0]
        if p["tag"] == 2 and p["value"] is not None and lo <= p["value"] <= hi:
            fields[name] = {"chunk": p["chunk"], "value": p["value"], "minimum": lo, "maximum": hi}
    return fields


def edit_patchset(document, edits):
    """AssetDocument -> fixed-size PatchSet; no instructions, refs, counts or strings edited.

    edits contains (combo index, node index, field name, value). Bounds are the observed
    corpus envelope, a conservative editor policy rather than a runtime guarantee.
    Duplicate targets are rejected; callers can replace a staged operation explicitly.
    """
    import nlg_model
    from nlg_asset import PatchSet
    _require(document.editable and len(document.sections) == 1, "Definition editing requires a strict single-section source.")
    a = document.sections[0].archive
    _require(a.version == 0x0601 and a.pad == 0, "Unsupported definition archive version/flags.")
    d = Definitions(a, document.names)
    _require(d.combos and not d.unknown_tags, "Unknown property tags or no combo graph; editing is disabled.")
    _require(isinstance(edits, (tuple, list)) and len(edits) <= 100, "Use at most 100 bounded definition edits.")
    patches = []; seen = set()
    for edit in edits:
        _require(isinstance(edit, (tuple, list)) and len(edit) == 4, "Invalid definition edit.")
        combo, node, field, value = edit
        _require(type(field) is str and field in FLOAT_FIELDS, "That definition field is inspect-only.")
        fields = editable_fields(d, combo, node)
        _require(field in fields, "Field is absent, duplicated, has a different type, or is outside the supported source range.")
        f = fields[field]; lo, hi = f["minimum"], f["maximum"]
        _require(type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi,
                 f"{field} must be finite and between {lo:g} and {hi:g}.")
        target = (combo, node, field)
        _require(target not in seen, "Duplicate definition edit target."); seen.add(target)
        raw = a.get_chunk_bytes(f["chunk"])
        patches.extend(nlg_model.diff_patches(f["chunk"], 8, raw[8:12], struct.pack(">f", value),
                       f"combo {combo}, node {node}: {field} = {value:g} (runtime unverified)"))
    return PatchSet(document.source_hashes, patches)


def animation_index(archive):
    """Explicit hash matches to 7002 clip names, retaining every collision/duplicate."""
    result = defaultdict(list); header = None
    for ri in archive.find_chunks():
        kind = archive.chunks[ri][2]; raw = archive.get_chunk_bytes(ri)
        if kind == 0x7001:
            header = (ri, struct.unpack_from(">I", raw, 8)[0] if len(raw) >= 12 else None)
        elif kind == 0x7002 and header:
            name = raw.split(b"\0", 1)[0].decode("latin1")
            result[string_to_hash(name, True)].append({"chunk": header[0], "name": name,
                                                      "frames": header[1], "evidence": "case-sensitive clip-name hash match"})
    return dict(result)


def techniques(raw, names=None):
    """Read-only BTGN shader/technique hash columns; unrelated to fighter tactics."""
    _require(len(raw) >= 16, "Truncated global technique table.")
    magic, version, count, columns = _words(raw[:16])
    _require(magic == 0x4254474E and version == 2 and columns == 1 and len(raw) == 16 + count * 8,
             "Unsupported global technique table layout.")
    words = _words(raw[16:]); names = names or {}
    return [{"index": i, "shader_hash": words[i], "shader_name": names.get(words[i]),
             "technique_hash": words[count + i], "technique_name": names.get(words[count + i])}
            for i in range(count)]
