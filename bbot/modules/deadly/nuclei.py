import json
import yaml
import subprocess
from pathlib import Path
from itertools import islice
from bbot.modules.base import BaseModule


class nuclei(BaseModule):

    watched_events = ["URL", "TECHNOLOGY"]
    produced_events = ["VULNERABILITY"]
    flags = ["active", "aggressive", "web-advanced"]
    meta = {"description": "Fast and customisable vulnerability scanner"}

    batch_size = 100
    options = {
        "version": "2.7.7",
        "tags": "",
        "templates": "",
        "severity": "",
        "ratelimit": 150,
        "concurrency": 25,
        "mode": "severe",
        "etags": "intrusive",
        "budget": 1,
    }
    options_desc = {
        "version": "nuclei version",
        "tags": "execute a subset of templates that contain the provided tags",
        "templates": "template or template directory paths to include in the scan",
        "severity": "Filter based on severity field available in the template.",
        "ratelimit": "maximum number of requests to send per second (default 150)",
        "concurrency": "maximum number of templates to be executed in parallel (default 25)",
        "mode": "technology | severe | manual. Technology: Only activate based on technology events that match nuclei tags. On by default. Severe: Only critical and high severity templates without intrusive. Manual: Fully manual settings",
        "etags": "tags to exclude from the scan",
        "budget": "Used in budget mode to set the number of requests which will be alloted to the nuclei scan",
    }
    deps_ansible = [
        {
            "name": "Download nuclei",
            "unarchive": {
                "src": "https://github.com/projectdiscovery/nuclei/releases/download/v#{BBOT_MODULES_NUCLEI_VERSION}/nuclei_#{BBOT_MODULES_NUCLEI_VERSION}_linux_amd64.zip",
                "include": "nuclei",
                "dest": "#{BBOT_TOOLS}",
                "remote_src": True,
            },
        }
    ]
    deps_pip = ["pyyaml"]
    in_scope_only = True

    def setup(self):

        # attempt to update nuclei templates
        nulcei_templates_dir = f"{self.helpers.tools_dir}/nuclei-templates"
        update_results = self.helpers.run(["nuclei", "-update-directory", nulcei_templates_dir, "-update-templates"])
        if update_results.stderr:
            if "Successfully downloaded nuclei-templates" in update_results.stderr:
                self.hugesuccess("Successfully updated nuclei templates")
            elif "No new updates found for nuclei templates" in update_results.stderr:
                self.hugeinfo("Nuclei templates already up-to-date")
            else:
                self.hugewarning("Failure while updating nuclei templates")
        else:
            self.hugewarning("Error running nuclei template update command")

        self.templates = self.config.get("templates")
        self.tags = self.config.get("tags")
        self.etags = self.config.get("etags")
        self.severity = self.config.get("severity")
        self.iserver = self.scan.config.get("interactsh_server", None)
        self.itoken = self.scan.config.get("interactsh_token", None)

        if self.config.get("mode") not in ("technology", "severe", "manual", "budget"):
            self.warning(f"Unable to intialize nuclei: invalid mode selected: [{self.config.get('mode')}]")
            return False

        if self.config.get("mode") == "technology":
            self.info(
                "Running nuclei in TECHNOLOGY mode. Scans will only be performed with the --automatic-scan flag set. This limits the templates used to those that match wappalyzer signatures"
            )
            self.tags = ""

        if self.config.get("mode") == "severe":
            self.info(
                "Running nuclei in SEVERE mode. Only critical and high severity templates will be used. Tag setting will be IGNORED."
            )
            self.severity = "critical,high"
            self.tags = ""

        if self.config.get("mode") == "manual":
            self.info(
                "Running nuclei in MANUAL mode. Settings will be passed directly into nuclei with no modification"
            )

        if self.config.get("mode") == "budget":
            self.info(
                f"Running nuclei in BUDGET mode. This mode calculates which nuclei templates can be used, constrained by your 'budget' of number of requests. Current budget is set to: {self.config.get('budget')}"
            )

            self.hugeinfo("Processing nuclei templates to perform budget calculations...")

            self.nucleibudget = NucleiBudget(self.config.get("budget"), nulcei_templates_dir)
            self.budget_templates_file = self.helpers.tempfile(self.nucleibudget.collapsable_templates, pipe=False)

            self.hugeinfo(
                f"Loaded [{str(sum(self.nucleibudget.severity_stats.values()))}] templates based on a budget of [{str(self.config.get('budget'))}] request(s)"
            )
            self.hugeinfo(
                f"Template Severity: Critical [{self.nucleibudget.severity_stats['critical']}] High [{self.nucleibudget.severity_stats['high']}] Medium [{self.nucleibudget.severity_stats['medium']}] Low [{self.nucleibudget.severity_stats['low']}] Info [{self.nucleibudget.severity_stats['info']}] Unknown [{self.nucleibudget.severity_stats['unknown']}]"
            )

        return True

    def handle_batch(self, *events):

        nuclei_input = [str(e.data) for e in events]
        for severity, template, host, name in self.execute_nuclei(nuclei_input):
            source_event = self.correlate_event(events, host)
            if source_event == None:
                continue
            self.emit_event(
                {
                    "severity": severity,
                    "host": str(source_event.host),
                    "url": host,
                    "description": f"template: {template}, name: {name}",
                },
                "VULNERABILITY",
                source_event,
            )

    def correlate_event(self, events, host):
        for event in events:
            if host in event:
                return event
        self.warning("Failed to correlate nuclei result with event")

    def execute_nuclei(self, nuclei_input):

        command = [
            "nuclei",
            "-silent",
            "-json",
            "-update-directory",
            f"{self.helpers.tools_dir}/nuclei-templates",
            "-rate-limit",
            self.config.get("ratelimit"),
            "-concurrency",
            str(self.config.get("concurrency")),
            "-duc",
            # "-r",
            # self.helpers.resolver_file,
        ]

        for cli_option in ("severity", "templates", "iserver", "itoken", "etags"):
            option = getattr(self, cli_option)

            if option:
                command.append(f"-{cli_option}")
                command.append(option)

        setup_tags = getattr(self, "tags")
        if setup_tags:
            command.append(f"-tags")
            command.append(setup_tags)

        if self.scan.config.get("interactsh_disable") == True:
            self.info("Disbling interactsh in accordance with global settings")
            command.append("-no-interactsh")

        if self.config.get("mode") == "technology":
            command.append("-as")

        if self.config.get("mode") == "budget":
            command.append("-t")
            command.append(self.budget_templates_file)

        for line in self.helpers.run_live(command, input=nuclei_input, stderr=subprocess.DEVNULL):
            try:
                j = json.loads(line)
            except json.decoder.JSONDecodeError:
                self.debug(f"Failed to decode line: {line}")
                continue
            template = j.get("template-id", "")

            # try to get the specific matcher name
            name = j.get("matcher-name", "")

            # fall back to regular name
            if not name:
                self.debug(
                    f"Couldn't get matcher-name from nuclei json, falling back to regular name. Template: [{template}]"
                )
                name = j.get("info", {}).get("name", "")

            severity = j.get("info", {}).get("severity", "").upper()
            host = j.get("host", "")

            if template and name and severity and host:
                yield (severity, template, host, name)
            else:
                self.debug("Nuclei result missing one or more required elements, not reporting. JSON: ({j})")

    def cleanup(self):
        resume_file = self.helpers.current_dir / "resume.cfg"
        resume_file.unlink(missing_ok=True)


