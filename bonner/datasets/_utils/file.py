from pathlib import Path
import zipfile
import tarfile
import requests
import uuid

from loguru import logger


def download(
    url: str,
    *,
    filepath: Path = None,
    stream: bool = True,
    allow_redirects: bool = True,
    chunk_size: int = 1024**2,
    force: bool = True,
) -> Path:
    if filepath is None:
        filepath = Path("/tmp") / f"{uuid.uuid4()}"
    elif filepath.exists():
        if not force:
            logger.debug(
                "Using previously downloaded file at"
                f" {filepath} instead of downloading from {url}"
            )
            return filepath
        else:
            filepath.unlink()

    logger.debug(f"Downloading from {url} to {filepath}")
    r = requests.Session().get(url, stream=stream, allow_redirects=allow_redirects)
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=chunk_size):
            f.write(chunk)

    return filepath


def untar(filepath: Path, *, extract_dir: Path = None, remove_tar: bool = True) -> Path:
    if extract_dir is None:
        extract_dir = Path("/tmp")

    logger.debug(f"Extracting from {filepath} to {extract_dir}")
    with tarfile.open(filepath) as tar:
        
        import os
        
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(tar, path=extract_dir)

    if remove_tar:
        logger.debug(f"Deleting {filepath} after extraction")
        filepath.unlink()

    return extract_dir


def unzip(filepath: Path, *, extract_dir: Path = None, remove_zip: bool = True) -> Path:
    if extract_dir is None:
        extract_dir = Path("/tmp")

    logger.debug(f"Extracting from {filepath} to {extract_dir}")
    with zipfile.ZipFile(filepath, "r") as f:
        f.extractall(extract_dir)

    if remove_zip:
        logger.debug(f"Deleting {filepath} after extraction")
        filepath.unlink()

    return extract_dir
