"""Tests for config_snapshot.py and the Taskfile mounts that use it.

Run: python3 -m unittest discover -s scripts -v

The invariant under test: no long-lived container reads a path inside a
development checkout. The Taskfile tests run `task` against a throwaway copy
of this repo (with a .git marker, like a real clone) and a docker stub that
records every call, so nothing touches a real daemon, volume or run dir.
"""

import contextlib
import io
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

sys.path.insert(0, HERE)
import config_snapshot as cs  # noqa: E402

SEP = "\x1f"

# The bind mounts each daemon must take from the snapshot (env is --env-file).
DAEMON_MOUNTS = {
    "postgres": {"env", "postgres-init"},
    "postfix": {"env", "resolv.conf", "postfix-overlay"},
    "dovecot": {"env", "resolv.conf", "dovecot-overlay"},
    "rspamd": {"env", "resolv.conf", "rspamd-overlay"},
    "fail2ban": {"env", "fail2ban-overlay"},
}


def tree_bytes(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for n in files:
            p = os.path.join(dirpath, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="config-snapshot-"))
        self.addCleanup(self.rmtree, self.tmp)
        self.assertIsNone(cs.git_worktree_of(self.tmp), "tempdir is inside a git tree")
        self.checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.checkout, ".git"))
        shutil.copytree(os.path.join(REPO, "configs"), os.path.join(self.checkout, "configs"),
                        ignore=shutil.ignore_patterns("dkim"))
        self.run_root = os.path.join(self.tmp, "run")
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    @staticmethod
    def rmtree(path):
        # Snapshot files are read-only; make them removable for cleanup.
        for dirpath, dirs, files in os.walk(path):
            for n in dirs + files:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | 0o700)
        shutil.rmtree(path)

    def src(self, name="mail1"):
        return os.path.join(self.checkout, "configs", name)


