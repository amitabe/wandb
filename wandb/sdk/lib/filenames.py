#
import os
from typing import Callable, Generator

WANDB_DIRS = ("wandb", ".wandb")

CONFIG_FNAME = "config.yaml"
OUTPUT_FNAME = "output.log"
DIFF_FNAME = "diff.patch"
SUMMARY_FNAME = "wandb-summary.json"
METADATA_FNAME = "wandb-metadata.json"
REQUIREMENTS_FNAME = "requirements.txt"
HISTORY_FNAME = "wandb-history.jsonl"
EVENTS_FNAME = "wandb-events.jsonl"
JOBSPEC_FNAME = "wandb-jobspec.json"
CONDA_ENVIRONMENTS_FNAME = "conda-environment.yaml"


def is_wandb_file(name: str) -> bool:
    return (
        name.startswith("wandb")
        or name == METADATA_FNAME
        or name == CONFIG_FNAME
        or name == REQUIREMENTS_FNAME
        or name == OUTPUT_FNAME
        or name == DIFF_FNAME
        or name == CONDA_ENVIRONMENTS_FNAME
    )


def filtered_dir(
    root: str, include_fn: Callable[[str], bool], exclude_fn: Callable[[str], bool]
) -> Generator[str, None, None]:
    """Simple generator to walk a directory.

    Args:
        root (str): The root directory to walk.
        include_fn (Callable[[str], bool]): A function that returns True if the file should be included.
        exclude_fn (Callable[[str], bool]): A function that returns True if the file should be excluded.

    Yields:
        Generator[str, None, None]: A generator of file paths.
    """

    for dirpath, dirs, files in os.walk(root):
        for dir in dirs:
            if exclude_fn(dir):
                dirs.remove(dir)
        for fname in files:
            file_path = os.path.join(dirpath, fname)
            if include_fn(file_path) and not exclude_fn(file_path):
                yield file_path


def exclude_wandb_fn(path: str) -> bool:
    return any(os.sep + wandb_dir + os.sep in path for wandb_dir in WANDB_DIRS)
