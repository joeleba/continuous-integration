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
Generates a daily HTML report for the projects.
The steps:
  1. Get the necessary data from Storage for projects/date.
  2. Manipulate the data to a format suitable for graphs.
  3. Generate a HTML report containing the graphs.
  4. Upload the generated HTMLs to GCP Storage.
"""
import argparse
import collections
import csv
import datetime
import json
import io
import os
import statistics
import subprocess
import sys
import tempfile
import urllib.request


# TODO(leba): Include JSON profiles data.
TMP = tempfile.gettempdir()
REPORTS_DIRECTORY = os.path.join(TMP, ".bazel_bench", "reports")
EVENTS_ORDER = [
    "Launch Blaze",
    "Initialize command",
    "Load packages",
    "Analyze dependencies",
    "Analyze licenses",
    "Prepare for build",
    "Build artifacts",
    "Complete build",
]

def _upload_to_storage(src_file_path, storage_bucket, destination_dir):
    """Uploads the file from src_file_path to the specified location on Storage.
    """
    args = ["gsutil", "cp", src_file_path, "gs://{}/{}".format(storage_bucket, destination_dir)]
    subprocess.run(args)


def _load_csv_from_remote_file(http_url):
    print(http_url)
    with urllib.request.urlopen(http_url) as resp:
        reader = csv.DictReader(io.TextIOWrapper(resp))
        return [row for row in reader]


def _load_json_from_remote_file(http_url):
    with urllib.request.urlopen(http_url) as resp:
        data = resp.read()
        encoding = resp.info().get_content_charset("utf-8")
        return json.loads(data.decode(encoding))


def _get_storage_url(storage_bucket, dated_subdir):
    return "https://{}.storage.googleapis.com/{}".format(storage_bucket, dated_subdir)


def _get_dated_subdir_for_project(project, date):
    return "{}/{}".format(project, date.strftime("%Y/%m/%d"))


def _get_proportion_breakdown(aggr_json_profile):
    bazel_commit_to_phases = {}
    for entry in aggr_json_profile:
        bazel_commit = entry["bazel_source"]
        if bazel_commit not in bazel_commit_to_phases:
            bazel_commit_to_phases[bazel_commit] = []
        bazel_commit_to_phases[bazel_commit].append({
            "name": entry["name"],
            "dur": entry["dur"]
        })

    bazel_commit_to_phase_proportion = {}
    for bazel_commit in bazel_commit_to_phases.keys():
        total_time = sum(
                [float(entry["dur"]) for entry in bazel_commit_to_phases[bazel_commit]])
        bazel_commit_to_phase_proportion[bazel_commit] = {
                entry["name"]: float(entry["dur"]) / total_time
                for entry in bazel_commit_to_phases[bazel_commit]}

    return bazel_commit_to_phase_proportion


def _fit_data_to_phase_proportion(reading, proportion_breakdown):
    result = []
    for phase in EVENTS_ORDER:
        if phase not in proportion_breakdown:
            result.append(0)
        else:
            result.append(reading * proportion_breakdown[phase])
    return result


def _prepare_data_for_graph(performance_data, aggr_json_profile):
    """Massage the data to fit a format suitable for graph generation.
    TODO(leba): Add hyperlink to each bazel commit.
    """
    bazel_commit_to_phase_proportion = _get_proportion_breakdown(
            aggr_json_profile)
    ordered_commit_to_readings = collections.OrderedDict()
    for entry in performance_data:
        bazel_commit = entry["bazel_commit"]
        if bazel_commit not in ordered_commit_to_readings:
            ordered_commit_to_readings[bazel_commit] = {
                "bazel_commit": bazel_commit,
                "wall_readings": [],
                "memory_readings": [],
            }
        ordered_commit_to_readings[bazel_commit]["wall_readings"].append(float(entry["wall"]))
        ordered_commit_to_readings[bazel_commit]["memory_readings"].append(float(entry["memory"]))

    wall_data = [["Bazel Commit"] + EVENTS_ORDER]
    memory_data = [["Bazel Commit", "Memory (MB)"]]

    for obj in ordered_commit_to_readings.values():
        wall_data.append(
                [obj["bazel_commit"]]
                + _fit_data_to_phase_proportion(
                        statistics.median(
                                obj["wall_readings"]),
                        bazel_commit_to_phase_proportion[bazel_commit]))
        memory_data.append([obj["bazel_commit"], statistics.median(obj["memory_readings"])])

    return wall_data, memory_data


def _row_component(content):
    return """
<div class="row">{content}</div>
""".format(content=content)


def _col_component(col_class, content):
    return """
<div class="{col_class}">{content}</div>
""".format(col_class=col_class, content=content)


def _single_graph(metric, metric_label, data, platform):
    """Returns the HTML <div> component of a single graph.
    """
    title = "[{}] Bar Chart of {} vs Bazel commits".format(platform, metric_label)
    vAxis = "Bazel Commits (chronological order)"
    hAxis = metric_label
    chart_id = "{}-{}".format(platform, metric)

    return """
