from collections.abc import Iterable, Mapping
from pathlib import Path

from tqdm import tqdm
import numpy as np
import pandas as pd
import xarray as xr

from .._utils import s3, nii
from ._utils import IDENTIFIER, BUCKET_NAME, BIBTEX
from ._stimulus_set import load_stimulus_metadata

RESOLUTION = "1pt8mm"
PREPROCESSING = "fithrf_GLMdenoise_RR"
N_SUBJECTS = 8
N_SESSIONS = (40, 40, 32, 30, 40, 32, 40, 30)
N_SESSIONS_HELD_OUT = 3
N_TRIALS_PER_SESSION = 750
ROI_SOURCES = {
    "surface": (
        "streams",
        "prf-visualrois",
        "prf-eccrois",
        "floc-places",
        "floc-faces",
        "floc-bodies",
        "floc-words",
        "HCP_MMP1",
        "Kastner2015",
        "nsdgeneral",
        "corticalsulc",
    ),
    "volume": ("MTL", "thalamus"),
}


def extract_stimulus_ids(subject: int) -> xr.DataArray:
    """Extract and format image IDs for all trials.

    :return: stimulus_ids seen at each trial with "session" and "trial" dimensions
    """
    metadata = load_stimulus_metadata()
    metadata = np.array(
        metadata.loc[
            :, [f"subject{subject + 1}_" in column for column in metadata.columns]
        ]
    )
    assert metadata.shape[-1] == 3
    indices = np.nonzero(metadata)
    trials = metadata[indices] - 1  # fix 1-indexing

    stimulus_ids_ = [f"image{idx:05}" for idx in indices[0]]
    session_ids = trials // N_TRIALS_PER_SESSION
    intra_session_trial_ids = trials % N_TRIALS_PER_SESSION

    stimulus_ids = xr.DataArray(
        data=np.full((max(N_SESSIONS), N_TRIALS_PER_SESSION), "", dtype="<U10"),
        dims=("session", "trial"),
    )
    stimulus_ids.values[session_ids, intra_session_trial_ids] = stimulus_ids_
    return stimulus_ids.assign_coords(
        {dim: (dim, np.arange(stimulus_ids.sizes[dim])) for dim in stimulus_ids.dims}
    )


def load_brain_mask(*, subject: int, resolution: str) -> xr.DataArray:
    """Load and format a Boolean brain mask for the functional data.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :return: Boolean brain mask
    """
    filepath = (
        Path("nsddata")
        / "ppdata"
        / f"subj{subject + 1:02}"
        / f"func{resolution}"
        / "brainmask.nii.gz"
    )
    s3.download(filepath, bucket=BUCKET_NAME)
    return nii.to_dataarray(Path(filepath), flatten=None).astype(bool, order="C")


def load_validity(*, subject: int, resolution: str) -> xr.DataArray:
    validity = []
    # TODO remove N_SESSIONS_HELD_OUT
    sessions = np.array(
        [
            f"nsd-{session}"
            for session in range(N_SESSIONS[subject] - N_SESSIONS_HELD_OUT)
        ]
        + ["prffloc"]
    )
    suffixes = np.array(
        [
            f"session{session + 1:02}"
            for session in range(N_SESSIONS[subject] - N_SESSIONS_HELD_OUT)
        ]
        + ["prffloc"]
    )
    for suffix in suffixes:
        filepath = (
            Path("nsddata")
            / "ppdata"
            / f"subj{subject + 1:02}"
            / f"func{resolution}"
            / f"valid_{suffix}.nii.gz"
        )
        s3.download(filepath, bucket=BUCKET_NAME)
        validity.append(
            nii.to_dataarray(Path(filepath), flatten=None)
            .expand_dims("session", axis=0)
            .astype(dtype=bool, order="C")
        )
    return xr.concat(validity, dim="session").assign_coords(
        {"session": ("session", sessions)}
    )


