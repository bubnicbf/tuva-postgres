"""Structural checks on deploy/kubernetes/*.yaml. Pure YAML parsing --
no `kubectl` required (unavailable in this sandbox; a real `kubectl
kustomize` / `--dry-run=client` pass is deferred to the final validation
report as an explicitly skipped check). These catch the concrete
regressions a reviewer could introduce by hand: a missing
concurrencyPolicy: Forbid, a privileged securityContext, a real secret
value, or the fake secret template being wired into the kustomization.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy" / "kubernetes"
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import yaml

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestKubernetesManifestsExist(unittest.TestCase):
    def test_all_expected_files_present(self):
        for name in (
            "serviceaccount.yaml", "configmap.yaml", "secret.example.yaml",
            "pvc.yaml", "cronjob.yaml", "kustomization.yaml", "README.md",
        ):
            self.assertTrue((DEPLOY_DIR / name).is_file(), f"missing {name}")

    def test_every_yaml_file_parses(self):
        for path in DEPLOY_DIR.glob("*.yaml"):
            with self.subTest(path=path.name):
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(doc, dict)


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestCronJob(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((DEPLOY_DIR / "cronjob.yaml").read_text(encoding="utf-8"))
        self.spec = self.doc["spec"]
        self.job_spec = self.spec["jobTemplate"]["spec"]
        self.pod_spec = self.job_spec["template"]["spec"]
        self.container = self.pod_spec["containers"][0]

    def test_kind_and_schedule(self):
        self.assertEqual(self.doc["kind"], "CronJob")
        self.assertRegex(self.spec["schedule"], r"^\d+ \d+ \* \* \*$")

    def test_concurrency_policy_forbid(self):
        self.assertEqual(self.spec["concurrencyPolicy"], "Forbid")

    def test_has_starting_and_active_deadlines(self):
        self.assertIsInstance(self.spec["startingDeadlineSeconds"], int)
        self.assertIsInstance(self.job_spec["activeDeadlineSeconds"], int)

    def test_bounded_history_and_retries(self):
        self.assertIsInstance(self.spec["successfulJobsHistoryLimit"], int)
        self.assertIsInstance(self.spec["failedJobsHistoryLimit"], int)
        self.assertIsInstance(self.job_spec["backoffLimit"], int)
        self.assertLessEqual(self.job_spec["backoffLimit"], 3)

    def test_restart_policy_never(self):
        self.assertEqual(self.pod_spec["restartPolicy"], "Never")

    def test_pod_security_context_restrictive(self):
        sc = self.pod_spec["securityContext"]
        self.assertTrue(sc["runAsNonRoot"])
        self.assertNotEqual(sc["runAsUser"], 0)

    def test_container_security_context_restrictive(self):
        sc = self.container["securityContext"]
        self.assertFalse(sc["allowPrivilegeEscalation"])
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertIn("ALL", sc["capabilities"]["drop"])

    def test_resource_requests_and_limits_set(self):
        resources = self.container["resources"]
        for key in ("requests", "limits"):
            self.assertIn("cpu", resources[key])
            self.assertIn("memory", resources[key])

    def test_secrets_via_secret_ref_not_inline(self):
        env_from_kinds = {list(e.keys())[0] for e in self.container["envFrom"]}
        self.assertIn("secretRef", env_from_kinds)
        self.assertIn("configMapRef", env_from_kinds)
        # no `env:` list with inline values pretending to be secrets
        rendered = (DEPLOY_DIR / "cronjob.yaml").read_text(encoding="utf-8")
        self.assertNotIn("PG_DSN:", rendered)
        self.assertNotIn("TUVA_API_TOKEN:", rendered)

    def test_uses_the_apps_own_run_command(self):
        self.assertEqual(self.container["args"], ["run"])

    def test_writable_volumes_explicitly_mounted(self):
        mount_paths = {m["mountPath"] for m in self.container["volumeMounts"]}
        self.assertIn("/app/data/raw", mount_paths)
        self.assertIn("/app/data/metrics", mount_paths)
        self.assertIn("/app/tmp", mount_paths)

    def test_image_is_a_documented_placeholder_not_a_real_pushed_tag(self):
        self.assertIn("REGISTRY_PLACEHOLDER", self.container["image"])
        self.assertIn("TAG_PLACEHOLDER", self.container["image"])


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestConfigMap(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((DEPLOY_DIR / "configmap.yaml").read_text(encoding="utf-8"))

    def test_kind(self):
        self.assertEqual(self.doc["kind"], "ConfigMap")

    def test_no_secret_keys_present(self):
        for forbidden in ("PG_DSN", "TUVA_API_TOKEN"):
            self.assertNotIn(forbidden, self.doc["data"])

    def test_insecure_http_not_enabled(self):
        self.assertNotIn("TUVA_API_ALLOW_INSECURE_HTTP", self.doc["data"])

    def test_manifest_url_is_https(self):
        self.assertTrue(self.doc["data"]["TUVA_API_MANIFEST_URL"].startswith("https://"))


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestSecretExample(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((DEPLOY_DIR / "secret.example.yaml").read_text(encoding="utf-8"))

    def test_kind_and_placeholder_only_values(self):
        self.assertEqual(self.doc["kind"], "Secret")
        for value in self.doc["stringData"].values():
            self.assertIn("REPLACE_ME", value)

    def test_not_referenced_by_kustomization(self):
        kustomization = yaml.safe_load((DEPLOY_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("secret.example.yaml", kustomization["resources"])


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestPVC(unittest.TestCase):
    def test_access_mode_read_write_once(self):
        doc = yaml.safe_load((DEPLOY_DIR / "pvc.yaml").read_text(encoding="utf-8"))
        self.assertEqual(doc["spec"]["accessModes"], ["ReadWriteOnce"])


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestKustomization(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((DEPLOY_DIR / "kustomization.yaml").read_text(encoding="utf-8"))

    def test_references_the_core_resources(self):
        for name in ("serviceaccount.yaml", "configmap.yaml", "pvc.yaml", "cronjob.yaml"):
            self.assertIn(name, self.doc["resources"])

    def test_image_placeholder_present(self):
        image = self.doc["images"][0]
        self.assertIn("PLACEHOLDER", image["newName"] + image["newTag"])


if __name__ == "__main__":
    unittest.main()
