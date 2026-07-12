# Release Process

PyPI publishing uses GitHub Actions Trusted Publishing. No long-lived PyPI token is stored in GitHub.

## One-time PyPI setup

1. Create and verify accounts on PyPI and TestPyPI. They are separate services.
2. In PyPI's publishing settings, create a pending GitHub publisher with:
   - PyPI project name: `tess-tools`
   - Owner: `guy71126`
   - Repository: `tess-tools`
   - Workflow: `release.yml`
   - Environment: `pypi`
3. In GitHub, create an environment named `pypi` and require manual approval for deployment.

A pending publisher does not reserve the package name. Complete the first publication promptly after configuring it.

## Release checklist

1. Confirm CI passes on `main`.
2. Update the version in `pyproject.toml`, `tess_tools/__init__.py`, and `CITATION.cff`.
3. Move changelog entries from `Unreleased` into a dated version section.
4. Run the complete offline suite and bounded live validation targets.
5. Build clean distributions with `python -m build`.
6. Validate metadata with `python -m twine check dist/*`.
7. Install the wheel in a fresh virtual environment and run all three command help checks.
8. Commit the release metadata and create an annotated `vX.Y.Z` tag.
9. Push the commit and tag, then publish a non-prerelease GitHub Release from that tag.
10. Approve the protected `pypi` deployment. The release workflow builds and publishes the distributions.
11. Install the package from PyPI in a fresh environment and run a final smoke test.

GitHub prereleases are built as workflow artifacts but are not published to production PyPI.
