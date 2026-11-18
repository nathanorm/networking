# AGENTS.md

- Spec: [docs/SPEC.md](docs/SPEC.md). Keep the first pass lean; don't build pass-2 features unasked.
- Everything runs through `make` (Docker only, no host Python). Run `make test` and `make lint` before finishing; `make fmt` fixes formatting.
- `apps/<name>/`: one deployable each (`pyproject.toml`, `Dockerfile`, `src/<pkg>/`, `tests/`). Apps never import each other.
- `shared/`: library code used by 2+ apps (`edgelab_protocol` is stdlib-only, no I/O).
- After editing any `pyproject.toml`, run `make lock`. App images build from the repo root.
