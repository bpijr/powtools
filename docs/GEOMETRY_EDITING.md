# Geometry editing evidence and limits

This covers the `nlg_model.py` topology and culling writer. All output archives and patch-set JSON files contain private game data and
belong outside Git and the source dump.

`nlg_model.remap_mesh` adds, removes or reorders vertices and faces within existing mesh
slots. Every output vertex explicitly names an original vertex whose unknown attributes,
colours, weights, bone indices and morph deltas it inherits. Known positions, normals and
UVs can be edited after remapping. Bone palettes, material records, mesh identity fields,
transforms and node partitions are retained. Empty meshes, arbitrary new bones, whole
mesh/node insertion or deletion, and vertices without valid provenance are refused.

`nlg_asset.PatchSet(..., topology={set_index: {mesh_index: mesh_data}})` records this edit
and serializes the map. Rebuilding regenerates B007 strips, B006 arrays and alignment,
B005 pointers, B004 offsets/counts and B00C local morph indices together. Packing must
first reproduce every original coupled chunk byte-for-byte. Batch relocation retains every
original inter-chunk and tail byte verbatim, adds zero alignment as needed, preserves every
untargeted chunk payload and chunk identity, records chunk sizes/hashes and fixed-size
changed ranges, then reparses and repeats deterministically. Topology patch sets use schema 2;
fixed patches and Blender object metadata retain schema 1. Old patch-set readers refuse
schema 2 instead of silently dropping topology changes.
For resized chunks the audit's `changed_bytes` is a conservative payload-size count;
`changed_chunks`, old/new sizes and hashes describe relocation, while equal-size chunks
retain exact changed ranges. It is not a count of physically differing whole-archive bytes.
Fixed patches may not overlap rebuilt chunks. Multi-section cinematic edits remain refused.

Position, topology and transform edits recompute 0x6101 culling bounds. Node references and
tree structure remain unchanged. Malformed trees, missing references and invalid bounds
are refused. Culling patches derived during rebuild appear separately in its audit.

Same-size texture patches compare decoded original pixels before encoding; unchanged lossy
textures retain the original encoded bytes. Changed textures regenerate the whole mip chain
and reject aliases, truncated pixel chains and dimension changes.

## Blender workflow

Import with the generic Punch-Out Asset importer. Duplicate or extrude imported vertices to
carry the `po_vertex_source` point attribute into new geometry. Delete vertices/faces as
needed, or edit UVs; differing UVs at a shared vertex produce separate output vertices.
Export compares all represented proven UV layers and recomputes affected normals. It refuses
missing provenance, missing/duplicate mesh objects, unapplied mesh modifiers and weight edits.
The exporter preserves each new vertex's chosen source morph deltas; it does not synthesize
new facial expressions or interpolate skin weights. Apply desired skin/morph authoring through
a separately verified workflow before relying on the result in game.

Imported texture images carry source identity. Painting their pixels exports the entire mip
chain through the same PatchSet. An unchanged imported image leaves compressed bytes intact.
Texture-only archives can use this route too. Material graph or texture-slot binding changes
belong to the material codec; image painting does not infer changes to shader references.

## Static evidence

- `corpus_geometry_scan.py`: 1,194 archives, 356 model sets re-encoded byte-exactly,
  6,263 mesh layouts validated, 212 morph chunks byte-exact, 273 culling boxes byte-exact,
  and deterministic controlled add/delete edits on all 356 sets in memory. Original vertex
  arrays and non-vertex coupled chunks restore exactly. B006 alignment filler is regenerated
  from the set's established pattern; an unused suffix need not survive a resize and reverse
  resize. No claim is made that those two edits recover the original padding bytes.
- `corpus_model_scan.py`: all 39,385 vertex arrays have established storage in their actual
  retail shader context. Existing synthetic incompatible-shader tests still refuse edits.
- `dol_vertex_format_scan.py`: three constructor identities, vtable setup links and 12
  constant GX VAT calls verified against executable SHA-256
  `6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`.
- The 510 formerly provisional arrays are 239 type 0x16 and 239 type 0x17 on
  diffusedetail (`0x46ABE398`, signed 16-bit / 256), seven type 0x26/4 on constantcolour
  (`0xEE9D919D`, signed 16-bit / 4096), and 25 type 0xFC on stadiumflatreflection
  (`0x32BC21E8`, signed 16-bit / 1024). These scales require the matching shader.
- `blender_asset_roundtrip.py`: synthetic plus ten private archive families pass unchanged
  and edited exports in Blender 3.6.1 and 5.2.0.
- `blender_geometry_roundtrip.py`: both versions pass synthetic plus four private archives
  (205 meshes), including topology, UV seams, morph remapping, full-mip texture painting,
  and unsafe-edit refusal. Synthetic RGBA32 and a private microphone texture
  are painted; other image formats retain their existing codec evidence.

Runtime verification is still pending. Candidate checks are a
static prop vertex/face edit (microphone or belt), an environment bound-extending edit with
camera movement (minorcircuit/gameworld), a skinned fighter duplicate/delete edit with
animation and facial morphs, and a mipped texture repaint checked at multiple distances.
Offline success does not establish runtime safety, LOD behavior or collision changes.
