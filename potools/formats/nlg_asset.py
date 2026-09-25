"""
nlg_asset.py — the generic asset document used by Blender and the CLI tools.

    Game library -> AssetDocument (this module) -> codecs (nlg_model, nlg_texture)
      -> Blender importer/exporter -> PatchSet -> rebuild_with_patches + validation

An AssetDocument is a read-only view of one archive: every section, every model set, every
texture table, and an inventory that says which part owns each chunk. Nothing here decodes a
format itself or needs bpy. Edits come back as a PatchSet of fixed-size byte changes, which
`rebuild_with_patches` applies to the untouched source and audits: outside the recorded
ranges the output must equal the source byte-for-byte.
"""
import hashlib
import json
import math
import struct
from pathlib import Path

import nlg_model
import nlg_texture
import nlg_animation

SCHEMA = 1   # version of the metadata stored on Blender objects and in patch-set files
TOPOLOGY_SCHEMA = 2  # older readers must refuse, rather than silently ignore resizing edits
LAYOUT_SCHEMA = 3    # whole-mesh table edits; schema-2 readers would drop them, so they refuse
RESIZE_SCHEMA = 4    # whole-chunk resizes (nlg_container.Resize); older readers must refuse


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _strict_sections(path):
    """Strict loader shared by the tools, so Blender never has a second parser."""
    import po_archive
    return po_archive.load_sections(path), po_archive


class SectionDoc:
    def __init__(self, index, archive, names):
        self.index, self.archive = index, archive
        self.model_sets = nlg_model.model_sets(archive)
        self.animations = nlg_animation.AnimationSet(archive)
        try:
            self.textures = nlg_texture.list_all_textures(archive, names)
            self.texture_note = None
        except ValueError as ex:
            self.textures = []; self.texture_note = str(ex)
        self.owner = {}
        for s in self.model_sets:
            for ri in s.chunk_ids():
                self.owner[ri] = f"model set {s.index}"
        for e in self.textures:
            self.owner.setdefault(e.header_chunk, "texture table %d" % e.table)
            self.owner.setdefault(e.data_chunk, "texture table %d" % e.table)
        for family, resources in (("rig", self.animations.rigs), ("animation clip", self.animations.clips)):
            for resource in resources:
                for ri in resource.chunks:
                    self.owner.setdefault(ri, "%s %d" % (family, resource.index))
        # Definition codecs own their ordered graphs; failures leave data preserved.
        import nlg_behavior
        self.definitions = None; self.definition_note = None
        try:
            self.definitions = nlg_behavior.Definitions(archive, names)
            for ri, owner in self.definitions.owner.items(): self.owner.setdefault(ri, owner)
        except nlg_behavior.BehaviorFormatError as ex:
            self.definition_note = str(ex)

    def inventory(self):
        """Every data chunk: type, size, original SHA-256 and owning part (None = preserved
        unknown data that no codec touches)."""
        a = self.archive
        return [{"chunk": ri, "type": "0x%04X" % a.chunks[ri][2], "bytes": a.chunks[ri][3],
                 "sha256": sha256(a.get_chunk_bytes(ri)), "owner": self.owner.get(ri)}
                for ri in a.find_chunks()]


