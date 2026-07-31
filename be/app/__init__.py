"""SanMai AI core FastAPI application package.

The public core ships an intentionally empty app exposing only liveness
(``/healthz``) and readiness (``/readyz``) probes. Business routers are added
by overlays / feature modules on top of this seam.
"""
