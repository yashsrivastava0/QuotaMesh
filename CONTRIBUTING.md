# Contributing to QuotaMesh

## Branch and commit workflow

Use `main` as the stable integration branch. Start each task on its own branch:

```sh
git switch main
git pull --ff-only
git switch -c feat/<short-task-name>
```

Use `fix/`, `docs/`, or `chore/` instead of `feat/` when that better describes the task. Keep unrelated work on separate branches. Commit each coherent, reviewable milestone; do not wait until a large task is finished to make its first commit.

Use Conventional Commit subjects, for example `feat(gateway): add profile selection` or `docs: clarify local setup`. Add an issue or task reference to the commit body when available. When there is no tracker ID, the branch slug and commit history provide the task link. Do not amend or rewrite commits that have already been pushed unless the repository owner explicitly requests it.

Push the task branch and open a pull request to `main`. Include the task reference, behavior changes, migration or security impact, and commands with their actual results. Merge only after the CI workflow and review requirements pass. Never force-push `main`.

The repository may enforce branch protection through GitHub settings. The checked-in workflow provides repeatable CI; repository administrators should require that check and pull-request review in GitHub if those controls are desired for all contributors.

## Local setup and checks

QuotaMesh requires Python 3.11 or newer.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python -m build
```

Use `QUOTAMESH_DATA_DIR` to isolate local test or development data. Use `quotamesh fake-upstream` for a provider-free manual smoke check. Do not use a real provider key in tests or examples.

## Change expectations

- Keep each change within the active roadmap phase. Document a required cross-phase prerequisite in the pull request.
- Preserve the PDF's privacy and local-first requirements. Never store request or response bodies in attempt history.
- Update the README or `docs/` when behavior, setup, architecture, or limitations change.
- Add or update focused tests when behavior changes and record only checks that actually ran.