class AssetDocument:
    def __init__(self, dict_path, names=None):
        self.path = Path(dict_path)
        archives, po_archive = _strict_sections(self.path)
        self.editable = False; self.limitation = None
        if len(archives) == 1:
            try:
                archives = [po_archive.load_archive(self.path)]; self.editable = True
            except po_archive.ArchiveError as ex:
                self.limitation = str(ex)
        else:
            self.limitation = f"Generic geometry export for this {len(archives)}-section container is read-only; use the cinematic editor for supported camera samples."
        self.names = names or {}
        self.source_hashes = [sha256(self.path.read_bytes()), sha256(self.path.with_suffix(".data").read_bytes())]
        self.sections = [SectionDoc(i, a, self.names) for i, a in enumerate(archives)]

    def name(self, h, fallback=None):
        return self.names.get(h) or fallback or "%08X" % h

    def cinematic_summary(self):
        """Section-local clips and explicit directory/model ownership, without guessed bindings."""
        import nlg_cinematic
        return [{"section": s.index, "cameras": [c.summary() for c in nlg_cinematic.camera_clips(s.archive)],
                 "relationships": nlg_cinematic.relationships(s)} for s in self.sections]

    def texture_hashes(self, section):
        return {e.hash: e for e in self.sections[section].textures}

    def material_references(self, section, model_set, mesh):
        """Texture hashes at registered shader input offsets. Local keys resolve to texture
        entries; all other keys remain explicitly external or unresolved."""
        import nlg_material_layout
        rec = nlg_material_layout.record_for_mesh(model_set, mesh); local = self.texture_hashes(section)
        found, external = [], []
        for ref in rec.references(self.sections[section].textures, self.names):
            o, h = ref["word"], ref["hash"]
            if h in local:
                found.append((o, local[h]))
            elif h not in (0, 0xFFFFFFFF):
                external.append((o, h))
        return found, external

    def mesh_metadata(self, section, model_set, mesh):
        """Everything an imported Blender object must carry back to its source bytes."""
        import nlg_material_layout
        node = model_set.node_of(mesh.index)
        material = nlg_material_layout.record_for_mesh(model_set, mesh).describe(self.sections[section].textures, self.names)
        off, size = model_set.material_span(mesh)
        local, external = self.material_references(section, model_set, mesh)
        attrs = []
        for a in mesh.attributes:
            attrs.append({"semantic": a.semantic, "set": a.set, "type": a.type, "stride": a.stride,
                          "flags": a.flags, "verified": a.verified, "sha256": sha256(a.raw)})
        editable = sorted({f"{a.semantic}{a.set}" for a in mesh.attributes
                           if a.verified and a.semantic in ("position", "normal", "texcoord")})
        if not model_set.skinned: editable.append("transform")
        return {
            "schema": SCHEMA,
            "source": {"path": str(self.path), "sha256": self.source_hashes, "section": section,
                       "editable_container": self.editable},
            "model_set": model_set.index,
            "node": {"index": node.index, "hash": node.name_hash, "name": self.name(node.name_hash)},
            "mesh": {"index": mesh.index, "hash": mesh.name_hash, "name": self.name(mesh.name_hash),
                     "vertices": mesh.vertex_count, "strip_sha256": sha256(struct.pack(">%dH" % len(mesh.strip), *mesh.strip)),
                     "record_sha256": sha256(mesh.record)},
            "material": {"shader": mesh.shader, "shader_name": self.name(mesh.shader), "offset": off, "bytes": size,
                         "layout_known": nlg_model.MATERIAL_SIZES.get(mesh.shader) == size,
                         "record_sha256": sha256(model_set.material_record(mesh)),
                         "textures": [{"word": o, "hash": e.hash, "name": e.name, "index": e.index} for o, e in local],
                         **material},
            # Applied to skinned sets too: crowd vertices only meet their bones after it.
            "transform": {"index": mesh.transform, "matrix": list(model_set.transforms[mesh.transform])},
            "skeleton": {"skinned": model_set.skinned, "palette": mesh.palette or []},
            "chunks": {"%04X" % t: ri for t, ri in model_set.chunks.items()},
            "attributes": attrs,
            "editable": editable,
            "preserved": [f"{a['semantic']}{a['set']}" for a in attrs if f"{a['semantic']}{a['set']}" not in editable],
            "dependencies": [{"word": o, "hash": h, "name": self.name(h), "resolution": "external or unresolved"} for o, h in external],
        }

    def summary(self):
        return {"schema": SCHEMA, "path": str(self.path), "sha256": self.source_hashes,
                "editable": self.editable, "limitation": self.limitation,
                "sections": [{"index": s.index, "model_sets": [
                    {"index": m.index, "nodes": len(m.nodes), "meshes": len(m.meshes), "transforms": len(m.transforms),
                     "skinned": m.skinned, "bones": len(m.bones)} for m in s.model_sets],
                    "textures": len(s.textures), "texture_note": s.texture_note,
                    "rigs": len(s.animations.rigs), "animation_clips": len(s.animations.clips),
                    "animation_issues": s.animations.issues,
                    "unowned_chunks": sum(1 for c in s.inventory() if c["owner"] is None)} for s in self.sections]}


