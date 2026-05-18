# AgentLab

AgentLab ist ein lokal entwickeltes und spaeter Kubernetes-betriebenes Agent-Orchestrierungssystem fuer GitLab-Repositories. Es ist als produktionsnahes Testsystem gebaut: spezialisierte Agents koennen Aufgaben planen, kleine validierte Patches erzeugen und anwenden, Tests ausfuehren, Security-Pruefungen starten, Diffs reviewen, Merge Requests vorbereiten und alle Merge-Entscheidungen durch deterministische Policy-Gates schicken.

Wichtig: Der Code wird aktuell lokal in einem Git-Workspace entwickelt. Der spaetere Betrieb ist fuer deine Infrastruktur vorgesehen:

- Kubernetes-Cluster aus 3 Debian-13-VMs
- Ollama auf Windows 10 VM mit NVIDIA L40
- GitLab auf Debian 13 VM
- Komodo auf Debian 13 VM

Gefaehrliche Defaults sind ausgeschaltet:

- `auto_merge_enabled: false`
- `direct_main_push_enabled: false`
- `push_agent_branches_enabled: false`
- kein Force Push
- keine GitLab Tokens im LLM-Prompt
- kein Docker-Socket-Mount in den Kubernetes-Beispielen

## Teil 1: Lokale Entwicklung unter Windows/Linux

Dieser Abschnitt beschreibt die Arbeit am AgentLab-Code selbst. Das ist der aktuelle Modus: lokal entwickeln, testen, Docker-/Kubernetes-Artefakte pflegen und spaeter in die Zielumgebung deployen.

### Voraussetzungen

Windows:

- Python 3.11 oder neuer
- Git
- optional Docker Desktop oder eine entfernte Build-Umgebung
- optional `kubectl`, falls du vom Windows-Rechner aus den Cluster steuerst

Linux, zum Beispiel Debian 13:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates
```

Optional fuer Image-Builds und Cluster-Steuerung:

```bash
sudo apt install -y docker.io
# kubectl gemaess deiner Cluster-Distribution installieren
```

### Projekt lokal einrichten

Windows PowerShell:

```powershell
cd C:\Users\Fabi\IdeaProjects\agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux:

```bash
cd /pfad/zu/agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### Lokale Konfiguration

Beispielkonfiguration kopieren:

Windows:

```powershell
Copy-Item config.example.yaml config.yaml
```

Linux:

```bash
cp config.example.yaml config.yaml
```

Lokal kann AgentLab auf einen bestehenden Checkout eines Ziel-Repositories zeigen:

```yaml
gitlab_url: "https://gitlab.local"
project_id: 12345
default_branch: "main"

target_repo_path: "../target-repo"
clone_target_repo: false
workspace_root: "./runs"

ollama:
  base_url: "http://ollama.local:11434"

auto_merge_enabled: false
direct_main_push_enabled: false
push_agent_branches_enabled: false
```

GitLab Token nur als Umgebungsvariable setzen. Der Token wird nicht an Ollama uebergeben.

Windows:

```powershell
$env:GITLAB_TOKEN = "glpat-..."
```

Linux:

```bash
export GITLAB_TOKEN="glpat-..."
```

### Lokale Tests

Windows:

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m agentlab.main --help
```

Linux:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m agentlab.main --help
```

Aktueller Stand:

```text
24 passed
```

### Lokale CLI-Nutzung

Nach Aktivierung der virtuellen Umgebung:

```bash
agentlab plan --config config.yaml
agentlab run-task --config config.yaml --task task.json
agentlab full-flow --config config.yaml
agentlab review-mr --config config.yaml --mr-id 123
agentlab recover --config config.yaml --ref main --commit-sha <sha>
agentlab dry-run --config config.yaml
agentlab status --config config.yaml
agentlab status --config config.yaml --run-id <run_id> --human
agentlab watch --config config.yaml --run-id <run_id>
```

Fuer `run-task` muss die Task-Datei `"approved": true` enthalten. Das ist Absicht: Implementierung braucht eine explizite Freigabe.

### Image lokal bauen

Windows mit Docker Desktop oder Linux mit Docker:

```bash
docker build -t registry.local/agentlab:0.1.0 .
docker push registry.local/agentlab:0.1.0
```

Wenn Docker lokal nicht verfuegbar ist, kann das Image spaeter auf einer Debian-VM, in GitLab CI oder ueber eine dedizierte Build-Umgebung gebaut werden.

## Teil 2: Zielbetrieb auf Debian 13 / Kubernetes

Die spaetere Runtime ist nicht der lokale Entwicklungsworkspace, sondern dein Kubernetes-Cluster aus drei Debian-13-VMs.

Empfohlene Zielarchitektur:

```text
Kubernetes Cluster, 3x Debian 13 VMs
  Namespace: agentlab
    AgentLab Jobs
      - frischer Repo-Checkout pro Run unter /workspace/repo
      - Audit-Logs unter /var/lib/agentlab/runs
      - GitLab Token nur als Kubernetes Secret
      - keine privileged Pods
      - kein Docker Socket