<script type="text/javascript">
    google.charts.setOnLoadCallback(drawChart);
    function drawChart() {{
      var data = google.visualization.arrayToDataTable({data})

      var options = {{
        title: "{title}",
        hAxis: {{
          title: "{hAxis}",
          minValue: 0,
        }},
        vAxis: {{
          title: "{vAxis}"
        }},
        bars: "horizontal",
        axes: {{
          y: {{
            0: {{ side: "right"}}
          }}
        }},
        isStacked: true
      }};
      var chart = new google.visualization.BarChart(document.getElementById("{chart_id}"));
      chart.draw(data, options);
  }}
  </script>
<div id="{chart_id}" style="height: 800px"></div>
""".format(
        title=title, data=data, hAxis=hAxis, vAxis=vAxis, chart_id=chart_id
    )


def _full_report(project, date, graph_components):
    """Returns the full HTML of a complete report, from the graph components.
    """
    return """
<html>
  <head>
    <script type="text/javascript" src="https://www.gstatic.com/charts/loader.js"></script>
    <script type="text/javascript">
      google.charts.load("current", {{ packages:["corechart"] }});
    </script>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/css/bootstrap.min.css" integrity="sha384-ggOyR0iXCbMQv3Xipma34MD+dH/1fQ784/j6cY/iJTQUOhcWr7x9JvoRxT2MZw1T" crossorigin="anonymous">
  </head>
  <body style="font-family: Roboto;">
    <div class="container-fluid">
      <div class="row">
        <div class="col-sm-12">
          <h1>[{project}] Report for {date}</h1>
        </div>
      </div>
      {graphs}
    </div>
  </body>
</html>
""".format(
        project=project, date=date, graphs=graph_components
    )


def _generate_report_for_date(project, date, storage_bucket):
    """Generates a html report for the specified date & project.

    Args:
      project: the project to generate report for. Check out bazel_bench.py.
      date: the date to generate report for.
      storage_bucket: the Storage bucket to upload the report to.
    """
    dated_subdir = _get_dated_subdir_for_project(project, date)
    root_storage_url = _get_storage_url(storage_bucket, dated_subdir)
    metadata_file_url = "{}/METADATA".format(root_storage_url)
    metadata = _load_json_from_remote_file(metadata_file_url)

    graph_components = []
    for platform_measurement in metadata["platforms"]:
        # Get the data
        performance_data = _load_csv_from_remote_file(
            "{}/{}".format(root_storage_url, platform_measurement["perf_data"])
        )
        aggr_json_profile = _load_csv_from_remote_file(
            "{}/{}".format(root_storage_url, platform_measurement["aggr_json_profiles"])
        )

        wall_data, memory_data = _prepare_data_for_graph(
            performance_data, aggr_json_profile)

        # Generate a graph for that platform.
        row_content = []
        row_content.append(
            _col_component("col-sm-10", _single_graph(
                metric="wall",
                metric_label="Wall Time (s)",
                data=wall_data,
                platform=platform_measurement["platform"],
            ))
        )

        row_content.append(
            _col_component("col-sm-10", _single_graph(
                metric="memory",
                metric_label="Memory (MB)",
                data=memory_data,
                platform=platform_measurement["platform"],
            ))
        )
        graph_components.append(
                _row_component(
                        _col_component(
                                "col-sm-5",
                                "<h2>{}</h2>".format(
                                        platform_measurement["platform"]))))
        graph_components.append(_row_component("\n".join(row_content)))

    content = _full_report(project, date, "\n".join(graph_components))

    if not os.path.exists(REPORTS_DIRECTORY):
        os.makedirs(REPORTS_DIRECTORY)

    report_tmp_file = "{}/report_{}_{}.html".format(
        REPORTS_DIRECTORY, project, date.strftime("%Y%m%d")
    )
    with open(report_tmp_file, "w") as fo:
        fo.write(content)

    #if storage_bucket:
#        _upload_to_storage(report_tmp_file, storage_bucket, dated_subdir + "/report.html")
    #else:
    #    print(content)


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Bazel Bench Daily Report")
    parser.add_argument("--date", type=str, help="Date in YYYY-mm-dd format.")
    parser.add_argument(
        "--project",
        action="append",
        help=(
            "Projects to generate report for. Use the storage_subdir defined" "in bazel_bench.py."
        ),
    )
    parser.add_argument("--storage_bucket", help="The GCP Storage bucket to upload the reports to.")
    parsed_args = parser.parse_args(args)

    date = (
        datetime.datetime.strptime(parsed_args.date, "%Y-%m-%d").date()
        if parsed_args.date
        else datetime.date.today()
    )

    for project in parsed_args.project:
        _generate_report_for_date(project, date, parsed_args.storage_bucket)


if __name__ == "__main__":
    sys.exit(main())
