bl_info = {
    "name": "One Shot Baking",
    "author": "Claude",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > One Shot Bake",
    "description": "Per collection: atlas unwrap, pack islands, bake and save one texture",
    "category": "Object",
}

import os
import math
import tempfile
import bpy
from mathutils import Vector
from contextlib import contextmanager
from bpy.props import (
    StringProperty, IntProperty, BoolProperty, FloatProperty,
    EnumProperty, PointerProperty, CollectionProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


# ===========================================================================
# helpers
# ===========================================================================

# Bake types whose data is not colour and must not get an sRGB transform.
NON_COLOR_BAKES = {"NORMAL", "ROUGHNESS", "POSITION", "UV", "AO"}

# UVPackmaster's packing mode for the 0..1 tile, which is the only one that
# makes sense when the whole point is a single baked image.
UVPM_SINGLE_TILE_MODE = "pack.single_tile"

FORMAT_EXT = {
    "PNG": ".png",
    "OPEN_EXR": ".exr",
    "TARGA": ".tga",
    "JPEG": ".jpg",
    "TIFF": ".tif",
}


def supported_kwargs(op, kwargs):
    """Drop any kwargs this Blender build's operator does not define.

    Signatures for uv.smart_project and uv.pack_islands have shifted between
    releases, so we filter rather than hardcode a version.
    """
    try:
        known = op.get_rna_type().properties.keys()
    except Exception:
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in known}


def find_layer_collection(layer_col, collection):
    if layer_col.collection == collection:
        return layer_col
    for child in layer_col.children:
        found = find_layer_collection(child, collection)
        if found:
            return found
    return None


def collection_meshes(collection, include_children):
    source = collection.all_objects if include_children else collection.objects
    return [o for o in source if o.type == "MESH" and len(o.data.polygons) > 0]


class VisibilityGuard:
    """Temporarily reveal collections and objects so they can be selected.

    Excluded or hidden objects raise on select_set(), which would otherwise
    fail the whole bake for a reason that is not obvious from the error.
    """

    def __init__(self):
        self._restore = []

    def reveal_collection(self, view_layer, collection):
        if collection.hide_viewport:
            self._restore.append((collection, "hide_viewport", True))
            collection.hide_viewport = False

        layer_col = find_layer_collection(view_layer.layer_collection, collection)
        if layer_col is None:
            return
        if layer_col.exclude:
            self._restore.append((layer_col, "exclude", True))
            layer_col.exclude = False
        if layer_col.hide_viewport:
            self._restore.append((layer_col, "hide_viewport", True))
            layer_col.hide_viewport = False

    def reveal_object(self, obj):
        if obj.hide_viewport:
            self._restore.append((obj, "hide_viewport", True))
            obj.hide_viewport = False
        try:
            if obj.hide_get():
                self._restore.append((obj, "_hide_set", True))
                obj.hide_set(False)
        except RuntimeError:
            pass

    def restore(self):
        for target, attr, value in reversed(self._restore):
            try:
                if attr == "_hide_set":
                    target.hide_set(value)
                else:
                    setattr(target, attr, value)
            except (RuntimeError, ReferenceError):
                pass
        self._restore.clear()


@contextmanager
def uv_editor_context(context):
    """Yield a context override with a real UV editor.

    uv.pack_islands polls for a UV editor, so calling it straight from the
    3D viewport fails. Borrow an area, then put it back.
    """
    window = context.window
    screen = window.screen

    area = next((a for a in screen.areas if a.type == "IMAGE_EDITOR"), None)
    borrowed = False

    if area is None:
        candidates = [a for a in screen.areas if a.type == "VIEW_3D"] or list(screen.areas)
        if not candidates:
            raise RuntimeError("No usable editor area to run UV packing in")
        area = max(candidates, key=lambda a: a.width * a.height)
        old_type = area.type
        area.type = "IMAGE_EDITOR"
        borrowed = True

    space = area.spaces.active
    old_mode = space.mode
    space.mode = "UV"
    region = next((r for r in area.regions if r.type == "WINDOW"), None)

    try:
        with context.temp_override(window=window, area=area, region=region):
            yield
    finally:
        try:
            space.mode = old_mode
        except (RuntimeError, ReferenceError):
            pass
        if borrowed:
            try:
                area.type = old_type
            except (RuntimeError, ReferenceError):
                pass


def uvpackmaster_available(context):
    """UVPackmaster is optional, so every failure has to degrade gracefully."""
    if "uvpackmaster3" not in dir(bpy.ops):
        return False, "UVPackmaster 3 is not installed"
    if not hasattr(context.scene, "uvpm3_props"):
        return False, "UVPackmaster 3 properties are missing"
    try:
        # Its poll checks only that the packing engine has been initialised,
        # which is the usual reason a scripted pack call fails.
        if not bpy.ops.uvpackmaster3.pack.poll():
            return False, "UVPackmaster 3 engine is not initialised (check its preferences)"
    except Exception as ex:
        return False, f"UVPackmaster 3 unavailable ({ex})"
    return True, None


def pack_with_uvpackmaster(context, props):
    """Drive UVPackmaster's packer, then hand its settings back untouched."""
    main = context.scene.uvpm3_props.default_main_props

    wanted = {
        # We bake one image, so force single tile. Any multi tile mode would
        # scatter islands across UDIMs and most of them would miss the bake.
        "active_main_mode_id": UVPM_SINGLE_TILE_MODE,
        "margin": props.uvpm_margin,
        "rotation_enable": props.uvpm_rotation_enable,
        "rotation_step": props.uvpm_rotation_step,
        "precision": props.uvpm_precision,
        "pixel_margin_enable": props.uvpm_use_pixel_margin,
        "pixel_margin": props.uvpm_pixel_margin,
        # Pixel margin only means anything against a texture size, and we know it.
        "pixel_margin_tex_size": props.resolution_x,
        "heuristic_enable": props.uvpm_heuristic_enable,
        "heuristic_search_time": props.uvpm_heuristic_time,
        "normalize_scale": props.uvpm_normalize_scale,
    }

    saved = {}
    for key, value in wanted.items():
        if not hasattr(main, key):
            continue
        saved[key] = getattr(main, key)
        try:
            setattr(main, key, value)
        except (TypeError, AttributeError, ValueError):
            saved.pop(key, None)

    # An empty mode_id leaves the operator without a scenario and it aborts,
    # so it has to be passed explicitly even though the mode is also set above.
    mode_id = getattr(main, "active_main_mode_id", UVPM_SINGLE_TILE_MODE)

    try:
        with uv_editor_context(context):
            bpy.ops.uv.select_all(action="SELECT")
        # EXEC_DEFAULT keeps the pack synchronous. Called any other way it goes
        # modal and would return before the islands have actually moved.
        bpy.ops.uvpackmaster3.pack("EXEC_DEFAULT", mode_id=mode_id)
    finally:
        for key, value in saved.items():
            try:
                setattr(main, key, value)
            except (TypeError, AttributeError, ValueError):
                pass


