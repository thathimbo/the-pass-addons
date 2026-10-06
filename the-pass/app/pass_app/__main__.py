import uvicorn

from .config import Settings
from .web import create_app


def main():
    s = Settings.from_env()
    print(f"The Pass on http://{s.host}:{s.port}  (projector: /  scanner: /scanner  desk: /desk  API docs: /docs)")
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
