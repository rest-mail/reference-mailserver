"""Fixture tests for check_api_ip.py and its Taskfile wiring.

Run: python3 -m unittest discover -s scripts -v

The Taskfile tests need `task` on PATH; docker is replaced by a stub that
records every call, so nothing touches a real daemon.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "check_api_ip.py")

sys.path.insert(0, HERE)
import check_api_ip  # noqa: E402

# Same shape as testbed configs/restmail/manifest.yml: other components carry
# IPs too, so a check that grabbed the first `ip:` would pick postgres's.
MANIFEST = """\
domain:     restmail.test
network:    testbed_mailnet
components:
  - { name: postgres, ip: 10.99.0.43 }
  - name: api
    ip: @API_IP@
    port: 8080
  - name: smtp-gateway
    ip: 10.99.0.13
    ports: { inbound: 25, submission: 587, submission_tls: 465 }
  - { name: webmail,   ip: 10.99.0.21 }
"""



def manifest(api_ip):
    return MANIFEST.replace("@API_IP@", str(api_ip))


DNS_ENV = """\
# DNS facts for the {name} instance.
MAIL_NAME={name}
MAIL_IP=10.99.0.11
POSTGRES_IP=10.99.0.41

# Testbed contract: API lives at this IP across the testbed network.
API_IP={api_ip}
"""


def committed_api_ip():
    """The API_IP configs/mail1/dns.env commits, so tests don't restate it."""
    return check_api_ip.env_api_ip(os.path.join(REPO, "configs", "mail1", "dns.env"))


