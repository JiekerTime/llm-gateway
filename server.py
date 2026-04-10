"""
llm-gateway — thin entry point for uvicorn.

All application logic lives in llm_gateway.app.
This file exists so that ``uvicorn server:app`` keeps working.
"""

from llm_gateway.app import app  # noqa: F401
