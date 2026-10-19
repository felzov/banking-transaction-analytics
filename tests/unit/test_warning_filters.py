"""The pytest config turns deprecation warnings into errors (filterwarnings in pyproject.toml).

Starlette, FastAPI and Uvicorn subclass UserWarning, not DeprecationWarning, so each is listed
there by name. Without its filter entry, warn() only records a warning and these tests fail with
"DID NOT RAISE".
"""

import warnings

import pytest
from fastapi.exceptions import FastAPIDeprecationWarning
from starlette.exceptions import StarletteDeprecationWarning
from uvicorn.config import UvicornDeprecationWarning


@pytest.mark.parametrize(
    "category",
    [
        DeprecationWarning,
        PendingDeprecationWarning,
        StarletteDeprecationWarning,
        FastAPIDeprecationWarning,
        UvicornDeprecationWarning,
    ],
    ids=lambda category: category.__name__,
)
def test_deprecation_warning_fails_the_test(category: type[Warning]) -> None:
    with pytest.raises(category):
        warnings.warn("deprecated", category, stacklevel=1)
