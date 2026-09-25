"""plugins.datasette-otel-viewer is validated at startup: typos and wrong
types stop the instance instead of silently falling back to defaults."""

import pytest
from datasette.utils import StartupError
from pydantic import ValidationError

from datasette_otel_viewer.config import OtelViewerConfig


def test_defaults():
    config = OtelViewerConfig()
    assert config.self_traces is True
    assert config.public_viewer is False
    assert config.retention_hours == 72
    assert config.db_name == "otel"
    assert config.service_name is None


def test_every_field_is_documented():
    undocumented = [
        name
        for name, field in OtelViewerConfig.model_fields.items()
        if not field.description
    ]
    assert undocumented == []


@pytest.mark.parametrize(
    "bad",
    [
        {"public_veiwer": True},
        {"max_spans": "lots"},
        {"retention_hours": -1},
        {"db_name": ""},
        {"self_traces": "sometimes"},
    ],
)
def test_rejects(bad):
    with pytest.raises(ValidationError):
        OtelViewerConfig.model_validate(bad)


@pytest.mark.asyncio
async def test_typo_fails_startup(make_ds):
    with pytest.raises(StartupError) as e:
        await make_ds(public_veiwer=True)
    message = str(e.value)
    assert "plugins.datasette-otel-viewer" in message
    assert "public_veiwer: Extra inputs are not permitted" in message
