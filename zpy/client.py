import functools
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Union
from uuid import UUID

import requests.exceptions
from pydash import set_, unset, is_empty

from cli.utils import download_url
from zpy.client_util import (
    add_newline,
    get,
    post,
    to_query_param_value,
    convert_size,
    auth_header,
    clear_last_print,
    is_done, convert_to_rag_query_params,
)

_init_done: bool = False
_auth_token: str = ""
_base_url: str = ""
_version: str = ""
_versioned_url: str = ""
_project: Union[Dict, None] = None


class InvalidAuthTokenError(Exception):
    """Raised when an auth_token is missing or invalid."""
    pass


class InvalidProjectError(Exception):
    """Raised when accessing a project which does not exist or without appropriate access permissions."""
    pass


class ClientNotInitializedError(Exception):
    """Raised when trying to use functionality which is dependent on calling client.init()"""
    pass


def init(
        auth_token: str = '',
        project_uuid: str = '',
        base_url: str = "https://ragnarok.zumok8s.org",
        version: str = "v2",
):
    """
    Initializes the zpy client library.

    Args:
        auth_token (str): API auth_token. Required for all internal API calls.
        project_uuid (str): A valid uuid4 project id. Required to scope permissions for all requested API objects.
        base_url (str, optional): API url. Overridable for testing different environments.
        version (str, optional): API version. Overridable for testing different API versions. Defaults to the most
                                 recent version.
    Returns:
        None: No return value.
    """
    global _init_done, _auth_token, _base_url, _version, _versioned_url, _project
    _auth_token = auth_token
    _base_url = base_url
    _version = version
    _versioned_url = f"{base_url}/api/{version}"

    try:
        UUID(project_uuid, version=4)
    except ValueError:
        raise InvalidProjectError("Init failed: project_uuid must be a valid uuid4 string.") from None

    if is_empty(auth_token):
        raise InvalidAuthTokenError(
            f"Init failed: invalid auth token - find yours at {_base_url}/settings/auth-token.")

    try:
        _project = get(
            f"{_versioned_url}/projects/{project_uuid}",
            headers=auth_header(_auth_token),
        ).json()
        _init_done = True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            raise InvalidAuthTokenError(
                f"Init failed: invalid auth token - find yours at {_base_url}/settings/auth-token.") from None
        elif e.response.status_code == 404:
            raise InvalidProjectError("Init failed: you are not part of this project or it does not exist.") from None


