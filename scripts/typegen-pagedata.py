#!/usr/bin/env python3
"""Export the Pydantic page-data models as JSON Schema; the Vite
page-data-types plugin (and `just types-pagedata`) turn them into
frontend/src/page_data/*.types.ts."""

import json
from pathlib import Path

from datasette_otel_viewer.page_data import __exports__

for model in __exports__:
    out = Path("frontend/src/page_data") / f"{model.__name__}_schema.json"
    out.write_text(json.dumps(model.model_json_schema(), indent=2))
    print(f"Wrote {out}")
