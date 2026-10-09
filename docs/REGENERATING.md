# Regenerating src/

This repository holds the *output* of the DS2 recomp pipeline as source. It does not carry
the pipeline. That lives in **DS2Recomp** (https://github.com/bifftannen1/DS2Recomp, private),
which has:

- `app/ds2_rex_diagnostic_manifest.toml`: the ReXGlue codegen manifest
- `config/codegen.partition.json`: the TU partition seed, which keeps the 157-TU layout stable
- `config/allocation.tsv.gz`, `config/names.tsv.gz`, `config/source_roots.tsv`, ...: function
  to source-file allocation and PDB names
- `tools/organize_generated.py` (`--verify`, `--ids pdb`): codegen output -> named tree
- `tools/naming/`, `tools/pdbchain/`: the naming chain
- `tools/build.py`: the end-to-end pipeline (SDK -> codegen -> organize -> app)

To refresh this repo, run the DS2Recomp build. Then replace `src/` here with
`app/generated_named/`, plus `ds2_rex_diagnostic_funcs.<N>.h` and `ds2_rex_diagnostic_pch.h`
from `app/generated/`. Keep `src/app/` and `src/modapi/`. Rebuild and run the object
comparison in docs/BUILDING.md.

The file docs/NAMED_TREE.md is copied verbatim from DS2Recomp. Its paths (`app/generated_named/`)
correspond to `src/` here.