class SnapshotToolTest(Fixture):
    def test_snapshot_copies_bytes_and_survives_source_changes(self):
        snap = cs.snapshot("mail1", self.src(), self.run_root)
        self.assertTrue(inside(snap, self.run_root))
        before = tree_bytes(snap)
        self.assertEqual(before, tree_bytes(self.src()))
        with open(os.path.join(self.src(), "env"), "a", encoding="utf-8") as f:
            f.write("POSTFIX_LOG_LEVEL=debug\n")
        shutil.rmtree(os.path.join(self.src(), "postgres-init"))
        self.assertEqual(tree_bytes(snap), before)
        shutil.rmtree(self.src())
        self.assertEqual(tree_bytes(snap), before)
        self.assertEqual(cs.current("mail1", self.run_root), snap)

    def test_no_symlinks_or_writable_files_in_snapshot(self):
        snap = cs.snapshot("mail1", self.src(), self.run_root)
        for dirpath, dirs, files in os.walk(snap):
            for n in dirs + files:
                p = os.path.join(dirpath, n)
                self.assertFalse(os.path.islink(p), p)
                if n in files:
                    self.assertFalse(os.lstat(p).st_mode & 0o222, p)

    def test_same_content_same_id_new_content_new_dir(self):
        a = cs.snapshot("mail1", self.src(), self.run_root)
        self.assertEqual(cs.snapshot("mail1", self.src(), self.run_root), a)
        old = tree_bytes(a)
        with open(os.path.join(self.src(), "resolv.conf"), "a", encoding="utf-8") as f:
            f.write("# changed\n")
        b = cs.snapshot("mail1", self.src(), self.run_root)
        self.assertNotEqual(a, b)
        self.assertEqual(tree_bytes(a), old, "older snapshot (maybe still mounted) changed")
        self.assertEqual(cs.current("mail1", self.run_root), b)

    def test_reused_snapshot_leaves_no_staging_behind(self):
        os.chmod(os.path.join(self.src(), "postfix-overlay"), 0o555)
        self.addCleanup(os.chmod, os.path.join(self.src(), "postfix-overlay"), 0o755)
        a = cs.snapshot("mail1", self.src(), self.run_root)
        self.assertEqual(cs.snapshot("mail1", self.src(), self.run_root), a)
        snaps = os.path.dirname(a)
        self.assertEqual(os.listdir(snaps), [os.path.basename(a)])

    def test_two_configs_are_isolated(self):
        a = cs.snapshot("mail1", self.src("mail1"), self.run_root)
        b = cs.snapshot("mail2", self.src("mail2"), self.run_root)
        self.assertTrue(inside(a, os.path.join(self.run_root, "mail1")))
        self.assertTrue(inside(b, os.path.join(self.run_root, "mail2")))
        self.assertEqual(cs.current("mail1", self.run_root), a)
        self.assertEqual(cs.current("mail2", self.run_root), b)

    def test_tampered_snapshot_is_refused_not_overwritten(self):
        snap = cs.snapshot("mail1", self.src(), self.run_root)
        env = os.path.join(snap, "env")
        os.chmod(env, 0o644)
        with open(env, "a", encoding="utf-8") as f:
            f.write("TAMPERED=1\n")
        with self.assertRaisesRegex(cs.SnapshotError, "no longer matches"):
            cs.snapshot("mail1", self.src(), self.run_root)
        with open(env, encoding="utf-8") as f:
            self.assertIn("TAMPERED=1", f.read())

    def assertRejectedWithoutSideEffects(self, regex, name="mail1", source=None, run_root=None):
        run_root = run_root or self.run_root
        existed = os.path.lexists(run_root)
        with self.assertRaisesRegex(cs.SnapshotError, regex):
            cs.snapshot(name, source or self.src(), run_root)
        if not existed:
            self.assertFalse(os.path.lexists(run_root), "rejected snapshot created RUN_ROOT")

    def test_missing_source(self):
        self.assertRejectedWithoutSideEffects("not found", source=self.src("nope"))

    def test_incomplete_source(self):
        os.remove(os.path.join(self.src(), "resolv.conf"))
        self.assertRejectedWithoutSideEffects("lacks regular file resolv.conf")

    def test_symlink_in_source(self):
        os.symlink("/etc/passwd", os.path.join(self.src(), "postfix-overlay", "leak"))
        self.assertRejectedWithoutSideEffects("symlink postfix-overlay/leak")

    def test_symlinked_source(self):
        link = os.path.join(self.tmp, "linked")
        os.symlink(self.src(), link)
        self.assertRejectedWithoutSideEffects("is a symlink", source=link)

    def test_git_dir_in_source(self):
        os.makedirs(os.path.join(self.src(), "rspamd-overlay", ".git"))
        self.assertRejectedWithoutSideEffects(r"\.git")

    def test_bad_names(self):
        for bad in ("../mail1", "a/b", "", ".hidden", "x" * 80):
            self.assertRejectedWithoutSideEffects("invalid CONFIG name", name=bad)

    def test_run_root_inside_checkout(self):
        self.assertRejectedWithoutSideEffects(
            "inside the git work tree", run_root=os.path.join(self.checkout, "run"))

    def test_symlinked_run_root_into_checkout(self):
        link = os.path.join(self.tmp, "run-link")
        os.symlink(self.checkout, link)
        self.assertRejectedWithoutSideEffects("inside the git work tree", run_root=link)
        self.assertEqual(os.listdir(self.checkout), [".git", "configs"])

    def test_relative_run_root(self):
        self.assertRejectedWithoutSideEffects("absolute", run_root="run")

    def test_run_root_with_colon(self):
        self.assertRejectedWithoutSideEffects("contains ':'", run_root=self.run_root + ":x")

    def test_group_writable_run_root(self):
        os.makedirs(self.run_root)
        os.chmod(self.run_root, 0o775)
        self.assertRejectedWithoutSideEffects("group/other-writable")
        self.assertEqual(os.listdir(self.run_root), [])

    def test_symlinked_instance_dir(self):
        os.makedirs(self.run_root, mode=0o700)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.run_root, "mail1"))
        self.assertRejectedWithoutSideEffects("is a symlink")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_other_instances_are_untouched(self):
        cs.snapshot("mail2", self.src("mail2"), self.run_root)
        other = os.path.join(self.run_root, "operator-notes")
        os.makedirs(other)
        with open(os.path.join(other, "keep"), "w", encoding="utf-8") as f:
            f.write("mine")
        before = tree_bytes(self.run_root)
        cs.snapshot("mail1", self.src("mail1"), self.run_root)
        after = tree_bytes(self.run_root)
        for k, v in before.items():
            self.assertEqual(after.get(k), v, k)

    def test_current_without_snapshot(self):
        with self.assertRaisesRegex(cs.SnapshotError, "does not exist"):
            cs.current("mail1", self.run_root)
        os.makedirs(self.run_root, mode=0o700)
        with self.assertRaisesRegex(cs.SnapshotError, "no config snapshot"):
            cs.current("mail1", self.run_root)

    def test_default_run_root_is_external(self):
        env = dict(os.environ)
        try:
            os.environ["XDG_STATE_HOME"] = "/srv/state"
            self.assertEqual(cs.default_run_root(), "/srv/state/reference-mailserver")
            os.environ.pop("XDG_STATE_HOME")
            self.assertEqual(cs.default_run_root(),
                             os.path.expanduser("~/.local/state/reference-mailserver"))
        finally:
            os.environ.clear()
            os.environ.update(env)


