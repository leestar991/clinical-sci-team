# Implementation Plan — Refactor `ppt-outline-generator` into a PPT Design-Spec Skill (ppt-master Step 2–4)

## Problem Statement
Refactor `/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-outline-generator` so it becomes a standalone skill that reproduces ppt-master's **Step 2 (Project Init) → Step 3 (Template Option) → Step 4 (Strategist)** pipeline and, from user input + already-converted uploads in `/mnt/uploads`, produces a complete PPT design spec: `design_spec.md` (I–XI narrative) + `spec_lock.md` (machine-readable contract). Relevant `references/`, `scripts/`, `templates/`, `workflows/` are copied directly from ppt-master (`/Users/louli/Documents/aigctools/ppt-master/skills/ppt-master/`). The skill stops at the spec; SVG generation/export stays downstream.

## Requirements (confirmed with user)
- **Output (1=a)**: pure ppt-master native — only `design_spec.md` + `spec_lock.md`. No `outline.md` / `slide-*.md`. Downstream `ppt-svg-generator` will be adapted separately.
- **Scope (2=a)**: strictly Step 2–4. Step 1 source conversion excluded (uploads already MD in `/mnt/uploads`); Step 5 image acquisition excluded.
- **Templates (3=a)**: selection/application only (copy user-given brand/layout/deck paths into the project + fuse). Creation stays in `ppt-template-design`; `create-template`/`create-brand` referenced as pointers only.
- **Path convention (4=b, overridden)**: project root `<project_path>` = **`/mnt/workspace`** (deer-flow session working directory, pre-existing absolute path). Spec lands at `/mnt/workspace/design_spec.md` + `/mnt/workspace/spec_lock.md`; scripts referenced as `/mnt/skills/custom/ppt-outline-generator/scripts/...`.
- **File manifest (5=c)**: derived from the actual Strategist + spec-template dependency closure (below).
- **Identity (6=a)**: keep directory/frontmatter name `ppt-outline-generator`, update `description`; rewrite ppt-master's split-mode/next-step handoff to point at downstream `ppt-svg-generator`.
- **Sub-decisions (adopted defaults)**: D1 copy `ai-image-comparison/` PNGs = yes; D2 copy `image-generator.md` + `shared-standards.md` = yes; D3 duplicate full `templates/` library = yes; import mode = `--copy` (preserve `/mnt/uploads` originals).

## Background (research findings)
- `references/strategist.md` is the Step-4 core. Mandatory reads / cross-links: `canvas-formats.md`; `templates/design_spec_reference.md`; `templates/spec_lock_reference.md`; `templates/icons/README.md` (+ `ls|grep` over icon libs); `scripts/config.py` (`INDUSTRY_COLORS`); for AI-image rows → `references/image-renderings/_index.md`, `references/image-palettes/_index.md`, `references/ai-image-comparison/`, `references/image-generator.md` (§4.2/§5.3 prompt depth); for any B/C/D/E images → `references/image-layout-patterns.md` (GATE), `references/image-layout-spec.md`, `references/svg-image-embedding.md`; Visualization Match → `templates/charts/charts_index.json`; formula → `scripts/latex_render.py` + `scripts/analyze_images.py`.
- Spec templates reference `shared-standards.md` (§XI SVG constraints) and `update_spec.py`.
- Script import closure is clean: `project_manager.py`→`project_utils.py`; `config.py`, `error_helper.py`, `analyze_images.py`, `latex_render.py`, `update_spec.py` are stdlib-only at import time. No need for `source_to_md/`, `image_backends/`, `svg_to_pptx/`, `pptx_to_svg/`.
- `project_manager.py` supports `init <name> --format <fmt> --dir <path>` (base defaults to `cwd/projects`, auto-names `<name>_<format>_<date>`) and `import-sources <project_path> <files> --move|--copy` (accepts an explicit project path).
- Sibling-skill precedent: `ppt-svg-generator` already vendored ppt-master `scripts/references/templates/workflows` wholesale and uses `/mnt/skills/custom/<skill>/...` + workspace conventions.

