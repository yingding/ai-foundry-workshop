# Foundry Deep Dive

Dev Day Foundry Deep Dive sample.

> **Disclaimer:** This repository is provided for learning, and experimentation only. It is **not** an official Microsoft product, is **not** production-ready, and ships **without warranty of any kind**. Code, configurations, and prompts may change without notice. Review and adapt everything before using it in your own environment, and never commit secrets, customer data, or other sensitive information.

## 📚 Contents

- [📝 Overview](#-overview)
- [🚀 Get Started](#-get-started)
  - [Prerequisites](#prerequisites)
  - [Install from source (development)](#install-from-source-development)
  - [⚠️ VS Code: notebook kernel not picking up the venv](#️-vs-code-notebook-kernel-not-picking-up-the-venv)

## 📝 Overview

A hands-on walkthrough of building enterprise-grade agents with Azure AI Foundry, exercised through Jupyter notebooks in this folder.

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