# ---------------------------------------------------------------------------------------
# Patch sets

def transform_patches(model_set, index, matrix):
    """Replace one B002 transform (16 floats, stored row-vector order) in place."""
    if type(index) is not int or not 0 <= index < len(model_set.transforms): raise ValueError("Transform index is out of range")
    if len(matrix) != 16 or not all(math.isfinite(v) for v in matrix): raise ValueError("Transform needs 16 finite components")
    ri = model_set.chunks[0xB002]
    old = struct.pack(">16f", *model_set.transforms[index]); new = struct.pack(">16f", *matrix)
    users = sum(m.transform == index for m in model_set.meshes)
    label = f"model set {model_set.index}: transform {index} changed (places {users} mesh{'es' if users != 1 else ''})"
    return nlg_model.diff_patches(ri, 64 * index, old, new, label)


class SectionPatch(nlg_model.Patch):
    """A fixed-size patch addressed to a cinematic section; routes the rebuild through
    nlg_cinematic.rebuild_sections. Patch itself carries `section` (0 by default)."""
    __slots__ = ()

    def __init__(self, section, chunk, offset, old, new, label):
        super().__init__(chunk, offset, old, new, label, section)


class PatchSet:
    """Changes against one exact source. Fixed-size patches address section 0 unless they
    say otherwise (SectionPatch). Topology entries (single-section archives only) map
    model-set indices to mesh-index/MeshData mappings; coupled chunks are rebuilt and audited.
    `layout` maps a model-set index to the number of output copies of each source mesh (0
    deletes it, 2 duplicates it; nlg_model.check_layout); topology keys then name output meshes,
    numbered in source order with copies following their original."""

    def __init__(self, source_hashes, patches=(), notes=(), topology=None, layout=None, resizes=()):
        self.source_hashes = list(source_hashes); self.patches = list(patches); self.notes = list(notes)
        self.topology = topology or {}; self.layout = layout or {}
        self.resizes = list(resizes)  # nlg_container.Resize: relaid through the container writer

    def review(self):
        """Plain-language change review, one line per labelled change."""
        by_label = {}
        for p in self.patches:
            label = (f"section {p.section}: " if isinstance(p, SectionPatch) or p.section else "") + p.label
            by_label[label] = by_label.get(label, 0) + len(p.new)
        changes = [f"{label} ({n} byte{'s' if n != 1 else ''})" for label, n in sorted(by_label.items())]
        changes += [f"model set {si}, mesh {mi}: rebuild topology ({d.vertex_count} vertices, {len(nlg_model.strip_to_tris(d.strip))} faces)"
                    for si, edits in sorted(self.topology.items()) for mi, d in sorted(edits.items())]
        for si, copies in sorted(self.layout.items()):
            gone = [m for m, n in enumerate(copies) if n == 0]; extra = [m for m, n in enumerate(copies) if n > 1]
            changes.append(f"model set {si}: mesh table rebuilt with {sum(copies)} meshes (was {len(copies)})" +
                           (f"; deleted source meshes {gone}" if gone else "") + (f"; duplicated source meshes {extra}" if extra else ""))
        changes += [f"section {r.section}: {r.label} (chunk {r.chunk} rebuilt as {len(r.new)} bytes)" for r in self.resizes]
        return changes + list(self.notes)

    def as_dict(self):
        schema = (RESIZE_SCHEMA if self.resizes else LAYOUT_SCHEMA if self.layout else
                  TOPOLOGY_SCHEMA if self.topology else SCHEMA)
        return {"format": "po-patch-set", "schema": schema, "source_sha256": self.source_hashes,
                "patches": [dict(p.as_dict(), old=p.old.hex(), new=p.new.hex()) for p in self.patches],
                "topology": [{"model_set": si, "mesh": mi, "strip": d.strip, "vertices": d.vertex_count,
                              "arrays": [a.hex() for a in d.arrays], "palette": d.palette, "source": d.source}
                             for si, edits in sorted(self.topology.items()) for mi, d in sorted(edits.items())],
                **({"mesh_layout": [{"model_set": si, "copies": list(c)} for si, c in sorted(self.layout.items())]}
                   if self.layout else {}),
                **({"resizes": [dict(r.as_dict(), new=r.new.hex()) for r in self.resizes]} if self.resizes else {}),
                "review": self.review()}

    @classmethod
    def from_dict(cls, value):
        if value.get("format") != "po-patch-set" or value.get("schema") not in (SCHEMA,TOPOLOGY_SCHEMA,LAYOUT_SCHEMA,RESIZE_SCHEMA):
            raise ValueError("Unsupported patch set.")
        if value.get("topology") and value["schema"] not in (TOPOLOGY_SCHEMA, LAYOUT_SCHEMA):
            raise ValueError("Topology edits require patch-set schema 2")
        if bool(value.get("mesh_layout")) != (value["schema"] == LAYOUT_SCHEMA):
            raise ValueError("Mesh table edits require patch-set schema 3")
        layout = {}
        for entry in value.get("mesh_layout", []):
            if entry["model_set"] in layout: raise ValueError("Duplicate mesh table edit")
            layout[entry["model_set"]] = [int(n) for n in entry["copies"]]
        if bool(value.get("resizes")) != (value["schema"] == RESIZE_SCHEMA):
            raise ValueError("Chunk resizes require patch-set schema 4")
        import nlg_container
        topology = {}
        for d in value.get("topology", []):
            edits = topology.setdefault(d["model_set"], {})
            if d["mesh"] in edits: raise ValueError("Duplicate topology edit")
            edits[d["mesh"]] = nlg_model.MeshData(d["strip"], d["vertices"],
                [bytes.fromhex(a) for a in d["arrays"]], d["palette"], d["source"])
        def patch(p):
            old, new = bytes.fromhex(p["old"]), bytes.fromhex(p["new"])
            if p.get("section", 0):
                return SectionPatch(p["section"], p["chunk"], p["offset"], old, new, p["label"])
            return nlg_model.Patch(p["chunk"], p["offset"], old, new, p["label"])
        return cls(value["source_sha256"], [patch(p) for p in value["patches"]], topology=topology, layout=layout,
                   resizes=[nlg_container.Resize.from_dict(r) for r in value.get("resizes", [])])


