from __future__ import annotations

import logging

import uvicorn

from doxatlas_data_api.config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    settings = get_settings()
    uvicorn.run("doxatlas_data_api.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