def ensure_uv_map(obj, name, overwrite):
    """Create (or reuse) the bake UV map and make it the active/highlighted one."""
    mesh = obj.data
    layer = mesh.uv_layers.get(name)

    if layer is not None and overwrite:
        mesh.uv_layers.remove(layer)
        layer = None

    if layer is None:
        if len(mesh.uv_layers) >= 8:
            raise RuntimeError(f"'{obj.name}' already has 8 UV maps, cannot add another")
        layer = mesh.uv_layers.new(name=name)

    mesh.uv_layers.active = layer
    layer.active_render = True
    return layer


def _polygon_area(points):
    """Newell's method, so n-gons are handled as well as triangles."""
    if len(points) < 3:
        return 0.0
    normal = Vector((0.0, 0.0, 0.0))
    for i in range(len(points)):
        normal += points[i].cross(points[(i + 1) % len(points)])
    return normal.length * 0.5


def normalize_texel_density(objects, uv_name):
    """Scale each object's UVs to match its real world size.

    Smart UV Project works in local space, so an object with unapplied scale
    gets the same share of the atlas as an unscaled one and ends up blurrier.
    Blender's own uv.average_islands_scale does not correct for this either,
    so we rescale per object before packing. Packing renormalises everything
    into 0..1 afterwards, preserving these relative proportions.
    """
    for obj in objects:
        mesh = obj.data
        layer = mesh.uv_layers.get(uv_name)
        if layer is None:
            continue

        local_area = sum(p.area for p in mesh.polygons)
        if local_area <= 0.0:
            continue

        matrix = obj.matrix_world
        world_area = 0.0
        for poly in mesh.polygons:
            world_area += _polygon_area([matrix @ mesh.vertices[i].co for i in poly.vertices])
        if world_area <= 0.0:
            continue

        factor = math.sqrt(world_area / local_area)
        if abs(factor - 1.0) < 1e-6:
            continue

        for datum in layer.data:
            datum.uv = (datum.uv[0] * factor, datum.uv[1] * factor)


def fit_uvs_to_unit_square(objects, uv_name):
    """Bring every object's UVs back inside 0..1 with a single shared transform.

    Density normalisation multiplies UVs by an object's world scale, which can
    push them across many UDIM tiles. Packing would then spread islands over
    those tiles instead of the one square we are baking into. Scaling everything
    by the same factor keeps the relative densities intact.
    """
    layers = []
    min_u = min_v = float("inf")
    max_u = max_v = float("-inf")

    for obj in objects:
        layer = obj.data.uv_layers.get(uv_name)
        if layer is None or len(layer.data) == 0:
            continue
        layers.append(layer)
        for datum in layer.data:
            u, v = datum.uv
            min_u = min(min_u, u); max_u = max(max_u, u)
            min_v = min(min_v, v); max_v = max(max_v, v)

    if not layers or min_u > max_u:
        return

    span = max(max_u - min_u, max_v - min_v)
    if span <= 0.0:
        return

    scale = 1.0 / span
    for layer in layers:
        for datum in layer.data:
            datum.uv = ((datum.uv[0] - min_u) * scale, (datum.uv[1] - min_v) * scale)


def make_bake_image(props, collection_name):
    name = props.image_name_pattern.replace("{collection}", collection_name)
    name = name.replace("{type}", props.bake_type.lower())

    existing = bpy.data.images.get(name)
    if existing is not None:
        if props.replace_existing_image:
            bpy.data.images.remove(existing)
        else:
            return existing, name

    image = bpy.data.images.new(
        name=name,
        width=props.resolution_x,
        height=props.resolution_y,
        alpha=props.use_alpha,
        float_buffer=props.use_float,
    )
    if props.bake_type in NON_COLOR_BAKES:
        try:
            image.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
    return image, name


class BakeTargetNodes:
    """Adds an active image texture node to every material, and takes it back out."""

    def __init__(self):
        self._created = []
        self._prev_active = []
        self._enabled_nodes = []

    def attach(self, objects, image):
        materials = []
        for obj in objects:
            for slot in obj.material_slots:
                if slot.material is not None and slot.material not in materials:
                    materials.append(slot.material)

        for mat in materials:
            if not mat.use_nodes:
                mat.use_nodes = True
                self._enabled_nodes.append(mat)

            tree = mat.node_tree
            node = tree.nodes.new("ShaderNodeTexImage")
            node.image = image
            node.label = "One Shot Bake Target"
            node.location = (
                min((n.location.x for n in tree.nodes), default=0.0) - 340.0,
                max((n.location.y for n in tree.nodes), default=0.0) + 320.0,
            )

            self._prev_active.append((tree, tree.nodes.active))
            for other in tree.nodes:
                other.select = False
            node.select = True
            tree.nodes.active = node
            self._created.append((tree, node))

        return len(materials)

    def detach(self, remove_nodes):
        if remove_nodes:
            for tree, node in self._created:
                try:
                    tree.nodes.remove(node)
                except (RuntimeError, ReferenceError):
                    pass

        for tree, node in self._prev_active:
            try:
                tree.nodes.active = node
            except (RuntimeError, ReferenceError):
                pass

        for mat in self._enabled_nodes:
            try:
                mat.use_nodes = False
            except (RuntimeError, ReferenceError):
                pass

        self._created.clear()
        self._prev_active.clear()
        self._enabled_nodes.clear()