def load_betas(
    *,
    subject: int,
    resolution: str,
    preprocessing: str,
    neuroid_filter: Iterable[bool],
) -> xr.DataArray:
    """Load betas.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :param preprocessing: "fithrf_GLMdenoise_RR", "fithrf", or "assumehrf"
    :return: betas
    """
    betas = []
    stimulus_ids = extract_stimulus_ids(subject)

    # TODO remove N_SESSIONS_HELD_OUT
    sessions = np.arange(N_SESSIONS[subject] - N_SESSIONS_HELD_OUT)
    for session in tqdm(sessions, desc="session"):
        filepath = (
            Path("nsddata_betas")
            / "ppdata"
            / f"subj{subject + 1:02}"
            / f"func{resolution}"
            / f"betas_{preprocessing}"
            / f"betas_session{session + 1:02}.hdf5"
        )
        s3.download(filepath, bucket=BUCKET_NAME)
        betas_session = (
            xr.open_dataset(filepath)["betas"]
            .rename(
                {
                    "phony_dim_0": "presentation",
                    "phony_dim_1": "z",
                    "phony_dim_2": "y",
                    "phony_dim_3": "x",
                }
            )
            .load()
            .transpose("x", "y", "z", "presentation")
            .astype(dtype=np.int16, order="C")
        )

        betas_session = (
            betas_session.assign_coords(
                {
                    dim: (dim, np.arange(betas_session.sizes[dim], dtype=np.uint8))
                    for dim in ("x", "y", "z")
                }
                | {
                    "stimulus_id": (
                        "presentation",
                        stimulus_ids.sel(session=session).data,
                    ),
                    "session_id": (
                        "presentation",
                        session
                        * np.ones(betas_session.sizes["presentation"], dtype=np.uint8),
                    ),
                    "trial": (
                        "presentation",
                        np.arange(betas_session.sizes["presentation"], dtype=np.uint16),
                    ),
                }
            )
            .stack({"neuroid": ("x", "y", "z")}, create_index=False)
            .isel({"neuroid": neuroid_filter})
            .transpose("neuroid", "presentation")
            .astype(dtype=np.float32, order="C")
        )
        betas_session /= 300
        betas.append(betas_session)
    return xr.concat(betas, dim="presentation")


def load_ncsnr(
    *,
    subject: int,
    resolution: str,
    preprocessing: str,
    neuroid_filter: Iterable[bool],
) -> xr.DataArray:
    """Load and format noise-ceiling signal-to-noise ratios (NCSNR).

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :param preprocessing: "fithrf_GLMdenoise_RR", "fithrf", or "assumehrf
    :return: noise-ceiling SNRs
    """
    filepath = (
        Path("nsddata_betas")
        / "ppdata"
        / f"subj{subject + 1:02}"
        / f"func{resolution}"
        / f"betas_{preprocessing}"
        / f"ncsnr.nii.gz"
    )
    s3.download(filepath, bucket=BUCKET_NAME)
    return (
        nii.to_dataarray(filepath)
        .isel({"neuroid": neuroid_filter})
        .astype(dtype=np.float64, order="C")
    )


def load_structural_scans(
    *,
    subject: int,
    resolution: str,
) -> xr.DataArray:
    """Load and format the structural scans registered to the functional data.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :return: structural scans
    """
    scans = []
    sequences = np.array(("T1", "T2", "SWI", "TOF"))
    for sequence in sequences:
        filepath = (
            Path("nsddata")
            / "ppdata"
            / f"subj{subject + 1:02}"
            / f"func{resolution}"
            / f"{sequence}_to_func{resolution}.nii.gz"
        )
        s3.download(filepath, bucket=BUCKET_NAME)
        scans.append(
            nii.to_dataarray(filepath, flatten=None)
            .expand_dims("sequence", axis=0)
            .astype(dtype=np.uint16, order="C")
        )
    return xr.concat(scans, dim="sequence").assign_coords(
        {"sequence": ("sequence", sequences)}
    )