def _chunk_data_offset(archive, ri):
    block = archive._chunk_block(ri)
    return sum(len(b) for b in archive.blocks[:block]) + archive.chunks[ri][4]


def rebuild_with_patches(dict_path, patch_set):
    """(dict bytes, data bytes, audit) for the source with patches applied. Raises when the
    source hash differs, a patch does not match its recorded bytes, or anything outside the
    recorded ranges changed. Deterministic: the same inputs always give the same bytes."""
    import po_archive
    if getattr(patch_set, "resizes", None):
        if patch_set.topology:
            raise ValueError("Topology edits and chunk resizes cannot be combined in one patch set")
        if patch_set.topology or patch_set.layout:
            raise ValueError("Chunk resizes cannot be combined with topology or mesh-table edits; export them separately")
        import nlg_container
        return nlg_container.rebuild(dict_path, patch_set)
    if len(po_archive.sections(Path(dict_path).read_bytes())) > 1 or any(isinstance(p, SectionPatch) for p in patch_set.patches):
        if patch_set.topology or patch_set.layout:
            raise ValueError("Topology edits inside multi-section cinematic containers are not supported")
        from nlg_cinematic import rebuild_sections
        return rebuild_sections(dict_path, patch_set)
    a = po_archive.load_archive(dict_path)
    if [sha256(a.orig_dict), sha256(a.orig_data)] != patch_set.source_hashes:
        raise ValueError("Patch set was made from a different source archive.")
    if any(p.section != 0 for p in patch_set.patches):
        raise ValueError("Multi-section edits are not supported")
    if patch_set.topology or patch_set.layout:
        return _rebuild_topology(a, patch_set)
    allowed = []
    for p in patch_set.patches:
        start = _chunk_data_offset(a, p.chunk) + p.offset; allowed.append((start, start + len(p.new)))
    nlg_model.apply_patches(a, patch_set.patches)
    derived = nlg_model.culling_patches(a) if patch_set.patches else []
    for p in derived:
        start = _chunk_data_offset(a, p.chunk) + p.offset; allowed.append((start, start + len(p.new)))
    nlg_model.apply_patches(a, derived)
    # A single changed run may cross two adjacent authorized fields (e.g. tint and alpha).
    # Merge only touching/overlapping intervals; a gap must still fail the outside-byte audit.
    covered = []
    for lo, hi in sorted(allowed):
        if covered and lo <= covered[-1][1]: covered[-1] = (covered[-1][0], max(covered[-1][1], hi))
        else: covered.append((lo, hi))
    new_dict, new_data = a.build_dict(), a.build_data()
    if new_dict != a.orig_dict:
        raise ValueError("A fixed-size patch changed the dictionary.")
    changed = nlg_model.changed_ranges(a.orig_data, new_data)
    stray = [r for r in changed if not any(lo <= r[0] and r[1] <= hi for lo, hi in covered)]
    if stray:
        raise ValueError(f"Bytes outside the recorded patch ranges changed: {stray[:5]}")
    audit = {"changed_ranges": changed, "changed_bytes": sum(hi - lo for lo, hi in changed),
             "patches": len(patch_set.patches), "derived_culling_patches": [p.as_dict() for p in derived],
             "output_sha256": [sha256(new_dict), sha256(new_data)]}
    return new_dict, new_data, audit


