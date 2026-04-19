bl_info = {
    "name": "Asset Prep Tools",
    "author": "Claude",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Hatch Tools",
    "description": "Baking preparation, GLB compression, and image conversion tools",
    "category": "Object",
}

import bpy
import re
import math
import subprocess
import os
import tempfile
from bpy.props import (
    StringProperty, IntProperty, BoolProperty, FloatProperty,
    EnumProperty, PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


# ===========================================================================
# BAKING PREP — helpers
# ===========================================================================

def _walk_layer_collections(layer_col, out):
    if getattr(layer_col, "is_selected", False):
        col = layer_col.collection
        if col and col not in out:
            out.append(col)
    for child in layer_col.children:
        _walk_layer_collections(child, out)


def get_selected_collections(context):
    selected = []
    root = context.view_layer.layer_collection
    _walk_layer_collections(root, selected)
    if hasattr(context, "selected_ids"):
        for id_block in context.selected_ids:
            if isinstance(id_block, bpy.types.Collection) and id_block not in selected:
                selected.append(id_block)
    if not selected:
        active_col = context.view_layer.active_layer_collection
        if active_col and active_col.collection:
            selected.append(active_col.collection)
    return selected


def strip_existing_id_prefix(name, prefix_pattern):
    match = re.match(prefix_pattern, name)
    if match:
        return match.group("rest")
    return name


def build_prefix(id_prefix, counter, extra_tag):
    parts = [id_prefix, str(counter)]
    if extra_tag.strip():
        parts.append(extra_tag.strip())
    return "_".join(parts)


def collections_with_id_prefix(prefix):
    pattern = re.compile(r"^" + re.escape(prefix) + r"_(?P<num>\d+)_")
    results = []
    for col in bpy.data.collections:
        m = pattern.match(col.name)
        if m:
            results.append((col, int(m.group("num"))))
    results.sort(key=lambda x: x[1])
    return results


def _strip_collection_prefix(name, col_name):
    prefix = col_name + "_"
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


GRID_COPY_KEY = "coltag_grid_copy"
GRID_ORIG_LOC_KEY = "coltag_original_location"


# ===========================================================================
# BAKING PREP — properties
# ===========================================================================

class CollectionTaggerSettings(PropertyGroup):
    id_prefix: StringProperty(name="ID Prefix", default="ID")
    start_index: IntProperty(name="Start Index", default=1, min=0)
    extra_tag: StringProperty(name="Extra Tag", default="")
    strip_old_prefix: BoolProperty(name="Strip Existing Prefix", default=True)
    apply_to_mesh_data: BoolProperty(name="Apply to Mesh Data", default=False)
    uv_map_name: StringProperty(name="UV Map Name", default="UVMap")
    uv_set_active: BoolProperty(name="Set Active (Blue Highlight)", default=True)
    uv_set_render: BoolProperty(name="Set Active Render (Camera Icon)", default=False)
    grid_spacing: FloatProperty(name="Spacing", default=2.0, min=0.01, soft_max=50.0, unit="LENGTH")
    grid_columns: IntProperty(name="Columns", default=0, min=0)
    grid_link_data: BoolProperty(name="Linked Duplicate", default=False)


# ===========================================================================
# BAKING PREP — operators
# ===========================================================================

class COLTAG_OT_label_collections(Operator):
    bl_idname = "coltag.label_collections"
    bl_label = "Label Selected Collections"
    bl_description = "Rename each selected collection by prepending <Prefix>_<N>[_<Tag>]_ to the original name"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.coltag_settings
        selected = get_selected_collections(context)
        if not selected:
            self.report({"WARNING"}, "No collections selected.")
            return {"CANCELLED"}
        prefix = settings.id_prefix.strip() or "ID"
        counter = settings.start_index
        extra_tag = settings.extra_tag.strip()
        strip_pattern = re.compile(
            r"^" + re.escape(prefix) + r"_\d+_(?:[^_]+_)?(?P<rest>.+)$"
        )
        renamed = []
        for col in selected:
            original = col.name
            base = (
                strip_existing_id_prefix(original, strip_pattern)
                if settings.strip_old_prefix else original
            )
            new_name = f"{build_prefix(prefix, counter, extra_tag)}_{base}"
            col.name = new_name
            renamed.append(f"{original!r} -> {new_name!r}")
            counter += 1
        self.report({"INFO"}, f"Renamed {len(renamed)} collection(s).")
        return {"FINISHED"}


class COLTAG_OT_propagate_names(Operator):
    bl_idname = "coltag.propagate_names"
    bl_label = "Propagate Names to Objects"
    bl_description = "For every ID-prefixed collection, prepend the collection name to each contained object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.coltag_settings
        prefix = settings.id_prefix.strip() or "ID"
        tagged = collections_with_id_prefix(prefix)
        if not tagged:
            self.report({"WARNING"}, f"No collections found with prefix '{prefix}_<N>_'.")
            return {"CANCELLED"}
        total = 0
        for col, _ in tagged:
            col_name = col.name
            for obj in col.objects:
                obj_base = _strip_collection_prefix(obj.name, col_name)
                obj.name = f"{col_name}_{obj_base}"
                if settings.apply_to_mesh_data and obj.data is not None:
                    data_base = _strip_collection_prefix(obj.data.name, col_name)
                    obj.data.name = f"{col_name}_{data_base}"
                total += 1
        self.report({"INFO"}, f"Renamed {total} object(s) across {len(tagged)} collection(s).")
        return {"FINISHED"}


class COLTAG_OT_clear_labels(Operator):
    bl_idname = "coltag.clear_labels"
    bl_label = "Clear ID Labels"
    bl_description = "Remove the ID prefix from all selected collections"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.coltag_settings
        selected = get_selected_collections(context)
        prefix = settings.id_prefix.strip() or "ID"
        strip_pattern = re.compile(
            r"^" + re.escape(prefix) + r"_\d+_(?:[^_]+_)?(?P<rest>.+)$"
        )
        cleared = 0
        for col in selected:
            m = strip_pattern.match(col.name)
            if m:
                col.name = m.group("rest")
                cleared += 1
        self.report({"INFO"}, f"Cleared prefix from {cleared} collection(s).")
        return {"FINISHED"}


class COLTAG_OT_grid_duplicate(Operator):
    bl_idname = "coltag.grid_duplicate"
    bl_label = "Duplicate to Grid"
    bl_description = "Duplicate every selected object and arrange the copies in a grid centred on the world origin"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.coltag_settings
        source_objects = list(context.selected_objects)
        if not source_objects:
            self.report({"WARNING"}, "No objects selected.")
            return {"CANCELLED"}
        n = len(source_objects)
        spacing = settings.grid_spacing
        cols = settings.grid_columns if settings.grid_columns > 0 else math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        offset_x = (cols - 1) * spacing * 0.5
        offset_y = (rows - 1) * spacing * 0.5
        bpy.ops.object.select_all(action="DESELECT")
        linked = settings.grid_link_data
        new_objects = []
        for i, src in enumerate(source_objects):
            col_idx = i % cols
            row_idx = i // cols
            new_obj = src.copy()
            if not linked and src.data is not None:
                new_obj.data = src.data.copy()
            new_obj[GRID_COPY_KEY] = True
            new_obj[GRID_ORIG_LOC_KEY] = list(src.location)
            new_obj.location.x = col_idx * spacing - offset_x
            new_obj.location.y = -(row_idx * spacing - offset_y)
            new_obj.location.z = 0.0
            for col in src.users_collection:
                col.objects.link(new_obj)
            src.hide_set(True)
            new_objects.append(new_obj)
        for obj in new_objects:
            obj.select_set(True)
        context.view_layer.objects.active = new_objects[-1]
        self.report({"INFO"}, f"Created {n} duplicate(s) in a {cols} x {rows} grid. Originals hidden.")
        return {"FINISHED"}


class COLTAG_OT_snap_back(Operator):
    bl_idname = "coltag.snap_back"
    bl_label = "Snap Back to Original"
    bl_description = "Move all grid-duplicated copies back to their original object locations"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        snapped = 0
        for obj in bpy.data.objects:
            if obj.get(GRID_COPY_KEY) and GRID_ORIG_LOC_KEY in obj:
                loc = obj[GRID_ORIG_LOC_KEY]
                obj.location.x = loc[0]
                obj.location.y = loc[1]
                obj.location.z = loc[2]
                snapped += 1
        if snapped == 0:
            self.report({"WARNING"}, "No grid copies found. Run Duplicate to Grid first.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Snapped {snapped} object(s) back to original location(s).")
        return {"FINISHED"}


class COLTAG_OT_add_uv_map(Operator):
    bl_idname = "coltag.add_uv_map"
    bl_label = "Add UV Map"
    bl_description = "Add a new UV map to every selected mesh object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.coltag_settings
        uv_name = settings.uv_map_name.strip() or "UVMap"
        mesh_objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not mesh_objects:
            self.report({"WARNING"}, "No mesh objects selected.")
            return {"CANCELLED"}
        added = 0
        for obj in mesh_objects:
            uv_maps = obj.data.uv_layers
            new_uv = uv_maps.new(name=uv_name)
            if settings.uv_set_active:
                uv_maps.active = new_uv
            if settings.uv_set_render:
                new_uv.active_render = True
                for uv in uv_maps:
                    if uv != new_uv:
                        uv.active_render = False
            added += 1
        self.report({"INFO"}, f"Added UV map '{uv_name}' to {added} object(s).")
        return {"FINISHED"}


# ===========================================================================
# CODE PREP — properties
# ===========================================================================

class GLBCompressorProps(PropertyGroup):
    process_dir: BoolProperty(name="Process Entire Directory", default=False)
    input_file: StringProperty(name="Input File", subtype='FILE_PATH')
    input_dir: StringProperty(name="Input Directory", subtype='DIR_PATH')
    output_dir: StringProperty(name="Output Directory", subtype='DIR_PATH',
                               description="Leave empty to write next to the source file(s)")
    use_resize: BoolProperty(name="Resize Textures", default=False)
    resize_width: IntProperty(name="Width", default=1024, min=1, max=16384)
    resize_height: IntProperty(name="Height", default=1024, min=1, max=16384)
    use_draco: BoolProperty(name="Draco Compression", default=True)
    draco_position_bits: IntProperty(name="Position Bits", default=14, min=1, max=16)
    draco_normal_bits: IntProperty(name="Normal Bits", default=10, min=1, max=16)
    draco_color_bits: IntProperty(name="Color Bits", default=8, min=1, max=16)
    draco_uv_bits: IntProperty(name="UV Bits", default=12, min=1, max=16)
    use_ktx2: BoolProperty(name="KTX2 Compression", default=False)
    ktx2_codec: EnumProperty(
        name="Codec",
        items=[('uastc', 'UASTC', 'Higher quality, larger files'),
               ('etc1s', 'ETC1S', 'Smaller files, good for colour textures')],
        default='etc1s',
    )
    ktx2_quality: IntProperty(name="Quality", default=128, min=1, max=255)
    use_webp: BoolProperty(name="WebP Textures", default=False)
    webp_quality: IntProperty(name="WebP Quality", default=80, min=0, max=100)
    verbose: BoolProperty(name="Verbose", default=True)


class GLTFJSXProps(PropertyGroup):
    process_dir: BoolProperty(name="Process Entire Directory", default=False)
    input_file: StringProperty(name="Input File", subtype='FILE_PATH')
    input_dir: StringProperty(name="Input Directory", subtype='DIR_PATH')
    output_dir: StringProperty(name="Output Directory", subtype='DIR_PATH')
    types: BoolProperty(name="TypeScript (-t)", default=False)
    keepnames: BoolProperty(name="Keep Names (-k)", default=False)
    keepgroups: BoolProperty(name="Keep Groups (-K)", default=False)
    bones: BoolProperty(name="Bones (-b)", default=False)
    meta: BoolProperty(name="Metadata (-m)", default=False)
    shadows: BoolProperty(name="Shadows (-s)", default=False)
    instance: BoolProperty(name="Instance (-i)", default=False)
    instanceall: BoolProperty(name="Instance All (-I)", default=False)
    exportdefault: BoolProperty(name="Export Default (-E)", default=False)
    console: BoolProperty(name="Log to Console (-c)", default=False)
    debug: BoolProperty(name="Debug (-D)", default=False)
    printwidth: IntProperty(name="Print Width (-w)", default=120, min=40, max=500)
    precision: IntProperty(name="Precision (-p)", default=3, min=0, max=10)
    draco_path: StringProperty(name="Draco Path (-d)", subtype='FILE_PATH')
    root: StringProperty(name="Root (-r)", subtype='DIR_PATH')
    transform: BoolProperty(name="Transform for Web (-T)", default=False)
    resolution: IntProperty(name="Resolution (-R)", default=1024, min=1, max=8192)
    keepmeshes: BoolProperty(name="Keep Meshes (-j)", default=False)
    keepmaterials: BoolProperty(name="Keep Materials (-M)", default=False)
    tex_format: EnumProperty(
        name="Texture Format (-f)",
        items=[('webp', 'WebP', ''), ('png', 'PNG', ''), ('jpg', 'JPG', '')],
        default='webp',
    )
    simplify: BoolProperty(name="Simplify (-S)", default=False)
    simplify_ratio: FloatProperty(name="Ratio", default=0.0, min=0.0, max=1.0, precision=4)
    simplify_error: FloatProperty(name="Error", default=0.0001, min=0.0, max=1.0, precision=6)


# ===========================================================================
# CODE PREP — operators
# ===========================================================================

class GLTF_OT_compress(Operator):
    bl_idname = "gltf.compress"
    bl_label = "Compress GLB"
    bl_description = "Run gltf-transform with the chosen options"

    def execute(self, context):
        props = context.scene.glb_compressor
        if props.process_dir:
            return self._process_directory(props)
        return self._process_single_file(props)

    def _process_single_file(self, props):
        src = bpy.path.abspath(props.input_file)
        if not src or not os.path.isfile(src):
            self.report({'ERROR'}, "Input file not found: " + src)
            return {'CANCELLED'}
        out_dir = bpy.path.abspath(props.output_dir) or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)
        name, ext = os.path.splitext(os.path.basename(src))
        dst = os.path.join(out_dir, f"{name}_compressed{ext}")
        ok = self._run_pipeline(props, src, dst)
        if ok:
            self.report({'INFO'}, "Done → " + dst)
        return {'FINISHED'} if ok else {'CANCELLED'}

    def _process_directory(self, props):
        in_dir = bpy.path.abspath(props.input_dir)
        if not os.path.isdir(in_dir):
            self.report({'ERROR'}, "Input directory not found: " + in_dir)
            return {'CANCELLED'}
        out_dir = bpy.path.abspath(props.output_dir) or in_dir
        os.makedirs(out_dir, exist_ok=True)
        glbs = [f for f in os.listdir(in_dir) if f.lower().endswith('.glb')]
        if not glbs:
            self.report({'WARNING'}, "No .glb files found in " + in_dir)
            return {'CANCELLED'}
        failures = 0
        for filename in glbs:
            src = os.path.join(in_dir, filename)
            name, ext = os.path.splitext(filename)
            dst = os.path.join(out_dir, f"{name}_compressed{ext}")
            if not self._run_pipeline(props, src, dst):
                failures += 1
        self.report(
            {'WARNING' if failures else 'INFO'},
            f"Processed {len(glbs)} file(s), {failures} failure(s). Output → {out_dir}",
        )
        return {'FINISHED'}

    def _build_steps(self, props):
        steps = []
        if props.use_resize:  steps.append('resize')
        if props.use_draco:   steps.append('draco')
        if props.use_ktx2:    steps.append('ktx2')
        elif props.use_webp:  steps.append('webp')
        return steps

    def _build_cmd(self, props, step, src, dst):
        cmd = ['gltf-transform', step, src, dst]
        if props.verbose:
            cmd.append('--verbose')
        if step == 'resize':
            cmd += ['--width', str(props.resize_width), '--height', str(props.resize_height)]
        elif step == 'draco':
            cmd += [
                '--quantize-position', str(props.draco_position_bits),
                '--quantize-normal', str(props.draco_normal_bits),
                '--quantize-color', str(props.draco_color_bits),
                '--quantize-texcoord', str(props.draco_uv_bits),
            ]
        elif step == 'ktx2':
            cmd += ['--codec', props.ktx2_codec]
            if props.ktx2_codec == 'etc1s':
                cmd += ['--quality', str(props.ktx2_quality)]
        elif step == 'webp':
            cmd += ['--quality', str(props.webp_quality)]
        return cmd

    def _run_pipeline(self, props, src, dst):
        steps = self._build_steps(props)
        if not steps:
            self.report({'WARNING'}, "No compression options selected — nothing to do.")
            return False
        tmp_files = []
        current_src = src
        try:
            for i, step in enumerate(steps):
                is_last = (i == len(steps) - 1)
                if is_last:
                    current_dst = dst
                else:
                    tmp = tempfile.NamedTemporaryFile(suffix='.glb', delete=False)
                    tmp.close()
                    tmp_files.append(tmp.name)
                    current_dst = tmp.name
                cmd = self._build_cmd(props, step, current_src, current_dst)
                print("▶ " + " ".join(cmd))
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, shell=(os.name == 'nt'))
                except FileNotFoundError:
                    self.report({'ERROR'}, "gltf-transform not found. Install: npm install -g @gltf-transform/cli")
                    return False
                if result.stdout: print(result.stdout)
                if result.stderr: print(result.stderr)
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "no output")[:300]
                    self.report({'ERROR'}, f"Step '{step}' failed (exit {result.returncode}): {detail}")
                    return False
                current_src = current_dst
        finally:
            for f in tmp_files:
                try:
                    os.remove(f)
                except OSError:
                    pass
        return True


