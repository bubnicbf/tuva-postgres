# Kubernetes deployment (not applied by this repository)

These manifests define a scheduled `CronJob` that runs the production
pipeline image (see `../../Dockerfile`) once a day. **Nothing here has
been pushed, applied, or deployed by any automated process** -- these are
reviewable, versioned manifests only. Turning them into a running
workload is a deliberate, separate operator action (see
`docs/RUNBOOK.md`).

## Files

- `serviceaccount.yaml` -- a dedicated, otherwise-unprivileged
  ServiceAccount for the pipeline pod (no RBAC bindings: the pipeline
  never talks to the Kubernetes API).
- `configmap.yaml` -- every **non-secret** pipeline setting (schemas,
  timeouts, log level, the manifest URL). Edit this per environment.
- `secret.example.yaml` -- **a template, not a real Secret.** Shows the
  two required keys (`PG_DSN`, `TUVA_API_TOKEN`) with placeholder values.
  Copy it, fill in real values through your cluster's secret-management
  process (sealed-secrets, External Secrets Operator, `kubectl create
  secret` from a local, gitignored file, etc.), and never commit the
  result. It is intentionally **not** included in `kustomization.yaml`'s
  resource list, so `kubectl kustomize .` never accidentally produces an
  applyable fake secret.
- `pvc.yaml` -- a `ReadWriteOnce` PersistentVolumeClaim for the immutable
  raw landing directory (`RAW_DATA_DIR`). `ReadWriteOnce` is sufficient
  because `concurrencyPolicy: Forbid` guarantees at most one pipeline pod
  runs at a time.
- `cronjob.yaml` -- the scheduled Job itself.
- `kustomization.yaml` -- assembles the above; set a real `images:` tag
  before using this anywhere (see comments in that file).

## Image

`cronjob.yaml` references a placeholder image:
`REGISTRY_PLACEHOLDER/tuva-postgres:TAG_PLACEHOLDER`. Build and push a
real image (`docker build -t <your-registry>/tuva-postgres:<tag> .`, see
`../../Dockerfile`) and override the placeholder via
`kustomization.yaml`'s `images:` field before applying anything.

## Validating without applying

```bash
kubectl kustomize deploy/kubernetes                       # render, don't apply
kubectl apply --dry-run=client -f deploy/kubernetes/cronjob.yaml
```

Both require a `kubectl` client; see the final validation report for
whether that was available when these manifests were authored.