def _build_denoise_scene(image, source_scene, non_color):
    """A throwaway scene whose only job is to run the compositor once."""
    temp = bpy.data.scenes.new("OSB_denoise")
    temp.render.engine = "BLENDER_WORKBENCH"   # nothing is rendered, keep it cheap
    temp.render.resolution_x = image.size[0]
    temp.render.resolution_y = image.size[1]
    temp.render.resolution_percentage = 100
    temp.render.use_compositing = True
    temp.render.use_sequencer = False
    temp.render.film_transparent = True

    settings = temp.render.image_settings
    settings.file_format = "PNG"
    settings.color_mode = "RGBA"
    settings.color_depth = "16"

    view = temp.view_settings
    try:
        if non_color:
            # Data maps must come back untouched, no view transform.
            view.view_transform = "Standard"
            view.look = "None"
        else:
            source = source_scene.view_settings
            view.view_transform = source.view_transform
            view.look = source.look
            view.exposure = source.exposure
            view.gamma = source.gamma
            temp.display_settings.display_device = source_scene.display_settings.display_device
    except TypeError:
        # An unavailable transform name should not sink the whole bake.
        view.view_transform = "Standard"

    return temp


def denoise_image_with_compositor(image, source_scene, non_color):
    """Run a baked image through the compositor's Denoise node, in place.

    Saving the render applies the scene's colour management on the way out, so
    for colour maps the view transform ends up baked into the texture. That is
    what you want for engines that will not apply one themselves. Data maps go
    through Standard so their values survive untouched.

    Blender 5.x replaced scene.node_tree with a compositing node group ending in
    a Group Output, so there is no Composite node to reach for here.
    """
    group = bpy.data.node_groups.new("OSB_denoise_tree", "CompositorNodeTree")
    temp = None
    loaded = None
    out_path = None

    try:
        group.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")

        source_node = group.nodes.new("CompositorNodeImage")
        source_node.image = image
        denoise_node = group.nodes.new("CompositorNodeDenoise")
        output_node = group.nodes.new("NodeGroupOutput")

        group.links.new(source_node.outputs["Image"], denoise_node.inputs["Image"])
        group.links.new(denoise_node.outputs["Image"], output_node.inputs[0])

        temp = _build_denoise_scene(image, source_scene, non_color)
        temp.compositing_node_group = group

        bpy.ops.render.render(write_still=False, use_viewport=False, scene=temp.name)

        render_result = bpy.data.images.get("Render Result")
        if render_result is None:
            return False, "no render result"

        # Render Result holds no readable pixel buffer, so it has to go via disk.
        handle, out_path = tempfile.mkstemp(suffix=".png", prefix="osb_denoise_")
        os.close(handle)
        render_result.save_render(out_path, scene=temp)

        loaded = bpy.data.images.load(out_path)
        if tuple(loaded.size) != tuple(image.size):
            return False, f"size mismatch {tuple(loaded.size)} vs {tuple(image.size)}"

        # save_render wrote an sRGB encoded file, so it has to be read back as
        # sRGB to undo that encoding. Matching the target's space instead would
        # leave data maps gamma encoded, since Non-Color skips the decode.
        loaded.colorspace_settings.name = "sRGB"

        count = image.size[0] * image.size[1] * image.channels
        buffer = [0.0] * count
        loaded.pixels.foreach_get(buffer)
        image.pixels.foreach_set(buffer)
        image.update()
        return True, None

    finally:
        if loaded is not None:
            bpy.data.images.remove(loaded)
        if temp is not None:
            bpy.data.scenes.remove(temp)
        bpy.data.node_groups.remove(group)
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def make_flat_baked_material(name, image, uv_name):
    """A material that is nothing but the baked texture wired to the output.

    No BSDF in between, so what you see is exactly the atlas. An explicit UV Map
    node avoids depending on whichever UV layer happens to be active later.
    """
    existing = bpy.data.materials.get(name)
    if existing is not None:
        bpy.data.materials.remove(existing)

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()

    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (300.0, 0.0)

    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.location = (0.0, 0.0)

    uv_node = tree.nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = uv_name
    uv_node.location = (-300.0, 0.0)

    tree.links.new(uv_node.outputs["UV"], tex.inputs["Vector"])
    tree.links.new(tex.outputs["Color"], output.inputs["Surface"])
    return mat


def exclude_collection(view_layer, collection):
    """Untick the collection in the outliner so only the baked copies show.

    Re-running the bake un-ticks it again via VisibilityGuard, so this does not
    lock the originals out of a later pass.
    """
    layer_collection = find_layer_collection(view_layer.layer_collection, collection)
    if layer_collection is None:
        return False
    layer_collection.exclude = True
    return True


def duplicate_with_baked_material(context, objects, image, uv_name, collection_name):
    """Copy the baked meshes into a sibling collection using the flat material."""
    target_name = collection_name + "_baked"

    target = bpy.data.collections.get(target_name)
    if target is None:
        target = bpy.data.collections.new(target_name)
        context.scene.collection.children.link(target)
    else:
        # Clear a previous run's output rather than piling up duplicates.
        for old in list(target.objects):
            mesh = old.data if old.type == "MESH" else None
            bpy.data.objects.remove(old, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)

    material = make_flat_baked_material(target_name + "_mat", image, uv_name)

    made = 0
    for obj in objects:
        copy = obj.copy()
        copy.data = obj.data.copy()
        copy.name = obj.name + "_baked"

        # Object-linked slots would survive clearing the mesh's materials.
        for slot in copy.material_slots:
            slot.link = "DATA"

        copy.data.materials.clear()
        copy.data.materials.append(material)
        for poly in copy.data.polygons:
            poly.material_index = 0

        layer = copy.data.uv_layers.get(uv_name)
        if layer is not None:
            copy.data.uv_layers.active = layer
            layer.active_render = True

        target.objects.link(copy)
        made += 1

    return target, made