class NucleiBudget:
    def __init__(self, budget, templates_dir):
        self.templates_dir = templates_dir
        self.yaml_list = self.get_yaml_list()
        self.budget_paths = self.find_budget_paths(budget)
        self.collapsable_templates, self.severity_stats = self.find_collapsable_templates()

    def get_yaml_list(self):
        return list(Path(self.templates_dir).rglob("*.yaml"))

    # Given the current budget setting, scan all of the templates for paths, sort them by frequency and select the first N (budget) items
    def find_budget_paths(self, budget):
        path_frequency = {}
        for yf in self.yaml_list:
            if yf:
                for paths in self.get_yaml_request_attr(yf, "path"):
                    for path in paths:
                        if path in path_frequency.keys():
                            path_frequency[path] += 1
                        else:
                            path_frequency[path] = 1

        sorted_dict = dict(sorted(path_frequency.items(), key=lambda item: item[1], reverse=True))
        return list(dict(islice(sorted_dict.items(), budget)).keys())

    def get_yaml_request_attr(self, yf, attr):
        p = self.parse_yaml(yf)
        requests = p.get("requests", [])
        for r in requests:
            raw = r.get("raw")
            if not raw:
                res = r.get(attr)
                yield res

    def get_yaml_info_attr(self, yf, attr):
        p = self.parse_yaml(yf)
        info = p.get("info", [])
        res = info.get(attr)
        yield res

    # Parse through all templates and locate those which match the conditions necessary to collapse down to the budget setting
    def find_collapsable_templates(self):
        collapsable_templates = []
        severity_dict = {}
        for yf in self.yaml_list:
            valid = True
            if yf:
                for paths in self.get_yaml_request_attr(yf, "path"):
                    if set(paths).issubset(self.budget_paths):

                        headers = self.get_yaml_request_attr(yf, "headers")
                        for header in headers:
                            if header:
                                valid = False

                        method = self.get_yaml_request_attr(yf, "method")
                        for m in method:
                            if m != "GET":
                                valid = False

                        max_redirects = self.get_yaml_request_attr(yf, "max-redirects")
                        for mr in max_redirects:
                            if mr:
                                valid = False

                        redirects = self.get_yaml_request_attr(yf, "redirects")
                        for rd in redirects:
                            if rd:
                                valid = False

                        cookie_reuse = self.get_yaml_request_attr(yf, "cookie-reuse")
                        for c in cookie_reuse:
                            if c:
                                valid = False

                        if valid:
                            collapsable_templates.append(str(yf))
                            severity_gen = self.get_yaml_info_attr(yf, "severity")
                            severity = next(severity_gen)
                            if severity in severity_dict.keys():
                                severity_dict[severity] += 1
                            else:
                                severity_dict[severity] = 1
        return collapsable_templates, severity_dict

    def parse_yaml(self, yamlfile):
        with open(yamlfile, "r") as stream:
            try:
                y = yaml.safe_load(stream)
                return y
            except yaml.YAMLError as e:
                self.debug(f"failed to read yaml file: {e}")
