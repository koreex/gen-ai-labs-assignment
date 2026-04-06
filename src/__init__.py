# SQL Agent package

try:
    from dotenv import load_dotenv  # type: ignore
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()
