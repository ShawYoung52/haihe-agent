from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    host = os.environ.get("WMS_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("WMS_PORT", "8008"))
    uvicorn.run("wms_vector_service.app:app", host=host, port=port, reload=False)

