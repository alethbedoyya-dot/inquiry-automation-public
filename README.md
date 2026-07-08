# Inquiry Automation

A three-phase Selenium automation project for inquiry and quotation workflows.

The public repository is a configurable template. Private URLs, supplier names,
account credentials, runtime JSON files, logs, caches, and generated documents
are intentionally excluded from Git.

## Install

```bash
python -m pip install -r requirements.txt
python setup.py
```

## Workflow

1. Phase 1: filter source rows in OMS and create inquiry forms in the parts portal.
2. Manual step: wait for inquiry approval.
3. Phase 2: resume approved inquiries, add items to cart, and generate quotations.
4. Phase 3: export PDF/Excel files, fill the supplier quote sheet, and import it back to OMS.

## Configuration

Copy the template and fill in your private deployment values:

```bash
copy user_config.example.py user_config.py
```

Required account fields:

- `OMS_USERNAME`
- `SPAREPARTS_USERNAME`
- `SPAREPARTS_PASSWORD`

Deployment-specific fields:

- `OMS_HOME_URL`
- `OMS_URL`
- `SPAREPARTS_URL`
- `SPAREPARTS_HOST_KEYWORD`
- supplier names and supplier IDs used by your OMS environment

`user_config.py` is ignored by Git. Do not commit it.

## Run

GUI launcher:

```bash
python launcher.py
```

Command-line phases:

```bash
python main.py
python main.py --resume
python main.py --finalize --import-submit
python main.py --setup-config
```

Useful options:

- `--close-browser`: close Edge after the run.
- `--step-by-step`: pause before key steps for troubleshooting.
- `--inquiry-results PATH`: use a specific inquiry JSON file.
- `--finalize-only pdf|excel|fill|import`: run only one finalize step.

## Project Layout

- `main.py`: three-phase orchestration.
- `launcher.py`: Tkinter launcher.
- `config.py`: public defaults and local override loading.
- `modules/oms.py`: OMS entry class, login and filtering.
- `modules/oms_operations.py`: OMS attachment, email, delete, and supplier-extension helpers.
- `modules/oms_finalize.py`: OMS export/import helpers for phase 3.
- `modules/spareparts.py`: parts portal entry class and inquiry workflow.
- `modules/spareparts_cart.py`: cart and quantity alignment helpers.
- `modules/spareparts_quotation.py`: quotation generation and export helpers.
- `modules/quote_fill.py`: PDF parsing and Excel filling.
- `utils/`: browser, download, image, grouping, persistence, and logging helpers.

## Runtime Data

These files are generated locally and ignored by Git:

- `user_config.py`
- `oms_data.json`
- `inquiry_results.json`
- `inquiry_last.json`
- `finalize_results.json`
- `logs/`
- `cache/`
- `test_results/`
- output folders such as `sessions/` and `报价单/`

## Tests

Offline tests that do not require OMS login:

```bash
python -m unittest test_quote_fill.py
python -m unittest test_oms_grouping_email_filter.py
python -m unittest test_output_paths.py
```

Browser or live-environment tests should only be run in a configured environment:

```bash
python test_oms_flow.py
python test_cart_qty_align.py
```