@unittest.skipUnless(shutil.which("task"), "task (go-task) is not on PATH")
class TaskfileMountTest(Fixture):
    """Every daemon `task up` starts mounts only its owned snapshot."""

    def setUp(self):
        super().setUp()
        for f in ("Taskfile.yml",):
            shutil.copy(os.path.join(REPO, f), self.checkout)
        shutil.copytree(os.path.join(REPO, "tasks"), os.path.join(self.checkout, "tasks"))
        shutil.copytree(os.path.join(REPO, "scripts"), os.path.join(self.checkout, "scripts"),
                        ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.log = os.path.join(self.tmp, "docker.log")
        stub = os.path.join(bindir, "docker")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/sh
                {{ for a in "$@"; do printf '%s\\037' "$a"; done; printf '\\n'; }} >> '{self.log}'
                case "$1" in
                  inspect) echo healthy ;;
                  ps) echo 0123abcd ;;
                esac
                # rspamadm dkim_keygen: write the key where -k points inside
                # the /keys mount, print a DNS record.
                case " $* " in *" dkim_keygen "*)
                  prev=; keys=; key=
                  for a in "$@"; do
                    case "$prev" in
                      -v) case "$a" in *:/keys) keys=${{a%:/keys}} ;; esac ;;
                      -k) key=${{a#/keys/}} ;;
                    esac
                    prev=$a
                  done
                  echo "PRIVATE KEY" > "$keys/$key"
                  echo '"v=DKIM1; k=rsa; " "p=ABC"'
                  ;;
                esac
                exit 0
                """))
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        for var in ("TESTBED_MANIFEST", "RUN_ROOT", "CONFIG"):
            self.env.pop(var, None)
        self.env["SKIP_API_IP_CHECK"] = "1"

    def task(self, *args, ok=True):
        r = subprocess.run(["task", *args, f"RUN_ROOT={self.run_root}"], cwd=self.checkout,
                           env=self.env, capture_output=True, text=True, timeout=120)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [line.rstrip(SEP).split(SEP) for line in f.read().splitlines()]

    def clear(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def daemons(self):
        """{container: {mount-basename: host-path}} for every `docker run -d`."""
        out = {}
        for c in self.calls():
            if c[:2] != ["run", "-d"]:
                continue
            name = c[c.index("--name") + 1]
            mounts = {}
            for i, a in enumerate(c):
                if a == "--env-file":
                    mounts["env"] = c[i + 1]
                elif a == "-v":
                    host = c[i + 1].split(":")[0]
                    if "/" in host:            # named volumes have no slash
                        mounts[os.path.basename(host)] = host
            out[name] = mounts
        return out

    def assertAllMountsInSnapshot(self, config, daemons):
        snap = cs.current(config, self.run_root)
        self.assertEqual(set(daemons), {f"mailref-{config}-{s}" for s in DAEMON_MOUNTS})
        for svc, want in DAEMON_MOUNTS.items():
            mounts = daemons[f"mailref-{config}-{svc}"]
            self.assertEqual(set(mounts), want, svc)
            for host in mounts.values():
                self.assertTrue(os.path.isabs(host), host)
                self.assertFalse(inside(host, self.checkout), f"{svc} mounts checkout path {host}")
                self.assertTrue(inside(host, snap), f"{svc} mounts {host}, not snapshot {snap}")
        return snap

    def test_up_mounts_only_the_snapshot_and_source_changes_do_not_reach_it(self):
        self.task("up", "CONFIG=mail1")
        snap = self.assertAllMountsInSnapshot("mail1", self.daemons())
        dns = [c for c in self.calls() if c[:2] == ["run", "--rm"] and "render-fragment" in c]
        self.assertEqual(len(dns), 1)
        self.assertIn(f"{snap}/dns.env:/env:ro", dns[0])
        mounted = tree_bytes(snap)

        # Mutate, then delete, the clone's config: the owned bytes don't move,
        # and a recreated container still mounts the same snapshot.
        with open(os.path.join(self.src(), "env"), "a", encoding="utf-8") as f:
            f.write("POSTFIX_LOG_LEVEL=debug\n")
        self.clear()
        self.task("postfix:restart", "CONFIG=mail1")
        self.assertEqual(self.daemons()["mailref-mail1-postfix"]["env"], f"{snap}/env")
        shutil.rmtree(os.path.join(self.checkout, "configs"))
        self.assertEqual(tree_bytes(snap), mounted)

        # Teardown needs no source config and targets mail1's names only.
        self.clear()
        self.task("destroy", "CONFIG=mail1")
        removed = [c[-1] for c in self.calls() if c[:2] == ["rm", "-f"]]
        self.assertEqual(sorted(removed), sorted(f"mailref-mail1-{s}" for s in DAEMON_MOUNTS))
        vols = [c[-1] for c in self.calls() if c[:3] == ["volume", "rm", "-f"]]
        self.assertEqual(vols, [
            "mailref-mail1-postgres-data", "mailref-mail1-maildir",
            "mailref-mail1-postfix-certs", "mailref-mail1-dovecot-certs",
        ])
        self.assertEqual(tree_bytes(snap), mounted)

    def test_two_configs_do_not_collide(self):
        self.task("up", "CONFIG=mail1")
        snap1 = self.assertAllMountsInSnapshot("mail1", self.daemons())
        before = tree_bytes(snap1)
        self.clear()
        self.task("up", "CONFIG=mail2", "HOSTNAME=mail2.test")
        snap2 = self.assertAllMountsInSnapshot("mail2", self.daemons())
        self.assertNotEqual(snap1, snap2)
        self.assertEqual(cs.current("mail1", self.run_root), snap1)
        self.assertEqual(tree_bytes(snap1), before)

    def test_refresh_is_explicit(self):
        self.task("up", "CONFIG=mail1")
        old = cs.current("mail1", self.run_root)
        with open(os.path.join(self.src(), "resolv.conf"), "a", encoding="utf-8") as f:
            f.write("# changed\n")
        # A recreate does not silently pick the edit up...
        self.clear()
        self.task("dovecot:restart", "CONFIG=mail1")
        self.assertTrue(inside(self.daemons()["mailref-mail1-dovecot"]["resolv.conf"], old))
        # ...config:snapshot pins a new one without touching containers...
        self.clear()
        r = self.task("config:snapshot", "CONFIG=mail1")
        self.assertIn("new snapshot", r.stderr)
        self.assertEqual(self.calls(), [])
        new = cs.current("mail1", self.run_root)
        self.assertNotEqual(old, new)
        self.assertTrue(os.path.isdir(old), "older snapshot was deleted")
        self.assertEqual(self.task("config:show", "CONFIG=mail1").stdout.strip(), new)
        # ...and the next recreate (or `up`) mounts it.
        self.clear()
        self.task("dovecot:restart", "CONFIG=mail1")
        self.assertTrue(inside(self.daemons()["mailref-mail1-dovecot"]["resolv.conf"], new))

    def test_dkim_provision_pins_the_key_and_recreates_rspamd_on_it(self):
        self.task("up", "CONFIG=mail1")
        old = cs.current("mail1", self.run_root)
        self.clear()
        self.task("dkim:provision", "CONFIG=mail1")
        new = cs.current("mail1", self.run_root)
        self.assertNotEqual(old, new)
        key = os.path.join(new, "rspamd-overlay", "dkim", "mail1.test.mail.key")
        with open(key, encoding="utf-8") as f:
            self.assertEqual(f.read(), "PRIVATE KEY\n")
        self.assertFalse(os.path.exists(os.path.join(old, "rspamd-overlay", "dkim")))
        daemons = self.daemons()
        self.assertEqual(set(daemons), {"mailref-mail1-rspamd"})
        for host in daemons["mailref-mail1-rspamd"].values():
            self.assertTrue(inside(host, new), host)

    def test_service_up_without_snapshot_fails_before_docker_run(self):
        for svc in DAEMON_MOUNTS:
            self.clear()
            r = self.task(f"{svc}:up", "CONFIG=mail1", ok=False)
            self.assertNotEqual(r.returncode, 0, svc)
            self.assertFalse([c for c in self.calls() if c[:1] == ["run"]], svc)

    def test_up_with_missing_source_fails_without_side_effects(self):
        r = self.task("up", "CONFIG=mail9", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not found", r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.lexists(self.run_root))

    def test_run_root_in_checkout_is_refused(self):
        r = subprocess.run(["task", "up", "CONFIG=mail1", f"RUN_ROOT={self.checkout}/run"],
                           cwd=self.checkout, env=self.env, capture_output=True, text=True,
                           timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside the git work tree", r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.lexists(os.path.join(self.checkout, "run")))


if __name__ == "__main__":
    unittest.main()