def load_rois(
    *,
    subject: int,
    sources: Mapping[str, Iterable[str]],
    resolution: str,
    neuroid_filter: Iterable[bool],
) -> xr.DataArray:
    """Load the ROI masks for a subject.

    :param subject: subject ID
    :param rois: dict with keys "surface" and "volume", each mapping to an Iterable of ROIs
    :param resolution: "1pt8mm" or "1mm"
    :return: ROI masks
    """
    rois = []
    for space, sources in sources.items():
        for source in sources:
            if space == "surface":
                filepath = (
                    Path("nsddata")
                    / "freesurfer"
                    / f"subj{subject + 1:02}"
                    / "label"
                    / f"{source}.mgz.ctab"
                )
            elif space == "volume":
                filepath = Path("nsddata") / "templates" / f"{source}.ctab"
            s3.download(filepath, bucket=BUCKET_NAME)

            mapping = (
                pd.read_csv(
                    filepath,
                    delim_whitespace=True,
                    names=("label", "roi"),
                )
                .set_index("roi")
                .to_dict()["label"]
            )

            volumes = {}
            for hemisphere in ("lh", "rh"):
                filepath = (
                    Path("nsddata")
                    / "ppdata"
                    / f"subj{subject + 1:02}"
                    / f"func{resolution}"
                    / "roi"
                    / f"{hemisphere}.{source}.nii.gz"
                )
                s3.download(filepath, bucket=BUCKET_NAME)
                volumes[hemisphere] = nii.to_dataarray(filepath)

                for roi, label in mapping.items():
                    if label != 0:
                        rois.append(
                            (volumes[hemisphere] == label)
                            .expand_dims(roi=[roi], axis=0)
                            .assign_coords(
                                {
                                    "source": ("roi", [source]),
                                    "space": ("roi", [space]),
                                    "hemisphere": ("roi", [hemisphere[0]]),
                                },
                            )
                            .isel({"neuroid": neuroid_filter})
                            .astype(bool, order="C")
                        )
    rois = xr.concat(rois, dim="roi")
    rois["label"] = rois["roi"].astype(str)
    return rois.drop("roi")


def load_receptive_fields(
    subject: int, resolution: str, neuroid_filter: Iterable[bool]
) -> xr.DataArray:
    """Load population receptive field mapping data.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :return: pRF data
    """
    prf_data = []
    quantities = np.array(
        (
            "angle",
            "eccentricity",
            "exponent",
            "gain",
            "R2",
            "size",
        )
    )
    for quantity in quantities:
        filepath = (
            Path("nsddata")
            / "ppdata"
            / f"subj{subject + 1:02}"
            / f"func{resolution}"
            / f"prf_{quantity}.nii.gz"
        )
        s3.download(filepath, bucket=BUCKET_NAME)
        prf_data.append(
            nii.to_dataarray(filepath)
            .expand_dims("quantity", axis=0)
            .isel({"neuroid": neuroid_filter})
            .astype(dtype=np.float64, order="C")
        )
    return xr.concat(prf_data, dim="quantity").assign_coords(
        {"quantity": ("quantity", quantities)}
    )


def load_functional_contrasts(
    subject: int, resolution: str, neuroid_filter: Iterable[bool]
) -> xr.DataArray:
    """Load functional contrasts.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :return: functional contrasts
    """
    categories = {}
    for filename in ("domains", "categories"):
        filepath = Path("nsddata") / "experiments" / "floc" / f"{filename}.tsv"
        s3.download(filepath, bucket=BUCKET_NAME)

        categories[filename] = list(pd.read_csv(filepath, sep="\t").iloc[:, 0].values)

    categories_combined = categories["domains"] + categories["categories"]
    superordinate = np.array(
        [
            True if category in categories["domains"] else False
            for category in categories_combined
        ],
        dtype=bool,
    )
    floc_data = {}
    for category in categories_combined:
        floc_data[category] = []
        for metric in ("tval", "anglemetric"):
            filepath = (
                Path("nsddata")
                / "ppdata"
                / f"subj{subject + 1:02}"
                / f"func{resolution}"
                / f"floc_{category}{metric}.nii.gz"
            )
            s3.download(filepath, bucket=BUCKET_NAME)

            floc_data[category].append(
                nii.to_dataarray(filepath)
                .expand_dims(
                    {
                        "category": [category],
                        "metric": [metric],
                    },
                    axis=(0, 1),
                )
                .isel({"neuroid": neuroid_filter})
                .astype(np.float64, order="C")
            )
        floc_data[category] = xr.concat(floc_data[category], dim="metric")
    floc_data = xr.concat(list(floc_data.values()), dim="category")
    return floc_data.assign_coords(
        {
            coord: (coord, floc_data[coord].astype(str).values)
            for coord in ("category", "metric")
        }
        | {"superordinate": ("category", superordinate)}
    )


