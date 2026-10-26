# Contributing to ArchFlow

Thanks for helping. Bug reports, ideas and pull requests are welcome.

## License of contributions

ArchFlow is published under [CC BY-NC-ND 4.0](LICENSE): you may read, run and share it for non-commercial
purposes, but not distribute modified versions. So that the maintainer can include your work, **by opening a pull
request you agree that your contribution may be used, modified and distributed by the maintainer as part of
ArchFlow under its current license or any future license**, and you confirm that you have the right to contribute
it. If you cannot agree to this, please open an issue describing the change instead.

## Set up

```bash
cp .env.example .env                      # set ARCHFLOW_INTERNAL_TOKEN (any random string); AI keys are optional
cd backend && uv sync --extra codegraph   # Python 3.11+, https://docs.astral.sh/uv/
uv run uvicorn archflow.main:app --reload --port 8000
cd collab && npm ci && npm run dev        # Node 22+
cd frontend && npm ci && npm run dev      # http://localhost:5173
```

## Before you open a pull request

```bash
cd backend && uv run pytest -q                    # + TEST_DATABASE_URL=postgresql://... for the Postgres store
uv run archflow-prompts check                     # prompt versions are locked
cd frontend && npm run build && npm run e2e       # typecheck, build, Playwright end-to-end
```

- **Database**: change the models in `backend/archflow/db/models.py`, then generate a migration with
  `uv run alembic -c archflow/db/alembic.ini revision --autogenerate -m "..."` and review it. The test suite checks
  that the migrations produce exactly the models' schema.
- **Prompts**: never edit a released prompt version. Add `v2` next to `v1` in `backend/archflow/prompts/<id>.yaml`,
  point `active` at it, run `uv run archflow-prompts lock`, and compare both with
  `uv run archflow-eval --prompt <id>=v1` against `--prompt <id>=v2 --baseline <report>`.
- **AI features** are Pydantic AI agents (`backend/archflow/agents`). Give a new one a YAML prompt, a case in
  `backend/archflow/evals/datasets/`, and an offline fallback where it makes sense. Suggestions and follow-ups come
  from the LLM, not from fixed lists.
- Keep pull requests focused, match the surrounding code style, and add tests for behaviour changes.

## Reporting bugs

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.