def image_looks_blank(image, samples=2000):
    """Sparse check for an image that never actually received a bake.

    Indexes pixels directly rather than slicing, because a full copy of a 4K
    buffer is 268MB and this runs once per collection.
    """
    width, height = image.size
    if width == 0 or height == 0:
        return True

    pixels = image.pixels
    total = width * height
    step = max(1, total // samples)

    for index in range(0, total, step):
        base = index * 4
        if pixels[base] > 0.0 or pixels[base + 1] > 0.0 or pixels[base + 2] > 0.0:
            return False
    return True


def save_image_as_webp(image, directory, quality):
    """Write a WebP copy of the baked image and return its path.

    save_copy keeps the datablock pointed at whatever the normal save produced,
    so the WebP is an extra artefact rather than a replacement.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, image.name + ".webp")

    previous_format = image.file_format
    try:
        image.file_format = "WEBP"
        image.save(filepath=path, quality=quality, save_copy=True)
    finally:
        image.file_format = previous_format
    return path


def load_webp_image(path, source_image):
    """Load the compressed file back so materials can reference it directly."""
    name = os.path.basename(path)
    existing = bpy.data.images.get(name)
    if existing is not None:
        bpy.data.images.remove(existing)

    loaded = bpy.data.images.load(path, check_existing=False)
    # A plain save writes the buffer through the image's own colour space, so
    # matching it here round-trips correctly. This is not the denoise case,
    # which went through save_render and its view transform.
    loaded.colorspace_settings.name = source_image.colorspace_settings.name
    return loaded


def save_image(image, props, collection_name):
    directory = bpy.path.abspath(props.output_dir)
    if not directory:
        return None

    os.makedirs(directory, exist_ok=True)
    ext = FORMAT_EXT.get(props.file_format, ".png")
    path = os.path.join(directory, image.name + ext)

    image.filepath_raw = path
    image.file_format = props.file_format
    image.save()
    return path


# ===========================================================================
# properties
# ===========================================================================

class OSB_CollectionItem(PropertyGroup):
    collection: PointerProperty(name="Collection", type=bpy.types.Collection)


class OSB_Settings(PropertyGroup):
    collections: CollectionProperty(type=OSB_CollectionItem)
    collections_index: IntProperty(default=0)

    include_children: BoolProperty(
        name="Include Child Collections", default=True,
        description="Also bake meshes inside nested collections",
    )

    # UV
    uv_map_name: StringProperty(
        name="UV Map Name", default="OneShotBake",
        description="Name of the UV map created for baking, set active (blue highlight)",
    )
    overwrite_uv_map: BoolProperty(
        name="Rebuild If Exists", default=True,
        description="Delete and recreate the bake UV map if one already exists",
    )
    angle_limit: FloatProperty(
        name="Angle Limit", default=1.15192, min=0.0, max=1.5708,
        subtype="ANGLE", description="Smart UV Project angle limit",
    )
    island_margin: FloatProperty(
        name="Unwrap Margin", default=0.0, min=0.0, max=1.0,
        description="Smart UV Project island margin. Packing runs afterwards, so 0 is usually fine",
    )
    correct_aspect: BoolProperty(name="Correct Aspect", default=True)
    scale_to_bounds: BoolProperty(name="Scale To Bounds", default=False)
    even_texel_density: BoolProperty(
        name="Even Texel Density", default=True,
        description=(
            "Give each object a share of the atlas proportional to its real world size. "
            "Required for objects with unapplied scale, which Smart UV Project ignores"
        ),
    )

    # Packing
    pack_margin: FloatProperty(
        name="Pack Margin", default=0.01, min=0.0, max=1.0,
        description="Space between islands after packing",
    )
    pack_rotate: BoolProperty(name="Rotate", default=True)
    pack_scale: BoolProperty(name="Scale", default=True)
    pack_merge_overlap: BoolProperty(name="Merge Overlapping", default=False)
    pack_shape_method: EnumProperty(
        name="Shape Method", default="CONCAVE",
        items=[
            ("CONCAVE", "Exact Shape (Concave)", "Tightest packing, slowest"),
            ("CONVEX", "Boundary Shape (Convex)", "Faster, slightly looser"),
            ("AABB", "Bounding Box", "Fastest, loosest"),
        ],
    )

    # UVPackmaster 3
    use_uvpackmaster: BoolProperty(
        name="Pack With UVPackmaster 3", default=False,
        description=(
            "Use UVPackmaster 3's packer instead of Blender's. Falls back to Blender's "
            "packer with a warning if UVPackmaster is missing or its engine is not set up"
        ),
    )
    uvpm_margin: FloatProperty(
        name="Margin", default=0.003, min=0.0, max=1.0, precision=4,
        description="UVPackmaster island margin, ignored when pixel margin is on",
    )
    uvpm_use_pixel_margin: BoolProperty(
        name="Pixel Margin", default=True,
        description="Set the margin in pixels against the bake resolution instead of UV units",
    )
    uvpm_pixel_margin: IntProperty(name="Pixels", default=8, min=0, max=256)
    uvpm_rotation_enable: BoolProperty(name="Rotation", default=True)
    uvpm_rotation_step: IntProperty(
        name="Rotation Step", default=90, min=1, max=180,
        description="Smaller steps pack tighter but take longer",
    )
    uvpm_precision: IntProperty(
        name="Precision", default=500, min=10, max=10000,
        description="Higher is tighter and slower",
    )
    uvpm_heuristic_enable: BoolProperty(
        name="Heuristic Search", default=False,
        description="Keep retrying for a better result until the time below runs out",
    )
    uvpm_heuristic_time: IntProperty(
        name="Search Seconds", default=5, min=0, max=600,
        description="0 means search until it stops improving",
    )
    uvpm_normalize_scale: BoolProperty(
        name="Normalize Scale", default=False,
        description=(
            "Let UVPackmaster equalise island scale. Leave off when Even Texel Density "
            "is on, since they would fight over the same thing"
        ),
    )

    # Image
    resolution_x: IntProperty(name="Width", default=2048, min=1, max=16384)
    resolution_y: IntProperty(name="Height", default=2048, min=1, max=16384)
    use_alpha: BoolProperty(name="Alpha", default=False)
    use_float: BoolProperty(name="32 Bit Float", default=False)
    image_name_pattern: StringProperty(
        name="Image Name", default="{collection}_{type}",
        description="Supports {collection} and {type}",
    )
    replace_existing_image: BoolProperty(
        name="Replace Existing Image", default=True,
        description="Reuse the name instead of piling up .001 duplicates",
    )

    # Bake
    bake_type: EnumProperty(
        name="Bake Type", default="DIFFUSE",
        items=[
            ("DIFFUSE", "Diffuse (Base Color)", ""),
            ("COMBINED", "Combined", ""),
            ("EMIT", "Emission", ""),
            ("NORMAL", "Normal", ""),
            ("ROUGHNESS", "Roughness", ""),
            ("AO", "Ambient Occlusion", ""),
            ("SHADOW", "Shadow", ""),
            ("POSITION", "Position", ""),
        ],
    )
    samples: IntProperty(name="Samples", default=32, min=1, max=16384)
    bake_margin: IntProperty(name="Bake Margin", default=16, min=0, max=256)
    device: EnumProperty(
        name="Device", default="GPU",
        items=[("CPU", "CPU", ""), ("GPU", "GPU", "")],
    )

    # Output
    output_dir: StringProperty(
        name="Output Folder", subtype="DIR_PATH", default="",
        description="Where to save baked textures. Leave empty to keep them in the blend only",
    )
    file_format: EnumProperty(
        name="Format", default="PNG",
        items=[
            ("PNG", "PNG", ""),
            ("JPEG", "JPEG", ""),
            ("TARGA", "Targa", ""),
            ("TIFF", "TIFF", ""),
            ("OPEN_EXR", "OpenEXR", ""),
        ],
    )
    denoise_with_compositor: BoolProperty(
        name="Denoise With Compositor", default=False,
        description=(
            "Run the baked image through the compositor's Denoise node. Colour maps come "
            "back with the scene's view transform applied; data maps go through Standard "
            "so their values are left alone. Runs before the baked duplicates are made"
        ),
    )
    make_baked_copies: BoolProperty(
        name="Duplicate With Baked Material", default=False,
        description=(
            "After baking, copy the meshes into a '<collection>_baked' collection and give "
            "them one material with the baked image wired straight to the Material Output"
        ),
    )
    compress_to_webp: BoolProperty(
        name="Compress To WebP", default=False,
        description=(
            "Also write a WebP copy after denoising, and point the baked duplicates at it. "
            "Needs an output folder, since compression happens on the saved file"
        ),
    )
    webp_quality: IntProperty(
        name="WebP Quality", default=85, min=0, max=100, subtype="PERCENTAGE",
        description="100 is lossless. 80-90 is usually indistinguishable at a fraction of the size",
    )
    hide_original_collection: BoolProperty(
        name="Untick Original Collection", default=True,
        description=(
            "After duplicating, untick the source collection in the outliner so only "
            "the baked copies are visible. Baking it again re-enables it automatically"
        ),
    )
    cleanup_nodes: BoolProperty(
        name="Remove Bake Nodes After", default=True,
        description="Delete the image texture nodes this addon adds once baking finishes",
    )


# ===========================================================================
# collection list operators
# ===========================================================================

class OSB_UL_collections(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        if item.collection:
            layout.prop(item, "collection", text="", emboss=False, icon="OUTLINER_COLLECTION")
        else:
            layout.prop(item, "collection", text="", icon="OUTLINER_COLLECTION")


class OSB_OT_add_collection(Operator):
    bl_idname = "osb.add_collection"
    bl_label = "Add Collection Slot"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.one_shot_bake
        item = props.collections.add()
        active = context.view_layer.active_layer_collection
        if active and active.collection and active.collection != context.scene.collection:
            already = {i.collection for i in props.collections}
            if active.collection not in already:
                item.collection = active.collection
        props.collections_index = len(props.collections) - 1
        return {"FINISHED"}


class OSB_OT_remove_collection(Operator):
    bl_idname = "osb.remove_collection"
    bl_label = "Remove Collection Slot"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.one_shot_bake
        if not props.collections:
            return {"CANCELLED"}
        props.collections.remove(props.collections_index)
        props.collections_index = max(0, props.collections_index - 1)
        return {"FINISHED"}


class OSB_OT_add_selected_collections(Operator):
    bl_idname = "osb.add_selected_collections"
    bl_label = "Add Selected"
    bl_description = "Add every collection selected in the Outliner"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.one_shot_bake
        already = {i.collection for i in props.collections}
        added = 0

        found = []
        if hasattr(context, "selected_ids"):
            for id_block in context.selected_ids:
                if isinstance(id_block, bpy.types.Collection):
                    found.append(id_block)
        if not found:
            active = context.view_layer.active_layer_collection
            if active and active.collection:
                found.append(active.collection)

        for collection in found:
            if collection in already or collection == context.scene.collection:
                continue
            props.collections.add().collection = collection
            already.add(collection)
            added += 1

        if not added:
            self.report({"WARNING"}, "No new collections selected in the Outliner")
            return {"CANCELLED"}

        props.collections_index = len(props.collections) - 1
        self.report({"INFO"}, f"Added {added} collection(s)")
        return {"FINISHED"}


class OSB_OT_clear_collections(Operator):
    bl_idname = "osb.clear_collections"
    bl_label = "Clear Collections"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.one_shot_bake.collections.clear()
        context.scene.one_shot_bake.collections_index = 0
        return {"FINISHED"}


# ===========================================================================
# the bake
# ===========================================================================

# Blender reports bake job progress through handlers rather than a return value,
# so a module level flag is the only way for the modal operator to hear about it.
_BAKE_SIGNAL = {"complete": False, "cancelled": False}


def _bake_complete_handler(*args):
    _BAKE_SIGNAL["complete"] = True


def _bake_cancel_handler(*args):
    _BAKE_SIGNAL["cancelled"] = True


def _bake_signal_reset():
    _BAKE_SIGNAL["complete"] = False
    _BAKE_SIGNAL["cancelled"] = False


def _bake_signal_attach():
    if _bake_complete_handler not in bpy.app.handlers.object_bake_complete:
        bpy.app.handlers.object_bake_complete.append(_bake_complete_handler)
    if _bake_cancel_handler not in bpy.app.handlers.object_bake_cancel:
        bpy.app.handlers.object_bake_cancel.append(_bake_cancel_handler)


def _bake_signal_detach():
    for handlers, func in (
        (bpy.app.handlers.object_bake_complete, _bake_complete_handler),
        (bpy.app.handlers.object_bake_cancel, _bake_cancel_handler),
    ):
        while func in handlers:
            handlers.remove(func)


class OSB_OT_bake(Operator):
    bl_idname = "osb.bake"
    bl_label = "One Shot Bake"
    bl_description = "Unwrap, pack, bake and save one texture per collection"
    bl_options = {"REGISTER"}

    # Timer interval. Events other than TIMER are passed through, which is what
    # keeps the interface usable while Cycles works in the background.
    TICK = 0.2

    # How long to let Cycles get a bake job registered before giving up on it.
    # Generous, because the first GPU bake of a session compiles kernels first.
    BAKE_START_GRACE_TICKS = int(120.0 / TICK)

    @classmethod
    def poll(cls, context):
        props = context.scene.one_shot_bake
        return any(item.collection for item in props.collections)

    # -- entry points ------------------------------------------------------

    def invoke(self, context, event):
        """Drive the work from a timer so Blender keeps redrawing."""
        error = self._setup(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}

        _bake_signal_reset()
        _bake_signal_attach()

        wm = context.window_manager
        self._timer = wm.event_timer_add(self.TICK, window=context.window)
        wm.modal_handler_add(self)
        self._set_status(context)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        """Blocking path, for scripts and background renders."""
        error = self._setup(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}

        try:
            while self._queue:
                collection = self._queue.pop(0)
                if self._prepare_collection(context, collection):
                    bpy.ops.object.bake(type=self._props.bake_type)
                    self._finish_collection(context)
                    self._done += 1
        except Exception as ex:
            self.report({"ERROR"}, f"Bake failed: {ex}")
            self._abort_pending(context)
            self._teardown(context)
            return {"CANCELLED"}

        self._teardown(context)
        return self._final_report()

    def modal(self, context, event):
        if event.type == "ESC" and event.value == "PRESS":
            self._cancel_requested = True
            self._set_status(context, "stopping after this bake")
            return {"RUNNING_MODAL"}

        # Anything that is not our timer belongs to the rest of Blender.
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        try:
            finished = self._step(context)
        except Exception as ex:
            self.report({"ERROR"}, f"Bake failed: {ex}")
            self._abort_pending(context)
            self._teardown(context)
            return {"CANCELLED"}

        if finished:
            self._teardown(context)
            return self._final_report()

        return {"RUNNING_MODAL"}

    def cancel(self, context):
        self._abort_pending(context)
        self._teardown(context)

    # -- state machine -----------------------------------------------------

    def _step(self, context):
        """One tick. Returns True when there is nothing left to do."""
        if self._state == "NEXT":
            if self._cancel_requested or not self._queue:
                return True

            collection = self._queue.pop(0)
            self._set_status(context, collection.name)

            if self._prepare_collection(context, collection):
                _bake_signal_reset()
                self._bake_ticks = 0
                self._job_seen = False
                # INVOKE_DEFAULT hands the bake to Blender's job system instead
                # of blocking the main thread the way a plain call would.
                returned = bpy.ops.object.bake("INVOKE_DEFAULT", type=self._props.bake_type)

                if "CANCELLED" in returned:
                    self._warnings.append(
                        f"'{collection.name}': Blender refused to start the bake, skipped"
                    )
                    self._abort_pending(context)
                elif "FINISHED" in returned:
                    # It ran inline rather than as a job; nothing to wait for.
                    self._finish_collection(context)
                    self._done += 1
                else:
                    self._state = "BAKING"
            return False

        if self._state == "BAKING":
            self._bake_ticks += 1
            name = self._pending["collection"].name if self._pending else "?"

            if _BAKE_SIGNAL["cancelled"]:
                self._warnings.append(f"'{name}': bake was cancelled")
                self._abort_pending(context)
                self._state = "NEXT"
                return False

            running = bpy.app.is_job_running("OBJECT_BAKE")
            if running:
                self._job_seen = True

            # Only "not running" AFTER it was seen running means finished. Testing
            # it before the job registers would call an unstarted bake complete and
            # save the cleared image, which is where the all black atlases came from.
            if _BAKE_SIGNAL["complete"] or (self._job_seen and not running):
                self._finish_collection(context)
                self._done += 1
                self._state = "NEXT"
            elif not self._job_seen:
                if self._bake_ticks > self.BAKE_START_GRACE_TICKS:
                    self._warnings.append(
                        f"'{name}': bake never started, skipped rather than saving a blank image"
                    )
                    self._abort_pending(context)
                    self._state = "NEXT"
                else:
                    # First GPU bake of a session compiles kernels before the job
                    # appears, which can take a while. Say so instead of looking hung.
                    self._set_status(context, f"{name} - waiting for Cycles to start")
            return False

        return True

    def _setup(self, context):
        scene = context.scene
        props = scene.one_shot_bake

        if not hasattr(scene, "cycles"):
            return "Cycles is not enabled. Enable the Cycles addon first"

        targets, seen = [], set()
        for item in props.collections:
            if item.collection and item.collection not in seen:
                targets.append(item.collection)
                seen.add(item.collection)
        if not targets:
            return "No collections assigned"

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        self._props = props
        self._scene = scene
        self._queue = targets
        self._total = len(targets)
        self._warnings = []
        self._copies_made = 0
        self._webp_made = 0
        self._excluded_originals = 0
        self._done = 0
        self._pending = None
        self._timer = None
        self._state = "NEXT"
        self._bake_ticks = 0
        self._job_seen = False
        self._cancel_requested = False
        self._saved_render = self._push_render_settings(scene)
        self._prev_selected = [o for o in context.view_layer.objects if o.select_get()]
        self._prev_active = context.view_layer.objects.active
        self._apply_render_settings(scene, props)
        return None

    def _teardown(self, context):
        _bake_signal_detach()
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (RuntimeError, ReferenceError):
                pass
            self._timer = None
        try:
            context.workspace.status_text_set(None)
        except (RuntimeError, AttributeError):
            pass
        self._pop_render_settings(self._scene, self._saved_render)
        self._restore_selection(context, self._prev_selected, self._prev_active)

    def _set_status(self, context, note=None):
        index = min(self._total - len(self._queue) + 1, self._total)
        text = f"One Shot Bake  {index}/{self._total}"
        if note:
            text += f"  |  {note}"
        text += "   (Esc to stop)"
        try:
            context.workspace.status_text_set(text)
        except (RuntimeError, AttributeError):
            pass

    def _final_report(self):
        for message in self._warnings:
            self.report({"WARNING"}, message)

        if self._done == 0:
            self.report({"ERROR"}, "Nothing was baked. See warnings above")
            return {"CANCELLED"}

        summary = f"Baked {self._done} of {self._total} collection(s)"
        if self._props.compress_to_webp:
            summary += f", {self._webp_made} WebP written"
        if self._props.make_baked_copies:
            summary += f", {self._copies_made} baked copies created"
            if self._excluded_originals:
                summary += f", {self._excluded_originals} source collection(s) unticked"
        if self._cancel_requested:
            summary += " (stopped early)"
        self.report({"INFO"}, summary)
        return {"FINISHED"}

    # -- per collection ----------------------------------------------------

    def _prepare_collection(self, context, collection):
        """Everything up to the bake. True means the bake is ready to start.

        The guard and the bake nodes have to stay alive across ticks, so they
        live on self._pending until the bake finishes rather than in a with block.
        """
        props = self._props
        meshes = collection_meshes(collection, props.include_children)
        if not meshes:
            self._warnings.append(f"'{collection.name}' has no mesh objects, skipped")
            return False

        guard = VisibilityGuard()
        nodes = BakeTargetNodes()
        self._pending = {
            "collection": collection, "guard": guard, "nodes": nodes,
            "image": None, "objects": [],
        }

        try:
            guard.reveal_collection(context.view_layer, collection)
            for obj in meshes:
                guard.reveal_object(obj)

            selectable = [o for o in meshes if o.name in context.view_layer.objects]
            missing = len(meshes) - len(selectable)
            if missing:
                self._warnings.append(
                    f"'{collection.name}': {missing} object(s) are not in the view layer, skipped"
                )
            if not selectable:
                self._abort_pending(context)
                return False

            for obj in selectable:
                ensure_uv_map(obj, props.uv_map_name, props.overwrite_uv_map)

            self._select(context, selectable)
            self._unwrap_and_pack(context, props, selectable)

            bakeable = [o for o in selectable if any(s.material for s in o.material_slots)]
            no_material = len(selectable) - len(bakeable)
            if no_material:
                self._warnings.append(
                    f"'{collection.name}': {no_material} object(s) have no material, "
                    "unwrapped but excluded from the bake"
                )
            if not bakeable:
                self._warnings.append(f"'{collection.name}': no materials to bake, skipped")
                self._abort_pending(context)
                return False

            image, _ = make_bake_image(props, collection.name)
            nodes.attach(bakeable, image)
            self._select(context, bakeable)

            self._pending["image"] = image
            self._pending["objects"] = bakeable
            return True

        except Exception:
            self._abort_pending(context)
            raise

    def _finish_collection(self, context):
        """Save, optionally duplicate, then release everything prepare set up."""
        pending = self._pending
        if pending is None:
            return

        props = self._props
        collection = pending["collection"]
        image = pending["image"]
        objects = pending["objects"]

        try:
            # A completely empty image almost always means the bake did not run,
            # which used to be saved silently as a black atlas.
            if image_looks_blank(image):
                self._warnings.append(
                    f"'{collection.name}': bake produced an empty image - check that the "
                    "objects are lit and visible to the renderer"
                )

            # Denoise first, so the saved file and the duplicated material both
            # pick up the cleaned image rather than the raw bake.
            if props.denoise_with_compositor:
                ok, reason = denoise_image_with_compositor(
                    image, self._scene, props.bake_type in NON_COLOR_BAKES
                )
                if not ok:
                    self._warnings.append(
                        f"'{collection.name}': compositor denoise skipped ({reason}), kept raw bake"
                    )

            path = save_image(image, props, collection.name)
            if path is None:
                self._warnings.append(
                    f"'{collection.name}': baked to '{image.name}' but no output folder set, not saved"
                )

            # WebP comes after the save, so it compresses the denoised result.
            material_image = image
            if props.compress_to_webp:
                directory = bpy.path.abspath(props.output_dir)
                if not directory:
                    self._warnings.append(
                        f"'{collection.name}': WebP needs an output folder, skipped"
                    )
                else:
                    try:
                        webp_path = save_image_as_webp(image, directory, props.webp_quality)
                        material_image = load_webp_image(webp_path, image)
                        self._webp_made += 1
                    except (RuntimeError, OSError) as ex:
                        self._warnings.append(
                            f"'{collection.name}': WebP compression failed ({ex})"
                        )

            if props.make_baked_copies:
                # Bake nodes are still on the originals here, but the copies get
                # a fresh material so they are unaffected.
                _, made = duplicate_with_baked_material(
                    context, objects, material_image, props.uv_map_name, collection.name
                )
                self._copies_made += made
        finally:
            pending["nodes"].detach(props.cleanup_nodes)
            pending["guard"].restore()
            # Must come after the restore, which would otherwise put the
            # collection's original visibility straight back.
            if props.make_baked_copies and props.hide_original_collection:
                if exclude_collection(context.view_layer, collection):
                    self._excluded_originals += 1
            self._pending = None

    def _abort_pending(self, context):
        """Release a prepared collection without saving anything."""
        pending = self._pending
        if pending is None:
            return
        try:
            pending["nodes"].detach(self._props.cleanup_nodes)
            pending["guard"].restore()
        except (RuntimeError, ReferenceError):
            pass
        finally:
            self._pending = None

    def _unwrap_and_pack(self, context, props, objects):
        smart_kwargs = supported_kwargs(bpy.ops.uv.smart_project, {
            "angle_limit": props.angle_limit,
            "island_margin": props.island_margin,
            "correct_aspect": props.correct_aspect,
            "scale_to_bounds": props.scale_to_bounds,
        })
        pack_kwargs = supported_kwargs(bpy.ops.uv.pack_islands, {
            "rotate": props.pack_rotate,
            "scale": props.pack_scale,
            "merge_overlap": props.pack_merge_overlap,
            "margin": props.pack_margin,
            "shape_method": props.pack_shape_method,
            # Consolidate into the tile we are baking, never spread across UDIMs.
            "udim_source": "CLOSEST_UDIM",
        })

        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(**smart_kwargs)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        # Must happen outside edit mode: uv_layers is stale while the bmesh is live.
        if props.even_texel_density:
            normalize_texel_density(objects, props.uv_map_name)
            fit_uvs_to_unit_square(objects, props.uv_map_name)

        use_uvpm = props.use_uvpackmaster
        if use_uvpm:
            available, reason = uvpackmaster_available(context)
            if not available:
                self._warnings.append(f"{reason}; packed with Blender instead")
                use_uvpm = False

        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            if use_uvpm:
                pack_with_uvpackmaster(context, props)
            else:
                with uv_editor_context(context):
                    bpy.ops.uv.select_all(action="SELECT")
                    bpy.ops.uv.pack_islands(**pack_kwargs)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

    # -- state -------------------------------------------------------------

    def _select(self, context, objects):
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]

    def _restore_selection(self, context, previous, active):
        try:
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            for obj in previous:
                try:
                    obj.select_set(True)
                except (RuntimeError, ReferenceError):
                    pass
            context.view_layer.objects.active = active
        except (RuntimeError, ReferenceError):
            pass

    def _push_render_settings(self, scene):
        bake = scene.render.bake
        saved = {
            "engine": scene.render.engine,
            "margin": bake.margin,
            "use_clear": bake.use_clear,
            "use_selected_to_active": bake.use_selected_to_active,
            "use_pass_direct": bake.use_pass_direct,
            "use_pass_indirect": bake.use_pass_indirect,
            "use_pass_color": bake.use_pass_color,
        }
        if hasattr(scene, "cycles"):
            saved["samples"] = scene.cycles.samples
            saved["device"] = scene.cycles.device
        return saved

    def _apply_render_settings(self, scene, props):
        scene.render.engine = "CYCLES"
        scene.cycles.samples = props.samples
        scene.cycles.device = props.device

        bake = scene.render.bake
        bake.margin = props.bake_margin
        bake.use_clear = True
        bake.use_selected_to_active = False

        # Plain colour output rather than lit shading.
        if props.bake_type == "DIFFUSE":
            bake.use_pass_direct = False
            bake.use_pass_indirect = False
            bake.use_pass_color = True

    def _pop_render_settings(self, scene, saved):
        try:
            bake = scene.render.bake
            scene.render.engine = saved["engine"]
            bake.margin = saved["margin"]
            bake.use_clear = saved["use_clear"]
            bake.use_selected_to_active = saved["use_selected_to_active"]
            bake.use_pass_direct = saved["use_pass_direct"]
            bake.use_pass_indirect = saved["use_pass_indirect"]
            bake.use_pass_color = saved["use_pass_color"]
            if "samples" in saved:
                scene.cycles.samples = saved["samples"]
                scene.cycles.device = saved["device"]
        except (RuntimeError, ReferenceError, KeyError):
            pass


# ===========================================================================
# panel
# ===========================================================================

class OSB_PT_main(Panel):
    bl_label = "One Shot Bake"
    bl_idname = "OSB_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "One Shot Bake"

    def draw(self, context):
        layout = self.layout
        props = context.scene.one_shot_bake

        box = layout.box()
        box.label(text="Collections", icon="OUTLINER_COLLECTION")
        row = box.row()
        row.template_list("OSB_UL_collections", "", props, "collections", props, "collections_index", rows=3)
        col = row.column(align=True)
        col.operator("osb.add_collection", text="", icon="ADD")
        col.operator("osb.remove_collection", text="", icon="REMOVE")
        col.separator()
        col.operator("osb.clear_collections", text="", icon="TRASH")
        box.operator("osb.add_selected_collections", icon="RESTRICT_SELECT_OFF")
        box.prop(props, "include_children")

        box = layout.box()
        box.label(text="Unwrap", icon="UV")
        box.prop(props, "uv_map_name")
        box.prop(props, "overwrite_uv_map")
        box.prop(props, "angle_limit")
        box.prop(props, "island_margin")
        row = box.row(align=True)
        row.prop(props, "correct_aspect", toggle=True)
        row.prop(props, "scale_to_bounds", toggle=True)
        box.prop(props, "even_texel_density")

        box = layout.box()
        box.label(text="Pack Islands", icon="MOD_UVPROJECT")
        box.prop(props, "use_uvpackmaster", toggle=True, icon="MOD_UVPROJECT")

        if props.use_uvpackmaster:
            sub = box.column(align=False)
            sub.prop(props, "uvpm_use_pixel_margin")
            if props.uvpm_use_pixel_margin:
                sub.prop(props, "uvpm_pixel_margin")
            else:
                sub.prop(props, "uvpm_margin")
            sub.prop(props, "uvpm_rotation_enable")
            row = sub.row()
            row.enabled = props.uvpm_rotation_enable
            row.prop(props, "uvpm_rotation_step")
            sub.prop(props, "uvpm_precision")
            sub.prop(props, "uvpm_heuristic_enable")
            row = sub.row()
            row.enabled = props.uvpm_heuristic_enable
            row.prop(props, "uvpm_heuristic_time")
            sub.prop(props, "uvpm_normalize_scale")
            if props.uvpm_normalize_scale and props.even_texel_density:
                sub.label(text="Conflicts with Even Texel Density", icon="ERROR")
        else:
            box.prop(props, "pack_shape_method")
            box.prop(props, "pack_margin")
            row = box.row(align=True)
            row.prop(props, "pack_rotate", toggle=True)
            row.prop(props, "pack_scale", toggle=True)
            box.prop(props, "pack_merge_overlap")

        box = layout.box()
        box.label(text="Image", icon="IMAGE_DATA")
        row = box.row(align=True)
        row.prop(props, "resolution_x")
        row.prop(props, "resolution_y")
        row = box.row(align=True)
        row.prop(props, "use_alpha", toggle=True)
        row.prop(props, "use_float", toggle=True)
        box.prop(props, "image_name_pattern")
        box.prop(props, "replace_existing_image")

        box = layout.box()
        box.label(text="Bake", icon="RENDER_STILL")
        box.prop(props, "bake_type")
        box.prop(props, "samples")
        box.prop(props, "bake_margin")
        box.prop(props, "device")
        box.prop(props, "cleanup_nodes")
        box.separator()
        box.prop(props, "denoise_with_compositor", toggle=True, icon="SHADERFX")
        box.prop(props, "make_baked_copies", toggle=True, icon="DUPLICATE")
        if props.make_baked_copies:
            box.prop(props, "hide_original_collection")

        box = layout.box()
        box.label(text="Output", icon="FILE_FOLDER")
        box.prop(props, "output_dir")
        box.prop(props, "file_format")
        box.separator()
        box.prop(props, "compress_to_webp", toggle=True, icon="FILE_IMAGE")
        if props.compress_to_webp:
            row = box.row()
            row.prop(props, "webp_quality", slider=True)
            if not props.output_dir:
                box.label(text="Set an output folder to enable WebP", icon="ERROR")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.8
        row.operator("osb.bake", icon="RENDER_STILL")


# ===========================================================================
# registration
# ===========================================================================

classes = (
    OSB_CollectionItem,
    OSB_Settings,
    OSB_UL_collections,
    OSB_OT_add_collection,
    OSB_OT_remove_collection,
    OSB_OT_add_selected_collections,
    OSB_OT_clear_collections,
    OSB_OT_bake,
    OSB_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.one_shot_bake = PointerProperty(type=OSB_Settings)


def unregister():
    # Tolerate a half-registered state so reloading never leaves a broken addon.
    if hasattr(bpy.types.Scene, "one_shot_bake"):
        del bpy.types.Scene.one_shot_bake
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