def _rebuild_topology(a, patch_set):
    """Audit relocated archives by chunk identity; only established geometry chunks may
    resize. Every other payload, all record flags and all nonzero inter-chunk bytes survive."""
    original = {ri: a.get_chunk_bytes(ri) for ri in a.find_chunks()}
    records = [tuple(r) for r in a.chunks]
    sets = nlg_model.model_sets(a); replacements = {}
    for si in {*patch_set.topology, *patch_set.layout}:
        if type(si) is not int or not 0 <= si < len(sets): raise ValueError("Unknown model set in topology edit")
        layout = nlg_model.copies_layout(sets[si], patch_set.layout[si]) if si in patch_set.layout else None
        replacements.update(nlg_model.rebuild_chunks(sets[si], patch_set.topology.get(si, {}), layout))
    if any(p.chunk in replacements for p in patch_set.patches):
        raise ValueError("A fixed-size patch overlaps a rebuilt topology chunk")
    nlg_model.apply_patches(a, patch_set.patches)
    gaps = _relocate_chunks(a, replacements)
    derived = nlg_model.culling_patches(a)
    nlg_model.apply_patches(a, derived)
    allowed = {}
    for p in [*patch_set.patches, *derived]: allowed.setdefault(p.chunk, []).append((p.offset, p.offset + len(p.new)))
    changes = []
    for ri, old in original.items():
        new = a.get_chunk_bytes(ri)
        if tuple(a.chunks[ri][:3]) != records[ri][:3]: raise ValueError("Chunk identity changed during topology rebuild")
        if ri not in replacements:
            ranges = nlg_model.changed_ranges(old, new)
            if any(not any(lo <= start and stop <= hi for lo, hi in allowed.get(ri, [])) for start, stop in ranges):
                raise ValueError(f"Unrecorded bytes changed in chunk {ri}")
        else:
            ranges = nlg_model.changed_ranges(old, new) if len(old) == len(new) else []
        if old != new:
            changes.append({"chunk": ri, "type": f"0x{a.chunks[ri][2]:04X}", "old_bytes": len(old), "new_bytes": len(new),
                            "old_sha256": sha256(old), "new_sha256": sha256(new), "ranges": ranges,
                            "resized": len(old) != len(new)})
    dd, da = a.build_dict(), a.build_data()
    audit = {"changed_chunks": changes, "changed_ranges": [], "audit_mode": "chunk identity with relocation",
             "changed_bytes": sum(max(c["old_bytes"], c["new_bytes"]) if c["resized"] else sum(b-a for a,b in c["ranges"]) for c in changes),
             "patches": len(patch_set.patches), "derived_culling_patches": [p.as_dict() for p in derived],
             "preserved_padding": gaps,
             "output_sha256": [sha256(dd), sha256(da)]}
    return dd, da, audit


