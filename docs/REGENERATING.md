# Regenerating src/

This repo holds the pipeline's *output*. The pipeline (codegen manifest, TU partition seed,
allocation and name tables, organizer, naming chain) lives in the separate **DS2Recomp** repo (not published).

To refresh:
1. Run the DS2Recomp build.
2. Replace `src/` with its `app/generated_named/` plus `app/generated/ds2_rex_diagnostic_pch.h`.
   Keep `src/app/` and `src/modapi/`.
3. Rebuild and run the object comparison in [BUILDING.md](BUILDING.md).
