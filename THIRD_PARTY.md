# Third-party dependency inventory

Recorded on 2026-10-06 from installed distribution metadata after `uv sync --locked`.
The versions are governed by `uv.lock`; development tools and transitive Python packages are included.
License declarations below are metadata reported by the packages, not a completed legal review.
Consult each package's shipped license/notice files before redistributing it.

The team has not selected a license for this project's own source. This inventory does not assign one.

| Python distribution | Installed version | Declared license |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| fastapi | 0.142.2 | MIT |
| google-re2 | 1.1.20251105 | BSD License |
| h11 | 0.16.0 | MIT License |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.6.0 | MIT License |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.1.1 | MIT |
| PyYAML | 6.0.3 | MIT License |
| ruff | 0.16.10 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

## Deployment and runtime tooling

OpenCode is installed as `@opencode/cli@2.0.22` by `scripts/setup_opencode_pipeline.sh`.
The adapter declares `@opencode/plugin@2.0.22` in `adapters/opencode/package.json`, although the shim
does not import it at runtime. These Node packages' shipped license/notice files must be reviewed
alongside their transitive dependencies; this inventory does not assert an unverified license.
The adapter has no vendored runtime dependencies; tests load it using Node's built-in runner.
The CLI's transitive npm dependency graph is not locked by this repository and needs a separate
license/notice review when distributing the deployment image.

The deployment image also includes Python 3.12, Node.js 22, uv 0.11.29, git and their system libraries;
Caddy 2 is a separate image. Terraform uses the AWS provider pinned by `infra/.terraform.lock.hcl`.
Their upstream image/package license notices must be preserved as appropriate when redistributing.
The Docker base images are version tags, not immutable digest pins.

Competition PDFs and team presentation materials are separate from the software dependency graph.
Project-wide redistribution terms and team attribution remain maintainer decisions.
