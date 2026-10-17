#!/usr/bin/env python3
"""Fail when any configs/*/dns.env API_IP differs from the testbed manifest.

The rest-mail instance manifest in rest-mail/testbed
(configs/restmail/manifest.yml) allocates the API's address as the `ip` of its
`api` component. Every configs/<name>/dns.env restates it as API_IP so
reference-dnsmasq can publish records pointing at it. Nothing else keeps the
copies in agreement: move the API in the manifest and the reference instances
keep publishing an address nothing listens on. This check makes that drift
loud (rest-mail/reference-mailserver#7).

The manifest is authoritative. Input that can't be checked (missing or
unparseable files, no or several `api` components, duplicate keys, missing or
repeated API_IP, anything that isn't an IP address) is a failure, never a
pass. dns.env files are parsed as data, never sourced.

Exit status: 0 when every API_IP matches, 1 otherwise.

Requires Python 3 and PyYAML.
"""

import argparse
import glob
import ipaddress
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("check-api-ip: PyYAML is required (pip install pyyaml)")

COMPONENT = "api"
ENV_KEY = "API_IP"
ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class CheckError(Exception):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    Plain safe_load keeps the last of two `ip:` keys without a word, which
    would let an ambiguous manifest pass.
    """


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise CheckError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_ip(text, where):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        raise CheckError(f"{where}: {text!r} is not an IP address") from None


def manifest_api_ip(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.load(f, Loader=UniqueKeyLoader)
    except FileNotFoundError:
        raise CheckError(
            f"manifest not found: {path} (clone rest-mail/testbed next to this "
            "repo, or set TESTBED_MANIFEST=<path>)"
        ) from None
    except OSError as e:
        raise CheckError(f"cannot read manifest {path}: {e.strerror}") from None
    except CheckError as e:
        raise CheckError(f"manifest {path}: {e}") from None
    except yaml.YAMLError as e:
        raise CheckError(f"manifest {path} is not valid YAML: {e}") from None

    if not isinstance(doc, dict):
        raise CheckError(f"manifest {path}: top level is not a mapping")
    components = doc.get("components")
    if not isinstance(components, list):
        raise CheckError(f"manifest {path}: no `components` list")
    for i, component in enumerate(components):
        if not isinstance(component, dict) or not isinstance(component.get("name"), str):
            raise CheckError(
                f"manifest {path}: components[{i}] is not a mapping with a `name`"
            )

    matches = [c for c in components if c["name"] == COMPONENT]
    if not matches:
        raise CheckError(f"manifest {path}: no `{COMPONENT}` component")
    if len(matches) > 1:
        raise CheckError(
            f"manifest {path}: {len(matches)} `{COMPONENT}` components; "
            "which one is authoritative is ambiguous"
        )
    ip = matches[0].get("ip")
    if not isinstance(ip, str):
        raise CheckError(f"manifest {path}: `{COMPONENT}` component has no `ip`")
    return parse_ip(ip, f"manifest {path} {COMPONENT}.ip")


def env_api_ip(path):
    found = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise CheckError(f"cannot read {path}: {e.strerror}") from None

    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ASSIGNMENT.match(line)
        if m is None:
            if ENV_KEY in line:
                raise CheckError(f"{path}:{n}: cannot parse {ENV_KEY} line: {line!r}")
            continue
        if m.group(1) == ENV_KEY:
            found.append((n, m.group(2)))

    if not found:
        raise CheckError(f"{path}: no {ENV_KEY}")
    if len(found) > 1:
        where = ", ".join(str(n) for n, _ in found)
        raise CheckError(f"{path}: {ENV_KEY} is set {len(found)} times (lines {where})")

    n, value = found[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return parse_ip(value, f"{path}:{n} {ENV_KEY}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check every <configs>/*/dns.env API_IP against the "
        "`api` component ip in the testbed instance manifest."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="testbed instance manifest (testbed/configs/restmail/manifest.yml)",
    )
    parser.add_argument(
        "--configs",
        default="configs",
        help="directory holding <name>/dns.env (default: configs)",
    )
    args = parser.parse_args(argv)

    try:
        want = manifest_api_ip(args.manifest)
    except CheckError as e:
        print(f"check-api-ip: {e}", file=sys.stderr)
        return 1

    envs = sorted(glob.glob(os.path.join(args.configs, "*", "dns.env")))
    if not envs:
        print(f"check-api-ip: no {args.configs}/*/dns.env to check", file=sys.stderr)
        return 1

    problems = []
    for env in envs:
        try:
            got = env_api_ip(env)
        except CheckError as e:
            problems.append(str(e))
            continue
        if got != want:
            problems.append(
                f"{env}: {ENV_KEY}={got} but {args.manifest} allocates "
                f"{COMPONENT}.ip={want}"
            )
        else:
            print(f"  {env}: {ENV_KEY}={got} matches the manifest")

    if problems:
        for problem in problems:
            print(f"check-api-ip: {problem}", file=sys.stderr)
        print(
            f"check-api-ip: the manifest is authoritative; set {ENV_KEY}={want} "
            "in the dns.env files above (or point TESTBED_MANIFEST at the "
            "right manifest)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
