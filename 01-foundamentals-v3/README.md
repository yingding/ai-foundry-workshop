# Foundry Deep Dive

Dev Day Foundry Deep Dive sample.

> **Disclaimer:** This repository is provided for learning, and experimentation only. It is **not** an official Microsoft product, is **not** production-ready, and ships **without warranty of any kind**. Code, configurations, and prompts may change without notice. Review and adapt everything before using it in your own environment, and never commit secrets, customer data, or other sensitive information.

## 📚 Contents

- [📝 Overview](#-overview)
  - [Key SDKs](#key-sdks)
- [📓 Notebooks](#-notebooks)
  - [Notebook dependency graph](#notebook-dependency-graph)
- [🚀 Get Started](#-get-started)
  - [Prerequisites](#prerequisites)
  - [Install from source (development)](#install-from-source-development)
  - [⚠️ VS Code: notebook kernel not picking up the venv](#️-vs-code-notebook-kernel-not-picking-up-the-venv)

## 📝 Overview

A hands-on walkthrough of building enterprise-grade agents with Azure AI Foundry, exercised through Jupyter notebooks in this folder. All notebooks use **Foundry Projects v2** (`azure-ai-projects>=2.0.0`) with the `openai_client.responses` and `openai_client.evals` APIs.

The notebooks progress from single-agent construction (01–02), through multi-agent orchestration (03) and observability (05), to evaluation and safety testing (04, 06–07).

### Key SDKs

| Package | Purpose |
|---------|---------|
| `azure-ai-projects` ≥ 2.1 | Foundry Agent Services — create agents, run responses, upload files |
| `azure-ai-evaluation` ≥ 1.16 | Cloud evaluators (quality, safety, NLP metrics) and red teaming |
| `agent-framework` + `agent-framework-foundry` | Microsoft Agent Framework — multi-agent workflows |
| `azure-monitor-opentelemetry` | Distributed tracing to Application Insights |
| `gradio` ≥ 6.14 | Chat UI for interactive agent demos |

## 📓 Notebooks

| # | Notebook | Topic | Description |
|---|----------|-------|-------------|
| 01 | `01-enterprise-knowledge-agent.ipynb` | Agent + Tools | Build a Foundry prompt agent with **Bing Grounding** (live web search), **Azure AI Search** (knowledge base), and **function tools** (enterprise data). Gradio chat UI. |
| 02 | `02-medical-diagnostic-agent.ipynb` | Agent + Code Interpreter | Medical diagnostic agent with **code interpreter** + **file search**. Analyzes lung cancer patient CSV data, generates scatter/jitter charts, and synthesizes treatment recommendations. Gradio UI with streaming. |
| 03 | `03-sequential-workflow-agent.ipynb` | Multi-Agent Orchestration | Orchestrates the enterprise-knowledge-agent and med-diagnostic-agent in a sequential workflow. Two approaches: **A** — declarative YAML (`WorkflowAgentDefinition`), **B** — pro-code (`agent-framework` SDK `SequentialBuilder`). Also available as a standalone script (`03basic_workflow.py`) with DevUI. |
| 04 | `04-automated-evaluations.ipynb` | Batch Evaluation | Cloud evaluation using Foundry's **OpenAI Evals API**. Scores pre-computed agent responses with relevance, violence, BLEU, F1, and METEOR evaluators. Includes RBAC role assignment and storage network diagnostics. |
| 05 | `05-sequential-multiagent-tracing.ipynb` | Observability | **Sequential multi-agent tracing** — weather agent → conversion agent pipeline with **OpenTelemetry** spans exported to **Azure Monitor Application Insights**. |
| 06 | `06-agent-auto-eval.ipynb` | Agent-Target Evaluation | Live agent evaluation — invokes the `med-diagnostic-agent` with test queries via the Responses API and scores live responses with task adherence, coherence, fluency, and violence evaluators. |
| 07 | `07-red-team-agent.ipynb` | Red Teaming | Automated adversarial testing of `med-diagnostic-agent` using the **AI Red Teaming Agent** with PyRIT-based attack strategies (Flip, Base64, Jailbreak, IndirectJailbreak) and safety evaluators (prohibited actions, data leakage, code vulnerability, violence). |

### Notebook dependency graph

```
01 → standalone (creates enterprise-knowledge-agent)
02 → standalone (creates med-diagnostic-agent)
03 → depends on 01 + 02 (orchestrates both agents)
04 → standalone (creates seattle-tourist-agent, runs batch eval)
05 → standalone (creates weather + conversion agents)
06 → depends on 02 (evaluates med-diagnostic-agent)
07 → depends on 02 (red teams med-diagnostic-agent)
```

## 🚀 Get Started

### Prerequisites

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/) — `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Install from source (development)

Initialize a `pyproject.toml` (only the first time):

```bash
cd 01-foundamentals-v3
uv init --bare --python 3.14
```

Create the venv and sync packages from `requirements.txt`:

```bash
cd 01-foundamentals-v3

uv venv --python 3.14 && source .venv/bin/activate
uv add --dev -r requirements.txt
uv pip install -e .
uv sync
```

Register the venv as a Jupyter kernel:

```bash
.venv/bin/python -m ipykernel install --user \
  --name foundamentals-v3 \
  --display-name "Python 3.14 (foundamentals-v3)"

jupyter kernelspec list 2>/dev/null || .venv/bin/python -m jupyter kernelspec list
```

### ⚠️ VS Code: notebook kernel not picking up the venv

If you select the venv from the Python interpreter picker but the notebook kernel never starts (or the kernelspec is missing from the picker), the **Python Environments** extension (`ms-python.vscode-python-envs`) is the likely cause.

**Symptom in the Jupyter output channel** (View → Output → "Jupyter"):

```
[warn] No interpreter with path ~/.../.venv/bin/python found in Python API
[warn] Kernel Spec for 'Python 3.14 (...)' ... hidden, as we cannot find a matching interpreter argv = '~/.../python'
[error] Failed to refresh the list of interpreters
```

**Root cause:** the new `ms-python.vscode-python-envs` extension reports interpreter URIs with `~`-prefixed paths, but the Jupyter extension only matches fully resolved absolute paths. Jupyter therefore hides every kernelspec and fails to refresh interpreters.

**Fix — disable the Python Environments extension:**

1. Open the Extensions panel (`⌘⇧X`).
2. Search for **`Python Environments`** (publisher: Microsoft, id: `ms-python.vscode-python-envs`).
3. Click the gear icon → **Disable (Workspace)** (or **Disable** globally).
4. Reload the window: `⌘⇧P` → **Developer: Reload Window**.
5. Reopen the notebook, click the kernel picker (top right) → **Select Another Kernel…** → **Jupyter Kernel…** → choose **Python 3.14 (foundamentals-v3)** (the kernelspec registered above; it has an absolute path baked in).

This restores the classic Python interpreter discovery that the Jupyter extension relies on.

## 🤖 Running the Workflow Script (`03basic_workflow.py`)

### What it does

A content-review workflow with quality-based routing, built with Microsoft Agent Framework's `WorkflowBuilder`:

```
Writer → Reviewer → [score >= 80] → Publisher → Summarizer
                  → [score < 80]  → Editor → Publisher → Summarizer
```

- **Writer** generates content from user input
- **Reviewer** evaluates quality (structured JSON with scores 0–100)
- If score < 80 → **Editor** improves the content based on feedback
- **Publisher** formats the final content
- **Summarizer** creates a publication report

### Prerequisites

1. Copy `.env.example` to `.env` and fill in your Azure credentials:
   ```bash
   cp .env.example .env
   ```
2. Required env vars: `PROJECT_ENDPOINT`, `MODEL_DEPLOYMENT_NAME`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `BING_CONNECTION_NAME`, `AZURE_SEARCH_CONNECTION_NAME`, `AZURE_SEARCH_INDEX_NAME`

### Run with DevUI

```bash
cd 01-foundamentals-v3
.venv/bin/python 03basic_workflow.py
```

Opens the Agent Framework DevUI at **http://localhost:8093**. Click **Configure & Run**, fill in `role` = `user` and `contents` with your text, then click **Run Workflow**.

## 🎭 Playwright MCP (browser automation for VS Code Copilot)

The Playwright MCP server lets VS Code Copilot control a browser — navigate pages, click elements, fill forms, and read page content.

### Setup

From the workspace root:

```bash
chmod +x bootstrap_playwright.sh
./bootstrap_playwright.sh
```

This installs:
- `@playwright/test` (npm dependency to avoid npx warnings)
- Bundled Chromium via `npx playwright install chromium` (no sudo, no system changes — stored in `~/Library/Caches/ms-playwright/`)
- Chromium for `@playwright/mcp` (may use a different Playwright version)

### VS Code configuration

The MCP server is configured in `.vscode/mcp.json`:

```json
{
  "servers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--browser", "chromium"]
    }
  }
}
```

### What Copilot can do with Playwright

- Navigate to URLs (e.g., open the DevUI at localhost)
- Read page snapshots (accessibility tree) to understand UI state
- Click buttons, fill forms, select dropdowns
- Type text, press keys, handle dialogs
- Take screenshots
- Manage browser tabs