def require_zpy_init(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not _init_done:
            raise ClientNotInitializedError(
                "Client not initialized: project and auth_token must be set via client.init().") from None
        return func(*args, **kwargs)

    return wrapper


class DatasetConfig:
    @require_zpy_init
    def __init__(self, sim_name: str):
        """
        Create a DatasetConfig. Used by zpy.preview and zpy.generate.

        Args:
            sim_name (str): Name of Sim.
        """
        self._sim = None
        self._config = {}

        unique_sim_filters = {
            "project": _project["id"],
            "name": sim_name,
        }
        sims = get(
            f"{_versioned_url}/sims/",
            params=unique_sim_filters,
            headers=auth_header(_auth_token),
        ).json()["results"]
        if len(sims) > 1:
            raise RuntimeError(
                f"Create DatasetConfig failed: Found more than 1 Sim for unique filters which should not be possible."
            )
        elif len(sims) == 1:
            print(f"Found Sim<{sim_name}> in Project<{_project['name']}>")
            self._sim = sims[0]
        else:
            raise RuntimeError(
                f"Create DatasetConfig failed: Could not find Sim<{sim_name}> in Project<{_project['name']}>."
            )

    @property
    def sim(self):
        """
        Returns:
            dict: The Sim object.
        """
        return self._sim

    @property
    def available_params(self):
        """
        Returns:
            dict: The configurable parameters on the Sim object.
        """
        return self._sim["run_kwargs"]

    @property
    def config(self):
        """
        Property which holds the parameters managed via DatasetConfig.set() and DatasetConfig.unset()

        Returns:
            dict: A dict representing a json object of gin config parameters.
        """
        return self._config

    def set(self, path: str, value: any):
        """Set a configurable parameter. Uses pydash.set_.

                https://pydash.readthedocs.io/en/latest/api.html#pydash.objects.set_

        Args:
            path: The json gin config path using pydash.set_ notation.
            value: The value to be set at the provided gin config path.
        Returns:
            None: No return value
        Examples:
            Given the following object, the value at path `a.b[0].c` is 1.

                    { a: { b: [ { c: 1 } ] } }
        """
        set_(self._config, path, value)

    def unset(self, path):
        """Remove a configurable parameter. Uses pydash.unset.

                https://pydash.readthedocs.io/en/latest/api.html#pydash.objects.unset

        Args:
            path: The json gin config path using pydash.set_ notation. Ex. given object { a: b: [{ c: 1 }]}, the value at path "a.b[0]c" is 1.
        Returns:
            None: No return value
        Examples:
            Given the following object, the value at path `a.b[0].c` is 1.

                    { a: { b: [ { c: 1 } ] } }
        """
        unset(self._config, path)


@add_newline
def preview(dataset_config: DatasetConfig, num_samples=10):
    """
    Generate a preview of output data for a given DatasetConfig.

    Args:
        dataset_config (DatasetConfig): Describes a Sim and its configuration. See DatasetConfig.
        num_samples (int): Number of preview samples to generate.
    Returns:
        dict[]: Sample images for the given configuration.
    """
    print(f"Generating preview:")

    filter_params = {
        "project": _project["id"],
        "sim": dataset_config.sim["id"],
        "state": "READY",
        "page-size": num_samples,
        **convert_to_rag_query_params(dataset_config.config, "config"),
    }
    simruns_res = get(
        f"{_versioned_url}/simruns/",
        params=filter_params,
        headers=auth_header(_auth_token),
    )
    simruns = simruns_res.json()["results"]

    if len(simruns) == 0:
        print(f"No preview available.")
        print("\t(no premade SimRuns matching filter)")
        return []

    file_query_params = {
        "run__sim": dataset_config.sim["id"],
        "path__icontains": ".rgb",
        "~path__icontains": ".annotated",
    }
    files_res = get(
        f"{_versioned_url}/files/",
        params=file_query_params,
        headers=auth_header(_auth_token),
    )
    files = files_res.json()["results"]
    if len(files) == 0:
        print(f"No preview available.")
        print("\t(no images found)")
        return []

    return files


@add_newline
def generate(
    name: str, dataset_config: DatasetConfig, num_datapoints: int, materialize=True
):
    """
    Generate a dataset.

    Args:
        name (str): Name of the dataset. Must be unique per Project.
        dataset_config (DatasetConfig): Specification for a Sim and its configurable parameters.
        num_datapoints (int): Number of datapoints in the dataset. A datapoint is an instant in time composed of all
                              the output images (rgb, iseg, cseg, etc) along with the annotations.
        materialize (bool): Optionally download the dataset.
    Returns:
        None: No return value.
    """
    dataset = post(
        f"{_versioned_url}/datasets/",
        data={
            "project": _project["id"],
            "name": name,
        },
        headers=auth_header(_auth_token),
    ).json()
    post(
        f"{_versioned_url}/datasets/{dataset['id']}/generate/",
        data={
            "project": _project["id"],
            "sim": dataset_config.sim["id"],
            "config": json.dumps(dataset_config.config),
            "amount": num_datapoints,
        },
        headers=auth_header(_auth_token),
    )

    print("Generating dataset:")
    print(json.dumps(dataset, indent=4, sort_keys=True))

    if materialize:
        print("Materialize requested, waiting until dataset finishes to download it.")
        dataset = get(
            f"{_versioned_url}/datasets/{dataset['id']}/",
            headers=auth_header(_auth_token),
        ).json()
        while not is_done(dataset["state"]):
            all_simruns_query_params = {"datasets": dataset["id"]}
            num_simruns = get(
                f"{_versioned_url}/simruns/",
                params=all_simruns_query_params,
                headers=auth_header(_auth_token),
            ).json()["count"]
            num_ready_simruns = get(
                f"{_versioned_url}/simruns/",
                params={**all_simruns_query_params, "state": "READY"},
                headers=auth_header(_auth_token),
            ).json()["count"]
            next_check_datetime = datetime.now() + timedelta(seconds=60)
            while datetime.now() < next_check_datetime:
                print(
                    "\r{}".format(
                        f"Dataset<{dataset['name']}> not ready for download in state {dataset['state']}. "
                        f"SimRuns READY: {num_ready_simruns}/{num_simruns}. "
                        f"Checking again in {(next_check_datetime - datetime.now()).seconds}s."
                    ),
                    end="",
                )
                time.sleep(1)

            clear_last_print()
            print("\r{}".format("Checking dataset...", end=""))
            dataset = get(
                f"{_versioned_url}/datasets/{dataset['id']}/",
                headers=auth_header(_auth_token),
            ).json()

        if dataset["state"] == "READY":
            print("Dataset is ready for download.")
            dataset_download_res = get(
                f"{_versioned_url}/datasets/{dataset['id']}/download/",
                headers=auth_header(_auth_token),
            ).json()
            name_slug = f"{dataset['name'].replace(' ', '_')}-{dataset['id'][:8]}.zip"
            # Throw it in /tmp for now I guess
            output_path = Path("/tmp") / name_slug
            print(
                f"Downloading {convert_size(dataset_download_res['size_bytes'])} dataset to {output_path}"
            )
            download_url(dataset_download_res["redirect_link"], output_path)
            print("Done.")
        else:
            print(
                f"Dataset is no longer running but cannot be downloaded with state = {dataset['state']}"
            )


class Dataset:
    _dataset = None

    @require_zpy_init
    def __init__(self, name: str = None, dataset: dict = None):
        """
        Construct a Dataset which is a local representation of a Dataset generated on the API.

        Args:
            name: If provided, Dataset will be automatically retrieved from the API.
            dataset: If Dataset has already been retrieved from the API, provide this.
        """
        self._name = name

        if dataset is not None:
            self._dataset = dataset
        else:
            unique_dataset_filters = {
                "project": _project["id"],
                "name": name,
            }
            datasets = get(
                f"{_versioned_url}/datasets/",
                params=unique_dataset_filters,
                headers=auth_header(_auth_token),
            ).json()["results"]
            self._dataset = datasets[0]

    @property
    def id(self):
        """
        Returns:
            str: The Dataset's unique identifier.
        """
        return self._dataset["id"]

    @property
    def name(self):
        """
        Returns:
            str: The Dataset's name.
        """
        return self._name

    @property
    def state(self):
        """
        Returns:
            str: The Dataset's state.
        """
        if not self._dataset:
            print("Dataset needs to be generated before you can access its state.")
        return self._dataset["state"]
