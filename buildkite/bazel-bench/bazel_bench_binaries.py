#!/usr/bin/env python3
#
# Copyright 2019 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http:#www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This script is downloaded and executed by BuildKite when the pipeline starts.
Runs bazel-bench on the defined projects, on every platforms the project runs
on.
"""

import argparse
import bazelci
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import yaml
import math

# TMP has different values, depending on the platform.
TMP = tempfile.gettempdir()
# TODO(leba): Move this to a separate config file.
PROJECTS = [
    {
        "name": "Bazel",
        "storage_subdir": "bazel",
        "git_repository": "https://github.com/bazelbuild/bazel.git",
        "bazel_command": "build //src:bazel",
        "active": True,
    }
]
BAZEL_REPOSITORY = "https://github.com/bazelbuild/bazel.git"
DATA_DIRECTORY = os.path.join(TMP, ".bazel-bench", "out")
BAZEL_BENCH_RESULT_FILENAME = "perf_data.csv"
AGGR_JSON_PROFILES_FILENAME = "aggr_json_profiles.csv"
PLATFORMS_WHITELIST = ['macos', 'ubuntu1604', 'ubuntu1804', 'rbe_ubuntu1604']
REPORT_GENERATION_PLATFORM = 'ubuntu1804'
STARTER_JOB_PLATFORM = 'ubuntu1804'
# The path to the directory that stores the bazel binaries.

def _bazel_bench_env_setup_command(platform):
  bazel_bench_env_setup_py_url = (
      "https://raw.githubusercontent.com/joeleba/continuous-integration/turbine-bm/buildkite/bazel-bench/bazel_bench_env_setup.py?%s"
      % int(time.time())
  )
  download_command = 'curl -sS "%s" -o bazel_bench_env_setup.py' % bazel_bench_env_setup_py_url
  exec_command = "%s bazel_bench_env_setup.py --platform=%s --gs_uri=%s" % (
      bazelci.PLATFORMS[platform]["python"],
      platform,
      "gs://perf.bazel.build/bazelbins/*",
  )
  return [download_command, exec_command]


def _get_platforms(project_name, whitelist):
    """Get the platforms on which this project is run on BazelCI.
    Filter the results with a whitelist & remove duplicates.

    Args:
      project_name: a string: the name of the project. e.g. "Bazel".
      whitelist: a list of string denoting the whitelist of supported platforms.

    Returns:
      A set of string: the platforms for this project.
    """
    http_config = bazelci.DOWNSTREAM_PROJECTS_PRODUCTION[project_name]["http_config"]
    configs = bazelci.fetch_configs(http_config, None)
    tasks = configs["tasks"]
    ci_platforms_for_project = [
        bazelci.get_platform_for_task(k, tasks[k]) for k in tasks]

    return set([p for p in ci_platforms_for_project if p in whitelist])


def _get_clone_path(repository, platform):
    """Returns the path to a local clone of the project.

    If there's a mirror available, use that. bazel-bench will take care of
    pulling/checking out commits. Else, clone the repo.

    Args:
      repository: the URL to the git repository.
      platform: the platform on which to build the project.

    Returns:
      A path to the local clone.
    """
    mirror_path = bazelci.get_mirror_path(repository, platform)
    if os.path.exists(mirror_path):
        bazelci.eprint("Found mirror for %s on %s." % repository, platform)
        return mirror_path

    return repository


def _ci_step_for_platform_and_commits(
    bazel_binaries_paths, platform, project, extra_options, date, bucket):
    """Perform bazel-bench for the platform-project combination.
    Uploads results to BigQuery.

    Args:
        bazel_binaries_paths: a list of strings: bazel binaries to be benchmarked.
        platform: a string: the platform to benchmark on.
        project: an object: contains the information of the project to be
          tested on.
        extra_options: a string: extra bazel-bench options.
        date: the date of the commits.
        bucket: the GCP Storage bucket to upload data to.

    Return:
        An object: the result of applying bazelci.create_step to wrap the
          command to be executed by buildkite-agent.
    """
    project_clone_path = _get_clone_path(project["git_repository"], platform)
    bazel_clone_path = _get_clone_path(BAZEL_REPOSITORY, platform)

    bazel_bench_command = " ".join(
        [
            "bazel",
            "run",
            "benchmark",
            "--",
            "--bazel_binaries=%s" % ",".join(bazel_binaries_paths),
            "--bazel_source=%s" % bazel_clone_path,
            "--project_source=%s" % project_clone_path,
            "--platform=%s" % platform,
            "--collect_memory",
            "--data_directory=%s" % DATA_DIRECTORY,
            "--csv_file_name=%s" % BAZEL_BENCH_RESULT_FILENAME,
            "--collect_json_profile",
            "--aggregate_json_profiles",
            extra_options,
            "--",
            project["bazel_command"],
        ]
    )
    # TODO(leba): Upload to BigQuery too.
    # TODO(leba): Use GCP Python client instead of gsutil.
    # TODO(https://github.com/bazelbuild/bazel-bench/issues/46): Include task-specific shell commands and build flags.

    # Upload everything under DATA_DIRECTORY to Storage.
    # This includes the raw data, aggr JSON profile and the JSON profiles
    # themselves.
    storage_subdir = "{}/{}/{}/".format(
        project["storage_subdir"], date.strftime("%Y/%m/%d"), platform
    )
    upload_output_files_storage_command = " ".join(
        [
            "gsutil",
            "-m",
            "cp",
            "-r",
            "{}/*".format(DATA_DIRECTORY),
            "gs://{}/{}".format(bucket, storage_subdir),
        ]
    )
    commands = (
        [bazelci.fetch_bazelcipy_command()]
        + _bazel_bench_env_setup_command(platform)
        + [bazel_bench_command, upload_output_files_storage_command]
    )
    label = (
        bazelci.PLATFORMS[platform]["emoji-name"]
        + " Running bazel-bench on project: %s" % project["name"]
    )
    return bazelci.create_step(label, commands, platform)


def _metadata_file_content(
    project_label, project_source, command, date, platforms,
    bucket, bazel_binaries):
    """Generate the METADATA file for each project.

    Args:
        project_label: the label of the project on Storage.
        project_source: the source of the project. e.g. a GitHub link.
        command: the bazel command executed during the runs e.g. bazel build ...
        date: the date of the runs.
        platform: the platform the runs were performed on.
        bucket: the GCP Storage bucket to load METADATA from.
        bazel_binaries: the binaries to benchmark.
    Returns:
        The content of the METADATA file for the project on that date.
    """
    data_root = "https://{}.storage.googleapis.com/{}/{}".format(
        bucket, project_label, date.strftime("%Y/%m/%d")
    )

    return {
        "name": project_label,
        "project_source": project_source,
        "command": command,
        "data_root": data_root,
        "binaries": bazel_binaries,
        "platforms": [
            {
                "platform": platform,
                "perf_data": "{}/{}".format(platform, BAZEL_BENCH_RESULT_FILENAME),
                "aggr_json_profiles": "{}/{}".format(platform, AGGR_JSON_PROFILES_FILENAME),
            }
            for platform in platforms
        ],
    }


def _create_and_upload_metadata(
    project_label, project_source, command, date, platforms,
    bucket, bazel_binaries):
    """Generate the METADATA file for each project & upload to Storage.

    METADATA provides information about the runs and where to get the
    measurements. It is later used by the script that generates the daily report
    to construct the graphs.

    Args:
        project_label: the label of the project on Storage.
        project_source: the source of the project. e.g. a GitHub link.
        command: the bazel command executed during the runs e.g. bazel build ...
        date: the date of the runs.
        platform: the platform the runs were performed on.
        bucket: the GCP Storage bucket to upload data to.
   """
    metadata_file_path = "{}/{}-metadata".format(TMP, project_label)

    with open(metadata_file_path, "w") as f:
        data = _metadata_file_content(
            project_label, project_source, command, date, platforms,
            bucket, bazel_binaries)
        json.dump(data, f)

    destination = "gs://{}/{}/{}/METADATA".format(
        bucket, project_label, date.strftime("%Y/%m/%d"))
    args = ["gsutil", "cp", metadata_file_path, destination]

    try:
        subprocess.check_output(args)
        bazelci.eprint("Uploaded {}'s METADATA to {}.".format(project_label, destination))
    except subprocess.CalledProcessError as e:
        bazelci.eprint("Error uploading: {}".format(e))


def _report_generation_step(
    date, project_label, bucket, platform, report_name, update_latest=False):
    """Generate the daily report.

    Also update the path reserved for the latest report of each project.
    """
    commands = []
    commands.append(" ".join([
        "python3.6",
        "report/generate_report.py",
        "--date={}".format(date),
        "--project={}".format(project_label),
        "--storage_bucket={}".format(bucket),
        "--report_name={}".format(report_name)
    ]))

    # Copy the generated report to a special path on GCS that's reserved for
    # "latest" reports. GCS doesn't support symlink.
    if update_latest:
        date_dir = date.strftime("%Y/%m/%d")
        report_dated_path_gcs = "gs://{}/{}/{}/{}.html".format(
            bucket, project_label, date_dir, report_name)
        report_latest_path_gcs = "gs://{}/{}/report_latest.html".format(
            bucket, project_label)
        commands.append(" ".join([
            "gsutil",
            "cp",
            report_dated_path_gcs,
            report_latest_path_gcs
        ]))
    label = "Generating report on {} for project: {}.".format(
        date, project_label)
    return bazelci.create_step(label, commands, platform)


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Bazel Bench CI Pipeline")
    parser.add_argument("--date", type=str)
    parser.add_argument("--bazel_bench_options", type=str, default="")
    parser.add_argument("--bucket", type=str, default="")
    parser.add_argument("--max_commits", type=int, default="")
    parser.add_argument("--report_name", type=str, default="report")
    parser.add_argument("--update_latest", action="store_true", default=False)
    parser.add_argument("--bazel_binaries", type=str, default="")
    parsed_args = parser.parse_args(args)

    bazel_bench_ci_steps = []
    date = (
        datetime.datetime.strptime(parsed_args.date, "%Y-%m-%d").date()
        if parsed_args.date
        else datetime.date.today()
    )
    bazel_binaries = parsed_args.bazel_binaries.split(",")
    bazel_binaries_paths = ["~/{}".format(bazel_binary) for bazel_binary in bazel_binaries]

    bazel_clone_path = bazelci.clone_git_repository(
        BAZEL_REPOSITORY, STARTER_JOB_PLATFORM)

    for project in PROJECTS:
        if not project["active"]:
            continue
        platforms = _get_platforms(
            project["name"], whitelist=PLATFORMS_WHITELIST)
        for platform in platforms:
            bazel_bench_ci_steps.append(
                _ci_step_for_platform_and_commits(
                    bazel_binaries_paths, platform, project,
                    parsed_args.bazel_bench_options, date, parsed_args.bucket
                )
            )
        _create_and_upload_metadata(
            project_label=project["storage_subdir"],
            project_source=project["git_repository"],
            command=project["bazel_command"],
            date=date,
            platforms=platforms,
            bucket=parsed_args.bucket,
            bazel_binaries=bazel_binaries
        )

        bazel_bench_ci_steps.append("wait")
        # If all the above steps succeed, generate the report.
        bazel_bench_ci_steps.append(
            _report_generation_step(
                date, project["storage_subdir"],
                parsed_args.bucket, REPORT_GENERATION_PLATFORM,
                parsed_args.report_name, parsed_args.update_latest))

        bazelci.eprint(yaml.dump({"steps": bazel_bench_ci_steps}))
        subprocess.run(
            ["buildkite-agent", "pipeline", "upload"],
            input=yaml.dump({"steps": bazel_bench_ci_steps}, encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
