from pathlib import Path
import importlib.util
import py_compile


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    py_compile.compile(str(ROOT / "main.py"), doraise=True)
    for file in (ROOT / "core").glob("*.py"):
        py_compile.compile(str(file), doraise=True)
    print("py_compile ok")


if __name__ == "__main__":
    main()