## Path mapping (deer-flow session dir)
- `<project_path>` = `/mnt/workspace` (pre-existing).
- Scaffold `/mnt/workspace/{sources,images,templates}`.
- Import `/mnt/uploads/*` (already MD) → `/mnt/workspace/sources/` via `import-sources /mnt/workspace <files> --copy`.
- Outputs: `/mnt/workspace/design_spec.md` + `/mnt/workspace/spec_lock.md`.
- `project_manager.py init`'s auto-named folder is NOT used (it cannot equal `/mnt/workspace`); scaffold subdirs in the existing root directly. All vendored docs that say `<project_path>` / `projects/<name>/` are read as `/mnt/workspace`.

## Pipeline (new SKILL.md)
```
/mnt/uploads (MD already converted)
  → Step 1: Project Init (scaffold /mnt/workspace + import-sources --copy)
  → Step 2: Template Option (selection/fusion only; creation → ppt-template-design)
  → Step 3: Strategist — Eight Confirmations (BLOCKING) → design_spec.md + spec_lock.md
  ⇢ handoff to ppt-svg-generator (downstream)
```

## File manifest to copy (ppt-master/skills/ppt-master/ → ppt-outline-generator/, preserve relative layout)
- scripts/: `project_manager.py`, `project_utils.py`, `config.py`, `error_helper.py`, `analyze_images.py`, `latex_render.py`, `update_spec.py`, `README.md`, `requirements.txt`, `docs/{project.md,image.md,conversion.md,troubleshooting.md,update_spec.md}`.
- references/: `strategist.md`, `canvas-formats.md`, `image-layout-patterns.md`, `image-layout-spec.md`, `svg-image-embedding.md`, `shared-standards.md`, `image-generator.md`, `image-renderings/` (all), `image-palettes/` (all), `ai-image-comparison/` (all).
- templates/: `design_spec_reference.md`, `spec_lock_reference.md`, `README.md`, `charts/` (all), `icons/` (all 5 libs + README), `brands/` (all), `layouts/` (all), `decks/` (all).
- workflows/: `topic-research.md` only.
- Remove superseded: `references/outline-templates.md` and the old outline-paradigm `SKILL.md` (replaced).

## Task Breakdown

**Task 0: Save this plan document.**
- Objective: persist the plan for reference during execution.
- Guidance: write this full plan to `/Users/louli/Documents/aigctools/clinical-sci-team/docs/plans/ppt-outline-generator-design-spec-refactor.md` (create `docs/plans/` if absent).
- Verify: file exists and contains the full plan.
- Demo: plan readable at the saved path.

**Task 1: Snapshot & clear the old skill, establish new directory skeleton.**
- Objective: ready `ppt-outline-generator/` to receive vendored content without losing recoverability.
- Guidance: record current `SKILL.md` + `references/outline-templates.md`; create empty `references/`, `scripts/`, `templates/`, `workflows/`; defer deleting old `SKILL.md` until Task 6.
- Verify: directory listing shows the four subdirs; old files still present pending replacement.
- Demo: skeleton exists alongside soon-to-be-replaced old files.

**Task 2: Vendor Step-2/Step-4 scripts with verified import closure.**
- Objective: copy the scripts manifest above.
- Guidance: preserve filenames/layout; resolve any `from <localmodule>` discovered at copy time.
- Test: `python3 -c "import project_manager, project_utils, config, error_helper, analyze_images, latex_render, update_spec"` from the scripts dir → no ImportError; `project_manager.py --help`; `project_manager.py init demo --format ppt169 --dir /tmp/pm_test` scaffolds; clean up the temp dir.
- Demo: `project_manager.py init/import-sources/validate` runs standalone.

**Task 3: Vendor the Strategist reference closure.**
- Objective: copy the references manifest (per D1/D2).
- Guidance: preserve `references/` layout so internal links resolve.
- Test: grep `strategist.md` for `](` targets + `read_file ` paths; assert each referenced `references/*` and `templates/*` exists; list unresolved.
- Demo: following every relative reference in `strategist.md` lands on a present file.

**Task 4: Vendor spec templates + template library.**
- Objective: copy the templates manifest (per D3), incl. large `icons/` (~11,600 files) and `charts/`.
- Guidance: keep `*_index.json` intact for Step-3 selection + Visualization Match.
- Test: parse `charts/brands/layouts/decks` index JSONs; `ls templates/icons/chunk-filled | grep home` returns a file.
- Demo: indexes load; `ls|grep` icon lookup works as Strategist §f expects.

