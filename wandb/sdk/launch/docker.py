import getpass
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Union


from docker.models.resource import Model  # type: ignore
from dockerpycreds.utils import find_executable  # type: ignore
from six.moves import shlex_quote
import wandb
from wandb.apis.internal import Api
from wandb.env import DOCKER
from wandb.errors import ExecutionError, LaunchError
from wandb.util import get_module

from . import _project_spec
from .utils import _is_wandb_dev_uri, _is_wandb_local_uri
from ..lib.git import GitRepo

_logger = logging.getLogger(__name__)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.wandb-autogenerated"
_PROJECT_TAR_ARCHIVE_NAME = "wandb-project-docker-build-context"


def validate_docker_installation() -> None:
    """Verify if Docker is installed on host machine."""
    if not find_executable("docker"):
        raise ExecutionError(
            "Could not find Docker executable. "
            "Ensure Docker is installed as per the instructions "
            "at https://docs.docker.com/install/overview/."
        )


def validate_docker_env(launch_project: _project_spec.LaunchProject) -> None:
    """Ensure project has a docker image associated with it."""
    if not launch_project.docker_image:
        raise ExecutionError(
            "LaunchProject with docker environment must specify the docker image "
            "to use via 'docker_image' field."
        )


def generate_docker_image(
    launch_project: _project_spec.LaunchProject, entry_cmd: str
) -> str:
    """Uses project and entry point to generate the docker image."""
    path = launch_project.project_dir
    # this check will always pass since the dir attribute will always be populated
    # by _fetch_project_local
    get_module(
        "repo2docker",
        required='wandb launch requires additional dependencies, install with pip install "wandb[launch]"',
    )
    assert isinstance(path, str)
    cmd: Sequence[str] = [
        "jupyter-repo2docker",
        "--no-run",
        "--user-id={}".format(launch_project.docker_user_id),
        path,
        '"{}"'.format(entry_cmd),
    ]

    _logger.info(
        "Generating docker image from git repo or finding image if it already exists.........."
    )
    wandb.termlog("Generating docker image, this may take a few minutes")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = ""
    # this will always pass, repo2docker writes to stderr.
    assert process.stderr
    for line in process.stderr:
        decoded_line = line.decode("utf-8")
        if decoded_line.endswith("\n"):
            decoded_line = decoded_line.rstrip("\n")
        print(decoded_line)  # don't spam termlog with all this
        stderr = stderr + decoded_line
    process.wait()
    image_id: List[str] = re.findall(r"Successfully tagged (.+):latest", stderr)
    if not image_id:
        image_id = re.findall(r"Reusing existing image \((.+)\)", stderr)
    if not image_id:
        raise LaunchError("error running repo2docker: {}".format(stderr))
    os.environ[DOCKER] = image_id[0]
    return image_id[0]


def pull_docker_image(docker_image: str) -> None:
    """Pulls the requested docker image."""
    import docker  # type: ignore

    info = docker_image.split(":")
    client = docker.from_env()
    try:
        if len(info) == 1:
            client.images.pull(info[0])
        else:
            client.images.pull(info[0], tag=info[1])
    except docker.errors.APIError as e:
        raise LaunchError("Docker server returned error: {}".format(e))


def build_docker_image(
    launch_project: _project_spec.LaunchProject, base_image: str, copy_code: bool,
) -> Union[Model, Any]:
    """Build a docker image containing the project in `work_dir`, using the base image.

    Arguments:
    launch_project: LaunchProject class instance
    base_image: base_image to build the docker image off of
    api: instance of wandb.apis.internal Api
    copy_code: boolean indicating if code should be copied into the docker container

    Returns:
        A `Model` instance of the docker image.

    Raises:
        LaunchError: if there is an issue communicating with the docker client
    """
    import docker

    image_name = "wandb_launch_{}".format(launch_project.run_id)
    image_uri = _get_docker_image_uri(
        name=image_name, work_dir=launch_project.project_dir
    )
    copy_code_line = ""
    workdir_line = ""
    copy_config_line = ""
    workdir = os.path.join("/home/", getpass.getuser())
    if launch_project.override_config:
        copy_config_line = "COPY {}/{} {}\n".format(
            _PROJECT_TAR_ARCHIVE_NAME, _project_spec.DEFAULT_CONFIG_PATH, workdir
        )
    if copy_code:
        copy_code_line = "COPY {}/ {}\n".format(_PROJECT_TAR_ARCHIVE_NAME, workdir)
        workdir_line = "WORKDIR {}\n".format(workdir)
    name_line = ""
    if launch_project.name:
        name_line = "ENV WANDB_NAME={wandb_name}\n"

    dockerfile = (
        "FROM {imagename}\n"
        "{copy_config_line}"
        "{copy_code_line}"
        "{workdir_line}"
        "{name_line}"
    ).format(
        imagename=base_image,
        copy_config_line=copy_config_line,
        copy_code_line=copy_code_line,
        workdir_line=workdir_line,
        name_line=name_line,
    )
    build_ctx_path = _create_docker_build_ctx(
        launch_project.project_dir,
        dockerfile,
        launch_project._runtime,
        launch_project.override_config,
    )
    with open(build_ctx_path, "rb") as docker_build_ctx:
        _logger.info("=== Building docker image %s ===", image_uri)
        #  TODO: replace with shelling out
        dockerfile = posixpath.join(
            _PROJECT_TAR_ARCHIVE_NAME, _GENERATED_DOCKERFILE_NAME
        )
        # TODO: remove the dependency on docker / potentially just do the append builder
        # found at: https://github.com/google/containerregistry/blob/master/client/v2_2/append_.py
        client = docker.from_env()
        try:
            image, _ = client.images.build(
                tag=image_uri,
                forcerm=True,
                dockerfile=dockerfile,
                fileobj=docker_build_ctx,
                custom_context=True,
                encoding="gzip",
            )
        except ConnectionError as e:
            raise LaunchError("Error communicating with docker client: {}".format(e))

    try:
        os.remove(build_ctx_path)
    except Exception:
        _logger.info(
            "Temporary docker context file %s was not deleted.", build_ctx_path
        )
    return image


