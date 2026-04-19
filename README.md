# Asset Prep Tools — Blender Addon

⚠️ Still in development ⚠️! A single Blender sidebar addon that combines three production utilities into one panel: baking preparation, GLB compression/JSX generation, and image batch conversion.

**Location:** `View3D > Sidebar (N) > Asset Prep`

---

## Tabs

### Baking

Tools for organizing and preparing scenes before texture baking.

#### ID Labeling

Rename collections with a structured prefix (`ID_1_CollectionName`) to keep bake targets uniquely identified.

| Setting               | Description                                                 |
| --------------------- | ----------------------------------------------------------- |
| ID Prefix             | Token prepended before the counter (e.g. `ID`)              |
| Start Index           | Counter value for the first selected collection             |
| Extra Tag             | Optional tag inserted between the counter and original name |
| Strip Existing Prefix | Remove a previously applied prefix before adding a new one  |

**Buttons:**

- **Label Selected Collections** — Renames all selected collections in the Outliner
- **Clear ID Labels** — Strips the prefix back off

> Ctrl/Shift-click collections in the Outliner to multi-select before labeling.

#### Propagate to Objects

Prepends the parent collection's full name to every object inside it, keeping object names in sync with their collection. Optionally renames mesh data-blocks too.

#### UV Map

Adds a named UV map to all selected mesh objects.

| Setting           | Description                                                  |
| ----------------- | ------------------------------------------------------------ |
| UV Map Name       | Name for the new UV channel                                  |
| Set Active        | Makes the new map the active selected layer (blue highlight) |
| Set Active Render | Makes the new map the active render layer (camera icon)      |

#### Grid Duplicator

Duplicates all selected objects and arranges the copies in a grid centred on the world origin. Originals are hidden. Useful for laying out bake targets flat.

| Setting          | Description                                               |
| ---------------- | --------------------------------------------------------- |
| Spacing          | Distance between grid cell centres                        |
| Columns          | Number of columns (0 = auto nearest square root)          |
| Linked Duplicate | Share mesh data between copies instead of full duplicates |

- **Duplicate to Grid** — Creates the grid layout
- **Snap Back to Original** — Moves copies back to their original world positions

---

### Code

Tools for compressing GLB files and generating React Three Fiber components.

#### GLB Compressor (`gltf-transform`)

Runs [`gltf-transform`](https://gltf-transform.donmccurdy.com/) pipelines on one file or an entire directory.

**Requires:** `npm install -g @gltf-transform/cli`

| Option                   | Description                                                                    |
| ------------------------ | ------------------------------------------------------------------------------ |
| Process Entire Directory | Batch-process every `.glb` in the input folder                                 |
| Input File / Directory   | Source file(s)                                                                 |
| Output Directory         | Destination folder (defaults to source location)                               |
| Resize Textures          | Downscale textures to a specified width × height                               |
| Draco Compression        | Compress mesh geometry with Draco (position/normal/color/UV quantization bits) |
| KTX2 Compression         | Compress textures to KTX2 — UASTC (high quality) or ETC1S (smallest)           |
| WebP Textures            | Convert textures to WebP at a chosen quality (disabled when KTX2 is active)    |
| Verbose                  | Print detailed pipeline output to the Blender console                          |

Output files are named `<original>_compressed.glb`.

#### gltfjsx — React Three Fiber

Runs [`gltfjsx`](https://github.com/pmndrs/gltfjsx) to generate a `.jsx` / `.tsx` component from a GLB.

**Requires:** Node.js / `npx`

Key options include TypeScript output, instancing, shadows, mesh simplification, and a full `--transform` pipeline (Draco + resize + material palette). All CLI flags are exposed directly in the UI.

---

### Image

Batch converts PNG files in a directory to web-friendly formats.

**Requires:**

- [ImageMagick](https://imagemagick.org/) (`magick` on PATH) for WebP
- [KTX-Software](https://github.com/KhronosGroup/KTX-Software) (`toktx` on PATH) for KTX2

#### WebP (ImageMagick)

| Setting  | Description                                        |
| -------- | -------------------------------------------------- |
| Quality  | Compression quality — 0 = smallest, 100 = lossless |
| Resize % | Scale images before converting (100 = no resize)   |

Outputs `<name>.webp` alongside the source files.

#### KTX2 — UASTC (`toktx`)

GPU-decoded format, highest visual quality. Two presets:

| Preset            | Use case                     |
| ----------------- | ---------------------------- |
| UASTC Max Quality | Best quality, larger files   |
| UASTC Low Quality | Smaller files, lower quality |

#### KTX2 — ETC1S (`toktx`)

Block-compressed format, smallest file sizes. Two presets:

| Preset            | Use case              |
| ----------------- | --------------------- |
| ETC1S Max Quality | Best quality at ETC1S |
| ETC1S Low Quality | Minimum file size     |

Output files are named `<name>_<preset_suffix>.ktx2` and written alongside the source PNGs.

---

## Installation

1. Copy `hatch_tools.py` to your Blender addons folder:
   - Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\`
   - macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/`
   - Linux: `~/.config/blender/<version>/scripts/addons/`
2. In Blender: **Edit > Preferences > Add-ons**, search for **Asset Prep Tools**, and enable it.
3. Open the N-panel in any 3D viewport (`N` key) and select the **Asset Prep** tab.
