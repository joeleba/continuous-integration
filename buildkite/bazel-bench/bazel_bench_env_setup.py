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
"""To be executed on each docker container that runs the tasks.

Clones the repository and downloads available bazel binaries.

"""
import argparse
import bazelci
import os
import sys
import subprocess


BB_ROOT = os.path.join(os.path.expanduser("~"), ".bazel-bench")
# The path to the directory that stores the bazel binaries.
BAZEL_BINARY_BASE_PATH = os.path.join(BB_ROOT, "bazel-bin")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Bazel Bench Environment Setup")
    parser.add_argument("--platform", type=str)
    parser.add_argument("--gs_uri", type=str)
    args = parser.parse_args(argv)

    bazel_binaries = args.bazel_binaries.split(",")
    #bazel_bin_dir = BAZEL_BINARY_BASE_PATH

    if not os.path.exists(BAZEL_BINARY_BASE_PATH):
      os.makedirs(BAZEL_BINARY_BASE_PATH)
    args = [
        "gsutil",
        "-m",
        "cp",
        "-r",
        "gs://perf.bazel.build/bazelbins/*",
        "{}/".format(BAZEL_BINARY_BASE_PATH)
    ]
    subprocess.call(args)

if __name__ == "__main__":
    sys.exit(main())