**Task 5: Vendor `workflows/topic-research.md` and reconcile cross-skill pointers.**
- Objective: copy `topic-research.md`; no dangling references to non-copied workflows.
- Guidance: do NOT copy `create-template`/`create-brand`/`resume-execute`; SKILL.md notes creation → `ppt-template-design`, downstream SVG → `ppt-svg-generator`.
- Test: grep vendored `references/` + `workflows/` for links to omitted workflows; confirm new SKILL.md has no dead links.
- Demo: `topic-research` fallback available; no SKILL-level dead links.

**Task 6: Write the new `SKILL.md` (Step 2–4, /mnt/workspace-rooted, design-spec output).**
- Objective: replace outline-paradigm SKILL.md with Step-2→4 pipeline outputting `design_spec.md` + `spec_lock.md`.
- Guidance:
  - Frontmatter: keep `name: ppt-outline-generator`; rewrite `description` ("produce a complete PPT design spec from user input + uploaded materials") with triggers.
  - Step 1 — Project Init: GATE = sources ready in `/mnt/uploads`. `<project_path> = /mnt/workspace`. Scaffold `/mnt/workspace/{sources,images,templates}`; `import-sources /mnt/workspace /mnt/uploads/<files> --copy`; do not rely on `init`'s auto-named folder.
  - Step 2 — Template Option: default free design; trigger only on explicit template directory paths (brand/layout/deck) → copy/fuse into `/mnt/workspace/templates/` (single-path dispatch + multi-path fusion + conflict rules). Creation → pointer to `ppt-template-design`.
  - Step 3 — Strategist: mandatory read of `references/strategist.md` + `templates/design_spec_reference.md`; Eight Confirmations (⛔ BLOCKING, single bundle); formula path (`latex_render.py`) + image analysis (`analyze_images.py`) before writing; output `/mnt/workspace/design_spec.md` + `/mnt/workspace/spec_lock.md` (read `spec_lock_reference.md`).
  - Replace ppt-master's split-mode/next-step handoff with a handoff to `ppt-svg-generator`.
  - Carry over Global Execution Discipline + Language rule trimmed to Step 2–4. Use `/mnt/skills/custom/ppt-outline-generator/scripts/...`; instruct that vendored docs' `<project_path>`/`projects/<name>` mean `/mnt/workspace`.
  - Adopt sibling-skill file-write / `bash <10000 chars` discipline.
- Test: read-through; verify every path SKILL.md cites exists; confirm no `outline.md`/`slide-*.md`/Pyramid-SCQA remnants.
- Demo: coherent Step 2–4 design-spec skill; BLOCKING gate + two output artifacts clearly specified.

**Task 7: Remove superseded artifacts and run end-to-end consistency pass.**
- Objective: delete `references/outline-templates.md`; final integrity check.
- Guidance: leave `.DS_Store` untouched; no orphaned old content.
- Test: (a) `mkdir -p /mnt/workspace/{sources,images,templates}` + `import-sources /mnt/workspace <sample.md> --copy` → validate populated root; confirm spec would land at `/mnt/workspace/design_spec.md` + `/mnt/workspace/spec_lock.md`; (b) cross-link audit across `SKILL.md` + `references/` + `templates/` → zero skill-level dead links; (c) index JSONs parse; (d) `requirements.txt` covers `latex_render.py`/`analyze_images.py` deps.
- Demo: clean dry-run — scaffold `/mnt/workspace`, import a sample MD, skill positioned to drive Eight Confirmations toward `design_spec.md` + `spec_lock.md`, with no leftover outline-era files.

## Notes for execution
- Source of truth for copies: `/Users/louli/Documents/aigctools/ppt-master/skills/ppt-master/`.
- Target skill: `/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-outline-generator/`.
- The heavy `templates/icons/` copy (~11,600 files) and `ai-image-comparison/` PNGs are intentional (self-contained skill).
- Preserve relative cross-links inside vendored `.md` files (do not rewrite their internal `references/`/`templates/`/`scripts/` paths); only the new top-level `SKILL.md` uses absolute `/mnt/skills/custom/ppt-outline-generator/...` invocation paths and the `/mnt/workspace` project root.