class GLTF_OT_jsx(Operator):
    bl_idname = "gltf.jsx"
    bl_label = "Generate JSX"
    bl_description = "Run gltfjsx to generate React Three Fiber component(s)"

    def execute(self, context):
        props = context.scene.gltfjsx
        if props.process_dir:
            return self._process_directory(props)
        return self._process_single_file(props)

    def _process_single_file(self, props):
        src = bpy.path.abspath(props.input_file)
        if not src or not os.path.isfile(src):
            self.report({'ERROR'}, "Input file not found: " + src)
            return {'CANCELLED'}
        out_dir = bpy.path.abspath(props.output_dir) or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)
        name, _ = os.path.splitext(os.path.basename(src))
        ext = '.tsx' if props.types else '.jsx'
        dst = os.path.join(out_dir, name + ext)
        ok = self._run(props, src, dst)
        if ok:
            self.report({'INFO'}, "Done → " + dst)
        return {'FINISHED'} if ok else {'CANCELLED'}

    def _process_directory(self, props):
        in_dir = bpy.path.abspath(props.input_dir)
        if not os.path.isdir(in_dir):
            self.report({'ERROR'}, "Input directory not found: " + in_dir)
            return {'CANCELLED'}
        out_dir = bpy.path.abspath(props.output_dir) or in_dir
        os.makedirs(out_dir, exist_ok=True)
        glbs = [f for f in os.listdir(in_dir) if f.lower().endswith('.glb')]
        if not glbs:
            self.report({'WARNING'}, "No .glb files found in " + in_dir)
            return {'CANCELLED'}
        ext = '.tsx' if props.types else '.jsx'
        failures = 0
        for filename in glbs:
            src = os.path.join(in_dir, filename)
            name, _ = os.path.splitext(filename)
            dst = os.path.join(out_dir, name + ext)
            if not self._run(props, src, dst):
                failures += 1
        self.report(
            {'WARNING' if failures else 'INFO'},
            f"Processed {len(glbs)} file(s), {failures} failure(s). Output → {out_dir}",
        )
        return {'FINISHED'}

    def _build_cmd(self, props, src, dst):
        cmd = ['npx', 'gltfjsx', src, '--output', dst]
        if props.types:         cmd.append('--types')
        if props.keepnames:     cmd.append('--keepnames')
        if props.keepgroups:    cmd.append('--keepgroups')
        if props.bones:         cmd.append('--bones')
        if props.meta:          cmd.append('--meta')
        if props.shadows:       cmd.append('--shadows')
        if props.instance:      cmd.append('--instance')
        if props.instanceall:   cmd.append('--instanceall')
        if props.exportdefault: cmd.append('--exportdefault')
        if props.console:       cmd.append('--console')
        if props.debug:         cmd.append('--debug')
        cmd += ['--printwidth', str(props.printwidth)]
        cmd += ['--precision', str(props.precision)]
        draco = bpy.path.abspath(props.draco_path)
        if draco and os.path.exists(draco):
            cmd += ['--draco', draco]
        root = bpy.path.abspath(props.root)
        if root and os.path.isdir(root):
            cmd += ['--root', root]
        if props.transform:
            cmd.append('--transform')
            cmd += ['--resolution', str(props.resolution), '--format', props.tex_format]
            if props.keepmeshes:    cmd.append('--keepmeshes')
            if props.keepmaterials: cmd.append('--keepmaterials')
            if props.simplify:
                cmd.append('--simplify')
                cmd += ['--ratio', str(props.simplify_ratio), '--error', str(props.simplify_error)]
        return cmd

    def _run(self, props, src, dst):
        cmd = self._build_cmd(props, src, dst)
        print("▶ " + " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, shell=(os.name == 'nt'))
        except FileNotFoundError:
            self.report({'ERROR'}, "npx not found. Make sure Node.js is installed.")
            return False
        if result.stdout: print(result.stdout)
        if result.stderr: print(result.stderr)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no output")[:300]
            self.report({'ERROR'}, f"gltfjsx failed (exit {result.returncode}): {detail}")
            return False
        return True


# ===========================================================================
# IMAGE PREP — properties & operators
# ===========================================================================

KTX2_PRESETS = {
    'UASTC_MAX': {
        'label': "UASTC Max Quality",
        'suffix': "_uastc_max",
        'args': ["--genmipmap", "--uastc", "4", "--uastc_rdo_l", "0.25",
                 "--uastc_rdo_d", "65536", "--zcmp", "22"],
    },
    'UASTC_LOW': {
        'label': "UASTC Low Quality",
        'suffix': "_uastc_low",
        'args': ["--genmipmap", "--uastc", "0", "--uastc_rdo_l", "10.0",
                 "--uastc_rdo_d", "256", "--zcmp", "1"],
    },
    'ETC1S_MAX': {
        'label': "ETC1S Max Quality",
        'suffix': "_etc1s_max",
        'args': ["--genmipmap", "--bcmp", "--clevel", "5", "--qlevel", "255"],
    },
    'ETC1S_LOW': {
        'label': "ETC1S Low Quality",
        'suffix': "_etc1s_low",
        'args': ["--genmipmap", "--bcmp", "--clevel", "0", "--qlevel", "1"],
    },
}


def make_ktx2_operator(preset_key):
    preset = KTX2_PRESETS[preset_key]

    class _Op(Operator):
        bl_idname = f"png_conv.convert_ktx2_{preset_key.lower()}"
        bl_label = preset['label']
        bl_description = f"Convert all PNGs to KTX2 — {preset['label']}"

        def execute(self, context):
            props = context.scene.png_conv_props
            directory = bpy.path.abspath(props.directory)
            if not directory or not os.path.isdir(directory):
                self.report({'ERROR'}, f"Invalid directory: {directory}")
                return {'CANCELLED'}
            png_files = [f for f in os.listdir(directory) if f.lower().endswith('.png')]
            if not png_files:
                self.report({'WARNING'}, "No PNG files found in directory")
                return {'CANCELLED'}
            converted = 0
            failed = 0
            for filename in png_files:
                stem = os.path.splitext(filename)[0]
                input_path = os.path.join(directory, filename)
                output_path = os.path.join(directory, stem + preset['suffix'] + ".ktx2")
                cmd = ["toktx"] + preset['args'] + [output_path, input_path]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode == 0:
                        converted += 1
                    else:
                        self.report({'WARNING'}, f"Failed {filename}: {result.stderr.strip() or result.stdout.strip()}")
                        failed += 1
                except FileNotFoundError:
                    self.report({'ERROR'}, "toktx not found — make sure it's installed and on your PATH")
                    return {'CANCELLED'}
                except subprocess.TimeoutExpired:
                    self.report({'WARNING'}, f"Timeout: {filename}")
                    failed += 1
            self.report({'INFO'}, f"{preset['label']}: {converted} done, {failed} failed")
            return {'FINISHED'}

    _Op.__name__ = f"PNG_CONV_OT_ktx2_{preset_key.lower()}"
    return _Op


OpUastcMax = make_ktx2_operator('UASTC_MAX')
OpUastcLow = make_ktx2_operator('UASTC_LOW')
OpEtc1sMax = make_ktx2_operator('ETC1S_MAX')
OpEtc1sLow = make_ktx2_operator('ETC1S_LOW')


class PNG_CONV_OT_webp(Operator):
    bl_idname = "png_conv.convert_webp"
    bl_label = "Convert to WebP"
    bl_description = "Convert all PNGs in the directory to WebP using ImageMagick"

    def execute(self, context):
        props = context.scene.png_conv_props
        directory = bpy.path.abspath(props.directory)
        if not directory or not os.path.isdir(directory):
            self.report({'ERROR'}, f"Invalid directory: {directory}")
            return {'CANCELLED'}
        png_files = [f for f in os.listdir(directory) if f.lower().endswith('.png')]
        if not png_files:
            self.report({'WARNING'}, "No PNG files found in directory")
            return {'CANCELLED'}
        converted = 0
        failed = 0
        for filename in png_files:
            input_path = os.path.join(directory, filename)
            output_path = os.path.join(directory, os.path.splitext(filename)[0] + ".webp")
            cmd = ["magick", input_path]
            if props.resize != 100:
                cmd += ["-resize", f"{props.resize}%"]
            cmd += ["-quality", str(props.webp_quality), output_path]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    converted += 1
                else:
                    self.report({'WARNING'}, f"Failed: {filename} — {result.stderr.strip()}")
                    failed += 1
            except FileNotFoundError:
                self.report({'ERROR'}, "ImageMagick not found — make sure 'magick' is on your PATH")
                return {'CANCELLED'}
            except subprocess.TimeoutExpired:
                self.report({'WARNING'}, f"Timeout: {filename}")
                failed += 1
        self.report({'INFO'}, f"WebP: {converted} done, {failed} failed")
        return {'FINISHED'}


class PNG_CONV_Props(PropertyGroup):
    directory: StringProperty(name="Directory", default="", subtype='DIR_PATH')
    webp_quality: IntProperty(name="Quality", default=85, min=0, max=100)
    resize: IntProperty(name="Resize %", default=100, min=1, max=200)


# ===========================================================================
# Tab switcher property
# ===========================================================================

class HatchToolsSettings(PropertyGroup):
    active_tab: EnumProperty(
        name="Tab",
        items=[
            ('BAKING', "Baking", "Collection labeling, UV maps, grid duplicator"),
            ('CODE',   "Code",   "GLB compression and JSX generation"),
            ('IMAGE',  "Image",  "PNG to WebP / KTX2 batch conversion"),
        ],
        default='BAKING',
    )


# ===========================================================================
# Single panel with internal tab switching
# ===========================================================================

class HATCH_PT_main(Panel):
    bl_label = "Asset Prep Tools"
    bl_idname = "HATCH_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Asset Prep"

    def draw(self, context):
        layout = self.layout
        ht = context.scene.hatch_tools

        # Horizontal tab row
        row = layout.row(align=True)
        row.prop_enum(ht, "active_tab", 'BAKING')
        row.prop_enum(ht, "active_tab", 'CODE')
        row.prop_enum(ht, "active_tab", 'IMAGE')
        layout.separator()

        if ht.active_tab == 'BAKING':
            self._draw_baking(layout, context)
        elif ht.active_tab == 'CODE':
            self._draw_code(layout, context)
        elif ht.active_tab == 'IMAGE':
            self._draw_image(layout, context)

    # ---- Baking Prep --------------------------------------------------------

    def _draw_baking(self, layout, context):
        settings = context.scene.coltag_settings

        box = layout.box()
        box.label(text="ID Settings", icon="SETTINGS")
        box.prop(settings, "id_prefix")
        box.prop(settings, "start_index")
        box.prop(settings, "extra_tag")
        box.prop(settings, "strip_old_prefix")

        box2 = layout.box()
        box2.label(text="Preview", icon="HIDE_OFF")
        prefix = settings.id_prefix.strip() or "ID"
        tag = ("_" + settings.extra_tag.strip()) if settings.extra_tag.strip() else ""
        box2.label(text=f"{prefix}_{settings.start_index}{tag}_YourCollectionName",
                   icon="COLLECTION_COLOR_01")

        layout.separator()
        layout.operator("coltag.label_collections", icon="OUTLINER_COLLECTION")
        layout.operator("coltag.clear_labels", icon="X")

        layout.separator()
        box3 = layout.box()
        box3.label(text="Propagate to Objects", icon="OBJECT_DATA")
        box3.prop(settings, "apply_to_mesh_data")
        box3.operator("coltag.propagate_names", icon="MESH_DATA")

        layout.separator()
        box_uv = layout.box()
        box_uv.label(text="UV Map", icon="UV")
        box_uv.prop(settings, "uv_map_name")
        row = box_uv.row(align=True)
        row.prop(settings, "uv_set_active", toggle=True, icon="LAYER_ACTIVE")
        row.prop(settings, "uv_set_render", toggle=True, icon="RESTRICT_RENDER_OFF")
        box_uv.operator("coltag.add_uv_map", icon="ADD")

        layout.separator()
        box4 = layout.box()
        box4.label(text="Grid Duplicator", icon="GRID")
        box4.prop(settings, "grid_spacing")
        box4.prop(settings, "grid_columns")
        box4.prop(settings, "grid_link_data")
        n = len(context.selected_objects)
        if n > 0:
            cols = settings.grid_columns if settings.grid_columns > 0 else math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
            box4.label(text=f"{n} selected  ->  {cols} x {rows} grid", icon="INFO")
        else:
            box4.label(text="Select objects in the viewport", icon="INFO")
        box4.operator("coltag.grid_duplicate", icon="DUPLICATE")
        box4.operator("coltag.snap_back", icon="LOOP_BACK")

        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 0.7
        col.label(text="Ctrl/Shift-click collections", icon="INFO")
        col.label(text="in the Outliner to multi-select.")

    # ---- Code Prep ----------------------------------------------------------

    def _draw_code(self, layout, context):
        glb = context.scene.glb_compressor
        jsx = context.scene.gltfjsx

        # GLB Compressor section
        layout.label(text="GLB Compressor (gltf-transform)", icon='MESH_DATA')

        box = layout.box()
        box.label(text="Input", icon='IMPORT')
        box.prop(glb, 'process_dir')
        box.prop(glb, 'input_dir' if glb.process_dir else 'input_file')

        box = layout.box()
        box.label(text="Output Directory", icon='EXPORT')
        box.prop(glb, 'output_dir', text="")

        box = layout.box()
        box.prop(glb, 'use_resize', icon='IMAGE_PLANE')
        if glb.use_resize:
            row = box.row(align=True)
            row.prop(glb, 'resize_width')
            row.prop(glb, 'resize_height')

        box = layout.box()
        box.prop(glb, 'use_draco', icon='MESH_DATA')
        if glb.use_draco:
            col = box.column(align=True)
            col.prop(glb, 'draco_position_bits')
            col.prop(glb, 'draco_normal_bits')
            col.prop(glb, 'draco_color_bits')
            col.prop(glb, 'draco_uv_bits')

        box = layout.box()
        box.prop(glb, 'use_ktx2', icon='TEXTURE')
        if glb.use_ktx2:
            box.prop(glb, 'ktx2_codec')
            if glb.ktx2_codec == 'etc1s':
                box.prop(glb, 'ktx2_quality')

        box = layout.box()
        row = box.row()
        row.enabled = not glb.use_ktx2
        row.prop(glb, 'use_webp', icon='IMAGE_DATA')
        if glb.use_webp and not glb.use_ktx2:
            box.prop(glb, 'webp_quality')
        elif glb.use_ktx2:
            box.label(text="(disabled — KTX2 active)", icon='INFO')

        layout.prop(glb, 'verbose', icon='CONSOLE')
        layout.operator('gltf.compress', text="Compress GLB(s)", icon='PLAY')

        layout.separator()

        # gltfjsx section
        layout.label(text="gltfjsx — React Three Fiber", icon='FILE_SCRIPT')

        box = layout.box()
        box.label(text="Input", icon='IMPORT')
        box.prop(jsx, 'process_dir')
        box.prop(jsx, 'input_dir' if jsx.process_dir else 'input_file')

        box = layout.box()
        box.label(text="Output Directory", icon='EXPORT')
        box.prop(jsx, 'output_dir', text="")

        box = layout.box()
        box.label(text="Output Options", icon='FILE_SCRIPT')
        col = box.column(align=False)
        for prop in ('types', 'keepnames', 'keepgroups', 'bones', 'meta',
                     'shadows', 'instance', 'instanceall', 'exportdefault', 'console', 'debug'):
            col.prop(jsx, prop)
        box.prop(jsx, 'printwidth')
        box.prop(jsx, 'precision')

        box = layout.box()
        box.label(text="Path Overrides", icon='FILE_FOLDER')
        box.prop(jsx, 'draco_path')
        box.prop(jsx, 'root')

        box = layout.box()
        box.prop(jsx, 'transform', icon='MODIFIER')
        if jsx.transform:
            box.prop(jsx, 'resolution')
            box.prop(jsx, 'tex_format')
            box.prop(jsx, 'keepmeshes')
            box.prop(jsx, 'keepmaterials')
            box.prop(jsx, 'simplify')
            if jsx.simplify:
                sub = box.column(align=True)
                sub.prop(jsx, 'simplify_ratio')
                sub.prop(jsx, 'simplify_error')

        layout.operator('gltf.jsx', text="Generate JSX", icon='PLAY')

    # ---- Image Prep ---------------------------------------------------------

    def _draw_image(self, layout, context):
        props = context.scene.png_conv_props

        layout.prop(props, "directory")
        layout.separator()

        box = layout.box()
        box.label(text="WebP  (ImageMagick)", icon='IMAGE_DATA')
        col = box.column(align=True)
        col.prop(props, "webp_quality", slider=True)
        col.prop(props, "resize", slider=True)
        box.operator("png_conv.convert_webp", icon='EXPORT')

        layout.separator()

        box = layout.box()
        box.label(text="KTX2 — UASTC  (toktx)", icon='IMAGE_DATA')
        box.label(text="GPU-uncompressed, high quality", icon='INFO')
        col = box.column(align=True)
        col.operator(OpUastcMax.bl_idname)
        col.operator(OpUastcLow.bl_idname)

        layout.separator()

        box = layout.box()
        box.label(text="KTX2 — ETC1S  (toktx)", icon='IMAGE_DATA')
        box.label(text="Block-compressed, smallest files", icon='INFO')
        col = box.column(align=True)
        col.operator(OpEtc1sMax.bl_idname)
        col.operator(OpEtc1sLow.bl_idname)


# ===========================================================================
# Registration
# ===========================================================================

classes = (
    # Properties
    HatchToolsSettings,
    CollectionTaggerSettings,
    GLBCompressorProps,
    GLTFJSXProps,
    PNG_CONV_Props,
    # Operators — Baking Prep
    COLTAG_OT_label_collections,
    COLTAG_OT_propagate_names,
    COLTAG_OT_clear_labels,
    COLTAG_OT_grid_duplicate,
    COLTAG_OT_snap_back,
    COLTAG_OT_add_uv_map,
    # Operators — Code Prep
    GLTF_OT_compress,
    GLTF_OT_jsx,
    # Operators — Image Prep
    PNG_CONV_OT_webp,
    OpUastcMax,
    OpUastcLow,
    OpEtc1sMax,
    OpEtc1sLow,
    # Panel
    HATCH_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.hatch_tools = PointerProperty(type=HatchToolsSettings)
    bpy.types.Scene.coltag_settings = PointerProperty(type=CollectionTaggerSettings)
    bpy.types.Scene.glb_compressor = PointerProperty(type=GLBCompressorProps)
    bpy.types.Scene.gltfjsx = PointerProperty(type=GLTFJSXProps)
    bpy.types.Scene.png_conv_props = PointerProperty(type=PNG_CONV_Props)


def unregister():
    del bpy.types.Scene.png_conv_props
    del bpy.types.Scene.gltfjsx
    del bpy.types.Scene.glb_compressor
    del bpy.types.Scene.coltag_settings
    del bpy.types.Scene.hatch_tools
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
