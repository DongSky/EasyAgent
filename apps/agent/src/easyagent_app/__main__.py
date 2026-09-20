import argparse
import os


def main():
    from . import create_app
    import uvicorn

    parser = argparse.ArgumentParser(description="EasyAgent browser app")
    parser.add_argument("--backend", default=os.environ.get("EAH_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    uvicorn.run(create_app(args.backend), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