Windows 10 VM mit NVIDIA L40
  Ollama
  http://ollama.local:11434

Debian 13 VM
  GitLab
  https://gitlab.local

Debian 13 VM
  Komodo
  optional fuer Betrieb, Deployments oder spaetere Job-Trigger
```

AgentLab selbst braucht keine GPU. Die Agents rufen Ollama nur ueber die lokale HTTP API auf. Die AgentLab-Runs laufen im Kubernetes-Cluster als kurzlebige Jobs, damit jeder Lauf isoliert, nachvollziehbar und wegwerfbar bleibt.

### Kubernetes-Konfiguration

Im Kubernetes-Cluster wird pro Job frisch in `/workspace/repo` geklont:

```yaml
target_repo_url: "https://gitlab.local/group/project.git"
target_repo_path: "/workspace/repo"
target_repo_ref: "main"
clone_target_repo: true
workspace_root: "/var/lib/agentlab/runs"

ollama:
  base_url: "http://ollama.local:11434"

docker_build_enabled: false
docker_compose_enabled: false
```

Wichtig: Keine Zugangsdaten in `target_repo_url` einbauen. Git-Zugriff erfolgt ueber ein Kubernetes Secret, zum Beispiel als gemountete `.netrc`.

### Kubernetes-Manifeste

Die Manifeste liegen unter:

```text
deploy/kubernetes/
```

Enthalten sind:

- Namespace `agentlab`
- ServiceAccount `agentlab-runner`
- PVC `agentlab-runs`
- ConfigMap `agentlab-config`
- Secret-Beispiel `secret.example.yaml`
- Job-Templates fuer `plan`, `dry-run`, `run-task` und `full-flow`

Vor dem Deployment `deploy/kubernetes/configmap.yaml` anpassen:

- `gitlab_url`
- `project_id`
- `target_repo_url`
- `target_repo_ref`
- `ollama.base_url`
- `protected_paths`
- Risk- und Diff-Limits
- aktivierte Test-/Scanner-Kommandos

Secret mit echten Werten erzeugen:

```bash
kubectl create namespace agentlab
kubectl -n agentlab create secret generic agentlab-secrets \
  --from-literal=GITLAB_TOKEN="glpat-..." \
  --from-literal=netrc=$'machine gitlab.local\n  login oauth2\n  password glpat-...'