def _relocate_chunks(a, replacements):
    """Batch resize while retaining each original gap verbatim. Extra alignment is zero;
    every downstream chunk moves by a multiple of 32. Untouched blocks are not rebuilt."""
    audit = []
    for bi in sorted({a._chunk_block(ri) for ri in replacements}):
        original = bytes(a.blocks[bi]); layout = a.data_chunks_in_block(bi)
        if not layout: raise ValueError("Cannot relocate a block without chunks")
        out = bytearray(original[:layout[0][1]])
        if out:
            audit.append({"block":bi,"after_chunk":None,"bytes":len(out),"sha256":sha256(out),
                          "old_offset":0,"new_offset":0,"nonzero_bytes":sum(v != 0 for v in out)})
        for n, (ri, off, size) in enumerate(layout):
            end = layout[n+1][1] if n+1 < len(layout) else len(original)
            gap = original[off+size:end]
            new = replacements.get(ri, original[off:off+size])
            a.chunks[ri][4], a.chunks[ri][3] = len(out), len(new)
            out += new; gap_offset = len(out); out += gap
            if bytes(out[gap_offset:gap_offset+len(gap)]) != gap: raise ValueError("Padding preservation failed")
            if gap:
                audit.append({"block": bi, "after_chunk": ri, "bytes": len(gap), "sha256": sha256(gap),
                              "old_offset": off+size, "new_offset": gap_offset,
                              "nonzero_bytes": sum(v != 0 for v in gap)})
            if n+1 < len(layout): out += bytes((size-len(new)) % 32)
        out += bytes(-len(out) % a.BLOCK_PAD)
        a.blocks[bi] = out
    return audit


def texture_patches(section, entry, rgba):
    """Same-size pixels and every mip level, with alias and truncation checks."""
    a = section.archive; start, end = entry.data_offset, entry.data_offset + entry.chain_size
    if len(rgba) != entry.width * entry.height * 4: raise ValueError("Texture pixel count changed")
    raw = a.get_chunk_bytes(entry.data_chunk)
    if start < 0 or end > len(raw): raise ValueError("Texture chain is outside its pixel chunk")
    if nlg_texture.decode_texture(a, entry) == bytes(rgba): return []
    for other in section.textures:
        if other.index != entry.index and other.data_chunk == entry.data_chunk:
            if start < other.data_offset + other.chain_size and other.data_offset < end:
                raise ValueError("Aliased texture pixels are read-only")
    ri, new = nlg_texture.replace_texture_rgba(a, entry, rgba)
    return nlg_model.diff_patches(ri, 0, raw, new, f"texture {entry.index}: pixels and mip chain changed", section=section.index)


def write_rebuild(dict_path, patch_set, out_dict):
    """Write a patched copy plus `<name>.patchset.json`; re-reads the output to confirm it
    parses, rebuilds exactly, and that a second rebuild is byte-identical."""
    import po_archive
    out_dict = Path(out_dict)
    if out_dict.resolve() == Path(dict_path).resolve():
        raise ValueError("Export to a new file; the source archive is never overwritten.")
    source = Path(dict_path).resolve()
    source_root = next((p for p in source.parents if (p / "hashid.bin").is_file()), source.parent)
    po_archive.external(out_dict, source_root)
    if any(out_dict.with_suffix(ext).exists() for ext in (".dict", ".data", ".patchset.json")):
        raise ValueError("Export destination already exists; choose a new file")
    d1, a1, audit = rebuild_with_patches(dict_path, patch_set)
    d2, a2, _ = rebuild_with_patches(dict_path, patch_set)
    if (d1, a1) != (d2, a2):
        raise ValueError("Rebuild is not deterministic.")
    out_dict.parent.mkdir(parents=True, exist_ok=True)
    out_dict.write_bytes(d1); out_dict.with_suffix(".data").write_bytes(a1)
    checks = po_archive.load_sections(out_dict)   # strict parse + exact round-trip of every section
    for check in checks: nlg_model.model_sets(check)
    report = dict(patch_set.as_dict(), audit=audit, deterministic=True)
    out_dict.with_suffix(".patchset.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report
