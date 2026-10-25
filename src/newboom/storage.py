from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

STORAGE_PATH = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
STORAGE_PATH.mkdir(exist_ok=True)


def save_data_to_file(data: dict, data_type: str, device: str) -> str:
    """
    Save the given data to a JSON file in the storage directory.
    The filename is generated based on the hash of the data.
    """
    # Generate a unique filename based on the hash of the data
    data_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    filename = (
        STORAGE_PATH
        / f"{device}-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-{data_hash}.{data_type}.json"
    )

    # Save the data to the file
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

    return str(filename)


def load_data_from_file(filename: str) -> dict:
    """
    Load data from a JSON file in the storage directory.
    """
    filepath = STORAGE_PATH / filename
    if not filepath.exists():
        raise FileNotFoundError(f"No such file: {filepath}")

    with open(filepath, "r") as f:
        data = json.load(f)

    return data


def list_uploaded_files(
    start: date | None = None, end: date | None = None
) -> list[str]:
    """
    List all uploaded files in the storage directory.
    """
    files = [f.name for f in STORAGE_PATH.iterdir() if f.is_file()]
    if start is not None:
        files = [
            f
            for f in files
            if start <= datetime.strptime(f.split("-")[1], "%Y-%m-%d").date()
        ]
    if end is not None:
        files = [
            f
            for f in files
            if datetime.strptime(f.split("-")[1], "%Y-%m-%d").date() <= end
        ]
    return files
