"""Version 1 of the public API.

Empty by design. The template has no business endpoints; a service built from
it adds routers here. The version lives in the URL path from day one because
retrofitting a version prefix onto live clients is far more expensive than
carrying an unused one.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1")