def create_data_assembly_subject(
    subject: int,
    resolution: str,
    preprocessing: str,
) -> xr.Dataset:
    """Create a subject's data assembly.

    :param subject: subject ID
    :param resolution: "1pt8mm" or "1mm"
    :param preprocessing: "fithrf_GLMdenoise_RR", "fithrf", or "assumehrf"
    :return: data assembly
    """
    validity = load_validity(subject=subject, resolution=resolution)
    brain_mask = load_brain_mask(subject=subject, resolution=resolution)
    neuroid_filter = np.logical_and(
        np.any(
            validity.stack({"neuroid": ("x", "y", "z")}, create_index=False).values,
            axis=0,
        ),
        brain_mask.stack({"neuroid": ("x", "y", "z")}, create_index=False).values,
    )

    variables = {
        "betas": load_betas(
            subject=subject,
            resolution=resolution,
            preprocessing=preprocessing,
            neuroid_filter=neuroid_filter,
        ),
        "ncsnr": load_ncsnr(
            subject=subject,
            resolution=resolution,
            preprocessing=preprocessing,
            neuroid_filter=neuroid_filter,
        ),
        "functional_contrasts": load_functional_contrasts(
            subject=subject,
            resolution=resolution,
            neuroid_filter=neuroid_filter,
        ),
        "receptive_fields": load_receptive_fields(
            subject=subject,
            resolution=resolution,
            neuroid_filter=neuroid_filter,
        ),
        "structural_scans": load_structural_scans(
            subject=subject, resolution=resolution
        ).rename({w: f"{w}_" for w in ("x", "y", "z")}),
        "brain_mask": brain_mask.rename({w: f"{w}_" for w in ("x", "y", "z")}),
        "validity": validity.rename({w: f"{w}_" for w in ("x", "y", "z")}),
        "rois": load_rois(
            subject=subject,
            sources=ROI_SOURCES,
            resolution=resolution,
            neuroid_filter=neuroid_filter,
        ),
    }
    return xr.Dataset(data_vars=variables)


def create_data_assembly(
    resolution: str = RESOLUTION,
    preprocessing: str = PREPROCESSING,
) -> Path:
    """Create the full data assembly.

    :param resolution: "1pt8mm" or "1mm", defaults to "1pt8mm"
    :param preprocessing: "fithrf_GLMdenoise_RR", "fithrf", or "assumehrf, defaults to "fithrf_GLMdenoise_RR"
    :return: data assembly
    """
    filepath = Path(f"{IDENTIFIER}.nc")
    xr.Dataset(
        attrs={
            "identifier": IDENTIFIER,
            "stimulus_set_identifier": IDENTIFIER,
            "preprocessing": preprocessing,
            "resolution": resolution,
            "reference": BIBTEX,
        },
    ).to_netcdf(filepath, mode="a", group="/")

    for subject in tqdm(range(N_SUBJECTS), desc="subject", leave=False):
        create_data_assembly_subject(
            subject=subject, resolution=resolution, preprocessing=preprocessing
        ).to_netcdf(filepath, mode="a", group=f"/subject-{subject}")

    return filepath