def get_docker_command(
    image: Union[Model, Any],
    launch_project: _project_spec.LaunchProject,
    api: Api,
    docker_args: Dict[str, Any] = None,
) -> List[str]:
    """Constructs the docker command using the image and docker args.

    Arguments:
    image: a Docker image to be run
    launch_project: an instance of LaunchProject
    api: an instance of wandb.apis.internal Api
    docker_args: a dictionary of additional docker args for the command
    """
    docker_path = "docker"
    cmd: List[Any] = [docker_path, "run", "--rm"]

    if _is_wandb_local_uri(api.settings("base_url")) and sys.platform == "darwin":
        _, _, port = _, _, port = api.settings("base_url").split(":")
        base_url = "http://host.docker.internal:{}".format(port)
    elif _is_wandb_dev_uri(api.settings("base_url")):
        base_url = "http://host.docker.internal:9002"
    else:
        base_url = api.settings("base_url")

    cmd += [
        "--env",
        f"WANDB_BASE_URL={base_url}",
        "--env",
        f"WANDB_API_KEY={api.api_key}",
        "--env",
        f"WANDB_PROJECT={launch_project.target_project}",
        "--env",
        f"WANDB_ENTITY={launch_project.target_entity}",
        "--env",
        f"WANDB_LAUNCH={True}",
        "--env",
        f"WANDB_LAUNCH_CONFIG_PATH={_project_spec.DEFAULT_CONFIG_PATH}",
        "--env",
        f"WANDB_RUN_ID={launch_project.run_id or None}",
        "--env",
        f"WANDB_DOCKER={launch_project.docker_image}",
    ]

    if docker_args:
        for name, value in docker_args.items():
            # Passed just the name as boolean flag
            if isinstance(value, bool) and value:
                if len(name) == 1:
                    cmd += ["-" + name]
                else:
                    cmd += ["--" + name]
            else:
                # Passed name=value
                if len(name) == 1:
                    cmd += ["-" + name, value]
                else:
                    cmd += ["--" + name, value]

    cmd += [image.tags[0]]
    return [shlex_quote(c) for c in cmd]


def _get_docker_image_uri(name: str, work_dir: str) -> str:
    """Returns a Docker image URI based on the git hash of the specified working directory.

    Arguments:
    name: The URI of the Docker repository with which to tag the image. The
        repository URI is used as the prefix of the image URI.
    work_dir: Path to the working directory in which to search for a git commit hash
    """
    name = name.replace(" ", "-") if name else "docker-project"
    # Optionally include first 7 digits of git SHA in tag name, if available.

    git_commit = GitRepo(work_dir).last_commit
    version_string = ":" + str(git_commit[:7]) if git_commit else ""
    return name + version_string


def _create_docker_build_ctx(
    work_dir: str,
    dockerfile_contents: str,
    runtime: Optional[str],
    run_config: Dict[str, Any],
) -> str:
    """Creates build context tarfile containing Dockerfile and project code, returning path to tarfile."""
    directory = tempfile.mkdtemp()
    try:
        dst_path = os.path.join(directory, "wandb-project-contents")
        shutil.copytree(src=work_dir, dst=dst_path)
        if run_config:
            config_path = os.path.join(dst_path, _project_spec.DEFAULT_CONFIG_PATH)
            with open(config_path, "w") as fp:
                json.dump(run_config, fp)
        if runtime:
            runtime_path = os.path.join(dst_path, "runtime.txt")
            with open(runtime_path, "w") as fp:
                fp.write(runtime)

        with open(os.path.join(dst_path, _GENERATED_DOCKERFILE_NAME), "w") as handle:
            handle.write(dockerfile_contents)
        _, result_path = tempfile.mkstemp()
        wandb.util.make_tarfile(
            output_filename=result_path,
            source_dir=dst_path,
            archive_name=_PROJECT_TAR_ARCHIVE_NAME,
        )
    finally:
        shutil.rmtree(directory)
    return result_path