class Fixture:
    """A temp dir holding a manifest and a configs/<name>/dns.env tree."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.manifest = os.path.join(tmp, "manifest.yml")
        self.configs = os.path.join(tmp, "configs")

    def write_manifest(self, text):
        with open(self.manifest, "w", encoding="utf-8") as f:
            f.write(text)

    def write_env(self, name, text):
        os.makedirs(os.path.join(self.configs, name), exist_ok=True)
        with open(os.path.join(self.configs, name, "dns.env"), "w", encoding="utf-8") as f:
            f.write(text)

    def standard(self, manifest_ip="10.99.0.20", mail1="10.99.0.20", mail2="10.99.0.20"):
        self.write_manifest(manifest(manifest_ip))
        self.write_env("mail1", DNS_ENV.format(name="mail1", api_ip=mail1))
        self.write_env("mail2", DNS_ENV.format(name="mail2", api_ip=mail2))


class CheckApiIpTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(tempfile.mkdtemp(prefix="check-api-ip-"))
        self.addCleanup(shutil.rmtree, self.fx.tmp)

    def run_check(self, manifest=None):
        return subprocess.run(
            [sys.executable, SCRIPT, "--manifest", manifest or self.fx.manifest,
             "--configs", self.fx.configs],
            capture_output=True, text=True,
        )

    def assert_passes(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def assert_fails(self, *needles, manifest=None):
        r = self.run_check(manifest)
        self.assertEqual(r.returncode, 1, f"expected failure\nstdout:{r.stdout}\nstderr:{r.stderr}")
        for needle in needles:
            self.assertIn(needle, r.stderr)
        return r

    def test_matching_manifest_passes(self):
        self.fx.standard()
        r = self.assert_passes()
        self.assertIn("mail1/dns.env: API_IP=10.99.0.20 matches", r.stdout)
        self.assertIn("mail2/dns.env: API_IP=10.99.0.20 matches", r.stdout)

    def test_quoted_env_value_passes(self):
        self.fx.standard()
        self.fx.write_env("mail2", 'API_IP="10.99.0.20"\n')
        self.assert_passes()

    def test_api_moved_in_manifest_fails_for_every_config(self):
        self.fx.standard(manifest_ip="10.99.0.30")
        r = self.assert_fails("mail1/dns.env: API_IP=10.99.0.20", "api.ip=10.99.0.30")
        self.assertIn("mail2/dns.env: API_IP=10.99.0.20", r.stderr)

    def test_one_config_drifted_fails(self):
        self.fx.standard(mail2="10.99.0.21")
        r = self.assert_fails("mail2/dns.env: API_IP=10.99.0.21")
        self.assertNotIn("mail1/dns.env: API_IP", r.stderr)

    def test_other_components_ip_is_not_mistaken_for_api(self):
        # mail1 points at postgres's address, which appears first in the manifest.
        self.fx.standard(mail1="10.99.0.43")
        self.assert_fails("mail1/dns.env: API_IP=10.99.0.43")

    def test_missing_manifest_fails(self):
        self.fx.standard()
        self.assert_fails("manifest not found", manifest=os.path.join(self.fx.tmp, "nope.yml"))

    def test_malformed_manifest_fails(self):
        self.fx.standard()
        self.fx.write_manifest("components:\n  - name: api\n    ip: [10.99.0.20\n")
        self.assert_fails("is not valid YAML")

    def test_manifest_not_a_mapping_fails(self):
        self.fx.standard()
        self.fx.write_manifest("- api\n")
        self.assert_fails("top level is not a mapping")

    def test_manifest_without_components_fails(self):
        self.fx.standard()
        self.fx.write_manifest("domain: restmail.test\n")
        self.assert_fails("no `components` list")

    def test_manifest_without_api_component_fails(self):
        self.fx.standard()
        self.fx.write_manifest("components:\n  - { name: postgres, ip: 10.99.0.20 }\n")
        self.assert_fails("no `api` component")

    def test_api_component_without_ip_fails(self):
        self.fx.standard()
        self.fx.write_manifest("components:\n  - { name: api, port: 8080 }\n")
        self.assert_fails("`api` component has no `ip`")

    def test_invalid_manifest_ip_fails(self):
        self.fx.standard(manifest_ip="10.99.0.300")
        self.assert_fails("api.ip: '10.99.0.300' is not an IP address")

    def test_unnamed_component_fails(self):
        self.fx.standard()
        self.fx.write_manifest("components:\n  - { ip: 10.99.0.20 }\n  - { name: api, ip: 10.99.0.20 }\n")
        self.assert_fails("components[0] is not a mapping with a `name`")

    def test_duplicate_api_components_fail(self):
        self.fx.standard()
        self.fx.write_manifest(
            "components:\n"
            "  - { name: api, ip: 10.99.0.20 }\n"
            "  - { name: api, ip: 10.99.0.30 }\n"
        )
        self.assert_fails("2 `api` components")

    def test_duplicate_api_components_fail_even_when_equal(self):
        self.fx.standard()
        self.fx.write_manifest(
            "components:\n"
            "  - { name: api, ip: 10.99.0.20 }\n"
            "  - { name: api, ip: 10.99.0.20 }\n"
        )
        self.assert_fails("2 `api` components")

    def test_duplicate_ip_key_in_manifest_fails(self):
        # safe_load would silently keep the last one.
        self.fx.standard()
        self.fx.write_manifest("components:\n  - name: api\n    ip: 10.99.0.30\n    ip: 10.99.0.20\n")
        self.assert_fails("duplicate key 'ip' at line 4")

    def test_duplicate_components_key_in_manifest_fails(self):
        self.fx.standard()
        self.fx.write_manifest(
            "components:\n  - { name: api, ip: 10.99.0.30 }\n"
            "components:\n  - { name: api, ip: 10.99.0.20 }\n"
        )
        self.assert_fails("duplicate key 'components'")

    def test_env_without_api_ip_fails(self):
        self.fx.standard()
        self.fx.write_env("mail2", "MAIL_NAME=mail2\n# API_IP=10.99.0.20\n")
        self.assert_fails("mail2/dns.env: no API_IP")

    def test_env_with_repeated_api_ip_fails_even_when_equal(self):
        self.fx.standard()
        self.fx.write_env("mail1", "API_IP=10.99.0.20\nAPI_IP=10.99.0.20\n")
        self.assert_fails("mail1/dns.env: API_IP is set 2 times (lines 1, 2)")

    def test_env_with_conflicting_api_ip_fails(self):
        self.fx.standard()
        self.fx.write_env("mail1", "API_IP=10.99.0.20\nexport API_IP=10.99.0.30\n")
        self.assert_fails("API_IP is set 2 times")

    def test_env_with_unparseable_api_ip_line_fails(self):
        self.fx.standard()
        self.fx.write_env("mail1", "API_IP = 10.99.0.20\n")
        self.assert_fails("cannot parse API_IP line")

    def test_env_with_invalid_ip_fails(self):
        pwned = os.path.join(self.fx.tmp, "pwned")
        for bad in ("", "10.99.0", "api.test", "010.99.0.20", "10.99.0.20 # api", f"$(touch {pwned})"):
            with self.subTest(value=bad):
                self.fx.standard(mail1=bad)
                self.assert_fails("mail1/dns.env:7 API_IP")
        self.assertFalse(os.path.exists(pwned), "dns.env must never be sourced")

    def test_no_dns_env_files_fails(self):
        self.fx.write_manifest(manifest("10.99.0.20"))
        os.makedirs(self.fx.configs)
        self.assert_fails("no " + self.fx.configs + "/*/dns.env to check")

    def test_committed_configs_parse(self):
        # The repo's own dns.env files must stay readable by the check.
        self.fx.write_manifest(manifest(committed_api_ip()))
        r = subprocess.run(
            [sys.executable, SCRIPT, "--manifest", self.fx.manifest, "--configs",
             os.path.join(REPO, "configs")],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(shutil.which("task"), "task (go-task) is not on PATH")
class TaskfileWiringTest(unittest.TestCase):
    """`task up` / `task dns:register` run the check before any docker call."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-api-ip-task-")
        self.addCleanup(shutil.rmtree, self.tmp)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.docker_log = os.path.join(self.tmp, "docker.log")
        stub = os.path.join(bindir, "docker")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/sh
                echo "$*" >> '{self.docker_log}'
                exit 0
                """))
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        for var in ("TESTBED_MANIFEST", "SKIP_API_IP_CHECK"):
            self.env.pop(var, None)
        # Config snapshots go to a throwaway run root, never the operator's.
        self.env["RUN_ROOT"] = os.path.join(self.tmp, "run")
        self.manifest = os.path.join(self.tmp, "manifest.yml")

    def write_manifest(self, api_ip):
        with open(self.manifest, "w", encoding="utf-8") as f:
            f.write(manifest(api_ip))

    def task(self, *args, env=None):
        return subprocess.run(
            ["task", *args], cwd=REPO, env=env or self.env,
            capture_output=True, text=True, timeout=60,
        )

    def docker_calls(self):
        if not os.path.exists(self.docker_log):
            return []
        with open(self.docker_log, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_up_with_drifted_manifest_fails_before_docker(self):
        self.write_manifest("10.99.0.99")
        r = self.task("up", f"TESTBED_MANIFEST={self.manifest}")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("api.ip=10.99.0.99", r.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_up_with_missing_manifest_fails_before_docker(self):
        r = self.task("up", f"TESTBED_MANIFEST={self.tmp}/absent.yml")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("manifest not found", r.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_manifest_override_via_environment(self):
        self.write_manifest("10.99.0.99")
        env = dict(self.env, TESTBED_MANIFEST=self.manifest)
        r = self.task("up", env=env)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(self.manifest, r.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_dns_register_with_matching_manifest_reaches_docker(self):
        self.write_manifest(committed_api_ip())
        # dns:register mounts dns.env from the pinned config snapshot.
        r = self.task("config:snapshot")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.task("dns:register", f"TESTBED_MANIFEST={self.manifest}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("matches the manifest", r.stdout)
        calls = self.docker_calls()
        self.assertTrue(calls and calls[0].startswith("run --rm"), calls)

    def test_skip_is_explicit_and_loud(self):
        r = self.task("check:api-ip", f"TESTBED_MANIFEST={self.tmp}/absent.yml", "SKIP_API_IP_CHECK=1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT checked", r.stderr)

    def test_skip_rejects_other_values(self):
        r = self.task("check:api-ip", f"TESTBED_MANIFEST={self.tmp}/absent.yml", "SKIP_API_IP_CHECK=yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be 1 or unset", r.stderr)


if __name__ == "__main__":
    unittest.main()
