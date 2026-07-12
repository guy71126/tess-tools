# Contributing

Bug reports, provider-coverage reports, documentation improvements, and focused pull requests are welcome.

## Development setup

Create a virtual environment and install the project in editable mode:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mast,fits]"
```

Run the offline suite before submitting a pull request:

```powershell
python -m unittest discover -s tests -v
python -m compileall tess_tools tests
```

The offline suite must not make network requests. Live MAST and TESSCut checks belong in the bounded validation harness and must remain opt-in:

```powershell
python -m tess_tools.live_validate validation/live_targets.json --max-targets 1 --refresh
```

## Pull requests

- Keep changes focused and preserve the documented JSON and CSV contracts unless the change explicitly versions them.
- Add or update offline tests for behavior changes.
- Document new providers, output fields, quality policies, and command-line options.
- Do not commit downloaded TESS products, credentials, caches, build artifacts, or virtual environments.

## Provider reports

For missing or misclassified products, include a public TIC ID or sky position, expected provider and product family, relevant sector, command used, and redacted output. Never include authentication tokens or private data.
