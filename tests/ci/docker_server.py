#!/usr/bin/env python

# here
import argparse
import json
import logging
import subprocess
from os import path as p, makedirs
from typing import List, Tuple

from docker_images_check import DockerImage
from env_helper import RUNNER_TEMP
from get_robot_token import get_parameter_from_ssm
from version_helper import get_version_from_repo, validate_version

TEMP_PATH = p.join(RUNNER_TEMP, "docker_images_check")
BUCKETS = {"amd64": "package_release", "arm64": "package_aarch64"}


class DelOS(argparse.Action):
    def __call__(self, _, namespace, __, option_string=None):
        no_build = self.dest[3:] if self.dest.startswith("no_") else self.dest
        if no_build in namespace.os:
            namespace.os.remove(no_build)


def version_arg(version: str) -> str:
    try:
        validate_version(version)
        return version
    except ValueError as e:
        raise argparse.ArgumentTypeError(e)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="A program to build clickhouse-server image, both alpine and "
        "ubuntu versions",
    )

    parser.add_argument(
        "--version",
        type=version_arg,
        default=get_version_from_repo().string,
        help="a version to build",
    )
    parser.add_argument(
        "--release-type",
        type=str,
        choices=("latest", "major", "minor", "patch", "head"),
        default="patch",
        help="version part that will be updated when '--version' is set",
    )
    parser.add_argument(
        "--image-path",
        type=str,
        default="docker/server",
        help="a path to docker context directory",
    )
    parser.add_argument(
        "--image-repo",
        type=str,
        default="clickhouse/clickhouse-server",
        help="image name on docker hub",
    )
    parser.add_argument(
        "--bucket-prefix",
        help="if set, then is used as source for deb and tgz files",
    )
    parser.add_argument("--push", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-push-images",
        action="store_false",
        dest="push",
        default=argparse.SUPPRESS,
        help="don't push images to docker hub",
    )
    parser.add_argument("--os", default=["ubuntu", "alpine"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-ubuntu",
        action=DelOS,
        nargs=0,
        default=argparse.SUPPRESS,
        help="don't build ubuntu image",
    )
    parser.add_argument(
        "--no-alpine",
        action=DelOS,
        nargs=0,
        default=argparse.SUPPRESS,
        help="don't build alpine image",
    )

    return parser.parse_args()


def build_and_push_image(
    image: DockerImage, push: bool, bucket_prefix: str, os: str, tag: str, version: str
) -> List[Tuple[str, str]]:
    result = []
    if os != "ubuntu":
        tag += f"-{os}"
    init_args = ["docker", "buildx", "build"]
    if push:
        init_args.append("--push")
        init_args.append("--output=type=image,push-by-digest=true")
        init_args.append(f"--tag={image.repo}")
    else:
        init_args.append("--output=type=docker")

    # `docker buildx build --load` does not support multiple images currently
    # images must be built separately and merged together with `docker manifest`
    digests = []
    for arch in BUCKETS:
        arch_tag = f"{tag}-{arch}"
        metadata_path = p.join(TEMP_PATH, arch_tag)
        dockerfile = p.join(image.full_path, f"Dockerfile.{os}")
        cmd_args = list(init_args)
        cmd_args.extend(buildx_args(bucket_prefix, arch))
        if not push:
            cmd_args.append(f"--tag={image.repo}:{arch_tag}")
        cmd_args.extend(
            [
                f"--metadata-file={metadata_path}",
                f"--build-arg=VERSION='{version}'",
                "--progress=plain",
                f"--file={dockerfile}",
                image.full_path,
            ]
        )
        cmd = " ".join(cmd_args)
        logging.info("Building image %s:%s for arch %s: %s", image.repo, tag, arch, cmd)
        with subprocess.Popen(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        ) as process:
            for line in process.stdout:  # type: ignore
                print(line, end="")
            retcode = process.wait()
            if retcode != 0:
                result.append((f"{image.repo}:{tag}-{arch}", "FAIL"))
                return result
            result.append((f"{image.repo}:{tag}-{arch}", "OK"))
            with open(metadata_path, "rb") as m:
                metadata = json.load(m)
                digests.append(metadata["containerimage.digest"])
    if push:
        cmd = (
            "docker buildx imagetools create "
            f"--tag {image.repo}:{tag} {' '.join(digests)}"
        )
        logging.info("Pushing merged %s:%s image: %s", image.repo, tag, cmd)
        with subprocess.Popen(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        ) as process:
            for line in process.stdout:  # type: ignore
                print(line, end="")
            retcode = process.wait()
            if retcode != 0:
                result.append((f"{image.repo}:{tag}", "FAIL"))
    else:
        logging.info(
            "Merging is available only on push, separate %s images are created",
            f"{image.repo}:{tag}-$arch",
        )

    return result


def buildx_args(bucket_prefix: str, arch: str) -> List[str]:
    args = [f"--platform=linux/{arch}"]
    if bucket_prefix:
        url = p.join(bucket_prefix, BUCKETS[arch])  # to prevent a double //
        args.append(f"--build-arg=REPOSITORY='{url}'")
        args.append(f"--build-arg=deb_location_url='{url}'")
    return args


def gen_tags(version: str, release_type: str) -> List[str]:
    """
    22.2.2.2 + latest:
    - latest
    - 22
    - 22.2
    - 22.2.2
    - 22.2.2.2
    22.2.2.2 + major:
    - 22
    - 22.2
    - 22.2.2
    - 22.2.2.2
    22.2.2.2 + minor:
    - 22.2
    - 22.2.2
    - 22.2.2.2
    22.2.2.2 + patch:
    - 22.2.2
    - 22.2.2.2
    22.2.2.2 + head:
    - head
    """
    parts = version.split(".")
    tags = []
    if release_type == "latest":
        tags.append(release_type)
        for i in range(len(parts)):
            tags.append(".".join(parts[: i + 1]))
    elif release_type == "major":
        for i in range(len(parts)):
            tags.append(".".join(parts[: i + 1]))
    elif release_type == "minor":
        for i in range(1, len(parts)):
            tags.append(".".join(parts[: i + 1]))
    elif release_type == "patch":
        for i in range(2, len(parts)):
            tags.append(".".join(parts[: i + 1]))
    elif release_type == "head":
        tags.append(release_type)
    else:
        raise ValueError(f"{release_type} is not valid release part")
    return tags


def main():
    logging.basicConfig(level=logging.INFO)
    makedirs(TEMP_PATH, exist_ok=True)
    args = parse_args()
    if args.push:
        subprocess.check_output(  # pylint: disable=unexpected-keyword-arg
            "docker login --username 'robotclickhouse' --password-stdin",
            input=get_parameter_from_ssm("dockerhub_robot_password"),
            encoding="utf-8",
            shell=True,
        )
    image = DockerImage(args.image_path, args.image_repo, False)
    tags = gen_tags(args.version, args.release_type)
    logging.info("Following tags will be created: %s", ", ".join(tags))
    for os in args.os:
        for tag in tags:
            build_and_push_image(
                image, args.push, args.bucket_prefix, os, tag, args.version
            )


if __name__ == "__main__":
    main()