```

Basisressourcen anwenden:

```bash
kubectl apply -k deploy/kubernetes
```

Planning Job starten:

```bash
kubectl apply -f deploy/kubernetes/job-plan.yaml
kubectl -n agentlab logs job/agentlab-plan -f
```

Dry Run starten:

```bash
kubectl apply -f deploy/kubernetes/job-dry-run.yaml
kubectl -n agentlab logs job/agentlab-dry-run -f
```

Einen freigegebenen Task ausfuehren:

```bash
kubectl apply -f deploy/kubernetes/task.example.configmap.yaml
kubectl apply -f deploy/kubernetes/job-run-task.yaml
kubectl -n agentlab logs job/agentlab-run-task -f
```

Vor echter Nutzung muss `task.example.configmap.yaml` durch einen freigegebenen Planning-Agent-Task ersetzt werden.

## Komponenten

- Planning Agent: analysiert Repository-Struktur, README-Dateien, TODOs, Manifeste und Tests. Aendert keinen Code.
- Implementation Agent: verarbeitet genau einen freigegebenen Task und arbeitet ausschliesslich ueber `PatchProposal`. Er erstellt `agent/<task-id>`, validiert den Patch, wendet ihn ueber `FileTool` an und committet lokal.
- MR Agent: erstellt oder aktualisiert GitLab Merge Requests mit Zusammenfassung, Checkliste, Risiko, Tests und Rollback-Hinweisen.
- Functional Test Agent: erkennt typische Testkommandos wie `python -m pytest`, `npm test`, `pnpm test`, `go test ./...` und `cargo test`.
- Build and Security Test Agent: fuehrt Docker-/Compose-Pruefungen nur aus, wenn sie aktiviert sind. Optionale Scanner wie `trivy`, `gitleaks`, `semgrep`, `bandit` und `npm audit` werden genutzt, wenn sie vorhanden sind.
- Code Quality Review Agent: prueft Lesbarkeit, Wartbarkeit, Fehlerbehandlung, Testqualitaet und unnoetige Aenderungen.
- Security and Architecture Review Agent: prueft Secrets, Auth, Injection-Risiken, Dockerfile-Risiken, Dependency-Risiken und Architekturbrueche.
- Gatekeeper: deterministische Policy Engine. Merge- und Direct-Main-Entscheidungen sind keine reine LLM-Entscheidung.
- Rollback/Recovery Agent: prueft fehlgeschlagene Pipelines und erstellt Revert-/Incident-Berichte.

## Transparenz und Live-Status

Jeder Run schreibt drei Dateien unter `workspace_root/<run_id>/`:

```text
audit.jsonl    unveraenderliches Audit-Protokoll
events.jsonl   Live-Event-Stream fuer Tools und spaetere UIs
status.json    aktueller Snapshot des Runs
```

`status.json` zeigt:

- globalen Run-Zustand: `pending`, `running`, `passed`, `blocked`, `failed`
- aktuellen Agent
- aktuelle Aktion
- Zustand pro Agent
- letzte Aktion
- Fehler, falls vorhanden
- Pfade zu Audit- und Event-Datei

Beispiele:

```bash
agentlab status --config config.yaml
agentlab status --config config.yaml --run-id <run_id> --human
agentlab watch --config config.yaml --run-id <run_id>
```

Im Kubernetes-Betrieb kannst du weiterhin die Pod-Logs verfolgen:

```bash
kubectl -n agentlab logs job/agentlab-plan -f
```

AgentLab spiegelt jedes Live-Event zusaetzlich als kurze JSON-Zeile auf `stderr`. Dadurch sieht `kubectl logs -f` sofort, welcher Agent welche Aktion startet, abschliesst, blockiert oder mit Fehler beendet. Die finale CLI-Ausgabe bleibt auf `stdout`.

Wenn du diese Live-Events in einer lokalen Shell nicht sehen willst:

Windows:

```powershell
$env:AGENTLAB_LIVE_EVENTS = "0"
```

Linux:

```bash
export AGENTLAB_LIVE_EVENTS=0
```

Fuer maschinenlesbare Transparenz sind `events.jsonl` und `status.json` die dauerhaft wichtigeren Quellen. Ein spaeterer Controller oder ein kleines Dashboard kann direkt darauf aufbauen.

## Sicherheitsmodell

- LLMs bekommen keine GitLab Tokens.
- LLMs fuehren keine Shell-Kommandos aus.
- Codeaenderungen laufen nur ueber validierte Unified Patches.
- Implementation Agent committet nicht auf den Default Branch.
- Kein Force Push.
- Keine Policy-Aenderung waehrend eines Agent-Runs.
- Kubernetes-Pods laufen als UID `10001`.
- `automountServiceAccountToken: false`
- `readOnlyRootFilesystem: true`
- Linux Capabilities werden gedroppt.
- Kein privileged Pod.
- Kein Docker-Socket-Mount.
- Audit-Logs landen unter `workspace_root/<run_id>/audit.jsonl`.
- Live-Status landet unter `workspace_root/<run_id>/status.json`.
- Live-Events landen unter `workspace_root/<run_id>/events.jsonl`.

## Docker Builds im Cluster

Die Kubernetes-Beispiele deaktivieren Docker Builds bewusst:

```yaml
docker_build_enabled: false
docker_compose_enabled: false
```

Der Grund: Ein Mount von `/var/run/docker.sock` waere praktisch, aber sicherheitstechnisch fast Root-Zugriff auf den Host. Fuer produktionsnahere Builds sollte spaeter einer dieser Wege ergaenzt werden:

- Kaniko
- rootless BuildKit
- dedizierter externer Build Runner
- GitLab CI Pipeline als Build-Gate

## Naechste Ausbaustufen

- AgentLab Image in deine interne Registry pushen.
- `configmap.yaml` auf deine lokalen Domaenen/IPs anpassen.
- Netzwerkpfade vom Kubernetes-Cluster zu GitLab und Ollama pruefen.
- Erst `job-plan.yaml`, dann `job-dry-run.yaml` ausfuehren.
- Danach einen echten Low-Risk Task als ConfigMap mounten und `job-run-task.yaml` testen.
- Spaeter: Controller bauen, der Jobs dynamisch erzeugt und Artefakte/MR-Kommentare zentral verwaltet.
