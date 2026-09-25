"""The ``plugins.datasette-otel-viewer`` config block, validated.

Every read of the plugin's config goes through ``get_config()``, and the
first one happens during startup, so a typo'd key or a wrong type stops
``datasette serve`` with a readable message instead of being silently ignored
(the old ``config.get(...)`` reads fell back to the default for any key they
did not know).
"""

from datasette.utils import StartupError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PLUGIN_NAME = "datasette-otel-viewer"


class OtelViewerConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    self_traces: bool = True
    """Store the spans this instance emits. False browses an existing store
    without recording. Only takes effect when this plugin installed the
    TracerProvider; see NOTES.md."""

    self_metrics: bool = True
    """Store this instance's own metrics, exported once a minute."""

    public_viewer: bool = False
    """Open the /-/otel pages and JSON API to everyone, without the
    datasette-otel-viewer permission. The raw otel tables stay gated."""

    retention_hours: float = Field(default=72, ge=0)
    """Whole traces, and metric points, older than this are pruned."""

    max_spans: int = Field(default=100_000, ge=0)
    """Past this many spans the oldest whole traces are pruned."""

    max_metric_points: int = Field(default=100_000, ge=0)
    """Past this many metric points the oldest are pruned."""

    db_name: str = Field(default="otel", min_length=1)
    """Name the store is attached under, as in /<db_name>/spans."""

    db_path: str = Field(default="otel.db", min_length=1)
    """SQLite file holding the store, created if missing. Relative paths
    resolve against the directory Datasette was started in."""

    service_name: str | None = None
    """service.name on self-recorded spans and metrics. Defaults to
    "datasette"."""


def get_config(datasette) -> OtelViewerConfig:
    """The validated config. A bad block raises StartupError, which the CLI
    prints without a traceback. Whichever read comes first reports it:
    prepare_connection reads db_name before any startup hook runs."""
    try:
        return OtelViewerConfig.model_validate(
            datasette.plugin_config(PLUGIN_NAME) or {}
        )
    except ValidationError as e:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc']) or '(config)'}: {err['msg']}"
            for err in e.errors()
        )
        raise StartupError(
            f"Invalid plugins.{PLUGIN_NAME} config:\n{problems}"
        ) from None
